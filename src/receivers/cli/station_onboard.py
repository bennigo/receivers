"""``receivers station onboard`` — the EPOS onboarding pipeline as one verb.

Codifies the 8-step sequence proven on VONC/VOFJ/VMOS (todo #150, recipe in
memory ``epos-station-onboarding-pipeline-the-8-step-sequen``) into a single
orchestrator with a **dry-run/report + pause-for-confirmation gate per
stage**. Manual oversight is the point: every stage reports what it sees (or
would do) and waits for a confirmation before its mutating half runs, so a
station is onboarded with eyes on each step rather than blind.

Stage order: ::

    1  tos-review           tos station verify --include-closed (+ triage emit)
    2  rinex-review         archive facts: years, RINEX version + header fields
    3  re-rinex             re-convert archive R2→R3 from raw (long, detached)
    4  constellation-audit  tos audit constellations — AFTER re-rinex (R3 data)
    5  fix-headers          header-only repair of the R2-stuck remainder
    6  sitelog              generate + commit the IGS/M3G site log
    7  m3g                  validate → publish to M3G
    8  sync-yaml            print the allowlist stanza + commit/push steps
    9  epos-disseminate     full-history push (long, detached)

Ordering is load-bearing: the constellation cross-check reads RINEX-3
headers (R2 under-reports), so it must run AFTER the re-rinex step, and
fix-headers then mops up only the R2-stuck remainder (no-raw days).

The underlying work is NOT reimplemented here — each stage composes the
existing verbs (``tos``, ``receivers rinex``, ``receivers epos-disseminate``,
``receivers m3g``) so the super-verb and the manual recipe stay in lockstep.
Only the archive *facts* review (stage 2) is native, because no single verb
prints that picture today.

Long-running stages (re-rinex, epos-disseminate) launch **detached**
(``start_new_session``, ``PYTHONUNBUFFERED=1``, output to
``~/.cache/gps_receivers/logs/retrofits/``) — the pattern that provably avoids
the block-buffered-stdout trap on full-history runs — and report the pid + log
path for monitoring.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("receivers.cli.station_onboard")

# Where detached long jobs write their logs (same tree the manual runs use).
DEFAULT_LOG_DIR = "~/.cache/gps_receivers/logs/retrofits"

# Candidate archive mounts, first existing wins — mirrors the --from-archive /
# fix-headers source resolution (read-only NFS views of the same ananas store).
_ARCHIVE_CANDIDATES = ("/mnt/rawgpsdata", "/mnt_data/rawgpsdata", "/mnt/data/gpsdata")


def _resolve_archive_root(override: Optional[str] = None) -> Optional[str]:
    """Archive root: explicit override > first existing candidate mount >
    dissemination source_root > receivers.cfg data_prepath."""
    if override:
        return str(Path(override).expanduser())
    for cand in _ARCHIVE_CANDIDATES:
        if Path(cand).is_dir():
            return cand
    try:
        from ..dissemination import load_dissemination_config

        for t in load_dissemination_config():
            if getattr(t, "tier", None) == "dissemination":
                src = getattr(t, "source_root", None)
                if src and Path(src).is_dir():
                    return str(Path(src))
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..config.receivers_config import get_receivers_config

        return str(Path(get_receivers_config().get_data_prepath()))
    except Exception:  # noqa: BLE001
        return None


def _resolve_program(name: str) -> str:
    """Console-script name if on PATH, else resolve through this interpreter."""
    if shutil.which(name):
        return name
    return sys.executable


def _resolve_raw_bounds(station: str, root: str, session: str) -> Optional[tuple[str, str]]:
    """(min, max) YYYYMMDD from the station/session's raw tree, or None.

    ``receivers rinex --from-archive`` REQUIRES -s/-e, so the re-rinex stage
    must supply them. Resolve from raw dir years when the operator didn't pass
    --start/--end (re-rinex converts from raw, so raw coverage is the true
    span).
    """
    root_p = Path(root)
    station = station.upper()
    years: List[int] = []
    for ydir in sorted(root_p.iterdir()):
        if not (ydir.is_dir() and ydir.name.isdigit() and len(ydir.name) == 4):
            continue
        has_raw = False
        for mon in ydir.iterdir():
            if (mon / station / session / "raw").is_dir():
                has_raw = True
                break
        if has_raw:
            years.append(int(ydir.name))
    if not years:
        return None
    return (f"{min(years)}0101", f"{max(years)}1231")


@dataclass(frozen=True)
class OnboardContext:
    """Resolved inputs shared by every stage."""

    station: str
    session: str = "15s_24hr"
    root: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    work_dir: Optional[str] = None
    log_dir: str = DEFAULT_LOG_DIR
    receivers_bin: str = "receivers"
    tos_bin: str = "tos"

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir).expanduser()

    def receivers_argv(self, *args: str) -> List[str]:
        return [_resolve_program(self.receivers_bin), *args]

    def tos_argv(self, *args: str) -> List[str]:
        return [_resolve_program(self.tos_bin), *args]


@dataclass(frozen=True)
class Stage:
    key: str
    title: str
    mutating: bool
    # preview(context) -> str  — the report/dry-run shown BEFORE the gate.
    preview: Callable[[OnboardContext], str]
    # exec_argv(context) -> list[str]  — the mutating command (None when
    # mutating is False). Long-running stages are launched detached.
    exec_argv: Optional[Callable[[OnboardContext], List[str]]] = None
    long_running: bool = False
    log_suffix: str = ""


def _run(argv: List[str], *, detached: bool = False, log: Optional[Path] = None) -> int:
    """Run a composed command. Detached = new session, output to ``log``."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if not detached:
        return subprocess.call(argv, env=env)
    log = log or Path("/dev/null")
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a")
    proc = subprocess.Popen(
        argv, env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
    )
    fh.close()
    print(f"   ⏳ detached pid={proc.pid} — log: {log}")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes", "")


