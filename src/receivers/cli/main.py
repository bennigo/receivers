#!/usr/bin/env python3
"""
receivers CLI - GPS Receiver Data Management Tool

Enhanced command-line interface for downloading and managing GPS receiver data.
Migrated from getSeptentrio3 with modern subcommand architecture.

Usage:
    receivers download STATION [STATION...] [OPTIONS]
    receivers status STATION
    receivers health STATION
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

from ..base.exceptions import ConfigurationError, ConnectionError
from ..base.receiver_factory import create_receiver, get_receiver_factory
from ..base.type_validator import ReceiverTypeValidator
from ..utils.time_utils import (
    calculate_download_time_range,
    generate_period_ranges,
    resolve_time_range,
)

# Import gps_parser for centralized config
try:
    import sys

    sys.path.append("../gps_parser/src")
    import gps_parser

    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None

# Import station config from utility to avoid circular imports
from ..config_utils import (
    get_all_station_configs,
    get_station_config,
    resolve_receiver_endpoint,
)

# Module-level logger for functions that don't receive a logger argument
_logger = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up logging for CLI commands.

    Delegates to the unified :func:`receivers.logging_config.setup_logging`.
    """
    from ..logging_config import setup_logging as _setup

    return _setup(level=level)


def parse_datetime(date_str: str) -> datetime:
    """Parse datetime string in format YYYYMMDD-HHMM or YYYYMMDD."""
    if "-" in date_str:
        return datetime.strptime(date_str, "%Y%m%d-%H%M")
    else:
        return datetime.strptime(date_str, "%Y%m%d")


# get_station_config function moved to config_utils.py to avoid circular imports


def _validate_station_for_download(
    station_id: str, logger: logging.Logger, session: str = None
) -> Optional[Any]:
    """Validate station config and return (receiver, station_config) or None on failure.

    Args:
        station_id: Station identifier
        logger: Logger instance
        session: Session type to validate (e.g., '15s_24hr', 'status_1hr')

    Returns:
        Receiver instance if valid, None if station should be skipped
    """
    station_config = get_station_config(station_id)
    if station_config is None:
        logger.warning(f"⚠️  Station {station_id} not found in configuration - SKIPPING")
        return None

    try:
        ip = station_config["router"]["ip"]
        # Accept either FTP or HTTP port — Trimble NetR9/NetR5 use HTTP downloads
        port = station_config["receiver"].get("ftpport") or station_config[
            "receiver"
        ].get("httpport")
        if not ip or not port:
            logger.warning(
                f"⚠️  Station {station_id} missing IP ({ip}) or port ({port}) - SKIPPING"
            )
            return None
    except KeyError as e:
        logger.warning(
            f"⚠️  Station {station_id} configuration missing required key {e} - SKIPPING"
        )
        return None

    # Check if session is supported by this receiver type
    if session:
        from ..config.receivers_config import get_receivers_config

        receivers_config = get_receivers_config()
        receiver_type = station_config.get("receiver_type", "").lower()
        # Fail-open, matching the scheduler's capability gate
        # (bulk_scheduler._get_stations_for_session): only enforce when the
        # receiver type declares a session_map in receivers.cfg. Types without
        # a section (e.g. mosaic-X5, which reuses the PolaRX5 session_map at the
        # receiver level and declares its real sessions per-station via
        # ``remote_sessions``) are passed through rather than rejected.
        supported = receivers_config.get_supported_sessions(receiver_type)
        # Case-insensitive: session names originate from configparser keys, which
        # are lower-cased on read. get_supported_sessions() restores the canonical
        # spelling for registry-known sessions, but a cfg-only session type would
        # still come back lower-cased — don't reject a valid station over casing.
        if supported and session.lower() not in {s.lower() for s in supported}:
            logger.info(
                f"⏭️  Skipping {station_id}: {session} not supported for {receiver_type} "
                f"(supported: {', '.join(supported)})"
            )
            return None

    receiver = create_receiver(station_id, station_config)
    return receiver


def _download_station_period(
    receiver,
    station_id: str,
    start: datetime,
    end: datetime,
    args,
    logger: logging.Logger,
    audit_logger=None,
    ffrequency: str = "",
    afrequency: str = "",
    reverse_chronological: bool = False,
) -> tuple:
    """Download a single period for one station.

    Returns:
        Tuple of (files_downloaded, errors, files_checked).
    """
    files_downloaded = 0
    errors = 0
    files_checked = 0

    try:
        # Test connection if requested
        if args.test_connection:
            status = receiver.get_connection_status()
            if not status.get("receiver"):
                logger.error(
                    f"Connection test failed for {station_id}: {status.get('error')}"
                )
                return 0, 1, 0
            logger.info(f"Connection test successful for {station_id}")

        # Download data. Default to retry_missing=True for operator-issued
        # downloads — "known missing" markers may have been written by a
        # transient failure and should not block an explicit retry. Use
        # --respect-known-missing to opt back into the historical behavior
        # (e.g. for fleet-wide scripts that want to skip known-bad stations).
        retry_missing = not getattr(args, "respect_known_missing", False)
        result = receiver.download_data(
            start=start,
            end=end,
            session=args.session,
            ffrequency=ffrequency,
            afrequency=afrequency,
            compression=args.compression,
            sync=args.sync,
            clean_tmp=args.clean_tmp,
            archive=args.archive,
            reverse_chronological=reverse_chronological,
            loglevel=args.loglevel,
            max_retries=getattr(args, "max_retries", 3),
            retry_missing=retry_missing,
        )

        # Report results
        files_downloaded = result.get("files_downloaded", 0)
        files_checked = result.get("files_checked", 0)

        # Propagate failure status from download_data() as an error.
        # download_data() catches its own exceptions (timeouts, stalls,
        # FTP errors) and returns status="failed" without re-raising.
        # Without this check, _download_one_station sees (0, 0) and
        # misclassifies the failure as "up_to_date".
        result_status = result.get("status", "unknown")
        if (
            result_status in ("failed", "configuration_error", "unreachable")
            and files_downloaded == 0
        ):
            errors += 1
        # Guard: "up_to_date" with 0 files checked means no timestamps were
        # generated (empty file_date_dict) — treat as error so the parallel
        # orchestrator doesn't misclassify it as genuinely up-to-date.
        elif result_status == "up_to_date" and result.get("files_checked", 1) == 0:
            errors += 1

        # Log to audit trail if production logging enabled
        if audit_logger:
            audit_logger.log_download_session(
                station_id,
                {
                    "session": args.session,
                    "status": result.get("status", "unknown"),
                    "duration": result.get("duration", 0),
                    "files_downloaded": files_downloaded,
                    "bytes_downloaded": result.get("total_bytes", 0),
                    "errors": result.get("errors", 0),
                    "start_time": start.isoformat() if start else None,
                    "end_time": end.isoformat() if end else None,
                    "connection_time": getattr(receiver, "_last_connection_time", None),
                },
            )

        logger.debug(f"Station {station_id}: {files_downloaded} files downloaded")
        logger.debug(
            f"Status: {result.get('status')}, Duration: {result.get('duration', 0):.2f}s"
        )

        if files_downloaded > 0:
            logger.debug("Downloaded files:")
            for file_path in result.get("downloaded_files", []):
                logger.debug(f"  - {file_path}")

    except (ConfigurationError, ConnectionError) as e:
        logger.error(f"Error processing {station_id}: {e}")
        errors += 1
    except Exception as e:
        import traceback

        logger.error(f"Unexpected error processing {station_id}: {e}")
        logger.debug(f"Traceback:\n{traceback.format_exc()}")
        errors += 1

    return files_downloaded, errors, files_checked


def _setup_rinex_callbacks(receivers_map: Dict[str, Any], session: str) -> None:
    """Set per-file RINEX callbacks on receivers that support it.

    Each receiver gets an ``_on_file_archived`` callback that submits the
    archived file to the RINEX process pool for fire-and-forget conversion.
    """
    try:
        from ..rinex.async_converter import submit_file_rinex

        for sid, receiver in receivers_map.items():

            def _on_archived(archive_path: str, _sid=sid, _session=session) -> None:
                submit_file_rinex(_sid, _session, archive_path)

            receiver._on_file_archived = _on_archived
    except Exception:
        pass


def cmd_download(args) -> int:
    """Download command - main data download functionality."""

    # Set up production logging if requested
    if getattr(args, "production", False) or getattr(args, "json_log", False):
        from ..base.production_logging import setup_production_logging

        production_config = setup_production_logging(
            json_output=getattr(args, "json_log", False),
            verbose=(args.loglevel == logging.DEBUG),
        )
        logger = production_config.create_station_logger("receivers")
        audit_logger = production_config.get_audit_logger()
    else:
        logger = setup_logging(args.loglevel)
        audit_logger = None

    # Normalize station tokens — strips stray commas/whitespace so
    # `receivers download AFST, ENTC, FAGD` works the same as
    # `receivers download AFST ENTC FAGD`. Without this, argparse passes
    # the literal trailing-comma tokens through and lookups silently fail.
    from .arguments import normalize_station_tokens

    args.stations = normalize_station_tokens(args.stations)

    # Expand --all to all configured stations
    if getattr(args, "all_stations", False):
        all_configs = get_all_station_configs()
        args.stations = sorted(all_configs.keys())
        logger.info(f"--all: resolved {len(args.stations)} stations")
    elif not args.stations:
        logger.error("No stations specified. Use station IDs or --all")
        return 1

    logger.info(f"Starting download for stations: {args.stations}")

    # Process time arguments (from getSeptentrio3 logic).
    # Shared with cmd_rinex via resolve_time_range(); the keyword flags below
    # reproduce THIS verb's long-standing behaviour exactly. cmd_rinex passes a
    # different set — the two had drifted apart and are deliberately still
    # different here; converging them is a separate reviewed decision.
    start_time, end_time, reverse_chronological = resolve_time_range(
        session=args.session,
        start=parse_datetime(args.start) if args.start else None,
        end=parse_datetime(args.end) if args.end else None,
        days=args.days,
        # download matches "1hr" case-SENSITIVELY (unlike cmd_rinex)
        case_insensitive_session=False,
        # download derives a start when only --end is given (cmd_rinex does not)
        infer_start_from_end=True,
        # download does NOT widen an equal start/end (cmd_rinex does)
        inclusive_same_day=False,
    )

    # Process session frequency arguments (from getSeptentrio3)
    afrequency = args.afrequency or args.session.split("_")[0]
    ffrequency = args.ffrequency or args.session.split("_")[1]

    # Convert frequency to gtimes format
    frequency_mapping = {
        "24hr": "1D",  # Daily
        "1hr": "1H",  # Hourly
    }
    ffrequency = frequency_mapping.get(ffrequency, ffrequency)

    logger.info(f"Time range: {start_time} to {end_time}")
    logger.info(
        f"Session: {args.session}, File frequency: {ffrequency}, Acquisition frequency: {afrequency}"
    )

    # Download for each station
    total_downloaded = 0
    total_errors = 0

    # External-fetch mode: stations whose data arrives from a third-party
    # server (stations.cfg external_url_template / operational_status=external)
    # instead of a receiver we reach directly. Reuses the resolved time range.
    # Same flag contract as the receiver path: --sync actually fetches,
    # --archive archives + indexes (file_tracking + archive_catalog).
    if getattr(args, "external", False):
        from pathlib import Path as _Path

        from ..config.receivers_config import get_receivers_config
        from ..external_fetch import (
            build_external_urls,
            external_station_config,
            fetch_external_station,
            raw_station_config,
        )

        data_prepath = _Path(get_receivers_config().get_data_prepath())
        sync = getattr(args, "sync", False)
        archive = getattr(args, "archive", False)
        tracker = None
        if archive:
            from ..health.file_tracker import FileTracker

            tracker = FileTracker()
        session_type = f"{args.session}_rinex"
        for sid in args.stations:
            cfg = raw_station_config(sid)
            if cfg is None:
                logger.error("external %s: no stations.cfg section — skipping", sid)
                total_errors += 1
                continue
            ext = external_station_config(cfg)
            if ext is None:
                logger.error(
                    "external %s: no external_url_template in stations.cfg — "
                    "not an external station",
                    sid,
                )
                total_errors += 1
                continue
            dest = (
                data_prepath
                / start_time.strftime("%Y")
                / start_time.strftime("%b").lower()
                / sid.upper()
                / args.session
                / "rinex"
            )
            if not sync:
                # Dry-run: report the remote files that would be fetched.
                urls = build_external_urls(
                    sid, ext["url_template"], ext["frequency"], start_time, end_time
                )
                logger.info(
                    "external %s: dry-run — %d file(s) would be fetched",
                    sid,
                    len(urls),
                )
                continue
            files = fetch_external_station(
                sid,
                cfg,
                start_time,
                end_time,
                dest,
                tracker=tracker,
                session_type=session_type,
            )
            total_downloaded += len(files)
        logger.info(
            f"External download complete. Files: {total_downloaded}, Errors: {total_errors}"
        )
        return 0 if total_errors == 0 else 1

    # Parallel mode: grouped batching with stagger and retry
    if getattr(args, "parallel", False) and len(args.stations) > 1:
        from .parallel import download_parallel

        summary = download_parallel(
            stations=args.stations,
            args=args,
            logger=logger,
            start_time=start_time,
            end_time=end_time,
            ffrequency=ffrequency,
            afrequency=afrequency,
            reverse_chronological=reverse_chronological,
            _audit_logger=audit_logger,
        )
        total_downloaded = summary.total_files
        total_errors = summary.failed + summary.unreachable

        # Wait for any pending RINEX conversions before exiting
        if getattr(args, "rinex", False):
            from ..rinex.async_converter import shutdown_rinex_pool

            shutdown_rinex_pool(wait=True)

        logger.info(
            f"Download complete. Total files: {total_downloaded}, Errors: {total_errors}"
        )
        return 0 if total_errors == 0 else 1

    # Network-first mode: when using -d (reverse_chronological) with multiple stations,
    # iterate day→station so all stations get the latest data first.
    network_first = reverse_chronological and len(args.stations) > 1

    if network_first:
        # Pre-validate all stations upfront
        receivers_map: Dict[str, Any] = {}
        for sid in args.stations:
            sid = sid.upper()
            receiver = _validate_station_for_download(sid, logger, session=args.session)
            if receiver is None:
                total_errors += 1
                continue
            receivers_map[sid] = receiver

        if not receivers_map:
            logger.error("No valid stations to download")
            return 1

        # Set up per-file RINEX callbacks
        if getattr(args, "rinex", False):
            _setup_rinex_callbacks(receivers_map, args.session)

        # Outer: periods (newest first), Inner: stations
        periods = generate_period_ranges(
            start_time, end_time, args.session, reverse=True
        )
        for period_start, period_end in periods:
            if args.session == "15s_24hr":
                logger.info(f"--- {period_start.strftime('%Y-%m-%d')} ---")
            else:
                logger.info(f"--- {period_start.strftime('%Y-%m-%d %H:%M')} ---")
            for sid, receiver in receivers_map.items():
                logger.info(f"Processing station: {sid}")
                dl, err, _checked = _download_station_period(
                    receiver,
                    sid,
                    period_start,
                    period_end,
                    args,
                    logger,
                    audit_logger,
                    ffrequency=ffrequency,
                    afrequency=afrequency,
                    reverse_chronological=False,  # single period, no need to reverse
                )
                total_downloaded += dl
                total_errors += err
    else:
        # Station-first: current behavior (single station or explicit -s/-e range)
        for station_id in args.stations:
            station_id = station_id.upper()
            logger.info(f"Processing station: {station_id}")

            receiver = _validate_station_for_download(
                station_id, logger, session=args.session
            )
            if receiver is None:
                total_errors += 1
                continue

            # Set up per-file RINEX callback
            if getattr(args, "rinex", False):
                _setup_rinex_callbacks({station_id: receiver}, args.session)

            dl, err, _checked = _download_station_period(
                receiver,
                station_id,
                start_time,
                end_time,
                args,
                logger,
                audit_logger,
                ffrequency=ffrequency,
                afrequency=afrequency,
                reverse_chronological=reverse_chronological,
            )
            total_downloaded += dl
            total_errors += err

    # Wait for pending RINEX conversions before exit (non-parallel paths)
    if getattr(args, "rinex", False):
        from ..rinex.async_converter import shutdown_rinex_pool

        shutdown_rinex_pool(wait=True)

    # Final summary
    logger.info(
        f"Download complete. Total files: {total_downloaded}, Errors: {total_errors}"
    )
    return 0 if total_errors == 0 else 1


def cmd_status(args) -> int:
    """Status command - thin wrapper around ``health --compact``.

    Provides the same compact output as before by delegating to cmd_health
    with appropriate defaults set.
    """
    # Set health-command defaults that status doesn't expose
    args.compact = True
    args.no_files = False
    args.no_ntrip = False
    # --host / --receiver-type are added to the status parser; ensure they exist
    if not hasattr(args, "host"):
        args.host = None
    if not hasattr(args, "receiver_type"):
        args.receiver_type = "PolaRX5"
    # Date flags — status is live-only
    args.start = None
    args.end = None
    args.days = None
    args.extract_all = False
    args.import_json = False
    args.export_json = False
    args.save_json = False
    args.skip_blocks = True
    args.force = False
    return cmd_health(args)


def _send_status_to_icinga(
    results: list, logger: logging.Logger, save_to_db: bool = True
) -> int:
    """Send status check results to Icinga monitoring system.

    Also saves health data to database for Grafana dashboards (same data,
    single extraction).

    Args:
        results: List of (health_data, station_config) tuples
        logger: Logger instance
        save_to_db: Also save health data to database (default: True)

    Returns:
        0 if all checks sent successfully, 1 otherwise
    """
    try:
        from ..monitoring.icinga_client import IcingaClient
    except ImportError:
        logger.error(
            "❌ Icinga client not available. Install requests: pip install requests"
        )
        return 1

    # Initialize database writer if saving to DB
    db_writer = None
    if save_to_db:
        try:
            from ..health.db_writer import HealthDatabaseWriter

            db_writer = HealthDatabaseWriter()
            if not db_writer.connect():
                logger.warning("Could not connect to database, skipping DB storage")
                db_writer = None
        except Exception as e:
            logger.warning(f"Database not available: {e}")
            db_writer = None

    client = IcingaClient()
    all_success = True
    has_critical = False

    for health, station_config in results:
        station_id = health.get("station_id", "UNKNOWN")

        # Track critical status
        if health.get("overall_status") == "critical":
            has_critical = True

        # Save to database (same data used for Icinga and Grafana)
        if db_writer:
            try:
                db_writer.write_health_data(health)
                logger.debug(f"Saved health data to database for {station_id}")
            except Exception as e:
                logger.warning(f"Failed to save to database for {station_id}: {e}")

        # Send all health-based checks to Icinga
        try:
            responses = client.send_health_from_json(health)

            # Print results
            print(f"\n=== Icinga results for {station_id} ===")
            for check_name, response in responses.items():
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} {check_name}: sent (HTTP {code})")
                else:
                    print(
                        f"{status} {check_name}: FAILED - {response.get('message', 'Unknown error')}"
                    )
                    all_success = False

        except Exception as e:
            logger.error(f"Failed to send checks for {station_id}: {e}")
            all_success = False

        # Send file status checks (check archive file system)
        try:
            from ..health.file_tracker import ArchiveFileChecker

            checker = ArchiveFileChecker()

            # Check daily files (15s_24hr)
            stats = checker.check_file_status(station_id, "15s_24hr", days_back=7)
            if stats:
                response = client.send_download_check(
                    station=station_id,
                    session_type="15s_24hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 7),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(
                        0, stats.get("files_expected", 0) - stats.get("files_found", 0)
                    ),
                    error_count=0,
                    warn_hours=26.0,  # Warning after 26 hours for daily files
                    crit_hours=50.0,  # Critical after 50 hours
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 15s_24hr file status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 15s_24hr file status: FAILED - {response.get('message', 'Unknown error')}"
                    )

            # Check hourly files (1Hz_1hr)
            stats = checker.check_file_status(station_id, "1Hz_1hr", days_back=1)
            if stats and stats.get("files_found", 0) > 0:
                response = client.send_download_check(
                    station=station_id,
                    session_type="1Hz_1hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(
                        0, stats.get("files_expected", 0) - stats.get("files_found", 0)
                    ),
                    error_count=0,
                    warn_hours=2.0,  # Warning after 2 hours for hourly files
                    crit_hours=4.0,  # Critical after 4 hours
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 1Hz_1hr file status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 1Hz_1hr file status: FAILED - {response.get('message', 'Unknown error')}"
                    )

            # Check daily RINEX files (15s_24hr_rinex) - only if directory exists
            stats = checker.check_file_status(station_id, "15s_24hr_rinex", days_back=7)
            if stats and stats.get("dir_exists", False):
                response = client.send_download_check(
                    station=station_id,
                    session_type="15s_24hr rinex",  # Icinga service name with space
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 7),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(
                        0, stats.get("files_expected", 0) - stats.get("files_found", 0)
                    ),
                    error_count=0,
                    warn_hours=26.0,
                    crit_hours=50.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 15s_24hr rinex file status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 15s_24hr rinex file status: FAILED - {response.get('message', 'Unknown error')}"
                    )

            # Check hourly RINEX files (1Hz_1hr_rinex) - only if directory exists
            stats = checker.check_file_status(station_id, "1Hz_1hr_rinex", days_back=1)
            if (
                stats
                and stats.get("dir_exists", False)
                and stats.get("files_found", 0) > 0
            ):
                response = client.send_download_check(
                    station=station_id,
                    session_type="1Hz_1hr rinex",  # Icinga service name with space
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(
                        0, stats.get("files_expected", 0) - stats.get("files_found", 0)
                    ),
                    error_count=0,
                    warn_hours=2.0,
                    crit_hours=4.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 1Hz_1hr rinex file status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 1Hz_1hr rinex file status: FAILED - {response.get('message', 'Unknown error')}"
                    )

            # Check 20Hz hourly files - only if directory exists
            stats = checker.check_file_status(station_id, "20Hz_1hr", days_back=1)
            if (
                stats
                and stats.get("dir_exists", False)
                and stats.get("files_found", 0) > 0
            ):
                response = client.send_download_check(
                    station=station_id,
                    session_type="20Hz_1hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(
                        0, stats.get("files_expected", 0) - stats.get("files_found", 0)
                    ),
                    error_count=0,
                    warn_hours=2.0,
                    crit_hours=4.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 20Hz_1hr file status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 20Hz_1hr file status: FAILED - {response.get('message', 'Unknown error')}"
                    )

            # Check 50Hz hourly files - only if directory exists
            stats = checker.check_file_status(station_id, "50Hz_1hr", days_back=1)
            if (
                stats
                and stats.get("dir_exists", False)
                and stats.get("files_found", 0) > 0
            ):
                response = client.send_download_check(
                    station=station_id,
                    session_type="50Hz_1hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(
                        0, stats.get("files_expected", 0) - stats.get("files_found", 0)
                    ),
                    error_count=0,
                    warn_hours=2.0,
                    crit_hours=4.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 50Hz_1hr file status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 50Hz_1hr file status: FAILED - {response.get('message', 'Unknown error')}"
                    )

        except Exception as e:
            logger.debug(f"Could not send file status checks for {station_id}: {e}")

        # Send 24hr processing status check
        try:
            from ..health.file_tracker import ProcessingStatusChecker

            proc_checker = ProcessingStatusChecker()
            result = proc_checker.check_24hr_processing(station_id)

            if result.get("file_exists", False):
                response = client.send_processing_check(
                    station=station_id,
                    check_name="24hr processing status",
                    status=result.get("status", "unknown"),
                    message=result.get("message", "Unknown status"),
                    days_behind=result.get("days_behind"),
                    latest_date=result.get("latest_date"),
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 24hr processing status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} 24hr processing status: FAILED - {response.get('message', 'Unknown error')}"
                    )
        except Exception as e:
            logger.debug(f"Could not send processing status for {station_id}: {e}")

        # Send RTK status check (NTRIP stream health)
        try:
            from ..config.receivers_config import ReceiversConfig
            from ..monitoring.ntrip_client import NTRIPConfig, check_ntrip_status

            receivers_cfg = ReceiversConfig()
            ntrip_status = check_ntrip_status(
                station_id=station_id,
                receivers_config=receivers_cfg,
                station_config=station_config,
            )

            if ntrip_status:
                response = client.send_rtk_check(
                    station=station_id,
                    ntrip_status=ntrip_status,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} rtk status: sent (HTTP {code})")
                else:
                    print(
                        f"{status} rtk status: FAILED - {response.get('message', 'Unknown error')}"
                    )
        except Exception as e:
            logger.debug(f"Could not send RTK status for {station_id}: {e}")

    # Close database connection
    if db_writer:
        db_writer.close()

    return 0 if all_success and not has_critical else 1


def _print_quick_status(health: Dict[str, Any], station_config: Dict[str, Any]) -> None:
    """Print compact status output for quick checks.

    Args:
        health: Health status dictionary from get_health_status()
        station_config: Station configuration dictionary
    """
    station_id = health.get("station_id", "UNKN")
    receiver_type = health.get("receiver_type", "Unknown")
    overall = health.get("overall_status", "unknown")

    # Get IP from station config (try common locations)
    ip = (
        station_config.get("ip")
        or station_config.get("router", {}).get("ip")
        or station_config.get("host")
        or "N/A"
    )

    # Overall status with emoji
    status_emoji = {
        "healthy": "✅",
        "warning": "⚠️",
        "critical": "❌",
        "unknown": "❓",
    }

    # Header line: station, type, IP, overall status
    print(
        f"{station_id} ({receiver_type}) @ {ip}  {status_emoji.get(overall, '❓')} {overall.upper()}"
    )

    # Port status from metrics.ports (aligns with Icinga Receiver status check)
    metrics = health.get("metrics", {})
    ports = metrics.get("ports", {})
    if ports:
        port_parts = []
        for port_name in ["ftp", "http", "control"]:
            port_data = ports.get(port_name, {})
            if isinstance(port_data, dict) and "open" in port_data:
                is_open = port_data.get("open", False)
                port_num = port_data.get("port", "?")
                if is_open:
                    port_parts.append(f"{port_name}:{port_num} ✅")
                else:
                    port_parts.append(f"{port_name}:{port_num} closed")
            else:
                port_parts.append(f"{port_name}: N/A")
        if port_parts:
            print(f"  Ports: {' | '.join(port_parts)}")
    else:
        # Fallback to connection status if no port data
        connection = health.get("connection", {})
        conn_parts = []
        for level, data in connection.items():
            status = data.get("status", "unknown")
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            conn_parts.append(f"{level}:{emoji}")
        if conn_parts:
            print(f"  Connection: {' '.join(conn_parts)}")

    # Key metrics on one line each
    metrics = health.get("metrics", {})
    if metrics:
        metric_lines = []

        # Power/Voltage
        power = metrics.get("power", {})
        if power:
            voltage = power.get("voltage")
            if voltage is not None:
                status = power.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"Voltage: {emoji} {voltage:.2f} V")

        # Temperature
        temp = metrics.get("temperature", {})
        if temp:
            value = temp.get("value")
            if value is not None:
                status = temp.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"Temp: {emoji} {value}°C")

        # CPU load
        cpu = metrics.get("cpu_load", {})
        if cpu:
            value = cpu.get("value", cpu.get("percent"))
            if value is not None:
                status = cpu.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"CPU: {emoji} {value}%")

        # Disk usage
        disk = metrics.get("disk", {})
        if disk:
            value = disk.get("usage_percent")
            if value is not None:
                status = disk.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"Disk: {emoji} {value:.0f}%")

        if metric_lines:
            print(f"  Metrics: {' | '.join(metric_lines)}")

        # Satellites (sent to Icinga as "Satellite status")
        sats = metrics.get("satellites", {})
        if sats:
            total = sats.get("total")
            if total is not None:
                status = sats.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                by_const = sats.get("by_constellation", {})
                const_str = (
                    ", ".join(f"{k}:{v}" for k, v in by_const.items())
                    if by_const
                    else ""
                )
                if const_str:
                    print(f"  Satellites: {emoji} {total} ({const_str})")
                else:
                    print(f"  Satellites: {emoji} {total}")

        # Position (sent to Icinga as "Station position")
        pos = metrics.get("position", {})
        if pos:
            fix_mode = pos.get("fix_mode") or pos.get("fix_type")
            if fix_mode:
                status = pos.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                sats_used = pos.get("satellites_used", "")
                lat = pos.get("latitude")
                lon = pos.get("longitude")
                height = pos.get("height")
                pos_str = f"{fix_mode}"
                if sats_used:
                    pos_str += f", {sats_used} sats"
                if lat is not None and lon is not None:
                    pos_str += f" @ {lat:.5f}, {lon:.5f}"
                    if height is not None:
                        pos_str += f", {height:.1f}m"
                print(f"  Position: {emoji} {pos_str}")

            # DOP values (Trimble provides PDOP/HDOP/VDOP/TDOP)
            pdop = pos.get("pdop")
            if pdop is not None:
                hdop = pos.get("hdop")
                vdop = pos.get("vdop")
                dop_parts = [f"PDOP:{pdop:.1f}"]
                if hdop is not None:
                    dop_parts.append(f"HDOP:{hdop:.1f}")
                if vdop is not None:
                    dop_parts.append(f"VDOP:{vdop:.1f}")
                print(f"  DOP: {' '.join(dop_parts)}")

        # Uptime
        uptime = metrics.get("uptime", {})
        if uptime and uptime.get("available") is not False:
            formatted = uptime.get("formatted")
            if formatted:
                print(f"  Uptime: {formatted}")

    # Logging status (sent to Icinga as "Logging status")
    data_quality = health.get("data_quality", {})
    disk_status = data_quality.get("disk", {})
    if disk_status:
        status = disk_status.get("status", "unknown")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            print(f"  Logging: {emoji} {status}")

    # RTK status (NTRIP stream health)
    rtk_status = health.get("rtk", {})
    if rtk_status:
        status = rtk_status.get("status", "unknown")
        message = rtk_status.get("message", "")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            print(f"  RTK: {emoji} {message}")

    # File status checks
    file_status = health.get("file_status", {})
    if file_status:
        file_parts = []
        # Order: raw files first, then RINEX, then high-rate
        session_order = [
            "15s_24hr",
            "1Hz_1hr",
            "15s_24hr_rinex",
            "1Hz_1hr_rinex",
            "20Hz_1hr",
            "50Hz_1hr",
        ]
        session_labels = {
            "15s_24hr": "15s",
            "1Hz_1hr": "1Hz",
            "15s_24hr_rinex": "15s-rnx",
            "1Hz_1hr_rinex": "1Hz-rnx",
            "20Hz_1hr": "20Hz",
            "50Hz_1hr": "50Hz",
        }
        # Get thresholds from icinga config
        try:
            from ..config.icinga_config import get_icinga_config

            icinga_thresholds = get_icinga_config().get_thresholds()
            daily_warn = icinga_thresholds.file_daily_warning_hours
            daily_crit = icinga_thresholds.file_daily_critical_hours
            hourly_warn = icinga_thresholds.file_hourly_warning_hours
            hourly_crit = icinga_thresholds.file_hourly_critical_hours
        except Exception:
            daily_warn, daily_crit = 26.0, 50.0
            hourly_warn, hourly_crit = 2.0, 4.0

        thresholds = {
            "15s_24hr": (daily_warn, daily_crit),
            "1Hz_1hr": (hourly_warn, hourly_crit),
            "15s_24hr_rinex": (daily_warn, daily_crit),
            "1Hz_1hr_rinex": (hourly_warn, hourly_crit),
            "20Hz_1hr": (hourly_warn, hourly_crit),
            "50Hz_1hr": (hourly_warn, hourly_crit),
        }
        for session in session_order:
            stats = file_status.get(session)
            if stats:
                hours = stats.get("hours_since_file")
                warn_h, crit_h = thresholds.get(session, (26, 50))
                if hours is None:
                    status = "critical"
                elif hours >= crit_h:
                    status = "critical"
                elif hours >= warn_h:
                    status = "warning"
                else:
                    status = "ok"
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                label = session_labels.get(session, session)
                file_parts.append(f"{label}:{emoji}")
        if file_parts:
            print(f"  Files: {' '.join(file_parts)}")

    # Archive status from file_tracking database (what we actually have)
    archive_status = health.get("archive_status", {})
    if archive_status:
        status = archive_status.get("status", "unknown")
        message = archive_status.get("message", "")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            print(f"  Archive: {emoji} {message}")

    # 24hr GAMIT timeseries (production processing — separate from our downloads)
    proc_status = health.get("processing_24hr", {})
    if proc_status:
        status = proc_status.get("status", "unknown")
        message = proc_status.get("message", "")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            short_msg = (
                message.replace("24hr processing ", "")
                .replace("OK - ", "")
                .replace("CRITICAL - ", "")
            )
            print(f"  Timeseries: {emoji} {short_msg}")


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


