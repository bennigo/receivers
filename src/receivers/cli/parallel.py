"""Parallel download orchestrator with time-staggered batching and retry.

Splits stations into groups of N and submits them to a shared
ThreadPoolExecutor with staggered timing.  Group 2 starts group_delay
seconds after group 1 is *submitted* (not finished), so slow 3G stations
in earlier groups never block later groups.

Thread safety:
- Each worker creates its own receiver instance (independent FTP/HTTP connections)
- Temp directories are partitioned by station_id/session
- Python logging is thread-safe
- Config reads create fresh parser instances
"""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Per-station wall-clock timeout (seconds).  This is the absolute ceiling
# for a single station download including all retries.  If a thread hangs
# (zombie FTP socket, kernel-level recv block) this ensures the parallel
# orchestrator still makes progress.  The daemon thread is abandoned and
# cleaned up when the process exits.
_STATION_WALL_TIMEOUT = 2400  # 40 minutes

logger = logging.getLogger("receivers.parallel")


# Counter for abandoned wall-clock-timeout threads.  Each abandoned thread
# holds whatever resources it was using at the moment of timeout: typically
# an FTP socket and a tmp file handle, occasionally a DB connection slot
# from BoundedSemaphore(20).  In a long-running scheduler these accumulate
# across days and can deplete the DB pool — so we surface the count for
# observability.  Reset is the operator's job (process restart).
_abandoned_threads_count = 0
_abandoned_threads_lock = threading.Lock()
# Threshold above which we log a WARNING each time the counter grows.  Below
# this we just bump the counter quietly — single-digit abandons per day are
# normal under sustained 3G/4G flakiness.
_ABANDONED_WARN_THRESHOLD = 10


def _record_abandoned_thread(station_id: str, timeout_s: int) -> int:
    """Bump the abandoned-thread counter and log when above threshold.

    Returns the new total so callers can include it in their own log lines.
    """
    global _abandoned_threads_count
    with _abandoned_threads_lock:
        _abandoned_threads_count += 1
        total = _abandoned_threads_count
    if total >= _ABANDONED_WARN_THRESHOLD:
        logger.warning(
            f"⚠️  Abandoned thread count: {total} (latest: {station_id}, "
            f"{timeout_s}s timeout). These threads still hold FTP sockets "
            f"and may hold DB connection slots. Restart the scheduler when "
            f"convenient to reclaim resources."
        )
    return total


def get_abandoned_thread_count() -> int:
    """Return current count of abandoned wall-clock-timeout threads."""
    with _abandoned_threads_lock:
        return _abandoned_threads_count


class RouterFailureCache:
    """Thread-safe cache of recently-failed router IPs.

    When a router is unreachable, all stations behind it will also fail.
    This cache lets subsequent stations skip the connectivity check
    for 5 minutes after the first failure.
    """

    _TTL = 300.0  # 5 minutes

    def __init__(self) -> None:
        self._failed: dict[str, float] = {}  # {router_ip: monotonic_timestamp}
        self._lock = threading.Lock()

    def mark_failed(self, router_ip: str) -> None:
        """Record a router as unreachable."""
        with self._lock:
            self._failed[router_ip] = time.monotonic()

    def is_failed(self, router_ip: str) -> bool:
        """Check if a router was recently marked as failed."""
        with self._lock:
            ts = self._failed.get(router_ip)
            if ts is None:
                return False
            if (time.monotonic() - ts) > self._TTL:
                del self._failed[router_ip]
                return False
            return True


# Module-level router cache shared across all parallel downloads
_router_cache = RouterFailureCache()


