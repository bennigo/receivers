"""Per-station stall timeout resolver, download performance recorder, and
pre-download health checks.

Provides:
    - get_stall_timeout(): Resolve effective stall timeout for a station
    - compute_adaptive_timeout(): Data-driven timeout from download_log history
    - record_download(): Log every download attempt to download_log table
    - check_station_health_gate(): Skip stations with known hardware/config issues
    - should_skip_station(): Backoff for stations with consecutive failures
    - get_packet_loss_factor(): Watchdog timeout multiplier for lossy links

Timeout priority:
    1. DB stations.stall_timeout_override (per-station)
    2. Adaptive timeout from download_log history (data-driven)
    2b. Session bootstrap for large-file sessions without adaptive data
    3. receivers.cfg [receiver_type] stall_timeout
    4. receivers.cfg [receiver_defaults] stall_timeout
    5. Hardcoded fallback (300s)
"""

import logging
import math
import time
from datetime import UTC, datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level cache for DB overrides: {station_id: timeout_seconds}
_override_cache: Optional[Dict[str, int]] = None
_cache_loaded_at: float = 0.0
_CACHE_TTL = 300.0  # Reload every 5 minutes

# Module-level cache for adaptive timeouts: {(station_id, session_type): (timeout, loaded_at)}
_adaptive_cache: Dict[Tuple[str, str], Tuple[int, float]] = {}

# Session-type bootstrap timeouts for stations without adaptive history.
# Breaks the bootstrap deadlock where slow 3G stations always time out at
# the receivers.cfg default (600s) and never complete the 2 downloads
# needed for the adaptive system to kick in.
_SESSION_BOOTSTRAP_TIMEOUTS: Dict[str, int] = {
    "15s_24hr": 900,  # Daily SBF (3-5 MB) on slow 3G can take 600-1500s
}

# Sessions where current satellite tracking predicts download viability.
# Daily backfill sessions (15s_24hr) pull yesterday's archived file from
# the receiver's disk — current sats=0 says nothing about whether yesterday's
# file is there, so the no_satellites gate is over-aggressive for those.
# Live/recent sessions still benefit from the gate.
_SESSIONS_REQUIRING_LIVE_SATS = {"1Hz_1hr", "status_1hr"}