def cmd_health_timeseries_extract(
    args, station_id: str, station_config: Dict[str, Any], logger: logging.Logger
) -> int:
    """Extract time-series health data from SBF files and save to JSON.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        station_config: Station configuration dictionary
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    from ..health.json_writer import HealthJSONWriter
    from ..health.timeseries_extractor import TimeSeriesHealthExtractor

    try:
        # Parse dates to extract using unified flags: -s/--start, -e/--end, -d/--days
        # For status_1hr (hourly session), -d counts hours not days (consistent with download)
        from ..utils.time_utils import calculate_download_time_range

        dates_to_extract = []
        start_date = None
        end_date = None
        session_type = "status_1hr"  # Health extraction uses status_1hr session
        is_hourly = True  # status_1hr is hourly data

        # Handle -d/--days: N periods back (hours for hourly, days for daily)
        if getattr(args, "days", None):
            # Use same time range calculation as download command for consistency
            start_time, end_time = calculate_download_time_range(
                session_type=session_type, lookback_periods=args.days
            )
            # Convert to dates for directory-based extraction
            # For hourly data, we need unique dates that span the hour range
            start_date = start_time.replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            )
            end_date = end_time.replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            )
            # Make sure we include end_date's day if end_time has hours
            if end_time.hour > 0 or end_time == start_time:
                pass  # end_date is correct
            logger.info(
                f"Period range: {args.days} {'hours' if is_hourly else 'days'} "
                f"({start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')})"
            )

        # Handle -s/--start
        if getattr(args, "start", None):
            try:
                start_date = datetime.strptime(args.start, "%Y%m%d")
            except ValueError:
                logger.error(
                    f"Invalid start date format: {args.start} (expected YYYYMMDD)"
                )
                return 1

        # Handle -e/--end
        if getattr(args, "end", None):
            try:
                end_date = datetime.strptime(args.end, "%Y%m%d")
            except ValueError:
                logger.error(f"Invalid end date format: {args.end} (expected YYYYMMDD)")
                return 1

        # If only start provided, end = start (single day)
        if start_date and not end_date:
            end_date = start_date

        # If only end provided, start = end (single day)
        if end_date and not start_date:
            start_date = end_date

        # Handle --extract-all
        if getattr(args, "extract_all", False):
            logger.error(
                "--extract-all not yet implemented (requires scanning archive directory)"
            )
            return 1

        # Validate we have dates
        if not start_date or not end_date:
            logger.error(
                "No dates to extract. Use -s DATE, -s START -e END, or -d DAYS"
            )
            return 1

        if start_date > end_date:
            logger.error("Start date must be before or equal to end date")
            return 1

        # Generate list of dates
        current_date = start_date
        while current_date <= end_date:
            dates_to_extract.append(current_date)
            current_date += timedelta(days=1)

        logger.info(
            f"Extracting data for {len(dates_to_extract)} days: "
            f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )

        # Get receiver type
        receiver_type = station_config.get("receiver", {}).get("type", "PolaRX5")

        # Get data paths from receivers_config
        from ..config.receivers_config import get_receivers_config

        receivers_config = get_receivers_config()
        data_prepath = receivers_config.get_prepath()

        logger.debug(f"Using data_prepath: {data_prepath}")
        logger.debug(f"Receiver type: {receiver_type}")

        # Initialize extractors
        extractor = TimeSeriesHealthExtractor(station_id, receiver_type)

        # Process each date
        success_count = 0
        skip_count = 0
        error_count = 0

        # Initialize file tracker for import tracking
        from ..health import FileTracker, compute_checksum

        file_tracker = FileTracker()
        tracker_connected = file_tracker.connect()

        today = datetime.now().date()

        for date in dates_to_extract:
            try:
                # Check if already imported (skip if --save-db and already in DB, unless --force)
                # For hourly data, never skip today since new hours keep arriving
                if (
                    getattr(args, "save_db", False)
                    and tracker_connected
                    and not getattr(args, "force", False)
                ):
                    # Convert datetime to date for tracking check
                    check_date = date.date() if hasattr(date, "date") else date
                    # Don't skip today for hourly sessions - new data may have arrived
                    is_today = check_date == today
                    if not is_today and file_tracker.is_health_imported(
                        station_id, check_date
                    ):
                        logger.info(
                            f"⏭️  Skipping {date.strftime('%Y-%m-%d')} (already imported to database)"
                        )
                        skip_count += 1
                        continue
                    elif is_today:
                        logger.debug(
                            f"Re-processing today ({date.strftime('%Y-%m-%d')}) for latest hourly data"
                        )

                # Build path to status_1hr directory for this date
                year = date.strftime("%Y")
                month = date.strftime("%b").lower()
                base_path = Path(data_prepath) / year / month

                # Check both status_1hr/ and status_1hr/raw/ subdirectories
                status_dir = base_path / station_id / "status_1hr"
                status_raw_dir = status_dir / "raw"

                if not status_dir.exists():
                    logger.warning(
                        f"Status directory not found for {date.strftime('%Y-%m-%d')}: {status_dir}"
                    )
                    error_count += 1
                    continue

                # Find all SBF files for this date
                date_str = date.strftime("%Y%m%d")
                sbf_files = []

                # Look for hourly status files: STATION{YYYYMMDD}{HH}00c.sbf.gz
                # Try both status_1hr/ and status_1hr/raw/ subdirectories
                search_dirs = [status_dir]
                if status_raw_dir.exists():
                    search_dirs.append(status_raw_dir)

                for hour in range(24):
                    filename = f"{station_id}{date_str}{hour:02d}00c.sbf.gz"

                    # Check each potential directory
                    for search_dir in search_dirs:
                        filepath = search_dir / filename
                        if filepath.exists() and filepath not in sbf_files:
                            sbf_files.append(filepath)
                            break  # Found in this dir, don't check others

                if not sbf_files:
                    logger.warning(
                        f"No SBF files found for {date.strftime('%Y-%m-%d')} in {status_dir}"
                    )
                    error_count += 1
                    continue

                logger.info(
                    f"Found {len(sbf_files)} SBF files for {date.strftime('%Y-%m-%d')}"
                )

                # Extract daily health data
                health_data = extractor.extract_daily_health(sbf_files, date)

                # Write to JSON
                json_writer = HealthJSONWriter(str(base_path), station_id)
                force = getattr(args, "force", False)

                json_path = json_writer.write_daily_health_data(
                    health_data, date, force=force
                )

                if json_path:
                    logger.info(f"✅ Wrote {json_path.name}")

                    # Update latest symlink
                    json_writer.write_daily_latest_symlink(json_path)

                    # Extract per-block JSONs for exploration (unless disabled)
                    if not getattr(args, "skip_blocks", False):
                        logger.info("Extracting per-block JSONs for exploration...")
                        from ..health.block_json_writer import BlockJsonWriter

                        block_writer = BlockJsonWriter(station_id, status_dir / "json")
                        try:
                            block_stats = block_writer.extract_all_blocks(
                                sbf_files, date.date()
                            )
                            if block_stats:
                                logger.info(
                                    f"✅ Extracted {len(block_stats)} block types:"
                                )
                                for block_name, count in block_stats.items():
                                    logger.info(f"   - {block_name}: {count} samples")
                            else:
                                logger.debug("No additional blocks found")
                        except Exception as e:
                            logger.warning(f"Per-block extraction failed: {e}")
                            if args.loglevel == logging.DEBUG:
                                import traceback

                                traceback.print_exc()

                    success_count += 1
                else:
                    # JSON was skipped (no source changes)
                    # But if --save-db is requested, we need to check if data is in DB
                    check_date = date.date() if hasattr(date, "date") else date
                    db_has_data = tracker_connected and file_tracker.is_health_imported(
                        station_id, check_date
                    )

                    if getattr(args, "save_db", False) and not db_has_data:
                        # JSON exists but DB is missing data - import from existing JSON
                        logger.info(
                            f"📥 JSON exists but DB missing - importing from existing JSON for {date.strftime('%Y-%m-%d')}"
                        )

                        # Find and load the existing JSON file
                        date_str = date.strftime("%Y%m%d")
                        json_dir = base_path / station_id / "status_1hr" / "json"
                        existing_json = (
                            json_dir / f"{station_id}_{date_str}_health.json"
                        )

                        if existing_json.exists():
                            try:
                                import json

                                with open(existing_json) as f:
                                    health_data = json.load(f)
                                logger.debug(f"Loaded existing JSON: {existing_json}")
                            except Exception as e:
                                logger.warning(f"Failed to load existing JSON: {e}")
                                health_data = None
                        else:
                            logger.warning(f"Expected JSON not found: {existing_json}")
                            health_data = None

                        # Will be imported in the save_db block below
                        success_count += 1
                    else:
                        logger.info(
                            f"⏭️  Skipped {date.strftime('%Y-%m-%d')} (no source changes, data in DB: {db_has_data})"
                        )
                        skip_count += 1
                        health_data = None  # Don't re-import to DB

                # Save to database if requested
                if getattr(args, "save_db", False) and health_data:
                    try:
                        from ..health.json_importer import HealthJsonImporter

                        with HealthJsonImporter() as importer:
                            if importer.connect(database="gps_health"):
                                rows_imported = importer.import_health_data(
                                    health_data, station_id, receiver_type
                                )
                                logger.info(
                                    f"💾 Saved {rows_imported} rows to database for {date.strftime('%Y-%m-%d')}"
                                )

                                # Mark as imported in file tracker
                                if tracker_connected and rows_imported > 0:
                                    checksum = compute_checksum(health_data)
                                    # Convert datetime to date for tracking
                                    track_date = (
                                        date.date() if hasattr(date, "date") else date
                                    )
                                    file_tracker.mark_health_imported(
                                        station_id,
                                        track_date,
                                        rows_imported,
                                        checksum,
                                        str(json_path) if json_path else None,
                                    )
                            else:
                                logger.warning("Failed to connect to database")
                    except Exception as e:
                        logger.warning(f"Database save failed: {e}")

            except Exception as e:
                logger.error(f"Failed to extract {date.strftime('%Y-%m-%d')}: {e}")
                if args.loglevel == logging.DEBUG:
                    import traceback

                    traceback.print_exc()
                error_count += 1

        # Cleanup file tracker
        file_tracker.close()

        # Summary
        logger.info(f"\n{'=' * 60}")
        logger.info("Extraction complete:")
        logger.info(f"  ✅ Extracted: {success_count}")
        logger.info(f"  ⏭️  Skipped: {skip_count}")
        logger.info(f"  ❌ Errors: {error_count}")
        logger.info(f"{'=' * 60}")

        return 0 if error_count == 0 else 1

    except Exception as e:
        logger.error(f"Time-series extraction failed: {e}")
        if args.loglevel == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return 1


def cmd_health_json_import(args, station_id: str, logger: logging.Logger) -> int:
    """Import JSON health files to database.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    from pathlib import Path

    from ..health.json_importer import HealthJsonImporter

    # Determine JSON directory
    if getattr(args, "json_dir", None):
        json_dir = Path(args.json_dir)
    else:
        # Auto-detect from data path
        from datetime import datetime

        from ..config.receivers_config import get_receivers_config

        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        now = datetime.now()
        month_abbr = now.strftime("%b").lower()
        year = now.strftime("%Y")

        json_dir = (
            Path(data_prepath) / year / month_abbr / station_id / "status_1hr" / "json"
        )

    if not json_dir.exists():
        logger.error(f"JSON directory not found: {json_dir}")
        return 1

    logger.info(f"Importing JSON health data for {station_id} from {json_dir}")

    # Parse date filters
    start_date = None
    end_date = None

    if getattr(args, "start", None):
        try:
            start_date = datetime.strptime(args.start, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid start date format: {args.start}")
            return 1

    if getattr(args, "end", None):
        try:
            end_date = datetime.strptime(args.end, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid end date format: {args.end}")
            return 1

    # Import
    try:
        with HealthJsonImporter() as importer:
            if not importer.connect(database="gps_health"):
                logger.error("Failed to connect to database")
                return 1

            files, rows, skipped = importer.import_directory(
                json_dir,
                station_id=station_id,
                start_date=start_date,
                end_date=end_date,
            )

            logger.info(
                f"Import complete: {files} files, {rows} rows imported, {skipped} skipped"
            )
            return 0

    except Exception as e:
        logger.error(f"Import failed: {e}")
        return 1


def cmd_health_json_export(args, station_id: str, logger: logging.Logger) -> int:
    """Export health data from database to JSON files.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    from datetime import datetime
    from pathlib import Path

    from ..health.json_importer import HealthJsonImporter

    # Determine output directory
    if getattr(args, "json_dir", None):
        output_dir = Path(args.json_dir)
    else:
        # Auto-detect from data path
        from ..config.receivers_config import get_receivers_config

        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        now = datetime.now()
        month_abbr = now.strftime("%b").lower()
        year = now.strftime("%Y")

        output_dir = (
            Path(data_prepath) / year / month_abbr / station_id / "status_1hr" / "json"
        )

    # Parse date filters
    start_date = None
    end_date = None

    if getattr(args, "start", None):
        try:
            start_date = datetime.strptime(args.start, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid start date format: {args.start}")
            return 1

    if getattr(args, "end", None):
        try:
            end_date = datetime.strptime(args.end, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid end date format: {args.end}")
            return 1

    logger.info(f"Exporting health data for {station_id} to {output_dir}")

    # Export
    try:
        with HealthJsonImporter() as exporter:
            if not exporter.connect(database="gps_health"):
                logger.error("Failed to connect to database")
                return 1

            files, rows = exporter.export_to_json(
                output_dir,
                station_id=station_id,
                start_date=start_date,
                end_date=end_date,
            )

            logger.info(f"Export complete: {files} files, {rows} rows exported")
            return 0

    except Exception as e:
        logger.error(f"Export failed: {e}")
        return 1


def _flag_identity_vs_cfg(
    station_id: str,
    identity: Dict[str, Any],
    station_config: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Log + record cfg/receiver identity drift.

    Thin wrapper around :func:`receivers.cfg.identity_check.flag_identity_vs_cfg`
    so the scheduler health jobs and this CLI path share the exact same
    detection rules and write to the same ``cfg_discrepancy`` log.
    """
    from ..cfg.identity_check import flag_identity_vs_cfg

    flag_identity_vs_cfg(station_id, identity, station_config, logger)


def cmd_health_single(args, station_id: str, logger: logging.Logger) -> int:
    """Process health command for a single station.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    try:
        # Check if JSON export requested (database -> JSON)
        if getattr(args, "export_json", False):
            return cmd_health_json_export(args, station_id, logger)

        # Check if JSON import requested (JSON -> database)
        if getattr(args, "import_json", False):
            return cmd_health_json_import(args, station_id, logger)

        station_config = resolve_receiver_endpoint(args, station_id)
        if station_config is None:
            logger.warning(f"⚠️  Station {station_id} not found in configuration")
            return 1

        # Check if time-series extraction requested (any date flag triggers extraction)
        if any(
            [
                getattr(args, "start", None),
                getattr(args, "end", None),
                getattr(args, "days", None),
                getattr(args, "extract_all", False),
            ]
        ):
            return cmd_health_timeseries_extract(
                args, station_id, station_config, logger
            )

        # --- Live mode (no date flags) ---
        # Create receiver instance using factory pattern
        receiver = create_receiver(station_id, station_config)

        # Get comprehensive health status (including files + NTRIP)
        from ..health.live_health import gather_comprehensive_health

        include_files = not getattr(args, "no_files", False)
        include_ntrip = not getattr(args, "no_ntrip", False)
        if station_config.get("_adhoc"):
            include_files = False
            include_ntrip = False

        health = gather_comprehensive_health(
            station_id,
            station_config,
            receiver,
            include_files=include_files,
            include_ntrip=include_ntrip,
        )

        # Compare receiver identity against stations.cfg.
        # The canonical workflow is TOS → cfg via `receivers cfg reconcile`,
        # so health no longer auto-writes. We only flag discrepancies and,
        # opt-in via --update-cfg, perform the legacy in-place update.
        identity = health.get("receiver_identity", {})
        if identity and not station_config.get("_adhoc"):
            _flag_identity_vs_cfg(station_id, identity, station_config, logger)
            if getattr(args, "update_cfg", False):
                from ..config.receivers_config import update_station_identity_in_cfg

                updated = update_station_identity_in_cfg(
                    station_id,
                    firmware_version=identity.get("firmware_version"),
                    receiver_model=identity.get("receiver_model"),
                    serial_number=identity.get("serial_number"),
                )
                if updated:
                    logger.info(
                        f"[{station_id}] Updated receiver identity in stations.cfg "
                        f"(--update-cfg)"
                    )

        # Save to JSON if requested
        if getattr(args, "save_json", False):
            json_path = receiver.save_health_to_json(health)
            if json_path:
                logger.info(f"Saved health data to {json_path}")

        # Save to database if requested
        if getattr(args, "save_db", False):
            if station_config.get("_adhoc"):
                logger.info(
                    f"[{station_id}] --host: skipping DB write for direct connection"
                )
            else:
                success = receiver.save_health_to_database(health)
                if success:
                    logger.info("Saved health data to database")
                    # Also write ping and port status for Grafana dashboard
                    _write_connectivity_status(station_id, health, logger)
                else:
                    logger.warning("Failed to save health data to database")

        # Return health + config for Icinga/compact handling in cmd_health
        # Store on args for the caller to collect
        if not hasattr(args, "_health_results"):
            args._health_results = []
        args._health_results.append((health, station_config))

        # Compact output mode (used by 'status' command wrapper)
        if getattr(args, "compact", False):
            # Output handled by cmd_health after collecting all stations
            return 0

        # Icinga output mode
        if getattr(args, "icinga", False):
            # Output handled by cmd_health after collecting all stations
            return 0

        # Output format
        if getattr(args, "json", False):
            # JSON output
            import json

            print(json.dumps(health, indent=2, default=str))
        else:
            # Human-readable output (detailed)
            print(f"Station: {health['station_id']}")
            print(f"Receiver Type: {health['receiver_type']}")
            identity = health.get("receiver_identity", {})
            if identity.get("receiver_model"):
                print(f"Model: {identity['receiver_model']}")
            if identity.get("firmware_version"):
                print(f"Firmware: {identity['firmware_version']}")
            if identity.get("serial_number"):
                print(f"Serial: {identity['serial_number']}")
            print(f"Timestamp: {health.get('timestamp', 'N/A')}")
            print(f"Overall Status: {health.get('overall_status', 'unknown').upper()}")

            # Connection summary (TCP + ports)
            connection = health.get("connection", {})
            metrics = health.get("metrics", {})
            ports = metrics.get("ports", {})

            print("\nConnection Health:")
            # Show TCP status
            if "tcp" in connection:
                tcp = connection["tcp"]
                status = tcp.get("status", "unknown")
                host = tcp.get("host", "")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                print(f"  tcp: {emoji} {status} ({host})")

            # Show port status (always show all ports for consistency, N/A if not configured)
            for port_name in ["http", "ftp", "control"]:
                if port_name in ports:
                    port_data = ports[port_name]
                    port_num = port_data.get("port", "?")
                    is_open = port_data.get("open", False)
                    if is_open:
                        print(f"  {port_name}: ✅ port {port_num}")
                    else:
                        print(f"  {port_name}: ❌ port {port_num} closed")
                else:
                    print(f"  {port_name}: N/A")

            # Metrics summary
            if metrics:
                print("\nMetrics:")
                # Power
                if "power" in metrics:
                    power = metrics["power"]
                    voltage = power.get("voltage", "N/A")
                    status = power.get("status", "unknown")
                    print(f"  power: {voltage} V [{status}]")

                # CPU
                if "cpu_load" in metrics:
                    cpu = metrics["cpu_load"]
                    percent = cpu.get("percent", "N/A")
                    status = cpu.get("status", "unknown")
                    print(f"  cpu_load: {percent}% [{status}]")

                # Temperature
                if "temperature" in metrics:
                    temp = metrics["temperature"]
                    value = temp.get("value", "N/A")
                    status = temp.get("status", "unknown")
                    print(f"  temperature: {value} C [{status}]")

                # Disk usage
                disk = metrics.get("disk", {})
                if disk and disk.get("available") is not False:
                    usage = disk.get("usage_percent")
                    if usage is not None:
                        total = disk.get("total_mb", 0)
                        free = disk.get("free_mb", 0)
                        status = disk.get("status", "unknown")
                        print(
                            f"  disk: {usage:.1f}% used ({free:.0f} MB free / {total:.0f} MB total) [{status}]"
                        )

                # Position
                if "position" in metrics:
                    pos = metrics["position"]
                    lat = pos.get("latitude")
                    lon = pos.get("longitude")
                    height = pos.get("height")
                    # Handle both PolaRX5 (fix_mode) and Trimble (fix_type) formats
                    fix = pos.get("fix_mode") or pos.get("fix_type", "unknown")
                    status = pos.get("status", "unknown")
                    if lat is not None and lon is not None:
                        if height is not None:
                            print(
                                f"  position: {lat:.6f}, {lon:.6f}, {height:.1f}m ({fix}) [{status}]"
                            )
                        else:
                            print(
                                f"  position: {lat:.6f}, {lon:.6f} ({fix}) [{status}]"
                            )
                    else:
                        print(f"  position: N/A [{status}]")

                    # DOP values (Trimble provides PDOP/HDOP/VDOP/TDOP)
                    pdop = pos.get("pdop")
                    if pdop is not None:
                        dop_parts = [f"PDOP={pdop:.1f}"]
                        for k in ["hdop", "vdop", "tdop"]:
                            v = pos.get(k)
                            if v is not None:
                                dop_parts.append(f"{k.upper()}={v:.1f}")
                        print(f"  dop: {', '.join(dop_parts)}")

                # Uptime
                uptime = metrics.get("uptime", {})
                if uptime and uptime.get("available") is not False:
                    formatted = uptime.get("formatted")
                    seconds = uptime.get("seconds")
                    if formatted:
                        print(f"  uptime: {formatted}")
                    elif seconds is not None:
                        days = seconds // 86400
                        hours = (seconds % 86400) // 3600
                        print(f"  uptime: {days}d {hours}h")

                # System info (serial number, antenna)
                system = metrics.get("system", {})
                if system:
                    parts = []
                    if system.get("serial_number"):
                        parts.append(f"SN:{system['serial_number']}")
                    if system.get("antenna_type"):
                        parts.append(f"Ant:{system['antenna_type']}")
                    if parts:
                        print(f"  system: {', '.join(parts)}")

                # Satellites
                if "satellites" in metrics:
                    sats = metrics["satellites"]
                    total = sats.get("total", 0)
                    by_const = sats.get("by_constellation", {})
                    status = sats.get("status", "unknown")
                    # Build constellation summary
                    const_parts = []
                    for const in ["GPS", "GLONASS", "Galileo", "BeiDou", "SBAS"]:
                        count = by_const.get(const)
                        if count:
                            const_parts.append(f"{const}:{count}")
                    const_str = ", ".join(const_parts) if const_parts else ""
                    if const_str:
                        print(f"  satellites: {total} ({const_str}) [{status}]")
                    else:
                        print(f"  satellites: {total} [{status}]")

            # Status summary
            summary = health.get("status_summary", {})
            if summary:
                print("\nStatus Summary:")
                print(f"  Healthy: {summary.get('healthy', 0)}")
                print(f"  Warning: {summary.get('warning', 0)}")
                print(f"  Critical: {summary.get('critical', 0)}")

        return 0

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1


def cmd_health(args) -> int:
    """Health command - get receiver health information for one or more stations."""
    logger = setup_logging(args.loglevel)

    # Initialize results collector for compact/icinga modes
    args._health_results = []

    # Normalize station tokens (strip stray commas, split comma-separated lists).
    from .arguments import normalize_station_tokens

    args.stations = normalize_station_tokens(args.stations)

    # Expand --all to all configured stations
    if getattr(args, "all_stations", False):
        all_configs = get_all_station_configs()
        args.stations = sorted(all_configs.keys())
        logger.info(f"--all: resolved {len(args.stations)} stations")
    elif not args.stations:
        logger.error("No stations specified. Use station IDs or --all")
        return 1

    # Get list of stations (now supports multiple)
    stations = [s.upper() for s in args.stations]

    # --host is single-target only: direct connection has no concept of "multiple stations"
    if getattr(args, "host", None) and len(stations) > 1:
        logger.error(
            "--host requires exactly one station label; got: " + ", ".join(stations)
        )
        return 1

    # --host is a live check only: date flags require DB access and make no sense for bench use
    if getattr(args, "host", None) and any(
        [
            getattr(args, "start", None),
            getattr(args, "end", None),
            getattr(args, "days", None),
            getattr(args, "extract_all", False),
        ]
    ):
        logger.error(
            "--host is for live checks only; --start/--end/--days/--extract-all are not supported"
        )
        return 1

    if len(stations) > 1:
        logger.info(f"Processing {len(stations)} stations: {', '.join(stations)}")

    # Determine if this is a timeseries extraction with -d flag (network-first candidate)
    has_date_flags = any(
        [
            getattr(args, "start", None),
            getattr(args, "end", None),
            getattr(args, "days", None),
            getattr(args, "extract_all", False),
        ]
    )
    # Network-first: -d with multiple stations → iterate day→station
    use_days = getattr(args, "days", None)
    network_first = (
        has_date_flags
        and use_days is not None
        and not getattr(args, "start", None)
        and len(stations) > 1
    )

    # Track results
    success_count = 0
    error_count = 0

    if network_first:
        # Network-first health extraction: day→station ordering
        # Calculate the date range, then iterate per-day across all stations
        from ..utils.time_utils import calculate_download_time_range as _calc_range

        session_type = "status_1hr"
        start_time, end_time = _calc_range(
            session_type=session_type, lookback_periods=use_days
        )
        # Convert to dates
        start_date = start_time.replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        end_date = end_time.replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )

        # Generate day list (newest first)
        dates = []
        current = start_date
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=1)
        dates.reverse()  # newest first

        logger.info(f"Network-first mode: {len(dates)} days x {len(stations)} stations")

        for date in dates:
            logger.info(f"\n--- {date.strftime('%Y-%m-%d')} ---")
            # Create single-day args for each station
            day_str = date.strftime("%Y%m%d")
            for station_id in stations:
                logger.info(f"Processing station: {station_id}")
                # Create a copy of args scoped to this single day
                import copy

                day_args = copy.copy(args)
                day_args.start = day_str
                day_args.end = day_str
                day_args.days = None  # Use start/end instead
                day_args._health_results = args._health_results  # Share collector

                result = cmd_health_single(day_args, station_id, logger)
                if result == 0:
                    success_count += 1
                else:
                    error_count += 1
    else:
        workers = getattr(args, "workers", 1)
        if workers > 1 and len(stations) > 1:
            # Parallel path
            import copy
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed

            results_lock = threading.Lock()

            def _run_one(sid: str) -> int:
                station_args = copy.copy(args)
                station_args._health_results = []
                station_logger = logging.getLogger(f"receivers.health.{sid}")
                rc = cmd_health_single(station_args, sid, station_logger)
                with results_lock:
                    args._health_results.extend(station_args._health_results)
                return rc

            logger.info(
                f"Running health checks for {len(stations)} stations "
                f"with {workers} parallel workers"
            )
            futures = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for sid in stations:
                    futures[pool.submit(_run_one, sid)] = sid

            for future in as_completed(futures):
                sid = futures[future]
                try:
                    rc = future.result()
                except Exception as exc:
                    logger.error(f"{sid}: health check raised {exc}")
                    rc = 1
                if rc == 0:
                    success_count += 1
                else:
                    error_count += 1
        else:
            # Sequential: current behavior
            for station_id in stations:
                if len(stations) > 1:
                    logger.info(f"\n{'=' * 60}")
                    logger.info(f"Processing station: {station_id}")
                    logger.info(f"{'=' * 60}")

                result = cmd_health_single(args, station_id, logger)

                if result == 0:
                    success_count += 1
                else:
                    error_count += 1

    # --- Post-collection output for compact/icinga modes ---
    results = args._health_results

    if results and getattr(args, "icinga", False):
        return _send_status_to_icinga(results, logger)

    if results and getattr(args, "compact", False):
        has_critical = False
        # JSON output if requested alongside compact
        if getattr(args, "json", False):
            import json

            if len(results) == 1:
                print(json.dumps(results[0][0], indent=2, default=str))
            else:
                print(json.dumps([r[0] for r in results], indent=2, default=str))
        else:
            for health, station_config in results:
                _print_quick_status(health, station_config)
                if health.get("overall_status") == "critical":
                    has_critical = True
        return 1 if has_critical else 0

    # Summary for multiple stations
    if len(stations) > 1:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Summary: {success_count} succeeded, {error_count} failed")
        logger.info(f"{'=' * 60}")

    return 0 if error_count == 0 else 1


def cmd_validate_web_accuracy(args) -> int:
    """Validate configuration accuracy using web interface scraping."""
    import sys
    from pathlib import Path

    # Add the receivers root directory to path
    receivers_root = Path(__file__).parent.parent.parent.parent
    sys.path.insert(0, str(receivers_root))

    try:
        from config_accuracy_validator import ConfigAccuracyValidator

        validator = ConfigAccuracyValidator()

        # Get stations to validate
        from .arguments import normalize_station_tokens

        if args.stations:
            station_ids = normalize_station_tokens(args.stations)
        else:
            # Get all configured stations
            station_configs = get_all_station_configs()
            station_ids = list(station_configs.keys())

        if not station_ids:
            print("❌ No stations found to validate")
            return 1

        # Run web accuracy validation
        results, summary = validator.validate_multiple_stations(station_ids)

        if args.summary:
            # Show summary only
            print("\n📊 CONFIGURATION ACCURACY SUMMARY")
            print(f"{'=' * 50}")
            print(f"Total stations: {summary['total_stations']}")
            print(f"✅ Correct configs: {summary['correct_configs']}")
            print(f"❌ Mismatched configs: {summary['mismatched_configs']}")
            print(f"⚠️  Unverifiable: {summary['unverifiable_configs']}")

            if summary["type_mismatches"]:
                print(f"\n🔧 Type mismatches: {', '.join(summary['type_mismatches'])}")

            if summary["name_mismatches"]:
                print(f"🏷️  Name mismatches: {', '.join(summary['name_mismatches'])}")

            accuracy_rate = summary["correct_configs"] / summary["total_stations"] * 100
            print(f"\n📈 Configuration accuracy: {accuracy_rate:.1f}%")
        else:
            # Show detailed results
            print("\n📋 DETAILED CONFIGURATION VALIDATION")
            print(f"{'=' * 60}")

            for station_id, result in results.items():
                print(f"\n🏢 {station_id}:")
                print(f"   IP: {result['ip']}")

                if result["status"] == "error":
                    print(f"   ❌ ERROR: {result['error']}")
                    continue

                if result.get("actual_type"):
                    type_status = "✅" if result.get("type_match", True) else "❌"
                    print(
                        f"   {type_status} Type: {result['configured_type']} vs {result['actual_type']}"
                    )

                if result.get("actual_station_name"):
                    name_status = "✅" if result.get("name_match", True) else "❌"
                    print(
                        f"   {name_status} Name: {station_id} vs {result['actual_station_name']}"
                    )

                if result.get("type_mismatch"):
                    print(f"      🔧 Fix: {result['type_mismatch']['suggested_fix']}")

                if result.get("name_mismatch"):
                    print(f"      🔧 Fix: {result['name_mismatch']['suggested_fix']}")

        return 0 if summary["mismatched_configs"] == 0 else 1

    except ImportError as e:
        print(f"❌ Web accuracy validation requires additional dependencies: {e}")
        print("   Install with: pip install beautifulsoup4")
        return 1
    except Exception as e:
        print(f"❌ Web accuracy validation failed: {e}")
        return 1


def cmd_validate(args) -> int:
    """Validate command - check receiver type configuration accuracy."""
    logger = setup_logging(args.loglevel)

    # Check if web accuracy validation was requested
    if args.web_accuracy:
        return cmd_validate_web_accuracy(args)

    try:
        # Initialize validator
        validator = ReceiverTypeValidator(logger)

        # Get receiver factory for available types
        factory = get_receiver_factory()
        available_types = list(factory.get_available_types().keys())
        logger.info(f"Available receiver types: {', '.join(available_types)}")

        # Get stations to validate
        if args.stations:
            # Validate specific stations
            from .arguments import normalize_station_tokens

            station_ids = normalize_station_tokens(args.stations)
            station_configs = {}
            for station_id in station_ids:
                config = get_station_config(station_id)
                if config:
                    station_configs[station_id] = config
                else:
                    logger.warning(f"Station {station_id} not found in configuration")
        else:
            # Validate all stations
            logger.info("Validating all stations in configuration...")
            station_configs = get_all_station_configs()

        if not station_configs:
            logger.error("No stations found to validate")
            return 1

        # Run validation
        logger.info(f"Validating receiver types for {len(station_configs)} stations...")
        results = validator.batch_validate_stations(station_configs)

        # Analyze results
        matches = sum(
            1 for r in results.values() if r.get("validation_status") == "match"
        )
        mismatches = sum(
            1 for r in results.values() if r.get("validation_status") == "mismatch"
        )
        unreachable = sum(
            1 for r in results.values() if r.get("validation_status") == "unreachable"
        )
        errors = sum(
            1 for r in results.values() if r.get("validation_status") == "error"
        )

        # Print summary
        print("\n=== RECEIVER TYPE VALIDATION RESULTS ===")
        print(f"Total stations validated: {len(results)}")
        print(f"✅ Correct receiver types: {matches}")
        print(f"❌ Mismatched receiver types: {mismatches}")
        print(f"🔌 Unreachable stations: {unreachable}")
        print(f"⚠️  Errors: {errors}")

        # Show mismatches in detail
        if mismatches > 0:
            print("\n=== RECEIVER TYPE MISMATCHES ===")
            for station_id, result in results.items():
                if result.get("validation_status") == "mismatch":
                    configured = result.get("configured_type", "Unknown")
                    detected = ", ".join(result.get("detected_types", []))
                    suggestion = result.get("suggestion", {})
                    recommended = suggestion.get("recommended_type", "Unknown")
                    confidence = suggestion.get("confidence", 0)

                    print(f"\n📡 {station_id} ({result.get('ip', 'Unknown IP')})")
                    print(f"   Configured: {configured}")
                    print(f"   Detected:   {detected}")
                    print(
                        f"   Recommended: {recommended} (confidence: {confidence:.1%})"
                    )

        # Generate correction report if requested
        if args.report:
            report = validator.generate_correction_report(results)
            print(f"\n{report}")

            # Save report to file
            report_file = f"receiver_validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(report_file, "w") as f:
                f.write(report)
            print(f"📄 Detailed report saved to: {report_file}")

        # Auto-fix if requested (EXPERIMENTAL)
        if args.fix and mismatches > 0:
            logger.warning(
                "⚠️  AUTO-FIX is EXPERIMENTAL - backup your stations.cfg first!"
            )
            response = input("Do you want to proceed with auto-corrections? [y/N]: ")

            if response.lower() in ["y", "yes"]:
                fixed_count = apply_receiver_type_corrections(results)
                print(f"🔧 Applied corrections to {fixed_count} stations")
                if fixed_count > 0:
                    print(
                        "⚠️  Please restart receivers service and verify functionality"
                    )
            else:
                print("Auto-fix cancelled")

        # Return appropriate exit code
        return 0 if (mismatches == 0 and errors == 0) else 1

    except Exception as e:
        logger.error(f"Validation failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


def cmd_rec_config(args) -> int:
    """Receiver configuration command - extract or push config for Septentrio receivers."""
    logger = setup_logging(args.loglevel)

    from pathlib import Path

    from ..config.receivers_config import get_receivers_config
    from ..septentrio.tcp_client import (
        DEFAULT_CONTROL_PORT,
        PolaRX5TCPClient,
        load_config_from_file,
        save_config_to_file,
    )

    # Parse station list
    station_list = [s.strip().upper() for s in args.stations.split(",")]

    # Get receiver config for default port and TCP credentials
    receivers_config = get_receivers_config()
    polarx5_config = receivers_config.get_receiver_config("polarx5")
    tcp_username = polarx5_config.get("tcp_username") or None
    tcp_password = polarx5_config.get("tcp_password") or None
    rec_config_dir = polarx5_config.get("rec_config_dir") or None
    default_port = args.port or polarx5_config.get("control_port", DEFAULT_CONTROL_PORT)
    if isinstance(default_port, str):
        default_port = int(default_port)

    # Build targets list with IPs and ports
    targets = []
    if getattr(args, "host", None):
        # Direct connection: bypass stations.cfg, use native receiver ports
        if len(station_list) > 1:
            logger.error("--host requires exactly one station label")
            return 1
        station_id = station_list[0]
        port = int(args.port or default_port)
        targets.append((station_id, args.host, port))
    else:
        for station_id in station_list:
            station_config = get_station_config(station_id)
            if station_config is None:
                logger.warning(
                    f"Station {station_id} not found in configuration - SKIPPING"
                )
                continue

            # Get IP - try multiple sources
            ip = (
                station_config.get("ip")
                or station_config.get("router", {}).get("ip")
                or station_config.get("host")
            )
            if not ip:
                logger.warning(f"Station {station_id} has no IP configured - SKIPPING")
                continue

            # Get port (station override > cli arg > config default)
            port = station_config.get("receiver", {}).get("controlport")
            if port:
                port = int(port)
            else:
                port = default_port

            targets.append((station_id, ip, port))

    if not targets:
        logger.error("No valid targets found")
        return 1

    logger.info(f"Targets: {', '.join(f'{s} ({ip}:{p})' for s, ip, p in targets)}")

    # Handle extract mode
    if args.extract:
        return _extract_configs(args, targets, logger, tcp_username, tcp_password)

    # Handle --tracking: build an identity-safe signal-tracking config (only
    # setSignalTracking/setSignalUsage + boot save) and push it via the same path.
    if getattr(args, "tracking", None):
        import os as _os
        import tempfile

        from ..septentrio.tracking import build_tracking_commands

        try:
            cmds = build_tracking_commands(args.tracking)
        except ValueError as exc:
            logger.error(str(exc))
            return 2
        tf = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix="track_", delete=False
        )
        tf.write(f"# rec-config --tracking {args.tracking} (identity-safe)\n")
        tf.write("\n".join(cmds) + "\n")
        tf.close()
        args.push = tf.name
        try:
            return _push_configs(
                args, targets, logger, tcp_username, tcp_password, rec_config_dir
            )
        finally:
            _os.unlink(tf.name)

    # Handle --set-antenna: push the reconciled antenna identity from
    # stations.cfg into each receiver (identity-safe: setAntennaOffset + boot
    # save only). Values are PER-STATION, so unlike --tracking each target
    # gets its own generated config file.
    if getattr(args, "set_antenna", False):
        import os as _os
        import tempfile

        from ..septentrio.antenna import build_antenna_commands_from_station_config

        worst = 0
        for target in targets:
            station_id = target[0]
            station_config = get_station_config(station_id)
            if station_config is None:
                logger.error(
                    f"[{station_id}] not in stations.cfg — --set-antenna reads "
                    "the reconciled antenna fields from cfg (no bench mode)"
                )
                worst = max(worst, 1)
                continue
            try:
                cmds = build_antenna_commands_from_station_config(station_config)
            except ValueError as exc:
                logger.error(f"[{station_id}] {exc}")
                worst = max(worst, 2)
                continue
            logger.info(f"[{station_id}] antenna push: {cmds[0]}")
            tf = tempfile.NamedTemporaryFile(
                "w", suffix=".txt", prefix=f"antenna_{station_id}_", delete=False
            )
            tf.write(f"# rec-config --set-antenna {station_id} (identity-safe)\n")
            tf.write("\n".join(cmds) + "\n")
            tf.close()
            args.push = tf.name
            try:
                rc = _push_configs(
                    args, [target], logger, tcp_username, tcp_password, rec_config_dir
                )
                worst = max(worst, rc)
            finally:
                _os.unlink(tf.name)
        return worst

    # Handle --set-domes: push the station's IERS DOMES into the receiver's
    # MARKER NUMBER (identity-safe: setMarkerParameters arg 2 + boot save).
    # Marker-side sibling of --set-antenna; kept separate so that verb's
    # "never touches the marker" guarantee stays literally true.
    if getattr(args, "set_domes", False):
        import os as _os
        import tempfile

        from ..septentrio.marker import (
            NoDomesError,
            build_domes_commands_from_station_config,
        )

        worst = 0
        for target in targets:
            station_id = target[0]
            station_config = get_station_config(station_id)
            if station_config is None:
                logger.error(
                    f"[{station_id}] not in stations.cfg — --set-domes reads "
                    "rinex_marker_number from cfg (no bench mode)"
                )
                worst = max(worst, 1)
                continue
            try:
                cmds = build_domes_commands_from_station_config(station_config)
            except NoDomesError as exc:
                # Not an error: most of the fleet has no DOMES, and those
                # stations must be left alone rather than filled with the
                # 4-char id. Exit status is unaffected.
                logger.info(f"[{station_id}] skipped — {exc}")
                continue
            except ValueError as exc:
                logger.error(f"[{station_id}] {exc}")
                worst = max(worst, 2)
                continue
            logger.info(f"[{station_id}] DOMES push: {cmds[0]}")
            tf = tempfile.NamedTemporaryFile(
                "w", suffix=".txt", prefix=f"domes_{station_id}_", delete=False
            )
            tf.write(f"# rec-config --set-domes {station_id} (identity-safe)\n")
            tf.write("\n".join(cmds) + "\n")
            tf.close()
            args.push = tf.name
            try:
                rc = _push_configs(
                    args, [target], logger, tcp_username, tcp_password, rec_config_dir
                )
                worst = max(worst, rc)
            finally:
                _os.unlink(tf.name)
        return worst

    # Handle --ntrip-stream / --disable-mount: identity-safe NTRIP server on/off
    # (+ optional --drop-sbf), pushed via the same temp-file path as --tracking.
    if getattr(args, "ntrip_stream", None) or getattr(args, "disable_mount", None):
        return _ntrip_stream_configs(
            args, targets, logger, tcp_username, tcp_password, rec_config_dir
        )

    # Handle push mode
    if args.push:
        return _push_configs(
            args, targets, logger, tcp_username, tcp_password, rec_config_dir
        )

    # Handle session check
    if getattr(args, "check_session", None):
        return _check_session(args, targets, logger, tcp_username, tcp_password)

    # Handle session enable (idempotent — skips already-enabled stations)
    if getattr(args, "enable_session", None):
        return _enable_session(
            args,
            targets,
            logger,
            tcp_username,
            tcp_password,
            rec_config_dir,
            session_name=args.enable_session,
            force=False,
        )

    # Handle session update (force re-push, no skip)
    if getattr(args, "update_session", None):
        return _enable_session(
            args,
            targets,
            logger,
            tcp_username,
            tcp_password,
            rec_config_dir,
            session_name=args.update_session,
            force=True,
        )

    # Handle session audit (compare receiver state to canonical template)
    if getattr(args, "audit_session", None):
        return _audit_session(args, targets, logger, tcp_username, tcp_password)

    return 0


def _ntrip_stream_configs(
    args, targets, logger, tcp_username, tcp_password, rec_config_dir
) -> int:
    """Turn an NTRIP server connection on/off, identity-safe (+ optional --drop-sbf).

    Per target: when the mountpoint must be resolved (``--disable-mount``) or the
    feeding SBF stream dropped (``--drop-sbf``), the receiver's current config is
    read first; otherwise the connection/state come straight from
    ``--ntrip-stream``. The resulting setNtripSettings (+ setSBFOutput none + boot
    save) commands are pushed through the normal :func:`_push_configs` path so
    they honour ``--dry-run`` / ``--no-save``.
    """
    import os as _os
    import tempfile

    from ..septentrio.ntrip import (
        build_ntrip_stream_commands,
        parse_ntrip_mounts,
        sbf_streams_for_conn,
    )
    from ..septentrio.tcp_client import PolaRX5TCPClient

    drop_sbf = getattr(args, "drop_sbf", False)
    need_read = bool(args.disable_mount) or drop_sbf
    overall = 0

    for station_id, ip, port in targets:
        config_text = ""
        if need_read:
            try:
                client = PolaRX5TCPClient(
                    ip,
                    station_id,
                    port,
                    timeout=getattr(args, "timeout", 30),
                    username=tcp_username,
                    password=tcp_password,
                    firmware_version=_station_firmware(station_id),
                )
                config_text = client.extract_config("Current")
                client.disconnect()
            except Exception as exc:  # noqa: BLE001 — surface as a per-target error
                logger.error(f"{station_id}: could not read config for NTRIP: {exc}")
                overall = 1
                continue
            if not config_text:
                logger.error(f"{station_id}: empty config — cannot resolve NTRIP")
                overall = 1
                continue

        # Resolve connection + desired state.
        if args.disable_mount:
            mounts = parse_ntrip_mounts(config_text)
            conn = mounts.get(args.disable_mount)
            if not conn:
                logger.error(
                    f"{station_id}: mountpoint {args.disable_mount!r} not found "
                    f"(known: {sorted(mounts) or 'none'})"
                )
                overall = 1
                continue
            state = "off"
            logger.info(f"{station_id}: {args.disable_mount} → {conn}")
        else:
            conn, state = args.ntrip_stream[0], args.ntrip_stream[1]

        drop_streams = sbf_streams_for_conn(config_text, conn) if drop_sbf else []
        try:
            cmds = build_ntrip_stream_commands(
                conn, state, drop_sbf_streams=drop_streams
            )
        except ValueError as exc:
            logger.error(f"{station_id}: {exc}")
            overall = 1
            continue

        tf = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix="ntrip_", delete=False
        )
        tf.write(
            f"# rec-config ntrip {conn}={state} "
            f"(mount={args.disable_mount or '-'}, drop_sbf={drop_streams or '-'})\n"
        )
        tf.write("\n".join(cmds) + "\n")
        tf.close()
        args.push = tf.name
        try:
            rc = _push_configs(
                args,
                [(station_id, ip, port)],
                logger,
                tcp_username,
                tcp_password,
                rec_config_dir,
            )
            overall = overall or rc
        finally:
            _os.unlink(tf.name)

    return overall


def _extract_configs(
    args, targets, logger, tcp_username=None, tcp_password=None
) -> int:
    """Extract configurations from receivers."""
    import difflib
    import os
    from pathlib import Path

    from ..config.receivers_config import get_receivers_config
    from ..septentrio.tcp_client import PolaRX5TCPClient, save_config_to_file

    config_type = args.config_type
    diff_file = Path(args.diff_with) if args.diff_with else None
    save_to_file = getattr(args, "save", False)

    # Determine output directory if saving
    output_dir = None
    if save_to_file:
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser()
        else:
            # Get from config or use default
            try:
                receivers_config = get_receivers_config()
                polarx5_config = receivers_config.get_receiver_config("polarx5")
                config_dir = polarx5_config.get("rec_config_dir")
                if config_dir:
                    output_dir = Path(os.path.expanduser(config_dir))
                else:
                    output_dir = Path("/tmp/polarconfig")
            except Exception:
                output_dir = Path("/tmp/polarconfig")

        # Create directory if it doesn't exist
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Output directory: {output_dir}")

    success_count = 0
    for station_id, ip, port in targets:
        if save_to_file:
            print(f"\n{'=' * 50}", file=sys.stderr)
            print(f"Extracting config from {station_id} ({ip}:{port})", file=sys.stderr)
            print(f"{'=' * 50}", file=sys.stderr)

        if args.dry_run:
            if save_to_file:
                print(
                    f"  [DRY RUN] Would extract {config_type} config", file=sys.stderr
                )
                print(
                    f"  [DRY RUN] Would save to: {output_dir}/PolaRx5_{station_id}_{config_type}_*.txt",
                    file=sys.stderr,
                )
            else:
                print(
                    f"# [DRY RUN] Would extract {config_type} config from {station_id}"
                )
            success_count += 1
            continue

        try:
            client = PolaRX5TCPClient(
                ip,
                station_id,
                port,
                timeout=args.timeout,
                username=tcp_username,
                password=tcp_password,
                firmware_version=_station_firmware(station_id),
            )
            config = client.extract_config(config_type)
            client.disconnect()

            if not config:
                logger.error(f"Failed to extract config from {station_id}")
                continue

            if save_to_file:
                # Save to file
                filepath = save_config_to_file(
                    config,
                    station_id,
                    config_type,
                    receiver_type="PolaRx5",
                    output_dir=output_dir,
                )
                print(f"  ✓ Saved to: {filepath}", file=sys.stderr)

                # Show diff if requested
                if diff_file and diff_file.exists():
                    old_config = diff_file.read_text().strip().split("\n")
                    new_config = config.strip().split("\n")
                    diff = list(
                        difflib.unified_diff(
                            old_config,
                            new_config,
                            fromfile=str(diff_file),
                            tofile=str(filepath),
                            lineterm="",
                        )
                    )
                    if diff:
                        print(
                            f"\n  Differences from {diff_file.name}:", file=sys.stderr
                        )
                        for line in diff[:50]:  # Limit output
                            print(f"    {line}", file=sys.stderr)
                        if len(diff) > 50:
                            print(
                                f"    ... ({len(diff) - 50} more lines)",
                                file=sys.stderr,
                            )
                    else:
                        print(
                            f"  ✓ No differences from {diff_file.name}", file=sys.stderr
                        )
            else:
                # Print to stdout (Unix convention)
                if len(targets) > 1:
                    # Add header for multiple stations
                    print(f"# Configuration for {station_id} ({config_type})")
                    print(f"# Extracted from {ip}:{port}")
                print(config)

            success_count += 1

        except Exception as e:
            logger.error(f"Error extracting from {station_id}: {e}")

    if save_to_file:
        print(f"\n{'=' * 50}", file=sys.stderr)
        print(
            f"Extracted {success_count}/{len(targets)} configurations", file=sys.stderr
        )
    return 0 if success_count == len(targets) else 1


def _station_firmware(station_id: str) -> Optional[str]:
    """Firmware version from stations.cfg, for firmware-aware receiver comms.

    Every TCP client we open should be told this — fw 5.7.0 enforces auth while
    earlier versions do not, and guessing wrong in either direction is harmful:
    skipping auth on a 5.7.0 box means silently rejected commands reported as
    success, while sending a doomed login to a pre-5.7 box feeds the
    brute-force counter that becomes a ~9h global lockout the moment that
    station is upgraded (the ISAK case). ``None`` (unknown) makes
    :mod:`receivers.septentrio.fw_policy` assume auth is required.
    """
    try:
        cfg = get_station_config(station_id, silent=True)
    except Exception:  # noqa: BLE001 - never let cfg lookup break a push
        return None
    if not cfg:
        return None
    val = cfg.get("receiver_firmware_version")
    if val is None:
        val = (cfg.get("receiver") or {}).get("firmware_version")
    text = str(val).strip() if val is not None else ""
    return text or None


def _push_configs(
    args, targets, logger, tcp_username=None, tcp_password=None, rec_config_dir=None
) -> int:
    """Push configuration to receivers."""
    import datetime
    from pathlib import Path

    from ..septentrio.tcp_client import PolaRX5TCPClient, load_config_from_file

    config_path = Path(args.push)
    try:
        commands = load_config_from_file(config_path)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1
    config_text = config_path.read_text()

    logger.info(f"Loaded {len(commands)} commands from {config_path.name}")
    if not args.no_save:
        logger.info("Will save to Boot config after applying")

    # Resolve station_config archive directory (rec_config_dir points to station_config/)
    archive_dir: Optional[Path] = None
    if rec_config_dir:
        archive_dir = Path(rec_config_dir).expanduser().resolve()
        if not archive_dir.exists():
            logger.warning(
                f"rec_config_dir not found: {archive_dir} — skipping auto-archive"
            )
            archive_dir = None

    # Dry run - show commands and exit
    if args.dry_run:
        print("\n--- DRY RUN - Commands to send ---")
        for cmd in commands:
            if cmd.strip() and not cmd.strip().startswith("#"):
                print(f"  {cmd}")
        print("--- End of commands ---\n")
        if archive_dir:
            print(
                f"  [DRY RUN] Would archive pushed config to {archive_dir}/STATION_SERIAL_Current_YYYYMMDD.txt"
            )
        print("Dry run complete. Use without --dry-run to execute.")
        return 0

    success_count = 0
    for station_id, ip, port in targets:
        print(f"\n{'=' * 50}")
        print(f"Pushing config to {station_id} ({ip}:{port})")
        print(f"{'=' * 50}")

        try:
            client = PolaRX5TCPClient(
                ip,
                station_id,
                port,
                timeout=args.timeout,
                username=tcp_username,
                password=tcp_password,
                firmware_version=_station_firmware(station_id),
            )
            success, errors = client.push_config(
                commands, save_to_boot=not args.no_save
            )

            # A rejected login on a fw ≥5.7.0 receiver means every command that
            # followed was refused. Reporting "applied successfully" there is a
            # lie the operator acts on, so fail loudly instead.
            if client.auth_failed:
                success = False
                errors = list(errors or []) + [
                    "authentication required by firmware but rejected — "
                    "no commands were applied"
                ]

            # Query serial number before disconnecting
            serial = None
            if success and archive_dir:
                try:
                    sbf = client.request_sbf_block("ReceiverSetup", 5902)
                    if sbf and len(sbf) >= 176:
                        raw = sbf[156:176]
                        serial = (
                            raw.split(b"\x00")[0]
                            .decode("ascii", errors="ignore")
                            .strip()
                            or None
                        )
                except Exception:
                    pass

            client.disconnect()

            if success:
                print("  ✓ Configuration applied successfully")
                if not args.no_save:
                    print("  ✓ Saved to Boot config")
                success_count += 1

                # Auto-archive pushed config to station_config/
                if archive_dir:
                    date_str = datetime.date.today().strftime("%Y%m%d")
                    serial_part = serial or "UNKNOWN"
                    dest = (
                        archive_dir
                        / f"{station_id}_{serial_part}_Current_{date_str}.txt"
                    )
                    dest.write_text(config_text)
                    print(f"  ✓ Config archived → {dest.name}")
            else:
                print("  ✗ Errors occurred:")
                for err in errors:
                    print(f"    - {err}")

        except Exception as e:
            logger.error(f"  Error pushing to {station_id}: {e}")

    print(f"\n{'=' * 50}")
    print(f"Successfully configured {success_count}/{len(targets)} receivers")
    return 0 if success_count == len(targets) else 1


# Sessions we know how to enable from the receivers package (canonical templates
# shipped at src/receivers/data/sessions/<name>.txt).
_ENABLABLE_SESSIONS = {"status_1hr", "15s_24hr"}


def parse_log_session_state(response: str, session_name: str) -> str:
    """Return enabled state of a named logging session in a getLogSession response.

    Recognises the Septentrio `LogSession, LOGn, <State>, DSKn, "<name>", ...`
    format (single or double quotes around name). Comparison is case-insensitive
    on the session name.

    Returns:
        "enabled", "disabled", "unused", or "missing" (not present in any slot).
    """
    target = session_name.strip().lower()
    state_for_target: Optional[str] = None
    for raw in response.split("\n"):
        line = raw.strip()
        if "LogSession" not in line:
            continue
        if line.startswith("$R:") and "getLogSession" in line:
            continue
        name_match = re.search(r"""['"]([^'"]*)['"]""", line)
        if not name_match:
            continue
        if name_match.group(1).strip().lower() != target:
            continue
        # Tokens are comma-separated: LogSession, LOGn, <State>, DSKn, "<name>", ...
        tokens = [t.strip() for t in line.split(",")]
        if len(tokens) >= 3:
            state = tokens[2].lower()
            if state in {"enabled", "disabled", "unused"}:
                # Prefer Enabled if duplicated across slots (defensive).
                if state == "enabled":
                    return "enabled"
                state_for_target = state
    return state_for_target if state_for_target else "missing"


def _check_session(args, targets, logger, tcp_username=None, tcp_password=None) -> int:
    """Check whether a named logging session is enabled on each target."""
    from ..septentrio.tcp_client import PolaRX5TCPClient

    session_name = args.check_session.strip()
    if not session_name:
        logger.error("--check-session requires a session name (e.g. status_1hr)")
        return 2

    if args.dry_run:
        for station_id, ip, port in targets:
            print(f"{station_id}: [DRY RUN] would query getLogSession on {ip}:{port}")
        return 0

    fail_count = 0
    error_count = 0
    for station_id, ip, port in targets:
        try:
            client = PolaRX5TCPClient(
                ip,
                station_id,
                port,
                timeout=args.timeout,
                username=tcp_username,
                password=tcp_password,
                firmware_version=_station_firmware(station_id),
            )
            if not client.connect():
                print(f"{station_id}: ERROR connect-failed")
                error_count += 1
                continue
            response = client.send_command("getLogSession", wait_time=0.5)
            client.disconnect()
        except Exception as e:
            print(f"{station_id}: ERROR {e}")
            error_count += 1
            continue

        state = parse_log_session_state(response, session_name)
        print(f"{station_id}: {session_name}={state}")
        if state != "enabled":
            fail_count += 1

    # Exit 0 only when every target is enabled. Otherwise non-zero so the
    # invocation is usable in shell pipelines (`|| echo missing`).
    if error_count:
        return 2
    return 0 if fail_count == 0 else 1


def _load_session_template(session_name: str) -> List[str]:
    """Load the canonical command list for a session from package data."""
    from importlib import resources

    try:
        text = (
            resources.files("receivers.data.sessions")
            .joinpath(f"{session_name}.txt")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as e:
        raise FileNotFoundError(
            f"No canonical template for session '{session_name}' "
            f"(expected src/receivers/data/sessions/{session_name}.txt): {e}"
        ) from e

    commands: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        commands.append(stripped)
    return commands


def _enable_session(
    args,
    targets,
    logger,
    tcp_username=None,
    tcp_password=None,
    rec_config_dir=None,
    session_name: Optional[str] = None,
    force: bool = False,
) -> int:
    """Push canonical session template to each target.

    When ``force=False`` (default — used by ``--enable-session``) the per-station
    pre-check skips targets where the session is already enabled, making bulk
    rollouts idempotent.

    When ``force=True`` (used by ``--update-session``) the pre-check still runs
    for reporting purposes but the template is pushed regardless — useful for
    propagating template edits across the fleet.
    """
    import datetime
    from pathlib import Path

    from ..septentrio.tcp_client import PolaRX5TCPClient

    # session_name passed by dispatcher; legacy fall-through for direct callers.
    if session_name is None:
        session_name = getattr(args, "enable_session", "")
    session_name = (session_name or "").strip()
    mode_label = "update" if force else "enable"

    if session_name not in _ENABLABLE_SESSIONS:
        logger.error(
            f"--{mode_label}-session does not support '{session_name}'. "
            f"Supported: {sorted(_ENABLABLE_SESSIONS)}"
        )
        return 2

    try:
        commands = _load_session_template(session_name)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2

    logger.info(
        f"Loaded {len(commands)} commands from canonical {session_name} template"
    )

    # Resolve archive directory (mirrors _push_configs behaviour).
    archive_dir: Optional[Path] = None
    if rec_config_dir:
        archive_dir = Path(rec_config_dir).expanduser().resolve()
        if not archive_dir.exists():
            logger.warning(
                f"rec_config_dir not found: {archive_dir} — skipping auto-archive"
            )
            archive_dir = None

    if args.dry_run:
        print("\n--- DRY RUN - Commands that would be sent ---")
        for cmd in commands:
            print(f"  {cmd}")
        print("--- End of commands ---")
        if force:
            print(
                f"Will push template to every target — {session_name} state is "
                "ignored (--update-session forces re-push).\n"
            )
        else:
            print(
                "Will pre-check getLogSession and skip targets where "
                f"{session_name} is already enabled.\n"
            )
        for station_id, ip, port in targets:
            print(f"  [DRY RUN] {station_id} ({ip}:{port})")
        return 0

    success_count = 0
    skip_count = 0
    verb = "Updating" if force else "Enabling"
    for station_id, ip, port in targets:
        print(f"\n{'=' * 50}")
        print(f"{verb} {session_name} on {station_id} ({ip}:{port})")
        print(f"{'=' * 50}")

        try:
            client = PolaRX5TCPClient(
                ip,
                station_id,
                port,
                timeout=args.timeout,
                username=tcp_username,
                password=tcp_password,
                firmware_version=_station_firmware(station_id),
            )
            if not client.connect():
                print("  ✗ Connect failed")
                continue

            # Pre-check reports current state; with force=True we push regardless.
            response = client.send_command("getLogSession", wait_time=0.5)
            state = parse_log_session_state(response, session_name)
            if state == "enabled" and not force:
                print(f"  ↷ {session_name} already enabled — skipping")
                client.disconnect()
                skip_count += 1
                success_count += 1
                continue

            if force and state == "enabled":
                print(f"  Current state: {session_name}=enabled; forcing re-push")
            else:
                print(f"  Current state: {session_name}={state}; pushing template")
            success, errors = client.push_config(
                commands, save_to_boot=not args.no_save
            )

            serial = None
            if success and archive_dir:
                try:
                    sbf = client.request_sbf_block("ReceiverSetup", 5902)
                    if sbf and len(sbf) >= 176:
                        raw = sbf[156:176]
                        serial = (
                            raw.split(b"\x00")[0]
                            .decode("ascii", errors="ignore")
                            .strip()
                            or None
                        )
                except Exception:
                    pass

            client.disconnect()

            if success:
                print(f"  ✓ Session {'re-pushed' if force else 'enabled'}")
                if not args.no_save:
                    print("  ✓ Saved to Boot config")
                success_count += 1
                if archive_dir:
                    date_str = datetime.date.today().strftime("%Y%m%d")
                    serial_part = serial or "UNKNOWN"
                    archive_tag = "UpdateSession" if force else "AddSession"
                    dest = (
                        archive_dir
                        / f"{station_id}_{serial_part}_{archive_tag}_{session_name}_{date_str}.txt"
                    )
                    dest.write_text("\n".join(commands) + "\n")
                    print(f"  ✓ Pushed template archived → {dest.name}")
            else:
                print("  ✗ Errors occurred:")
                for err in errors:
                    print(f"    - {err}")
        except Exception as e:
            logger.error(f"  Error {mode_label}ing on {station_id}: {e}")

    print(f"\n{'=' * 50}")
    action_past = "re-pushed" if force else "enabled"
    skip_note = "" if force else f" ({skip_count} already enabled)"
    print(
        f"Successfully {action_past} {session_name} on "
        f"{success_count}/{len(targets)} receivers{skip_note}"
    )
    return 0 if success_count == len(targets) else 1


def _audit_session(args, targets, logger, tcp_username=None, tcp_password=None) -> int:
    """Compare receiver session config to canonical template; report drift.

    For each target, extracts the current `lstConfigFile` dump, parses out
    the named session's state (block list, interval, retention, priority,
    file naming), and diffs against the template shipped with the package.

    Exit code 0 only when every target matches the template. Non-zero if any
    target shows drift / missing / connect error — suitable for nightly
    compliance sweeps in cron.
    """
    from ..septentrio.session_state import diff_session_state, parse_session_state
    from ..septentrio.tcp_client import PolaRX5TCPClient

    session_name = args.audit_session.strip()
    if not session_name:
        logger.error("--audit-session requires a session name (e.g. status_1hr)")
        return 2

    try:
        template_cmds = _load_session_template(session_name)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2

    template = parse_session_state(template_cmds, session_name)
    if template is None:
        logger.error(
            f"Canonical template for '{session_name}' does not define the session"
        )
        return 2

    logger.info(
        f"Loaded canonical {session_name} template "
        f"({len(template.sbf_blocks)} SBF blocks, interval={template.interval})"
    )

    if args.dry_run:
        for station_id, ip, port in targets:
            print(
                f"{station_id}: [DRY RUN] would extract Current config from {ip}:{port}"
            )
        return 0

    drift_count = 0
    missing_count = 0
    error_count = 0
    ok_count = 0

    for station_id, ip, port in targets:
        try:
            client = PolaRX5TCPClient(
                ip,
                station_id,
                port,
                timeout=args.timeout,
                username=tcp_username,
                password=tcp_password,
                firmware_version=_station_firmware(station_id),
            )
            if not client.connect():
                print(f"{station_id}: ERROR connect-failed")
                error_count += 1
                continue
            config_text = client.extract_config("Current")
            client.disconnect()
        except Exception as e:
            print(f"{station_id}: ERROR {e}")
            error_count += 1
            continue

        receiver_state = parse_session_state(config_text.splitlines(), session_name)
        if receiver_state is None:
            print(f"{station_id}: MISSING — no LOG slot has session '{session_name}'")
            missing_count += 1
            continue

        diffs = diff_session_state(receiver_state, template)
        if not diffs:
            print(
                f"{station_id}: OK ({receiver_state.log_slot}/"
                f"{receiver_state.stream_slot or '?'})"
            )
            ok_count += 1
        else:
            print(
                f"{station_id}: DRIFT ({receiver_state.log_slot}/"
                f"{receiver_state.stream_slot or '?'})"
            )
            for d in diffs:
                print(f"  - {d}")
            drift_count += 1

    print(f"\n{'=' * 50}")
    print(
        f"Audit summary: ok={ok_count} drift={drift_count} "
        f"missing={missing_count} error={error_count} (of {len(targets)})"
    )
    if error_count:
        return 2
    return 0 if (drift_count == 0 and missing_count == 0) else 1


def cmd_rec_provision(args) -> int:
    """Provision a Septentrio PolaRx5 receiver for fw 5.7.0."""
    import re
    import socket
    import ssl
    import struct
    import time
    from pathlib import Path

    logger = setup_logging(args.loglevel)

    from ..config.receivers_config import get_receivers_config
    from ..septentrio.tcp_client import DEFAULT_CONTROL_PORT, load_config_from_file

    receivers_config = get_receivers_config()
    polarx5_config = receivers_config.get_receiver_config("polarx5")

    tcp_username = polarx5_config.get("tcp_username") or None
    tcp_password = polarx5_config.get("tcp_password") or None
    factory_username = polarx5_config.get("factory_username") or "RxAdmin"
    factory_password = polarx5_config.get("factory_password") or "S3pt3ntr10"
    admin_username = polarx5_config.get("admin_username") or "admin"
    admin_password = polarx5_config.get("admin_password") or None
    default_port = args.port or int(
        polarx5_config.get("control_port", DEFAULT_CONTROL_PORT)
    )

    set_ip = getattr(args, "set_ip", None)
    gateway = (
        getattr(args, "gateway", None)
        or polarx5_config.get("desk_gateway")
        or "192.168.100.1"
    )
    netmask = (
        getattr(args, "netmask", None)
        or polarx5_config.get("desk_netmask")
        or "255.255.255.0"
    )
    dns1 = getattr(args, "dns1", None) or polarx5_config.get("desk_dns1") or ""
    dns2 = getattr(args, "dns2", None) or polarx5_config.get("desk_dns2") or ""
    apply_config = getattr(args, "apply_config", None)

    if getattr(args, "bootstrap", False):
        if not getattr(args, "host", None):
            logger.error(
                "--bootstrap requires --host (bench/desk use only — do not use on deployed stations)"
            )
            return 1
        if not set_ip:
            set_ip = polarx5_config.get("desk_bootstrap_ip") or "192.168.100.60"
        if not dns1:
            dns1 = polarx5_config.get("desk_dns1") or ""
        if not dns2:
            dns2 = polarx5_config.get("desk_dns2") or ""

    if not tcp_username or not tcp_password:
        logger.error(
            "tcp_username and tcp_password must be set in receivers.cfg [polarx5]"
        )
        return 1

    # Load SSH public key body if requested
    ssh_key_body: Optional[str] = None
    if not args.skip_ssh_key:
        key_path_str = polarx5_config.get("tcp_ssh_key_path") or None
        if key_path_str:
            pub_path = Path(key_path_str).expanduser().with_suffix(".pub")
            if not pub_path.exists():
                pub_path = Path(str(Path(key_path_str).expanduser()) + ".pub")
            if pub_path.exists():
                parts = pub_path.read_text().strip().split()
                if len(parts) >= 2:
                    ssh_key_body = parts[1]
                    logger.debug(f"SSH key loaded from {pub_path}")
            else:
                logger.warning(
                    f"SSH public key not found at {pub_path} — skipping key push"
                )

    # Build target list
    targets = []
    if getattr(args, "host", None):
        # Direct connection: bypass stations.cfg, use native receiver port
        if len(args.stations) > 1:
            logger.error("--host requires exactly one station label")
            return 1
        station_id = args.stations[0].strip().upper()
        port = int(args.port or default_port)
        targets.append((station_id, args.host, port))
    else:
        for station_id in [s.strip().upper() for s in args.stations]:
            station_config = get_station_config(station_id)
            if station_config is None:
                logger.warning(f"Station {station_id} not found — SKIPPING")
                continue
            ip = station_config.get("ip") or station_config.get("router", {}).get("ip")
            if not ip:
                logger.warning(f"Station {station_id} has no IP configured — SKIPPING")
                continue
            port = int(
                station_config.get("receiver", {}).get("controlport") or default_port
            )
            targets.append((station_id, ip, port))

    if not targets:
        logger.error("No valid targets found")
        return 1

    def _recv_until_prompt(sock: socket.socket, timeout: float = 5.0) -> str:
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                sock.settimeout(1.0)
                chunk = sock.recv(4096)
                if chunk:
                    buf += chunk
                    decoded = buf.decode("utf-8", errors="ignore")
                    if re.search(r"IP\d+>", decoded[-30:]):
                        break
            except (TimeoutError, OSError):
                if buf:
                    break
        return buf.decode("utf-8", errors="ignore")

    def _send(sock: socket.socket, cmd: str) -> str:
        time.sleep(0.15)  # brief drain before each command (avoids mixed responses)
        sock.sendall((cmd + "\n").encode("utf-8"))
        return _recv_until_prompt(sock)

    def _check_port(host: str, chk_port: int, timeout: float = 3.0) -> bool:
        try:
            with socket.create_connection((host, chk_port), timeout=timeout):
                return True
        except OSError:
            return False

    success_count = 0
    for station_id, ip, port in targets:
        print(f"\n{'=' * 55}")
        print(f"Provisioning {station_id}  ({ip}:{port})")
        print(f"{'=' * 55}")

        if args.dry_run:
            print(f"  [DRY RUN] login, {tcp_username}, ***")
            print(
                f"  [DRY RUN] factory bootstrap if needed (factory_username={factory_username})"
            )
            if admin_password:
                print(f"  [DRY RUN] sual, User2, {admin_username}, ***, User")
            if set_ip:
                if dns1 or dns2:
                    print(
                        f"  [DRY RUN] setIPSettings, Static, '{set_ip}', '{netmask}', '{gateway}', '', '{dns1}', '{dns2}'"
                    )
                else:
                    print(
                        f"  [DRY RUN] setIPSettings, Static, '{set_ip}', '{netmask}', '{gateway}'"
                    )
            if ssh_key_body:
                print(f"  [DRY RUN] sual, User1, {tcp_username}, ***, User, <ssh-key>")
            print("  [DRY RUN] sis, all, FTP")
            print("  [DRY RUN] shs, HTTP")
            print("  [DRY RUN] eccf, Current, Boot")
            if apply_config:
                try:
                    cmds = load_config_from_file(Path(apply_config))
                    print(
                        f"  [DRY RUN] Apply config {Path(apply_config).name} ({len(cmds)} commands)"
                    )
                except FileNotFoundError:
                    print(f"  [DRY RUN] Apply config — FILE NOT FOUND: {apply_config}")
            print("  [DRY RUN] Read serial number (esoc ReceiverSetup)")
            if apply_config and polarx5_config.get("rec_config_dir"):
                print(
                    f"  [DRY RUN] Archive config → {polarx5_config.get('rec_config_dir')}/{station_id}_SERIAL_Current_YYYYMMDD.txt"
                )
            success_count += 1
            continue

        try:
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(args.timeout)
            using_tls = False
            try:
                raw.connect((ip, port))
                sock = raw
            except ConnectionRefusedError:
                # Receiver may be in TLS-only mode (e.g., after firmware upgrade resets sis).
                # Try the TLS port (port - 1: 28784 → 28783).
                tls_port = port - 1
                raw.close()
                raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                raw.settimeout(args.timeout)
                raw.connect((ip, tls_port))
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(raw)
                using_tls = True
                print(
                    f"  ⚠  Port {port} refused — receiver is in sis=secure mode "
                    f"(firmware upgrade resets IP services)"
                )
                print(
                    f"     Connecting via TLS port {tls_port} and restoring services…"
                )

            # Read initial prompt
            prompt = sock.recv(1024).decode("utf-8", errors="ignore")
            logger.debug(f"Initial prompt: {prompt!r} (tls={using_tls})")

            bootstrapped = False
            conn_id = "IP10"

            if "$E" in prompt or not prompt.strip():
                print("  ⚠  Unexpected initial response — aborting")
                sock.close()
                continue

            # Step 1: try standard login
            resp = _send(sock, f"login, {tcp_username}, {tcp_password}")
            if "$R! LogIn" in resp:
                print(f"  ✓ Logged in as {tcp_username} (accounts already exist)")
            elif "$E: Invalid command" in resp:
                print("  ✓ fw≤5.5.0 — no auth required, proceeding")
            else:
                # Standard login failed — try factory bootstrap (creates User1 and logs in)
                resp = _send(
                    sock,
                    f"login, {tcp_username}, {tcp_password}, {factory_username}, {factory_password}",
                )
                if "$R! LogIn" in resp:
                    print(
                        f"  ✓ Factory bootstrap succeeded — {tcp_username} created as User1"
                    )
                    bootstrapped = True
                else:
                    # Last resort: fw 5.6.0 with no user accounts returns
                    # "$R? LogIn: Wrong username or password!" for both 2-
                    # and 4-arg login forms, but the receiver still accepts
                    # unauthenticated commands. Probe with a benign read-only
                    # call; if it works, bootstrap normally.
                    probe = _send(sock, "getReceiverCapabilities")
                    if "$R:" in probe or "$R!" in probe:
                        print(
                            f"  ✓ Connected (no user accounts — bootstrapping {tcp_username})"
                        )
                        _send(
                            sock,
                            f"sual, User1, {tcp_username}, {tcp_password}, User",
                        )
                        bootstrapped = True
                    else:
                        print(f"  ✗ Authentication failed: {resp[:120]!r}")
                        print("    Check credentials in receivers.cfg [polarx5]")
                        sock.close()
                        continue

            # Extract connection ID from prompt (e.g. "IP10>") for SBF requests
            for line in reversed(resp.splitlines()):
                stripped = line.strip()
                if stripped.endswith(">") and stripped[:-1]:
                    conn_id = stripped[:-1]
                    break

            # Step 2: create/update admin account (always — not just on fresh bootstrap)
            if admin_password:
                resp = _send(
                    sock, f"sual, User2, {admin_username}, {admin_password}, User"
                )
                if "$R:" in resp or "$R!" in resp:
                    action = "created" if bootstrapped else "updated"
                    print(f"  ✓ Admin account {action} (User2: {admin_username})")
                else:
                    print(f"  ⚠  Admin account: {resp[:80]!r}")
            elif bootstrapped:
                print(
                    "  ⚠  admin_password not set in receivers.cfg — skipping admin account"
                )

            # Step 3a: assign static IP (permanent — survives factoryReset, no eccf needed)
            if set_ip:
                if dns1 or dns2:
                    ip_cmd = f"setIPSettings, Static, '{set_ip}', '{netmask}', '{gateway}', '', '{dns1}', '{dns2}'"
                    dns_note = f" dns={dns1}/{dns2}"
                else:
                    ip_cmd = (
                        f"setIPSettings, Static, '{set_ip}', '{netmask}', '{gateway}'"
                    )
                    dns_note = ""
                resp = _send(sock, ip_cmd)
                if "$R:" in resp or "$R!" in resp:
                    print(
                        f"  ✓ IP set: {set_ip}/{netmask} gw {gateway}{dns_note} (permanent, takes effect on reboot)"
                    )
                else:
                    print(f"  ⚠  setIPSettings: {resp[:80]!r}")
                # Receiver suspends I/O while applying IP/DNS change. The
                # window has been observed >2s on fw 5.7.0; a fixed 1.5s
                # sleep silently drops the next 1-3 commands. Poll with a
                # benign read until the receiver answers, then continue.
                time.sleep(2.0)
                for _ in range(10):
                    probe = _send(sock, "gecf, Current")
                    if "$R:" in probe or "$R!" in probe:
                        break
                    time.sleep(0.5)

            # Step 3b: push SSH public key to gpsops account
            if ssh_key_body:
                resp = _send(
                    sock,
                    f"sual, User1, {tcp_username}, {tcp_password}, User, {ssh_key_body}",
                )
                if "$R:" in resp or "$R!" in resp:
                    print(f"  ✓ SSH public key set on {tcp_username}")
                else:
                    print(f"  ⚠  SSH key push: {resp[:80]!r}")

            # Step 4: enable FTP, keep both plaintext (28784) and TLS (28783) command ports
            resp = _send(sock, "sis, all, FTP")
            if "$R:" in resp or "$R!" in resp:
                print("  ✓ FTP enabled (both command ports kept active)")
            else:
                print(f"  ⚠  setIPServices: {resp[:80]!r}")

            # Step 6: disable HTTPS redirect (keep 8060->80 router forwards working)
            resp = _send(sock, "shs, HTTP")
            if "$R:" in resp or "$R!" in resp:
                print("  ✓ HTTPS redirect disabled (HTTP only)")
            else:
                print(f"  ⚠  setHttpsSettings: {resp[:80]!r}")

            # Step 7: save to Boot
            resp = _send(sock, "eccf, Current, Boot")
            if "$R:" in resp or "$R!" in resp:
                print("  ✓ Saved to Boot config")
            else:
                print(f"  ⚠  eccf: {resp[:80]!r}")

            # Step 7b: verify port restoration after sis (only when TLS fallback was needed)
            if using_tls:
                time.sleep(0.5)
                ok_plain = _check_port(ip, port, timeout=3.0)
                ok_ftp = _check_port(ip, 21, timeout=3.0)
                plain_icon = "✓" if ok_plain else "✗"
                ftp_icon = "✓" if ok_ftp else "✗"
                print(
                    f"  {plain_icon} Port {port} (plain) {'open' if ok_plain else 'STILL CLOSED'}"
                    f"   {ftp_icon} FTP port 21 {'open' if ok_ftp else 'closed'}"
                )
                if not ok_plain:
                    print(
                        f"  ✗ sis restoration may have failed — run: nc -zv {ip} {port}"
                    )

            # Step 8: apply receiver config file (Expert Console upload)
            if apply_config:
                config_path = Path(apply_config)
                try:
                    cmds = load_config_from_file(config_path)
                    print(f"  → Applying {config_path.name} ({len(cmds)} commands)…")
                    errors = 0
                    for cmd in cmds:
                        resp = _send(sock, cmd)
                        if "$E" in resp:
                            print(f"  ⚠  {cmd[:50]} → {resp.strip()[:60]}")
                            errors += 1
                    if errors == 0:
                        print(f"  ✓ Config applied ({len(cmds)} commands)")
                    else:
                        print(f"  ⚠  Config applied with {errors} command errors")
                except FileNotFoundError:
                    print(f"  ✗ Config file not found: {apply_config}")

            # Step 9: read serial number via SBF ReceiverSetup block (5902)
            serial: Optional[str] = None
            try:
                sock.sendall(f"esoc, {conn_id}, ReceiverSetup\n".encode())
                sbf_buf = b""
                sbf_end = time.time() + 3.0
                sock.settimeout(0.5)
                while time.time() < sbf_end:
                    try:
                        chunk = sock.recv(8192)
                        if chunk:
                            sbf_buf += chunk
                            idx = sbf_buf.find(b"\x24\x40")
                            if idx >= 0 and len(sbf_buf) - idx >= 180:
                                break
                    except TimeoutError:
                        if sbf_buf:
                            break
                idx = sbf_buf.find(b"\x24\x40")
                if idx >= 0 and len(sbf_buf) - idx >= 176:
                    data = sbf_buf[idx:]
                    block_id = struct.unpack_from("<H", data, 4)[0] & 0x1FFF
                    if block_id == 5902:
                        raw = data[156:176]
                        s = (
                            raw.split(b"\x00")[0]
                            .decode("ascii", errors="ignore")
                            .strip()
                        )
                        serial = s or None
            except Exception:
                pass
            if serial:
                print(f"  Serial number: {serial}")
            else:
                print("  (serial number: could not read)")

            # Step 10: extract current config from receiver and save to station archive
            rec_config_dir_str = polarx5_config.get("rec_config_dir") or None
            if rec_config_dir_str:
                import datetime as _dt

                from ..septentrio.tcp_client import PolaRX5TCPClient

                archive_dir = Path(rec_config_dir_str).expanduser().resolve()
                archive_dir.mkdir(parents=True, exist_ok=True)
                try:
                    client = PolaRX5TCPClient(
                        ip,
                        station_id,
                        port,
                        timeout=args.timeout,
                        username=tcp_username,
                        password=tcp_password,
                        firmware_version=_station_firmware(station_id),
                    )
                    config = client.extract_config("Current")
                    client.disconnect()
                    if config and len(config.splitlines()) > 5:
                        date_str = _dt.date.today().strftime("%Y%m%d")
                        dest = (
                            archive_dir / f"{station_id}_PolaRx5_Current_{date_str}.txt"
                        )
                        dest.write_text(config)
                        print(f"  ✓ Config saved → {dest.name}")
                    else:
                        print(
                            "  ⚠  Config extraction returned too few lines — not saved"
                        )
                except Exception as e:
                    print(f"  ⚠  Config save failed: {e}")

            sock.close()
            print(f"  → {station_id} provisioned successfully")
            success_count += 1

        except ConnectionRefusedError:
            print(f"  ✗ Connection refused to {ip}:{port}")
        except TimeoutError:
            print(f"  ✗ Connection timed out to {ip}:{port}")
        except Exception as e:
            logger.error(f"  ✗ {station_id}: {e}")

    print(f"\n{'=' * 55}")
    print(f"Provisioned {success_count}/{len(targets)} receivers")
    return 0 if success_count == len(targets) else 1


def apply_receiver_type_corrections(
    validation_results: Dict[str, Dict[str, Any]],
) -> int:
    """Apply receiver type corrections to stations.cfg (EXPERIMENTAL).

    Args:
        validation_results: Results from validation

    Returns:
        Number of corrections applied
    """
    # TODO: Implement auto-correction to stations.cfg
    # This would require:
    # 1. Reading stations.cfg
    # 2. Updating receiver_type fields for mismatched stations
    # 3. Writing back to stations.cfg
    # 4. Validating the changes

    _logger.warning(
        "Auto-correction not yet implemented - use --report to get manual corrections"
    )
    return 0


def _create_rinex_converter(
    station_id: str,
    args,
    rinex_version,
    apply_hatanaka,
    compression_format,
    naming_convention,
    observation_types,
    logger: logging.Logger,
    rinex_config: dict = None,
    loglevel_override: int = None,
    receiver_type_override: str = None,
):
    """Create appropriate RINEX converter for a station.

    ``loglevel_override`` quiets the converter's internal chatter (parallel
    batch mode raises it to WARNING so N workers don't flood the console).

    ``receiver_type_override`` (``"polarx"``/``"netrs"``/``"netr9"``/``"ashtech"``)
    replaces the station-config receiver type — used by the re-rinex per-file
    dispatch, which selects the converter from each raw file's ACTUAL format
    (magic bytes) rather than the station's current configured receiver.

    Returns:
        Tuple of (converter, raw_extension) or (None, None) on failure.
    """
    _conv_loglevel = (
        loglevel_override if loglevel_override is not None else args.loglevel
    )
    from ..rinex import (
        AshtechConverter,
        LeicaConverter,
        R00Converter,
        SBFConverter,
        TrimbleConverter,
        TrimbleNativeConverter,
    )

    station_config = get_station_config(station_id)
    if station_config is None:
        logger.warning(f"Station {station_id} not found in configuration - SKIPPING")
        return None, None, None

    receiver_type = station_config.get("receiver", {}).get("type", "").lower()
    # Per-file dispatch (re-rinex): the raw file's actual format wins over the
    # station's CURRENT configured receiver (authoritative when they disagree).
    if receiver_type_override:
        receiver_type = receiver_type_override.lower()

    # Determine if native Trimble converter should be used.
    # CLI --native-trimble wins; then re-rinex mode (feeding the IMO archive)
    # defaults it on — Trimble raw (.T02/.T00) MUST take the native converter to
    # emit RINEX 3 (runpkr00+teqc can't), and it is ignored for SBF stations
    # (they route to SBFConverter below regardless); then config; else off.
    if getattr(args, "native_trimble", False):
        use_native_trimble = True
    elif _is_rerinex_mode(args):
        use_native_trimble = True
    elif rinex_config:
        use_native_trimble = rinex_config.get("use_native_trimble", False)
    else:
        use_native_trimble = False

    session_type = getattr(args, "session", None)

    # --ashtech wins outright: pre-2012 archive campaigns convert historical
    # Ashtech .atc regardless of the station's CURRENT receiver type (RHOF is
    # a PolaRX5 today; its 2008-2011 raw is a µZ-12).
    if getattr(args, "ashtech", False) or receiver_type == "ashtech":
        converter = AshtechConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            keep_intermediate=getattr(args, "keep_intermediate", False),
            loglevel=_conv_loglevel,
            session_type=session_type,
        )
        raw_extension = ".atc"

    # --r00 is the same escape hatch for the Trimble 4000SSi/4000Si era: VMEY is
    # a PolaRX5 today, but its 2008-2012 raw is .r00. Same runpkr00+teqc chain as
    # .T02; the converter pins teqc's -week from the observation date.
    elif getattr(args, "r00", False) or receiver_type == "r00":
        converter = R00Converter(
            station_id=station_id,
            rinex_version=rinex_version,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            keep_intermediate=getattr(args, "keep_intermediate", False),
            loglevel=_conv_loglevel,
            session_type=session_type,
        )
        raw_extension = ".r00*"  # match .r00 and .r00.gz

    elif getattr(args, "trimble", False) and not receiver_type_override:
        # --trimble restricts the run to Trimble raw regardless of the station's
        # CURRENT receiver type (NYLA is a PolaRX5 today; its pre-2019 raw is a
        # NetRS). This branch only handles the CALLER's probe (no per-file override
        # yet): it returns a native converter so the station isn't skipped and sets
        # the .T0[02]* discovery glob. The batch loop then re-dispatches PER FILE
        # via receiver_type_override=netrs/netr9 — which reaches the version-aware
        # branches below (NetRS→R2 default, NetR9→R3), honouring the version policy.
        # The `[02]` class + trailing `*` also match the .gz variants.
        if not TrimbleNativeConverter.is_available():
            logger.error(
                "--trimble needs the native Trimble converter (Docker trm2rinex "
                "image); run tools/trimble-native/setup.sh"
            )
            return None, None, None
        converter = TrimbleNativeConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            loglevel=_conv_loglevel,
            session_type=session_type,
        )
        raw_extension = ".T0[02]*"

    elif (
        "polarx" in receiver_type
        or "septentrio" in receiver_type
        or "mosaic" in receiver_type
    ):
        converter = SBFConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            observation_types=observation_types,
            loglevel=_conv_loglevel,
            session_type=session_type,
        )
        raw_extension = ".sbf.gz"
    elif "netr9" in receiver_type:
        if use_native_trimble:
            if not TrimbleNativeConverter.is_available():
                logger.error("Native Trimble converter not available")
                print("\n" + "=" * 60)
                print("NATIVE TRIMBLE CONVERTER - Setup Required")
                print("=" * 60)
                print("\nThe --native-trimble option requires Docker with the")
                print("trm2rinex image installed.")
                print("\nQuick setup:")
                print("  cd tools/trimble-native && ./setup.sh")
                print("\nManual setup:")
                print("  docker pull geodesyewsp/trm2rinex:cli-light")
                print(
                    "  docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light"
                )
                print(
                    "\nAlternative: Use standard conversion (without --native-trimble)"
                )
                print("  receivers rinex STATION -d 1")
                print("=" * 60 + "\n")
                return None, None, None
            converter = TrimbleNativeConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                apply_hatanaka=apply_hatanaka,
                compression_format=compression_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(
                    args, "no_header_correction", False
                ),
                loglevel=_conv_loglevel,
                session_type=session_type,
            )
        else:
            converter = TrimbleConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                apply_hatanaka=apply_hatanaka,
                compression_format=compression_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(
                    args, "no_header_correction", False
                ),
                keep_intermediate=getattr(args, "keep_intermediate", False),
                loglevel=_conv_loglevel,
                session_type=session_type,
            )
        raw_extension = ".T02*"  # Match .T02 and .T02.gz
    elif "netrs" in receiver_type:
        # NetRS L2 is codeless: RINEX 3 codes the L2 range as C2D, which GAMIT
        # cannot map to P2 (all data deleted with "no P2 range"). Pin NetRS to
        # RINEX 2.11 unless the user explicitly passed --version. Bound to
        # receiver type — same rule as the scheduler path (_create_converter).
        # Making RINEX 3 for a NetRS requires an explicit --version 3.
        if getattr(args, "rinex_version", None) is None:
            from ..rinex import NamingConvention, RinexVersion

            _vmap = {
                2: RinexVersion.RINEX_2,
                3: RinexVersion.RINEX_3,
                4: RinexVersion.RINEX_4,
            }
            rinex_version = _vmap.get(
                int((rinex_config or {}).get("netrs_rinex_version", 2)),
                RinexVersion.RINEX_2,
            )
            if rinex_version == RinexVersion.RINEX_2:
                naming_convention = NamingConvention.SHORT
            logger.info(
                f"{station_id}: NetRS pinned to RINEX {rinex_version.value} "
                "(pass --version 3 to override)"
            )
        if use_native_trimble:
            if not TrimbleNativeConverter.is_available():
                logger.error("Native Trimble converter not available")
                print("\n" + "=" * 60)
                print("NATIVE TRIMBLE CONVERTER - Setup Required")
                print("=" * 60)
                print("\nThe --native-trimble option requires Docker with the")
                print("trm2rinex image installed.")
                print("\nQuick setup:")
                print("  cd tools/trimble-native && ./setup.sh")
                print("\nManual setup:")
                print("  docker pull geodesyewsp/trm2rinex:cli-light")
                print(
                    "  docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light"
                )
                print(
                    "\nAlternative: Use standard conversion (without --native-trimble)"
                )
                print("  receivers rinex STATION -d 1")
                print("=" * 60 + "\n")
                return None, None, None
            converter = TrimbleNativeConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                apply_hatanaka=apply_hatanaka,
                compression_format=compression_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(
                    args, "no_header_correction", False
                ),
                loglevel=_conv_loglevel,
                session_type=session_type,
            )
        else:
            converter = TrimbleConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                apply_hatanaka=apply_hatanaka,
                compression_format=compression_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(
                    args, "no_header_correction", False
                ),
                keep_intermediate=getattr(args, "keep_intermediate", False),
                loglevel=_conv_loglevel,
                session_type=session_type,
            )
        raw_extension = ".T00*"  # Match .T00 and .T00.gz
    elif "g10" in receiver_type or "leica" in receiver_type:
        converter = LeicaConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            keep_intermediate=getattr(args, "keep_intermediate", False),
            loglevel=_conv_loglevel,
            session_type=session_type,
        )
        raw_extension = ".m00.gz"
    else:
        logger.warning(
            f"Unsupported receiver type '{receiver_type}' for {station_id} - SKIPPING"
        )
        return None, None, None

    # Validate tools
    if not getattr(args, "dry_run", False):
        tools = converter.validate_tools()
        missing = [t for t, avail in tools.items() if not avail]
        if missing:
            from ..tools import ToolManager

            manager = ToolManager()
            logger.error(
                f"Missing required tools for {station_id}: {', '.join(missing)}"
            )
            # Print detailed installation guide
            print(manager.get_installation_guide(missing))
            return None, None, None

    # Re-rinex feeds the long-term archive: a Hatanaka failure (usually
    # corrupt raw observation lines) must FAIL the file, not stage an
    # uncompacted .o.Z that breaks the .D.Z convention and the resume-skip.
    if _is_rerinex_mode(args):
        converter.strict_hatanaka = True

    return converter, raw_extension, station_config


def _reconvert_source_root(args, data_prepath: str) -> str:
    """Raw-source root for the convert path.

    Re-rinexing runs off the PERSISTENT archive: ``--source-dir`` (explicit) or
    ``--from-archive`` (defaults to the sync.yaml dissemination source_root). The
    normal daily convert (neither flag) keeps the ephemeral data_prepath. Gated on
    these two flags, NOT --work-dir (which carries a default that must never flip
    the daily convert into archive/staging mode).
    """
    src = getattr(args, "source_dir", None)
    if src:
        return str(src)
    if getattr(args, "from_archive", False):
        try:
            from ..dissemination import load_dissemination_config

            for t in load_dissemination_config():
                if getattr(t, "tier", None) == "dissemination":
                    s = getattr(t, "source_root", None)
                    if s:
                        return str(s)
        except Exception:  # noqa: BLE001
            pass
    return str(data_prepath)


def _is_rerinex_mode(args) -> bool:
    """True in re-rinex mode (read archive → stage → push): --source-dir or
    --from-archive. The daily convert (neither) is unaffected."""
    return bool(
        getattr(args, "source_dir", None) or getattr(args, "from_archive", False)
    )


def _reconvert_output_dir(raw_file: Path, source_root: str, args) -> Path:
    """Where the converted RINEX lands.

    ``--output-dir`` (flat) wins if set. In RE-RINEX mode (``--source-dir`` given)
    stage into ``work_dir/<YYYY>/<mon>/<SID>/<session>/rinex/`` (tree preserved
    from ``source_root``) for a later push — disk-backed ``--work-dir`` default
    ``~/tmp/rinex_fixes``, not /tmp (tmpfs). Otherwise (daily convert) the sibling
    ``rinex/`` of the raw dir, in place. Gated on --source-dir so the defaulted
    --work-dir doesn't stage the daily convert.
    """
    if getattr(args, "output_dir", None):
        return Path(args.output_dir)
    raw_parent = raw_file.parent
    in_place = (
        raw_parent.parent / "rinex"
        if raw_parent.name == "raw"
        else raw_parent / "rinex"
    )
    if not _is_rerinex_mode(args):
        return in_place  # daily convert: write in place to data_prepath
    work_dir = getattr(args, "work_dir", None)
    if not work_dir:
        return in_place
    try:
        rel = raw_parent.relative_to(source_root)  # YYYY/mon/SID/session/raw
        return Path(work_dir).expanduser() / rel.parent / "rinex"
    except ValueError:
        return in_place


def _backup_existing_rinex_for_date(
    output_dir: Path,
    obs_date: datetime,
    station_id: str,
    session: str,
    logger: logging.Logger,
    dry_run: bool = False,
) -> int:
    """Move existing RINEX in ``output_dir`` for ``obs_date`` to a sibling
    ``rinex_bak/`` (a DELETABLE backup) before a re-conversion overwrites it.

    Matches by observation datetime (date for daily sessions, date+hour for
    hourly) via the shared filename parser, so it catches the old file whatever
    its naming (RINEX2-short vs RINEX3-short). Returns the count moved.
    """
    import shutil as _sh

    from ..rinex.header_fix import _rinex_obs_datetime

    if not output_dir.is_dir():
        return 0
    hourly = "1hr" in (session or "").lower()
    bak = output_dir.parent / "rinex_bak"
    moved = 0
    for p in sorted(output_dir.iterdir()):
        if not p.is_file():
            continue
        d = _rinex_obs_datetime(p.name, station_id)
        if d is None:
            continue
        match = (d == obs_date) if hourly else (d.date() == obs_date.date())
        if not match:
            continue
        if dry_run:
            logger.info("[DRY RUN] would back up %s -> rinex_bak/", p.name)
            moved += 1
            continue
        bak.mkdir(parents=True, exist_ok=True)
        dest = bak / p.name
        i = 1
        while dest.exists():
            dest = bak / f"{p.stem}_{i}{p.suffix}"
            i += 1
        _sh.move(str(p), str(dest))
        logger.debug("backed up %s -> %s", p.name, dest)
        moved += 1
    return moved


def _staged_rinex_for_date(
    output_dir: Path,
    obs_date: datetime,
    station_id: str,
    session: str,
) -> Optional[Path]:
    """Return a COMPLETE staged RINEX in ``output_dir`` for ``obs_date``, else None.

    Used for idempotent resume: re-running the same re-rinex command skips a date
    whose output is already staged. "Complete" is cheap but sound because the
    converter applies TOS header corrections *before* compression, so the final
    compressed file only exists once conversion fully succeeded — we just confirm
    the compression magic bytes and a non-trivial size to reject a truncated
    remnant from a hard kill mid-compression. Matches obs-date via the shared
    filename parser (naming-agnostic), same as ``_backup_existing_rinex_for_date``.
    """
    from ..rinex.header_fix import _rinex_obs_datetime

    if not output_dir.is_dir():
        return None
    hourly = "1hr" in (session or "").lower()
    for p in sorted(output_dir.iterdir()):
        if not p.is_file():
            continue
        d = _rinex_obs_datetime(p.name, station_id)
        if d is None:
            continue
        match = (d == obs_date) if hourly else (d.date() == obs_date.date())
        if not match:
            continue
        try:
            if p.stat().st_size < 200:
                return None
            with open(p, "rb") as fh:
                magic = fh.read(2)
        except OSError:
            return None
        # .Z (compress) = 1f 9d, .gz = 1f 8b
        if magic in (b"\x1f\x9d", b"\x1f\x8b"):
            return p
        return None
    return None


def _cmd_del_backup(stations, args, start_time, end_time, logger) -> int:
    """Delete the rinex_bak/ backups created by --backup-old.

    Walks ``<data_prepath>/YYYY/mon/<SID>/<session>/rinex_bak/`` and removes the
    backup files in the date range (or all, with --all). Dry-run with --dry-run.
    Empty rinex_bak/ dirs are removed after.
    """
    from ..config.receivers_config import get_receivers_config
    from ..rinex.header_fix import _rinex_obs_datetime

    data_prepath = Path(get_receivers_config().get_data_prepath())
    session = args.session
    all_mode = getattr(args, "all", False)
    dry = getattr(args, "dry_run", False)
    total = 0
    for sid in stations:
        removed = 0
        for year_dir in sorted(data_prepath.glob("[12][09][0-9][0-9]")):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                bak = month_dir / sid / session / "rinex_bak"
                if not bak.is_dir():
                    continue
                for p in sorted(bak.iterdir()):
                    if not p.is_file():
                        continue
                    if not all_mode:
                        d = _rinex_obs_datetime(p.name, sid)
                        if d is None or not (start_time <= d < end_time):
                            continue
                    if dry:
                        print(f"  [DRY RUN] would delete {p}")
                    else:
                        p.unlink()
                    removed += 1
                if not dry and bak.is_dir() and not any(bak.iterdir()):
                    bak.rmdir()
        print(
            f"  {sid}: {'would delete' if dry else 'deleted'} {removed} backup file(s)"
        )
        total += removed
    print(f"del-backup: {total} file(s) {'(dry-run)' if dry else 'removed'}")
    return 0


def _receiver_type_for_raw(path: Path) -> Optional[str]:
    """Effective receiver type for a raw file, from its CONTENT (magic bytes)
    plus extension — the authority when stations.cfg / TOS disagrees.

    Returns a ``_create_rinex_converter`` ``receiver_type_override`` string, or
    None when the file isn't a recognised raw format (caller skips it). Septentrio
    magic → ``polarx``; Ashtech U/R → ``ashtech``; Trimble carries no printable
    magic, so the container extension splits NetRS (``.T00`` → ``netrs``, RINEX-2
    pin) from NetR9 (``.T02`` → ``netr9``) — the split that drives the version rule.
    """
    from ..archive.raw_format import (
        ASHTECH_R,
        ASHTECH_U,
        SBF,
        TRIMBLE,
        classify_raw,
    )

    fmt = classify_raw(path)
    if fmt == SBF:
        return "polarx"
    if fmt in (ASHTECH_U, ASHTECH_R):
        return "ashtech"
    if fmt == TRIMBLE:
        return "netr9" if ".t02" in path.name.lower() else "netrs"
    return None  # UNKNOWN — not a raw we can convert


def _rinex_convert_station_period(
    station_id: str,
    converter,
    raw_extension: str,
    start_time: datetime,
    end_time: datetime,
    args,
    logger: logging.Logger,
    durations: Optional[List[float]] = None,
    progress=None,
    only_dates=None,
    flush_fn=None,
    flush_every=0,
    build_params: Optional[dict] = None,
) -> tuple:
    """Convert raw files to RINEX for a single station and time period.

    ``durations`` (optional, caller-owned) collects per-file conversion
    seconds across calls, feeding the in-run ETA lines and the final
    timing summary.

    ``only_dates`` (optional set of datetime.date) restricts the scan to
    exactly those dates — the targeted regen path (``--dates``), typically
    fed by ``archive-audit`` and paired with ``--force``.

    ``progress`` (optional ChunkProgress handle) switches to parallel-batch
    reporting: per-file ✅/⏭️ detail goes to the file log only
    (``batch_quiet`` records), the handle feeds the periodic progress
    board, and the inline ETA lines are suppressed. Failures stay loud.

    Returns:
        Tuple of (converted, failed, skipped).
    """
    from ..config.receivers_config import get_receivers_config
    from ..rinex.converter_base import NetworkUnavailableError

    converted = 0
    failed = 0
    skipped = 0
    _validation_refusals: list = []
    # Raw files sbf2rin refused for "No relevant data available". Collected for
    # the epilog, where each is VERIFIED against its bytes before anything is
    # suggested — the message alone is not proof (see failure_class' runpkr00
    # entry, which once advised deleting 1,367 good files on a converter error).
    _empty_raw: list = []
    _log_extra = {"batch_quiet": True} if progress is not None else None

    try:
        config = get_receivers_config()
        data_prepath = config.get_data_prepath()
        source_root = _reconvert_source_root(args, data_prepath)

        # Per-file converter dispatch (re-rinex, build params supplied): pick the
        # converter from each raw file instead of the station's current configured
        # receiver — so a station swapped to another receiver still reconverts its
        # historical raw. Two flavours:
        #   * auto (no forced format): type from the raw's ACTUAL magic bytes;
        #   * --trimble: RESTRICT to Trimble raw and force the Trimble sub-type by
        #     container extension (.T00→netrs, .T02→netr9) even if the magic is
        #     ambiguous — the escape hatch for mislabelled Trimble.
        # Either way the NetRS/NetR9 branch decides the RINEX version (NetRS→R2
        # default, NetR9→R3), so --trimble now honours the version policy (pass
        # --version to override). --ashtech stays a single-converter force.
        _trimble_mode = getattr(args, "trimble", False)
        _per_file = (
            _is_rerinex_mode(args)
            and not getattr(args, "ashtech", False)
            # --r00 is a single-converter force, like --ashtech: per-file
            # re-dispatch would resolve by receiver_type and lose R00Converter.
            and not getattr(args, "r00", False)
            and build_params is not None
        )
        _conv_cache: dict = {}

        def _converter_for(raw_path: Path):
            """(converter, receiver_type) for a raw file; (None, None) to skip."""
            if not _per_file:
                return converter, None
            if _trimble_mode:
                # Trimble-only run: force the sub-type from the extension.
                rt = "netr9" if ".t02" in raw_path.name.lower() else "netrs"
            else:
                rt = _receiver_type_for_raw(raw_path)
            if rt is None:
                return None, None
            if rt not in _conv_cache:
                c, _ext, _sc = _create_rinex_converter(
                    station_id,
                    args,
                    build_params["rinex_version"],
                    build_params["apply_hatanaka"],
                    build_params["compression_format"],
                    build_params["naming_convention"],
                    build_params["observation_types"],
                    logger,
                    rinex_config=build_params.get("rinex_config"),
                    loglevel_override=(
                        logging.WARNING if progress is not None else None
                    ),
                    receiver_type_override=rt,
                )
                _conv_cache[rt] = c
            return _conv_cache[rt], rt

        current_date = start_time
        raw_files = []
        # AUTO per-file globs EVERY extension (format decided by content);
        # --trimble restricts to raw_extension (.T0[02]*); daily/forced use the
        # single configured raw_extension.
        _glob_all = _per_file and not _trimble_mode

        while current_date < end_time:
            cur_day = current_date.date()
            year = current_date.strftime("%Y")
            month = current_date.strftime("%b").lower()

            if "1hr" in args.session.lower():
                stem = f"{station_id}{current_date.strftime('%Y%m%d%H%M')}"
                filename = (
                    f"{stem}*" if _glob_all else f"{stem}*.{raw_extension.lstrip('.')}"
                )
                raw_dir = (
                    Path(source_root) / year / month / station_id / args.session / "raw"
                )
                current_date += timedelta(hours=1)
            else:
                stem = f"{station_id}{current_date.strftime('%Y%m%d')}"
                filename = f"{stem}*" if _glob_all else f"{stem}*{raw_extension}"
                raw_dir = (
                    Path(source_root) / year / month / station_id / args.session / "raw"
                )
                current_date += timedelta(days=1)

            if only_dates is not None and cur_day not in only_dates:
                continue
            if raw_dir.exists():
                matches = list(raw_dir.glob(filename))
                raw_files.extend(matches)

        if not raw_files:
            print(f"  No raw files found for {station_id}")
            logger.warning(f"No raw files found for {station_id} in date range")
            return 0, 0, 1

        if progress is not None:
            progress.set_total(len(raw_files))
            logger.info(
                f"Found {len(raw_files)} raw files to convert", extra=_log_extra
            )
        else:
            print(f"  Found {len(raw_files)} raw file(s) to convert")
            logger.info(f"Found {len(raw_files)} raw files to convert")

        if getattr(args, "dry_run", False):
            for raw_file in raw_files:
                _dc, _drt = _converter_for(Path(raw_file))
                if _dc is None:
                    _why = (
                        "unrecognised raw format"
                        if _drt is None
                        else f"no {_drt} converter available"
                    )
                    print(f"  [DRY RUN] skip {raw_file.name}: {_why}")
                    continue
                _fmt_note = f" [{_drt}]" if _drt else ""
                print(f"  [DRY RUN] Would convert: {raw_file.name}{_fmt_note}")
                if getattr(args, "backup_old", False):
                    raw_parent = Path(raw_file).parent
                    _od = (
                        raw_parent.parent / "rinex"
                        if raw_parent.name == "raw"
                        else raw_parent / "rinex"
                    )
                    try:
                        _obs = _dc._extract_date_from_filename(Path(raw_file))
                    except Exception:  # noqa: BLE001
                        _obs = None
                    if _obs is not None:
                        _backup_existing_rinex_for_date(
                            _od, _obs, station_id, args.session, logger, dry_run=True
                        )
            return 0, 0, 0

        rerinex = _is_rerinex_mode(args)
        force = getattr(args, "force", False)
        # Only treat "0 corrections" as a failure when corrections were expected;
        # --no-header-correction legitimately produces 0.
        expect_corrections = not getattr(args, "no_header_correction", False)

        for file_idx, raw_file in enumerate(raw_files):
            # Select the converter from THIS file's format (per-file dispatch),
            # else the single passed converter. An unrecognised raw is skipped
            # loudly rather than fed to the wrong converter.
            converter, _rt = _converter_for(Path(raw_file))
            if converter is None:
                if _rt is None:
                    logger.warning(
                        "skipping %s: unrecognised raw format",
                        Path(raw_file).name,
                        extra=_log_extra,
                    )
                    skipped += 1
                else:
                    # Format recognised but its converter could not be BUILT
                    # (missing tool / Docker down) — a real failure, not a silent
                    # skip, so a Docker outage can't masquerade as "nothing to do".
                    logger.error(
                        "%s: no %s converter available (missing tool/Docker?) "
                        "— not converted",
                        Path(raw_file).name,
                        _rt,
                        extra=_log_extra,
                    )
                    failed += 1
                if progress is not None:
                    progress.advance()
                continue

            output_dir = _reconvert_output_dir(Path(raw_file), source_root, args)
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                _obs = converter._extract_date_from_filename(Path(raw_file))
            except Exception:  # noqa: BLE001
                _obs = None

            # Idempotent resume: in re-rinex mode, skip a date already staged
            # (complete) from a previous run unless --force. Lets an interrupted
            # run (e.g. TOS/network drop) be finished by re-running the SAME
            # command — done work is not repeated. Skip the backup too.
            if rerinex and not force and _obs is not None:
                staged = _staged_rinex_for_date(
                    output_dir, _obs, station_id, args.session
                )
                if staged is not None:
                    if progress is None:
                        print(
                            f"  ⏭️  {Path(raw_file).name}: already staged ({staged.name})"
                        )
                    else:
                        progress.advance()
                    logger.info(
                        f"⏭️  {raw_file} already staged as {staged.name}",
                        extra=_log_extra,
                    )
                    skipped += 1
                    continue

            # Back up the existing RINEX (same obs date) before this re-conversion
            # overwrites it — deletable rinex_bak/ copy (opt-in via --backup-old).
            if getattr(args, "backup_old", False) and _obs is not None:
                _n = _backup_existing_rinex_for_date(
                    output_dir, _obs, station_id, args.session, logger
                )
                if _n:
                    print(f"  📦 backed up {_n} existing file(s) → rinex_bak/")

            result = converter.convert_file(
                raw_file,
                output_dir=output_dir,
                force=force,
            )

            # Re-rinex feeds the IMO archive: a file that converted but got NO TOS
            # header corrections is unusable (wrong marker/observer/DOMES). For a
            # T02→RINEX from raw, corrections are always needed, so 0 here means the
            # metadata step no-op'd — treat as a failure and DON'T stage it, so a
            # re-run retries this date. (Network failures already raised above.)
            if (
                rerinex
                and expect_corrections
                and result.success
                and result.header_corrections_applied == 0
            ):
                if result.rinex_file and Path(result.rinex_file).exists():
                    try:
                        Path(result.rinex_file).unlink()
                    except OSError:
                        pass
                print(
                    f"  ❌ {Path(raw_file).name}: no TOS header corrections applied "
                    "— not staged (re-run to retry)"
                )
                logger.error(
                    f"❌ {raw_file}: 0 header corrections in re-rinex mode — dropped"
                )
                failed += 1
                if progress is not None:
                    progress.advance()
                continue

            if result.success:
                dur = getattr(result, "duration_seconds", 0.0) or 0.0
                if progress is None:
                    print(
                        f"  ✅ {raw_file.name} -> {result.rinex_file.name} ({dur:.1f}s)"
                    )
                else:
                    progress.advance(dur)
                logger.info(
                    f"✅ {raw_file.name} -> {result.rinex_file.name} ({dur:.1f}s)",
                    extra=_log_extra,
                )
                if result.header_corrections_applied > 0:
                    logger.debug(
                        f"   Applied {result.header_corrections_applied} header corrections"
                    )
                converted += 1
                # --push incremental flush: every `flush_every` conversions, push
                # the files staged so far to the archive (only the not-yet-pushed
                # ones; the closure serializes + tracks them). Steady progress on a
                # long re-rinex instead of one push at the very end.
                if (
                    flush_fn is not None
                    and flush_every
                    and converted % flush_every == 0
                ):
                    flush_fn()
                if durations is not None:
                    durations.append(dur)
                    # Periodic ETA: after the first 5 conversions, then every
                    # 15. Already-staged dates ahead skip in ~ms (est. is upper).
                    # Only for real batches — in period-first mode each call
                    # sees a handful of files and the ETA would be noise. The
                    # progress board covers this in parallel mode.
                    n = len(durations)
                    # Refresh the ETA every 15 conversions (+ an early one at 5)
                    # — often enough to track, cheap enough not to run per-file,
                    # and the growing avg makes each estimate more accurate.
                    if (
                        progress is None
                        and (n == 5 or n % 15 == 0)
                        and len(raw_files) >= 15
                    ):
                        from ..utils.batch_parallel import fmt_time_left

                        avg = sum(durations) / n
                        remaining = len(raw_files) - (file_idx + 1)
                        eta_s = int(remaining * avg)
                        print(
                            f"  ⏱️  avg {avg:.1f}s/file over {n} conversions; "
                            f"{remaining} file(s) left in range · {fmt_time_left(eta_s)}",
                            flush=True,
                        )

                # Track RINEX output in file_tracking DB
                try:
                    from ..scheduling.bulk_scheduler import _track_rinex_output_files

                    output_files = [str(result.rinex_file)]
                    _track_rinex_output_files(
                        station_id, args.session, output_files, logger
                    )
                except Exception as e:
                    logger.debug(f"RINEX file tracking failed: {e}")
            elif getattr(result, "validation_category", None):
                # Raw-validation refusal: ONE compact line mid-run; the detail
                # + suggested procedure prints in the end-of-batch epilog.
                print(f"  🛡  {raw_file.name}: refused ({result.validation_category})")
                _validation_refusals.append(result)
                failed += 1
                if progress is not None:
                    progress.advance()
            else:
                # Failures stay LOUD on console in every mode.
                print(f"  ❌ {raw_file.name}: {result.message}")
                logger.error(f"❌ {raw_file.name}: {result.message}")
                if "no relevant data available" in (result.message or "").lower():
                    _empty_raw.append(raw_file)
                failed += 1
                if progress is not None:
                    progress.advance()

    except NetworkUnavailableError:
        # TOS/network down mid-run — propagate so cmd_rinex aborts the push and
        # prints the resume hint. Files already staged this run stay put; the
        # in-progress date left no compressed product (corrections precede
        # compression), so a re-run of the same command resumes cleanly.
        raise

    except Exception as e:
        logger.error(f"Error processing {station_id}: {e}")
        if args.loglevel == logging.DEBUG:
            import traceback

            traceback.print_exc()
        failed += 1

    # End-of-batch procedure for refused files (kept out of the mid-run
    # stream): what was refused, why, and what to do about each category —
    # including the READY-TO-RUN remediation command when paths allow it.
    if _validation_refusals:
        from ..rinex.converter_base import validation_epilog

        epilog = validation_epilog(_validation_refusals)
        if epilog:
            print(f"\n🛡  {epilog}")
            logger.warning(epilog)
        try:
            _root = str(_reconvert_source_root(args, data_prepath)).rstrip("/")
            rels = []
            for r in _validation_refusals:
                if r.validation_category in ("wrong-date", "wrong-format"):
                    p_str = str(r.raw_file)
                    if p_str.startswith(_root + "/"):
                        rels.append(p_str[len(_root) + 1 :])
            if rels:
                print("\n   → ready-to-run remediation (dry-run; add --yes to move):")
                print(
                    "     receivers archive-sort --check-station \\\n       --file "
                    + " \\\n              ".join(rels[:30])
                )
                if len(rels) > 30:
                    print(f"     ... and {len(rels) - 30} more — use --list FILE")
        except Exception:  # noqa: BLE001 - suggestion is best-effort
            pass

    # Raw that carries no observations at all. sbf2rin is right to refuse it and
    # no retry can help, so surface it as a data finding with a ready-to-run
    # removal — but only for files PROVEN empty against their own bytes.
    if _empty_raw:
        try:
            from ..rinex.sbf_inspect import inspect_sbf

            _root = str(_reconvert_source_root(args, data_prepath)).rstrip("/")
            proven, unproven = [], []
            for raw in _empty_raw:
                obs = inspect_sbf(raw)
                p_str = str(raw)
                rel = p_str[len(_root) + 1 :] if p_str.startswith(_root + "/") else None
                if obs.is_provably_empty and rel:
                    proven.append((rel, raw.stat().st_size, obs.epochs))
                else:
                    unproven.append((raw.name, obs.describe()))

            if proven:
                cap = max(sz for _, sz, _ in proven)
                eps = proven[0][2]
                print(
                    f"\n   ⚪ {len(proven)} raw file(s) contain NO observations — "
                    f"receiver logged epochs (e.g. {eps}) with zero satellites "
                    f"throughout, i.e. an antenna-side fault. Verified against the "
                    f"bytes, not just the converter message. These can never "
                    f"produce RINEX."
                )
                print("      receivers archive-rm --catalog-prod \\")
                print(f"        --max-size {cap} \\")
                print(
                    "        --file "
                    + " \\\n               ".join(
                        rel for rel, _, _ in sorted(proven)[:30]
                    )
                )
                if len(proven) > 30:
                    print(f"      ... and {len(proven) - 30} more")
                print(
                    f"      (dry-run as shown; add --yes to delete — only files "
                    f"≤{cap} bytes are removed, catalog pruned on both DBs)"
                )
            if unproven:
                print(
                    f"\n   ⚠️  {len(unproven)} file(s) sbf2rin called empty could NOT "
                    f"be proven empty — NOT suggested for removal:"
                )
                for name, why in unproven[:5]:
                    print(f"        {name}: {why}")
        except Exception:  # noqa: BLE001 - suggestion is best-effort
            pass

    return converted, failed, skipped


def _discover_receiver_lan_ip(router_ip: str, control_port: int) -> Optional[str]:
    """Find the receiver's LAN IP behind the router from the existing control
    DNAT (src_dport == control_port). Best-effort; returns None if unavailable."""
    try:
        from ..cfg.telemetry_probe import list_port_forwards

        for rule in list_port_forwards(router_ip):
            if str(rule.get("src_dport")) == str(control_port):
                return rule.get("dest_ip")
    except Exception as exc:  # noqa: BLE001
        _logger.debug(f"could not list port-forwards on {router_ip}: {exc}")
    return None


def cmd_rec_upgrade_firmware(args) -> int:
    """Flash PolaRX5 firmware (stream-download) + chained aftermath. Dry-run default."""
    import subprocess

    from ..cfg.device_probe import StationProbeResolveError, resolve_station_probe
    from ..config.receivers_config import get_receivers_config
    from ..septentrio import firmware_upgrade as fw
    from ..septentrio.tcp_client import DEFAULT_CONTROL_PORT

    logger = _logger
    dry_run = not args.no_dry_run
    rc = get_receivers_config()
    pc = rc.get_receiver_config("polarx5")
    tcp_user = pc.get("tcp_username") or None
    tcp_pwd = pc.get("tcp_password") or None
    if not tcp_user or not tcp_pwd:
        logger.error("tcp_username/tcp_password must be set in receivers.cfg [polarx5]")
        return 1
    default_port = args.port or int(pc.get("control_port", DEFAULT_CONTROL_PORT))

    # ---- resolve the .suf -------------------------------------------------
    if args.suf:
        suf = Path(args.suf).expanduser()
        m = re.search(r"(\d+\.\d+\.\d+)", suf.name)
        target_version = args.to or (m.group(1) if m else None)
    else:
        target_version = args.to
        fw_dir = args.firmware_dir or pc.get("firmware_dir")
        if not fw_dir:
            try:
                fw_dir = rc.config.get("paths", "firmware_dir")
            except Exception:  # noqa: BLE001
                fw_dir = None
        if not fw_dir:
            logger.error(
                "no firmware dir — pass --suf PATH or --firmware-dir DIR "
                "(or set [paths] firmware_dir in receivers.cfg)"
            )
            return 1
        fw_path = Path(fw_dir).expanduser()
        if fw_path.is_file() and fw_path.suffix == ".suf":
            # Forgiving: --firmware-dir was pointed straight at the .suf file.
            suf = fw_path
            if not target_version:
                m = re.search(r"(\d+\.\d+\.\d+)", suf.name)
                target_version = m.group(1) if m else None
        else:
            suf = (
                fw_path / target_version / "firmware" / f"PolaRx5-{target_version}.suf"
            )
    if not target_version:
        logger.error("could not determine target version — pass --to VERSION")
        return 1
    if not suf.is_file():
        logger.error(f"firmware file not found: {suf}")
        return 1
    if args.via:
        logger.warning(
            "--via stepping is not yet implemented; doing a direct upgrade to %s "
            "(.suf is a full image, so direct is the documented path)",
            target_version,
        )

    # ---- resolve targets (sid, ip, control_port) --------------------------
    targets: List[Tuple[str, str, int]] = []
    if args.host:
        if len(args.stations) > 1:
            logger.error("--host requires exactly one station label")
            return 1
        host, _, p = args.host.partition(":")
        targets.append(
            (args.stations[0].strip().upper(), host, int(p) if p else default_port)
        )
    else:
        for sid in [s.strip().upper() for s in args.stations]:
            try:
                hp = resolve_station_probe(sid)
            except StationProbeResolveError as exc:
                logger.error(f"{sid}: {exc}")
                continue
            ip, _, p = hp.partition(":")
            targets.append((sid, ip, int(p) if p else default_port))
    if not targets:
        logger.error("no valid targets")
        return 1

    suf_sha = fw.sha256_of(suf)
    suf_size = suf.stat().st_size
    print(f"\n{'=' * 60}")
    print(f"Firmware: {suf.name}  → target {target_version}")
    print(f"  path: {suf}")
    print(f"  size: {suf_size} bytes   sha256: {suf_sha[:16]}…")
    print(f"  mode: {'DRY-RUN (no flash)' if dry_run else 'LIVE FLASH'}")
    print(f"{'=' * 60}")

    overall_ok = True
    for sid, ip, port in targets:
        tls_port = args.tls_port or (port - 1)
        print(f"\n--- {sid}  ({ip}:{port}, TLS reconnect via {tls_port}) ---")

        # current firmware
        cur = None
        try:
            sock, _tls = fw.connect_control(ip, port, timeout=args.timeout)
            try:
                fw.login(sock, tcp_user, tcp_pwd)
                cur = fw.read_firmware_version(sock)
            finally:
                sock.close()
        except fw.FirmwareUpgradeError as exc:
            print(f"  ⚠  could not probe current firmware: {exc}")
        print(f"  current: {cur or 'unknown'}  →  target: {target_version}")
        if cur and cur == target_version:
            print("  ✓ already at target — skipping")
            continue

        # ---- pre-flash provisioning (the 5.7 auth cliff) --------------------
        # Below 5.7 a receiver serves unauthenticated commands even with NO user
        # accounts, so an account-less station is indistinguishable from a healthy
        # one. At 5.7 auth becomes mandatory: the same receiver now has nothing to
        # authenticate against, and the scheduler's 5-minute health poll becomes a
        # stream of failed logins. VONC was flashed at 19:20 on 2026-08-20 and was
        # globally locked out (~19 h, ALL accounts) within minutes.
        #
        # NOT a complete explanation, and don't let it become one: ISAK (2026-08-01)
        # locked out the same way with its accounts INTACT — recovery logged in
        # with the normal credentials and reported "accounts already exist". So
        # missing accounts are one sufficient cause, not the only one, and VONC's
        # own case is unproven (the lockout blocks the query that would settle it).
        # Pre-provisioning is the right precaution either way: it is a no-op when
        # the accounts already exist, and the alternative costs ~19 h.
        #
        # rec-provision creates the accounts, and it must run BEFORE the flash —
        # afterwards you are racing the scheduler for a receiver that may already
        # be locked. Gated on the TARGET crossing the cliff, so ordinary 5.6.x
        # work is untouched; re-provisioning an already-provisioned receiver is a
        # no-op for accounts (it just refreshes FTP/HTTPS).
        from ..septentrio import rxupgrade as _rxu

        needs_pre_provision = (
            _rxu.requires_auth(target_version)
            and not _rxu.requires_auth(cur)
            and not getattr(args, "no_pre_provision", False)
        )
        if needs_pre_provision:
            if dry_run:
                print(
                    f"  [DRY-RUN] would PRE-PROVISION first: {cur or 'unknown'} →"
                    f" {target_version} crosses the 5.7 auth cliff, and the"
                    " receiver may have no user accounts"
                )
            else:
                print(
                    f"  pre-provisioning ({cur or 'unknown'} → {target_version}"
                    " crosses the 5.7 auth cliff — accounts must exist BEFORE"
                    " auth becomes mandatory)"
                )
                _pre = subprocess.run(
                    [sys.executable, "-m", "receivers.cli.main", "rec-provision", sid]
                )
                if _pre.returncode != 0:
                    # Refuse rather than flash: a receiver that cannot be
                    # provisioned now is one that locks itself out afterwards,
                    # and recovery costs ~19 h of downtime.
                    print(
                        f"  ❌ pre-provision failed (rc={_pre.returncode}) — NOT"
                        " flashing. Fix the accounts first, or override with"
                        " --no-pre-provision if you know they exist."
                    )
                    overall_ok = False
                    continue
                print("  ✓ pre-provision OK")

        # Post-upgrade reconnect. The web UI sends the SAME `erst, upgrade`
        # command this verb does, and JONC reconnected with plain reconcile after
        # a web-UI flash — because a deployed station reboots into its saved boot
        # config, which keeps plaintext (FTP/TCP) open. So the reconnect tries
        # PLAINTEXT (control port) first, with TLS at tls_port as a fallback for
        # the rare sis=secure case. We can't pre-verify plaintext-persistence
        # (it's post-reboot), so this is informational — not a gate.
        tls_ok = fw.tls_lifeline_ok(ip, tls_port)
        if args.ensure_port_forward and not tls_ok:
            lan_ip = _discover_receiver_lan_ip(ip, port)
            if lan_ip:
                try:
                    from ..cfg.telemetry_probe import ensure_port_forwards

                    res = ensure_port_forwards(
                        ip,
                        lan_ip,
                        [
                            {
                                "name": f"{sid}-ctrl-tls",
                                "src_dport": str(tls_port),
                                "dest_port": str(fw.RECEIVER_TLS_PORT),
                                "proto": ["tcp"],
                            }
                        ],
                        dry_run=dry_run,
                    )
                    print(
                        f"  TLS forward {tls_port}→{lan_ip}:{fw.RECEIVER_TLS_PORT}: "
                        f"{res.get('created') or res.get('existing') or 'planned'}"
                    )
                    tls_ok = tls_ok or (not dry_run)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠  TLS port-forward ensure failed: {exc}")
            else:
                print("  ⚠  could not discover receiver LAN IP — skipping TLS forward")
        print(
            f"  reconnect: plaintext {ip}:{port} first, "
            f"TLS fallback {ip}:{tls_port} ({'handshake OK' if tls_ok else 'unverified'})"
        )
        if not tls_ok:
            print(
                "  ℹ deployed stations normally keep plaintext across the upgrade "
                "(saved boot config) — plaintext-first reconnect covers this. If a "
                "station does NOT, recover via the web UI (port 80→8060) or a 28783 "
                "forward."
            )

        if dry_run:
            if getattr(args, "flash_backend", "rxupgrade") == "rxupgrade":
                # Show the literal argv (password masked). A dry run whose output
                # cannot be compared against the real invocation is not much of a
                # check — this is the line an operator can eyeball or paste.
                from ..septentrio import rxupgrade as rxu

                try:
                    _cmd = rxu.build_command(
                        rxu.resolve_binary(getattr(args, "rxupgrade_bin", None)),
                        host=ip,
                        suf=suf,
                        port=port,
                        username=tcp_user,
                        password=tcp_pwd,
                        log_path=Path("~/.cache/gps_receivers/logs").expanduser()
                        / f"rxupgrade_{sid}_<stamp>.log",
                        restore_config=not getattr(args, "no_restore_config", False),
                        ignore_ssl_errors=getattr(args, "ignore_ssl_errors", False),
                    )
                    print(f"  [DRY-RUN] would run: {rxu.redact(_cmd)}")
                except rxu.RxUpgradeError as exc:
                    print(f"  [DRY-RUN] ⚠ vendor tool unavailable: {exc}")
                print(
                    "  [DRY-RUN] verdict would come from the transcript "
                    "('Upgrade Finished'), not the exit code"
                )
            else:
                print(
                    "  [DRY-RUN] would: exeResetReceiver,Upgrade → stream .suf → "
                    "reboot → reconnect (plaintext→TLS) → verify"
                )
            aftermath = [
                s
                for s, on in (
                    ("rec-provision", not args.no_provision),
                    ("update-device --change", not args.no_record),
                    ("reconcile (local)", not args.no_record),
                    ("vitjun", not args.no_record),
                    ("health", True),
                )
                if on
            ]
            print(f"  [DRY-RUN] would chain: {', '.join(aftermath)}")
            continue

        backend = getattr(args, "flash_backend", "rxupgrade")

        # HARD GUARD — applies to the 'tcp' core ONLY. That core
        # (exeResetReceiver,Upgrade → upgrade-mode handshake → stream) is NOT
        # hardware-proven: on OLAC it left the receiver in recovery mode needing a
        # manual reboot. The default 'rxupgrade' core is Septentrio's own tool and
        # carries no such gate — it is what flashed VMEY on 2026-08-20.
        if (
            backend == "tcp"
            and not args.host
            and not getattr(args, "allow_deployed_flash", False)
        ):
            print(
                "  ❌ flash refused: the TCP flash core is EXPERIMENTAL / not yet "
                "hardware-proven (it left OLAC in recovery mode)."
            )
            print(
                "     Use the default vendor core (drop --flash-backend tcp), or "
                "bench-validate with --host <ip>, or force with "
                "--allow-deployed-flash once the handshake is proven."
            )
            overall_ok = False
            continue

        if not args.yes:
            try:
                ans = (
                    input(f"  ⚠ FLASH {sid} {cur or '?'}→{target_version}? [y/N] ")
                    .strip()
                    .lower()
                )
            except EOFError:
                ans = "n"
            if ans not in ("y", "yes"):
                print("  skipped")
                continue

        # ---- the flash: vendor core ----
        if backend == "rxupgrade":
            import time

            from ..septentrio import rxupgrade as rxu

            log_path = (
                Path.home()
                / ".cache/gps_receivers/logs"
                / f"rxupgrade_{sid}_{time.strftime('%Y%m%d_%H%M')}.log"
            )
            try:
                res = rxu.run_upgrade(
                    station_id=sid,
                    host=ip,
                    port=port,
                    suf=suf,
                    username=tcp_user,
                    password=tcp_pwd,
                    log_path=log_path,
                    binary=getattr(args, "rxupgrade_bin", None),
                    restore_config=not getattr(args, "no_restore_config", False),
                    ignore_ssl_errors=getattr(args, "ignore_ssl_errors", False),
                    timeout_s=getattr(args, "rxupgrade_timeout", 1800),
                )
            except rxu.RxUpgradeError as exc:
                print(f"  ❌ rxupgrade could not run: {exc}")
                overall_ok = False
                continue

            if not res.flashed:
                print(
                    f"  ❌ flash failed: no 'Upgrade Finished' in the transcript "
                    f"(rc={res.returncode}); see {log_path}"
                )
                overall_ok = False
                continue

            print(f"  ✓ vendor reports 'Upgrade Finished' — log: {log_path}")
            if res.verify_step_failed:
                # EXPECTED, not a fault: the vendor's own post-flash reconnect
                # fails whenever the reboot closed the port it came in on.
                print(
                    "    ℹ the tool's post-flash reconnect errored — expected when "
                    "the reboot closes the control port; verifying independently"
                )
            try:
                newver = fw.wait_for_reboot_and_verify(
                    ip,
                    port,
                    username=tcp_user,
                    password=tcp_pwd,
                    expect_version=target_version,
                    reboot_wait_s=args.reboot_wait,
                    tls_port=tls_port,
                )
                print(f"  ✓ {sid} now on firmware {newver}")
            except fw.FirmwareUpgradeError as exc:
                # The image is in — only our confirmation failed. Say so precisely
                # rather than implying the flash did not happen.
                print(f"  ⚠ flashed, but could not confirm the running version: {exc}")
                print(
                    f"     check manually (plaintext {port}, TLS {tls_port}); "
                    "rxupgrade restores the config, so plaintext often survives"
                )

        # ---- the flash: hand-rolled TCP core ----
        else:
            try:
                sock, _tls = fw.connect_control(ip, port, timeout=args.timeout)
                fw.login(sock, tcp_user, tcp_pwd)
                print("  flashing — DO NOT interrupt power/connection…")

                def _prog(sent: int, total: int) -> None:
                    pct = (sent * 100) // total if total else 0
                    print(f"\r    {pct:3d}%  ({sent}/{total})", end="", flush=True)

                fw.stream_suf(sock, suf, progress=_prog)
                print()
                try:
                    sock.close()
                except OSError:
                    pass
                print(
                    f"  streamed; waiting up to {args.reboot_wait}s for reboot + TLS reconnect…"
                )
                newver = fw.wait_for_reboot_and_verify(
                    ip,
                    port,
                    username=tcp_user,
                    password=tcp_pwd,
                    expect_version=target_version,
                    reboot_wait_s=args.reboot_wait,
                    tls_port=tls_port,
                )
                print(f"  ✓ {sid} now on firmware {newver}")
            except fw.FirmwareUpgradeError as exc:
                print(f"  ❌ flash failed: {exc}")
                overall_ok = False
                continue

        # ---- aftermath (chained existing verbs) ----
        def _run(label: str, argv: List[str]) -> None:
            print(f"  → {label}: receivers {' '.join(argv)}")
            r = subprocess.run([sys.executable, "-m", "receivers.cli.main", *argv])
            if r.returncode != 0:
                print(f"    ⚠ {label} returned {r.returncode} — run it manually")

        if not args.no_provision:
            _run("rec-provision", ["rec-provision", sid])
        if not args.no_record:
            _run(
                "update-device (TOS)",
                [
                    "cfg",
                    "update-device",
                    "--station",
                    sid,
                    "--field",
                    "firmware_version",
                    "--change",
                    "--no-dry-run",
                ],
            )
            # Sync stations.cfg receiver_firmware_version from TOS — NOT the
            # receiver. Post-5.7 the receiver sits behind the auth cliff and the
            # TCP probe fails, but update-device just wrote the true firmware to
            # TOS, so TOS is authoritative here. --global --push commits the
            # gps-config-data repo so the sync timer propagates to rek-d01.
            _run(
                "reconcile (local cfg ← TOS)",
                [
                    "cfg",
                    "reconcile",
                    sid,
                    "--field",
                    "receiver_firmware_version",
                    "--source",
                    "tos",
                    "--yes",
                ],
            )
            _run(
                "reconcile (gps-config-data commit/push)",
                [
                    "cfg",
                    "reconcile",
                    sid,
                    "--field",
                    "receiver_firmware_version",
                    "--source",
                    "tos",
                    "--yes",
                    "--global",
                    "--push",
                ],
            )
            # A firmware flash is exactly the kind of intervention the vitjun
            # record exists for, and it was the one step this chain did NOT
            # cover — so it was the one most likely to be forgotten, precisely
            # because everything around it is automated. VONC's 5.6.0→5.7.0 on
            # 2026-08-20 went unrecorded for two days for that reason.
            _run(
                "vitjun (TOS maintenance record)",
                [
                    "cfg",
                    "visit",
                    "--station",
                    sid,
                    "--type",
                    "remote",
                    "--reason",
                    "improvements",
                    "--work",
                    f"Firmware upgrade {cur or 'unknown'} → {target_version} "
                    f"via rec-upgrade-firmware ({backend} backend); "
                    "rec-provision re-enabled FTP and disabled the HTTPS redirect",
                    "--no-dry-run",
                ],
            )
        _run("health", ["health", sid])

    return 0 if overall_ok else 1


def _push_reindex(
    args,
    files: list[str],
    *,
    root: str,
    storage_location: Optional[str],
    dest_prefix: Optional[str],
) -> None:
    """After a --fix-headers --push, refresh archive_catalog.content_sha256.

    A header rewrite changes content_sha256 (it is over decompressed content),
    so every pushed file's catalog row is now stale and the integrity verify
    would flag it. With --reindex we re-hash the pushed (staged) files and
    upsert the rows on the catalog host; without it we just point the user at
    the reindex verb instead of the old (useless-here) `archive-sync --status`.
    """
    # Naming a catalog TARGET implies the reindex: --catalog-prod (production set)
    # or --catalog-host (explicit) means "push AND reindex there", folding the
    # otherwise-forgettable post-push reindex into the same command. Bare
    # --reindex still reindexes the local dev gps_health.
    do_reindex = (
        getattr(args, "reindex", False)
        or getattr(args, "catalog_prod", False)
        or bool(getattr(args, "catalog_host", None))
    )
    if not do_reindex:
        print("   ↻ catalog sha256 for these files is now stale. Refresh by")
        print("      re-running this command with --catalog-prod (push + reindex the")
        print("      production catalog set in one go), or separately:")
        print(f"        receivers archive-reindex --dir {root} --catalog-prod")
        print(
            "      (--catalog-prod writes ALL [archive] catalog_hosts; --catalog-host "
            "overrides; plain --reindex writes the local dev gps_health only)."
        )
        return

    if not storage_location or not dest_prefix:
        print("   ⚠️  --reindex: no archive storage_location/dest resolved — skipped")
        return

    from ..archive import reindex_files_multi, resolve_catalog_hosts

    _prod = getattr(args, "catalog_prod", False)
    hosts = resolve_catalog_hosts(getattr(args, "catalog_host", None), prod=_prod)
    if _prod and not hosts:
        print(
            "   ⚠️  --catalog-prod but [archive] catalog_hosts is unset in "
            "receivers.cfg — refusing to reindex (would silently hit dev). "
            "Set catalog_hosts = rek-d01.vedur.is, pgdev.vedur.is."
        )
        return
    if hosts == [None] and not _prod:
        print(
            "   ↻ reindexing the DEFAULT gps_health (database.cfg). Add "
            "--catalog-prod to write the production catalog set instead."
        )
    try:
        results = reindex_files_multi(
            hosts,
            files,
            root=root,
            storage_location=storage_location,
            dest_prefix=dest_prefix,
        )
        for label, stats in results.items():
            if stats is None:
                print(
                    f"   ⚠️  reindex FAILED on {label} — catalogs may DIVERGE; re-run."
                )
                continue
            print(
                f"   ↻ reindexed archive_catalog on {label}: "
                f"{stats.updated} updated, {stats.inserted} inserted, "
                f"{stats.unchanged} unchanged"
                + (f", {stats.skipped} unparsable" if stats.skipped else "")
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail the push
        print(f"   ⚠️  --reindex failed: {type(exc).__name__}: {exc}")


def _push_cleanup(all_details: list[dict], *, work_dir, tos_cache) -> None:
    """After push+reindex, tidy the staging work-dir (opt-in via --cleanup).

    Deletes staged rinex/ obs (now on the archive) and TOS-confirmed
    rinex_archive/ backups; never removes rinex_org/ preservations. Best-effort
    — never fails the run.
    """
    from ..rinex.header_fix import cleanup_after_push

    try:
        stats = cleanup_after_push(all_details, work_dir=work_dir, tos_cache=tos_cache)
        print(
            f"   🧹 cleanup: removed {stats['staged_removed']} staged obs, "
            f"{stats['backups_removed']} TOS-confirmed backup(s); kept "
            f"{stats['backups_kept']} unconfirmed backup(s), "
            f"{stats['org_kept']} rinex_org preservation(s)"
        )
        for msg in stats["errors"][:5]:
            print(f"      ⚠️  {msg}")
    except Exception as exc:  # noqa: BLE001 — cleanup must never fail the push
        print(f"   ⚠️  --cleanup failed: {type(exc).__name__}: {exc}")


def _resolve_archive_target():
    """Return (rsync_dest, storage_location, dest_path) for the archive tier.

    (None, None, None) when no archive-tier target exists in sync.yaml.
    """
    try:
        from ..archive.config import load_sync_config

        for t in load_sync_config():
            if getattr(t, "tier", None) == "archive":
                dest = t.dest
                full = f"{t.user}@{t.host}:{dest}" if t.host else dest
                return full, t.name, dest
    except Exception:  # noqa: BLE001
        pass
    return None, None, None


def _flush_fixed_batch(
    batch_details: list[dict],
    *,
    args,
    work_dir,
    archive_dest: str,
    archive_name,
    archive_destpath,
    tos_cache,
) -> dict:
    """Push + reindex + cleanup ONE batch of fixed files (incremental durability).

    Called mid-run every N fixed files so an interruption loses at most this
    batch — a re-run then skips already-pushed files (their headers match TOS).
    Best-effort: a failed rsync leaves the batch un-pushed (retried on re-run),
    never raises.
    """
    import subprocess
    import tempfile
    import time

    work = Path(work_dir)
    fixed = [d["file"] for d in batch_details if d.get("fixed")]
    orgs = [d["preserved_org"] for d in batch_details if d.get("preserved_org")]

    def _rel(paths):
        out = []
        for p in paths:
            try:
                out.append(str(Path(p).relative_to(work)))
            except ValueError:
                continue
        return out

    rel_fixed = _rel(fixed)
    if not rel_fixed:
        return {"pushed": 0}
    rel_org = _rel(orgs)
    push_rel = rel_fixed + rel_org
    src = str(work).rstrip("/") + "/"
    dest = archive_dest.rstrip("/") + "/"

    listf = tempfile.NamedTemporaryFile(
        "w", suffix=".rsync-files", delete=False, encoding="utf-8"
    )
    try:
        listf.write("\n".join(push_rel) + "\n")
        listf.close()
        org_note = f" + {len(rel_org)} rinex_org" if rel_org else ""
        print(f"\n🚀 batch push: {len(rel_fixed)} fixed{org_note} → {dest}", flush=True)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [
                    "rsync",
                    "-av",
                    "--files-from",
                    listf.name,
                    "--no-owner",
                    "--no-group",
                    src,
                    dest,
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )
        except subprocess.TimeoutExpired:
            print("   ⚠️  batch rsync timed out — not durable, will retry on re-run")
            return {"pushed": 0}
        except FileNotFoundError:
            print("   ⚠️  rsync not found — cannot push")
            return {"pushed": 0}
        dt = time.monotonic() - t0
        if proc.returncode != 0:
            from ..archive.remove import clean_ssh_stderr

            print(
                f"   ⚠️  batch rsync exit {proc.returncode} ({dt:.0f}s): "
                f"{clean_ssh_stderr(proc.stderr)}"
            )
            return {"pushed": 0}
        print(f"   ✓ pushed {len(rel_fixed)} file(s) in {dt:.0f}s")
        _push_reindex(
            args,
            [str(work / r) for r in push_rel],
            root=str(work),
            storage_location=archive_name,
            dest_prefix=archive_destpath,
        )
        if getattr(args, "cleanup", False):
            _push_cleanup(batch_details, work_dir=work, tos_cache=tos_cache)
        return {"pushed": len(rel_fixed)}
    finally:
        try:
            Path(listf.name).unlink()
        except OSError:
            pass


def _parse_dates_arg(value: str):
    """Delegates to utils.time_utils.parse_dates_arg (shared with
    epos-disseminate --dates)."""
    from ..utils.time_utils import parse_dates_arg

    return parse_dates_arg(value)


def _parallel_workers(args, stations, start_time, end_time, logger) -> int:
    """Resolve ``--parallel`` into a worker count (1 = run sequentially).

    ``--parallel`` alone → load-aware auto sizing; ``--parallel N`` → N,
    clamped to the chunk count. Sequential whenever there is nothing to
    parallelize (single chunk) or the dates are missing.
    """
    if not start_time or not end_time:
        return 1
    from ..utils.batch_parallel import resolve_workers, split_year_ranges

    n_chunks = len(stations) * len(split_year_ranges(start_time, end_time))
    return resolve_workers(getattr(args, "parallel", None), n_chunks, logger)


def _parallel_workers_for_chunks(args, n_chunks: int, logger) -> int:
    """Resolve ``--parallel`` against a known chunk count (1 = sequential)."""
    from ..utils.batch_parallel import resolve_workers

    return resolve_workers(getattr(args, "parallel", None), n_chunks, logger)


def _push_reconverted(work_dir, args, logger, only_rel=None) -> Dict[str, Any]:
    """Push re-rinexed files from a --work-dir staging tree back to the archive.

    Archive-side --backup-old (move old ``rinex/FILE`` → ``rinex_bak/FILE`` via the
    rawdata SSH gateway) runs BEFORE the rsync, preserving the superseded file on
    the archive. Then rsync the staged tree to the archive tier and reindex.
    Dry-run supported. Never raises.

    Returns push stats for the run summary: ``staged`` (files in the list),
    ``pushed`` (files rsync actually transferred — files already identical on
    the archive are not re-sent, so pushed < staged is normal), ``rc`` (rsync
    exit, None if rsync never ran), ``seconds``, ``note`` (error text or
    dry-run marker).
    """
    import subprocess
    import tempfile
    import time

    stats: Dict[str, Any] = {
        "staged": 0,
        "pushed": 0,
        "rc": None,
        "seconds": 0.0,
        "note": "",
    }
    work = Path(work_dir).expanduser()
    if only_rel is not None:
        # Incremental flush: push exactly these (the files staged since the last
        # flush) so archive-side --backup-old never re-touches already-pushed
        # files. The caller tracks which rel paths have gone.
        rel = list(only_rel)
    else:
        # Only finished products. The converter stages its intermediates in the
        # SAME rinex/ directory (`ISAK…0000a.13o`, `…_gfzrnx.13o`) and deletes
        # them when the file is done, so listing everything meant rsync was
        # handed paths that vanished underneath it — `link_stat … No such file
        # or directory`, exit 24, partial transfer. With --parallel that is not
        # a rare race: other year-chunks are converting throughout the push.
        # Archive products are always compressed (.Z / .gz); intermediates never
        # are, which is the cheapest reliable discriminator.
        all_staged = [
            p for p in work.rglob("*") if p.is_file() and p.parent.name == "rinex"
        ]
        staged = [p for p in all_staged if p.suffix in (".Z", ".gz")]
        _intermediates = len(all_staged) - len(staged)
        if _intermediates:
            logger.debug(
                "push: skipping %d uncompressed intermediate(s) in staging",
                _intermediates,
            )
        rel = []
        for p in staged:
            try:
                rel.append(str(p.relative_to(work)))
            except ValueError:
                pass
    stats["staged"] = len(rel)
    if not rel:
        print("  push: no staged RINEX files to push")
        stats["note"] = "nothing staged"
        return stats

    # Chokepoint format guard: never push a .Z file whose bytes aren't LZW
    # compress (the gzip-as-.Z regression class). Refused files stay staged,
    # so a re-run after fixing them (fix_gzip_z_to_lzw.sh) picks them up.
    from ..archive.format_guard import split_bad_z

    rel, bad_z = split_bad_z(rel, logger, root=str(work))
    stats["staged"] = len(rel)
    if bad_z:
        print(
            f"  ❌ format guard: {len(bad_z)} .Z file(s) with wrong magic "
            "refused (gzip-as-.Z? fix with fix_gzip_z_to_lzw.sh) — see log"
        )
        stats["note"] = f"{len(bad_z)} file(s) refused by format guard"
        if not rel:
            return stats

    dry = getattr(args, "dry_run", False)
    full, name, destpath = _resolve_archive_target()
    if not full:
        logger.error("push: no archive-tier target in sync.yaml — cannot push")
        stats["note"] = "no archive target configured"
        return stats

    # HARD RULE: never overwrite an archived product that cannot be regenerated.
    #
    # `--fix-headers` has enforced this since 1e0d81d (2026-07-03) via
    # check_regenerable() + preserve_original_file() -> permanent rinex_org/.
    # The re-rinex path built two days later (615941c, 706dd28) shipped its own
    # `--backup-old` -> rinex_bak/ instead, which LOOKS like the same protection
    # but is unconditional and explicitly deletable. So a re-conversion of a
    # raw-less date overwrote an irreplaceable file with no preservation and no
    # refusal. The rule had been written as a step inside one verb rather than
    # as an invariant over every operation that overwrites an archived product.
    #
    # This gate restores the invariant for the push path: any file whose archive
    # counterpart is NOT regenerable is dropped from the push (fail closed —
    # skip the date, keep the run going, and surface it in the summary so it
    # cannot pass unnoticed). Permanent rinex_org/ preservation on the archive
    # side is the follow-up; refusing to overwrite is what stops data loss now.
    refused_unregenerable: List[str] = []
    # NOT for --fix-headers: that path already satisfies the invariant the other
    # way round — it calls check_regenerable() itself and copies un-regenerable
    # originals to the permanent rinex_org/ BEFORE rewriting the header
    # (header_fix.py:408-418), refusing if preservation fails. Gating it here
    # too would refuse precisely the files it has just preserved, silently
    # blocking legitimate header repairs on raw-less products. The gate is for
    # the re-conversion path, which has no preservation of its own.
    refuses_apply = bool(rel) and not getattr(args, "fix_headers", False)
    gate_applies = refuses_apply and bool(
        getattr(args, "from_archive", False) or getattr(args, "source_dir", None)
    )
    if gate_applies:
        try:
            from ..rinex.header_fix import _rinex_obs_datetime
            from ..rinex.raw_presence import check_regenerable

            src_root = Path(_reconvert_source_root(args, ""))
            kept: List[str] = []
            for r in rel:
                arch = src_root / r
                parts = r.split("/")
                sid = parts[2] if len(parts) > 4 else ""
                sess = parts[3] if len(parts) > 4 else None
                obs = _rinex_obs_datetime(arch.name, sid)
                if obs is None or not arch.exists():
                    kept.append(r)  # nothing on the archive to lose
                    continue
                try:
                    regen = check_regenerable(
                        arch, obs, station_id=sid, session_type=sess
                    )
                except Exception:  # noqa: BLE001 — never fail open on an error
                    refused_unregenerable.append(r)
                    continue
                if getattr(regen, "regenerable", False):
                    kept.append(r)
                else:
                    refused_unregenerable.append(r)
            if refused_unregenerable:
                rel = kept
                stats["refused_unregenerable"] = len(refused_unregenerable)
                print(
                    f"  🛡️  REFUSED to overwrite {len(refused_unregenerable)} "
                    f"un-regenerable archive file(s) — no raw to rebuild them from. "
                    f"Use --fix-headers (preserves to rinex_org/) or restore the raw."
                )
                for r in refused_unregenerable[:5]:
                    print(f"        {r}")
        except Exception as exc:  # noqa: BLE001
            # The gate must never silently disappear: if it cannot run, say so.
            print(f"  ⚠️  regenerability gate could not run ({exc}) — push aborted")
            stats["note"] = f"regenerability gate failed: {exc}"
            return stats

    # DEGRADATION GATE — refuse a replacement that carries LESS data than the
    # file it would overwrite.
    #
    # The regenerability gate above proves a raw file EXISTS. It never proves
    # that raw decodes, nor that the result is as complete. That gap is the one
    # thing rinex_bak was actually protecting against, and it is the silent
    # class: a raw truncated mid-day converts to a valid-looking SHORTER RINEX,
    # passes the staged-completeness check, the compression-format guard and the
    # converter's identity gate (first-obs date and position are all fine), and
    # replaces a fuller original. Nothing downstream would ever notice.
    #
    # VMEY's 7 truncated .sbf.gz and 11 bad .T02 fail outright, so they are safe
    # by accident, not by design. This makes it by design.
    #
    # Cheap test on purpose: compare DECOMPRESSED size, not epoch counts. A
    # Hatanaka .Z would need CRX2RNX to count epochs, a third decode per file on
    # both sides; decompressed size is one gunzip and is monotonic in content.
    # The tolerance absorbs header-only differences (a rewritten REC # / TYPE
    # line changes the byte count slightly); real truncation is orders larger.
    if rel and not dry:
        import subprocess as _sp

        def _decompressed_size(p: Path) -> Optional[int]:
            """Uncompressed byte count, or None when it cannot be determined."""
            try:
                out = _sp.run(["gzip", "-dc", str(p)], capture_output=True, timeout=120)
                if out.returncode == 0 and out.stdout:
                    return len(out.stdout)
            except Exception:  # noqa: BLE001 — a probe must never break a push
                return None
            return None

        _DEGRADE_TOL = 0.02  # 2 % — below this is header churn, not data loss
        refused_degraded: List[str] = []
        try:
            src_root_deg = Path(_reconvert_source_root(args, ""))
            kept_deg: List[str] = []
            for r in rel:
                arch_f = src_root_deg / r
                new_f = work / r
                if not arch_f.is_file() or not new_f.is_file():
                    kept_deg.append(r)  # nothing to lose, or nothing staged
                    continue
                old_n, new_n = _decompressed_size(arch_f), _decompressed_size(new_f)
                if old_n is None or new_n is None or old_n == 0:
                    kept_deg.append(r)  # undecidable — the other gates still apply
                    continue
                if new_n < old_n * (1 - _DEGRADE_TOL):
                    refused_degraded.append(f"{r} ({new_n} < {old_n} bytes)")
                else:
                    kept_deg.append(r)
            if refused_degraded:
                rel = kept_deg
                stats["refused_degraded"] = len(refused_degraded)
                print(
                    f"  🛡️  REFUSED to overwrite {len(refused_degraded)} archive "
                    f"file(s) with a SMALLER replacement — the new conversion "
                    f"carries less data than the file on the archive (truncated "
                    f"raw, or a converter regression). Investigate before forcing."
                )
                for r in refused_degraded[:5]:
                    print(f"        {r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  degradation gate could not run ({exc}) — push aborted")
            stats["note"] = f"degradation gate failed: {exc}"
            return stats

    # Pre-overwrite safety copy — LOCAL ONLY. Never writes rinex_bak/ to the
    # archive.
    #
    # Policy (bgo, 2026-08-19): the archive holds raw/, rinex/ and rinex_org/
    # and nothing else. rinex_org is the TERMINAL state for data that cannot be
    # regenerated — at that point the preserved copy IS the original. A
    # pre-overwrite backup is a different thing: a VERIFICATION-WINDOW artefact,
    # kept only until the regeneration is confirmed good. So it belongs beside
    # the staging tree, not in the archive (~265 GB of server-side rinex_bak had
    # accumulated on a 98%-full volume).
    #
    # COPY, not move. The old server-side path MOVED rinex/FILE → rinex_bak/FILE
    # BEFORE the rsync, so a failed rsync left the archive's rinex/ empty with
    # the only copy sitting in rinex_bak — a real window where a bak cleanup
    # would have destroyed the last copy. Copying leaves the live file in place
    # until rsync replaces it, so that window does not exist.
    if getattr(args, "backup_old", False) and rel:
        import shutil as _shutil

        backup_root = work.parent / f"{work.name}_prev"
        copied = missing = failed = 0
        try:
            src_root_bak = Path(_reconvert_source_root(args, ""))
            for r in rel:
                a = src_root_bak / r
                if not a.is_file():
                    missing += 1
                    continue
                if dry:
                    copied += 1
                    continue
                d = backup_root / r
                try:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(str(a), str(d))
                    copied += 1
                except OSError:
                    failed += 1
            extra = f"  (absent={missing})" if missing else ""
            extra += f"  ⚠️ FAILED={failed}" if failed else ""
            print(
                f"  📦 local pre-overwrite copy: {copied} file(s) "
                f"{'(dry-run)' if dry else f'→ {backup_root}'}{extra}"
            )
            if failed:
                # A backup we asked for and did not get is not a detail.
                print(
                    "  ⚠️  some pre-overwrite copies FAILED — the archive files "
                    "they cover will be overwritten with no local fallback"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  local pre-overwrite copy failed: {exc}")

    dest = full.rstrip("/") + "/"
    src = str(work).rstrip("/") + "/"
    if dry:
        print(f"  [DRY RUN] would rsync {len(rel)} file(s) → {dest}")
        stats["note"] = "dry-run"
        return stats

    listf = tempfile.NamedTemporaryFile(
        "w", suffix=".rsync-files", delete=False, encoding="utf-8"
    )
    try:
        listf.write("\n".join(rel) + "\n")
        listf.close()
        print(f"  🚀 push: {len(rel)} file(s) → {dest}", flush=True)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [
                    "rsync",
                    "-av",
                    "--files-from",
                    listf.name,
                    "--no-owner",
                    "--no-group",
                    src,
                    dest,
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            print(f"  ⚠️  rsync failed: {exc}")
            stats["note"] = f"rsync failed: {exc}"
            return stats
        stats["seconds"] = time.monotonic() - t0
        stats["rc"] = proc.returncode
        # rsync -av lists each file it actually transferred; files already
        # identical on the archive are skipped silently. Count real transfers
        # so the summary distinguishes "pushed" from "already there".
        rel_set = set(rel)
        stats["pushed"] = sum(
            1 for ln in proc.stdout.splitlines() if ln.strip() in rel_set
        )
        if proc.returncode != 0:
            from ..archive.remove import clean_ssh_stderr

            err = clean_ssh_stderr(proc.stderr)
            stats["note"] = err
            print(
                f"  ⚠️  rsync exit {proc.returncode} "
                f"({stats['pushed']}/{len(rel)} transferred, "
                f"{stats['seconds']:.0f}s): {err}"
            )
            print(
                "     already-transferred files are durable; re-run the push "
                "to send the rest (rsync skips what is already there)"
            )
            return stats
        print(
            f"  ✓ pushed {stats['pushed']} file(s) in {stats['seconds']:.0f}s "
            f"({len(rel) - stats['pushed']} already on archive)"
        )
        _push_reindex(
            args,
            [str(work / r) for r in rel],
            root=str(work),
            storage_location=name,
            dest_prefix=destpath,
        )
    finally:
        try:
            Path(listf.name).unlink()
        except OSError:
            pass
    return stats


def cmd_rinex(args) -> int:
    """RINEX conversion command - convert raw GPS data to RINEX format."""
    logger = setup_logging(args.loglevel)
    # Re-rinex (--from-archive) stages into a dedicated ~/tmp dir rather than the
    # fix-headers default ~/tmp/rinex_fixes — only when the user didn't set
    # --work-dir explicitly (i.e. it's still the argparse default).
    if getattr(args, "from_archive", False) and (
        getattr(args, "work_dir", "") in ("~/tmp/rinex_fixes", None, "")
    ):
        args.work_dir = "~/tmp/rinex_reconvert"
    stations = [s.upper() for s in args.stations]

    # Import rinex module
    try:
        from ..rinex import (
            NamingConvention,
            RinexVersion,
            SBFConverter,
            TrimbleConverter,
        )
        from ..rinex.converter_base import CompressionFormat
    except ImportError as e:
        logger.error(f"RINEX module not available: {e}")
        return 1

    # Load RINEX defaults from config
    from ..config.receivers_config import get_receivers_config

    try:
        rinex_config = get_receivers_config().get_rinex_config()
    except FileNotFoundError:
        rinex_config = {
            "default_version": 3,
            "default_naming": "short",
            "default_hatanaka": True,
            "default_compression": "gz",
            "apply_header_corrections": True,
        }

    # Parse RINEX version (CLI overrides config)
    version_map = {
        2: RinexVersion.RINEX_2,
        3: RinexVersion.RINEX_3,
        4: RinexVersion.RINEX_4,
    }
    # Use CLI arg if explicitly set, otherwise use config default
    cli_version = getattr(args, "rinex_version", None)
    if cli_version is not None:
        rinex_version = version_map.get(cli_version, RinexVersion.RINEX_3)
    else:
        config_version = rinex_config.get("default_version", 3)
        rinex_version = version_map.get(config_version, RinexVersion.RINEX_3)

    # Parse output format → apply_hatanaka + compression_format.
    # None means "read from config" (the default) — EXCEPT in re-rinex mode, where
    # the product is pushed into the IMO archive (Hatanaka .D.Z); default to legacy
    # so the staged name matches the archive (a .gz would leave both .Z and .gz).
    # Mode-scoped on purpose: the daily/manual convert keeps its config compression.
    output_format = args.output_format
    if output_format is None and _is_rerinex_mode(args):
        output_format = "legacy"
    if output_format == "legacy":
        apply_hatanaka = True
        compression_format = CompressionFormat.Z
    elif output_format == "modern":
        apply_hatanaka = False
        compression_format = CompressionFormat.GZ
    else:
        apply_hatanaka = None  # Let converter read from config
        compression_format = None

    # Parse naming convention (CLI overrides config)
    if args.naming is not None:
        naming_str = args.naming
    else:
        naming_str = rinex_config.get("default_naming", "short")
    naming_convention = (
        NamingConvention.SHORT if naming_str == "short" else NamingConvention.LONG
    )

    # Parse observation types
    observation_types = None
    if getattr(args, "observation_types", None):
        observation_types = [t.strip() for t in args.observation_types.split(",")]

    # Parse date range. Shared with cmd_download via resolve_time_range(); the
    # keyword flags reproduce THIS verb's behaviour exactly, which differs from
    # download's in all three respects below. The divergence is pre-existing and
    # preserved on purpose — unifying it changes which files a given -s/-e pair
    # selects, and that needs its own review.
    start_time, end_time, reverse_chronological = resolve_time_range(
        session=args.session,
        start=parse_datetime(args.start) if getattr(args, "start", None) else None,
        end=parse_datetime(args.end) if getattr(args, "end", None) else None,
        days=getattr(args, "days", None),
        # rinex lowercases before matching "1hr" (download does not)
        case_insensitive_session=True,
        # rinex has no "end but no start" branch, so start stays None
        infer_start_from_end=False,
        # rinex widens an equal start/end into an inclusive range
        inclusive_same_day=True,
    )

    # --dates: targeted regen — exactly these dates, range derived from them
    # (overrides -s/-e). Pair with --force to redo dates whose staged/pushed
    # product exists but is bad; typically fed by 'receivers archive-audit'.
    only_dates = None
    if getattr(args, "dates", None):
        try:
            only_dates = _parse_dates_arg(args.dates)
        except (ValueError, OSError) as exc:
            logger.error(f"--dates: {exc}")
            return 1
        if not only_dates:
            logger.error("--dates: no dates parsed")
            return 1
        start_time = datetime.combine(min(only_dates), datetime.min.time())
        end_time = datetime.combine(
            max(only_dates) + timedelta(days=1), datetime.min.time()
        )
        print(f"Targeted dates: {len(only_dates)} date(s)")

    if not start_time:
        if getattr(args, "fix_headers", False) and getattr(args, "all", False):
            pass  # --fix-headers --all scans the entire archive, no date needed
        else:
            logger.error(
                "No date range specified. Use -s/--start, -e/--end, or -d/--days"
            )
            return 1

    # --del-backup: remove the rinex_bak/ backups (from --backup-old) once the
    # re-rinexed archive is verified. Its own dispatch — no conversion.
    if getattr(args, "del_backup", False):
        return _cmd_del_backup(stations, args, start_time, end_time, logger)

    # Print progress info (always visible, not dependent on log level).
    # Skip when --fix-headers — it prints its own header below.
    if not getattr(args, "fix_headers", False):
        print(f"RINEX conversion for {len(stations)} station(s)")
        print(
            f"Date range: {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}"
        )
        print(f"RINEX version: {rinex_version.value}, Naming: {naming_str}")
        logger.info(f"RINEX conversion for {len(stations)} stations")
        logger.info(f"Date range: {start_time} to {end_time}")
        logger.info(f"RINEX version: {rinex_version.value}, Naming: {naming_str}")

    # --fix-headers: in-place TOS header correction of archived RINEX (no
    # re-conversion). Walks the RINEX archive, not raw files, so it has its own
    # dispatch and bypasses the converter flow below.
    if getattr(args, "fix_headers", False):
        from ..rinex.header_fix import fix_headers_station

        # --correct-receiver/--correct-antenna: opt normally-flag-only hardware
        # fields into rewriting. These OVERRIDE a deliberate safety guard (the
        # header records the actual hardware at acquisition), so restrict them to
        # a single, deliberately-chosen station — a multi-station/fleet run would
        # corrupt any dual-instrument site, overwriting a correct streaming-box
        # header with the TOS daily receiver.
        _correct_hw = set()
        if getattr(args, "correct_receiver", False):
            _correct_hw.add("receiver")
        if getattr(args, "correct_antenna", False):
            _correct_hw.add("antenna")
        _correct_hw = frozenset(_correct_hw)

        # --skip-fields: keys removed from the correctable set for this run.
        # Validated against the known keys so a typo ("coordinate") fails loudly
        # instead of silently rewriting the field it was meant to protect.
        _VALID_SKIP = {
            "marker",
            "domes",
            "antenna_height",
            "coordinates",
            "observer_agency",
            "receiver",
            "antenna",
        }
        _skip_fields = frozenset(
            f.strip()
            for f in (getattr(args, "skip_fields", None) or "").split(",")
            if f.strip()
        )
        _bad_skip = _skip_fields - _VALID_SKIP
        if _bad_skip:
            logger.error(
                "--skip-fields: unknown field(s) %s. Valid keys: %s",
                ", ".join(sorted(_bad_skip)),
                ", ".join(sorted(_VALID_SKIP)),
            )
            return 2

        if _correct_hw and len(stations) != 1:
            logger.error(
                "--correct-receiver/--correct-antenna rewrite hardware headers "
                "and must target exactly ONE station (got %d: %s). Run them one "
                "station at a time — a fleet run would corrupt dual-instrument "
                "sites.",
                len(stations),
                ", ".join(stations),
            )
            return 1

        # Default source dir: read from sync.yaml's first dissemination target's
        # source_root (same field the EPOS disseminator uses). Falls back to
        # data_prepath from receivers.cfg if sync.yaml is unavailable.
        #
        # NB: on the laptop this source_root (the dissemination target,
        # /mnt_data/rawgpsdata) is the read-only NFS mount of the SAME ananas
        # store the --push writes to (rawdata:~/gpsdata is a disk on ananas). So
        # fix-headers ROUND-TRIPS the archive: read ananas via NFS → fix → push
        # ananas via the rawdata gateway. This is not a bookkeeping bug; the
        # archive target's own source_root (/mnt/data/gpsdata) is rek-d01's local
        # collection root, a rek-d01-only field this laptop path does not use.
        _source_dir = getattr(args, "source_dir", None)
        if not _source_dir:
            try:
                from ..dissemination import load_dissemination_config

                targets = load_dissemination_config()
                # Grab the first tier:dissemination target's source_root
                for t in targets:
                    if getattr(t, "tier", None) == "dissemination":
                        src = getattr(t, "source_root", None)
                        if src:
                            _source_dir = src
                            break
            except Exception:  # noqa: BLE001
                pass
        if not _source_dir:
            _source_dir = str(Path(get_receivers_config().get_data_prepath()))

        all_mode = getattr(args, "all", False)
        if all_mode:
            print(
                f"Fix-headers mode (--all): scanning ENTIRE archive for "
                f"{len(stations)} station(s), session={args.session}, "
                f"archive_old={getattr(args, 'archive_old', False)}, "
                f"dry_run={getattr(args, 'dry_run', True)}"
            )
        else:
            if not start_time:
                logger.error(
                    "No date range specified. Use -s/--start, -e/--end, -d/--days, "
                    "or --all to scan the entire archive."
                )
                return 1
            print(
                f"Fix-headers mode: rewriting discrepant TOS header fields in place "
                f"(archive_old={getattr(args, 'archive_old', False)}, "
                f"dry_run={getattr(args, 'dry_run', False)})"
            )
        total_fixed = 0
        total_skipped = 0
        total_errors = 0
        # Push/reindex/cleanup now happen INCREMENTALLY per batch inside
        # fix_headers_station (via _flush_fn below), so no run-wide accumulation
        # of staged files is needed here.
        # Shared TOS cache — 1 call per station across all files/dates.
        from ..dissemination.tos_access import TOSSesionCache

        tos_cache = TOSSesionCache()
        import time as _time

        _t_start = _time.monotonic()  # wall-clock for the run's total in the summary
        _dry_run = getattr(args, "dry_run", False)
        # Resolve the staging work-dir once (None disables staging → fix in place).
        _work_dir = (
            Path(args.work_dir).expanduser() if getattr(args, "work_dir", "") else None
        )
        # --clean: empty the accumulating work-dir before staging so it holds
        # only this run's files. Never touches the source archive or dry-runs.
        if getattr(args, "clean", False) and _work_dir is not None and not _dry_run:
            import shutil

            if _work_dir.exists():
                removed = sum(1 for _ in _work_dir.rglob("*") if _.is_file())
                shutil.rmtree(_work_dir)
                print(
                    f"🧹 --clean: emptied staging work-dir {_work_dir} ({removed} file(s))"
                )
            _work_dir.mkdir(parents=True, exist_ok=True)
        # --push setup: resolve the archive target ONCE and build the batch
        # flush closure so fixes push INCREMENTALLY (every --push-batch fixed
        # files) instead of once at the end. An interruption then loses at most
        # one batch, and a re-run of the same command resumes — already-pushed
        # files now match TOS on the archive, so they're skipped as clean.
        # Resolve early → fail fast before any fixing if push can't proceed.
        _flush_fn = None
        _flush_every = 0
        _push_dest_display = None
        if getattr(args, "push", False) and not _dry_run:
            _adest, _aname, _adestpath = _resolve_archive_target()
            if not _adest:
                logger.error(
                    "--push: no archive tier target in sync.yaml — cannot push"
                )
                return 1
            if _work_dir is None:
                logger.error("--push needs a staging --work-dir (staging is disabled)")
                return 1
            _flush_every = max(1, int(getattr(args, "push_batch", 100) or 100))
            _push_dest_display = _adest.rstrip("/") + "/"

            # Catalog connectivity preflight (todo #71a). Each batch flush
            # reindexes EVERY [archive] catalog_hosts entry; an unreachable host
            # otherwise only shows up as a per-batch "catalogs may DIVERGE"
            # warning, so a multi-hour run finishes "successfully" while the
            # catalogs silently drift (rek-d01 ↔ pgdev diverged ~12 days
            # unnoticed). Test every target UP FRONT and refuse to start a
            # production run on any failure — fail fast, before any fixing.
            _will_reindex = (
                getattr(args, "reindex", False)
                or getattr(args, "catalog_prod", False)
                or bool(getattr(args, "catalog_host", None))
            )
            if _will_reindex:
                from ..archive import (
                    preflight_catalog_hosts,
                    resolve_catalog_hosts,
                )

                _pf_prod = getattr(args, "catalog_prod", False)
                _pf_hosts = resolve_catalog_hosts(
                    getattr(args, "catalog_host", None), prod=_pf_prod
                )
                if _pf_prod and not _pf_hosts:
                    logger.error(
                        "--catalog-prod but [archive] catalog_hosts is unset in "
                        "receivers.cfg — refusing to start (reindex would "
                        "silently hit dev instead of prod)."
                    )
                    return 1
                _pf = preflight_catalog_hosts(_pf_hosts)
                _pf_bad = [h for h, err in _pf.items() if err is not None]
                if _pf_bad:
                    if getattr(args, "force", False):
                        logger.warning(
                            "catalog preflight FAILED for %s but -f/--force "
                            "given — continuing anyway; catalogs MAY DIVERGE.",
                            ", ".join(_pf_bad),
                        )
                    else:
                        logger.error(
                            "Refusing to start: %d/%d catalog host(s) "
                            "unreachable (%s). Fix connectivity/credentials "
                            "(database.cfg per-host creds; see todo #71), or "
                            "pass -f/--force to run anyway (risks divergence).",
                            len(_pf_bad),
                            len(_pf_hosts),
                            ", ".join(_pf_bad),
                        )
                        return 1
                else:
                    print(
                        "   ✓ catalog preflight: "
                        f"{len(_pf_hosts)} host(s) reachable "
                        f"({', '.join(h or 'localhost' for h in _pf_hosts)})"
                    )

            def _flush_fn(batch_details, _a=_adest, _n=_aname, _p=_adestpath):
                return _flush_fixed_batch(
                    batch_details,
                    args=args,
                    work_dir=_work_dir,
                    archive_dest=_a,
                    archive_name=_n,
                    archive_destpath=_p,
                    tos_cache=tos_cache,
                )

        # --parallel: chunk the work like the re-rinex path — (station, year)
        # chunks in a date range, per-station chunks with --all (whole-archive
        # scans can't be split by year up front). Sequential runs keep the
        # exact one-call-per-station behavior.
        from ..utils.batch_parallel import run_chunks, split_year_ranges

        _fh_start = start_time or datetime(2000, 1, 1)
        _fh_end = end_time or datetime.now()
        if getattr(args, "parallel", None) and not all_mode:
            fh_chunks = [
                (sid, ys, ye)
                for sid in stations
                for ys, ye in split_year_ranges(_fh_start, _fh_end)
            ]
        else:
            fh_chunks = [(sid, _fh_start, _fh_end) for sid in stations]
        fh_workers = _parallel_workers_for_chunks(args, len(fh_chunks), logger)

        # The incremental push flush does rsync + catalog writes — serialize it
        # across workers (concurrent gateway pushes are unreliable; see engine
        # push-path notes). TOSSesionCache is shared: a concurrent first-fetch
        # per station is a benign duplicate, after which all workers hit cache.
        if _flush_fn is not None and fh_workers > 1:
            import threading as _threading

            _flush_lock = _threading.Lock()
            _unlocked_flush = _flush_fn

            def _flush_fn(batch_details, _f=_unlocked_flush, _lk=_flush_lock):
                with _lk:
                    return _f(batch_details)

        def _fh_label(chunk):
            sid, ys, _ye = chunk
            return f"{sid}" if len(fh_chunks) == len(stations) else f"{sid} {ys.year}"

        _fh_handles: Dict[Any, Any] = {}

        def _fh_run_chunk(chunk):
            sid, ys, ye = chunk
            h = _fh_handles.get(chunk)
            if h is None:
                print(f"\nProcessing: {_fh_label(chunk)}")
            else:
                h.start()
            try:
                res = fix_headers_station(
                    sid,
                    args.session,
                    ys,
                    ye,
                    archive_old=getattr(args, "archive_old", False),
                    dry_run=_dry_run,
                    all_files=all_mode,
                    work_dir=_work_dir,
                    source_dir=Path(_source_dir) if _source_dir else None,
                    tos_cache=tos_cache,
                    flush_fn=_flush_fn,
                    flush_every=_flush_every,
                    push_dest=_push_dest_display,
                    retry_attempts=getattr(args, "retry_attempts", 2),
                    retry_backoff=getattr(args, "retry_backoff", 3.0),
                    correct_hardware=_correct_hw,
                    skip_fields=_skip_fields,
                    loglevel=args.loglevel,
                    progress=h,
                )
                if h is not None:
                    h.finish(ok=True)
                return res
            except BaseException:
                if h is not None:
                    h.finish(ok=False)
                raise

        if fh_workers > 1:
            from ..utils.batch_parallel import ProgressBoard

            print(
                f"\nParallel fix-headers: {len(fh_chunks)} chunk(s) "
                f"on {fh_workers} worker(s) — progress line every 30s"
            )
            _fh_board = ProgressBoard(interval=30)
            for _chunk in fh_chunks:
                _fh_handles[_chunk] = _fh_board.handle(_fh_label(_chunk))
            with _fh_board:
                _outcomes = run_chunks(
                    fh_chunks, _fh_run_chunk, workers=fh_workers, logger=logger
                )
        else:
            _outcomes = []
            from ..utils.batch_parallel import ChunkOutcome

            for _chunk in fh_chunks:
                oc = ChunkOutcome(_chunk)
                try:
                    oc.value = _fh_run_chunk(_chunk)
                except Exception as e:  # noqa: BLE001 — chunk isolation
                    oc.error = e
                    logger.error("fix-headers chunk %s failed: %s", _chunk, e)
                _outcomes.append(oc)

        for _oc in _outcomes:
            if not _oc.ok or _oc.value is None:
                total_errors += 1
                print(f"  {_fh_label(_oc.chunk)}: ❌ chunk failed — {_oc.error}")
                continue
            summary = _oc.value
            print(
                f"  {_fh_label(_oc.chunk)}: scanned={summary['scanned']} "
                + (
                    f"would_fix={summary.get('would_fix', 0)} clean={summary.get('clean', summary.get('skipped', 0))} "
                    if _dry_run
                    else f"fixed={summary['fixed']} skipped={summary['skipped']} "
                )
                + f"errors={summary['errors']}"
            )
            if summary["errors"]:
                # Group errors by message (printed ONCE at the end, not per file
                # during the run), and list the affected filenames so the
                # unreadable/no-TOS files are actionable for follow-up.
                _by_err: dict[str, list[str]] = {}
                for d in summary.get("details", []):
                    if d.get("error"):
                        name = Path(d.get("file", "?")).name
                        _by_err.setdefault(d["error"], []).append(name)
                print("   Errors:")
                for msg, names in sorted(_by_err.items(), key=lambda kv: -len(kv[1])):
                    print(f"      {msg}: {len(names)} file(s)")
                    shown = ", ".join(sorted(names)[:20])
                    more = f" … (+{len(names) - 20} more)" if len(names) > 20 else ""
                    print(f"         {shown}{more}")
                # Unreadable files → suggest the guarded archive-rm verb (dry-run,
                # inert on copy-paste). Show each file's size and bound the
                # suggested delete to the largest of them (--max-size), so the
                # command can never remove anything bigger than what was flagged.
                # NB: a substantial unreadable file may be corrupt-but-recoverable
                # via regenerate-from-raw (a DIFFERENT process) — hence the
                # manual-check warning.
                _unreadable: list[tuple[str, int]] = []
                for d in summary.get("details", []):
                    if not (d.get("error") or "").startswith(
                        "could not read RINEX header"
                    ):
                        continue
                    src = d.get("file", "")
                    try:
                        sz = Path(src).stat().st_size
                        rel = str(Path(src).relative_to(Path(_source_dir)))
                    except (OSError, ValueError):
                        continue
                    _unreadable.append((rel, sz))
                if _unreadable:
                    _cap = max(sz for _, sz in _unreadable)
                    print(
                        f"\n   🗑️  {len(_unreadable)} unreadable file(s) "
                        f"(size {min(s for _, s in _unreadable)}–{_cap} bytes) — "
                        "useless as RINEX. To remove from the archive:"
                    )
                    print(
                        "      ⚠️  CHECK EACH manually first — a large corrupt file may be "
                        "recoverable via regenerate-from-raw (a separate process), not deletion."
                    )
                    print("      receivers archive-rm --catalog-prod \\")
                    print(f"        --max-size {_cap} \\")
                    print(
                        "        --file "
                        + " ".join(rel for rel, _ in sorted(_unreadable))
                    )
                    print(
                        "      (dry-run as shown; add --yes to actually delete — only "
                        f"files ≤{_cap} bytes are removed, catalog pruned on both DBs)"
                    )
            total_fixed += summary.get("would_fix", summary.get("fixed", 0))
            total_skipped += summary.get("clean", summary.get("skipped", 0))
            total_errors += summary["errors"]
        _elapsed = _time.monotonic() - _t_start
        _h, _rem = divmod(int(_elapsed), 3600)
        _m, _s = divmod(_rem, 60)
        _dur = f"{_h}h {_m}m {_s}s" if _h else f"{_m}m {_s}s" if _m else f"{_s}s"
        print(
            f"\nSummary: {'would_fix' if _dry_run else '✅'} {total_fixed} "
            f"{'(dry-run)' if _dry_run else 'fixed'}, "
            f"{'clean' if _dry_run else '⏭️  skipped'} {total_skipped}, "
            f"❌ {total_errors} errors  ⏱  {_dur}"
        )

        # Pushing is incremental: fix_headers_station flushed each batch of
        # --push-batch fixed files via _flush_fn (push -> reindex -> cleanup) as
        # it went, so progress is already durable and there is nothing to push
        # here. An interrupted run RESUMES on re-invocation of the same command
        # (already-pushed files now match TOS on the archive -> skipped as clean).
        if getattr(args, "push", False) and not _dry_run:
            if total_fixed == 0:
                print("\n⏭️  --push: 0 files fixed - nothing pushed")
            else:
                print(
                    f"\n✅ --push: {total_fixed} file(s) pushed incrementally "
                    f"(batches of {_flush_every}); re-run to resume if interrupted."
                )
        return 0 if total_errors == 0 else 1

    # Track results
    total_converted = 0
    total_failed = 0
    total_skipped = 0

    # Network-first: -d with multiple stations → period→station ordering
    network_first = reverse_chronological and len(stations) > 1

    # A TOS/network drop mid-run raises NetworkUnavailableError out of the convert
    # loop; abort the whole run (don't push a partial batch) and tell the user to
    # re-run the same command to resume. Already-staged dates are skipped on resume.
    import time as _time

    from ..rinex.converter_base import NetworkUnavailableError

    run_t0 = _time.monotonic()
    durations: List[float] = []
    push_stats: Optional[Dict[str, Any]] = None
    par_workers = _parallel_workers(args, stations, start_time, end_time, logger)
    aborted = False

    # --push incremental flush (re-rinex): push to the archive every
    # `_rinex_flush_every` conversions instead of one big push at the very end.
    # The closure pushes ONLY files staged since the last flush (tracked in
    # `_pushed_rel`) so archive-side --backup-old never re-touches an already-
    # pushed file; it is serialized (concurrent gateway ssh is unreliable) and
    # accumulates into push_stats. A failed rsync leaves its files un-marked so a
    # later flush / the final flush retries them.
    import threading as _threading

    _do_flush = bool(
        getattr(args, "push", False)
        and _is_rerinex_mode(args)
        and getattr(args, "work_dir", None)
    )
    _pushed_rel: set = set()
    _flush_lock = _threading.Lock()
    _rinex_flush_every = (
        (int(getattr(args, "push_batch", 0) or 0) or 300) if _do_flush else 0
    )

    def _rinex_flush() -> None:
        nonlocal push_stats
        with _flush_lock:
            work = Path(args.work_dir).expanduser()
            # Scope to THIS run's stations. The staging tree is shared across
            # runs (~/tmp/rinex_reconvert), and _pushed_rel only lives for the
            # life of this process — so on the first flush every leftover file
            # from an earlier station's run counted as "new". A VMEY run that
            # converted 5 files pushed 6,901, ran archive-side --backup-old over
            # another station's already-archived files (needlessly creating
            # rinex_bak copies), and reindexed 786 catalog rows that were not
            # VMEY's. Layout is <YYYY>/<mon>/<SID>/<session>/rinex/<file>.
            run_sids = {s.upper() for s in stations}
            all_rel = []
            for p in work.rglob("*"):
                if not (p.is_file() and p.parent.name == "rinex"):
                    continue
                rel = str(p.relative_to(work))
                parts = Path(rel).parts
                if len(parts) >= 3 and parts[2].upper() not in run_sids:
                    continue  # another run's leftovers — not ours to push
                all_rel.append(rel)
            new_rel = [r for r in all_rel if r not in _pushed_rel]
            if not new_rel:
                return
            st = _push_reconverted(args.work_dir, args, logger, only_rel=new_rel)
            # Mark done only if the rsync actually ran ok (rc==0) — a failed push
            # leaves them un-marked so a later flush retries. In --dry-run nothing
            # transfers (rc None) but we still mark, so each flush previews only
            # its own new batch (shows the cadence) instead of re-listing all.
            if st.get("rc") == 0 or getattr(args, "dry_run", False):
                _pushed_rel.update(new_rel)
            if push_stats is None:
                push_stats = st
            else:
                push_stats["staged"] += st.get("staged", 0)
                push_stats["pushed"] += st.get("pushed", 0)
                if st.get("note"):
                    push_stats["note"] = st["note"]

    _flush_fn = _rinex_flush if _do_flush else None

    # Shared build params so the re-rinex per-file dispatch can construct a
    # converter for whatever format each raw file actually is (see
    # _rinex_convert_station_period / _receiver_type_for_raw).
    _build_params = {
        "rinex_version": rinex_version,
        "apply_hatanaka": apply_hatanaka,
        "compression_format": compression_format,
        "naming_convention": naming_convention,
        "observation_types": observation_types,
        "rinex_config": rinex_config,
    }

    try:
        if network_first:
            # Pre-create converters for all stations
            converters: Dict[str, tuple] = {}
            for station_id in stations:
                conv, ext, _ = _create_rinex_converter(
                    station_id,
                    args,
                    rinex_version,
                    apply_hatanaka,
                    compression_format,
                    naming_convention,
                    observation_types,
                    logger,
                    rinex_config,
                )
                if conv is not None:
                    converters[station_id] = (conv, ext)
                else:
                    total_skipped += 1

            if not converters:
                logger.error("No valid stations to convert")
                return 1

            # Outer: periods (newest first), Inner: stations
            periods = generate_period_ranges(
                start_time, end_time, args.session, reverse=True
            )
            for period_start, period_end in periods:
                if args.session == "15s_24hr":
                    logger.info(f"\n--- {period_start.strftime('%Y-%m-%d')} ---")
                else:
                    logger.info(f"\n--- {period_start.strftime('%Y-%m-%d %H:%M')} ---")
                for station_id, (conv, ext) in converters.items():
                    logger.info(f"Processing station: {station_id}")
                    c, f, s = _rinex_convert_station_period(
                        station_id,
                        conv,
                        ext,
                        period_start,
                        period_end,
                        args,
                        logger,
                        durations=durations,
                        only_dates=only_dates,
                        flush_fn=_flush_fn,
                        flush_every=_rinex_flush_every,
                        build_params=_build_params,
                    )
                    total_converted += c
                    total_failed += f
                    total_skipped += s
        elif par_workers > 1:
            # Parallel: date-disjoint (station, year) chunks on a load-aware
            # thread pool. Each chunk gets its OWN converter instance (own
            # TOS cache) so nothing mutable is shared; NetworkUnavailableError
            # aborts the whole batch (resume by re-running, staged dates skip).
            from ..utils.batch_parallel import (
                ProgressBoard,
                run_chunks,
                split_year_ranges,
            )

            year_ranges = split_year_ranges(start_time, end_time)
            chunks = [(sid, ys, ye) for sid in stations for ys, ye in year_ranges]
            workers = par_workers
            print(
                f"\nParallel conversion: {len(chunks)} year-chunk(s) "
                f"on {workers} worker(s) — per-file detail goes to the log; "
                "console shows a progress line every 30s (failures always shown)"
            )
            board = ProgressBoard(interval=30)
            handles = {
                chunk: board.handle(f"{chunk[0]} {chunk[1].year}") for chunk in chunks
            }

            def _convert_chunk(chunk):
                sid, ys, ye = chunk
                h = handles[chunk]
                h.start()
                try:
                    conv, ext, _ = _create_rinex_converter(
                        sid,
                        args,
                        rinex_version,
                        apply_hatanaka,
                        compression_format,
                        naming_convention,
                        observation_types,
                        logger,
                        rinex_config,
                        # converter internals (3 lines/file) would flood the
                        # console × N workers; per-file results still reach
                        # the file log via the batch_quiet records.
                        loglevel_override=max(logging.WARNING, args.loglevel),
                    )
                    if conv is None:
                        return (0, 0, 1)
                    res = _rinex_convert_station_period(
                        sid,
                        conv,
                        ext,
                        ys,
                        ye,
                        args,
                        logger,
                        durations=durations,
                        progress=h,
                        only_dates=only_dates,
                        flush_fn=_flush_fn,
                        flush_every=_rinex_flush_every,
                        build_params=_build_params,
                    )
                    h.finish(ok=True)
                    return res
                except BaseException:
                    h.finish(ok=False)
                    raise

            with board:
                outcomes = run_chunks(
                    chunks,
                    _convert_chunk,
                    workers=workers,
                    logger=logger,
                    abort_on=(NetworkUnavailableError,),
                )
            # Per-chunk breakdown: chunks with real work get a line; the
            # fully-staged rest is one count (this is what tells you WHY only
            # one stream of conversions was visible).
            quiet_chunks = 0
            print("\nPer-chunk results:")
            for oc in outcomes:
                if oc.ok and oc.value is not None:
                    c, f, s = oc.value
                    total_converted += c
                    total_failed += f
                    total_skipped += s
                    if c or f:
                        label = f"{oc.chunk[0]} {oc.chunk[1].year}"
                        print(
                            f"  {label}: ✅ {c} converted, ❌ {f} failed, "
                            f"⏭️  {s} already staged"
                        )
                    else:
                        quiet_chunks += 1
                elif oc.error is not None:
                    total_failed += 1
                    print(
                        f"  {oc.chunk[0]} {oc.chunk[1].year}: ❌ chunk failed — {oc.error}"
                    )
            if quiet_chunks:
                print(f"  ({quiet_chunks} chunk(s) fully staged already — skips only)")
        else:
            # Station-first: current behavior
            for station_id in stations:
                print(f"\nProcessing: {station_id}")
                logger.info(f"\n{'=' * 60}")
                logger.info(f"Processing station: {station_id}")
                logger.info(f"{'=' * 60}")

                conv, ext, _ = _create_rinex_converter(
                    station_id,
                    args,
                    rinex_version,
                    apply_hatanaka,
                    compression_format,
                    naming_convention,
                    observation_types,
                    logger,
                    rinex_config,
                )
                if conv is None:
                    total_skipped += 1
                    continue

                c, f, s = _rinex_convert_station_period(
                    station_id,
                    conv,
                    ext,
                    start_time,
                    end_time,
                    args,
                    logger,
                    durations=durations,
                    only_dates=only_dates,
                    flush_fn=_flush_fn,
                    flush_every=_rinex_flush_every,
                    build_params=_build_params,
                )
                total_converted += c
                total_failed += f
                total_skipped += s
    except NetworkUnavailableError as e:
        aborted = True
        print(f"\n🌐 {e}")
        print(
            "   Aborting — metadata source unreachable, so the push is skipped to "
            "avoid staging header-less files.\n"
            "   Fix connectivity, then re-run the SAME command to resume: "
            "already-staged dates are skipped, only the unfinished ones convert."
        )
        logger.error(f"Re-rinex aborted (network unavailable): {e}")

    # --push: rsync the re-rinexed staging tree back to the archive (archive-side
    # --backup-old runs first). Only in re-rinex mode (--source-dir + --work-dir).
    # Skipped on a network abort — a resumed run pushes once conversion completes.
    if (
        not aborted
        and getattr(args, "push", False)
        and _is_rerinex_mode(args)
        and getattr(args, "work_dir", None)
    ):
        print("\nPushing re-rinexed files to the archive:")
        _rinex_flush()  # final flush: push whatever wasn't flushed mid-run

    # Summary — conversion counts, per-file timing, elapsed, and push outcome.
    elapsed = int(_time.monotonic() - run_t0)
    elapsed_str = f"{elapsed // 3600}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}"
    # "conversions:" is explicit because a --push run also emits reindex
    # warnings, and the two were being read as one number. Reindex reports its
    # own aggregate separately; nothing it logs feeds these counts.
    lines = [
        f"Summary: conversions ✅ {total_converted} converted, "
        f"❌ {total_failed} failed, ⏭️  {total_skipped} skipped (already staged)"
    ]
    if total_failed:
        lines.append(
            f"  ↳ {total_failed} conversion failure(s) — these are NOT the "
            f"reindex warnings above; re-run with -v for the per-file reason"
        )
    if durations:
        avg = sum(durations) / len(durations)
        lines.append(
            f"  ⏱️  elapsed {elapsed_str} — conversion avg {avg:.1f}s/file "
            f"(min {min(durations):.1f} / max {max(durations):.1f}, "
            f"{sum(durations):.0f}s converting)"
        )
    else:
        lines.append(f"  ⏱️  elapsed {elapsed_str}")
    if push_stats is not None:
        rc = push_stats["rc"]
        if push_stats["note"] and rc in (None, 0):
            lines.append(f"  🚀 push: {push_stats['note']}")
        elif rc == 0:
            lines.append(
                f"  🚀 push: {push_stats['pushed']}/{push_stats['staged']} "
                f"transferred in {push_stats['seconds']:.0f}s "
                f"({push_stats['staged'] - push_stats['pushed']} already on archive)"
            )
        else:
            lines.append(
                f"  🚀 push: INCOMPLETE (rsync exit {rc}) — "
                f"{push_stats['pushed']}/{push_stats['staged']} transferred; "
                f"re-run to push the rest: {push_stats['note']}"
            )
    elif aborted:
        lines.append("  🚀 push: skipped (network abort — re-run to resume)")

    print()
    logger.info(f"\n{'=' * 60}")
    logger.info("RINEX Conversion Summary:")
    for line in lines:
        print(line)
        logger.info(line)
    logger.info(f"{'=' * 60}")

    return 0 if total_failed == 0 else 1


def cmd_tools(args) -> int:
    """Tools management command - install and configure RINEX conversion tools."""
    from ..tools import ToolManager

    manager = ToolManager()

    if not hasattr(args, "tools_command") or args.tools_command is None:
        # No subcommand - show help
        print("Usage: receivers tools <command>")
        print("\nCommands:")
        print("  list         List all tools and their installation status")
        print("  install      Install a specific tool")
        print("  install-all  Install all auto-installable tools")
        print("  check        Check tool availability")
        print("  configure    Update receivers.cfg with tool paths")
        print("\nRun 'receivers tools <command> --help' for more info")
        return 0

    if args.tools_command == "list":
        tools = manager.list_tools()
        print("\nRINEX Conversion Tools")
        print("=" * 70)

        for name, info in tools.items():
            status_icon = "✅" if info["status"] == "installed" else "❌"
            if info["status"] == "manual_required":
                status_icon = "📋"

            auto = " (auto)" if info["auto_install"] else " (manual)"

            print(f"\n{status_icon} {name}{auto}")
            print(f"   {info['description']}")
            if info["installed_path"]:
                print(f"   Path: {info['installed_path']}")
            if info["version"]:
                print(f"   Version: {info['version']}")
            print(f"   Required for: {', '.join(info['required_for'])}")

        print("\n" + "=" * 70)
        print("Run 'receivers tools install <name>' to install a tool")
        print("Run 'receivers tools install-all' to install all auto-installable tools")
        return 0

    elif args.tools_command == "install":
        tool_name = args.tool_name
        force = getattr(args, "force", False)

        print(f"\nInstalling {tool_name}...")
        result = manager.install(tool_name, force=force)

        if result.success:
            print(f"\n✅ {result.message}")
            if result.path:
                print(f"   Path: {result.path}")
            return 0
        else:
            print(f"\n❌ {result.message}")
            return 1

    elif args.tools_command == "install-all":
        force = getattr(args, "force", False)

        print("\nInstalling all auto-installable tools...")
        results = manager.install_all(force=force)

        success_count = sum(1 for r in results if r.success)
        fail_count = len(results) - success_count

        print(f"\nSummary: ✅ {success_count} installed, ❌ {fail_count} failed")

        for r in results:
            if not r.success and "Manual installation required" not in r.message:
                print(f"  ❌ {r.tool_name}: {r.message}")

        return 0 if fail_count == 0 else 1

    elif args.tools_command == "check":
        receiver_type = getattr(args, "receiver_type", None)

        tools = manager.check_tools(receiver_type=receiver_type)

        if receiver_type:
            print(f"\nTools for {receiver_type}:")
        else:
            print("\nTool availability:")

        for name, available in tools.items():
            icon = "✅" if available else "❌"
            print(f"  {icon} {name}")

        all_available = all(tools.values())
        if all_available:
            print("\n✅ All required tools are available")
            return 0
        else:
            print("\n⚠️  Some tools are missing. Run 'receivers tools install-all'")
            return 1

    elif args.tools_command == "configure":
        from pathlib import Path

        config_path = None
        if hasattr(args, "config") and args.config:
            config_path = Path(args.config)

        updated = manager.configure_receivers_cfg(config_path)

        if updated:
            print("✅ Updated receivers.cfg with tool paths")
            return 0
        else:
            print("ℹ️  No changes needed (tools already configured)")
            return 0

    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser using standardized arguments module."""
    from .arguments import create_argument_parser

    parser = create_argument_parser()

    # Get subparsers and set command functions
    # We need to access the subparsers to set the func defaults
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_map = action.choices
            if "download" in subparsers_map:
                subparsers_map["download"].set_defaults(func=cmd_download)
            if "status" in subparsers_map:
                subparsers_map["status"].set_defaults(func=cmd_status)
            if "health" in subparsers_map:
                subparsers_map["health"].set_defaults(func=cmd_health)
            if "validate" in subparsers_map:
                subparsers_map["validate"].set_defaults(func=cmd_validate)
            if "rec-config" in subparsers_map:
                subparsers_map["rec-config"].set_defaults(func=cmd_rec_config)
            if "rec-provision" in subparsers_map:
                subparsers_map["rec-provision"].set_defaults(func=cmd_rec_provision)
            if "rec-upgrade-firmware" in subparsers_map:
                subparsers_map["rec-upgrade-firmware"].set_defaults(
                    func=cmd_rec_upgrade_firmware
                )
            if "rinex" in subparsers_map:
                subparsers_map["rinex"].set_defaults(func=cmd_rinex)
            if "tools" in subparsers_map:
                subparsers_map["tools"].set_defaults(func=cmd_tools)
            break

    return parser


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Handle scheduler subcommands
    if args.command == "scheduler":
        try:
            from .scheduler import handle_scheduler_command

            return handle_scheduler_command(args)
        except ImportError:
            print(
                "❌ Scheduler requires APScheduler. Install with: pip install apscheduler"
            )
            return 1

    # Handle TOS subcommands
    if args.command == "tos":
        try:
            from .tos import handle_tos_command

            return handle_tos_command(args)
        except ImportError as e:
            print(
                "❌ TOS command requires tostools. Install with: pip install tostools"
            )
            print(f"   Error: {e}")
            return 1

    # Handle database management subcommands
    if args.command == "db":
        from .db import handle_db_command

        return handle_db_command(args)

    # Handle cfg reconciliation subcommands
    if args.command == "cfg":
        from .cfg import handle_cfg_command

        return handle_cfg_command(args)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 130
    except Exception as e:
        _logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
