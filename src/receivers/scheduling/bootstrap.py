"""Bootstrap / cold-start mode for the GPS scheduler.

When the scheduler starts with an empty or near-empty archive (cold start),
this module detects the situation and schedules aggressive initial downloads
to catch up quickly before transitioning to normal cron scheduling.

Bootstrap waves:
  Wave 1 (t+0  to t+Wmin):  15s_24hr downloads for all stations
  Wave 2 (t+W  to t+2W min): 1Hz_1hr downloads for all stations
  Wave 3 (t+2W to t+3W min): status_1hr downloads for all stations
  Wave 4 (t+3W+):            health checks for all stations

After initial catch-up (``initial_lookback_days``), extended backfill
continues via the normal backfill scheduler for ``full_lookback_days``.
"""

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Threshold: fewer than this many entries in file_tracking → cold start
COLD_START_THRESHOLD = 10


def detect_cold_start(sessions: Optional[List[str]] = None) -> bool:
    """Check if this is a cold start (no or very little prior data).

    When ``sessions`` is provided, only counts file_tracking entries for those
    session types.  This enables session-specific bootstrap — e.g. triggering a
    bootstrap for 15s_24hr when those rows have been cleared, even though
    1Hz_1hr and status_1hr data still exists.

    Returns True if the (filtered) count is below COLD_START_THRESHOLD.
    Also returns True if the database is unreachable (treat as cold start).
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                if sessions:
                    # Include both raw and rinex variants
                    all_types = list(sessions)
                    for s in sessions:
                        all_types.append(f"{s}_rinex")
                    ph = ", ".join(["%s"] * len(all_types))
                    cur.execute(
                        f"SELECT COUNT(*) FROM file_tracking "
                        f"WHERE status IN ('downloaded', 'archived') "
                        f"AND session_type IN ({ph})",
                        all_types,
                    )
                    label = f"session(s) {', '.join(sessions)}"
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM file_tracking WHERE status IN ('downloaded', 'archived')"
                    )
                    label = "total"

                count = cur.fetchone()[0]
                is_cold = count < COLD_START_THRESHOLD
                if is_cold:
                    logger.info(
                        f"Cold start detected: file_tracking ({label}) has {count} entries "
                        f"(threshold: {COLD_START_THRESHOLD})"
                    )
                else:
                    logger.debug(
                        f"Not a cold start: file_tracking ({label}) has {count} entries"
                    )
                return is_cold

    except ImportError:
        logger.debug("psycopg2 not available — assuming cold start")
        return True
    except Exception as e:
        logger.warning(
            f"Cannot check file_tracking for cold start: {e} — assuming cold start"
        )
        return True


def schedule_bootstrap(
    scheduler: Any,
    stations: Dict[str, Dict[str, Any]],
    session_configs: Dict[str, Any],
    bootstrap_cfg: Dict[str, Any],
    production_mode: bool = True,
    station_filter: Optional[List[str]] = None,
) -> int:
    """Schedule aggressive initial downloads for cold-start bootstrap.

    Args:
        scheduler: APScheduler BackgroundScheduler instance
        stations: Dict of station_id → station config
        session_configs: Dict of session_type → ScheduleConfig
        bootstrap_cfg: Bootstrap configuration from scheduler.yaml
        production_mode: Whether to use production logging
        station_filter: Optional list of stations to limit bootstrap to

    Returns:
        Number of bootstrap jobs created
    """
    from .bulk_scheduler import _download_station_data_job

    distribution_window = bootstrap_cfg.get("distribution_window", 10)
    initial_lookback = bootstrap_cfg.get("initial_lookback_days", 3)

    # Determine eligible stations (active, not passive, not discontinued)
    eligible = []
    for station_id, config in sorted(stations.items()):
        if config.get("station_status") in ("discontinued", "inactive"):
            continue
        if config.get("health_check") == "passive":
            continue
        if station_filter and station_id not in station_filter:
            continue
        eligible.append(station_id)

    if not eligible:
        logger.warning("Bootstrap: no eligible stations found")
        return 0

    now = datetime.now(UTC)
    total_jobs = 0
    wave_offset = 0

    # Session waves — use config list or all enabled sessions
    configured_sessions = bootstrap_cfg.get("sessions")
    session_order = configured_sessions or ["15s_24hr", "1Hz_1hr", "status_1hr"]

    for session_type in session_order:
        if session_type not in session_configs:
            continue

        config = session_configs[session_type]
        if not config.enabled:
            continue

        rinex = config.rinex
        timeout = config.timeout_minutes

        # Distribute stations across the wave's distribution window
        wave_start = now + timedelta(minutes=wave_offset)

        for i, station_id in enumerate(eligible):
            if len(eligible) > 1 and distribution_window > 0:
                offset_seconds = int((i / len(eligible)) * distribution_window * 60)
            else:
                offset_seconds = 0

            run_time = wave_start + timedelta(seconds=offset_seconds)
            job_id = f"bootstrap_{session_type}_{station_id}"

            scheduler.add_job(
                func=_download_station_data_job,
                trigger="date",
                run_date=run_time,
                args=[
                    station_id,
                    session_type,
                    production_mode,
                    initial_lookback,
                    timeout,
                    rinex,
                ],
                id=job_id,
                replace_existing=True,
            )
            total_jobs += 1

        logger.info(
            f"Bootstrap wave: {len(eligible)} {session_type} jobs "
            f"(lookback={initial_lookback}d, window={distribution_window}m, "
            f"starts at +{wave_offset}m)"
        )
        wave_offset += distribution_window

    logger.info(
        f"Bootstrap complete: {total_jobs} one-shot jobs across "
        f"{len(session_order)} session waves"
    )
    return total_jobs
