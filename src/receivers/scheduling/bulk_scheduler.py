#!/usr/bin/env python3
"""
APScheduler-based bulk download system for GPS receivers.

Features:
- Distributed scheduling across time windows
- Complete manual operation compatibility
- Production logging integration
- Email alert integration
- Performance monitoring
- Fault tolerance and recovery
"""

import fcntl
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from .lookback import Lookback
from .schedule_parser import ScheduleTrigger, apply_distribution_window, parse_schedule

if TYPE_CHECKING:
    from .load_monitor import LoadMonitor
    from .pipeline import PipelineStateStore

# Module-level reference for config watcher job (APScheduler needs module-level functions)
_scheduler_instance: Optional["BulkDownloadScheduler"] = None

# Module-level pipeline state store (lazy-initialized)
_pipeline_store: Optional["PipelineStateStore"] = None

# Module-level load monitor (lazy-initialized)
_load_monitor: Optional["LoadMonitor"] = None

# Per-session batch result accumulator (thread-safe, reset after each daily summary)
import threading as _threading

_BATCH_STATS: dict = (
    {}
)  # {session_type: {"ok": [...], "fail": {...}, "expected": [...], "skipped": [...]}}
_BATCH_LOCK = _threading.Lock()


def _record_batch_result(
    session_type: str, station_id: str, outcome: str, error: str = ""
) -> None:
    """Accumulate per-job result for the batch summary (non-blocking, fire-and-forget).

    Outcome buckets:
      - "ok"       — download succeeded
      - "expected" — gate skip that will not retry today (sticky, e.g. disk_broken)
      - "skipped"  — gate skip that may be retried (self-clearing, e.g. disk_full)
      - anything else — counted as a failure
    """
    with _BATCH_LOCK:
        bucket = _BATCH_STATS.setdefault(
            session_type,
            {"ok": [], "fail": {}, "expected": [], "skipped": []},
        )
        if outcome == "ok":
            bucket["ok"].append(station_id)
        elif outcome == "expected":
            bucket.setdefault("expected", []).append(station_id)
        elif outcome == "skipped":
            bucket.setdefault("skipped", []).append(station_id)
        else:
            bucket["fail"][station_id] = error or outcome


def _categorize_failure(error_msg: str) -> str:
    """Return a short failure category tag from an error message."""
    msg = error_msg.lower()
    # Expected/known-issue categories (checked first — explicit beats heuristic)
    if msg in ("no_satellites", "disk_broken", "disk_full"):
        return msg
    if "gps_week_rollover" in msg:
        return "gps_week_rollover"
    if "hardware_broken" in msg:
        return "hardware_broken"
    if "no_signal" in msg:
        return "no_signal"
    # Connection / network
    if any(p in msg for p in ("unreachable", "no route", "network")):
        return "unreachable"
    if any(p in msg for p in ("connection refused", "errno 111")):
        return "conn_refused"
    if any(
        p in msg for p in ("timed out", "timeout", "stall", "watchdog", "no progress")
    ):
        return "timeout"
    if any(p in msg for p in ("not found", "404", "550")):
        return "file_not_ready"
    if "size mismatch" in msg:
        return "size_mismatch"
    if any(p in msg for p in ("401", "530", "auth")):
        return "auth"
    if any(p in msg for p in ("configuration", "invalid ip")):
        return "config"
    return "other"


def _log_batch_summary_job(session_type: str) -> None:
    """Log accumulated batch results and reset the counter (APScheduler job)."""
    _log = logging.getLogger("receivers.scheduler")
    with _BATCH_LOCK:
        stats = _BATCH_STATS.pop(session_type, {})
    ok = stats.get("ok", [])
    fail = stats.get("fail", {})
    expected = stats.get("expected", [])
    skipped = stats.get("skipped", [])
    total = len(ok) + len(fail) + len(expected) + len(skipped)
    if total == 0:
        _log.debug(f"📋 {session_type} batch summary: no results accumulated yet")
        return
    fail_names = sorted(fail.keys())
    if len(fail_names) > 8:
        fail_str = ", ".join(fail_names[:8]) + f" [+{len(fail_names) - 8} more]"
    else:
        fail_str = ", ".join(fail_names) if fail_names else "—"

    # Group failures by category for actionable summary
    category_counts: Dict[str, int] = {}
    for err in fail.values():
        cat = _categorize_failure(err)
        category_counts[cat] = category_counts.get(cat, 0) + 1
    cat_str = " ".join(f"{cat}:{n}" for cat, n in sorted(category_counts.items()))

    _log.info(
        f"📋 {session_type} batch: {len(ok)} ✅  {len(fail)} ❌  ({total} total)"
        + (f" — {cat_str} — {fail_str}" if fail else "")
        + (
            f" — ⏭️  {len(expected)} expected: {', '.join(sorted(expected))}"
            if expected
            else ""
        )
        + (
            f" — ⏳ {len(skipped)} skipped (will retry): {', '.join(sorted(skipped))}"
            if skipped
            else ""
        )
    )


_RETRY_MAX_WORKERS = 8  # parallel workers for the second-chance window


def _retry_failed_daily_job(session_type: str) -> None:
    """Second-chance parallel retry for stations that failed in today's primary window.

    Queries download_log for stations whose only outcomes today are failures and who
    still have no archived file in file_tracking.  Runs all retries in parallel (up to
    _RETRY_MAX_WORKERS threads) so the entire retry window completes in ~10 minutes
    rather than the hours a sequential run would take.

    Results flow into _BATCH_STATS via _download_station_data_job → _record_batch_result,
    so the batch summary (which fires ~15 min later) captures recovered stations.

    Designed to fire ~30 min after the primary distribution window closes.
    """
    _log = logging.getLogger("receivers.scheduler")
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from ..health.database_factory import DatabaseConnectionFactory

        today_midnight = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Pass yesterday's date explicitly in UTC. Using SQL's
        # CURRENT_DATE inside the query is session-timezone dependent —
        # if the PG session tz isn't UTC, the date filter would slip.
        yesterday_utc = (today_midnight - timedelta(days=1)).date()

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                # Pull last failure reason per station for context in log messages.
                cur.execute(
                    """
                    WITH today_failures AS (
                        SELECT DISTINCT ON (sid) sid, outcome, message
                        FROM download_log
                        WHERE session_type = %s
                          AND ts >= %s
                          AND outcome NOT IN ('completed', 'up_to_date', 'expected')
                        ORDER BY sid, ts DESC
                    ),
                    today_successes AS (
                        SELECT DISTINCT sid
                        FROM file_tracking
                        WHERE session_type = %s
                          AND file_date = %s
                          AND status IN ('downloaded', 'archived')
                    )
                    SELECT f.sid, f.outcome, f.message
                    FROM today_failures f
                    LEFT JOIN today_successes s ON s.sid = f.sid
                    WHERE s.sid IS NULL
                    ORDER BY f.sid
                    """,
                    (session_type, today_midnight, session_type, yesterday_utc),
                )
                rows = cur.fetchall()

        if not rows:
            _log.info(f"🔁 Second-chance {session_type}: no failures to retry")
            return

        # Build per-station context: sid → (original_category)
        station_cats: Dict[str, str] = {
            sid: _categorize_failure(str(msg or outcome)) for sid, outcome, msg in rows
        }
        station_ids = sorted(station_cats.keys())

        cat_counts: Dict[str, int] = {}
        for cat in station_cats.values():
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        cat_str = " ".join(f"{c}:{n}" for c, n in sorted(cat_counts.items()))
        sid_preview = ", ".join(station_ids[:10]) + (
            f" [+{len(station_ids) - 10} more]" if len(station_ids) > 10 else ""
        )
        _log.info(
            f"🔁 Second-chance {session_type}: {len(station_ids)} queued"
            f" [{cat_str}] — {sid_preview}"
        )

        # Resolve scheduler context for correct production_mode / rinex / timeout
        production_mode = False
        run_rinex = False
        timeout_minutes = 45
        if _scheduler_instance is not None:
            production_mode = _scheduler_instance.production_mode
            cfg = _scheduler_instance.schedule_configs.get(session_type)
            if cfg is not None:
                run_rinex = cfg.rinex
                timeout_minutes = cfg.timeout_minutes

        recovered: List[str] = []
        still_failing: List[str] = []
        _results_lock = _threading.Lock()

        def _retry_one(sid: str) -> Tuple[str, bool]:
            try:
                _download_station_data_job(
                    sid,
                    session_type,
                    production_mode,
                    timeout_minutes=timeout_minutes,
                    run_rinex=run_rinex,
                )
                # Confirm success via file_tracking (download_station_data_job logs
                # per-station success/failure; we just need the aggregate for summary)
                with DatabaseConnectionFactory.connection(single_host=True) as _conn:
                    with _conn.cursor() as _cur:
                        _cur.execute(
                            """
                            SELECT 1 FROM file_tracking
                            WHERE sid = %s AND session_type = %s
                              AND file_date = CURRENT_DATE - 1
                              AND status IN ('downloaded', 'archived')
                            LIMIT 1
                            """,
                            (sid, session_type),
                        )
                        return sid, _cur.fetchone() is not None
            except Exception as exc:
                _log.warning(f"Second-chance {session_type} {sid}: {exc}")
                return sid, False

        workers = min(_RETRY_MAX_WORKERS, len(station_ids))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="sc_retry"
        ) as pool:
            futures = {pool.submit(_retry_one, sid): sid for sid in station_ids}
            for future in as_completed(futures):
                sid, success = future.result()
                with _results_lock:
                    (recovered if success else still_failing).append(sid)

        recovered.sort()
        still_failing.sort()
        fail_preview = ", ".join(still_failing[:10]) + (
            f" [+{len(still_failing) - 10} more]" if len(still_failing) > 10 else ""
        )
        _log.info(
            f"🔁 Second-chance {session_type} complete:"
            f" {len(recovered)}/{len(station_ids)} recovered"
            + (f" — {', '.join(recovered)}" if recovered else "")
            + (f" | still failing: {fail_preview}" if still_failing else "")
        )

    except ImportError:
        _log.debug("psycopg2 not available — second-chance retry disabled")
    except Exception as exc:
        _log.error(
            f"Second-chance {session_type} job error: {type(exc).__name__}: {exc}"
        )


def _get_pipeline_store() -> Optional["PipelineStateStore"]:
    """Get or create the module-level PipelineStateStore instance."""
    global _pipeline_store
    if _pipeline_store is None:
        try:
            from .pipeline import PipelineStateStore

            _pipeline_store = PipelineStateStore()
        except Exception:
            pass
    return _pipeline_store


def _get_load_monitor() -> Optional["LoadMonitor"]:
    """Get the module-level LoadMonitor instance (set during scheduler init)."""
    return _load_monitor


def _check_config_changes_job() -> None:
    """Check for config changes (standalone job function for APScheduler).

    Watches both ``stations.cfg`` (station status/identity) and
    ``station_areas.yaml`` (Grafana area grouping) so config edits apply on their
    own — no manual reseed.
    """
    if _scheduler_instance is not None:
        _scheduler_instance._check_config_changes()
        _scheduler_instance._check_areas_config_changes()


def _heartbeat_job() -> None:
    """Write the liveness heartbeat file (standalone job fn for APScheduler).

    Touches ``{cache}/heartbeat`` every minute. The external monitor
    (``receivers.monitoring.host_disk_check``) alerts when this file goes stale —
    the "scheduler active but wedged" detector for the 2026-07-21 /home-full
    incident, where the process stayed ``active`` while every write failed with
    ``OSError: [Errno 28]``. The heartbeat shares the volume with the logs, so a
    full disk freezes it too: staleness catches both a hung scheduler AND a full
    volume. A write failure here is itself the signal (stale mtime) — swallow it.
    """
    try:
        if _scheduler_instance is not None:
            hb = Path(_scheduler_instance.log_dir).parent / "heartbeat"
        else:
            hb = Path.home() / ".cache" / "gps_receivers" / "heartbeat"
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(f"{time.time()}\n")
    except Exception:  # noqa: BLE001 - a failed write is the signal, not a crash
        logging.getLogger("receivers.scheduler").debug(
            "heartbeat write failed", exc_info=True
        )


_MIRROR_METRICS_LAST: Dict[str, int] = {}


def _mirror_metrics_job() -> None:
    """Log the dual-write (pgdev mirror) failure counters once an hour.

    The counters exist so mirror trouble is *observed* rather than buried in a
    per-event ``logger.warning`` — the rek-d01/pgdev catalog divergence went
    unnoticed for months because nothing aggregated those warnings. Logs only
    when something changed since the last tick, so a healthy mirror is silent.
    """
    log = logging.getLogger("receivers.scheduler")
    try:
        from ..health.database_factory import MirrorMetrics

        snap = MirrorMetrics.snapshot()
        delta = {k: v - _MIRROR_METRICS_LAST.get(k, 0) for k, v in snap.items()}
        delta = {k: v for k, v in delta.items() if v}
        if delta:
            log.warning(
                "mirror dual-write issues in the last hour: %s (cumulative: %s)",
                ", ".join(f"{k}={v}" for k, v in sorted(delta.items())),
                ", ".join(f"{k}={v}" for k, v in sorted(snap.items())),
            )
        _MIRROR_METRICS_LAST.update(snap)
    except Exception:  # noqa: BLE001 - reporting must never break the scheduler
        log.debug("mirror metrics report failed", exc_info=True)


def _write_connectivity_status(
    station_id: str, health_data: Dict[str, Any], logger: logging.Logger
) -> None:
    """Write ping and port status to database for Grafana dashboard.

    Delegates to shared ConnectivityWriter module. Uses health data timestamp
    instead of NOW() for consistent time alignment across block tables.

    Args:
        station_id: Station identifier
        health_data: Health data dictionary with connection info
        logger: Logger instance
    """
    from ..health.connectivity_writer import ConnectivityWriter

    writer = ConnectivityWriter(logger)
    writer.write_connectivity_status(station_id, health_data)


def _is_retryable_download(result: Dict[str, Any]) -> bool:
    """Check whether a failed download result is worth retrying.

    Retryable: timeout, connection reset, stall, broken pipe, FTP file-not-found
    (550/404 — receiver hasn't finalized the file yet at midnight), size mismatch
    (receiver was still writing when we downloaded).
    NOT retryable: station unreachable, configuration_error, auth failures.
    """
    status = result.get("status", "")
    if status in ("unreachable", "configuration_error"):
        return False

    error_msg = result.get("error_message", "").lower()
    # Hard permanent errors — no point retrying
    permanent_patterns = [
        "401",
        "530",  # FTP auth failed
        "configuration",
        "invalid ip",
    ]
    if any(p in error_msg for p in permanent_patterns):
        return False

    # Retryable errors — includes timing issues at midnight rollover
    retryable_patterns = [
        "timed out",
        "timeout",
        "connection reset",
        "stall",
        "broken pipe",
        "watchdog",
        "no progress",
        # Septentrio midnight file rollover: FTP briefly refuses connections
        "connection refused",
        "errno 111",
        # Receiver hasn't finalized the daily file yet (FTP 550 / HTTP 404)
        "not found",
        "404",
        "550",
        # File was still growing when we downloaded it
        "size mismatch",
    ]
    return any(p in error_msg for p in retryable_patterns)


# Push-on-download (write-through to the long-term archive). Cached active sync
# target, resolved once per process — the scheduler restarts to pick up config
# changes anyway, so a process-lifetime cache is consistent with everything else.
_push_target = None
_push_target_loaded = False
# Cap concurrent pushes so parallel download workers don't trip rawdata's sshd
# MaxStartups (manifests as "connection reset/closed"). Download jobs run as
# threads in the main scheduler process (the RINEX ProcessPool is separate) and
# the rinex-completion callback also fires here, so one process-level semaphore
# bounds both raw and rinex pushes globally.
_PUSH_CONCURRENCY = 4
_push_sem = _threading.BoundedSemaphore(_PUSH_CONCURRENCY)


