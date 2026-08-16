"""Don't spend a transfer on a receiver that is tracking nothing.

RFEL read 0 satellites for weeks while still writing files on schedule. The
yield guard keeps that output out of the archive, but by then the bytes have
already crossed a 3G link and been decoded. Health data already knows better:
``block_satellite_tracking`` is written every 5 minutes, so the fault is visible
long before the next download window.

**Self-healing by construction.** Health checks run on their own executor and are
NOT gated by this — they keep probing a gated station every 5 minutes. The moment
the receiver tracks a satellite again, ``max(total)`` goes positive and the next
download window proceeds normally. Nothing needs to be un-gated by hand, which
matters because the failure being detected is usually fixed by someone at the
site who will never think to clear a flag.

**MAX, not average.** One epoch with satellites proves the receiver can see sky.
An average would gate a station that is merely struggling — poor sky view, a
partial obstruction, winter icing — and those files are real data we want.

**Fails open on every uncertainty.** No connection, no rows, too few samples, a
raising query: download. A receiver family that reports no satellite block at all
must never be gated by the absence of evidence.

**Connection discipline matters here.** rek-d01 runs against ``max_connections =
100`` and has logged slot exhaustion daily since 2026-08-05 (11,378 errors on the
08-10 collapse day). ~169 stations download in the same :01-:06 window, so a gate
that opened its own connection per job would add ~169 concurrent connections to
an already-exhausted budget — turning a safety feature into an outage. Two
defences: the decision is cached per station for :data:`_CACHE_TTL_S`, and the
query itself borrows from the semaphore-bounded pool rather than opening a
socket. In the steady state this costs roughly one query per station per TTL.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Health writes every ~5 min, so a decision older than that cannot be improved by
# asking again. Re-querying per download job would be pure cost.
_CACHE_TTL_S = 300

_cache: dict = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    """Drop the memoised decisions (tests)."""
    with _cache_lock:
        _cache.clear()


# Window of health history the decision is made on.
DEFAULT_WINDOW_HOURS = 6

# Health writes every ~5 min, so 6 h is ~72 samples. Requiring 12 means a
# station is only gated on a well-populated window: a health outage, a restart,
# or a newly-added station leaves too few samples and the gate stays open.
DEFAULT_MIN_SAMPLES = 12


def satellite_health(
    station_id: str,
    conn: Any,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> Optional[Tuple[int, int]]:
    """Return ``(sample_count, max_satellites)`` over the window, or None.

    None means "no usable evidence" — no connection, no rows, or a failed query.
    It is never a statement about the receiver.
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:  # noqa: SIM117
            cur.execute(
                """
                SELECT count(*), coalesce(max(st.total), 0)
                FROM block_satellite_tracking st
                JOIN stations s USING (sid)
                WHERE s.marker_name = %s
                  AND st.ts > now() - make_interval(hours => %s)
                """,
                (station_id, window_hours),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - a gate must not break downloads
        logger.debug("download gate: health query failed for %s: %s", station_id, exc)
        return None
    if not row or not row[0]:
        return None
    return int(row[0]), int(row[1])


def should_skip_download(
    station_id: str,
    conn: Any,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    use_cache: bool = False,
) -> Tuple[bool, str]:
    """``(skip, reason)`` — skip only when health PROVES nothing is tracked."""
    if use_cache:
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(station_id)
            if hit and now - hit[0] < _CACHE_TTL_S:
                return hit[1]

    health = satellite_health(station_id, conn, window_hours=window_hours)
    if health is None:
        return _remember(
            station_id, (False, "no satellite health evidence — proceeding"), use_cache
        )

    samples, max_sats = health
    if samples < min_samples:
        return False, (
            f"only {samples} health samples in {window_hours}h "
            f"(need {min_samples}) — proceeding"
        )
    if max_sats > 0:
        return _remember(
            station_id,
            (False, f"tracking {max_sats} satellites — proceeding"),
            use_cache,
        )

    verdict = (
        True,
        (
            f"0 satellites across all {samples} health samples in the last "
            f"{window_hours}h — receiver is producing files with no observations. "
            f"Downloads resume automatically as soon as it tracks a satellite again."
        ),
    )
    return _remember(station_id, verdict, use_cache)


def _remember(station_id: str, verdict: Tuple[bool, str], use_cache: bool):
    if use_cache:
        with _cache_lock:
            _cache[station_id] = (time.monotonic(), verdict)
    return verdict
