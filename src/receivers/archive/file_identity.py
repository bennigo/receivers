"""Report-only integrity probes for archived RINEX files.

Two cheap, header-only checks that need neither ``teqc`` nor a raw decode, so
they are affordable in the periodic integrity sweep as well as an on-demand
full-archive audit:

* **stray / wrong-station** — the file's ``APPROX POSITION XYZ`` (ECEF) is
  closest to a DIFFERENT station than the one whose tree it is filed under.
  Position DECIDES identity (bgo's rule: coordinates are truth; filename and
  path are only claims). Stations sit kilometres apart, so "nearest is not the
  expected station" is a strong signal even from a noisy single-point APPROX.
* **stacked / multi-document** — more than one RINEX document is concatenated
  into a single ``.D.Z`` (each historical re-process appended a compress member
  instead of replacing). A conformant reader sees only the first document; the
  rest is dead weight. Detected by counting ``END OF HEADER`` (one per real
  document) — NOT ``MARKER NAME``, which recurs within a single document via
  event-flag-3 "new site occupation" records (mid-session marker/antenna
  changes) and would false-positive those valid files as stacked.

Both are REPORT-ONLY. Remediation needs eyes:

* a stray → ``receivers archive-sort --file <rel>`` (authoritative ``teqc
  +meta`` raw decode + gateway-guarded relocation);
* a stacked file → a clean single-document re-rinex from the original raw.

The probe reuses :func:`tostools.rinex.reader.read_rinex_file` (handles
``.Z``/``.gz``) and the fleet-geometry helpers in :mod:`receivers.archive.sort`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .sort import (
    _ecef_to_latlon,
    _haversine_m,
    fleet_coordinates,
    nearest_station,
    resolve_position_gate_m,
)

logger = logging.getLogger("receivers.archive.identity")

# RINEX daily/hourly extensions that carry an observation header we can read.
_RINEX_EXTS: Tuple[str, ...] = (".Z", ".gz", ".o", ".O", ".crx", ".rnx")

Xyz = Tuple[float, float, float]


@dataclass(frozen=True)
class IdentityFinding:
    """One report-only finding for a single archived file."""

    rel: str  # path shown to the operator (relative to root, or basename)
    station_id: str  # station tree the file is filed under
    kind: str  # 'stray' | 'stacked'
    detail: str  # human-readable explanation + suggested next command
    severity: str = "warning"


# --------------------------------------------------------------------------- #
# Header parsing (pure — operate on already-decompressed text)
# --------------------------------------------------------------------------- #


def count_documents(text: str) -> int:
    """Number of concatenated RINEX documents in ``text``.

    Counts ``END OF HEADER`` — exactly one terminates each document's header,
    so N of them means N concatenated documents (a genuine stack).

    Do NOT count ``MARKER NAME``/``MARKER NUMBER``: a *single* valid document
    legitimately carries several of those via event-flag-3 ("new site
    occupation") records when the marker/antenna changes mid-session — e.g. the
    NYLA late-2022 files that toggled MarkerName NYLA↔FAGC intraday. Counting
    marker records false-positives every such file as "stacked" (the 2026-07-10
    regression: 9 valid NYLA files misflagged). ``END OF HEADER`` appears once
    per document regardless of how many in-stream event records follow.
    """
    return text.count("END OF HEADER")


def parse_first_approx_xyz(text: str) -> Optional[Xyz]:
    """Parse the FIRST ``APPROX POSITION XYZ`` (ECEF metres) from a header.

    Returns ``None`` when absent or unparseable. The three coordinate fields
    occupy the columns before the label; splitting on whitespace is robust to
    the exact Fortran field widths.
    """
    idx = text.find("APPROX POSITION XYZ")
    if idx == -1:
        return None
    line_start = text.rfind("\n", 0, idx) + 1
    prefix = text[line_start:idx]
    parts = prefix.split()
    if len(parts) < 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None


def classify_position(
    xyz: Optional[Xyz],
    station_id: str,
    fleet: dict,
    gate_m: float,
) -> Optional[Tuple[str, float, float]]:
    """Decide whether ``xyz`` belongs to ``station_id``.

    Returns ``None`` when the position confirms (or cannot refute) the filed
    identity, or ``(nearest_station, nearest_dist_m, expected_dist_m)`` when the
    position is closest to a DIFFERENT station (a stray). ``gate_m`` gates the
    "confirmed at own mark" fast path; the stray decision itself is relative
    (nearest ≠ expected) and therefore gate-independent.
    """
    if xyz is None or all(abs(c) < 1.0 for c in xyz):
        return None  # no usable position — nothing to confirm
    if station_id not in fleet:
        return None  # station has no surveyed coords in cfg — cannot compare
    latlon = _ecef_to_latlon(xyz)
    if latlon is None:
        return None
    lat, lon = latlon
    expected_dist = _haversine_m(lat, lon, *fleet[station_id])
    if expected_dist <= gate_m:
        return None  # within gate of its own mark — confirmed
    nearest, near_dist = nearest_station(lat, lon, fleet)
    if nearest is None or nearest == station_id:
        return None  # still closest to itself — noisy but same station
    return (nearest, near_dist, expected_dist)


# --------------------------------------------------------------------------- #
# Single-file probe
# --------------------------------------------------------------------------- #


def probe_rinex_file(
    path: Path,
    station_id: str,
    *,
    fleet: Optional[dict] = None,
    gate_m: Optional[float] = None,
    rel: Optional[str] = None,
) -> List[IdentityFinding]:
    """Run the stray + stacked checks on one archived RINEX file.

    Reads and decompresses the file ONCE. Unreadable files return no findings
    (corruption is the size/hash checks' job, not this probe's). ``fleet`` and
    ``gate_m`` are resolved from cfg when not supplied — pass them in for a
    batch to avoid re-reading cfg per file.
    """
    from tostools.rinex.reader import read_rinex_file

    display = rel or Path(path).name
    findings: List[IdentityFinding] = []
    try:
        content = read_rinex_file(str(path))
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.debug("identity probe: cannot read %s: %s", path, exc)
        return findings
    if not content:
        return findings
    text = content.decode("utf-8", errors="ignore")

    ndocs = count_documents(text)
    if ndocs > 1:
        findings.append(
            IdentityFinding(
                display,
                station_id,
                "stacked",
                f"{ndocs} concatenated RINEX documents in one file — a reader "
                "sees only the first; re-rinex from the original raw to collapse "
                "to a single document",
            )
        )

    if fleet is None:
        fleet = fleet_coordinates()
    if gate_m is None:
        gate_m = resolve_position_gate_m()
    verdict = classify_position(parse_first_approx_xyz(text), station_id, fleet, gate_m)
    if verdict is not None:
        near, near_d, exp_d = verdict
        findings.append(
            IdentityFinding(
                display,
                station_id,
                "stray",
                f"position is closest to {near} ({near_d:.0f} m) but filed under "
                f"{station_id} ({exp_d:.0f} m away) — confirm + relocate with: "
                f"receivers archive-sort {station_id} --verify-station",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# Directory walk (for the on-demand archive-audit verb)
# --------------------------------------------------------------------------- #


def _iter_rinex_files(rinex_dir: Path, station_id: str) -> Iterable[Path]:
    if not rinex_dir.is_dir():
        return
    for p in sorted(rinex_dir.iterdir()):
        if not p.is_file():
            continue
        if not p.name.startswith(station_id):
            continue
        if p.name.endswith(_RINEX_EXTS) or (
            len(p.suffix) >= 4 and p.suffix[1:3].isdigit()
        ):
            yield p


def audit_rinex_dir(
    rinex_dir: Path,
    station_id: str,
    *,
    root: Optional[Path] = None,
    fleet: Optional[dict] = None,
    gate_m: Optional[float] = None,
) -> List[IdentityFinding]:
    """Probe every RINEX file in one ``…/<STA>/<session>/rinex/`` directory."""
    if fleet is None:
        fleet = fleet_coordinates()
    if gate_m is None:
        gate_m = resolve_position_gate_m()
    findings: List[IdentityFinding] = []
    for p in _iter_rinex_files(Path(rinex_dir), station_id):
        rel = str(p.relative_to(root)) if root else p.name
        findings.extend(
            probe_rinex_file(p, station_id, fleet=fleet, gate_m=gate_m, rel=rel)
        )
    return findings