def _load_overrides() -> Dict[str, int]:
    """Load per-station stall_timeout_override values from the database.

    Returns:
        Dict mapping station_id -> timeout in seconds.
        Empty dict if DB unavailable.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sid, stall_timeout_override FROM stations "
                    "WHERE stall_timeout_override IS NOT NULL"
                )
                return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.debug("Could not load stall_timeout overrides from DB: %s", e)
        return {}


def _get_overrides() -> Dict[str, int]:
    """Get cached overrides, refreshing if stale."""
    global _override_cache, _cache_loaded_at

    now = time.monotonic()
    if _override_cache is None or (now - _cache_loaded_at) > _CACHE_TTL:
        _override_cache = _load_overrides()
        _cache_loaded_at = now
        if _override_cache:
            logger.debug(
                "Loaded %d stall_timeout overrides: %s",
                len(_override_cache),
                ", ".join(f"{k}={v}s" for k, v in _override_cache.items()),
            )

    return _override_cache


def invalidate_cache() -> None:
    """Force reload of all caches on next call."""
    global _override_cache, _cache_loaded_at, _adaptive_cache
    global _health_gate_cache, _backoff_cache, _loss_factor_cache
    _override_cache = None
    _cache_loaded_at = 0.0
    _adaptive_cache.clear()
    _health_gate_cache.clear()
    _backoff_cache.clear()
    _loss_factor_cache.clear()


def compute_adaptive_timeout(
    station_id: str,
    session_type: str,
    expected_file_size: Optional[int] = None,
    safety_factor: float = 1.5,
    min_timeout: int = 300,
    max_timeout: int = 1800,
) -> Optional[int]:
    """Compute a data-driven timeout from download_log history.

    Queries the last 7 days of completed downloads for this station+session
    to determine average speed, then calculates how long a full download
    should take with a safety margin.

    Args:
        station_id: Station identifier.
        session_type: Session type (e.g. '15s_24hr', '1Hz_1hr').
        expected_file_size: Expected file size in bytes. If None, uses
            average file_size from history.
        safety_factor: Multiplier applied to estimated transfer time.
        min_timeout: Minimum timeout in seconds.
        max_timeout: Maximum timeout in seconds.

    Returns:
        Adaptive timeout in seconds, or None if insufficient data
        (fewer than 2 completed downloads in last 7 days).
    """
    station_id = station_id.upper()

    # Check cache first
    cache_key = (station_id, session_type)
    now = time.monotonic()
    if cache_key in _adaptive_cache:
        cached_timeout, cached_at = _adaptive_cache[cache_key]
        if (now - cached_at) < _CACHE_TTL:
            return cached_timeout

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT AVG(avg_speed_bps), AVG(file_size), COUNT(*)
                       FROM download_log
                       WHERE sid = %s
                         AND session_type = %s
                         AND outcome = 'completed'
                         AND avg_speed_bps > 0
                         AND file_size > 0
                         AND ts > NOW() - INTERVAL '7 days'""",
                    (station_id, session_type),
                )
                row = cur.fetchone()

        if row is None or row[2] < 2:
            # Insufficient data
            return None

        avg_speed: float = row[0]
        avg_file_size: float = row[1]
        sample_count: int = row[2]

        file_size = expected_file_size if expected_file_size else avg_file_size
        if avg_speed <= 0:
            return None

        timeout = math.ceil(file_size / avg_speed * safety_factor)
        timeout = max(min_timeout, min(timeout, max_timeout))

        logger.debug(
            "Station %s (%s): adaptive timeout = %ds "
            "(avg_speed=%.0f B/s, file_size=%.0f, samples=%d)",
            station_id,
            session_type,
            timeout,
            avg_speed,
            file_size,
            sample_count,
        )

        # Cache the result
        _adaptive_cache[cache_key] = (timeout, now)
        return timeout

    except Exception as e:
        logger.debug(
            "Could not compute adaptive timeout for %s/%s: %s",
            station_id,
            session_type,
            e,
        )
        return None


def get_stall_timeout(
    station_id: str,
    receiver_type: str,
    default: int = 300,
    session_type: Optional[str] = None,
    expected_file_size: Optional[int] = None,
) -> int:
    """Resolve the effective stall timeout for a station.

    Priority:
        1. DB stations.stall_timeout_override (per-station)
        2. Adaptive timeout from download_log history (data-driven)
        2b. Session bootstrap for large-file sessions without adaptive data
        3. receivers.cfg [receiver_type] stall_timeout
        4. receivers.cfg [receiver_defaults] stall_timeout
        5. ``default`` argument (fallback)

    Args:
        station_id: Station identifier (e.g. 'LAHC').
        receiver_type: Receiver type key (e.g. 'netr9', 'polarx5').
        default: Hardcoded fallback if nothing else is configured.
        session_type: Session type for adaptive lookup (optional).
        expected_file_size: Expected file size for adaptive calculation (optional).

    Returns:
        Timeout in seconds.
    """
    station_id = station_id.upper()

    # 1. Per-station DB override (highest priority — manual control)
    overrides = _get_overrides()
    if station_id in overrides:
        timeout = overrides[station_id]
        logger.debug(
            "Station %s: using DB stall_timeout_override = %ds", station_id, timeout
        )
        return timeout

    # 2. Adaptive timeout from download_log history
    if session_type:
        adaptive = compute_adaptive_timeout(
            station_id, session_type, expected_file_size
        )
        if adaptive is not None:
            logger.debug(
                "Station %s: using adaptive timeout = %ds", station_id, adaptive
            )
            return adaptive

    # 2b. Session bootstrap: generous default for large-file sessions
    # without adaptive history.  Lets slow 3G stations complete at least
    # twice so the adaptive system can take over with real data.
    if session_type and session_type in _SESSION_BOOTSTRAP_TIMEOUTS:
        bootstrap = _SESSION_BOOTSTRAP_TIMEOUTS[session_type]
        logger.debug(
            "Station %s (%s): no adaptive data, using bootstrap timeout = %ds",
            station_id,
            session_type,
            bootstrap,
        )
        return bootstrap

    # 3-4. receivers.cfg: [receiver_type] stall_timeout -> [receiver_defaults] stall_timeout
    try:
        from ..config.receivers_config import get_receivers_config

        cfg = get_receivers_config()
        receiver_cfg = cfg.get_receiver_config(receiver_type)
        timeout = receiver_cfg.get("stall_timeout", default)
        if isinstance(timeout, (int, float)):
            return int(timeout)
    except Exception as e:
        logger.debug("Could not read stall_timeout from receivers.cfg: %s", e)

    # 5. Hardcoded fallback
    return default