@dataclass
class StationResult:
    """Result of downloading data for a single station."""

    station_id: str
    status: str  # completed, up_to_date, unreachable, failed, skipped
    files_downloaded: int = 0
    duration: float = 0.0
    attempt: int = 1  # 1=first, 2=retry
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output."""
        d: dict = {
            "station_id": self.station_id,
            "status": self.status,
            "files_downloaded": self.files_downloaded,
            "duration": round(self.duration, 2),
            "attempt": self.attempt,
        }
        if self.error_message:
            d["error_message"] = self.error_message
        return d


@dataclass
class ParallelSummary:
    """Summary of a parallel download run."""

    total_stations: int = 0
    successful: int = 0
    unreachable: int = 0
    failed: int = 0
    skipped: int = 0
    total_files: int = 0
    total_duration: float = 0.0
    retried: int = 0
    retry_recovered: int = 0
    results: dict[str, StationResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output (experiment runner)."""
        return {
            "total_stations": self.total_stations,
            "successful": self.successful,
            "unreachable": self.unreachable,
            "failed": self.failed,
            "skipped": self.skipped,
            "total_files": self.total_files,
            "total_duration": round(self.total_duration, 2),
            "retried": self.retried,
            "retry_recovered": self.retry_recovered,
            "stations": {sid: r.to_dict() for sid, r in self.results.items()},
        }


def _record_parallel_outcome(
    station_id: str,
    args: Any,
    outcome: str,
    duration: float,
    attempt: int,
    message: str,
) -> None:
    """Record a download outcome to download_log (fire-and-forget)."""
    try:
        from ..utils.stall_timeout import record_download

        session_type = getattr(args, "session", None) or "15s_24hr"
        record_download(
            station_id,
            session_type,
            outcome,
            duration_seconds=duration,
            attempt=attempt,
            message=message,
        )
    except Exception:
        pass  # Fire-and-forget — DB issues must not crash parallel downloads


