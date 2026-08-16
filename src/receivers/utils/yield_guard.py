"""Reject raw files that carry no usable observation data.

A receiver that has lost its antenna (or its sky view) keeps producing files on
schedule — they are simply almost empty. RFEL is the worked example: 566 health
samples over two days with **0 satellites tracked, max 0**, while still emitting
hourly 1Hz files of ~8 KB against a fleet-normal ~460 KB. Those files archive
fine, fail to convert, and then sit in the catalog looking like real data.

Size is the practical discriminator, but an absolute floor cannot work: a
Septentrio 1Hz hour, a Trimble 15s day and a status file differ by orders of
magnitude, and every receiver model differs again. So the floor is **relative to
the station's own recent history** for that same session type — a station is
compared only against itself.

**Fail-open, always.** No database, no baseline, or too few samples means the
file is archived. A guard that discards data when its own inputs are missing is
worse than no guard: the failure mode of this module must be "kept something
useless", never "dropped something real".

Zero satellites is reported separately (see :func:`satellite_alert`) rather than
folded in here. Filtering alone would quietly hide a dead instrument that needs a
site visit — the file stops arriving in the archive and nobody is told why.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Below this fraction of the station's own median, a file carries no plausible
# observation payload. RFEL sits at ~1.7% of normal, so 10% is far from the edge
# while still leaving room for genuinely short hours (power blips, partial hours
# at a session boundary).
DEFAULT_MIN_FRACTION = 0.10

# Fewer samples than this and the median is not a baseline, it is noise.
DEFAULT_MIN_SAMPLES = 20

DEFAULT_LOOKBACK_DAYS = 14


@dataclass
class GuardVerdict:
    """Outcome of a yield check. ``allowed`` is the only field callers must honour."""

    allowed: bool
    reason: str = ""
    size: int = 0
    median: Optional[int] = None
    station: str = "UNKNOWN"

    @property
    def fraction(self) -> Optional[float]:
        if not self.median:
            return None
        return self.size / self.median


@dataclass
class YieldGuardConfig:
    """Wiring for :class:`receivers.utils.file_archiver.FileArchiver`.

    ``connection`` is a live gps_health connection used only to read the size
    baseline. Passing None (or ``enabled=False``) disables the guard entirely.
    """

    connection: Any = None
    enabled: bool = True
    min_fraction: float = DEFAULT_MIN_FRACTION
    min_samples: int = DEFAULT_MIN_SAMPLES
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    quarantine_root: Optional[Path] = None


def parse_archive_path(archive_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Pull ``(station, session_type)`` out of an archive path.

    Layout is ``…/YYYY/mon/STA/session/category/FILE``, so the station and
    session sit at a fixed offset from the end. Returns ``(None, None)`` for
    anything that does not match — the caller then fails open.
    """
    parts = archive_path.parts
    if len(parts) < 4:
        return None, None
    # …/STA/session/category/FILE  → -4, -3
    station, session = parts[-4], parts[-3]
    if not (len(station) == 4 and station.isalnum()):
        return None, None
    return station.upper(), session


def median_size(
    conn: Any,
    station: str,
    session_type: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> Optional[int]:
    """Median archived raw size for this station+session, or None if not enough history.

    Deliberately restricted to ``file_size > 0``: the catalog carries rows whose
    size is 0 because the file went missing, and including them would drag the
    median toward zero and disarm the guard exactly where it is needed.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY file_size),
                       count(*)
                FROM archive_catalog
                WHERE station = %s
                  AND session_type = %s
                  AND file_category = 'raw'
                  AND file_size > 0
                  AND file_date > current_date - %s
                """,
                (station, session_type, lookback_days),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - never let a guard query break archiving
        logger.debug("yield guard: baseline query failed for %s: %s", station, exc)
        return None

    if not row or row[0] is None or (row[1] or 0) < min_samples:
        return None
    return int(row[0])


def check_yield(
    size: int,
    station: str,
    session_type: str,
    conn: Any,
    *,
    min_fraction: float = DEFAULT_MIN_FRACTION,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> GuardVerdict:
    """Decide whether a raw file of ``size`` bytes is worth archiving."""
    if conn is None:
        return GuardVerdict(True, "no db connection — fail open", size, None, station)

    baseline = median_size(
        conn,
        station,
        session_type,
        lookback_days=lookback_days,
        min_samples=min_samples,
    )
    if baseline is None:
        return GuardVerdict(True, "no baseline — fail open", size, None, station)

    floor = baseline * min_fraction
    if size >= floor:
        return GuardVerdict(True, "ok", size, baseline, station)

    return GuardVerdict(
        False,
        (
            f"{size} bytes is {size / baseline:.1%} of this station's "
            f"{session_type} median ({baseline} bytes) — below the "
            f"{min_fraction:.0%} floor"
        ),
        size,
        baseline,
        station,
    )


def quarantine(tmp_file: Path, quarantine_root: Path, station: str) -> Optional[Path]:
    """Move a rejected file out of the pipeline but keep it for inspection.

    Quarantine lives OUTSIDE the archive tree so nothing rejected can be picked
    up by a reindex or a converter sweep. Returns the destination, or None if the
    move failed (the caller still refuses to archive either way).
    """
    try:
        dest_dir = Path(quarantine_root) / station
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / tmp_file.name
        shutil.move(str(tmp_file), str(dest))
        return dest
    except Exception as exc:  # noqa: BLE001
        logger.warning("yield guard: could not quarantine %s: %s", tmp_file, exc)
        return None


def satellite_alert(station: str, total_satellites: Optional[int]) -> bool:
    """Log a distinct, greppable alert when a receiver tracks nothing.

    Separate from the size guard on purpose: quarantining the files fixes the
    archive but hides the instrument fault. This is the line that should reach
    Icinga/Grafana so the station gets attention. Returns True when it fired.
    """
    if total_satellites is None or total_satellites > 0:
        return False
    logging.getLogger(f"receivers.health.{station}").error(
        "🛰️  ZERO SATELLITES TRACKED at %s — receiver is producing files with no "
        "observations; its raw output will be quarantined by the yield guard. "
        "Needs investigation (antenna, cable, or sky view), not just filtering.",
        station,
    )
    return True