def record_download(
    station_id: str,
    session_type: str,
    outcome: str,
    *,
    file_date: Optional[object] = None,
    filename: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    bytes_downloaded: Optional[int] = None,
    file_size: Optional[int] = None,
    stall_timeout_used: Optional[int] = None,
    attempt: int = 1,
    message: Optional[str] = None,
) -> None:
    """Record a download attempt to the download_log table.

    Fire-and-forget: failures are logged at DEBUG level but never raised.

    Args:
        station_id: Station identifier.
        session_type: Session type (e.g. '15s_24hr').
        outcome: One of 'completed', 'stall_timeout', 'failed', 'unreachable'.
        file_date: Date of the data file (optional).
        filename: Downloaded filename (optional).
        duration_seconds: Wall-clock download time (optional).
        bytes_downloaded: Actual bytes received (optional).
        file_size: Expected/total file size (optional).
        stall_timeout_used: Effective timeout value used (optional).
        attempt: Retry attempt number, 1-based.
        message: Error message or context (optional).
    """
    # Compute avg_speed_bps
    avg_speed_bps: Optional[float] = None
    if (
        bytes_downloaded is not None
        and duration_seconds is not None
        and duration_seconds > 0
    ):
        avg_speed_bps = bytes_downloaded / duration_seconds

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO download_log
                       (sid, session_type, outcome, file_date, filename,
                        duration_seconds, bytes_downloaded, file_size,
                        avg_speed_bps, stall_timeout_used, attempt, message)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        station_id.upper(),
                        session_type,
                        outcome,
                        file_date,
                        filename,
                        duration_seconds,
                        bytes_downloaded,
                        file_size,
                        avg_speed_bps,
                        stall_timeout_used,
                        attempt,
                        message,
                    ),
                )
    except Exception as e:
        logger.debug("Could not record download to download_log: %s", e)


# ---------------------------------------------------------------------------
# Health gate: skip stations with known hardware/config issues
# ---------------------------------------------------------------------------

# Cache: {(station_id, session_type): (skip_reason_or_None, monotonic_timestamp)}
# Keyed on (station_id, session_type) because the gate is now session-aware:
# the same station can legitimately yield different answers for 1Hz_1hr
# (live data — sats=0 → skip) vs 15s_24hr (yesterday's file — sats=0 → proceed).
_health_gate_cache: Dict[Tuple[str, Optional[str]], Tuple[Optional[str], float]] = {}
_HEALTH_GATE_TTL = 300.0  # 5 minutes