def _get_push_target():
    """Return the active sync target (imo_archive) for push-on-download, or None.

    None = no active archive target → push-on-download is off (the cutover gate).
    Cached for the process lifetime; failures resolve to None (push disabled).
    """
    global _push_target, _push_target_loaded
    if _push_target_loaded:
        return _push_target
    _push_target_loaded = True
    try:
        from ..archive import load_sync_config

        _push_target = next((t for t in load_sync_config() if t.active), None)
    except Exception:
        _push_target = None
    return _push_target


# Immediate per-push read-back config: (read_root, frozenset(sessions)) or None.
# Loaded once from scheduler.yaml archive_verify (read_root + push_verify_sessions);
# None = no immediate read-back (read_root unset or no sessions). Process-lifetime
# cache, consistent with _get_push_target (restart picks up config changes).
_push_verify = None
_push_verify_loaded = False


def _get_push_verify():
    """Return (read_root, sessions) for immediate per-push read-back, or None."""
    global _push_verify, _push_verify_loaded
    if _push_verify_loaded:
        return _push_verify
    _push_verify_loaded = True
    try:
        from .config_loader import load_scheduler_config

        cfg = load_scheduler_config().get("archive_verify", {})
        read_root = cfg.get("read_root")
        sessions = frozenset(cfg.get("push_verify_sessions") or [])
        _push_verify = (read_root, sessions) if read_root and sessions else None
    except Exception:
        _push_verify = None
    return _push_verify


def _push_to_storage(local_paths, logger) -> None:
    """Best-effort write-through of just-produced files to the long-term archive.

    Mirrors freshly downloaded/converted files to rawdata immediately (low latency
    for downstream consumers), then the ``:45`` archive-sync sweep reconciles any
    miss. Gated on an active archive target. Hard-isolated: NOTHING here may
    propagate into the download worker — every failure is swallowed and logged.
    """
    if not local_paths:
        return
    target = _get_push_target()
    if target is None:
        return  # no active archive target — cutover not active, push disabled
    try:
        from ..archive import ArchiveSync, open_catalog_conns

        # Bound concurrency (sshd MaxStartups) — never block a worker forever.
        if not _push_sem.acquire(timeout=120):
            logger.warning("push-storage skipped (push slots busy)")
            return
        verify_cfg = _get_push_verify()
        try:
            # Fan the catalog rows out to EVERY catalog host (rek-d01 + pgdev),
            # not just the local default — otherwise the mirror silently diverges
            # (only explicit --catalog-prod reindex used to reach it). conns[0] is
            # the operational host (carries the watermark); the rest are mirrors.
            with open_catalog_conns(prod=True) as catalog_conns:
                conn = catalog_conns[0] if catalog_conns else None
                sync = ArchiveSync(target, conn=conn, catalog_conns=catalog_conns)
                pushed, errors, digests = sync.push_explicit(list(local_paths))
                # Immediate read-back for the critical small-volume sessions
                # (15s_24hr). Runs while holding the push slot but does NOT sleep
                # (verify_pushed retries=0): only an NFS read + DB update, so a
                # lagging file defers to the cold pass rather than throttling the
                # midnight burst. Isolated: a verify hiccup must not mislabel the
                # already-committed push as skipped.
                if verify_cfg and digests:
                    read_root, vsessions = verify_cfg
                    try:
                        vstats = sync.verify_pushed(
                            digests, read_root=read_root, sessions=vsessions
                        )
                        if vstats.verified or vstats.deferred:
                            logger.info(
                                f"🔁 push-verify: {vstats.verified} confirmed, "
                                f"{vstats.deferred} deferred → cold pass"
                            )
                        for f in vstats.findings:
                            logger.error(f"push-verify: {f}")
                    except Exception as ve:
                        logger.warning(
                            f"push-verify skipped ({type(ve).__name__}: {ve})"
                        )
        finally:
            _push_sem.release()
        if pushed:
            logger.info(f"⤴️  push-storage: {pushed} file(s) → {target.name}")
        for err in errors:
            logger.warning(f"push-storage: {err}")
    except Exception as e:  # never let the push harm the download
        logger.warning(f"push-storage skipped ({type(e).__name__}: {e})")


# Module-level download function for APScheduler serialization
def _download_station_data_job(
    station_id: str,
    session_type: str,
    production_mode: bool = False,
    lookback_periods: int = 1,
    timeout_minutes: int = 30,
    run_rinex: bool = False,
):
    """Download data for a single station (standalone job function for APScheduler).

    This is a module-level function to allow APScheduler to serialize it to the database.
    Instance methods cannot be serialized when the instance contains non-serializable
    objects like schedulers.

    Args:
        station_id: Station identifier
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
        production_mode: Whether to use production logging
        lookback_periods: Number of periods to check (1=last period only, 2=last 2 periods, etc.)
        timeout_minutes: Maximum job duration in minutes (for monitoring and eventual enforcement)
        run_rinex: Whether to run RINEX conversion after download
    """
    exec_start_time = datetime.now(UTC)

    # Set up logging
    logger = logging.getLogger(f"receivers.download.{station_id}")

    # Satellite gate: don't spend a transfer on a receiver that is tracking
    # nothing. Health data already knows — block_satellite_tracking is written
    # every 5 minutes, independently of downloads, so a dead receiver is visible
    # before the window opens.
    #
    # This is self-healing on purpose: health keeps probing a gated station, so
    # the moment it tracks a satellite again the next window proceeds. Nothing
    # has to be cleared by hand — the person who fixes the antenna will never
    # think to reset a flag.
    #
    # Fails open on any uncertainty (no DB, no rows, thin sample count, error).
    #
    # Borrows from the semaphore-bounded pool and memoises per station: rek-d01
    # runs at max_connections=100 and has logged slot exhaustion daily since
    # 2026-08-05, and ~169 stations download in the same :01-:06 window. A gate
    # that opened its own socket per job would add ~169 concurrent connections
    # to an already-exhausted budget and cause the outage it exists to prevent.
    try:
        from ..health.database_factory import DatabaseConnectionFactory
        from ..utils.download_gate import should_skip_download

        with DatabaseConnectionFactory.connection(single_host=True) as _gate_conn:
            _skip, _why = should_skip_download(station_id, _gate_conn, use_cache=True)
        if _skip:
            logger.warning(f"🛰️  SKIPPING download for {station_id}: {_why}")
            return
    except Exception as exc:  # noqa: BLE001 - the gate must never block a download
        logger.debug(f"satellite gate skipped for {station_id}: {exc}")

    # Load-aware throttling: check system load before starting
    monitor = _get_load_monitor()
    if monitor is not None:
        from .task_interface import TaskPriority as _TP

        # Live 1Hz acquisition is REALTIME (threshold 1.0); daily and status
        # sessions are STANDARD (0.8), per TaskPriority's own docstring.
        #
        # This previously assigned STANDARD unconditionally -- the if/else was
        # a stub whose two branches were identical -- so EVERY live download was
        # gated at 0.8. When load_monitoring was first enabled on 2026-08-08 the
        # whole fleet stopped downloading 19 minutes later and stayed down ~33 h,
        # self-skipping rather than erroring.
        #
        # NOTE: this alone does not make load_monitoring safe to re-enable. The
        # ceilings must first exceed the scheduler's healthy working point
        # (rek-d01 sits at 72-208 threads and load 10-15 while healthy, against
        # a shipped max_active_jobs=80 / max_cpu_load=8.0), and the thread check
        # reads process-wide threading.active_count(), so the archive
        # reconciler's own workers count against live downloads.
        priority = _TP.REALTIME if session_type == "1Hz_1hr" else _TP.STANDARD
        if not monitor.can_start_job(priority):
            load = monitor.get_load()
            load_msg = (
                f"load gate: cpu={load.cpu_load_1m:.1f} threads={load.active_threads}"
            )
            logger.info(
                f"⏳ Load gate: skipping {station_id} ({session_type}) — "
                f"system overloaded ({load_msg}), will retry on next trigger"
            )
            from ..utils.stall_timeout import record_download

            record_download(
                station_id, session_type, outcome="skipped", message=load_msg
            )
            return

    # Health gate: skip stations with known hardware issues (no satellites, broken disk).
    # Uses cached station_latest_metrics — cheap DB read. Only fires on fresh data (<30 min).
    #
    # Outcome semantics:
    #   - "skipped" — self-clearing condition (e.g. disk_full may free up via
    #     file rotation). Second-chance retry includes these.
    #   - "expected" — sticky condition (no_satellites for live sessions,
    #     disk_broken). Second-chance retry excludes these (bulk_scheduler.py:171).
    try:
        from ..utils.stall_timeout import check_station_health_gate
        from ..utils.stall_timeout import record_download as _rd_health

        # Gate reasons that may self-clear within a day, so retry is worth attempting.
        _RETRYABLE_GATES = {"disk_full"}

        health_skip = check_station_health_gate(station_id, session_type)
        if health_skip:
            outcome = "skipped" if health_skip in _RETRYABLE_GATES else "expected"
            logger.info(
                f"⏭️  {station_id} ({session_type}) [{health_skip}] — outcome={outcome}"
            )
            _rd_health(station_id, session_type, outcome=outcome, message=health_skip)
            _record_batch_result(session_type, station_id, outcome, health_skip)
            return
    except Exception as exc:
        # Gate failure is non-fatal — proceed with download attempt.
        # Log at DEBUG so the failure is visible when diagnosing
        # unexpected downloads on broken stations.
        logger.debug(
            f"Health gate check failed for {station_id} ({session_type}): "
            f"{type(exc).__name__}: {exc}"
        )

    # Pipeline tracking (lightweight observability)
    pipeline_job = None
    pipeline_store = _get_pipeline_store()
    if pipeline_store is not None:
        try:
            from .pipeline import PipelineJob, PipelineStage
            from .task_interface import TaskPriority

            # Determine enabled stages based on session type and config
            stages = [PipelineStage.DOWNLOAD]
            if run_rinex and session_type != "status_1hr":
                stages.append(PipelineStage.RINEX)
            if session_type == "status_1hr":
                stages.append(PipelineStage.HEALTH)

            pipeline_job = PipelineJob.create(
                station_id=station_id,
                session_type=session_type,
                target_time=exec_start_time,
                enabled_stages=stages,
                priority=TaskPriority.STANDARD,
            )
            pipeline_job.mark_stage_started(PipelineStage.DOWNLOAD)
            pipeline_store.save_job(pipeline_job)
        except Exception:
            pipeline_job = None  # Non-critical — proceed without tracking

    # Initialise outside try so the outer except can safely reference it even
    # if the imports or setup_production_logging() raises before assignment.
    audit_logger = None

    try:
        # Track job start time for duration monitoring
        job_start_time = time.time()

        logger.info(f"Starting download: {station_id} ({session_type})")

        # Import receiver management here to avoid circular imports
        from ..base.production_logging import setup_production_logging
        from ..cli.main import create_receiver, get_station_config

        # Set up production logging. The create_station_logger / getLogger
        # calls are kept for their side effects (logger registration); the
        # returned objects aren't directly used in this scope.
        if production_mode:
            prod_config = setup_production_logging(json_output=False, verbose=False)
            prod_config.create_station_logger(station_id)
            audit_logger = prod_config.get_audit_logger()
        else:
            logging.getLogger(f"receivers.download.{station_id}")

        # Get station configuration
        station_config = get_station_config(station_id)
        if not station_config:
            raise ValueError(f"No configuration found for station {station_id}")

        # Known-issue gate: skip stations with explicitly configured issues.
        # Set known_issue = <reason> in stations.cfg for receivers that can't
        # produce data due to firmware bugs, broken hardware, etc.
        # outcome='expected' excludes these from the second-chance retry queue.
        known_issue = (station_config.get("known_issue") or "").strip()
        if known_issue:
            logger.info(
                f"⏭️  {station_id} ({session_type}) [known_issue:{known_issue}] — expected, not retried"
            )
            from ..utils.stall_timeout import record_download as _rd_ki

            _rd_ki(station_id, session_type, outcome="expected", message=known_issue)
            _record_batch_result(session_type, station_id, "expected", known_issue)
            return

        # Create receiver instance
        receiver = create_receiver(station_id, station_config)

        # Determine time range based on session type and lookback_periods
        # Use time_utils for consistent time calculation between CLI and scheduler
        from ..utils.time_utils import calculate_download_time_range

        start_time, end_time = calculate_download_time_range(
            session_type, lookback_periods
        )

        if session_type == "15s_24hr":
            frequency = "1D"
        else:
            frequency = "1H"

        # Download data with all our enhanced features
        result = receiver.download_data(
            start=start_time,
            end=end_time,
            session=session_type,
            ffrequency=frequency,
            sync=True,  # Always sync in scheduled mode
            archive=True,  # Always archive
            immediate_archive=True,  # Use fault-tolerant immediate archiving
            clean_tmp=False,  # Keep partial files so FTP REST resume works on retry
            compression=".gz",
            reverse_chronological=True,  # Prioritize latest data (like -D flag)
            retry_missing=True,  # Always retry known-missing files in scheduled mode
            loglevel=logging.INFO,
        )

        # In-job retry: if the download failed with a retryable error and
        # we have enough time remaining, wait briefly and try once more.
        # PolaRX5 supports FTP resume so partial progress is preserved.
        success_statuses = ("completed", "up_to_date", "dry_run")
        status = result.get("status", "completed")
        if status not in success_statuses and _is_retryable_download(result):
            elapsed = time.time() - job_start_time
            remaining = (timeout_minutes * 60) - elapsed
            # Only retry if we used less than 60% of the budget and have ≥2min left
            if elapsed < (timeout_minutes * 60 * 0.6) and remaining > 120:
                logger.info(
                    f"🔄 Retrying {station_id} ({session_type}) after 60s cooldown "
                    f"({remaining:.0f}s remaining in job budget)"
                )
                time.sleep(60)
                result = receiver.download_data(
                    start=start_time,
                    end=end_time,
                    session=session_type,
                    ffrequency=frequency,
                    sync=True,
                    archive=True,
                    immediate_archive=True,
                    clean_tmp=False,  # Keep partial files for resume
                    compression=".gz",
                    reverse_chronological=True,
                    retry_missing=True,
                    loglevel=logging.INFO,
                )
                status = result.get("status", "completed")

        # Check result status to determine success/failure
        # Possible statuses: completed, up_to_date, dry_run (success)
        #                    failed, unreachable, configuration_error (failure)
        files_downloaded = result.get("files_downloaded", 0)
        duration = result.get("duration", 0)
        downloaded_files = result.get("downloaded_files", [])

        # Mark download stage in pipeline
        if pipeline_job is not None and pipeline_store is not None:
            try:
                from .pipeline import PipelineStage

                if status not in success_statuses:
                    pipeline_job.mark_stage_failed(
                        PipelineStage.DOWNLOAD,
                        result.get("error_message", status),
                    )
                else:
                    pipeline_job.mark_stage_complete(
                        PipelineStage.DOWNLOAD,
                        output_files=downloaded_files,
                        metrics={
                            "files_downloaded": files_downloaded,
                            "duration": duration,
                        },
                    )
                pipeline_store.save_job(pipeline_job)
            except Exception:
                pass

        # Calculate total job duration for monitoring
        job_duration_seconds = time.time() - job_start_time
        job_duration_minutes = job_duration_seconds / 60

        # Monitor job duration relative to configured timeout
        timeout_threshold = timeout_minutes * 0.8  # 80% threshold for warnings
        if job_duration_minutes > timeout_threshold:
            percent_of_timeout = (job_duration_minutes / timeout_minutes) * 100
            logger.warning(
                f"⏱️  Long-running job: {station_id} ({session_type}) took {job_duration_minutes:.1f}min "
                f"({percent_of_timeout:.0f}% of {timeout_minutes}min timeout, {files_downloaded} files)"
            )

        # Log results to audit trail
        if audit_logger:
            audit_logger.log_download_session(
                station_id,
                {
                    "session": session_type,
                    "status": status,
                    "duration": duration,
                    "job_duration": job_duration_seconds,
                    "files_downloaded": files_downloaded,
                    "bytes_downloaded": result.get("bytes_downloaded", 0),
                    "errors": result.get("errors", 0),
                    "scheduled": True,
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "timeout_minutes": timeout_minutes,
                    "timeout_percent": (
                        (job_duration_minutes / timeout_minutes) * 100
                        if timeout_minutes > 0
                        else 0
                    ),
                },
            )

        # Report based on actual status with emoji-based logging style
        if status not in success_statuses:
            # Any non-success status: failed, unreachable, configuration_error, etc.
            error_msg = result.get("error_message") or result.get("error", status)
            category = _categorize_failure(str(error_msg))
            logger.error(
                f"❌ Failed: {station_id} ({session_type}) [{category}] - {error_msg} ({duration:.1f}s)"
            )
            from ..utils.stall_timeout import record_download

            record_download(
                station_id, session_type, outcome=status, message=str(error_msg)
            )
        elif status == "up_to_date":
            # All files already in archive - verified on disk
            logger.info(
                f"✅ Up-to-date: {station_id} ({session_type}) - {files_downloaded} files in {duration:.1f}s"
            )
        else:
            # Completed with downloads or dry_run
            if files_downloaded > 0:
                logger.info(
                    f"✅ Completed: {station_id} ({session_type}) - {files_downloaded} files in {duration:.1f}s"
                )
            else:
                files_checked = result.get("files_checked", 0)
                if files_checked > 0:
                    logger.info(
                        f"✅ Completed: {station_id} ({session_type}) - 0 files (already synced) in {duration:.1f}s"
                    )
                else:
                    logger.warning(
                        f"⚠️  No files: {station_id} ({session_type}) - 0 files checked/downloaded in {duration:.1f}s"
                    )

        # Accumulate result for batch summary (daily sessions only)
        _record_batch_result(
            session_type,
            station_id,
            "ok" if status in success_statuses else "fail",
            (
                result.get("error_message", status)
                if status not in success_statuses
                else ""
            ),
        )

        # Push-on-download: mirror just-downloaded raw to the long-term archive
        # immediately (low-latency write-through; the :45 sweep reconciles misses).
        # Best-effort — cannot harm the download. RINEX is pushed separately when
        # its async conversion completes (below).
        if status in success_statuses and downloaded_files:
            _push_to_storage(downloaded_files, logger)

        # Run RINEX conversion if enabled and download was successful
        if run_rinex and status in success_statuses:
            raw_files = result.get("downloaded_files", [])
            if raw_files:
                try:
                    from ..rinex.async_converter import submit_rinex_conversion

                    future = submit_rinex_conversion(
                        station_id, session_type, start_time, end_time
                    )

                    if future:
                        # Optional pipeline tracking — independent of the push.
                        if pipeline_job is not None and pipeline_store is not None:
                            try:
                                from .pipeline import PipelineStage

                                pipeline_job.mark_stage_started(PipelineStage.RINEX)
                                pipeline_store.save_job(pipeline_job)
                            except Exception:
                                pass

                        # On conversion completion (callback runs in the main
                        # process for the ProcessPool): push the derived RINEX to
                        # the archive AND, if enabled, mark the pipeline stage. The
                        # push must NOT depend on pipeline tracking — otherwise raw
                        # syncs but rinex silently doesn't when tracking is off.
                        def _on_rinex_done(f, pj=pipeline_job, ps=pipeline_store):
                            exc = f.exception()
                            if pj is not None and ps is not None:
                                try:
                                    from .pipeline import PipelineStage

                                    if exc:
                                        pj.mark_stage_failed(
                                            PipelineStage.RINEX, str(exc)
                                        )
                                    else:
                                        pj.mark_stage_complete(PipelineStage.RINEX)
                                    ps.save_job(pj)
                                except Exception:
                                    pass
                            if exc is None:
                                try:
                                    out = (f.result() or {}).get("output_files", [])
                                    _push_to_storage(out, logger)
                                except Exception:
                                    pass

                        future.add_done_callback(_on_rinex_done)
                except Exception as e:
                    logger.warning(f"⚠️  RINEX submission failed for {station_id}: {e}")

        # Extract health data from status_1hr SBF files and write to DB
        if session_type == "status_1hr" and status in success_statuses:
            if downloaded_files:
                # Mark health stage started in pipeline
                if pipeline_job is not None and pipeline_store is not None:
                    try:
                        from .pipeline import PipelineStage

                        pipeline_job.mark_stage_started(PipelineStage.HEALTH)
                        pipeline_store.save_job(pipeline_job)
                    except Exception:
                        pass

                _extract_and_store_health_data(station_id, downloaded_files, logger)

                # Mark health stage complete in pipeline
                if pipeline_job is not None and pipeline_store is not None:
                    try:
                        from .pipeline import PipelineStage

                        pipeline_job.mark_stage_complete(PipelineStage.HEALTH)
                        pipeline_store.save_job(pipeline_job)
                    except Exception:
                        pass

        # Final pipeline save
        if pipeline_job is not None and pipeline_store is not None:
            try:
                pipeline_store.save_job(pipeline_job)
            except Exception:
                pass

    except Exception as e:
        # Unexpected exception during download
        error_type = type(e).__name__
        logger.error(f"❌ Exception: {station_id} ({session_type}) - {error_type}: {e}")

        # Record in batch summary so the station shows up in the periodic
        # batch report — without this, a hard exception leaves the station
        # invisible (not in ok, not in fail, not in expected) and totals
        # silently under-count.
        _record_batch_result(session_type, station_id, "fail", f"{error_type}: {e}")

        # Mark pipeline as failed
        if pipeline_job is not None and pipeline_store is not None:
            try:
                from .pipeline import PipelineStage

                # Mark whatever stage was running as failed
                for stage in pipeline_job.stages:
                    from .pipeline import StageStatus

                    if pipeline_job.stages[stage].status == StageStatus.RUNNING:
                        pipeline_job.mark_stage_failed(stage, f"{error_type}: {e}")
                pipeline_store.save_job(pipeline_job)
            except Exception:
                pass

        # Log failure to audit trail
        if audit_logger:
            audit_logger.log_failure_event(
                station_id,
                {
                    "session": session_type,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "scheduled": True,
                },
            )


