"""Aggregate admission gate for SBF decoding inside live health checks.

The scheduler gives health monitoring its own APScheduler executor, so health
is isolated from downloads and backfill.  It is NOT isolated from *conversion*:
``PolaRX5.get_health_status()`` falls back to decoding a status_1hr SBF file
with ``bin2asc`` whenever the TCP/control probe yields no metrics, and that runs
on the health worker thread.

Per-job ceilings alone do not bound this.  ``bin2asc`` wedges are *correlated* —
a firmware revision it chokes on affects every station carrying it, so the
fallback fires fleet-wide at once and each occurrence holds a health thread for
its full timeout.  That is the 2026-08-11 mechanism (77 wedged ``bin2asc``, up
to 2.9 h each, fleet monitoring blind).  What has to be bounded is how many
decodes may be in flight *at the same time*, across all stations.

The gate is deliberately NON-BLOCKING.  Queueing for a permit would just move
the wait onto the same thread it is meant to protect — a ThreadPoolExecutor
queues rather than rejects, which is exactly why the original starvation was
silent.  When no permit is free the health check skips the SBF enrichment and
reports port-only status instead.  That degradation is the one the freshness
monitor (``health_freshness_check``) is built to tolerate: rows keep landing,
only the extra metrics are missing for that cycle.

What a skip costs, precisely.  ``is_online`` comes from the ICMP probe alone
(``connectivity_writer._write_ping_status``), so ``block_ping_status`` — what
the freshness monitor reads — is unaffected.  ``build_health_status`` takes the
worst of the connection and metric statuses, and the skipped SBF metrics are
overwhelmingly ``ok``, so dropping them cannot reclassify a station as worse.
The one real cost is that a genuinely CRITICAL SBF metric (say a low voltage)
goes unreported for that cycle — which is still strictly better than the
alternative it replaces, where the health thread hangs and the station reports
nothing at all for hours.

Tuning note: the slot count is env-var-only, so changing it on rek-d01 means
editing ``Environment=`` in the systemd unit and restarting — NOT a
gps-config-data push like ``status_monitoring.workers`` and the other knobs.
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

# Concurrent SBF decodes permitted across all live health checks.
#
# Sized well below the health executor's worker count: the fallback fires on
# roughly 3 % of Septentrio health runs (374 in 9.2 h across 90 stations,
# measured on rek-d01 2026-08-25) and takes ~0.2 s, so 4 permits are far more
# than the steady state needs while still leaving the executor's remaining
# threads free during a correlated wedge.
DEFAULT_HEALTH_DECODE_SLOTS = 4


def _configured_slots() -> int:
    """Read the slot count, defaulting rather than raising on a bad value.

    This runs at import, inside the scheduler process — a typo in the unit file
    must not take fleet monitoring down on startup.
    """
    raw = os.environ.get("RECEIVERS_HEALTH_DECODE_SLOTS")
    if raw is None:
        return DEFAULT_HEALTH_DECODE_SLOTS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid RECEIVERS_HEALTH_DECODE_SLOTS (%r) — using %d",
            raw,
            DEFAULT_HEALTH_DECODE_SLOTS,
        )
        return DEFAULT_HEALTH_DECODE_SLOTS


HEALTH_DECODE_SLOTS = _configured_slots()

_semaphore = threading.BoundedSemaphore(HEALTH_DECODE_SLOTS)

# Skips are counted rather than logged per occurrence: a correlated wedge would
# otherwise emit one warning per station per 5-minute cycle.
_skipped = 0
_skipped_lock = threading.Lock()


@contextmanager
def decode_slot(station_id: str) -> Iterator[bool]:
    """Try to claim a decode permit without waiting.

    Args:
        station_id: Station identifier, for logging

    Yields:
        True if a permit was claimed (and is released on exit), False if every
        permit was busy — in which case the caller must skip decoding rather
        than wait for one.
    """
    acquired = _semaphore.acquire(blocking=False)
    if not acquired:
        with _skipped_lock:
            global _skipped
            _skipped += 1
            count = _skipped
        logger.warning(
            "Health SBF decode skipped for %s — all %d decode slots busy "
            "(%d skipped since start); reporting port-only health",
            station_id,
            HEALTH_DECODE_SLOTS,
            count,
        )
        yield False
        return

    try:
        yield True
    finally:
        _semaphore.release()


def skipped_count() -> int:
    """Number of health decodes skipped for want of a permit since start."""
    with _skipped_lock:
        return _skipped


def _reset_for_tests() -> None:
    """Restore a fresh semaphore and counter (test support only)."""
    global _semaphore, _skipped
    _semaphore = threading.BoundedSemaphore(HEALTH_DECODE_SLOTS)
    with _skipped_lock:
        _skipped = 0