# --------------------------------------------------------------------------- #
# Stage 1 — TOS review
# --------------------------------------------------------------------------- #


def _preview_tos_review(ctx: OnboardContext) -> str:
    return (
        f"Run TOS audits as a pass/fail oracle (closed-join history included):\n"
        f"   $ tos station verify {ctx.station} --include-closed\n"
        f"\nIf findings remain, emit the combined triage file to edit + apply:\n"
        f"   $ tos station triage {ctx.station} --include-closed\n"
        f"   … edit the ACTION file (uncomment + fill <FILL_VALUE>) …\n"
        f"   $ tos audit apply <action-file> --dry-run   # then --apply\n"
        f"   $ tos station verify {ctx.station} --include-closed   # re-verify clean\n"
        f"\nHand-checks the audit still misses (per the recipe):\n"
        f"   · azimuth on antennas installed ≥2012 → assume true north (0.0) by "
        f"rule; pre-2012 campaign setups stay skipped\n"
        f"   · receiver GPS/GLO on CLOSED receivers (--include-closed covers them, "
        f"but the missing-attributes default is an ASSUMPTION — the data check is "
        f"stage 4 constellation-audit)\n"
        f"   · foundation_depth needs a real measured value where it applies "
        f"(bedrock-on-rock monuments)\n"
    )


def _run_tos_review(ctx: OnboardContext) -> None:
    rc = _run(ctx.tos_argv("station", "verify", ctx.station, "--include-closed"))
    print(
        f"   {'✅ verify clean' if rc == 0 else f'⚠️  verify exit {rc} — see triage above'}"
    )


# --------------------------------------------------------------------------- #
# Stage 2 — RINEX review (native, read-only)
# --------------------------------------------------------------------------- #