def _check_health_ping_online(station_id: str) -> bool | None:
    """Check health monitor's debounced reachability for a station.

    Checks two sources in a single query:
    1. ICMP ping (block_ping_status) — debounced, any of last 3 OK
    2. Download port status (block_port_status) — fallback for Trimble
       receivers that don't respond to ICMP but have open FTP/HTTP ports

    Does NOT trust NTRIP-only connectivity (station_connectivity view)
    since NTRIP proves the receiver has internet but NOT that the download
    server can reach its private 10.x IP.

    Returns True if ping OR port is OK, False if both fail, None if no data.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (SELECT bool_or(is_online) FROM (
                            SELECT is_online FROM block_ping_status
                            WHERE sid = %s AND ts > NOW() - INTERVAL '1 hour'
                            ORDER BY ts DESC LIMIT 3
                        ) p) AS ping_ok,
                        (SELECT bool_or(download_status IN ('open', 'ok')) FROM (
                            SELECT download_status FROM block_port_status
                            WHERE sid = %s AND ts > NOW() - INTERVAL '1 hour'
                            ORDER BY ts DESC LIMIT 3
                        ) q) AS port_ok
                    """,
                    (station_id, station_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                ping_ok, port_ok = row
                if ping_ok is True or port_ok is True:
                    return True
                # Both failed or no data
                has_data = ping_ok is not None or port_ok is not None
                return False if has_data else None
    except Exception:
        return None


def _bail(
    station_id: str,
    args: Any,
    t0: float,
    attempt: int,
    status: str,
    msg: str,
    router_ip: str | None = None,
    files_downloaded: int = 0,
    outcome_override: str | None = None,
) -> StationResult:
    """Record outcome and return a StationResult for early/post-download exit paths.

    Args:
        outcome_override: If set, use this as the download_log outcome instead
            of ``status``.  Needed when StationResult.status differs from the
            download_log outcome (e.g. wall-timeout: status="failed" but
            outcome="stall_timeout").
    """
    if router_ip:
        _router_cache.mark_failed(router_ip)
    duration = time.monotonic() - t0
    _record_parallel_outcome(
        station_id, args, outcome_override or status, duration, attempt, msg
    )
    # Only populate error_message for non-success statuses
    is_success = status in ("completed", "up_to_date")
    return StationResult(
        station_id=station_id,
        status=status,
        files_downloaded=files_downloaded,
        duration=duration,
        attempt=attempt,
        error_message=None if is_success else msg,
    )


def _split_into_groups(items: list, group_size: int) -> list[list]:
    """Split a list into groups of at most group_size."""
    return [items[i : i + group_size] for i in range(0, len(items), group_size)]


def _download_one_station(
    station_id: str,
    args: Any,
    start_time: datetime,
    end_time: datetime,
    ffrequency: str,
    afrequency: str,
    reverse_chronological: bool,
    attempt: int = 1,
    skip_backoff_check: bool = False,
) -> StationResult:
    """Download data for a single station (runs in a thread worker).

    Creates its own receiver instance and logger for thread safety.
    Returns a StationResult — no shared mutable state.
    """
    station_id = station_id.upper()
    t0 = time.monotonic()

    # Lazy import to avoid circular dependencies
    from .main import _download_station_period, _validate_station_for_download

    worker_logger = logging.getLogger(f"receivers.parallel.{station_id}")

    # Validate station config (no network, fast)
    receiver = _validate_station_for_download(
        station_id, worker_logger, session=args.session
    )
    if receiver is None:
        return _bail(
            station_id,
            args,
            t0,
            attempt,
            "skipped",
            "Station validation failed (config/session)",
        )

    # --- Pre-download gates (skip known-bad stations quickly) ---
    session_type = getattr(args, "session", None)

    # Health gate: skip stations with hardware/config issues
    try:
        from ..utils.stall_timeout import check_station_health_gate

        health_skip = check_station_health_gate(station_id, session_type)
        if health_skip:
            msg = f"Health gate: {health_skip}"
            worker_logger.info(f"⏭️  {station_id}: {msg}")
            return _bail(station_id, args, t0, attempt, "skipped", msg)
    except Exception:
        pass  # Health gate is advisory — failures must not block downloads

    # Single health-ping lookup, reused by backoff override and connectivity check
    ping_online = _check_health_ping_online(station_id)

    # Consecutive failure backoff: skip stations that keep failing
    # But override if health monitor or a quick ping shows the station is online
    # TODO: TANC shares battery with repeater → repeater dies first → station
    # flaps online/offline. Need "flapping station" detection that doesn't
    # penalize power-related connectivity issues.
    try:
        from ..utils.stall_timeout import clear_backoff_cache, should_skip_station

        if not skip_backoff_check and should_skip_station(station_id):
            # Prefer health monitor's debounced ping (2-consecutive-failure
            # threshold, updated every 5 min) over a single fresh ping
            online = ping_online
            if online is None:
                online = receiver._quick_ping()  # Fallback if no health data
            if not online:
                msg = "Consecutive failure backoff (last 5 attempts failed, still offline)"
                worker_logger.info(f"⏭️  {station_id}: {msg}")
                return _bail(station_id, args, t0, attempt, "skipped", msg)
            else:
                source = "health monitor" if online is True else "ping"
                worker_logger.info(
                    f"🔄 {station_id}: Backoff overridden — station online ({source})"
                )
                clear_backoff_cache(station_id)
    except Exception:
        pass  # Backoff is advisory

    # Router group skip: if router was recently unreachable, skip all stations behind it
    router_ip = receiver.station_info.get("router", {}).get("ip")
    if router_ip and _router_cache.is_failed(router_ip):
        msg = f"Router {router_ip} group skip (recently unreachable)"
        worker_logger.info(f"⏭️  {station_id}: {msg}")
        return _bail(station_id, args, t0, attempt, "unreachable", msg)

    # Quick connectivity check before attempting download.
    # Prefer the health monitor's debounced view (tolerates lossy 3G/4G links)
    # over a single live ICMP ping that fails on 25% packet loss.
    if ping_online is True:
        # Health monitor confirms station is online — skip the live ping
        pass
    elif ping_online is False:
        # Health monitor confirms station is offline — skip without pinging
        return _bail(
            station_id,
            args,
            t0,
            attempt,
            "unreachable",
            "Connectivity check: offline (health monitor)",
            router_ip=router_ip,
        )
    else:
        # No recent health data — fall back to live ICMP ping
        if not receiver._quick_ping():
            return _bail(
                station_id,
                args,
                t0,
                attempt,
                "unreachable",
                "Ping check failed",
                router_ip=router_ip,
            )

    # Set up per-file RINEX callback if --rinex flag is set.
    # PolaRX5 calls _on_file_archived after each file is archived, so RINEX
    # conversion starts immediately — not after the whole station finishes.
    # Non-PolaRX5 receivers ignore this; per-station fallback fires below.
    _rinex_file_count = []  # tracks per-file callbacks (thread-safe append)
    if getattr(args, "rinex", False):
        try:
            from ..rinex.async_converter import submit_file_rinex

            def _on_archived(archive_path: str) -> None:
                submit_file_rinex(station_id, args.session, archive_path)
                _rinex_file_count.append(1)

            receiver._on_file_archived = _on_archived
        except Exception:
            pass  # If import fails, fall back to per-station

    # Attempt download with a wall-clock timeout.
    # We run the download in a daemon thread so that if it hangs on a zombie
    # FTP socket we can abandon it after _STATION_WALL_TIMEOUT seconds.
    result_container: list[tuple[int, int, int] | Exception] = []

    def _do_download() -> None:
        try:
            result_container.append(
                _download_station_period(
                    receiver,
                    station_id,
                    start_time,
                    end_time,
                    args,
                    worker_logger,
                    audit_logger=None,
                    ffrequency=ffrequency,
                    afrequency=afrequency,
                    reverse_chronological=reverse_chronological,
                )
            )
        except Exception as exc:
            result_container.append(exc)

    dl_thread = threading.Thread(target=_do_download, daemon=True)
    dl_thread.start()
    dl_thread.join(timeout=_STATION_WALL_TIMEOUT)

    if dl_thread.is_alive():
        # Thread hung — abandon it (daemon=True means it dies with the process).
        # Record so the operator can see accumulating zombie-thread leaks
        # before they deplete the DB connection pool.
        total_abandoned = _record_abandoned_thread(station_id, _STATION_WALL_TIMEOUT)
        msg = (
            f"Station download timed out after {_STATION_WALL_TIMEOUT}s "
            f"(zombie connection; {total_abandoned} abandoned in this run)"
        )
        worker_logger.error(f"⏰ {station_id}: {msg}")
        return _bail(
            station_id,
            args,
            t0,
            attempt,
            "failed",
            msg,
            outcome_override="stall_timeout",
        )

    # Thread completed — check result
    if not result_container:
        return _bail(
            station_id, args, t0, attempt, "failed", "Download returned no result"
        )

    result = result_container[0]
    if isinstance(result, Exception):
        return _bail(
            station_id,
            args,
            t0,
            attempt,
            "failed",
            f"{type(result).__name__}: {result}",
        )

    files_downloaded, errors, files_checked = result

    if errors > 0:
        return _bail(
            station_id,
            args,
            t0,
            attempt,
            "failed",
            f"{errors} error(s) during download",
            files_downloaded=files_downloaded,
        )

    status = "completed" if files_downloaded > 0 else "up_to_date"

    # Per-station RINEX fallback: for receivers that don't support the per-file
    # callback (Trimble, Leica), submit a batch conversion after download.
    if (
        status == "completed"
        and getattr(args, "rinex", False)
        and not _rinex_file_count
    ):
        try:
            from ..rinex.async_converter import submit_rinex_conversion

            submit_rinex_conversion(station_id, args.session, start_time, end_time)
        except Exception:
            pass  # RINEX submission must never fail a download

    return _bail(
        station_id,
        args,
        t0,
        attempt,
        status,
        f"{files_downloaded} file(s), {files_checked} checked",
        files_downloaded=files_downloaded,
    )


def _get_session_defaults(session_type: str) -> dict[str, Any]:
    """Read batches and distribution_window from scheduler.yaml for a session.

    Returns dict with 'batches' and 'distribution_window' keys, or empty dict
    if scheduler config is unavailable.
    """
    try:
        from ..scheduling.config_loader import load_scheduler_config

        config = load_scheduler_config()
        session_cfg = config.get("sessions", {}).get(session_type, {})
        return {
            "batches": session_cfg.get("batches"),
            "distribution_window": session_cfg.get("distribution_window"),
        }
    except Exception:
        return {}


def download_parallel(
    stations: list[str],
    args: Any,
    logger: logging.Logger,
    start_time: datetime,
    end_time: datetime,
    ffrequency: str,
    afrequency: str,
    reverse_chronological: bool,
    _audit_logger: Any = None,
) -> ParallelSummary:
    """Download data for multiple stations in parallel with grouped batching.

    Parameters are resolved in priority order:
    1. CLI flags (--batches, --distribution-window)
    2. scheduler.yaml session config (batches, distribution_window)
    3. Hardcoded defaults (10 batches, 10 minutes)

    From batches + distribution_window, we derive:
    - group_size = ceil(stations / batches)
    - group_delay = distribution_window * 60 / batches

    Algorithm:
    1. Split stations into groups of group_size
    2. Submit each group to a shared ThreadPoolExecutor with group_delay
       stagger between submissions (groups run concurrently — no blocking)
    3. Wait for all futures to complete
    4. Collect unreachable/failed stations
    5. Wait retry_delay seconds, retry them (same staggered approach)
    6. Return ParallelSummary

    Args:
        stations: List of station IDs
        args: Parsed CLI arguments (needs session, sync, archive, etc.)
        logger: Logger instance
        start_time: Download start time
        end_time: Download end time
        ffrequency: File frequency (e.g., "1D", "1H")
        afrequency: Acquisition frequency (e.g., "15s", "1Hz")
        reverse_chronological: Whether to download newest first
        _audit_logger: Audit logger (unused in parallel mode)

    Returns:
        ParallelSummary with results for all stations
    """
    # Resolve parameters: CLI > scheduler.yaml > defaults
    # Use explicit None checks — 0 is a valid value (no delay).
    session_defaults = _get_session_defaults(getattr(args, "session", "15s_24hr"))

    cli_batches = getattr(args, "batches", None)
    cli_window = getattr(args, "distribution_window", None)

    if cli_batches is not None:
        batches = cli_batches
    elif session_defaults.get("batches") is not None:
        batches = session_defaults["batches"]
    else:
        batches = 2

    if cli_window is not None:
        distribution_window = cli_window
    elif session_defaults.get("distribution_window") is not None:
        distribution_window = session_defaults["distribution_window"]
    else:
        distribution_window = 10

    retry_delay = getattr(args, "retry_delay", 90.0)

    # Normalize station IDs
    stations = [s.upper() for s in stations]
    summary = ParallelSummary(total_stations=len(stations))

    # Derive group_size and group_delay from batches + window
    group_size = math.ceil(len(stations) / batches)
    group_delay = (distribution_window * 60) / batches

    logger.info(
        f"Parallel download: {len(stations)} stations, "
        f"{batches} batches of ~{group_size}, "
        f"{distribution_window}min window ({group_delay:.0f}s between groups)"
    )

    # Split into groups
    groups = _split_into_groups(stations, group_size)
    logger.info(f"Split into {len(groups)} groups")

    t0 = time.monotonic()

    # First pass: process all groups
    results = _process_groups(
        groups,
        args,
        logger,
        start_time,
        end_time,
        ffrequency,
        afrequency,
        reverse_chronological,
        group_delay,
        attempt=1,
    )
    summary.results.update(results)

    # --- Retry loop ---
    # Transient failures (FTP timeout, busy receiver, packet loss) often
    # succeed on subsequent attempts once batch pressure subsides.
    # Wall-timeout and hardware-issue stations are excluded.
    NON_RETRYABLE_PATTERNS = (
        "timed out after",
        "zombie connection",
        "disk_full",
        "no_satellites",
    )
    max_retries = getattr(args, "max_retries", 3)

    for retry_pass in range(2, max_retries + 2):  # attempt 2, 3, ..., max_retries+1
        retryable = [
            sid
            for sid, r in summary.results.items()
            if r.status in ("unreachable", "failed")
            and not any(p in (r.error_message or "") for p in NON_RETRYABLE_PATTERNS)
        ]

        if not retryable:
            break

        # Circuit breaker: skip if >80% failed (network/VPN issue)
        total_attempted = len(summary.results)
        if total_attempted > 10:
            failure_rate = len(retryable) / total_attempted
            if failure_rate >= 0.80:
                logger.warning(
                    f"⚠️  {len(retryable)}/{total_attempted} stations "
                    f"({failure_rate:.0%}) failed — "
                    f"possible network issue. Skipping retries."
                )
                break

        # Network-wide recovery: clear stale backoff entries (first retry only)
        skip_backoff_ids = None
        if retry_pass == 2:
            backoff_exempt: set[str] = set()
            if total_attempted > 10:
                success_count = sum(
                    1
                    for r in summary.results.values()
                    if r.status in ("completed", "up_to_date")
                )
                success_rate = success_count / total_attempted
                backoff_skipped = [
                    sid
                    for sid, r in summary.results.items()
                    if r.status in ("failed", "skipped")
                    and r.error_message
                    and "backoff" in r.error_message.lower()
                ]
                if success_rate >= 0.30 and backoff_skipped:
                    try:
                        from ..utils.stall_timeout import clear_all_backoff_cache

                        clear_all_backoff_cache()
                        backoff_exempt = set(backoff_skipped)
                        logger.info(
                            f"🔄 Network recovery detected ({success_rate:.0%} success) — "
                            f"cleared backoff cache, adding {len(backoff_skipped)} stations to retry"
                        )
                        retryable_set = set(retryable)
                        for sid in backoff_skipped:
                            if sid not in retryable_set:
                                retryable.append(sid)
                                retryable_set.add(sid)
                    except Exception:
                        pass  # Backoff clearing is advisory
            skip_backoff_ids = backoff_exempt or None

        # Increasing delay: retry_delay * pass_number (90s, 180s, 270s, ...)
        delay = retry_delay * (retry_pass - 1)
        retry_num = retry_pass - 1
        logger.info(
            f"🔄 Retry pass {retry_num}/{max_retries}: "
            f"{len(retryable)} stations, waiting {delay:.0f}s: "
            f"{' '.join(retryable)}"
        )
        time.sleep(delay)

        retry_groups = _split_into_groups(retryable, group_size)
        retry_results = _process_groups(
            retry_groups,
            args,
            logger,
            start_time,
            end_time,
            ffrequency,
            afrequency,
            reverse_chronological,
            group_delay,
            attempt=retry_pass,
            skip_backoff_ids=skip_backoff_ids,
        )

        # Update results
        for sid, result in retry_results.items():
            if result.status in ("completed", "up_to_date"):
                summary.retry_recovered += 1
            summary.results[sid] = result

        summary.retried += len(retryable)

    # Calculate summary totals
    summary.total_duration = time.monotonic() - t0
    for result in summary.results.values():
        if result.status in ("completed", "up_to_date"):
            summary.successful += 1
            summary.total_files += result.files_downloaded
        elif result.status == "unreachable":
            summary.unreachable += 1
        elif result.status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1

    # Wait for pending RINEX conversions before printing summary
    if getattr(args, "rinex", False):
        try:
            from ..rinex.async_converter import shutdown_rinex_pool

            shutdown_rinex_pool(wait=True)
        except Exception:
            pass

    # Print summary
    _print_summary(summary, logger)

    # Emit structured JSON for experiment runner
    if getattr(args, "json_log", False):
        import json

        print(f"EXPERIMENT_RESULT:{json.dumps(summary.to_dict())}")

    return summary


def _process_groups(
    groups: list[list[str]],
    args: Any,
    logger: logging.Logger,
    start_time: datetime,
    end_time: datetime,
    ffrequency: str,
    afrequency: str,
    reverse_chronological: bool,
    group_delay: float,
    attempt: int,
    skip_backoff_ids: set[str] | None = None,
) -> dict[str, StationResult]:
    """Process groups with time-staggered starts — groups don't wait for each other.

    All groups share a single ThreadPoolExecutor.  Each group is submitted
    after a ``group_delay`` sleep, but we never wait for the previous group
    to finish first.  This means slow stations (e.g. 3G links) in group 1
    do not block groups 2, 3, …  After every group has been submitted we
    wait for *all* futures to complete and collect results.
    """
    total_workers = sum(len(g) for g in groups)
    all_futures: dict[Future[StationResult], str] = {}
    results: dict[str, StationResult] = {}

    with ThreadPoolExecutor(max_workers=total_workers) as executor:
        # Submit groups with staggered timing
        for i, group in enumerate(groups):
            group_label = f"[{i + 1}/{len(groups)}]"
            logger.info(
                f"Group {group_label}: submitting {len(group)} stations — "
                f"{' '.join(group)}"
            )

            for sid in group:
                future = executor.submit(
                    _download_one_station,
                    sid,
                    args,
                    start_time,
                    end_time,
                    ffrequency,
                    afrequency,
                    reverse_chronological,
                    attempt,
                    skip_backoff_ids is not None and sid in skip_backoff_ids,
                )
                all_futures[future] = sid

            # Stagger delay before next group (skip after last group)
            if i < len(groups) - 1:
                logger.info(f"Stagger delay: {group_delay:.0f}s before next group")
                time.sleep(group_delay)

        # Collect ALL results (groups run concurrently)
        for future in as_completed(all_futures):
            sid = all_futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = StationResult(
                    station_id=sid,
                    status="failed",
                    attempt=attempt,
                    error_message=f"Thread error: {e}",
                )
            results[sid] = result

            # Log individual result
            status_icon = {
                "completed": "OK",
                "up_to_date": "OK (up to date)",
                "unreachable": "UNREACHABLE",
                "failed": "FAILED",
                "skipped": "SKIPPED",
            }.get(result.status, result.status)

            msg = f"  {result.station_id}: {status_icon}"
            if result.files_downloaded > 0:
                msg += f" ({result.files_downloaded} files)"
            msg += f" [{result.duration:.1f}s]"
            if result.error_message:
                msg += f" — {result.error_message}"
            logger.info(msg)

    return results


def _print_summary(summary: ParallelSummary, logger: logging.Logger) -> None:
    """Print parallel download summary."""
    logger.info("=" * 60)
    logger.info("Parallel download summary:")
    logger.info(f"  Total stations: {summary.total_stations}")
    logger.info(f"  Successful: {summary.successful} ({summary.total_files} files)")
    if summary.unreachable > 0:
        logger.info(f"  Unreachable: {summary.unreachable}")
    if summary.failed > 0:
        logger.info(f"  Failed: {summary.failed}")
    if summary.skipped > 0:
        logger.info(f"  Skipped: {summary.skipped}")
    if summary.retried > 0:
        logger.info(
            f"  Retried: {summary.retried}, recovered: {summary.retry_recovered}"
        )
    logger.info(f"  Duration: {summary.total_duration:.1f}s")
    logger.info("=" * 60)
