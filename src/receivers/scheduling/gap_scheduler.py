"""Periodic gap detection for scheduled downloads.

Scans archive directories for missing files across all configured session
types.  Runs on the 'backfill' executor at a configurable interval (default
every 2 hours).

Reuses the GapDetector from health.file_tracker — this module is just the
APScheduler-compatible entry point.
"""

import logging
from typing import List, Optional

from .lookback import Lookback

logger = logging.getLogger("receivers.scheduler.gaps")


def _run_gap_detection_job(
    session_types: List[str],
    days_back: int = 7,
    rinex_days_back: int = 30,
    lookback: Optional[Lookback] = None,
) -> None:
    """APScheduler job: scan for gaps in archived files.

    Iterates active stations, calls GapDetector.get_gap_summary() for each
    session type, and logs the results.  max_instances=1 prevents overlap.

    Args:
        session_types: List of session types to scan (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday
        rinex_days_back: Days to look back for RINEX scan (longer than gap detection)
    """
    try:
        from ..cli.main import get_all_station_configs
        from ..health.file_tracker import GapDetector
    except ImportError as e:
        logger.debug(f"Gap detection dependencies not available: {e}")
        return

    try:
        # Get active station IDs
        all_stations = get_all_station_configs()
        station_ids = [
            sid
            for sid, cfg in all_stations.items()
            if cfg.get("enabled", True)
            and cfg.get("station_status") not in ("discontinued", "inactive")
            and cfg.get("health_check") != "passive"
        ]

        if not station_ids:
            logger.info("Gap detection: no active stations")
            return

        # Build receiver_types dict so archive checks use correct file extensions
        # (e.g., .T02 for NetR9, .T00 for NetRS, .m00 for G10 instead of default .sbf.gz)
        receiver_types = {
            sid: cfg.get("receiver_type", "")
            for sid, cfg in all_stations.items()
            if sid in station_ids and cfg.get("receiver_type")
        }

        lb = lookback or Lookback(days_back, "days")
        logger.info(
            f"Gap detection: scanning {len(station_ids)} stations, "
            f"{len(session_types)} sessions, {lb.describe(session_types)}"
        )

        with GapDetector() as detector:
            for session_type in session_types:
                # max_files is what makes files_back exact. Under days_back it
                # equals the full enumerated range, so passing it is a no-op
                # there and the two units share one call path.
                summary = detector.get_gap_summary(
                    station_ids,
                    session_type,
                    days_back=lb.date_span_days(session_type),
                    receiver_types=receiver_types,
                    max_files=lb.file_count(session_type),
                )

                total_gaps = summary.get("total_gaps", 0)
                total_expected = summary.get("total_expected", 0)
                total_archived = summary.get("total_archived", 0)

                if total_gaps > 0:
                    stations_with_gaps = sorted(
                        [
                            sid
                            for sid, info in summary.get("stations", {}).items()
                            if info.get("gaps", 0) > 0
                        ]
                    )
                    _MAX_LISTED = 10
                    if len(stations_with_gaps) > _MAX_LISTED:
                        gap_str = (
                            " ".join(stations_with_gaps[:_MAX_LISTED])
                            + f" [+{len(stations_with_gaps) - _MAX_LISTED} more]"
                        )
                    else:
                        gap_str = " ".join(stations_with_gaps)
                    logger.info(
                        f"Gap detection {session_type}: "
                        f"{total_gaps} gaps / {total_expected} expected "
                        f"({total_archived} archived). "
                        f"Missing: {gap_str}"
                    )
                    # Self-refill: queue the gapped stations so the backfill
                    # worker actually fills them. Without this, gap_detection
                    # only reports and the backfill_progress queue drains once
                    # and never refills.
                    from .backfill import _enqueue_backfill

                    # The queued backfill row must cover the SAME window the
                    # gaps were found in, or the worker re-widens what this run
                    # just narrowed.
                    _enqueue_backfill(
                        session_type,
                        stations_with_gaps,
                        lb.date_span_days(session_type),
                    )
                else:
                    logger.info(
                        f"Gap detection {session_type}: "
                        f"no gaps ({total_archived}/{total_expected} archived)"
                    )

            # RINEX freshness scan — all stations with RINEX converters
            from datetime import date, timedelta

            from ..config.receiver_registry import has_rinex_converter

            convertible_ids = [
                sid
                for sid, cfg in all_stations.items()
                if sid in station_ids
                and has_rinex_converter(cfg.get("receiver_type", ""))
            ]

            if convertible_ids:
                end_date = date.today() - timedelta(days=1)
                start_date = end_date - timedelta(days=rinex_days_back)
                total_found = 0
                total_added = 0

                for rinex_type in ("15s_24hr_rinex", "1Hz_1hr_rinex"):
                    for sid in convertible_ids:
                        found, added = detector.scan_rinex_files(
                            sid,
                            rinex_type,
                            start_date,
                            end_date,
                        )
                        total_found += found
                        total_added += added

                logger.info(
                    f"RINEX scan ({rinex_days_back}d): {len(convertible_ids)} stations, "
                    f"{total_found} files found, {total_added} upserted"
                )

    except Exception as e:
        logger.error(f"Gap detection failed: {type(e).__name__}: {e}")