def _header_fields(text: str) -> dict:
    d: dict = {}
    for line in text.splitlines():
        if "RINEX VERSION / TYPE" in line:
            d["version"] = line.split()[0]
        elif "MARKER NUMBER" in line:
            d["marker_number"] = line[:20].strip()
        elif "OBSERVER / AGENCY" in line:
            d["observer"] = line[:20].strip()
            d["agency"] = line[20:40].strip()
        elif "REC # / TYPE / VERS" in line:
            d["rec_num"] = line[:20].strip()
            d["rec_type"] = line[20:40].strip()
            d["rec_vers"] = line[40:60].strip()
    return d


def _rinex_review_lines(station: str, root: str, session: str) -> List[str]:
    """Per-year archive facts for one station/session's RINEX tree."""
    from tostools.rinex.reader import read_rinex_file

    root_p = Path(root)
    lines: List[str] = []
    years = sorted(
        (p for p in root_p.iterdir() if p.is_dir() and p.name.isdigit()),
        key=lambda p: int(p.name),
    )
    found_any = False
    for ydir in years:
        rinex_dirs: List[Path] = []
        for mon in ydir.iterdir():
            d = mon / station / session / "rinex"
            if d.is_dir():
                rinex_dirs.append(d)
        if not rinex_dirs:
            continue
        files = sorted(
            f for d in rinex_dirs for f in d.iterdir() if f.is_file()
        )
        if not files:
            continue
        found_any = True
        sample = files[0]
        try:
            content = read_rinex_file(str(sample))
            text = content.decode("utf-8", errors="ignore") if content else ""
            hd = _header_fields(text)
        except Exception as exc:  # noqa: BLE001
            hd = {"_err": str(exc)}
        lines.append(
            f"   {ydir.name}: {len(files):4d} RINEX files · ver {hd.get('version', '?')}"
            f" · MARKER# {hd.get('marker_number', '∅') or '∅'}"
            f" · AGENCY {hd.get('agency', '∅') or '∅'}"
            f" · REC {hd.get('rec_type', '∅') or '∅'} {hd.get('rec_vers', '')}"
            f"   [sample {sample.name}]"
        )
    if not found_any:
        lines.append("   (no archived RINEX found — check the archive root)")
    return lines


def _preview_rinex_review(ctx: OnboardContext) -> str:
    if not ctx.root:
        return "   ⚠️  no archive root resolved — pass --root to inspect the archive."
    lines = _rinex_review_lines(ctx.station, ctx.root, ctx.session)
    head = (
        f"Archive RINEX picture for {ctx.station} ({ctx.session}) under {ctx.root}:\n"
        + "\n".join(lines)
        + "\n\nDecide the remediation path from the version/header spread:\n"
        f"   · raw coverage exists + R2 → stage 3 re-rinex (R2→R3)\n"
        f"   · no raw / header-only issues → stage 4 fix-headers\n"
        f"   · MARKER NUMBER must be the IERS DOMES only, else stripped\n"
        f"     (a 4-char value = 'no DOMES' — never the station id)."
    )
    return head


# --------------------------------------------------------------------------- #
# Stage 3 — re-rinex (long, detached)
# --------------------------------------------------------------------------- #


def _rerinex_argv(ctx: OnboardContext) -> List[str]:
    argv = ctx.receivers_argv(
        "rinex", ctx.station, "--session", ctx.session, "--from-archive",
        "--parallel",
    )
    start, end = ctx.start, ctx.end
    if not (start and end) and ctx.root:
        bounds = _resolve_raw_bounds(ctx.station, ctx.root, ctx.session)
        if bounds:
            start, end = start or bounds[0], end or bounds[1]
    if start:
        argv += ["-s", start]
    if end:
        argv += ["-e", end]
    if ctx.work_dir:
        argv += ["--work-dir", ctx.work_dir]
    argv += ["--push", "--catalog-prod", "--backup-old"]
    return argv