def check_station_health_gate(
    station_id: str,
    session_type: Optional[str] = None,
) -> Optional[str]:
    """Check if a station should be skipped based on its last health data.

    Queries station_latest_metrics and block_disk_status.
    Returns a skip reason string, or None if the station is OK to download.

    Skip conditions:
    - satellites_tracked == 0 → "no_satellites" — only for sessions in
      ``_SESSIONS_REQUIRING_LIVE_SATS`` (currently 1Hz_1hr, status_1hr).
      Daily backfill sessions (15s_24hr) pull yesterday's archived file and
      are not gated on current sats. ``session_type=None`` keeps the legacy
      behaviour (always gate) for safety.
    - block_disk_status.total_mb == 0 → "disk_broken" (all sessions)
    - All checks require live metrics fresher than 30 minutes (stale → proceed)

    NB: ``disk_usage_pct > 98`` is **not** a skip — a full receiver disk still
    serves fresh ring-buffered data, so it only warns and proceeds (see ALHV,
    2026-06-12). It used to return "disk_full"; that gate was removed.

    Cache is keyed on ``(station_id, session_type)`` so the same station can
    yield different answers for different sessions.

    Args:
        station_id: Station identifier.
        session_type: Session type (e.g. '15s_24hr'). When None, the
            no_satellites gate applies to keep callers without context safe.

    Returns:
        Skip reason string, or None if station should proceed.
    """
    station_id = station_id.upper()
    cache_key = (station_id, session_type)

    # Check cache
    now = time.monotonic()
    if cache_key in _health_gate_cache:
        cached_reason, cached_at = _health_gate_cache[cache_key]
        if (now - cached_at) < _HEALTH_GATE_TTL:
            return cached_reason

    try:
        reason = _query_health_gate(station_id, session_type)
    except Exception as e:
        logger.debug("Health gate query failed for %s: %s", station_id, e)
        reason = None
    _health_gate_cache[cache_key] = (reason, now)
    return reason


def _query_health_gate(
    station_id: str,
    session_type: Optional[str] = None,
) -> Optional[str]:
    """Internal: query DB for health gate decision."""
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                # Check latest metrics (satellites, disk)
                cur.execute(
                    """SELECT satellites_tracked, disk_usage_pct, last_update
                       FROM station_latest_metrics
                       WHERE station_id = %s""",
                    (station_id,),
                )
                row = cur.fetchone()

                if row is not None:
                    sats, disk_pct, last_update = row

                    # Only act on fresh data (< 30 min old)
                    if last_update is not None:
                        # Ensure timezone-aware comparison
                        if last_update.tzinfo is None:
                            last_update = last_update.replace(tzinfo=UTC)
                        age_seconds = (
                            datetime.now(tz=UTC) - last_update
                        ).total_seconds()

                        if age_seconds < 1800:
                            if (
                                sats is not None
                                and sats == 0
                                and (
                                    session_type is None
                                    or session_type in _SESSIONS_REQUIRING_LIVE_SATS
                                )
                            ):
                                return "no_satellites"
                            if disk_pct is not None and disk_pct > 98:
                                # A full receiver disk is an ALERT, not a skip.
                                # The receiver keeps ring-buffering fresh data
                                # (legacy rek downloads such stations fine), so
                                # gating here only loses data: ALHV stalled
                                # 2026-06-12 → 06-19 because its NetR9 disk sat
                                # at 98-99% while still serving hourly files.
                                # Disk fullness is already surfaced via
                                # station_latest_metrics / dashboards, so just
                                # warn and proceed with the download.
                                logger.warning(
                                    "%s receiver disk %.1f%% full — downloading "
                                    "anyway (not gating)",
                                    station_id,
                                    disk_pct,
                                )

                    # Check for broken disk (total_mb = 0 with recent data).
                    # Distinct from disk_full: total_mb=0 means the disk is
                    # broken or unmounted (GJAC pattern), not just nearly full.
                    # Only query block_disk_status when disk_pct is 0 or NULL
                    # (1-2 stations), skipping the query for all healthy stations.
                    if disk_pct is None or disk_pct == 0:
                        cur.execute(
                            """SELECT total_mb FROM block_disk_status
                               WHERE sid = %s AND ts > NOW() - INTERVAL '1 hour'
                               ORDER BY ts DESC LIMIT 1""",
                            (station_id,),
                        )
                        disk_row = cur.fetchone()
                        if disk_row and disk_row[0] is not None and disk_row[0] == 0:
                            return "disk_broken"

    except Exception as e:
        logger.debug("Health gate check failed for %s: %s", station_id, e)

    return None


# ---------------------------------------------------------------------------
# Consecutive failure backoff
# ---------------------------------------------------------------------------

