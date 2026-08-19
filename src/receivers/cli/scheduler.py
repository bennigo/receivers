#!/usr/bin/env python3
"""
Scheduler management CLI for bulk GPS receiver downloads.

Provides complete control over the APScheduler-based bulk download system
while maintaining full compatibility with manual operations.
"""

import argparse
import json
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    from ..scheduling.bulk_scheduler import HAS_APSCHEDULER, BulkDownloadScheduler
    from ..scheduling.config_loader import (
        create_default_config_file,
        load_scheduler_config,
    )
except ImportError:
    HAS_APSCHEDULER = False


def cmd_scheduler_start(args) -> int:
    """Start the bulk download scheduler."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    # Parse --only flag
    scheduler_types = None
    if getattr(args, "only", None):
        scheduler_types = [s.strip() for s in args.only.split(",")]
        print(f"📋 Running only: {', '.join(scheduler_types)}")

    try:
        # Create scheduler with filtering options
        scheduler = BulkDownloadScheduler(
            production_mode=not args.verbose,
            max_workers=args.max_workers,
            station_filter=getattr(args, "stations", None),
            max_stations_per_session=getattr(args, "max_stations", None),
            scheduler_types=scheduler_types,
        )

        # Schedule all sessions
        scheduler.schedule_all_sessions()

        # Count jobs before starting
        jobs_count = len(scheduler.get_scheduled_jobs())
        print(f"✅ Scheduled {jobs_count} download jobs")

        # Set up signal handling for graceful shutdown
        def signal_handler(signum, frame):
            print("\\n🛑 Shutting down scheduler...")
            scheduler.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        print(f"🚀 Starting scheduler with {scheduler.max_workers} workers...")
        print("   Press Ctrl+C to stop")

        # Start scheduler (non-blocking for BackgroundScheduler)
        scheduler.start()

        # Give scheduler a moment to process jobs and calculate next run times
        time.sleep(0.1)

        # Now show scheduled jobs with their actual next run times
        if args.show_jobs:
            jobs = scheduler.get_scheduled_jobs()
            print("\\nScheduled jobs:")
            for job in sorted(jobs, key=lambda x: x["next_run"] or ""):
                next_run = job["next_run"] or "Not scheduled"
                print(f"  {job['id']}: {next_run}")

        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\\n🛑 Scheduler stopped by user")
        return 0
    except Exception as e:
        print(f"❌ Scheduler failed: {e}")
        return 1


def cmd_scheduler_status(args) -> int:
    """Show scheduler status and job information."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    try:
        # Create scheduler (no start)
        scheduler = BulkDownloadScheduler(production_mode=True)

        # Get status
        status = scheduler.get_job_status()
        jobs = scheduler.get_scheduled_jobs()

        print("📊 Scheduler Status")
        print("=" * 50)
        print(f"Running: {status['scheduler_running']}")
        print(f"Total jobs: {status['total_jobs']}")
        print(f"Active downloads: {status['running_jobs']}")

        if status["current_jobs"]:
            print(f"Current jobs: {', '.join(status['current_jobs'])}")

        if args.show_jobs and jobs:
            print(f"\\n📅 Scheduled Jobs ({len(jobs)})")
            print("-" * 50)

            # Group by session type
            by_session = {}
            for job in jobs:
                session = job["id"].split("_")[0]
                if session not in by_session:
                    by_session[session] = []
                by_session[session].append(job)

            for session_type, session_jobs in sorted(by_session.items()):
                print(f"\\n{session_type} ({len(session_jobs)} stations):")

                # Show next few runs
                next_runs = sorted(
                    [j for j in session_jobs if j["next_run"]],
                    key=lambda x: x["next_run"],
                )[:5]

                for job in next_runs:
                    station = job["id"].split("_", 1)[1]
                    next_run = datetime.fromisoformat(job["next_run"]).strftime(
                        "%H:%M:%S"
                    )
                    print(f"  {station}: {next_run}")

                if len(session_jobs) > 5:
                    print(f"  ... and {len(session_jobs) - 5} more")

        return 0

    except Exception as e:
        print(f"❌ Failed to get scheduler status: {e}")
        return 1


def cmd_scheduler_config(args) -> int:
    """Manage scheduler configuration."""
    import os

    # Determine config path (respects GPS_CONFIG_PATH env var)
    gps_config_dir = os.getenv("GPS_CONFIG_PATH")
    if gps_config_dir:
        config_file = Path(gps_config_dir) / "scheduler.yaml"
    else:
        config_file = Path.home() / ".config" / "gpsconfig" / "scheduler.yaml"

    if args.create:
        try:
            created_file = create_default_config_file()
            print(f"✅ Created scheduler configuration: {created_file}")
            print("   Edit this file to customize scheduling behavior")
            return 0
        except Exception as e:
            print(f"❌ Failed to create configuration: {e}")
            return 1

    if args.show:
        if config_file.exists():
            # Load and display as YAML (or raw content)
            try:
                config = load_scheduler_config(config_file)
                print(f"📋 Current scheduler configuration ({config_file}):")
                print(json.dumps(config, indent=2))
            except Exception:
                # Fall back to showing raw file
                print(f"📋 Current scheduler configuration ({config_file}):")
                print(config_file.read_text())
        else:
            print(f"❌ No configuration file found at {config_file}")
            print("   Create one with: receivers scheduler config --create")
            return 1

    return 0