def _preview_rerinex(ctx: OnboardContext) -> str:
    argv = _rerinex_argv(ctx)
    span = f"{ctx.start or 'FIRST'}..{ctx.end or 'LAST'}"
    if not (ctx.start and ctx.end) and ctx.root:
        bounds = _resolve_raw_bounds(ctx.station, ctx.root, ctx.session)
        if bounds:
            span = f"{bounds[0]}..{bounds[1]} (resolved from raw coverage)"
    return (
        f"Re-convert the archive from raw (R2→R3, recovers GLO/GAL/BDS) for "
        f"{span}:\n"
        f"   $ {' '.join(argv)}\n"
        f"\nLong-running — launches DETACHED with PYTHONUNBUFFERED=1; monitor "
        f"the log, then continue. Resumes on re-run (staging tree IS the state; "
        f"no --force)."
    )


def _stage_log_path(ctx: OnboardContext, suffix: str) -> Path:
    return ctx.log_path / f"{ctx.station.lower()}_{suffix}.log"


# --------------------------------------------------------------------------- #
# Stage 4 — fix-headers
# --------------------------------------------------------------------------- #


def _fixheaders_argv(ctx: OnboardContext, *, push: bool) -> List[str]:
    argv = ctx.receivers_argv(
        "rinex", ctx.station, "--fix-headers", "--all", "--session", ctx.session,
    )
    if ctx.work_dir:
        argv += ["--work-dir", ctx.work_dir]
    if push:
        argv += ["--push", "--catalog-prod"]
    return argv


def _preview_fixheaders(ctx: OnboardContext) -> str:
    return (
        f"Header-only repair of archived RINEX (no re-conversion). Dry-run first "
        f"to see the would-fix breakdown:\n"
        f"   $ {' '.join(_fixheaders_argv(ctx, push=False))}\n"
        f"\nThen, on confirm, apply + push (un-regenerable originals auto-preserved "
        f"to rinex_org/):\n"
        f"   $ {' '.join(_fixheaders_argv(ctx, push=True))}"
    )


# --------------------------------------------------------------------------- #
# Stage 5 — constellation audit
# --------------------------------------------------------------------------- #


def _preview_constellation(ctx: OnboardContext) -> str:
    return (
        f"Cross-check the receiver's TOS constellation toggles against the "
        f"archived RINEX header set — run AFTER re-rinex so the R3 per-system "
        f"'SYS / # / OBS TYPES' list is authoritative (R2 under-reports):\n"
        f"   $ tos audit constellations {ctx.station}\n"
        f"   $ tos audit constellations {ctx.station} --triage   # if it disagrees\n"
        f"\nTrust order: live receiver > raw decode > RINEX header. Data shows a "
        f"system + TOS doesn't → set_true (safe even from R2); TOS says true + "
        f"data doesn't → review (R3 only)."
    )


def _run_constellation(ctx: OnboardContext) -> None:
    _run(ctx.tos_argv("audit", "constellations", ctx.station))


# --------------------------------------------------------------------------- #
# Stage 6 — sitelog
# --------------------------------------------------------------------------- #


def _sitelog_argv(ctx: OnboardContext) -> List[str]:
    return ctx.receivers_argv("epos-disseminate", "--station", ctx.station, "--sitelog")


def _preview_sitelog(ctx: OnboardContext) -> str:
    return (
        f"Generate the IGS/M3G site log from TOS and commit it to gps-sitelogs "
        f"(change-gated — a no-op when unchanged):\n"
        f"   $ {' '.join(_sitelog_argv(ctx))}"
    )


# --------------------------------------------------------------------------- #
# Stage 7 — M3G
# --------------------------------------------------------------------------- #


def _preview_m3g(ctx: OnboardContext) -> str:
    return (
        f"Validate the site log against M3G/EPOS, then publish:\n"
        f"   $ {ctx.receivers_bin} m3g validate --station {ctx.station}\n"
        f"   $ {ctx.receivers_bin} m3g submit --station {ctx.station} --publish\n"
        f"\nHTTP 422 'Owner/Contact' = the responsible agency is not registered "
        f"on gnss-metadata.eu (name/abbrev must match TOS/agencies.yaml)."
    )


