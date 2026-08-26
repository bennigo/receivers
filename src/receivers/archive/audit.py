"""Archive audit — reconstruct 'what needs fixing' from the archive itself.

Re-rinex resume state lives in a LOCAL staging tree, so a different host has
no idea what was already regenerated or what failed. This module derives the
worklist from the archive (the ground truth): walk a station/session's rinex
dirs on a read-only archive mount and flag, per file,

  * ``bad-name``   — convention-breaking products (lowercase ``.d.Z``/``.o.Z``,
                     bare ``.d``/``.o``, tmp leftovers). Junk: the convention
                     is ``STA<ddd><s>.YYD.Z`` (uppercase Hatanaka + .Z).
  * ``bad-magic``  — ``.Z`` bytes that aren't LZW compress (the gzip-as-.Z
                     era). Content is fine — remediation is recompression
                     (fix_gzip_z_to_lzw.sh / re-push), NOT deletion.
  * ``unreadable`` — (``--deep``) the file fails to decompress. Junk +
                     regenerate from raw.
  * ``old-version``— (``--check-version``) the product is still RINEX 2
                     (CRINEX v1 first line) → regeneration candidate.
  * ``missing``    — a raw file exists for the date but no valid rinex
                     product does (daily sessions only).

The report ends in two ready-to-run commands: ``archive-rm`` for the junk and
``rinex --dates ... --force`` for the regeneration candidates — so an audit on
ANY host reproduces the campaign state without local knowledge.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

from .format_guard import bad_z_format

logger = logging.getLogger("receivers.archive.audit")

# STA + DOY + session letter (0 daily, a-x hourly) + .YYD.Z (uppercase Hatanaka)
VALID_OBS_RE = re.compile(r"^[A-Z0-9]{4}(\d{3})([0a-x])\.(\d{2})D\.Z$")
# Raw daily files: STA + YYYYMMDD + anything + known raw extension
RAW_DATE_RE = re.compile(
    r"^[A-Z0-9]{4}(\d{8}).*\.(T02|T00|t02|t00|sbf|SBF|m00|M00)(\.gz)?$"
)

MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


@dataclass
class AuditFinding:
    rel_path: str  # archive-relative (feeds archive-rm directly)
    # bad-name | bad-magic | unreadable | old-version | missing |
    # session-less-layout
    issue: str
    detail: str
    size: int = 0
    file_date: Optional[date] = None
    junk: bool = False  # deletable (archive-rm candidate)
    regen: bool = False  # date should be re-rinexed from raw


@dataclass
class AuditReport:
    station: str
    session: str
    scanned: int = 0
    clean: int = 0
    findings: List[AuditFinding] = field(default_factory=list)

    @property
    def junk_paths(self) -> List[str]:
        return [f.rel_path for f in self.findings if f.junk]

    @property
    def junk_max_size(self) -> int:
        sizes = [f.size for f in self.findings if f.junk]
        return max(sizes) if sizes else 0

    @property
    def regen_dates(self) -> List[date]:
        return sorted({f.file_date for f in self.findings if f.regen and f.file_date})

    def counts(self) -> dict:
        out: dict = {}
        for f in self.findings:
            out[f.issue] = out.get(f.issue, 0) + 1
        return out


def _obs_date(fname: str) -> Optional[date]:
    """Date from a convention (or near-convention) obs filename via DOY+YY."""
    m = re.match(r"^[A-Z0-9]{4}(\d{3})[0a-x]?\.(\d{2})", fname, re.IGNORECASE)
    if not m:
        return None
    doy, yy = int(m.group(1)), int(m.group(2))
    year = 1900 + yy if yy >= 80 else 2000 + yy
    try:
        return date(year, 1, 1) + timedelta(days=doy - 1)
    except (ValueError, OverflowError):
        return None


def _rinex_version_class(path: Path) -> Optional[int]:
    """2 or 3 from the CRINEX first line (v1 ↔ RINEX2, v3 ↔ RINEX3).

    Reads only the head of the stream (zcat handles gzip AND LZW). None when
    undeterminable — callers must not flag what they cannot prove.
    """
    try:
        proc = subprocess.Popen(
            ["zcat", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        head = proc.stdout.read(80) if proc.stdout else b""
        proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if b"COMPACT RINEX" not in head:
        return None
    lead = head.strip()[:3]
    if lead.startswith(b"1"):
        return 2
    if lead.startswith(b"3"):
        return 3
    return None


def _decompress_ok(path: Path) -> bool:
    """Full-stream decompression test (works for gzip and LZW via zcat)."""
    try:
        res = subprocess.run(
            ["zcat", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _classify_bad_name(fname: str) -> str:
    low = fname.lower()
    if low.endswith((".tmp", ".fixtmp", ".plain")):
        return "tool leftover"
    if re.search(r"\.\d{2}o\.z$", low):
        return "uncompacted obs under .Z (Hatanaka failed?)"
    if re.search(r"\.\d{2}[do]$", low):
        return "uncompressed product"
    if re.search(r"\.\d{2}d\.z$", fname) and not re.search(r"\.\d{2}D\.Z$", fname):
        return "lowercase .d.Z (pre-migration naming)"
    return "unrecognized name for a rinex product dir"


def audit_station_session(
    source_root: Path,
    station: str,
    session: str,
    *,
    years: Optional[Set[int]] = None,
    deep: bool = False,
    check_version: bool = False,
    check_missing: bool = True,
    check_identity: bool = False,
    progress=None,
) -> AuditReport:
    """Audit one station/session across the archive tree. Read-only.

    With ``check_identity`` each valid RINEX product is also probed (header
    only) for a stray position (closest to a different station → issue
    ``stray``, fix via ``archive-sort``) and for stacking (multiple RINEX
    documents concatenated → issue ``stacked``, fix via a clean re-rinex).
    Report-only: neither is auto-repaired.
    """
    station = station.upper()
    report = AuditReport(station=station, session=session)

    # Fleet geometry + gate for the identity probe — resolved once. Fail-open.
    _fleet: dict = {}
    _gate_m = 10.0
    if check_identity:
        try:
            from .sort import fleet_coordinates as _fc
            from .sort import resolve_position_gate_m as _rg

            _fleet = _fc()
            _gate_m = _rg()
        except Exception:  # noqa: BLE001
            check_identity = False

    rinex_dirs: List[Path] = []
    stray_layout_dirs: List[Path] = []
    for ydir in sorted(source_root.iterdir()) if source_root.is_dir() else []:
        if not (ydir.is_dir() and ydir.name.isdigit()):
            continue
        if years and int(ydir.name) not in years:
            continue
        for mon in MONTHS:
            d = ydir / mon / station / session / "rinex"
            if d.is_dir():
                rinex_dirs.append(d)
            # A SESSION-LESS sibling — YYYY/mon/STA/{raw,rinex}/ with no
            # session segment. Every walk in this codebase descends through a
            # session, so such a directory is invisible everywhere: absent from
            # archive_catalog (0 rows fleet-wide) and skipped by both
            # archive-sort scans as well as this audit. Report it rather than
            # walk it — 73 exist (2019-2023, TKJS/TKJ2/TORK/GONH/SEY9/JONC),
            # 56 of them byte-identical duplicates but 16 holding 82 files
            # that exist nowhere else plus 19 that differ from their canonical
            # twin. Walking them would enumerate the duplicates as findings;
            # what is wanted is that they stop being invisible.
            for kind in ("rinex", "raw"):
                sd = ydir / mon / station / kind
                if sd.is_dir():
                    stray_layout_dirs.append(sd)

    for sd in stray_layout_dirs:
        try:
            n = sum(1 for f in sd.iterdir() if f.is_file())
        except OSError:
            n = 0
        report.findings.append(
            AuditFinding(
                rel_path=str(sd.relative_to(source_root)),
                issue="session-less-layout",
                detail=(
                    f"{n} file(s) in YYYY/mon/{station}/{sd.name}/ — no session "
                    "segment, so this directory is invisible to every archive "
                    "walk and absent from archive_catalog. NOT auto-handled: "
                    "some such dirs hold files that exist nowhere else. Verify "
                    "against the canonical session dir before doing anything."
                ),
                size=0,
                # Never junk= or regen=: junk feeds archive-rm directly, and
                # these are exactly the files that must not be deleted blind.
            )
        )

    if progress is not None:
        progress.set_total(len(rinex_dirs))

    for rdir in rinex_dirs:
        valid_dates: Set[date] = set()
        for f in sorted(rdir.iterdir()):
            if f.is_dir():
                continue  # superseded_rt_* etc. — intentional, out of scope
            report.scanned += 1
            rel = str(f.relative_to(source_root))
            fname = f.name
            size = f.stat().st_size if f.exists() else 0
            fdate = _obs_date(fname)

            if not VALID_OBS_RE.match(fname):
                report.findings.append(
                    AuditFinding(
                        rel_path=rel,
                        issue="bad-name",
                        detail=_classify_bad_name(fname),
                        size=size,
                        file_date=fdate,
                        junk=True,
                        regen=fdate is not None,
                    )
                )
                continue

            reason = bad_z_format(f)
            if reason is not None:
                report.findings.append(
                    AuditFinding(
                        rel_path=rel,
                        issue="bad-magic",
                        detail=reason,
                        size=size,
                        file_date=fdate,
                        junk=False,  # content is fine — recompress, don't delete
                        regen=False,
                    )
                )
                # still a product for the date (content-wise)
                if fdate:
                    valid_dates.add(fdate)
                continue

            if deep and not _decompress_ok(f):
                report.findings.append(
                    AuditFinding(
                        rel_path=rel,
                        issue="unreadable",
                        detail="does not decompress (truncated/corrupt)",
                        size=size,
                        file_date=fdate,
                        junk=True,
                        regen=fdate is not None,
                    )
                )
                continue

            if check_version:
                ver = _rinex_version_class(f)
                if ver == 2:
                    report.findings.append(
                        AuditFinding(
                            rel_path=rel,
                            issue="old-version",
                            detail="RINEX 2 product (CRINEX v1) — not re-rinexed",
                            size=size,
                            file_date=fdate,
                            junk=False,  # replaced in place by the regen push
                            regen=fdate is not None,
                        )
                    )
                    if fdate:
                        valid_dates.add(fdate)
                    continue

            # Report-only identity probe on the valid product: stray + stacked.
            id_findings = []
            if check_identity:
                from .file_identity import probe_rinex_file

                for pf in probe_rinex_file(
                    f, station, fleet=_fleet, gate_m=_gate_m, rel=rel
                ):
                    id_findings.append(
                        AuditFinding(
                            rel_path=rel,
                            issue=pf.kind,  # 'stray' | 'stacked'
                            detail=pf.detail,
                            size=size,
                            file_date=fdate,
                            junk=False,  # never delete: relocate (stray) / re-rinex (stacked)
                            regen=(pf.kind == "stacked" and fdate is not None),
                        )
                    )
            if id_findings:
                report.findings.extend(id_findings)
            else:
                report.clean += 1
            if fdate:
                valid_dates.add(fdate)

        # missing-rinex: raw present, no valid product (daily sessions only —
        # hourly raw session letters don't map 1:1 to rinex hour letters).
        if check_missing and not session.lower().endswith("1hr"):
            raw_dir = rdir.parent / "raw"
            if raw_dir.is_dir():
                for rf in raw_dir.iterdir():
                    m = RAW_DATE_RE.match(rf.name)
                    if not m:
                        continue
                    try:
                        rdate = datetime.strptime(m.group(1), "%Y%m%d").date()
                    except ValueError:
                        continue
                    if rdate not in valid_dates:
                        report.findings.append(
                            AuditFinding(
                                rel_path=str(rf.relative_to(source_root)),
                                issue="missing",
                                detail="raw exists but no valid rinex product",
                                size=0,
                                file_date=rdate,
                                junk=False,
                                regen=True,
                            )
                        )
                        valid_dates.add(rdate)  # one finding per date

        if progress is not None:
            progress.advance()

    return report