def cmd_scheduler_test(args) -> int:
    """Test scheduler setup without starting."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    # Parse --only flag
    scheduler_types = None
    if getattr(args, "only", None):
        scheduler_types = [s.strip() for s in args.only.split(",")]
        print(f"📋 Testing only: {', '.join(scheduler_types)}")

    try:
        print("🧪 Testing scheduler setup...")

        # Create scheduler with filtering options
        scheduler = BulkDownloadScheduler(
            production_mode=True,
            station_filter=getattr(args, "stations", None),
            max_stations_per_session=getattr(args, "max_stations", None),
            scheduler_types=scheduler_types,
        )

        # Load stations
        print(f"✅ Loaded {len(scheduler.stations)} station configurations")

        # Show filtering info
        if scheduler.station_filter:
            print(f"🔍 Station filter: {', '.join(scheduler.station_filter)}")
        if scheduler.max_stations_per_session:
            print(f"🔢 Max stations per session: {scheduler.max_stations_per_session}")

        # Test scheduling (without starting)
        scheduler.schedule_all_sessions()
        jobs = scheduler.get_scheduled_jobs()

        print(f"✅ Successfully scheduled {len(jobs)} jobs")

        # Show distribution by session
        by_session = {}
        station_list = {}
        for job in jobs:
            session = job["id"].split("_")[0]
            station = job["id"].split("_", 1)[1]
            by_session[session] = by_session.get(session, 0) + 1
            if session not in station_list:
                station_list[session] = []
            station_list[session].append(station)

        print("\\n📊 Job distribution:")
        for session, count in sorted(by_session.items()):
            config = scheduler.schedule_configs.get(session, {})
            schedule_time = (
                f"{config.schedule_minute:02d}:XX"
                if hasattr(config, "schedule_minute")
                else "Unknown"
            )
            frequency = getattr(config, "frequency", "unknown")
            stations = ", ".join(sorted(station_list[session])[:5])
            if len(station_list[session]) > 5:
                stations += f" +{len(station_list[session]) - 5} more"
            print(f"  {session}: {count} stations ({frequency} at {schedule_time})")
            print(f"           Stations: {stations}")

        # Test next run times
        if jobs:
            next_jobs = sorted(
                [j for j in jobs if j["next_run"]], key=lambda x: x["next_run"]
            )[:3]
            print("\\n⏰ Next few scheduled runs:")
            for job in next_jobs:
                station = job["id"].split("_", 1)[1]
                session = job["id"].split("_")[0]
                next_run = datetime.fromisoformat(job["next_run"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                print(f"  {station} ({session}): {next_run}")

        print("\\n✅ Scheduler test completed successfully")
        print("   Use 'receivers scheduler start' to run the scheduler")

        return 0

    except Exception as e:
        print(f"❌ Scheduler test failed: {e}")
        return 1


def _signal(proc, action) -> bool:
    """Send a signal, tolerating a process that already exited or is not ours."""
    import psutil

    try:
        action()
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        print(f"⚠️  no permission to signal PID {proc.pid} (owned by another user?)")
        return False


def cmd_scheduler_stop(args) -> int:
    """Stop the running scheduler."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    try:
        import os

        import psutil

        # Find running scheduler process
        current_pid = os.getpid()
        scheduler_pids = []

        matched = {}
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline", [])
                if (
                    cmdline
                    and "receivers" in " ".join(cmdline)
                    and "scheduler" in " ".join(cmdline)
                    and "start" in " ".join(cmdline)
                ):
                    if proc.info["pid"] != current_pid:
                        matched[proc.info["pid"]] = proc.info.get("ppid")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Signal only the ROOT scheduler(s). The RINEX pool workers are forked
        # from the main process, so they inherit its cmdline verbatim and match
        # this filter too — on rek-d01 that is 1 main + 16 workers = 17 matches.
        #
        # The old code terminated all 17 SERIALLY with a 30 s wait each: up to
        # 510 s against the unit's TimeoutStopSec=120, so ExecStop could never
        # finish and systemd SIGKILLed the whole cgroup every single deploy
        # (measured 4m0.4s = 8 timeouts x 30 s, Result=signal). It got worse
        # when the pool went 4 -> 16 workers; at 4 it was 150 s, already over.
        #
        # Signalling the parent is also the CORRECT shutdown: it runs the
        # graceful APScheduler shutdown, and its children exit with it. Anything
        # left over is reaped by systemd's KillMode=control-group.
        scheduler_pids = [pid for pid, ppid in matched.items() if ppid not in matched]
        workers = len(matched) - len(scheduler_pids)

        if not scheduler_pids:
            print("ℹ️  No running scheduler found")
            return 0

        print(
            f"🛑 Found {len(scheduler_pids)} scheduler process(es)"
            + (f" (+{workers} worker(s), signalled via the parent)" if workers else "")
        )

        # Total budget for the graceful phase. Must stay under the unit's
        # TimeoutStopSec (120 s) or systemd kills us mid-shutdown and the
        # scheduler never gets to close its jobstore cleanly.
        timeout = float(getattr(args, "timeout", None) or 60)

        procs = []
        for pid in scheduler_pids:
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue

        try:
            if args.force:
                print(f"⚡ Force stopping {len(procs)} scheduler process(es)...")
                for p in procs:
                    _signal(p, p.kill)
                psutil.wait_procs(procs, timeout=5)
                print("✅ Scheduler forcefully stopped")
                return 0

            print(f"🛑 Gracefully stopping {len(procs)} scheduler process(es)...")
            print(f"   Waiting up to {timeout:.0f}s for active work to finish...")
            # Signal everything FIRST, then wait on one shared deadline — the
            # waits must overlap, not accumulate.
            for p in procs:
                _signal(p, p.terminate)
            gone, alive = psutil.wait_procs(procs, timeout=timeout)
            for p in gone:
                print(f"✅ Scheduler stopped gracefully (PID {p.pid})")

            if alive:
                print(
                    f"⚠️  {len(alive)} process(es) still running after {timeout:.0f}s "
                    "— force killing"
                )
                for p in alive:
                    _signal(p, p.kill)
                _, still = psutil.wait_procs(alive, timeout=5)
                if still:
                    print(f"❌ {len(still)} process(es) survived SIGKILL")
                    return 1
                print("✅ Scheduler forcefully stopped")
        except Exception as e:
            print(f"❌ Failed to stop scheduler: {e}")
            return 1

        return 0

    except ImportError:
        print("❌ psutil not available. Install with: pip install psutil")
        print(
            "   Or manually stop the scheduler with: pkill -f 'receivers scheduler start'"
        )
        return 1
    except Exception as e:
        print(f"❌ Failed to stop scheduler: {e}")
        return 1