# --------------------------------------------------------------------------- #
# Stage 8 — sync.yaml allowlist
# --------------------------------------------------------------------------- #


def _preview_sync_yaml(ctx: OnboardContext) -> str:
    return (
        f"Add {ctx.station} to the sync.yaml stations: allowlist in "
        f"gps-config-data (gates the daily 08:30 EPOS 'live' sweep), then "
        f"commit + push (sync timer propagates ~10 min):\n"
        f"\n   # in gps-config-data/sync.yaml\n"
        f"   stations:\n"
        f"     - {ctx.station}\n"
        f"\n   $ cd ~/git/gps-config-data && git add sync.yaml\n"
        f"   $ git commit -m 'epos: add {ctx.station} to sync allowlist' && git push"
    )


# --------------------------------------------------------------------------- #
# Stage 9 — epos-disseminate (long, detached)
# --------------------------------------------------------------------------- #


def _epos_argv(ctx: OnboardContext) -> List[str]:
    return ctx.receivers_argv(
        "epos-disseminate", "--station", ctx.station,
        "--start", "first", "--end", "last", "--parallel",
    )


def _preview_epos(ctx: OnboardContext) -> str:
    return (
        f"Full-history EPOS push (RINEX3 long-name conversion + upload, "
        f"batched supersede-cleanup):\n"
        f"   $ {' '.join(_epos_argv(ctx))}\n"
        f"\nLong-running — launches DETACHED with PYTHONUNBUFFERED=1. Verify the "
        f"range summary (pushed/failed/superseded) at the end of the log."
    )


# --------------------------------------------------------------------------- #
# Stage table + orchestrator
# --------------------------------------------------------------------------- #


STAGES: List[Stage] = [
    Stage("tos-review", "TOS review", False, _preview_tos_review),
    Stage("rinex-review", "RINEX review", False, _preview_rinex_review),
    Stage(
        "re-rinex", "Re-rinex (R2→R3 from raw)", True, _preview_rerinex,
        exec_argv=_rerinex_argv, long_running=True, log_suffix="rerinex",
    ),
    Stage("constellation-audit", "Constellation audit (R3)", False, _preview_constellation),
    Stage(
        "fix-headers", "Fix headers (R2-stuck remainder)", True, _preview_fixheaders,
        exec_argv=lambda c: _fixheaders_argv(c, push=True),
    ),
    Stage("sitelog", "Site log", True, _preview_sitelog, exec_argv=_sitelog_argv),
    Stage("m3g", "M3G publish", True, _preview_m3g,
          exec_argv=lambda c: c.receivers_argv(
              "m3g", "submit", "--station", c.station, "--publish",
          )),
    Stage("sync-yaml", "sync.yaml allowlist", False, _preview_sync_yaml),
    Stage(
        "epos-disseminate", "EPOS full-history push", True, _preview_epos,
        exec_argv=_epos_argv, long_running=True, log_suffix="epos_full",
    ),
]


def _run_stage(stage: Stage, ctx: OnboardContext, dry_run: bool, yes: bool) -> None:
    print(f"\n── Stage: {stage.title} ({stage.key}) " + "─" * 30)
    print(stage.preview(ctx))
    if not stage.mutating:
        if not dry_run and not yes:
            _confirm("Continue to the next stage? [Enter/y]")
        return
    argv = stage.exec_argv(ctx) if stage.exec_argv else []
    if dry_run:
        print(f"   [dry-run] would execute: {' '.join(argv)}")
        return
    if not (yes or _confirm(f"Execute '{stage.key}'? [y/N]")):
        print("   ⏭  skipped")
        return
    if stage.long_running:
        log = _stage_log_path(ctx, stage.log_suffix)
        _run(argv, detached=True, log=log)
    else:
        _run(argv)