# Cache: {station_id: (should_skip, monotonic_timestamp)}
_backoff_cache: Dict[str, Tuple[bool, float]] = {}
_BACKOFF_CACHE_TTL = 600.0  # 10 minutes


def should_skip_station(station_id: str) -> bool:
    """Check if a station has too many consecutive failures to be worth retrying.

    Queries the last 5 download_log entries. If all are non-completed
    (failed/unreachable/stall_timeout), returns True.

    Cached for 10 minutes — a successful download breaks the streak.

    Args:
        station_id: Station identifier.

    Returns:
        True if station should be skipped (consecutive failure backoff).
    """
    station_id = station_id.upper()

    now = time.monotonic()
    if station_id in _backoff_cache:
        cached_skip, cached_at = _backoff_cache[station_id]
        if (now - cached_at) < _BACKOFF_CACHE_TTL:
            return cached_skip

    skip = _query_consecutive_failures(station_id)
    _backoff_cache[station_id] = (skip, now)
    return skip


def clear_backoff_cache(station_id: str) -> None:
    """Clear backoff cache for a station (e.g., when ping confirms it's online)."""
    _backoff_cache.pop(station_id.upper(), None)


def clear_all_backoff_cache() -> None:
    """Clear the entire backoff cache.

    Called when the network is confirmed working (e.g., majority of first-pass
    downloads succeeded), so stale backoff entries from a previous outage
    don't prevent stations from being retried.
    """
    _backoff_cache.clear()


def _query_consecutive_failures(station_id: str) -> bool:
    """Internal: query download_log for consecutive failures."""
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT outcome FROM download_log
                       WHERE sid = %s
                         AND ts > NOW() - INTERVAL '48 hours'
                       ORDER BY ts DESC
                       LIMIT 5""",
                    (station_id,),
                )
                rows = cur.fetchall()

        if len(rows) < 5:
            return False  # Not enough history — give it a chance

        return all(row[0] in ("failed", "unreachable", "stall_timeout") for row in rows)

    except Exception as e:
        logger.debug("Backoff check failed for %s: %s", station_id, e)
        return False


# ---------------------------------------------------------------------------
# Packet loss factor for watchdog timeout
# ---------------------------------------------------------------------------

# Cache: {station_id: (factor, monotonic_timestamp)}
_loss_factor_cache: Dict[str, Tuple[float, float]] = {}
_LOSS_FACTOR_TTL = 300.0  # 5 minutes


def get_packet_loss_factor(station_id: str) -> float:
    """Get a watchdog timeout multiplier based on packet loss.

    Queries station_connectivity for packet_loss percentage and returns
    a multiplier:
    - 0–20% loss: 1.0x
    - 20–50% loss: linear 1.0x to 2.0x
    - 50%+ loss: 2.0x (capped)

    Args:
        station_id: Station identifier.

    Returns:
        Multiplier (1.0 to 2.0). Returns 1.0 if data unavailable.
    """
    station_id = station_id.upper()

    now = time.monotonic()
    if station_id in _loss_factor_cache:
        cached_factor, cached_at = _loss_factor_cache[station_id]
        if (now - cached_at) < _LOSS_FACTOR_TTL:
            return cached_factor

    factor = _query_packet_loss_factor(station_id)
    _loss_factor_cache[station_id] = (factor, now)
    return factor


def _query_packet_loss_factor(station_id: str) -> float:
    """Internal: query station_connectivity for packet loss."""
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT packet_loss FROM station_connectivity
                       WHERE sid = %s""",
                    (station_id,),
                )
                row = cur.fetchone()

        if row is None or row[0] is None:
            return 1.0

        loss_pct: float = row[0]

        if loss_pct <= 20:
            return 1.0
        elif loss_pct >= 50:
            return 2.0
        else:
            # Linear interpolation: 20% → 1.0x, 50% → 2.0x
            return 1.0 + (loss_pct - 20) / 30.0

    except Exception as e:
        logger.debug("Packet loss query failed for %s: %s", station_id, e)
        return 1.0