def _run_rinex_conversion(
    station_id: str,
    session_type: str,
    raw_files: List[str],
    station_config: Dict[str, Any],
    logger: logging.Logger,
):
    """Run RINEX conversion on downloaded files.

    Args:
        station_id: Station identifier
        session_type: Session type (15s_24hr, 1Hz_1hr)
        raw_files: List of paths to raw files to convert
        station_config: Station configuration dictionary
        logger: Logger instance
    """
    # Backfill RINEX is low-priority recovery of OLD data. Yield to live
    # downloads + inline RINEX when the box is under load — the raw converts on
    # the next backfill pass or the 6h reconciler once load drops. This function
    # is called only from the backfill path (never the inline submit_rinex_*
    # pool), so deferring here cannot stall a live download. Without this gate a
    # big historical backlog (e.g. the 2026-08-09 reconnection recovery) saturates
    # the box, the live load-gate then skips ~285 downloads/hour, and today's
    # data never arrives to be RINEX'd at all.
    monitor = _get_load_monitor()
    if monitor is not None:
        from .task_interface import TaskPriority

        if not monitor.can_start_job(TaskPriority.BACKFILL):
            load = monitor.get_load()
            logger.info(
                f"⏳ Backfill RINEX deferred: {station_id} ({session_type}) — "
                f"load high (cpu={load.cpu_load_1m:.1f}), will retry next pass"
            )
            return

    try:
        from .task_interface import TaskConfig, TaskFrequency, TaskType
        from .tasks.rinex_task import RINEXTask

        logger.info(
            f"🔄 Starting RINEX conversion: {station_id} ({len(raw_files)} files)"
        )
        start_time = time.time()

        # Determine RINEX output directory: sibling "rinex/" next to "raw/"
        # e.g. .../15s_24hr/raw/FILE.gz → .../15s_24hr/rinex/
        rinex_output_dir = None
        if raw_files:
            first_raw = Path(raw_files[0])
            if first_raw.parent.name == "raw":
                rinex_output_dir = first_raw.parent.parent / "rinex"
                rinex_output_dir.mkdir(parents=True, exist_ok=True)

        # Create task config
        config = TaskConfig(
            task_type=TaskType.RINEX,
            session_type=session_type,
            schedule_minute=0,
            distribution_window=10,
            frequency=(
                TaskFrequency.HOURLY
                if session_type == "1Hz_1hr"
                else TaskFrequency.DAILY
            ),
            lookback_periods=1,
            max_concurrent=1,
            timeout_minutes=30,
        )

        # Create and execute RINEX task
        task = RINEXTask(
            station_id=station_id,
            config=config,
            logger=logger,
            input_files=raw_files,
            output_dir=rinex_output_dir,
            rinex_version=3,
            apply_hatanaka=True,
            apply_header_corrections=True,
        )

        result = task.execute()
        duration = time.time() - start_time

        if result.success:
            files_converted = result.data.get("files_converted", 0)
            logger.info(
                f"✅ RINEX complete: {station_id} - {files_converted} files in {duration:.1f}s"
            )
        else:
            logger.warning(f"⚠️  RINEX partial/failed: {station_id} - {result.message}")

        # Track RINEX output files in file_tracking
        if result.output_files:
            _track_rinex_output_files(
                station_id, session_type, result.output_files, logger
            )

    except ImportError as e:
        logger.warning(f"⚠️  RINEX not available: {e}")
    except Exception as e:
        logger.error(f"❌ RINEX failed: {station_id} - {type(e).__name__}: {e}")


def _track_rinex_output_files(
    station_id: str, session_type: str, output_files: List[str], logger: logging.Logger
) -> None:
    """Record RINEX output files in file_tracking as '{session_type}_rinex'.

    Uses the same _parse_rinex_filename logic as the archive reconciler
    to extract file_date and file_hour from RINEX filenames.
    """
    import os
    import re
    from datetime import date as date_type
    from datetime import timedelta

    rinex_session = f"{session_type}_rinex"
    tracked = 0

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                for fpath in output_files:
                    try:
                        filename = os.path.basename(fpath)
                        fsize = (
                            os.path.getsize(fpath) if os.path.exists(fpath) else None
                        )

                        # Parse RINEX filename to get date/hour
                        file_date, file_hour = _parse_rinex_filename(
                            filename, station_id
                        )
                        if file_date is None:
                            continue

                        cur.execute(
                            """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'archived', %s)""",
                            (
                                station_id,
                                rinex_session,
                                file_date,
                                file_hour,
                                filename,
                                fsize,
                            ),
                        )
                        tracked += 1
                    except Exception as e:
                        logger.debug(f"Could not track RINEX file {fpath}: {e}")
                        continue
            conn.commit()

        if tracked > 0:
            logger.debug(
                f"Tracked {tracked} RINEX files for {station_id}/{rinex_session}"
            )

    except Exception as e:
        logger.warning(f"Failed to track RINEX files for {station_id}: {e}")


def _parse_rinex_filename(filename: str, station_id: str):
    """Parse RINEX filename to extract file_date and file_hour.

    Thin wrapper around RinexNamer.parse_date_hour(). Kept as a module-level
    function so APScheduler-serialized jobs can call it without importing
    the full rinex package at job-dispatch time.
    """
    from receivers.rinex.rinex_namer import RinexNamer

    return RinexNamer.parse_date_hour(filename, station_id=station_id)


def _extract_and_store_health_data(
    station_id: str, file_paths: List[str], logger: logging.Logger
) -> None:
    """Extract health data from status_1hr SBF files and write to database.

    Called after successful status_1hr downloads. Delegates to the shared
    extraction function in backfill module.

    Args:
        station_id: Station identifier
        file_paths: List of downloaded file paths
        logger: Logger instance
    """
    try:
        from .backfill import _extract_and_store_health

        start_time = time.time()
        imported = _extract_and_store_health(station_id, file_paths, logger)
        duration = time.time() - start_time

        if imported > 0:
            logger.info(
                f"Extracted health data: {station_id} - "
                f"{imported}/{len(file_paths)} files in {duration:.1f}s"
            )
    except ImportError as e:
        logger.debug(f"Health extraction not available: {e}")
    except Exception as e:
        logger.warning(f"Health extraction failed for {station_id}: {e}")


try:
    import logging.handlers

    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler

    HAS_APSCHEDULER = True
except ImportError as e:
    HAS_APSCHEDULER = False
    _import_error = str(e)


# Module-level status check function for APScheduler serialization
def _status_check_job(
    station_id: str, send_to_db: bool = True, send_to_icinga: bool = True
):
    """Run health status check for a station (standalone job function for APScheduler).

    Uses the same code path as: receivers health STATION --save-db --icinga
    by calling gather_comprehensive_health() rather than receiver.get_health_status()
    directly. This ensures NTRIP checks, file status, and power_type handling
    are included.

    Args:
        station_id: Station identifier
        send_to_db: Write health data to PostgreSQL
        send_to_icinga: Send passive checks to Icinga
    """
    logger = logging.getLogger(f"receivers.health.{station_id}")

    try:
        logger.info(f"Starting health check: {station_id}")
        start_time = time.time()

        # Import here to avoid circular imports
        from ..cli.main import create_receiver, get_station_config
        from ..health.live_health import gather_comprehensive_health

        # Get station configuration
        station_config = get_station_config(station_id)
        if not station_config:
            logger.error(f"❌ Health check failed: No config for {station_id}")
            return

        # Create receiver
        receiver = create_receiver(station_id, station_config)

        # Get comprehensive health — same as CLI 'receivers health' command
        try:
            health_data = gather_comprehensive_health(
                station_id,
                station_config,
                receiver,
                include_files=False,
                include_ntrip=True,
            )
        except Exception as e:
            logger.warning(f"Could not get live health from {station_id}: {e}")
            health_data = {"station_id": station_id, "error": str(e)}

        # Compare receiver identity against stations.cfg and update the
        # cfg_discrepancy log so `cfg list` / `cfg history` reflect the
        # latest probe. Best-effort: failures must not break the health job.
        try:
            from ..cfg.identity_check import flag_from_health_data

            flag_from_health_data(station_id, health_data, station_config, logger)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{station_id}] cfg discrepancy check failed: {exc}")

        # Write to PostgreSQL
        db_success = False
        if send_to_db and health_data:
            try:
                from ..health.db_writer import HealthDatabaseWriter

                writer = HealthDatabaseWriter()
                db_success = writer.write_health_data(health_data)
                if db_success:
                    logger.debug(f"Health data written to database for {station_id}")
                    # Also write ping and port status for Grafana dashboard
                    _write_connectivity_status(station_id, health_data, logger)
            except ImportError:
                logger.debug("PostgreSQL writer not available")
            except Exception as e:
                logger.warning(f"Database write failed for {station_id}: {e}")

        # Send to Icinga
        icinga_sent = 0
        if send_to_icinga and health_data:
            try:
                from ..monitoring.icinga_client import IcingaClient

                client = IcingaClient()
                results = client.send_health_from_json(health_data)
                icinga_sent = sum(
                    1 for r in results.values() if r.get("success", False)
                )
            except ImportError:
                logger.debug("Icinga client not available")
            except Exception as e:
                logger.warning(f"Icinga send failed for {station_id}: {e}")

        duration = time.time() - start_time
        status_parts = []
        if db_success:
            status_parts.append("DB")
        if icinga_sent > 0:
            status_parts.append(f"Icinga({icinga_sent})")

        status_str = ", ".join(status_parts) if status_parts else "no targets"
        logger.info(
            f"✅ Health check complete: {station_id} - {status_str} ({duration:.1f}s)"
        )

    except Exception as e:
        logger.error(f"❌ Health check failed: {station_id} - {type(e).__name__}: {e}")

        # Record the failure as an EXPLICIT offline sample instead of letting it
        # become a gap. A missing row is ambiguous — it cannot distinguish "the
        # station did not answer" from "the scheduler was not running" — and the
        # receiver-retained metrics that arrive later via status_1hr DO backfill
        # this window (voltage, temperature, satellites all reappear at their
        # original timestamps), so without a row the outage silently vanishes
        # from the graph once the late data lands.
        #
        # The normal failure path already handles this: gather_comprehensive_health
        # raising leaves an error-only dict, which still reaches
        # _write_connectivity_status and writes is_online=false. Only failures
        # raised BEFORE that inner try — create_receiver, the imports, anything
        # unexpected — land here, and those were writing nothing at all.
        # status_task.py has done this on its own failure path all along.
        if send_to_db:
            try:
                from ..health.connectivity_writer import ConnectivityWriter

                ConnectivityWriter(logger).write_ping_only(
                    station_id,
                    {"connection": {"error": f"{type(e).__name__}: {e}"}},
                )
            except Exception:  # noqa: BLE001 - recording must never break the job
                logger.debug(
                    f"[{station_id}] could not record offline ping sample",
                    exc_info=True,
                )