def cmd_station_onboard(args: argparse.Namespace) -> int:
    station = args.station.upper()
    ctx = OnboardContext(
        station=station,
        session=args.session,
        root=_resolve_archive_root(args.root),
        start=args.start,
        end=args.end,
        work_dir=args.work_dir,
        log_dir=args.log_dir,
        receivers_bin=args.receivers_bin,
        tos_bin=args.tos_bin,
    )

    selected = STAGES
    if args.stages:
        keys = [s.strip().lower() for s in args.stages.split(",") if s.strip()]
        selected = [s for s in STAGES if s.key in keys]
        if not selected:
            print(f"❌ no known stage in {args.stages}; known: "
                  f"{', '.join(s.key for s in STAGES)}")
            return 2
    if args.from_stage:
        idx = next((i for i, s in enumerate(STAGES) if s.key == args.from_stage), None)
        if idx is None:
            print(f"❌ unknown --from stage {args.from_stage}")
            return 2
        selected = [s for s in selected if s.key in {s2.key for s2 in STAGES[idx:]}]

    print(f"🚀 station onboard {station}  "
          f"({'DRY-RUN' if args.dry_run else 'live'})")
    if args.dry_run:
        print("   report-only — no mutating stage will run")
    if not ctx.root:
        print("   ⚠️  archive root unresolved (stage 2 will be limited); pass --root")
    print(f"   archive root: {ctx.root or '(unresolved)'}")
    print(f"   stages: {', '.join(s.key for s in selected)}")

    for stage in selected:
        _run_stage(stage, ctx, args.dry_run, args.yes)

    print(f"\n🏁 onboard walk for {station} finished "
          f"({'dry-run' if args.dry_run else 'live'}).")
    return 0


def create_station_onboard_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "station",
        help="Station-level pipelines (EPOS onboarding)",
        description="Station-level orchestration verbs.",
    )
    st = parser.add_subparsers(dest="station_cmd", required=True)
    onboard = st.add_parser(
        "onboard",
        help="Walk the EPOS onboarding pipeline (todo #150) with a "
        "dry-run/pause gate per stage",
        description=(
            "Run the proven 8-step EPOS onboarding sequence for one station, "
            "reporting (and pausing) at each stage before its mutating half "
            "executes. Long jobs (re-rinex, epos-disseminate) launch detached."
        ),
    )
    onboard.add_argument("station", help="4-char station id (e.g. VMEY)")
    onboard.add_argument(
        "--session", default="15s_24hr",
        help="Session type (default: 15s_24hr)",
    )
    onboard.add_argument(
        "--root", default=None,
        help="Local archive root (default: auto-resolve the read-only mount)",
    )
    onboard.add_argument("-s", "--start", default=None,
                         help="Re-rinex start date YYYYMMDD (default: resolve)")
    onboard.add_argument("-e", "--end", default=None,
                         help="Re-rinex end date YYYYMMDD (default: resolve)")
    onboard.add_argument(
        "--work-dir", default=None,
        help="Staging dir for re-rinex / fix-headers (default: the rinex CLI's)",
    )
    onboard.add_argument(
        "--stages", default=None,
        help="Comma-separated stage keys to run (default: all, in order)",
    )
    onboard.add_argument(
        "--from", dest="from_stage", default=None,
        help="Start at this stage key (resume)",
    )
    onboard.add_argument(
        "--yes", action="store_true",
        help="Skip the per-stage confirmation gates (unattended)",
    )
    onboard.add_argument(
        "--dry-run", action="store_true",
        help="Report every stage, never execute a mutating one",
    )
    onboard.add_argument(
        "--log-dir", default=DEFAULT_LOG_DIR,
        help="Directory for detached long-job logs (default: retrofits/)",
    )
    onboard.add_argument(
        "--receivers-bin", default="receivers",
        help="receivers console script (default: receivers on PATH)",
    )
    onboard.add_argument(
        "--tos-bin", default="tos",
        help="tos console script (default: tos on PATH)",
    )
    onboard.set_defaults(func=cmd_station_onboard)
    return parser
