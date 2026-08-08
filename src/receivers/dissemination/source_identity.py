"""Does this archived file actually belong to this station?

The dissemination QC gate runs AFTER ``set_header``, so it inspects a header the
pipeline has just normalised against TOS. That is fine for provenance fields
(agency, DOMES) and useless for **identity**: asking "does this file match the
station?" of a header just rewritten to match the station can only ever answer
yes. The check has to see the file as archived.

It cost real data integrity. ISAK's receiver was taken off the mark for a
campaign survey in August 2016 — 5 marks, 210-245 km away, days 214-227. The raw
validator refused those files at conversion time, but dissemination reads
archived RINEX and never sees raw, ``set_header`` moved the position onto ISAK,
and the QC gate then compared that rewrite against TOS, matched, and published
all 14 to the EPOS portal under ISAK's marker and DOMES.

So this gate runs on the SOURCE, before anything is converted or rewritten, and
answers the one question no post-rewrite check can.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Beyond this the file was recorded somewhere else, not mis-headered. Matches
#: the correction bounds in ``tostools.rinex.validator`` / ``corrector`` — a file
#: too far to correct is a file too far to publish.
MAX_SITE_DISTANCE_M = 1000.0


class SourceIdentityVerdict:
    """Outcome of the pre-conversion identity check."""

    __slots__ = ("ok", "message", "distance_m")

    def __init__(
        self, ok: bool, message: str = "", distance_m: Optional[float] = None
    ) -> None:
        self.ok = ok
        self.message = message
        self.distance_m = distance_m

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


def check_source_identity(
    source: Path,
    session: Optional[dict[str, Any]],
    *,
    max_distance_m: float = MAX_SITE_DISTANCE_M,
    loglevel: int = logging.WARNING,
) -> SourceIdentityVerdict:
    """Verify the SOURCE header's position against the station's TOS position.

    Passes (and says why) whenever it cannot decide: no session, no station
    coordinates, an unreadable header, or a source with no APPROX POSITION. A
    gate that blocks on missing information would halt the whole fleet the first
    time TOS was briefly unreachable — the failure mode this must not have. It
    blocks only on a positive, measured contradiction.
    """
    if not session:
        return SourceIdentityVerdict(True, "no TOS session — identity not checked")

    lat, lon, alt = session.get("lat"), session.get("lon"), session.get("altitude")
    if lat is None or lon is None or alt is None:
        # Exactly the hole that made the QC `coordinates` field inert: the check
        # is declared but silently has nothing to compare. Say so out loud.
        logger.warning(
            "identity check skipped for %s: TOS session carries no surveyed "
            "position (lat/lon/altitude)",
            Path(source).name,
        )
        return SourceIdentityVerdict(True, "no TOS coordinates — identity not checked")

    try:
        from tostools.rinex.reader import extract_header_info, read_rinex_header

        header = read_rinex_header(Path(source), loglevel=loglevel)
        info = extract_header_info(header, loglevel=loglevel) if header else {}
        raw_xyz = str((info or {}).get("APPROX POSITION XYZ") or "").split()[:3]
        if len(raw_xyz) != 3:
            return SourceIdentityVerdict(True, "source has no APPROX POSITION XYZ")
        file_xyz = tuple(float(v) for v in raw_xyz)

        from tostools.gps_metadata_qc import wgs84toitrf08

        tos_xyz = tuple(wgs84toitrf08.transform(float(lat), float(lon), float(alt)))
    except Exception as exc:  # noqa: BLE001 - never fail dissemination on a probe
        logger.debug("identity check could not evaluate %s: %s", source, exc)
        return SourceIdentityVerdict(True, f"identity not checked ({exc})")

    distance = math.dist(file_xyz, tos_xyz)
    if distance <= max_distance_m:
        return SourceIdentityVerdict(True, "", distance)

    marker = str(session.get("marker") or "").upper() or "<station>"
    message = (
        f"source was recorded {distance / 1000:.1f} km from {marker}'s surveyed "
        f"position — this file is not {marker}'s"
    )
    logger.error("REFUSING to disseminate %s: %s", Path(source).name, message)
    return SourceIdentityVerdict(False, message, distance)