@dataclass
class ScheduleConfig:
    """Configuration for scheduled downloads.

    Supports both legacy format (schedule_minute + frequency) and new flexible format (schedule).

    New flexible schedule formats:
    - Single time: "00:10" (daily at 00:10)
    - Hourly minute: ":15" (every hour at :15)
    - Interval: "6h", "45m" (every N hours/minutes)
    - Multiple times: ["00:10", "08:10", "16:10"]
    - Cron expression: "cron: */15 * * * *"

    Legacy format (still supported):
    - schedule_minute + frequency: "daily" or "hourly"
    """

    session_type: str
    distribution_window: int  # Minutes to spread downloads across
    enabled: bool = True
    max_concurrent: int = 3
    timeout_minutes: int = 30
    lookback_periods: int = (
        1  # Number of periods to check (1=last period only, 2=last 2 periods, etc.)
    )
    rinex: bool = False  # Whether to run RINEX conversion after download

    # New flexible schedule format (preferred)
    schedule: Optional[Union[str, List[str], Dict[str, Any]]] = None

    # Midnight offset: extra minutes added at hour 0 to avoid clashing with
    # daily sessions (e.g., 15s_24hr).  Only meaningful for hourly sessions.
    midnight_offset: int = 0

    # Legacy format fields (for backward compatibility)
    schedule_minute: Optional[int] = None
    frequency: Optional[str] = None

    def __post_init__(self):
        """Convert legacy format to new format if needed."""
        if self.schedule is None:
            # No new format specified, check for legacy format
            if self.schedule_minute is not None and self.frequency is not None:
                # Convert legacy to dict format for parsing
                self.schedule = {
                    "schedule_minute": self.schedule_minute,
                    "frequency": self.frequency,
                }
            else:
                raise ValueError(
                    f"Session {self.session_type}: Must specify either 'schedule' or "
                    f"both 'schedule_minute' and 'frequency'"
                )