def cmd_scheduler_backfill(args) -> int:
    """Manually trigger backfill for a specific session type."""
    from datetime import date, timedelta

    session_type = getattr(args, "session", "status_1hr")
    days_back = getattr(args, "days", 30)
    stations = getattr(args, "stations", None)

    print(f"Starting manual backfill for {session_type} ({days_back} days back)")

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        # If specific stations requested, just process them
        if stations:
            station_ids = [s.upper() for s in stations]
        else:
            # Get stations from backfill_progress table
            with DatabaseConnectionFactory.connection(single_host=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT sid FROM backfill_progress
                        WHERE session_type = %s
                          AND status IN ('pending', 'in_progress')
                        ORDER BY last_run ASC NULLS FIRST
                    """,
                        (session_type,),
                    )
                    station_ids = [row[0] for row in cur.fetchall()]

        if not station_ids:
            print(f"No stations pending backfill for {session_type}")
            return 0

        max_stations = getattr(args, "max_stations", None)
        if max_stations:
            station_ids = station_ids[:max_stations]

        print(f"Processing {len(station_ids)} stations: {', '.join(station_ids[:10])}")
        if len(station_ids) > 10:
            print(f"  ... and {len(station_ids) - 10} more")

        from ..scheduling.backfill import _backfill_station_day_generic

        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)
        archiving_mode = getattr(args, "archiving_mode", "bulk")
        immediate = archiving_mode != "bulk"

        total_processed = 0
        for station_id in station_ids:
            current = start_date
            while current <= end_date:
                has_more = _backfill_station_day_generic(
                    station_id,
                    current,
                    end_date,
                    session_type,
                    immediate_archive=immediate,
                )
                total_processed += 1
                if not has_more:
                    break
                current += timedelta(days=1)

                # Show progress
                if total_processed % 10 == 0:
                    print(f"  Processed {total_processed} station-days...")

        print(f"\nBackfill complete: {total_processed} station-days processed")
        return 0

    except ImportError as e:
        print(f"Required modules not available: {e}")
        return 1
    except Exception as e:
        print(f"Backfill failed: {e}")
        return 1


def cmd_scheduler_long_term_gaps(args) -> int:
    """Classify a station's long-term gap (read-only) — the worklist the
    long-term backfill would consume.

    Surfaces, per station/session, the DB-driven classification: ``queued``
    (download candidates, receiver may still hold them) vs ``confirmed_gone``
    (ran off the receiver auto-delete cycle — skip) vs ``provisional_absent``.
    Anchors the window to the station's true gap start (last-archived + 1), not
    a fixed trailing window. See docs/design/long-term-backfill.md (#136).
    """
    from ..scheduling.long_term_backfill import format_report, query_long_term_gaps

    session = getattr(args, "session", "15s_24hr")
    lookback = getattr(args, "lookback", 365)
    stations = [s.upper() for s in (args.stations or [])]
    if not stations:
        print("No --stations given (long-term gap classification is per-station).")
        return 1
    for sid in stations:
        print(format_report(query_long_term_gaps(sid, session, lookback_days=lookback)))
    return 0


def cmd_scheduler_long_term_backfill(args) -> int:
    """Recover a station's classified gaps through the full per-day pipeline."""
    from ..scheduling.long_term_backfill import (
        format_report,
        run_long_term_backfill_station,
    )

    session = getattr(args, "session", "15s_24hr")
    lookback = getattr(args, "lookback", 365)
    run_rinex = not getattr(args, "no_rinex", False)
    dry_run = getattr(args, "dry_run", False)
    max_days = getattr(args, "max_days", None)
    for sid in args.stations or []:
        report = run_long_term_backfill_station(
            sid.upper(),
            session,
            lookback_days=lookback,
            run_rinex=run_rinex,
            dry_run=dry_run,
            max_days=max_days,
        )
        print(format_report(report))
    return 0


def cmd_scheduler_clean_stale_tmp(args) -> int:
    """Delete stale partial downloads from the tmp directory."""
    session = getattr(args, "session", "15s_24hr")
    hours = getattr(args, "hours", 4.0)
    dry_run = getattr(args, "dry_run", False)

    try:
        from pathlib import Path

        from ..config.receivers_config import ReceiversConfig
        from ..scheduling.backfill import clean_stale_tmp

        if dry_run:
            tmp_root = Path(ReceiversConfig().get_tmp_dir())
            print(
                f"[DRY RUN] Would scan {tmp_root}/*/{session}/ for files older than {hours}h"
            )
            count = 0
            affected = []
            now_ts = __import__("time").time()
            for station_dir in tmp_root.iterdir():
                if not station_dir.is_dir():
                    continue
                sess_dir = station_dir / session
                if not sess_dir.is_dir():
                    continue
                for f in sess_dir.iterdir():
                    if not f.is_file():
                        continue
                    age_h = (now_ts - f.stat().st_mtime) / 3600
                    if age_h >= hours:
                        print(
                            f"  Would remove: {station_dir.name}/{session}/{f.name} ({age_h:.1f}h old)"
                        )
                        count += 1
                        if station_dir.name not in affected:
                            affected.append(station_dir.name)
            print(f"Would remove {count} file(s) from {len(affected)} station(s)")
            return 0

        count, affected = clean_stale_tmp(session, stale_hours=hours)
        print(f"Removed {count} stale tmp file(s) from {len(affected)} station(s)")
        if affected:
            print(f"Affected stations: {' '.join(affected)}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=__import__("sys").stderr)
        return 1


def cmd_scheduler_reconcile(args) -> int:
    """Manually trigger raw->RINEX archive reconciliation."""

    days_back = getattr(args, "days", 30)
    dry_run = getattr(args, "dry_run", False)
    stations = getattr(args, "stations", None)

    print(f"Archive reconciler: scanning {days_back} days back")
    if dry_run:
        print("  DRY RUN: no conversions will be performed")

    try:
        from datetime import date, timedelta

        from ..cli.main import get_all_station_configs
        from ..config.receiver_registry import has_rinex_converter
        from ..health.file_tracker import ArchiveFileChecker
        from ..scheduling.archive_reconciler import (
            _find_raw_file,
            _find_rinex_file,
            _reconcile_station_session,
        )

        all_stations = get_all_station_configs()

        if stations:
            target_stations = {}
            for s in stations:
                sid = s.upper()
                cfg = all_stations.get(sid, {})
                rt = cfg.get("receiver_type", "").lower()
                target_stations[sid] = rt if rt else "polarx5"
        else:
            target_stations = {
                sid: cfg.get("receiver_type", "").lower()
                for sid, cfg in all_stations.items()
                if cfg.get("enabled", True)
                and has_rinex_converter(cfg.get("receiver_type", ""))
                and cfg.get("station_status") not in ("discontinued", "inactive")
                and cfg.get("health_check") != "passive"
            }

        session_types = ["15s_24hr", "1Hz_1hr"]
        print(f"Scanning {len(target_stations)} stations, sessions: {session_types}")

        checker = ArchiveFileChecker()
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)

        total_missing = 0
        total_converted = 0
        total_errors = 0

        if dry_run:
            # Dry-run: use lightweight inner loop to list missing files only
            from datetime import datetime, timezone

            for station_id in sorted(target_stations):
                receiver_type = target_stations[station_id]
                for session_type in session_types:
                    current = start_date
                    while current <= end_date:
                        dt = datetime.combine(current, datetime.min.time()).replace(
                            tzinfo=UTC
                        )
                        hours = [0] if session_type == "15s_24hr" else list(range(24))
                        for hour in hours:
                            file_dt = dt.replace(hour=hour)
                            raw_path = _find_raw_file(
                                station_id,
                                session_type,
                                file_dt,
                                checker,
                                receiver_type=receiver_type,
                            )
                            if raw_path is None:
                                continue
                            if _find_rinex_file(raw_path) is None:
                                total_missing += 1
                                print(f"  MISSING RINEX: {raw_path}")
                        current += timedelta(days=1)
            print(f"\nDry run complete: {total_missing} raw files missing RINEX")
        else:
            for station_id in sorted(target_stations):
                receiver_type = target_stations[station_id]
                for session_type in session_types:
                    missing, converted, errors = _reconcile_station_session(
                        station_id,
                        session_type,
                        start_date,
                        end_date,
                        checker,
                        receiver_type=receiver_type,
                    )
                    total_missing += missing
                    total_converted += converted
                    total_errors += errors
            print(
                f"\nReconciliation complete: {total_missing} missing, "
                f"{total_converted} converted, {total_errors} errors"
            )

        return 0

    except ImportError as e:
        print(f"Required modules not available: {e}")
        return 1
    except Exception as e:
        print(f"Reconciliation failed: {e}")
        return 1


def cmd_scheduler_integrity(args) -> int:
    """Manually trigger file integrity checking."""

    session_arg = getattr(args, "session", "all")
    days_back = getattr(args, "days", 7)
    stations = getattr(args, "stations", None)
    no_receiver = getattr(args, "no_receiver", False)
    tolerance = getattr(args, "tolerance", 50.0)

    if session_arg == "all":
        sessions = ["15s_24hr", "1Hz_1hr", "status_1hr"]
    else:
        sessions = [session_arg]

    print(f"Integrity check: {', '.join(sessions)}, {days_back} days back")
    if no_receiver:
        print("  Receiver comparison: disabled")
    else:
        print(f"  Receiver comparison: enabled (tolerance={tolerance}%)")
    if stations:
        print(f"  Stations: {', '.join(s.upper() for s in stations)}")

    try:
        from ..scheduling.integrity_checker import _run_integrity_check_job

        result = _run_integrity_check_job(
            session_types=sessions,
            days_back=days_back,
            check_receiver=not no_receiver,
            size_tolerance_pct=tolerance,
            station_filter=[s.upper() for s in stations] if stations else None,
        )

        if result is None:
            print("\nIntegrity check completed (no summary returned)")
        else:
            print("\nIntegrity check completed")

        return 0

    except ImportError as e:
        print(f"Required modules not available: {e}")
        return 1
    except Exception as e:
        print(f"Integrity check failed: {e}")
        return 1


def cmd_scheduler_horizon_probe(args) -> int:
    """Manually trigger the per-receiver horizon probe (unified file index).

    Lists each receiver's date-directory index to find the oldest file it still
    holds and records it in ``receiver_horizon`` (the dynamic fetch floor for
    ``missing_on_receiver``). Handy for one-off validation against real receivers.
    """
    stations = getattr(args, "stations", None)
    session = getattr(args, "session", None)
    station_filter = [s.upper() for s in stations] if stations else None
    probe_cfg = {}
    if session and session != "all":
        probe_cfg["sessions"] = [session]

    scope = ", ".join(station_filter) if station_filter else "all active stations"
    print(f"Receiver horizon probe: {scope}")
    if probe_cfg.get("sessions"):
        print(f"  Sessions: {', '.join(probe_cfg['sessions'])}")

    try:
        from ..scheduling.receiver_horizon_probe import _run_horizon_probe_job

        _run_horizon_probe_job(probe_cfg or None, station_filter=station_filter)
    except Exception as e:
        print(f"Horizon probe failed: {e}")
        return 1

    # Show what got recorded (for the filtered stations, or the latest overall).
    try:
        from ..health import FileTracker

        tracker = FileTracker()
        if tracker.connect():
            with tracker.read_cursor() as cur:
                if station_filter:
                    cur.execute(
                        "SELECT sid, session_type, oldest_date, oldest_hour "
                        "FROM receiver_horizon WHERE sid = ANY(%s) "
                        "ORDER BY sid, session_type",
                        (station_filter,),
                    )
                else:
                    cur.execute(
                        "SELECT sid, session_type, oldest_date, oldest_hour "
                        "FROM receiver_horizon ORDER BY observed_at DESC LIMIT 40"
                    )
                rows = cur.fetchall()
            tracker.close()
            if rows:
                print(f"\nreceiver_horizon ({len(rows)} rows):")
                for sid, sess, oldest, hour in rows:
                    hr = f" h{hour}" if hour is not None else ""
                    print(f"  {sid:6} {sess:12} {oldest}{hr}")
            else:
                print(
                    "\nNo horizons recorded — stations may be unreachable, or "
                    "have no directory-listing API (NetRS)."
                )
    except Exception:  # noqa: BLE001 - summary is best-effort
        pass
    return 0


def cmd_scheduler_pipeline_status(args) -> int:
    """Show pipeline job status and history."""
    try:
        from ..scheduling.pipeline import PipelineStateStore

        store = PipelineStateStore()
        station = getattr(args, "station", None)
        session = getattr(args, "session", None)
        limit = getattr(args, "limit", 20)

        if station:
            jobs = store.load_jobs_by_station(
                station.upper(),
                session_type=session,
                limit=limit,
            )
            print(
                f"Pipeline jobs for {station.upper()}"
                + (f" ({session})" if session else "")
            )
        else:
            # Show stats and recent incomplete jobs
            stats = store.get_stats()
            print("Pipeline Statistics")
            print("=" * 50)
            print(f"Total jobs:      {stats['total_jobs']}")
            print(f"Complete:        {stats['complete_jobs']}")
            print(f"Incomplete:      {stats['incomplete_jobs']}")

            if stats["by_session_type"]:
                print("\nBy session type:")
                for session_type, count in sorted(stats["by_session_type"].items()):
                    print(f"  {session_type}: {count}")

            jobs = store.load_incomplete_jobs()
            if not jobs:
                print("\nNo incomplete pipeline jobs")
                return 0
            print(f"\nIncomplete jobs ({len(jobs)}):")

        if not jobs:
            print("  No matching pipeline jobs found")
            return 0

        print(f"\n{'Station':<8} {'Session':<12} {'Stages':<40} {'Updated':<20}")
        print("-" * 80)

        for job in jobs[:limit]:
            stages_str = []
            for stage, result in job.stages.items():
                icon = {
                    "pending": "⏳",
                    "running": "🔄",
                    "completed": "✅",
                    "failed": "❌",
                    "skipped": "⏭️",
                }.get(result.status.value, "?")
                stages_str.append(f"{icon}{stage.value}")

            updated = (
                job.updated_at.strftime("%Y-%m-%d %H:%M") if job.updated_at else "N/A"
            )
            print(
                f"{job.station_id:<8} {job.session_type:<12} {' '.join(stages_str):<40} {updated:<20}"
            )

        return 0

    except ImportError as e:
        print(f"Pipeline tracking not available: {e}")
        return 1
    except Exception as e:
        print(f"Failed to get pipeline status: {e}")
        return 1


def cmd_scheduler_load_status(args) -> int:
    """Show current system load and throttling status."""
    try:
        from ..scheduling.config_loader import load_scheduler_config
        from ..scheduling.load_monitor import LoadMonitor

        config = load_scheduler_config()
        load_cfg = config.get("load_monitoring", {})

        if not load_cfg.get("enabled", False):
            print("Load monitoring is disabled in scheduler.yaml")
            print("Enable it with: load_monitoring: { enabled: true }")
            return 0

        monitor = LoadMonitor(load_cfg)
        status = monitor.get_status()

        print("System Load Status")
        print("=" * 50)
        print(
            f"CPU load (1m):     {status['cpu_load_1m']:.2f}  (max: {status['thresholds']['max_cpu_load']})"
        )
        print(f"CPU load (5m):     {status['cpu_load_5m']:.2f}")
        print(
            f"Active threads:    {status['active_threads']}  (max: {status['thresholds']['max_active_jobs']})"
        )
        print(
            f"Network:           {status['network_mbps']:.1f} Mbps  (max: {status['thresholds']['max_network_mbps']})"
        )

        print("\nJob admission by priority:")
        for priority_name, allowed in status["can_start"].items():
            icon = "✅" if allowed else "❌"
            print(f"  {icon} {priority_name}")

        return 0

    except ImportError as e:
        print(f"Load monitoring not available: {e}")
        return 1
    except Exception as e:
        print(f"Failed to get load status: {e}")
        return 1


def cmd_scheduler_bootstrap(args) -> int:
    """Manually trigger bootstrap (cold-start) downloads."""
    if not HAS_APSCHEDULER:
        print("APScheduler not available. Install with: pip install apscheduler")
        return 1

    try:
        from ..scheduling.bootstrap import schedule_bootstrap

        days = getattr(args, "days", 3)
        stations_arg = getattr(args, "stations", None)
        station_filter = [s.upper() for s in stations_arg] if stations_arg else None

        scheduler = BulkDownloadScheduler(
            production_mode=not getattr(args, "verbose", False),
            station_filter=station_filter,
        )

        bootstrap_cfg = {
            "distribution_window": getattr(args, "window", 10),
            "initial_lookback_days": days,
        }

        jobs = schedule_bootstrap(
            scheduler=scheduler.scheduler,
            stations=scheduler.stations,
            session_configs=scheduler.schedule_configs,
            bootstrap_cfg=bootstrap_cfg,
            production_mode=scheduler.production_mode,
            station_filter=station_filter,
        )

        if jobs == 0:
            print("No bootstrap jobs created (no eligible stations)")
            return 0

        print(f"Created {jobs} bootstrap download jobs (lookback={days} days)")
        print("Starting scheduler to execute bootstrap jobs...")

        import signal

        def signal_handler(signum, frame):
            print("\nStopping bootstrap...")
            scheduler.stop()
            import sys

            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        scheduler.start()

        # Wait for jobs to complete
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nBootstrap stopped by user")
        return 0
    except Exception as e:
        print(f"Bootstrap failed: {e}")
        return 1


def cmd_scheduler_backfill_status(args) -> int:
    """Show backfill progress and priority queue."""
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        session = getattr(args, "session", None)

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                if session:
                    cur.execute(
                        """
                        SELECT sid, session_type, status, next_date, backfill_end,
                               files_found, files_imported, files_missing, files_error,
                               last_run
                        FROM backfill_progress
                        WHERE session_type = %s
                        ORDER BY status, last_run ASC NULLS FIRST
                    """,
                        (session,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT sid, session_type, status, next_date, backfill_end,
                               files_found, files_imported, files_missing, files_error,
                               last_run
                        FROM backfill_progress
                        ORDER BY session_type, status, last_run ASC NULLS FIRST
                    """
                    )

                rows = cur.fetchall()

        if not rows:
            print("No backfill progress entries found")
            return 0

        # Summary
        by_status = {}
        for row in rows:
            status = row[2]
            by_status[status] = by_status.get(status, 0) + 1

        title = "Backfill Status" + (f" ({session})" if session else "")
        print(title)
        print("=" * 60)
        for status, count in sorted(by_status.items()):
            print(f"  {status}: {count} stations")

        # Detail table
        print(
            f"\n{'Station':<8} {'Session':<12} {'Status':<12} {'Next Date':<12} {'End':<12} {'Found':<6} {'Import':<6} {'Miss':<6} {'Err':<4}"
        )
        print("-" * 90)

        limit = getattr(args, "limit", 30)
        for row in rows[:limit]:
            sid, sess, status, next_dt, end_dt = row[0], row[1], row[2], row[3], row[4]
            found, imported, missing, errors = (
                row[5] or 0,
                row[6] or 0,
                row[7] or 0,
                row[8] or 0,
            )
            next_str = str(next_dt) if next_dt else "N/A"
            end_str = str(end_dt) if end_dt else "N/A"
            print(
                f"{sid:<8} {sess:<12} {status:<12} {next_str:<12} {end_str:<12} {found:<6} {imported:<6} {missing:<6} {errors:<4}"
            )

        if len(rows) > limit:
            print(f"... and {len(rows) - limit} more")

        return 0

    except ImportError as e:
        print(f"Database not available: {e}")
        return 1
    except Exception as e:
        print(f"Failed to get backfill status: {e}")
        return 1


def cmd_scheduler_gaps(args) -> int:
    """Find gaps in downloaded files."""
    from datetime import date, timedelta

    try:
        from ..cli.main import get_all_station_configs
        from ..health.file_tracker import GapDetector
    except ImportError as e:
        print(f"❌ Required modules not available: {e}")
        return 1

    # Get stations
    station_filter = getattr(args, "stations", None)
    if station_filter:
        station_ids = [s.upper() for s in station_filter]
    else:
        # Get all enabled stations
        all_stations = get_all_station_configs()
        station_ids = [
            sid for sid, cfg in all_stations.items() if cfg.get("enabled", True)
        ]

    # Limit stations for testing
    max_stations = getattr(args, "max_stations", None)
    if max_stations:
        station_ids = station_ids[:max_stations]

    session_type = getattr(args, "session", "15s_24hr")
    days_back = getattr(args, "days", 7)

    print(f"🔍 Finding gaps in {session_type} data for {len(station_ids)} stations")
    print(f"   Checking last {days_back} days\n")

    # Calculate date range
    end_date = date.today() - timedelta(days=1)  # Yesterday
    start_date = end_date - timedelta(days=days_back - 1)

    with GapDetector() as detector:
        if getattr(args, "summary", False):
            # Get summary for all stations
            summary = detector.get_gap_summary(
                station_ids,
                session_type,
                days_back=days_back,
            )

            print(f"📊 Gap Summary ({summary['start_date']} to {summary['end_date']})")
            print(f"   Total expected files: {summary['total_expected']}")
            print(f"   Total archived files: {summary['total_archived']}")
            print(f"   Total gaps (need download): {summary['total_gaps']}")

            if summary["total_gaps"] > 0:
                print("\n   Stations with gaps:")
                stations_with_gaps = [
                    (sid, info)
                    for sid, info in summary["stations"].items()
                    if info["gaps"] > 0
                ]
                for sid, info in sorted(
                    stations_with_gaps, key=lambda x: -x[1]["gaps"]
                )[:20]:
                    pct = (
                        100 * info["archived"] / info["expected"]
                        if info["expected"]
                        else 0
                    )
                    print(f"     {sid}: {info['gaps']} gaps ({pct:.0f}% complete)")

                if len(stations_with_gaps) > 20:
                    print(f"     ... and {len(stations_with_gaps) - 20} more stations")
        else:
            # Show detailed gaps per station
            total_gaps = 0
            for station_id in sorted(station_ids):
                gaps = detector.find_gaps(
                    station_id,
                    session_type,
                    start_date,
                    end_date,
                    sync_first=True,
                    skip_missing_on_receiver=not getattr(
                        args, "include_missing", False
                    ),
                )

                if gaps:
                    total_gaps += len(gaps)
                    print(f"📁 {station_id}: {len(gaps)} gaps")
                    if getattr(args, "verbose", False):
                        for gap in gaps[:10]:
                            hour_str = (
                                f" hour {gap.file_hour:02d}"
                                if gap.file_hour is not None
                                else ""
                            )
                            print(f"     {gap.file_date}{hour_str}")
                        if len(gaps) > 10:
                            print(f"     ... and {len(gaps) - 10} more")

            print(f"\n✅ Total gaps found: {total_gaps}")

    return 0


def cmd_scheduler_restart(args) -> int:
    """Restart the scheduler (stop and start)."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    print("🔄 Restarting scheduler...")

    # Stop the scheduler
    stop_args = argparse.Namespace(
        force=args.force, timeout=getattr(args, "timeout", None) or 60.0
    )
    result = cmd_scheduler_stop(stop_args)

    if result != 0:
        print("❌ Failed to stop scheduler, cannot restart")
        return result

    # Brief pause to ensure clean shutdown
    time.sleep(2)

    # Start the scheduler with same options as before
    # Note: This will use default settings. Users should manually start with custom options if needed.
    start_args = argparse.Namespace(
        max_workers=args.max_workers if hasattr(args, "max_workers") else 5,
        show_jobs=False,
        verbose=args.verbose if hasattr(args, "verbose") else False,
        stations=getattr(args, "stations", None),
        max_stations=getattr(args, "max_stations", None),
    )

    print("🚀 Starting scheduler...")
    return cmd_scheduler_start(start_args)


def create_scheduler_parser(subparsers):
    """Add scheduler subcommands to the main parser."""

    # Scheduler command group
    scheduler_parser = subparsers.add_parser(
        "scheduler",
        help="Manage bulk download scheduler",
        description="APScheduler-based bulk download system",
    )

    scheduler_subparsers = scheduler_parser.add_subparsers(
        dest="scheduler_command", help="Scheduler commands"
    )

    # Start command
    start_parser = scheduler_subparsers.add_parser(
        "start", help="Start the bulk download scheduler"
    )
    start_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum concurrent downloads (default: from scheduler.yaml, or 15)",
    )
    start_parser.add_argument(
        "--show-jobs",
        action="store_true",
        help="Show all scheduled jobs before starting",
    )
    start_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    start_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only schedule these specific stations (e.g., OLKE ELDC THOB)",
    )
    start_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations per session (for testing)",
    )
    start_parser.add_argument(
        "--only",
        type=str,
        help="Only run specific scheduler types (comma-separated). "
        "Options: health, 15s_24hr, 1Hz_1hr, status_1hr, downloads, all",
    )
    start_parser.set_defaults(func=cmd_scheduler_start)

    # Status command
    status_parser = scheduler_subparsers.add_parser(
        "status", help="Show scheduler status and jobs"
    )
    status_parser.add_argument(
        "--show-jobs", action="store_true", help="Show detailed job information"
    )
    status_parser.set_defaults(func=cmd_scheduler_status)

    # Config command
    config_parser = scheduler_subparsers.add_parser(
        "config", help="Manage scheduler configuration"
    )
    config_parser.add_argument(
        "--create", action="store_true", help="Create default configuration file"
    )
    config_parser.add_argument(
        "--show", action="store_true", help="Show current configuration"
    )
    config_parser.set_defaults(func=cmd_scheduler_config)

    # Test command
    test_parser = scheduler_subparsers.add_parser(
        "test", help="Test scheduler setup without starting"
    )
    test_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only test these specific stations (e.g., OLKE ELDC THOB)",
    )
    test_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations per session (for testing)",
    )
    test_parser.add_argument(
        "--only",
        type=str,
        help="Only test specific scheduler types (comma-separated). "
        "Options: health, 15s_24hr, 1Hz_1hr, status_1hr, downloads, all",
    )
    test_parser.set_defaults(func=cmd_scheduler_test)

    # Stop command
    stop_parser = scheduler_subparsers.add_parser(
        "stop", help="Stop the running scheduler"
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Force immediate shutdown without waiting for active downloads",
    )
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help=(
            "Seconds to wait for a graceful shutdown before SIGKILL (default: 60). "
            "Keep this BELOW the systemd unit's TimeoutStopSec (120s) — if ExecStop "
            "outlives it, systemd SIGKILLs the whole cgroup and the scheduler never "
            "closes its jobstore cleanly."
        ),
    )
    stop_parser.set_defaults(func=cmd_scheduler_stop)

    # Restart command
    restart_parser = scheduler_subparsers.add_parser(
        "restart", help="Restart the scheduler (stop and start)"
    )
    restart_parser.add_argument(
        "--force",
        action="store_true",
        help="Force immediate shutdown without waiting for active downloads",
    )
    restart_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum concurrent downloads after restart (default: from scheduler.yaml, or 15)",
    )
    restart_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging after restart",
    )
    restart_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only schedule these specific stations after restart",
    )
    restart_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations per session after restart",
    )
    restart_parser.set_defaults(func=cmd_scheduler_restart)

    # Pipeline status command
    pipeline_parser = scheduler_subparsers.add_parser(
        "pipeline-status", help="Show pipeline job status and history"
    )
    pipeline_parser.add_argument("--station", type=str, help="Filter by station ID")
    pipeline_parser.add_argument(
        "--session",
        type=str,
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Filter by session type",
    )
    pipeline_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to show (default: 20)",
    )
    pipeline_parser.set_defaults(func=cmd_scheduler_pipeline_status)

    # Load status command
    load_parser = scheduler_subparsers.add_parser(
        "load-status", help="Show current system load and throttling status"
    )
    load_parser.set_defaults(func=cmd_scheduler_load_status)

    # Bootstrap command
    bootstrap_parser = scheduler_subparsers.add_parser(
        "bootstrap", help="Trigger bootstrap (cold-start) downloads"
    )
    bootstrap_parser.add_argument(
        "--days", type=int, default=3, help="Number of days to look back (default: 3)"
    )
    bootstrap_parser.add_argument(
        "--stations", nargs="+", help="Only bootstrap these specific stations"
    )
    bootstrap_parser.add_argument(
        "--window",
        type=int,
        default=10,
        help="Distribution window in minutes (default: 10)",
    )
    bootstrap_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    bootstrap_parser.set_defaults(func=cmd_scheduler_bootstrap)

    # Backfill status command
    backfill_status_parser = scheduler_subparsers.add_parser(
        "backfill-status", help="Show backfill progress and priority queue"
    )
    backfill_status_parser.add_argument(
        "--session",
        type=str,
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Filter by session type",
    )
    backfill_status_parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximum number of entries to show (default: 30)",
    )
    backfill_status_parser.set_defaults(func=cmd_scheduler_backfill_status)

    # Gaps command
    gaps_parser = scheduler_subparsers.add_parser(
        "gaps", help="Find gaps in downloaded files"
    )
    gaps_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only check these specific stations (e.g., OLKE ELDC THOB)",
    )
    gaps_parser.add_argument(
        "--max-stations", type=int, help="Maximum number of stations to check"
    )
    gaps_parser.add_argument(
        "--session",
        type=str,
        default="15s_24hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Session type to check (default: 15s_24hr)",
    )
    gaps_parser.add_argument(
        "--days", type=int, default=7, help="Number of days to check (default: 7)"
    )
    gaps_parser.add_argument(
        "--summary",
        action="store_true",
        help="Show summary across all stations instead of per-station details",
    )
    gaps_parser.add_argument(
        "--include-missing",
        action="store_true",
        help="Include files known to be missing on receiver",
    )
    gaps_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed gap information"
    )
    gaps_parser.set_defaults(func=cmd_scheduler_gaps)

    # Backfill command
    backfill_parser = scheduler_subparsers.add_parser(
        "backfill", help="Manually trigger backfill for a session type"
    )
    backfill_parser.add_argument(
        "--session",
        type=str,
        default="status_1hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Session type to backfill (default: status_1hr)",
    )
    backfill_parser.add_argument(
        "--days", type=int, default=30, help="Number of days to backfill (default: 30)"
    )
    backfill_parser.add_argument(
        "--stations", nargs="+", help="Only backfill these specific stations"
    )
    backfill_parser.add_argument(
        "--max-stations", type=int, help="Maximum number of stations to process"
    )
    backfill_parser.add_argument(
        "--archiving-mode",
        type=str,
        default="bulk",
        choices=["bulk", "immediate"],
        help="Archiving mode: bulk (download all then archive) or immediate (default: bulk)",
    )
    backfill_parser.set_defaults(func=cmd_scheduler_backfill)

    # Long-term gap classification (read-only; design #136)
    lt_gaps_parser = scheduler_subparsers.add_parser(
        "long-term-gaps",
        help="Classify a station's long-term gap (queued vs confirmed-gone) from the DB",
    )
    lt_gaps_parser.add_argument(
        "--stations", nargs="+", required=True, help="Station id(s), e.g. SARP KOSK"
    )
    lt_gaps_parser.add_argument(
        "--session",
        default="15s_24hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
    )
    lt_gaps_parser.add_argument(
        "--lookback", type=int, default=365, help="Hard cap on days to look back"
    )
    lt_gaps_parser.set_defaults(func=cmd_scheduler_long_term_gaps)

    # Long-term backfill — recover classified gaps through the full pipeline (#136)
    lt_bf_parser = scheduler_subparsers.add_parser(
        "long-term-backfill",
        help="Recover a station's classified gaps (download+RINEX+archive+DB)",
    )
    lt_bf_parser.add_argument("--stations", nargs="+", required=True)
    lt_bf_parser.add_argument(
        "--session", default="15s_24hr", choices=["15s_24hr", "1Hz_1hr", "status_1hr"]
    )
    lt_bf_parser.add_argument("--lookback", type=int, default=365)
    lt_bf_parser.add_argument(
        "--max-days", type=int, help="Cap days to recover this run"
    )
    lt_bf_parser.add_argument(
        "--no-rinex", action="store_true", help="Raw only (skip RINEX)"
    )
    lt_bf_parser.add_argument(
        "--dry-run", action="store_true", help="Classify only; no downloads"
    )
    lt_bf_parser.set_defaults(func=cmd_scheduler_long_term_backfill)

    # Reconcile command
    reconcile_parser = scheduler_subparsers.add_parser(
        "reconcile", help="Reconcile SBF archives with RINEX output"
    )
    reconcile_parser.add_argument(
        "--days", type=int, default=30, help="Number of days to scan (default: 30)"
    )
    reconcile_parser.add_argument(
        "--stations", nargs="+", help="Only check these specific stations"
    )
    reconcile_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be converted without actually converting",
    )
    reconcile_parser.set_defaults(func=cmd_scheduler_reconcile)

    # Integrity check command
    integrity_parser = scheduler_subparsers.add_parser(
        "integrity", help="Run file integrity checks on archived data"
    )
    integrity_parser.add_argument(
        "--session",
        type=str,
        default="all",
        help="Session type to check, or 'all' (default: all)",
    )
    integrity_parser.add_argument(
        "--days", type=int, default=7, help="Number of days to check (default: 7)"
    )
    integrity_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only check these specific stations (e.g., ENTC ELDC)",
    )
    integrity_parser.add_argument(
        "--no-receiver",
        action="store_true",
        help="Skip remote receiver comparison (FTP SIZE / HTTP Content-Length)",
    )
    integrity_parser.add_argument(
        "--tolerance",
        type=float,
        default=50.0,
        help="Size deviation tolerance percentage (default: 50.0)",
    )
    integrity_parser.set_defaults(func=cmd_scheduler_integrity)

    horizon_parser = scheduler_subparsers.add_parser(
        "horizon-probe",
        help="Probe receivers for their oldest-held file (dynamic fetch floor)",
    )
    horizon_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only probe these stations (default: all active stations)",
    )
    horizon_parser.add_argument(
        "--session",
        default=None,
        help="Restrict to one session type (default: the receiver's seeded sessions)",
    )
    horizon_parser.set_defaults(func=cmd_scheduler_horizon_probe)

    clean_stale_parser = scheduler_subparsers.add_parser(
        "clean-stale-tmp",
        help="Delete stale partial downloads from tmp (older than --hours)",
    )
    clean_stale_parser.add_argument(
        "--session",
        default="15s_24hr",
        help="Session type to clean (default: 15s_24hr)",
    )
    clean_stale_parser.add_argument(
        "--hours",
        type=float,
        default=4.0,
        help="Files older than this many hours are considered stale (default: 4)",
    )
    clean_stale_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without removing anything",
    )
    clean_stale_parser.set_defaults(func=cmd_scheduler_clean_stale_tmp)

    return scheduler_parser


# Handle scheduler subcommands
def handle_scheduler_command(args) -> int:
    """Handle scheduler subcommands."""

    if not hasattr(args, "scheduler_command") or not args.scheduler_command:
        print("❌ No scheduler command specified")
        print(
            "Available commands: start, stop, restart, status, config, test, gaps, backfill, backfill-status, reconcile, integrity, horizon-probe, pipeline-status, load-status, bootstrap"
        )
        return 1

    return args.func(args)


if __name__ == "__main__":
    # Direct CLI testing
    parser = argparse.ArgumentParser(description="GPS Receiver Scheduler")
    subparsers = parser.add_subparsers(dest="command")

    create_scheduler_parser(subparsers)

    args = parser.parse_args()

    if args.command == "scheduler":
        sys.exit(handle_scheduler_command(args))
    else:
        parser.print_help()
        sys.exit(1)