class BulkDownloadScheduler:
    """APScheduler-based bulk download system with full manual compatibility."""

    def __init__(
        self,
        database_url: str = None,
        log_dir: Path = None,
        production_mode: bool = True,
        max_workers: int = None,
        station_filter: List[str] = None,
        max_stations_per_session: int = None,
        config_path: Path = None,
        scheduler_types: List[str] = None,
    ):
        if not HAS_APSCHEDULER:
            raise ImportError(
                "APScheduler not available. Install with: pip install apscheduler"
            )

        # Load YAML configuration (with fallback to defaults)
        from .config_loader import load_scheduler_config

        self.yaml_config = load_scheduler_config(config_path)

        # Apply configuration (CLI args override YAML)
        scheduler_cfg = self.yaml_config["scheduler"]

        # Expand ~ in database path from YAML
        db_path = scheduler_cfg.get(
            "database", f"{Path.home()}/.cache/gps_receivers/scheduler.db"
        )
        if isinstance(db_path, str):
            db_path = str(Path(db_path).expanduser())
        self.database_url = database_url or f"sqlite:///{db_path}"

        # Expand ~ in log_dir path from YAML
        log_path = scheduler_cfg.get(
            "log_dir", Path.home() / ".cache" / "gps_receivers" / "logs"
        )
        if isinstance(log_path, str):
            log_path = Path(log_path).expanduser()
        self.log_dir = log_dir or log_path
        self.production_mode = production_mode
        self.max_workers = (
            max_workers
            if max_workers is not None
            else scheduler_cfg.get("max_workers", 15)
        )
        self.station_filter = (
            [s.upper() for s in station_filter] if station_filter else None
        )
        self.max_stations_per_session = max_stations_per_session

        # Parse scheduler_types filter
        # Valid types: health, 15s_24hr, 1Hz_1hr, status_1hr, downloads (all download sessions), all
        self.scheduler_types = self._parse_scheduler_types(scheduler_types)

        # PID lock file to prevent duplicate instances
        lock_dir = Path(db_path).parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = lock_dir / "scheduler.lock"
        self._lock_fd = None

        # Set up logging
        self._setup_logging()

        # Initialize scheduler with persistent job store
        self._setup_scheduler()

        # Load schedule configurations from YAML (with defaults as fallback)
        self.schedule_configs = {}
        for session_type in ["15s_24hr", "1Hz_1hr", "status_1hr"]:
            session_cfg = self.yaml_config["sessions"].get(session_type, {})

            # Check for new flexible 'schedule' field first
            schedule = session_cfg.get("schedule")

            # If no new schedule field, use legacy format (schedule_minute + frequency)
            midnight_offset = session_cfg.get("midnight_offset", 0)

            if schedule is None:
                schedule_minute = session_cfg.get(
                    "schedule_minute",
                    (
                        10
                        if session_type == "15s_24hr"
                        else 15 if session_type == "1Hz_1hr" else 25
                    ),
                )
                frequency = session_cfg.get(
                    "frequency", "daily" if session_type == "15s_24hr" else "hourly"
                )

                self.schedule_configs[session_type] = ScheduleConfig(
                    session_type=session_type,
                    schedule_minute=schedule_minute,
                    frequency=frequency,
                    distribution_window=session_cfg.get(
                        "distribution_window", 10 if session_type != "status_1hr" else 5
                    ),
                    enabled=session_cfg.get("enabled", True),
                    max_concurrent=session_cfg.get(
                        "max_concurrent",
                        (
                            3
                            if session_type == "15s_24hr"
                            else 4 if session_type == "1Hz_1hr" else 5
                        ),
                    ),
                    timeout_minutes=session_cfg.get(
                        "timeout_minutes",
                        (
                            45
                            if session_type == "15s_24hr"
                            else 30 if session_type == "1Hz_1hr" else 15
                        ),
                    ),
                    lookback_periods=session_cfg.get("lookback_periods", 1),
                    rinex=session_cfg.get("rinex", False),
                    midnight_offset=midnight_offset,
                )
            else:
                # New flexible schedule format
                self.schedule_configs[session_type] = ScheduleConfig(
                    session_type=session_type,
                    schedule=schedule,
                    distribution_window=session_cfg.get(
                        "distribution_window", 10 if session_type != "status_1hr" else 5
                    ),
                    enabled=session_cfg.get("enabled", True),
                    max_concurrent=session_cfg.get(
                        "max_concurrent",
                        (
                            3
                            if session_type == "15s_24hr"
                            else 4 if session_type == "1Hz_1hr" else 5
                        ),
                    ),
                    timeout_minutes=session_cfg.get(
                        "timeout_minutes",
                        (
                            45
                            if session_type == "15s_24hr"
                            else 30 if session_type == "1Hz_1hr" else 15
                        ),
                    ),
                    lookback_periods=session_cfg.get("lookback_periods", 1),
                    rinex=session_cfg.get("rinex", False),
                    midnight_offset=midnight_offset,
                )

        # Load station configurations
        self.stations = self._load_station_configs()

        # Load receiver session capabilities from receivers.cfg
        self.receiver_sessions = self._load_receiver_session_capabilities()

        # Track running jobs
        self.running_jobs = {}

        # Set module-level reference for config watcher job
        global _scheduler_instance
        _scheduler_instance = self

        # Initialize load monitor (module-level for access by job functions)
        global _load_monitor
        load_cfg = self.yaml_config.get("load_monitoring", {})
        if load_cfg.get("enabled", False):
            from .load_monitor import LoadMonitor

            _load_monitor = LoadMonitor(load_cfg)
            self.logger.info(
                f"Load monitor enabled (CPU max={load_cfg.get('max_cpu_load', 8.0)}, "
                f"network max={load_cfg.get('max_network_mbps', 80)} Mbps, "
                f"jobs max={load_cfg.get('max_active_jobs', 80)})"
            )
        else:
            _load_monitor = None

    def _parse_scheduler_types(self, scheduler_types: List[str] = None) -> dict:
        """Parse scheduler types filter into a structured dict.

        Args:
            scheduler_types: List of types like ['health', '15s_24hr'] or ['downloads', 'health']

        Returns:
            Dict with keys: health, 15s_24hr, 1Hz_1hr, status_1hr (all True/False)
        """
        # Default: all enabled
        result = {
            "health": True,
            "15s_24hr": True,
            "1Hz_1hr": True,
            "status_1hr": True,
        }

        if scheduler_types is None or "all" in scheduler_types:
            return result

        # Start with all disabled
        result = {k: False for k in result}

        for stype in scheduler_types:
            stype = stype.lower().strip()

            if stype == "health":
                result["health"] = True
            elif stype == "downloads":
                # Enable all download sessions
                result["15s_24hr"] = True
                result["1Hz_1hr"] = True
                result["status_1hr"] = True
            elif stype in ["15s_24hr", "15s", "daily"]:
                result["15s_24hr"] = True
            elif stype in ["1hz_1hr", "1hz", "hourly"]:
                result["1Hz_1hr"] = True
            elif stype in ["status_1hr", "status"]:
                result["status_1hr"] = True

        return result

    def _setup_logging(self):
        """Set up scheduler logging via the unified logging system."""
        from ..logging_config import setup_logging

        self.logger = setup_logging(
            log_dir=self.log_dir,
            component="scheduler",
        )

    def _setup_scheduler(self):
        """Initialize APScheduler with persistent storage."""

        # Job store configuration
        jobstores = {"default": SQLAlchemyJobStore(url=self.database_url)}

        # Executor configuration
        # Separate executors prevent different workloads from starving each other:
        #   default  — live downloads
        #   health   — real-time health monitoring
        #   backfill — backfill, gap detection, archive reconciler
        health_workers = max(1, min(self.max_workers // 3, 30))
        backfill_workers = max(self.max_workers // 5, 5)
        executors = {
            "default": ThreadPoolExecutor(self.max_workers),
            "health": ThreadPoolExecutor(health_workers),
            "backfill": ThreadPoolExecutor(backfill_workers),
        }

        # Job defaults — read from YAML config if available
        yaml_job_defaults = self.yaml_config.get("scheduler", {}).get(
            "job_defaults", {}
        )
        job_defaults = {
            "coalesce": yaml_job_defaults.get("coalesce", True),
            "max_instances": yaml_job_defaults.get("max_instances", 3),
            "misfire_grace_time": yaml_job_defaults.get("misfire_grace_time", 300),
        }

        # Initialize scheduler
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores, executors=executors, job_defaults=job_defaults
        )

        # Add event listeners
        self.scheduler.add_listener(self._job_executed, EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(self._job_error, EVENT_JOB_ERROR)

    def _load_station_configs(self) -> Dict[str, Dict[str, Any]]:
        """Load station configurations from gps_parser."""
        stations = {}

        # Read configured_serial / configured_firmware directly from stations.cfg.
        # get_all_station_configs() does not expose these fields.
        cfg_identity: Dict[str, Dict[str, Any]] = {}
        cfg_path = self._get_stations_cfg_path()
        if cfg_path and cfg_path.exists():
            import configparser as _cp

            _parser = _cp.ConfigParser(strict=False)
            _parser.read(str(cfg_path))
            for section in _parser.sections():
                sid = section.upper()
                cfg_identity[sid] = {
                    "configured_serial": _parser.get(
                        section, "receiver_serial", fallback=None
                    )
                    or None,
                    "configured_firmware": _parser.get(
                        section, "receiver_firmware_version", fallback=None
                    )
                    or None,
                }

        try:
            # Use the existing station loading from CLI
            from ..cli.main import get_all_station_configs

            all_stations = get_all_station_configs()

            for station_id, config in all_stations.items():
                # Belt-and-braces: passive (data-source-only) stations are
                # already filtered in get_all_station_configs(); never let
                # one reach the scheduler even if a caller bypasses that.
                if str(config.get("station_role", "")).strip().lower() == "passive":
                    continue
                # Extract relevant configuration
                # 'active' is the default — normalize to None (NULL in DB)
                station_status = config.get("station_status")
                if station_status and station_status.lower() == "active":
                    station_status = None
                health_check = config.get("health_check")
                if health_check and health_check.lower() == "active":
                    health_check = None
                receiver_type = config.get("receiver_type", "unknown")

                # Auto-detect inactive stations: flag if receiver_type is genuinely absent
                if not station_status:
                    rx_missing = receiver_type.lower() in ("none", "", "unknown")
                    if rx_missing:
                        station_status = "inactive"

                ident = cfg_identity.get(station_id, {})
                # acquisition_mode + the on-disk sessions a stream station logs
                # (remote_sessions) drive the session-aware download skip in
                # _get_stations_for_session — they MUST be carried here or the
                # skip silently no-ops (config.get returns "" for absent keys).
                from ..streaming.config import get_acquisition_mode

                remote_sessions = [
                    s.strip()
                    for s in str(config.get("remote_sessions") or "").split(",")
                    if s.strip()
                ]
                stations[station_id] = {
                    "station_id": station_id,
                    "receiver_type": receiver_type,
                    "ip_number": config.get("ip_number", ""),
                    "ip_port": config.get("ip_port", 21),
                    "enabled": config.get("enabled", True),
                    "timeout_category": config.get("timeout_category", "default"),
                    "station_status": station_status,
                    "health_check": health_check,
                    "acquisition_mode": get_acquisition_mode(config),
                    "remote_sessions": remote_sessions,
                    "configured_serial": ident.get("configured_serial"),
                    "configured_firmware": ident.get("configured_firmware"),
                }

        except Exception as e:
            self.logger.error(f"Failed to load station configurations: {e}")
            # Fallback: empty station list
            stations = {}

        self.logger.info(f"Loaded {len(stations)} station configurations")
        return stations

    def _sync_station_status_to_db(self) -> None:
        """Sync station_status and health_check values from config to the database.

        Two separate fields:
        - station_status: lifecycle (NULL=active, discontinued, inactive)
        - health_check: monitoring mode (NULL=active, passive)

        This runs at startup and when config file changes are detected.
        """
        try:
            from ..health.database_factory import DatabaseConnectionFactory

            with DatabaseConnectionFactory.connection() as conn:
                with conn.cursor() as cur:
                    status_synced = 0
                    hc_synced = 0
                    identity_synced = 0
                    for station_id, config in self.stations.items():
                        station_status = config.get("station_status")
                        health_check = config.get("health_check")
                        configured_serial = config.get("configured_serial")
                        configured_firmware = config.get("configured_firmware")
                        cur.execute(
                            """
                            UPDATE stations
                            SET station_status    = %s,
                                health_check      = %s,
                                configured_serial   = %s,
                                configured_firmware = %s
                            WHERE sid = %s
                              AND (station_status    IS DISTINCT FROM %s
                                OR health_check      IS DISTINCT FROM %s
                                OR configured_serial   IS DISTINCT FROM %s
                                OR configured_firmware IS DISTINCT FROM %s)
                        """,
                            (
                                station_status,
                                health_check,
                                configured_serial,
                                configured_firmware,
                                station_id,
                                station_status,
                                health_check,
                                configured_serial,
                                configured_firmware,
                            ),
                        )
                        if cur.rowcount > 0:
                            if station_status:
                                status_synced += 1
                            if health_check:
                                hc_synced += 1
                            if configured_serial or configured_firmware:
                                identity_synced += 1

                    if status_synced or hc_synced or identity_synced:
                        self.logger.info(
                            f"Synced to DB: {status_synced} station_status, "
                            f"{hc_synced} health_check, {identity_synced} configured_identity"
                        )
                    else:
                        self.logger.debug(
                            "station_status/health_check/configured_identity already in sync with DB"
                        )

                    # Suppress stations that disappeared from stations.cfg.
                    if self.stations:
                        placeholders = ",".join(["%s"] * len(self.stations))
                        cur.execute(
                            f"""
                            UPDATE stations
                            SET station_status = 'suppressed', updated_at = NOW()
                            WHERE sid NOT IN ({placeholders})
                              AND station_status IS NULL
                            RETURNING sid
                            """,
                            list(self.stations.keys()),
                        )
                        gone = [r[0] for r in cur.fetchall()]
                        if gone:
                            self.logger.warning(
                                "Suppressed %d station(s) no longer in stations.cfg: %s",
                                len(gone),
                                ", ".join(gone),
                            )

        except ImportError:
            self.logger.debug("psycopg2 not available — skipping status sync")
        except Exception as e:
            self.logger.warning(f"Failed to sync station status to DB: {e}")

    def _get_stations_cfg_path(self) -> Optional[Path]:
        """Get the path to stations.cfg using gps_parser."""
        try:
            import gps_parser

            parser = gps_parser.ConfigParser()
            return Path(parser.get_stations_config_path())
        except Exception:
            return None

    def _check_config_changes(self) -> None:
        """Check if stations.cfg has been modified and reload if so.

        Scheduled as a periodic job. Compares file mtime to detect changes.
        When a change is found, reloads station configs and syncs to DB.
        """
        cfg_path = self._get_stations_cfg_path()
        if not cfg_path or not cfg_path.exists():
            return

        try:
            current_mtime = cfg_path.stat().st_mtime
        except OSError:
            return

        if not hasattr(self, "_config_mtime"):
            self._config_mtime = current_mtime
            return

        if current_mtime == self._config_mtime:
            return

        self.logger.info(
            f"stations.cfg changed (mtime {self._config_mtime:.0f} → {current_mtime:.0f}), "
            f"reloading station configs"
        )
        self._config_mtime = current_mtime

        old_stations = self.stations
        self.stations = self._load_station_configs()
        self._sync_station_status_to_db()

        # Log meaningful changes
        new_ids = set(self.stations) - set(old_stations)
        removed_ids = set(old_stations) - set(self.stations)
        changed = []
        for sid in set(self.stations) & set(old_stations):
            old_ss = old_stations[sid].get("station_status")
            new_ss = self.stations[sid].get("station_status")
            old_hc = old_stations[sid].get("health_check")
            new_hc = self.stations[sid].get("health_check")
            if old_ss != new_ss:
                changed.append(
                    f"{sid}: status {old_ss or 'active'} → {new_ss or 'active'}"
                )
            if old_hc != new_hc:
                changed.append(
                    f"{sid}: health_check {old_hc or 'active'} → {new_hc or 'active'}"
                )

        if new_ids:
            self.logger.info(f"New stations: {', '.join(sorted(new_ids))}")
        if removed_ids:
            self.logger.info(f"Removed stations: {', '.join(sorted(removed_ids))}")
        if changed:
            self.logger.info(f"Config changes: {'; '.join(changed)}")

        # A config edit can flip a station's acquisition_mode to/from "stream"
        # (or add/remove a stream station). The .bnc + .SKL configs that BNC and
        # the pipeline read are only regenerated by the daily stream_config_refresh
        # job, so without this an acquisition_mode=stream flip would not take
        # effect until 06:00. Re-run the refresh + supervise now so stream changes
        # are picked up on the same ~1-min cadence as status changes. Idempotent
        # and stream-stations-only; no-op when stream capture is disabled.
        self._refresh_stream_capture_on_config_change()

    def _refresh_stream_capture_on_config_change(self) -> None:
        """(Re)generate stream configs + (re)start daemons after a cfg change.

        Gated on ``stream_capture.enabled`` so it is a no-op in environments
        without BNC. Errors are isolated — a stream refresh failure must never
        propagate out of the config watcher and kill the job.
        """
        if not self.yaml_config.get("stream_capture", {}).get("enabled", False):
            return
        try:
            from .stream_scheduler import (
                _run_stream_config_refresh_job,
                _run_stream_supervise_job,
            )

            self.logger.info("Config change: refreshing stream capture configs")
            _run_stream_config_refresh_job()
            _run_stream_supervise_job()
        except (
            Exception
        ) as e:  # noqa: BLE001 — never let stream refresh kill the watcher
            self.logger.error(f"Stream refresh after config change failed: {e}")

    def _reseed_areas(self) -> None:
        """Rewrite ``station_area_members`` from the deployed station_areas.yaml."""
        try:
            from ..db.seeder import Seeder

            result = Seeder().seed_areas()
            self.logger.info(
                "Reseeded station areas: %s areas, %s members",
                result.get("areas"),
                result.get("members"),
            )
        except Exception as e:  # noqa: BLE001 — never let a reseed error kill the job
            self.logger.error(f"Area reseed failed: {e}")

    def _check_areas_config_changes(self) -> None:
        """Watch ``station_areas.yaml`` mtime and reseed areas when it changes.

        Areas are config (gps-config-data → sync timer → runtime config dir), so
        an edit should apply on its own, mirroring the ``stations.cfg`` auto-sync.
        On the first call (scheduler startup) it reseeds once so the DB matches
        the deployed yaml regardless of how the station list was last seeded; on
        later calls it reseeds only when the file's mtime changes.
        """
        from ..db.seeder import resolve_areas_yaml

        try:
            areas_path = resolve_areas_yaml()
            current_mtime = areas_path.stat().st_mtime
        except OSError:
            return

        if not hasattr(self, "_areas_mtime"):
            # Startup: align the DB with the deployed yaml, then track mtime.
            self._areas_mtime = current_mtime
            self._reseed_areas()
            return

        if current_mtime == self._areas_mtime:
            return

        self.logger.info(
            "station_areas.yaml changed (mtime %.0f → %.0f), reseeding areas",
            self._areas_mtime,
            current_mtime,
        )
        self._areas_mtime = current_mtime
        self._reseed_areas()

    def _load_receiver_session_capabilities(self) -> Dict[str, List[str]]:
        """Load session capabilities for each receiver type from receivers.cfg.

        Returns:
            Dict mapping receiver_type (lowercase) to list of supported sessions
            Example: {'polarx5': ['15s_24hr', '1Hz_1hr', 'status_1hr'],
                     'netr9': ['15s_24hr', '1Hz_1hr']}
        """
        import configparser
        from pathlib import Path

        capabilities = {}

        try:
            # Find receivers.cfg using gps_parser (respects GPS_CONFIG_PATH)
            try:
                import gps_parser

                parser_config = gps_parser.ConfigParser()
                gps_config_dir = parser_config.config_path
                config_path = Path(gps_config_dir) / "receivers.cfg"
            except (ImportError, Exception) as e:
                self.logger.debug(f"Could not get config dir from gps_parser: {e}")
                # Fallback to standard location
                config_path = Path.home() / ".config" / "gpsconfig" / "receivers.cfg"

            if not config_path.exists():
                self.logger.warning(
                    f"receivers.cfg not found at {config_path}, all sessions will be attempted"
                )
                return {}

            config = configparser.ConfigParser()
            config.read(config_path)

            # Check each receiver type section
            for receiver_type in [
                "polarx5",
                "mosaic-x5",
                "netr5",
                "netr9",
                "netrs",
                "g10",
            ]:
                if receiver_type not in config:
                    continue

                sessions = []
                # Check which session_map_* keys exist
                # Note: ConfigParser keys are case-insensitive, but we need to check the actual keys
                # because receivers.cfg uses lowercase 'hz' (session_map_1hz_1hr) while our
                # session name uses mixed case 'Hz' (1Hz_1hr)
                for session in ["15s_24hr", "1Hz_1hr", "status_1hr"]:
                    # Try both the session name as-is and lowercase version
                    key = f"session_map_{session}"
                    key_lower = f"session_map_{session.lower()}"

                    # Check if either version exists in config
                    if (
                        key in config[receiver_type]
                        or key_lower in config[receiver_type]
                    ):
                        sessions.append(session)

                capabilities[receiver_type] = sessions
                self.logger.debug(
                    f"Receiver {receiver_type} supports sessions: {sessions}"
                )

            self.logger.info(
                f"Loaded session capabilities for {len(capabilities)} receiver types"
            )

        except Exception as e:
            self.logger.error(f"Failed to load receiver session capabilities: {e}")

        return capabilities

    def schedule_all_sessions(self):
        """Schedule all configured download sessions with interleaved job creation.

        Creates jobs in round-robin order by station to ensure all session types
        are distributed evenly in the job queue when using interval triggers.

        Order: AFST(15s, 1Hz, status) → ALFD(15s, 1Hz, status) → ...
        Not: 15s(AFST,ALFD,...) → 1Hz(AFST,ALFD,...) → status(...)
        """

        # Build station lists for each session type
        session_stations = {}
        for session_type, config in self.schedule_configs.items():
            # Check scheduler_types filter
            if not self.scheduler_types.get(session_type, True):
                self.logger.info(f"Skipping session (--only filter): {session_type}")
                continue

            if not config.enabled:
                self.logger.info(f"Skipping disabled session: {session_type}")
                continue

            stations_for_session = self._get_stations_for_session(session_type)
            if not stations_for_session:
                self.logger.warning(
                    f"No stations configured for session: {session_type}"
                )
                continue

            session_stations[session_type] = stations_for_session

        # Create jobs in interleaved order (all sessions for station1, then station2, etc.)
        # This ensures when interval triggers fire all jobs simultaneously, the queue
        # contains a mix of all session types, not just the first session type
        all_stations = set()
        for stations in session_stations.values():
            all_stations.update(stations)
        all_stations = sorted(all_stations)  # Consistent ordering

        total_jobs = 0
        midnight_jobs = 0
        for station_id in all_stations:
            # Schedule all session types for this station
            for session_type, stations in session_stations.items():
                if station_id not in stations:
                    continue

                config = self.schedule_configs[session_type]
                station_index = stations.index(station_id)

                # Parse schedule and apply distribution window
                base_trigger = parse_schedule(config.schedule)

                # Midnight offset handling: for hourly sessions with midnight_offset,
                # create two job sets — one for hours 1-23 (normal) and one for
                # hour 0 (offset).  Pure cron-based, no runtime coordination.
                if (
                    config.midnight_offset > 0
                    and base_trigger.trigger_type == "cron"
                    and "hour" not in base_trigger.trigger_kwargs
                ):
                    # Hours 1-23: normal schedule
                    normal_trigger = ScheduleTrigger(
                        trigger_type="cron",
                        trigger_kwargs={**base_trigger.trigger_kwargs, "hour": "1-23"},
                        description=f"{base_trigger.description} (hours 1-23)",
                    )
                    trigger_type, trigger_kwargs = apply_distribution_window(
                        normal_trigger,
                        station_index,
                        len(stations),
                        config.distribution_window,
                    )
                    job_id = f"{session_type}_{station_id}"
                    self.scheduler.add_job(
                        func=_download_station_data_job,
                        trigger=trigger_type,
                        args=[
                            station_id,
                            session_type,
                            self.production_mode,
                            config.lookback_periods,
                            config.timeout_minutes,
                            config.rinex,
                        ],
                        id=job_id,
                        replace_existing=True,
                        **trigger_kwargs,
                    )
                    total_jobs += 1

                    # Hour 0: offset schedule
                    base_minute = base_trigger.trigger_kwargs.get("minute", 0)
                    if isinstance(base_minute, int):
                        midnight_minute = base_minute + config.midnight_offset
                    else:
                        midnight_minute = config.midnight_offset
                    midnight_trigger = ScheduleTrigger(
                        trigger_type="cron",
                        trigger_kwargs={"hour": 0, "minute": midnight_minute},
                        description=f"{base_trigger.description} (hour 0, offset +{config.midnight_offset}m)",
                    )
                    trigger_type, trigger_kwargs = apply_distribution_window(
                        midnight_trigger,
                        station_index,
                        len(stations),
                        config.distribution_window,
                    )
                    midnight_job_id = f"{session_type}_midnight_{station_id}"
                    self.scheduler.add_job(
                        func=_download_station_data_job,
                        trigger=trigger_type,
                        args=[
                            station_id,
                            session_type,
                            self.production_mode,
                            config.lookback_periods,
                            config.timeout_minutes,
                            config.rinex,
                        ],
                        id=midnight_job_id,
                        replace_existing=True,
                        **trigger_kwargs,
                    )
                    midnight_jobs += 1
                    total_jobs += 1
                else:
                    # Standard scheduling (no midnight split)
                    trigger_type, trigger_kwargs = apply_distribution_window(
                        base_trigger,
                        station_index,
                        len(stations),
                        config.distribution_window,
                    )
                    job_id = f"{session_type}_{station_id}"
                    self.scheduler.add_job(
                        func=_download_station_data_job,
                        trigger=trigger_type,
                        args=[
                            station_id,
                            session_type,
                            self.production_mode,
                            config.lookback_periods,
                            config.timeout_minutes,
                            config.rinex,
                        ],
                        id=job_id,
                        replace_existing=True,
                        **trigger_kwargs,
                    )
                    total_jobs += 1

        # Log summary + register batch summary jobs for daily sessions
        for session_type, stations in session_stations.items():
            config = self.schedule_configs[session_type]
            base_trigger = parse_schedule(config.schedule)
            extra = ""
            if config.midnight_offset > 0:
                extra = f", midnight_offset={config.midnight_offset}m"
            self.logger.info(
                f"Scheduled {len(stations)} stations for {session_type} "
                f"(window={config.distribution_window}m, {base_trigger.description}{extra})"
            )
            # Register batch-summary and second-chance jobs for daily sessions
            # only — sessions that fire once per day with an explicit hour+minute.
            #
            # Hourly sessions (":15", interval triggers) skip this on purpose:
            # the next primary cycle is at most 60 min later, so a 30-min
            # second-chance retry would just race the next cycle, and a
            # 50-min batch summary would mix windows.
            #
            # Timeline for 15s_24hr at 00:01, distribution_window=10:
            #   00:01-00:11  primary downloads
            #   00:41        second-chance parallel retry starts (window + 30 min)
            #   00:49        second-chance done (~8 min for 42 stations × 8 workers)
            #   01:01        batch summary fires (window + 50 min), captures all results
            tkw = base_trigger.trigger_kwargs
            is_daily_cron = (
                base_trigger.trigger_type == "cron"
                and "hour" in tkw
                and "minute" in tkw
            )
            if is_daily_cron:
                sched_hour = int(str(tkw["hour"]).split(",")[0])
                sched_minute = int(tkw["minute"])

                # Batch summary: fires after second-chance has had time to finish.
                total_offset = config.distribution_window + 50
                summary_minute = (sched_minute + total_offset) % 60
                summary_hour = (sched_hour + (sched_minute + total_offset) // 60) % 24
                self.scheduler.add_job(
                    func=_log_batch_summary_job,
                    trigger="cron",
                    args=[session_type],
                    hour=summary_hour,
                    minute=summary_minute,
                    id=f"{session_type}_batch_summary",
                    replace_existing=True,
                    misfire_grace_time=600,
                )
                self.logger.debug(
                    f"Batch summary for {session_type} scheduled at "
                    f"{summary_hour:02d}:{summary_minute:02d} UTC"
                )

                # Second-chance retry: parallel, fires 30 min after session start.
                # Runs up to _RETRY_MAX_WORKERS concurrent downloads (~8-10 min total).
                # Results flow into _BATCH_STATS so the batch summary is complete.
                # Example: session at 00:01, window=10 → second-chance at 00:41.
                retry_total = config.distribution_window + 30
                retry_minute = (sched_minute + retry_total) % 60
                retry_hour = (sched_hour + (sched_minute + retry_total) // 60) % 24
                self.scheduler.add_job(
                    func=_retry_failed_daily_job,
                    trigger="cron",
                    args=[session_type],
                    hour=retry_hour,
                    minute=retry_minute,
                    id=f"{session_type}_second_chance",
                    replace_existing=True,
                    misfire_grace_time=600,
                    executor="backfill",
                    max_instances=1,
                )
                self.logger.debug(
                    f"Second-chance retry for {session_type} scheduled at "
                    f"{retry_hour:02d}:{retry_minute:02d} UTC"
                )
            else:
                # Non-daily-cron schedule (interval, hourly with no explicit
                # `hour` key, etc.) — skipped intentionally. Log so future
                # debugging makes the absence visible instead of silent.
                self.logger.debug(
                    f"{session_type}: trigger {base_trigger.trigger_type} "
                    f"({tkw}) is not a daily cron — skipping batch summary "
                    f"and second-chance retry registration."
                )

        if midnight_jobs:
            self.logger.info(f"Created {midnight_jobs} midnight-offset jobs")
        self.logger.info(f"Total: {total_jobs} download jobs scheduled")

        # Schedule health monitoring if enabled
        self._schedule_health_monitoring()

        # Sync station_status values to DB at startup
        self._sync_station_status_to_db()

        # Schedule config file watcher (every 1 minute)
        self._schedule_config_watcher()

        # Schedule liveness heartbeat (every 1 minute) — external monitor alerts
        # when it goes stale (the "active but wedged" detector, incident 2026-07-21)
        self._schedule_heartbeat()

        # Schedule backfill, gap detection, archive reconciler, and integrity checker
        self._schedule_multi_session_backfill()
        self._schedule_gap_detection()
        self._schedule_archive_reconciler()
        self._schedule_integrity_checker()
        self._schedule_archive_sync()
        self._schedule_epos_disseminate()
        self._schedule_epos_reactive()
        self._schedule_archive_verify()
        self._schedule_local_prune()
        self._schedule_receiver_horizon_probe()
        self._schedule_morning_recovery()
        self._schedule_long_term_backfill()
        self._schedule_reconnection_backfill()
        self._schedule_stream_capture()

        # Catch up any missed daily downloads (e.g., 15s_24hr if scheduler started after midnight)
        self._schedule_daily_catchup(session_stations)

        # Bootstrap: aggressive initial downloads on cold start
        self._schedule_bootstrap()

    def _schedule_stream_capture(self) -> None:
        """Schedule BNC stream supervision + the ingest/downsample/gap pipeline.

        Gated behind ``stream_capture.enabled`` (default False): the stream
        subsystem requires BNC deployed on the host and is opt-in per environment.
        """
        sc_cfg = self.yaml_config.get("stream_capture", {})
        if not sc_cfg.get("enabled", False):
            self.logger.debug("Stream capture disabled in config")
            return

        from .stream_scheduler import (
            _run_stream_config_refresh_job,
            _run_stream_pipeline_job,
            _run_stream_supervise_job,
        )

        # Config refresh: (re)generate .bnc + refresh .SKL headers from TOS (daily).
        cfg_trigger = parse_schedule(sc_cfg.get("config_refresh_schedule", "06:00"))
        self.scheduler.add_job(
            func=_run_stream_config_refresh_job,
            trigger=cfg_trigger.trigger_type,
            id="stream_config_refresh",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **cfg_trigger.trigger_kwargs,
        )

        sup_trigger = parse_schedule(sc_cfg.get("supervise_schedule", "10m"))
        self.scheduler.add_job(
            func=_run_stream_supervise_job,
            trigger=sup_trigger.trigger_type,
            id="stream_supervise",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **sup_trigger.trigger_kwargs,
        )

        days_back = sc_cfg.get("days_back", 1)
        pipe_trigger = parse_schedule(sc_cfg.get("pipeline_schedule", ":20"))
        self.scheduler.add_job(
            func=_run_stream_pipeline_job,
            trigger=pipe_trigger.trigger_type,
            args=[days_back],
            id="stream_pipeline",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **pipe_trigger.trigger_kwargs,
        )
        self.logger.info(
            f"Scheduled stream capture (supervise {sup_trigger.description}, "
            f"pipeline {pipe_trigger.description}, {days_back}d back)"
        )

    def _schedule_config_watcher(self) -> None:
        """Schedule periodic config file change detection."""
        # Initialize mtime tracking
        cfg_path = self._get_stations_cfg_path()
        if cfg_path and cfg_path.exists():
            try:
                self._config_mtime = cfg_path.stat().st_mtime
                self.logger.debug(f"Tracking config changes: {cfg_path}")
            except OSError:
                pass

        self.scheduler.add_job(
            func=_check_config_changes_job,
            trigger="interval",
            minutes=1,
            id="config_watcher",
            replace_existing=True,
        )
        self.logger.info("Scheduled config watcher (every 1 min)")

    def _schedule_heartbeat(self) -> None:
        """Schedule the 1-min liveness heartbeat file write.

        The external monitor (``receivers.monitoring.host_disk_check``, run by the
        ``gps-host-monitor`` systemd timer) alerts when the heartbeat goes stale —
        the "scheduler active but wedged" detector for the 2026-07-21 /home-full
        freeze. Runs on the default executor; the write is a sub-millisecond touch.
        """
        self.scheduler.add_job(
            func=_heartbeat_job,
            trigger="interval",
            minutes=1,
            id="liveness_heartbeat",
            replace_existing=True,
        )
        self.logger.info("Scheduled liveness heartbeat (every 1 min)")

        # Mirror dual-write counters — silent unless something actually failed.
        self.scheduler.add_job(
            func=_mirror_metrics_job,
            trigger="interval",
            hours=1,
            id="mirror_metrics_report",
            replace_existing=True,
        )
        self.logger.info("Scheduled mirror dual-write metrics report (hourly)")

    def _schedule_backfill(self) -> None:
        """DEPRECATED: Use _schedule_multi_session_backfill() instead."""
        self._schedule_multi_session_backfill()

    def _schedule_multi_session_backfill(self) -> None:
        """Schedule multi-session backfill inside the :25-:55 window.

        Creates one interval job per session type on the 'backfill' executor.
        Each job is self-gating: it checks the clock on entry and returns
        immediately if outside the configured window.

        Progress is tracked in the backfill_progress table (migrations 016, 017).
        """
        backfill_cfg = self.yaml_config.get("backfill", {})
        if not backfill_cfg.get("enabled", True):
            self.logger.info("Backfill disabled in config")
            return

        from .backfill import _backfill_next_station_for_session

        window_start = backfill_cfg.get("window_start", 25)
        window_end = backfill_cfg.get("window_end", 55)
        archiving_mode = backfill_cfg.get("archiving_mode", "bulk")
        schedule = backfill_cfg.get("schedule", "5m")
        sessions = backfill_cfg.get("sessions", ["status_1hr"])
        strategy = backfill_cfg.get("strategy", "round_robin")
        max_workers = int(backfill_cfg.get("max_workers", 8))

        base_trigger = parse_schedule(schedule)

        for session_type in sessions:
            job_id = f"backfill_{session_type}"
            trigger_type, trigger_kwargs = (
                base_trigger.trigger_type,
                base_trigger.trigger_kwargs.copy(),
            )

            # Pass rinex flag from session config (if session is configured)
            run_rinex = False
            if session_type in self.schedule_configs:
                run_rinex = self.schedule_configs[session_type].rinex

            self.scheduler.add_job(
                func=_backfill_next_station_for_session,
                trigger=trigger_type,
                args=[
                    session_type,
                    window_start,
                    window_end,
                    archiving_mode,
                    run_rinex,
                    strategy,
                    max_workers,
                ],
                id=job_id,
                replace_existing=True,
                max_instances=1,
                executor="backfill",
                **trigger_kwargs,
            )

        self.logger.info(
            f"Scheduled backfill for {len(sessions)} session types "
            f"(window :{window_start:02d}-:{window_end:02d}, {base_trigger.description}, "
            f"{max_workers} workers/tick)"
        )

    def _schedule_gap_detection(self) -> None:
        """Schedule periodic gap detection on the backfill executor.

        Scans archive directories for missing files and logs gap counts.
        """
        gap_cfg = self.yaml_config.get("gap_detection", {})
        if not gap_cfg.get("enabled", True):
            self.logger.info("Gap detection disabled in config")
            return

        from .gap_scheduler import _run_gap_detection_job

        schedule = gap_cfg.get("schedule", "2h")
        sessions = gap_cfg.get("sessions", ["15s_24hr", "1Hz_1hr", "status_1hr"])
        lookback = Lookback.from_config(
            gap_cfg, default_days=7, section_name="gap_detection"
        )

        # RINEX scan uses a longer window — defaults to the reconciler's own
        # count. Read files_back too, so switching that section over does not
        # silently drop this fallback to the hardcoded 30.
        reconciler_cfg = self.yaml_config.get("archive_reconciler", {})
        rinex_days_back = gap_cfg.get(
            "rinex_days_back",
            reconciler_cfg.get("days_back", reconciler_cfg.get("files_back", 30)),
        )

        base_trigger = parse_schedule(schedule)

        self.scheduler.add_job(
            func=_run_gap_detection_job,
            trigger=base_trigger.trigger_type,
            args=[sessions, 7, rinex_days_back],
            kwargs={"lookback": lookback},
            id="gap_detection",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        # Schedule an immediate first run (interval triggers default to
        # start_date=now+interval which delays first execution)
        self.scheduler.add_job(
            func=_run_gap_detection_job,
            trigger="date",
            run_date=datetime.now() + timedelta(seconds=60),
            args=[sessions, 7, rinex_days_back],
            kwargs={"lookback": lookback},
            id="gap_detection_startup",
            replace_existing=True,
            executor="backfill",
        )

        self.logger.info(
            f"Scheduled gap detection ({base_trigger.description}, "
            f"{lookback.describe(sessions)}, RINEX {rinex_days_back} days, "
            f"immediate first run)"
        )

    def _schedule_archive_reconciler(self) -> None:
        """Schedule periodic SBF->RINEX archive reconciliation.

        Scans archive for SBF files missing their RINEX counterpart and
        triggers conversion.  PolaRX5 stations only.
        """
        reconciler_cfg = self.yaml_config.get("archive_reconciler", {})
        if not reconciler_cfg.get("enabled", True):
            self.logger.info("Archive reconciler disabled in config")
            return

        from .archive_reconciler import _run_archive_reconciler_job

        schedule = reconciler_cfg.get("schedule", "6h")
        sessions = reconciler_cfg.get("sessions", ["15s_24hr", "1Hz_1hr"])
        lookback = Lookback.from_config(
            reconciler_cfg, default_days=30, section_name="archive_reconciler"
        )

        base_trigger = parse_schedule(schedule)

        self.scheduler.add_job(
            func=_run_archive_reconciler_job,
            trigger=base_trigger.trigger_type,
            args=[sessions],
            kwargs={"lookback": lookback},
            id="archive_reconciler",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        # Immediate first run
        self.scheduler.add_job(
            func=_run_archive_reconciler_job,
            trigger="date",
            run_date=datetime.now() + timedelta(seconds=120),
            args=[sessions],
            kwargs={"lookback": lookback},
            id="archive_reconciler_startup",
            replace_existing=True,
            executor="backfill",
        )

        self.logger.info(
            f"Scheduled archive reconciler ({base_trigger.description}, "
            f"{lookback.describe(sessions)}, immediate first run)"
        )

    def _schedule_local_prune(self) -> None:
        """Schedule the local ring-buffer prune (see receivers.archive.prune).

        Ages out local gpsdata copies whose long-term archive copy is
        catalog-confirmed; retention + disk guardrails come from the
        [local_prune] section (config, not code — tune as the disk grows).
        Disabled by default; inert until scheduler.yaml enables it.
        """
        prune_cfg = self.yaml_config.get("local_prune", {})
        if not prune_cfg.get("enabled", False):
            self.logger.info("Local prune disabled in config")
            return

        from .local_prune import _run_local_prune_job

        schedule = prune_cfg.get("schedule", "05:10")
        base_trigger = parse_schedule(schedule)
        self.scheduler.add_job(
            func=_run_local_prune_job,
            trigger=base_trigger.trigger_type,
            args=[prune_cfg],
            id="local_prune",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )
        self.logger.info(
            f"Scheduled local prune ({base_trigger.description}, retention="
            f"{prune_cfg.get('retention_days')}, min_free_gb="
            f"{prune_cfg.get('min_free_gb')})"
        )

    def _schedule_receiver_horizon_probe(self) -> None:
        """Schedule the per-receiver horizon probe (unified file index slice-2b.3).

        Lists each receiver's date-directory index to find the oldest file it
        still holds, recording it in ``receiver_horizon`` — the dynamic fetch
        floor for ``missing_on_receiver``. Disabled by default; inert until
        scheduler.yaml enables it (``[receiver_horizon_probe] enabled: true``).
        Daily is ample — the horizon advances ~1 day/day. Runs on the backfill
        executor so it never competes with live downloads.
        """
        probe_cfg = self.yaml_config.get("receiver_horizon_probe", {})
        if not probe_cfg.get("enabled", False):
            self.logger.info("Receiver horizon probe disabled in config")
            return

        from .receiver_horizon_probe import _run_horizon_probe_job

        schedule = probe_cfg.get("schedule", "04:40")
        base_trigger = parse_schedule(schedule)
        self.scheduler.add_job(
            func=_run_horizon_probe_job,
            trigger=base_trigger.trigger_type,
            args=[probe_cfg],
            id="receiver_horizon_probe",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )
        # Delayed first run (5 minutes after start) so a fresh scheduler
        # populates horizons without waiting for the daily slot.
        self.scheduler.add_job(
            func=_run_horizon_probe_job,
            trigger="date",
            run_date=datetime.now() + timedelta(seconds=300),
            args=[probe_cfg],
            id="receiver_horizon_probe_startup",
            replace_existing=True,
            executor="backfill",
        )
        self.logger.info(
            f"Scheduled receiver horizon probe ({base_trigger.description}, "
            "immediate first run in 5 min)"
        )

    def _schedule_long_term_backfill(self) -> None:
        """Schedule the daily long-term-backfill backstop (design #136).

        Disabled by default — inert until scheduler.yaml ``[long_term_backfill]
        enabled: true``. Runs on the 'backfill' executor, max_instances=1, so it
        never competes with live downloads and never overlaps itself.
        """
        cfg = self.yaml_config.get("long_term_backfill", {})
        if not cfg.get("enabled", False):
            self.logger.info("Long-term backfill disabled in config")
            return
        from .long_term_backfill import _run_long_term_backfill_job

        base = parse_schedule(cfg.get("daily_schedule", "04:00"))
        self.scheduler.add_job(
            func=_run_long_term_backfill_job,
            trigger=base.trigger_type,
            kwargs={
                "sessions": cfg.get("sessions", ["15s_24hr", "1Hz_1hr"]),
                "lookback_days": cfg.get("lookback_days", 365),
                "max_workers": cfg.get("max_workers", 2),
                "max_days_per_station": cfg.get("max_days_per_station"),
                "run_rinex": cfg.get("run_rinex", True),
            },
            id="long_term_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            executor="backfill",
            **base.trigger_kwargs,
        )
        self.logger.info(f"Scheduled long-term backfill backstop ({base.description})")

    def _schedule_reconnection_backfill(self) -> None:
        """Schedule the reconnection trigger (#136) — recover stations that just
        came online. Disabled by default; shares the ``[long_term_backfill]``
        config section."""
        cfg = self.yaml_config.get("long_term_backfill", {})
        if not cfg.get("enabled", False):
            return
        from .long_term_backfill import _run_reconnection_backfill_job

        base = parse_schedule(cfg.get("reconnection_schedule", "15m"))
        self.scheduler.add_job(
            func=_run_reconnection_backfill_job,
            trigger=base.trigger_type,
            kwargs={
                "min_outage_days": cfg.get("min_outage_days", 3),
                "lookback_days": cfg.get("lookback_days", 365),
                "run_rinex": cfg.get("run_rinex", True),
                "max_days_per_station": cfg.get("max_days_per_station"),
                "reconnection_window_minutes": cfg.get(
                    "reconnection_window_minutes", 20
                ),
            },
            id="reconnection_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            executor="backfill",
            **base.trigger_kwargs,
        )
        self.logger.info(
            f"Scheduled reconnection backfill trigger ({base.description})"
        )

    def _schedule_integrity_checker(self) -> None:
        """Schedule periodic file integrity checking.

        Scans archive for untracked files, validates gzip integrity,
        checks file sizes against median for anomalies, and optionally
        compares with remote receiver via FTP SIZE / HTTP Content-Length.
        """
        checker_cfg = self.yaml_config.get("integrity_checker", {})
        if not checker_cfg.get("enabled", True):
            self.logger.info("Integrity checker disabled in config")
            return

        from .integrity_checker import _run_integrity_check_job

        schedule = checker_cfg.get("schedule", "6h")
        sessions = checker_cfg.get("sessions", ["15s_24hr", "1Hz_1hr", "status_1hr"])
        lookback = Lookback.from_config(
            checker_cfg, default_days=7, section_name="integrity_checker"
        )
        check_receiver = checker_cfg.get("check_receiver", True)
        size_tolerance_pct = checker_cfg.get("size_tolerance_pct", 50.0)
        hash_fill_limit = checker_cfg.get("hash_fill_limit", 1000)
        # Report-only stray/stacked probe. Default: on, daily sessions only
        # (identity_sessions=None → non-'1hr' sessions; hourly is 24x the files
        # for no extra signal).
        check_identity = checker_cfg.get("check_identity", True)
        identity_sessions = checker_cfg.get("identity_sessions", None)

        base_trigger = parse_schedule(schedule)

        # args positions 5/6 = station_filter (None = all), hash_fill_limit;
        # 7/8 = check_identity, identity_sessions. days_back stays at its
        # historical default here — `lookback` (passed as a kwarg) supersedes it.
        job_args = [
            sessions,
            7,
            check_receiver,
            size_tolerance_pct,
            None,
            hash_fill_limit,
            check_identity,
            identity_sessions,
        ]
        job_kwargs = {"lookback": lookback}

        self.scheduler.add_job(
            func=_run_integrity_check_job,
            trigger=base_trigger.trigger_type,
            args=job_args,
            kwargs=job_kwargs,
            id="integrity_checker",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        # Delayed first run (3 minutes after start)
        self.scheduler.add_job(
            func=_run_integrity_check_job,
            trigger="date",
            run_date=datetime.now() + timedelta(seconds=180),
            args=job_args,
            kwargs=job_kwargs,
            id="integrity_checker_startup",
            replace_existing=True,
            executor="backfill",
        )

        self.logger.info(
            f"Scheduled integrity checker ({base_trigger.description}, "
            f"{lookback.describe(sessions)}, tolerance={size_tolerance_pct}%, "
            f"receiver_check={'on' if check_receiver else 'off'}, immediate first run)"
        )

    def _schedule_archive_sync(self) -> None:
        """Schedule the batch delta push to the long-term archive gateway.

        Pushes raw files newer than each target's watermark to rawdata (-> ananas)
        and forward-indexes them in archive_catalog, then logs freshness.
        Disabled by default — double-gated: this scheduler flag in scheduler.yaml
        AND ``active: true`` on a target in sync.yaml. See design 1781867391.
        """
        cfg = self.yaml_config.get("archive_sync", {})
        if not cfg.get("enabled", False):
            self.logger.info("Archive sync disabled in config")
            return

        from ..archive.job import run_archive_sync_job

        schedule = cfg.get("schedule", ":45")
        max_age_minutes = cfg.get("max_age_minutes", 120)
        base_trigger = parse_schedule(schedule)

        self.scheduler.add_job(
            func=run_archive_sync_job,
            trigger=base_trigger.trigger_type,
            kwargs={"max_age_minutes": max_age_minutes},
            id="archive_sync",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        self.logger.info(f"Scheduled archive sync ({base_trigger.description})")

    def _schedule_epos_disseminate(self) -> None:
        """Schedule the EPOS dissemination sweep (T8).

        Disseminates a trailing window of daily files for every EPOS-eligible
        station to the active dissemination target and indexes each pushed file.
        Double-gated and inert by default: ``epos_disseminate.enabled`` in
        scheduler.yaml AND ``active: true`` on a dissemination target in sync.yaml.
        Runs on the backfill executor, after the archive-sync window.
        """
        cfg = self.yaml_config.get("epos_disseminate", {})
        if not cfg.get("enabled", False):
            self.logger.info("EPOS dissemination disabled in config")
            return

        from ..dissemination.job import run_epos_disseminate_job

        schedule = cfg.get("schedule", ":50")
        days_back = cfg.get("days_back", 3)
        no_qc = cfg.get("no_qc", False)
        base_trigger = parse_schedule(schedule)

        self.scheduler.add_job(
            func=run_epos_disseminate_job,
            trigger=base_trigger.trigger_type,
            kwargs={"days_back": days_back, "no_qc": no_qc},
            id="epos_disseminate",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        self.logger.info(f"Scheduled EPOS dissemination ({base_trigger.description})")

    def _schedule_epos_reactive(self) -> None:
        """Schedule the reactive EPOS sweep (T6).

        A daily TOS-fingerprint diff that re-ETLs / re-disseminates / stops only
        the stations whose TOS metadata or EPOS eligibility changed since the last
        run. Double-gated and inert by default: ``epos_reactive.enabled`` in
        scheduler.yaml AND ``active: true`` on a dissemination target in sync.yaml.
        Runs on the backfill executor (one daily pass).
        """
        cfg = self.yaml_config.get("epos_reactive", {})
        if not cfg.get("enabled", False):
            self.logger.info("EPOS reactive sweep disabled in config")
            return

        from ..dissemination.job import run_epos_reactive_job

        schedule = cfg.get("schedule", "06:30")
        backfill_days = cfg.get("backfill_days", 365)
        no_qc = cfg.get("no_qc", False)
        base_trigger = parse_schedule(schedule)

        self.scheduler.add_job(
            func=run_epos_reactive_job,
            trigger=base_trigger.trigger_type,
            kwargs={"backfill_days": backfill_days, "no_qc": no_qc},
            id="epos_reactive",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        self.logger.info(f"Scheduled EPOS reactive sweep ({base_trigger.description})")

    def _schedule_archive_verify(self) -> None:
        """Schedule periodic archive read-back verification.

        Re-hashes archived files against ``archive_catalog.content_sha256``
        (read-back via the read-only mount) and cross-checks local
        ``file_tracking`` hashes — detects archive bit-rot / partial transfers
        and local↔archive divergence. Disabled by default; enable in
        scheduler.yaml ``archive_verify``. ``read_root`` must point at the
        archive mount (rek-d01: /mnt/rawgpsdata) for read-back; without it only
        the DB-only cross-check runs. ``reverify_after_days`` re-checks already
        verified rows for slow bit-rot.
        """
        cfg = self.yaml_config.get("archive_verify", {})
        if not cfg.get("enabled", False):
            self.logger.info("Archive verify disabled in config")
            return

        from ..archive.job import run_archive_verify_job

        schedule = cfg.get("schedule", "3h")
        base_trigger = parse_schedule(schedule)
        kwargs = {
            "read_root": cfg.get("read_root"),
            "storage_location": cfg.get("storage_location", "imo_archive"),
            "limit": cfg.get("limit", 500),
            "reverify_after_days": cfg.get("reverify_after_days"),
        }

        self.scheduler.add_job(
            func=run_archive_verify_job,
            trigger=base_trigger.trigger_type,
            kwargs=kwargs,
            id="archive_verify",
            replace_existing=True,
            max_instances=1,
            executor="backfill",
            **base_trigger.trigger_kwargs,
        )

        self.logger.info(
            f"Scheduled archive verify ({base_trigger.description}, "
            f"read_back={'on' if kwargs['read_root'] else 'off'})"
        )

    def _schedule_morning_recovery(self) -> None:
        """Schedule the daily morning recovery pass.

        Runs once per day (default 01:30 UTC) on the 'backfill' executor.
        Targets stations whose previous-day 15s_24hr file is still missing
        after the primary 00:01 window and the 00:36 second-chance retry —
        typically PolaRX5 receivers that need ~30-90 min after midnight to
        finish file rotation. Must complete before GAMIT processing starts
        at 03:00-04:00 UTC.

        Disabled by default in package; enabled in operational deployment
        via gps-config-data scheduler.yaml.

        See `docs/design/morning-recovery.md` for the full rationale.
        """
        cfg = self.yaml_config.get("morning_recovery", {})
        if not cfg.get("enabled", False):
            self.logger.info("Morning recovery disabled in config")
            return

        from .morning_recovery import _run_morning_recovery_job

        # `schedule` may be a single string (legacy) or a list of strings
        # (multi-fire). Each entry is independently routed through
        # parse_schedule(), so any supported format works in either form —
        # daily times ("01:30"), intervals ("6h"), or full cron expressions
        # ("cron: 30 1,6 * * *").
        schedule_raw = cfg.get("schedule", "01:30")
        schedules: List[str] = (
            schedule_raw if isinstance(schedule_raw, list) else [schedule_raw]
        )
        sessions = cfg.get("sessions", ["15s_24hr"])
        days_back = cfg.get("days_back", 1)
        max_workers = cfg.get("max_workers", 4)
        station_timeout_minutes = cfg.get("station_timeout_minutes", 8)
        bypass_known_missing = cfg.get("bypass_known_missing", False)
        # Defense-in-depth: a `missing` row younger than this counts as
        # potentially-transient and stays in the retry queue. Older rows
        # are trusted as "verified absent". 0 disables the age guard.
        stale_missing_window_minutes = cfg.get("stale_missing_window_minutes", 120)

        for idx, sched in enumerate(schedules):
            base_trigger = parse_schedule(sched)
            # Keep id stable for single-fire (legacy) so operators can
            # locate the job by name. Multi-fire uses an indexed suffix.
            job_id = (
                "morning_recovery" if len(schedules) == 1 else f"morning_recovery_{idx}"
            )

            self.scheduler.add_job(
                func=_run_morning_recovery_job,
                trigger=base_trigger.trigger_type,
                args=[
                    sessions,
                    days_back,
                    max_workers,
                    station_timeout_minutes,
                    bypass_known_missing,
                    stale_missing_window_minutes,
                ],
                id=job_id,
                replace_existing=True,
                max_instances=1,
                executor="backfill",
                misfire_grace_time=900,  # 15 min grace if scheduler restarted near fire time
                **base_trigger.trigger_kwargs,
            )

            self.logger.info(
                f"🌅 Scheduled morning recovery [{job_id}] "
                f"({base_trigger.description}, sessions={sessions}, "
                f"days_back={days_back}, workers={max_workers}, "
                f"bypass_known_missing={bypass_known_missing}, "
                f"stale_missing_window={stale_missing_window_minutes}m)"
            )

    def _detect_outage_gap(self, session_type: str = "15s_24hr") -> int:
        """Detect how many days of data are missing since the last successful download.

        Uses per-station worst-case gap: finds the station furthest behind
        (oldest MAX(file_date)) and returns that gap in days. This ensures the
        lookback covers ALL tracked stations. Safe because sync=True skips
        already-archived files (no-op for stations that are up to date).

        Returns the gap in days (minimum 1, capped by max_recovery_days).
        Falls back to 1 if DB is unavailable or no tracking data exists.
        """
        try:
            from ..health.database_factory import DatabaseConnectionFactory

            max_days = self.yaml_config.get("recovery", {}).get("max_recovery_days", 30)

            with DatabaseConnectionFactory.connection(single_host=True) as conn:
                with conn.cursor() as cur:
                    # Per-station gap: find the station furthest behind.
                    # Uses 5th-percentile to be robust against single-station
                    # outliers while still covering 95% of tracked stations.
                    cur.execute(
                        """
                        WITH per_station AS (
                            SELECT sid, MAX(file_date) AS last_date
                            FROM file_tracking
                            WHERE session_type = %s
                              AND status IN ('downloaded', 'archived')
                              AND file_hour IS NULL
                            GROUP BY sid
                        )
                        SELECT
                            COUNT(*) AS tracked_stations,
                            CURRENT_DATE - percentile_disc(0.05) WITHIN GROUP
                                (ORDER BY last_date) AS p5_gap_days,
                            CURRENT_DATE - MIN(last_date) AS max_gap_days,
                            CURRENT_DATE - MAX(last_date) AS min_gap_days
                        FROM per_station
                    """,
                        (session_type,),
                    )
                    row = cur.fetchone()

                    if row and row[0] and row[0] > 0:
                        from datetime import date

                        tracked = row[0]
                        p5_gap = row[1] if row[1] is not None else 1
                        max_gap = row[2] if row[2] is not None else 1
                        min_gap = row[3] if row[3] is not None else 1

                        # Use 5th-percentile gap (covers 95% of stations)
                        gap_days = max(1, min(int(p5_gap), max_days))

                        if gap_days > 1:
                            self.logger.warning(
                                f"Outage detected for {session_type}: "
                                f"{tracked} tracked stations, "
                                f"p5 gap={p5_gap}d, worst={max_gap}d, best={min_gap}d "
                                f"— catch-up lookback={gap_days} days"
                            )
                        return gap_days

        except ImportError:
            self.logger.debug("psycopg2 not available — using default lookback")
        except Exception as e:
            self.logger.warning(
                f"Failed to detect outage gap: {e} — using default lookback"
            )

        return 1  # Safe fallback

    def _schedule_daily_catchup(self, session_stations: Dict[str, List[str]]) -> None:
        """Schedule immediate catch-up downloads for daily sessions missed due to late start.

        When the scheduler starts after a daily session's scheduled time (e.g., 15s_24hr
        at 00:01 but scheduler started at 12:00), the cron trigger won't fire until
        tomorrow. This method schedules one-shot downloads distributed across a window
        so yesterday's data is fetched immediately.

        Only applies to daily sessions (cron triggers with a specific hour).
        Hourly sessions naturally catch up on their next hourly tick.
        """
        now = datetime.now(UTC)
        catchup_count = 0

        for session_type, stations in session_stations.items():
            config = self.schedule_configs[session_type]
            if not config.enabled or not stations:
                continue

            base_trigger = parse_schedule(config.schedule)

            # Only catch up daily sessions — those with a specific hour in the cron trigger
            if base_trigger.trigger_type != "cron":
                continue
            trigger_hour = base_trigger.trigger_kwargs.get("hour")
            if trigger_hour is None:
                continue  # Hourly session — no catch-up needed

            scheduled_hour = int(trigger_hour)
            scheduled_minute = int(base_trigger.trigger_kwargs.get("minute", 0))

            # Check if the scheduled time already passed today
            local_now = datetime.now()
            today_target = local_now.replace(
                hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0
            )
            if local_now <= today_target:
                continue  # Not missed — will fire at the scheduled time

            # Detect actual outage gap for dynamic lookback
            recovery_cfg = self.yaml_config.get("recovery", {})
            if recovery_cfg.get("auto_recovery_enabled", True):
                lookback = self._detect_outage_gap(session_type)
            else:
                lookback = config.lookback_periods

            # Daily session missed — schedule catch-up with distribution window
            window = config.distribution_window
            for i, station_id in enumerate(stations):
                if len(stations) > 1 and window > 0:
                    offset_seconds = int((i / len(stations)) * window * 60)
                else:
                    offset_seconds = 0

                run_time = now + timedelta(seconds=30 + offset_seconds)
                job_id = f"catchup_{session_type}_{station_id}"
                self.scheduler.add_job(
                    func=_download_station_data_job,
                    trigger="date",
                    run_date=run_time,
                    args=[
                        station_id,
                        session_type,
                        self.production_mode,
                        lookback,
                        config.timeout_minutes,
                        config.rinex,
                    ],
                    id=job_id,
                    replace_existing=True,
                )
                catchup_count += 1

            self.logger.info(
                f"Catch-up: {len(stations)} {session_type} downloads "
                f"(lookback={lookback} days, scheduled time {scheduled_hour:02d}:{scheduled_minute:02d} "
                f"already passed, distributing over {window}min)"
            )

        if catchup_count:
            self.logger.info(
                f"Total catch-up: {catchup_count} one-shot download jobs scheduled"
            )

    def _schedule_bootstrap(self) -> None:
        """Detect cold start and schedule aggressive initial downloads if needed.

        Delegates to the bootstrap module. Skipped when:
        - bootstrap is disabled in config
        - file_tracking already has data (not a cold start)
        """
        bootstrap_cfg = self.yaml_config.get("bootstrap", {})
        if not bootstrap_cfg.get("enabled", True):
            self.logger.info("Bootstrap disabled in config")
            return

        try:
            from .bootstrap import detect_cold_start, schedule_bootstrap

            bootstrap_sessions = bootstrap_cfg.get("sessions")
            if not detect_cold_start(sessions=bootstrap_sessions):
                self.logger.debug("Not a cold start — skipping bootstrap")
                return

            jobs = schedule_bootstrap(
                scheduler=self.scheduler,
                stations=self.stations,
                session_configs=self.schedule_configs,
                bootstrap_cfg=bootstrap_cfg,
                production_mode=self.production_mode,
                station_filter=self.station_filter,
            )
            if jobs > 0:
                self.logger.info(f"Bootstrap: {jobs} one-shot download jobs scheduled")

        except ImportError as e:
            self.logger.debug(f"Bootstrap module not available: {e}")
        except Exception as e:
            self.logger.warning(f"Bootstrap scheduling failed: {e}")

    def _get_stations_for_session(self, session_type: str) -> List[str]:
        """Get list of stations that support a specific session type."""

        stations = []
        skipped = []

        for station_id, config in self.stations.items():
            if not config.get("enabled", True):
                continue

            # Skip non-active stations (lifecycle or monitoring mode)
            if config.get("station_status") in ("discontinued", "inactive"):
                continue
            if config.get("health_check") == "passive":
                continue
            # Stream-capture stations: session-aware download. The stream pipeline
            # (BNC) provides sessions the receiver does NOT log to disk (e.g. 1Hz);
            # the sessions it DOES log (remote_sessions, e.g. GONH's 15s_24hr) are
            # still downloaded here because the SBF-derived RINEX is the
            # authoritative archival product and supersedes the stream's real-time
            # downsample. A stream station with no remote_sessions (pure low-bandwidth
            # stream) downloads nothing.
            if str(config.get("acquisition_mode", "")).strip().lower() == "stream":
                if session_type not in config.get("remote_sessions", []):
                    continue

            # Apply station filter if specified
            if self.station_filter and station_id not in self.station_filter:
                continue

            # Check if receiver type supports this session
            receiver_type = config.get("receiver_type", "").lower()
            if self.receiver_sessions and receiver_type in self.receiver_sessions:
                supported_sessions = self.receiver_sessions[receiver_type]
                if session_type not in supported_sessions:
                    skipped.append(f"{station_id}({receiver_type})")
                    continue

            stations.append(station_id)

        # Log skipped stations
        if skipped:
            self.logger.info(
                f"Skipped {len(skipped)} stations for {session_type} (unsupported by receiver type): {', '.join(skipped[:5])}"
            )

        # Apply max stations limit if specified
        if (
            self.max_stations_per_session
            and len(stations) > self.max_stations_per_session
        ):
            stations = stations[: self.max_stations_per_session]
            self.logger.info(
                f"Limited {session_type} to {self.max_stations_per_session} stations for testing"
            )

        return stations

    def _schedule_session_downloads(
        self, session_type: str, config: ScheduleConfig, stations: List[str]
    ):
        """Schedule downloads for a specific session type using flexible schedule format."""

        # Parse the schedule configuration
        base_trigger = parse_schedule(config.schedule)

        for i, station_id in enumerate(stations):
            # Apply distribution window to spread stations across time
            trigger_type, trigger_kwargs = apply_distribution_window(
                base_trigger, i, len(stations), config.distribution_window
            )

            # Create job ID
            job_id = f"{session_type}_{station_id}"

            # Schedule the job with parsed trigger (uses job_defaults max_instances=70)
            self.scheduler.add_job(
                func=_download_station_data_job,
                trigger=trigger_type,
                args=[
                    station_id,
                    session_type,
                    self.production_mode,
                    config.lookback_periods,
                    config.timeout_minutes,
                    config.rinex,
                ],
                id=job_id,
                replace_existing=True,
                **trigger_kwargs,
            )

        self.logger.info(
            f"Scheduled {len(stations)} stations for {session_type} "
            f"({base_trigger.description})"
        )

    def _schedule_health_monitoring(self):
        """Schedule health monitoring jobs (send to Icinga + PostgreSQL).

        Health checks run every 5 minutes for all stations that support live health.
        Equivalent to: receivers health STATION --icinga --save-db
        """
        # Check scheduler_types filter
        if not self.scheduler_types.get("health", True):
            self.logger.info("Health monitoring skipped (--only filter)")
            return

        # Check if health monitoring is enabled in config
        status_monitoring = self.yaml_config.get("status_monitoring", {})
        if not status_monitoring.get("enabled", True):
            self.logger.info("Health monitoring disabled in config")
            return

        # Get schedule (default: every 5 minutes)
        schedule = status_monitoring.get("schedule", "5m")

        # Get stations that support health checks (all receiver types with get_health_status)
        # Supported: PolaRX5, mosaic-X5, NetR9, NetRS, NetR5, G10
        supported_health_types = {
            "polarx5",
            "mosaic-x5",
            "netr9",
            "netrs",
            "netr5",
            "g10",
        }
        health_stations = []
        skipped_stations = []
        for station_id, config in self.stations.items():
            if not config.get("enabled", True):
                continue

            # Skip non-active stations (lifecycle or monitoring mode)
            station_status = config.get("station_status")
            health_check = config.get("health_check")
            if station_status in ("discontinued", "inactive"):
                skipped_stations.append(station_id)
                continue
            if health_check == "passive":
                skipped_stations.append(station_id)
                continue

            # Apply station filter if specified
            if self.station_filter and station_id not in self.station_filter:
                continue

            # Check if receiver type supports health checks
            receiver_type = config.get("receiver_type", "").lower()
            if receiver_type not in supported_health_types:
                continue

            health_stations.append(station_id)

        if skipped_stations:
            self.logger.info(
                f"Skipping {len(skipped_stations)} discontinued/passive stations: "
                f"{', '.join(sorted(skipped_stations))}"
            )

        if not health_stations:
            self.logger.info("No stations support health monitoring")
            return

        # Apply max stations limit
        if (
            self.max_stations_per_session
            and len(health_stations) > self.max_stations_per_session
        ):
            health_stations = health_stations[: self.max_stations_per_session]

        # Parse schedule
        base_trigger = parse_schedule(schedule)

        # Distribution window for health checks (default 3 minutes)
        distribution_window = status_monitoring.get("distribution_window", 3)

        # Schedule health check jobs
        for i, station_id in enumerate(sorted(health_stations)):
            trigger_type, trigger_kwargs = apply_distribution_window(
                base_trigger, i, len(health_stations), distribution_window
            )

            job_id = f"health_{station_id}"
            self.scheduler.add_job(
                func=_status_check_job,
                trigger=trigger_type,
                args=[station_id, True, True],  # send_to_db=True, send_to_icinga=True
                id=job_id,
                replace_existing=True,
                executor="health",
                **trigger_kwargs,
            )

        self.logger.info(
            f"Scheduled {len(health_stations)} stations for health monitoring "
            f"({base_trigger.description})"
        )

    def _download_station_data(self, station_id: str, session_type: str):
        """Download data for a single station (wrapper for backward compatibility).

        This method wraps the module-level function for backward compatibility.
        Direct scheduling uses the module-level function to avoid serialization issues.
        """
        job_id = f"{session_type}_{station_id}"
        start_time = datetime.now(UTC)

        try:
            self.running_jobs[job_id] = start_time
            _download_station_data_job(station_id, session_type, self.production_mode)
        finally:
            # Clean up
            if job_id in self.running_jobs:
                del self.running_jobs[job_id]

    def _job_executed(self, event):
        """Handle successful job execution."""
        self.logger.debug(f"Job executed: {event.job_id}")

    def _job_error(self, event):
        """Handle job execution errors."""
        self.logger.error(f"Job error: {event.job_id} - {event.exception}")

    def _acquire_lock(self) -> None:
        """Acquire an exclusive file lock to prevent duplicate scheduler instances.

        Raises:
            RuntimeError: If another scheduler instance is already running.
        """
        existing_pid = ""
        try:
            # Open for reading+writing so we can read existing PID before overwriting
            self._lock_fd = open(self._lock_path, "a+")
            self._lock_fd.seek(0)
            existing_pid = self._lock_fd.read().strip()
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._lock_fd:
                self._lock_fd.close()
            self._lock_fd = None
            raise RuntimeError(
                f"Another scheduler instance is already running (PID {existing_pid}). "
                f"Lock file: {self._lock_path}"
            )
        # Write our PID for diagnostics
        self._lock_fd.seek(0)
        self._lock_fd.truncate()
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()

    def _release_lock(self) -> None:
        """Release the file lock and clean up."""
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except OSError:
                pass
            self._lock_fd = None
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _log_misfire_status(self) -> None:
        """Log whether daily download jobs were misfired or recovered at startup."""

        now = datetime.now(UTC)
        for job in self.scheduler.get_jobs():
            if not job.id.endswith("_batch_summary") and "_00_" not in job.id:
                continue
            # Only inspect the first station slot of each daily session
            # (job IDs for midnight 15s_24hr look like "15s_24hr_ELDC_0001")
            next_run = getattr(job, "next_run_time", None)
            if next_run is None:
                continue
            # If the next run is more than 23 hours away the current trigger
            # fired today — check whether the previous window was misfired
            delta_h = (next_run - now).total_seconds() / 3600
            if delta_h > 23:
                # Already ran today (next_run is tomorrow)
                pass
            # We focus on the *session-level* picture; inspect named summary jobs
        # Check each registered batch-summary job to surface misfire state
        seen: set[str] = set()
        for job in self.scheduler.get_jobs():
            if not job.id.endswith("_batch_summary"):
                continue
            session_type = job.args[0] if job.args else job.id
            if session_type in seen:
                continue
            seen.add(session_type)
            next_run = getattr(job, "next_run_time", None)
            if next_run is None:
                self.logger.warning(
                    f"⚠️  {session_type} batch-summary job has no next_run_time — "
                    "may have been paused or misfired"
                )
                continue
            # Find the matching download trigger to determine last scheduled window
            config = self.schedule_configs.get(session_type)
            if config is None:
                continue
            base_trigger = parse_schedule(config.schedule)
            tkw = base_trigger.trigger_kwargs
            if base_trigger.trigger_type != "cron" or "hour" not in tkw:
                continue

            now = datetime.now(UTC)
            sched_hour = int(str(tkw["hour"]).split(",")[0])
            sched_minute = int(tkw["minute"])
            window_end = now.replace(
                hour=sched_hour,
                minute=(sched_minute + config.distribution_window) % 60,
                second=0,
                microsecond=0,
            )
            if window_end > now:
                # Window hasn't happened today yet — nothing to check
                continue
            # Window has passed — was there a download run?
            minutes_since_window = (now - window_end).total_seconds() / 60
            if minutes_since_window < 600 / 60:
                self.logger.info(
                    f"✅ {session_type} midnight window recently closed "
                    f"({minutes_since_window:.0f}m ago) — misfire check not needed"
                )
            else:
                self.logger.warning(
                    f"⚠️  {session_type} midnight window was {minutes_since_window:.0f}m ago "
                    f"(00:{sched_minute:02d}–{sched_hour:02d}:{(sched_minute + config.distribution_window) % 60:02d} UTC). "
                    "If scheduler was down then, files may be missing. "
                    "Run: receivers scheduler backfill --session "
                    f"{session_type} --days 1"
                )

    def start(self):
        """Start the scheduler.

        Acquires an exclusive lock to prevent duplicate instances.
        """
        self._acquire_lock()
        try:
            self.scheduler.start()
            self.logger.info(f"Scheduler started successfully (PID {os.getpid()})")
            self._log_misfire_status()
        except Exception as e:
            self._release_lock()
            self.logger.error(f"Failed to start scheduler: {e}")
            raise

    def stop(self):
        """Stop the scheduler and release the lock."""
        try:
            self.scheduler.shutdown(wait=True)
            self.logger.info("Scheduler stopped")
        except Exception as e:
            self.logger.error(f"Error stopping scheduler: {e}")
        finally:
            self._release_lock()

    def get_scheduled_jobs(self) -> List[Dict[str, Any]]:
        """Get list of all scheduled jobs."""
        jobs = []

        for job in self.scheduler.get_jobs():
            # Handle different APScheduler versions
            next_run = getattr(job, "next_run_time", None)
            if next_run is None:
                next_run = getattr(job, "next_run", None)

            jobs.append(
                {
                    "id": job.id,
                    "name": getattr(job, "name", job.id),
                    "trigger": str(job.trigger),
                    "next_run": next_run.isoformat() if next_run else None,
                    "args": getattr(job, "args", []),
                }
            )

        return jobs

    def get_job_status(self) -> Dict[str, Any]:
        """Get scheduler and job status."""
        return {
            "scheduler_running": self.scheduler.running,
            "total_jobs": len(self.scheduler.get_jobs()),
            "running_jobs": len(self.running_jobs),
            "current_jobs": list(self.running_jobs.keys()),
        }


def create_scheduler_config() -> Path:
    """DEPRECATED: Use config_loader.create_default_config_file() instead.

    This function created JSON config at ~/.config/gps_receivers/scheduler.json.
    The new function creates YAML config at ~/.config/gpsconfig/scheduler.yaml.
    """
    import warnings

    warnings.warn(
        "create_scheduler_config() is deprecated. "
        "Use receivers.scheduling.config_loader.create_default_config_file() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from .config_loader import create_default_config_file

    return create_default_config_file()


# Example usage and testing
if __name__ == "__main__":
    if not HAS_APSCHEDULER:
        print("APScheduler not available. Install with: pip install apscheduler")
        sys.exit(1)

    # Create scheduler
    scheduler = BulkDownloadScheduler(production_mode=True)

    # Schedule all sessions
    scheduler.schedule_all_sessions()

    # Show scheduled jobs
    jobs = scheduler.get_scheduled_jobs()
    print(f"Scheduled {len(jobs)} jobs:")
    for job in jobs[:5]:  # Show first 5
        print(f"  {job['id']}: {job['trigger']}")

    print(f"\nScheduler status: {scheduler.get_job_status()}")

    # Note: In production, you would call scheduler.start() and keep the process running
