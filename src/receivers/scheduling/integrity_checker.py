"""Periodic file integrity checker for GPS archive.

Validates downloaded/archived files by:
1. Finding untracked files (on disk but not in file_tracking DB)
2. Size consistency check (flag files deviating from station/session median)
3. Remote receiver comparison for flagged files (FTP SIZE / HTTP Content-Length)

Runs on the 'backfill' executor at a configurable interval (default every 6h).
"""

import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from .lookback import Lookback

if TYPE_CHECKING:
    from ..health.file_tracker import ArchiveFileChecker, FileTracker

logger = logging.getLogger("receivers.scheduler.integrity")


def _run_integrity_check_job(
    session_types: List[str],
    days_back: int = 7,
    check_receiver: bool = True,
    size_tolerance_pct: float = 50.0,
    station_filter: Optional[List[str]] = None,
    hash_fill_limit: int = 1000,
    check_identity: bool = True,
    identity_sessions: Optional[List[str]] = None,
    lookback: Optional[Lookback] = None,
) -> None:
    """APScheduler job: periodic file integrity check.

    For each active station and session type, scans the last N days:
    1. Find untracked files (on disk, not in DB) and register them
    2. Size consistency check (flag outliers vs median)
    3. For flagged files only: compare against receiver (FTP SIZE)

    Then a global phase (not date-windowed):
    4. Content-hash fill — lazily compute file_tracking.content_sha256 for
       present files that lack it (Option B: keep the hot download/archive path
       hash-free). Bounded by ``hash_fill_limit`` per run.

    When ``check_identity`` is set, a report-only header probe also runs per
    station/session over the archived RINEX in the window, flagging stray files
    (position closest to a different station) and stacked/multi-document files.
    Findings are logged (never auto-repaired — relocation/re-rinex need eyes).

    Args:
        session_types: Session types to check (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday
        check_receiver: Whether to do FTP SIZE comparison for flagged files
        size_tolerance_pct: Flag files deviating more than this % from median
        station_filter: If set, only check these station IDs (CLI use)
        hash_fill_limit: Max files to content-hash per run (0 disables the phase)
        check_identity: Run the report-only stray/stacked header probe
        identity_sessions: Sessions the identity probe runs on. Default (None)
            = daily sessions only (those NOT ending in '1hr'): the same
            stray/stacked signal appears on the daily product, and probing
            hourly (1Hz_1hr = 24x the files) every run is wasteful.
    """
    try:
        from ..cli.main import get_all_station_configs
        from ..health.file_tracker import ArchiveFileChecker, FileTracker
    except ImportError as e:
        logger.debug(f"Integrity checker dependencies not available: {e}")
        return

    start_time = time.time()
    lb = lookback or Lookback(days_back, "days")
    logger.info(
        f"Starting integrity check: {lb.describe(session_types)}, "
        f"sessions={session_types}, tolerance={size_tolerance_pct}%"
    )

    # Get active stations
    all_stations = get_all_station_configs()
    active_stations = {
        sid: cfg
        for sid, cfg in all_stations.items()
        if cfg.get("enabled", True)
        and cfg.get("station_status") not in ("discontinued", "inactive")
    }

    # Apply station filter if provided (CLI mode)
    if station_filter:
        active_stations = {
            sid: cfg for sid, cfg in active_stations.items() if sid in station_filter
        }

    if not active_stations:
        logger.warning("No active stations found for integrity check")
        return

    # Date range. end_date is shared; the start is per-session because a
    # files_back window means hours for an hourly session and days for a daily
    # one — see the loop below.
    end_date = date.today() - timedelta(days=1)

    checker = ArchiveFileChecker()
    tracker = FileTracker()
    if not tracker.connect():
        logger.warning("Cannot connect to database for integrity check")
        return

    # Resolve fleet geometry + position gate ONCE for the whole run (the
    # report-only identity probe). Fail-open: if cfg is unreadable the probe
    # is skipped rather than aborting the size/hash checks.
    fleet: Dict[str, Any] = {}
    gate_m = 10.0
    # Identity probe scope: daily sessions only by default. Hourly (1Hz_1hr) is
    # ~24x the file volume and carries no extra stray/stacked signal over the
    # daily product, so re-decompressing it every run is wasteful.
    if identity_sessions is None:
        identity_sessions = [s for s in session_types if not s.endswith("1hr")]
    identity_set = set(identity_sessions) if check_identity else set()
    if identity_set:
        try:
            from ..archive.sort import fleet_coordinates, resolve_position_gate_m

            fleet = fleet_coordinates()
            gate_m = resolve_position_gate_m()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"identity probe disabled (fleet/gate unavailable): {e}")
            identity_set = set()

    total_untracked = 0
    total_registered = 0
    total_suspect = 0
    total_checked = 0
    total_stray = 0
    total_stacked = 0

    try:
        for session_type in session_types:
            start_date = end_date - timedelta(days=lb.date_span_days(session_type) - 1)
            for station_id in sorted(active_stations):
                try:
                    result = _check_station_session(
                        station_id,
                        session_type,
                        start_date,
                        end_date,
                        checker,
                        tracker,
                        active_stations[station_id],
                        check_receiver=check_receiver,
                        size_tolerance_pct=size_tolerance_pct,
                        fleet=fleet if session_type in identity_set else None,
                        gate_m=gate_m,
                    )
                    total_untracked += result["untracked"]
                    total_registered += result["registered"]
                    total_suspect += result["suspect"]
                    total_checked += result["checked"]
                    total_stray += result.get("stray", 0)
                    total_stacked += result.get("stacked", 0)
                except Exception as e:
                    logger.debug(
                        f"Integrity check failed for {station_id}/{session_type}: {e}"
                    )

        # Phase 4 (global, not date-windowed): lazily fill content_sha256.
        hash_stats = {"hashed": 0, "missing": 0, "corrupt": 0}
        if hash_fill_limit > 0:
            hash_stats = _run_content_hash_fill(tracker, checker, hash_fill_limit)
    finally:
        tracker.close()

    duration = time.time() - start_time
    logger.info(
        f"Integrity check complete in {duration:.1f}s: "
        f"{total_checked} files checked, {total_untracked} untracked found, "
        f"{total_registered} registered, {total_suspect} suspect, "
        f"{hash_stats['hashed']} content-hashed, "
        f"{total_stray} stray, {total_stacked} stacked"
    )


def _run_content_hash_fill(
    tracker: "FileTracker",
    checker: "ArchiveFileChecker",
    limit: int = 1000,
) -> Dict[str, int]:
    """Lazily compute content_sha256 for present files that lack it (mig 052).

    Option B of the integrity design: the hot download/archive path does NOT
    hash. This phase fills ``file_tracking.content_sha256`` for
    'downloaded'/'archived' rows where it is NULL — newest first, bounded by
    ``limit`` per run. ``content_hashed_at`` orders the scan (NULLs first) and,
    once the backlog is drained, lets a future re-hash detect local bit-rot.

    Raw is immutable, so its hash is write-once. RINEX rows are filled here too;
    a later header-fix workflow must reset content_sha256 to NULL so the new
    content gets re-hashed — this phase never overwrites a non-NULL hash.

    Returns counts: {hashed, missing, corrupt}.
    """
    result = {"hashed": 0, "missing": 0, "corrupt": 0}
    if not tracker._conn:
        return result

    from ..utils.content_hash import CorruptArchiveFileError, content_sha256

    try:
        with tracker.read_cursor() as cur:
            cur.execute(
                """SELECT sid, session_type, file_date, file_hour, filename
                FROM file_tracking
                WHERE status IN ('downloaded', 'archived')
                  AND content_sha256 IS NULL
                  AND filename IS NOT NULL
                ORDER BY content_hashed_at NULLS FIRST, file_date DESC
                LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    except Exception as e:
        # Most likely the migration 052 columns are absent — skip quietly.
        logger.debug(f"content-hash fill query failed (mig 052 applied?): {e}")
        return result

    for sid, session_type, file_date, file_hour, filename in rows:
        dt = datetime.combine(file_date, datetime.min.time())
        archive_dir = checker.get_archive_directory(
            sid,
            session_type,
            year=file_date.year,
            month=dt.strftime("%b").lower(),
        )
        path = os.path.join(archive_dir, filename)

        if not os.path.isfile(path):
            # Local file gone (pruned past retention, or never landed) — leave
            # the hash NULL; a later run retries if the file reappears.
            result["missing"] += 1
            continue

        try:
            digest = content_sha256(path)
        except CorruptArchiveFileError as exc:
            result["corrupt"] += 1
            logger.warning(f"content-hash: corrupt local file {path}: {exc}")
            tracker.mark_file_suspect(
                sid,
                session_type,
                file_date,
                file_hour,
                reason=f"content_sha256 failed: {exc}",
            )
            continue
        except OSError as exc:
            result["missing"] += 1
            logger.debug(f"content-hash: cannot read {path}: {exc}")
            continue

        try:
            with tracker._conn.cursor() as cur:
                if file_hour is None:
                    cur.execute(
                        """UPDATE file_tracking
                        SET content_sha256 = %s, content_hashed_at = NOW(),
                            updated_at = NOW()
                        WHERE sid = %s AND session_type = %s
                          AND file_date = %s AND file_hour IS NULL""",
                        (digest, sid, session_type, file_date),
                    )
                else:
                    cur.execute(
                        """UPDATE file_tracking
                        SET content_sha256 = %s, content_hashed_at = NOW(),
                            updated_at = NOW()
                        WHERE sid = %s AND session_type = %s
                          AND file_date = %s AND file_hour = %s""",
                        (digest, sid, session_type, file_date, file_hour),
                    )
            tracker._conn.commit()
            result["hashed"] += 1
        except Exception as e:
            logger.debug(f"content-hash update failed for {path}: {e}")
            if tracker._conn:
                tracker._conn.rollback()

    if result["hashed"] or result["corrupt"]:
        logger.info(
            f"Content-hash fill: {result['hashed']} hashed, "
            f"{result['missing']} missing, {result['corrupt']} corrupt "
            f"(limit {limit})"
        )
    return result


def _check_station_session(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    tracker: "FileTracker",
    station_config: Dict[str, Any],
    check_receiver: bool = True,
    size_tolerance_pct: float = 50.0,
    fleet: Optional[Dict[str, Any]] = None,
    gate_m: float = 10.0,
) -> Dict[str, int]:
    """Check integrity for a single station/session combination.

    Returns:
        Dict with counts: untracked, registered, suspect, checked, stray, stacked
    """
    result = {
        "untracked": 0,
        "registered": 0,
        "suspect": 0,
        "checked": 0,
        "stray": 0,
        "stacked": 0,
    }

    receiver_type = station_config.get("receiver_type", "").lower() or None

    # Phase 1: Scan archive for untracked files
    untracked = _find_untracked_files(
        station_id,
        session_type,
        start_date,
        end_date,
        checker,
        tracker,
        receiver_type=receiver_type,
    )
    result["untracked"] = len(untracked)

    # Register untracked files that pass validation
    from ..utils.archive_validator import ArchiveValidator

    validator = ArchiveValidator()

    for file_path, file_date, file_hour in untracked:
        try:
            if validator.validate_archived_file(Path(file_path)):
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                tracker.mark_file_archived(
                    station_id,
                    session_type,
                    file_date,
                    file_hour,
                    filename,
                    file_size,
                )
                tracker.mark_integrity_checked(
                    station_id,
                    session_type,
                    file_date,
                    file_hour,
                )
                result["registered"] += 1
                logger.debug(f"Registered untracked file: {file_path}")
            else:
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                tracker.mark_file_suspect(
                    station_id,
                    session_type,
                    file_date,
                    file_hour,
                    filename,
                    file_size,
                    reason="untracked file failed archive validation",
                )
                result["suspect"] += 1
                logger.warning(
                    f"Suspect untracked file (validation failed): {file_path}"
                )
        except Exception as e:
            logger.debug(f"Error processing untracked file {file_path}: {e}")

    # Phase 2: Size consistency check for tracked files
    flagged = _size_consistency_check(
        station_id,
        session_type,
        start_date,
        end_date,
        tracker,
        tolerance_pct=size_tolerance_pct,
    )
    result["checked"] = flagged["total_checked"]

    if flagged["outliers"] and check_receiver:
        # Phase 3: Remote receiver comparison for outliers only
        for file_date, file_hour, file_size, median_size in flagged["outliers"]:
            try:
                remote_size = _get_remote_file_size(
                    station_id,
                    session_type,
                    file_date,
                    file_hour,
                    station_config,
                )
                if remote_size is None:
                    # File no longer on receiver — mark suspect but don't delete
                    tracker.mark_file_suspect(
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                        reason=f"size outlier ({file_size} vs median {median_size}), "
                        f"no longer on receiver",
                    )
                    result["suspect"] += 1
                elif abs(remote_size - file_size) < 100:
                    # Sizes match (within rounding) — file is OK
                    tracker.mark_integrity_checked(
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                    )
                else:
                    # Sizes differ — mark suspect
                    tracker.mark_file_suspect(
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                        reason=f"size mismatch: local={file_size}, "
                        f"remote={remote_size}, median={median_size}",
                    )
                    result["suspect"] += 1
            except Exception as e:
                logger.debug(
                    f"Remote check failed for {station_id}/{session_type}/"
                    f"{file_date}: {e}"
                )
    elif not flagged["outliers"]:
        # No outliers — mark all checked files as integrity verified
        _mark_all_integrity_checked(
            station_id,
            session_type,
            start_date,
            end_date,
            tracker,
        )

    # Phase 5 (report-only): stray / stacked header probe on RINEX in the window.
    if fleet is not None:
        try:
            probe = _identity_stacked_check(
                station_id, session_type, start_date, end_date, checker, fleet, gate_m
            )
            result["stray"] = probe["stray"]
            result["stacked"] = probe["stacked"]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"identity probe failed for {station_id}/{session_type}: {e}")

    return result


def _identity_stacked_check(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    fleet: Dict[str, Any],
    gate_m: float,
) -> Dict[str, int]:
    """Report-only probe: flag stray + stacked RINEX in the window.

    Walks the ``…/<STA>/<session>/rinex/`` directory for each month spanned by
    the window and probes the daily RINEX header (no ``teqc``). Findings are
    logged under ``receivers.scheduler.integrity`` (the monitoring channel);
    nothing is moved, deleted, or re-rinexed — remediation needs eyes.
    """
    from ..archive.file_identity import probe_rinex_file

    result = {"stray": 0, "stacked": 0}

    # RINEX lives under the '<session>_rinex' path (rinex/ subdir).
    rinex_session = (
        session_type if session_type.endswith("_rinex") else f"{session_type}_rinex"
    )

    unique_months: Set[Tuple[int, str]] = set()
    current = start_date
    while current <= end_date:
        dt = datetime.combine(current, datetime.min.time())
        unique_months.add((current.year, dt.strftime("%b").lower()))
        current += timedelta(days=1)

    from ..utils.download_tracker import parse_date_from_filename

    for year, month in unique_months:
        rinex_dir = checker.get_archive_directory(
            station_id, rinex_session, year=year, month=month
        )
        if not os.path.isdir(rinex_dir):
            continue
        for entry in sorted(os.listdir(rinex_dir)):
            if not entry.startswith(station_id):
                continue
            path = os.path.join(rinex_dir, entry)
            if not os.path.isfile(path):
                continue
            # Keep to the date window (the dir is a whole month).
            parsed = parse_date_from_filename(entry, station_id)
            if parsed is not None:
                fdate = parsed[0]
                if fdate < start_date or fdate > end_date:
                    continue
            for f in probe_rinex_file(
                Path(path), station_id, fleet=fleet, gate_m=gate_m
            ):
                result[f.kind] = result.get(f.kind, 0) + 1
                logger.warning("integrity[%s]: %s — %s", f.kind, f.rel, f.detail)

    return result


def _find_untracked_files(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    tracker: "FileTracker",
    receiver_type: Optional[str] = None,
) -> List[Tuple[str, date, Optional[int]]]:
    """Find files on disk that have no file_tracking entry.

    Returns:
        List of (file_path, file_date, file_hour) tuples
    """
    import glob as glob_mod

    untracked = []

    # Generate all dates and check archive directories
    unique_months: Set[Tuple[int, str]] = set()
    current = start_date
    while current <= end_date:
        dt = datetime.combine(current, datetime.min.time())
        unique_months.add((current.year, dt.strftime("%b").lower()))
        current += timedelta(days=1)

    for year, month in unique_months:
        archive_dir = checker.get_archive_directory(
            station_id,
            session_type,
            year=year,
            month=month,
        )
        if not os.path.isdir(archive_dir):
            continue

        pattern = os.path.join(archive_dir, f"{station_id}*")
        for filepath in glob_mod.glob(pattern):
            if not os.path.isfile(filepath):
                continue

            fsize = os.path.getsize(filepath)
            if fsize < 50:  # MIN_ARCHIVE_FILE_SIZE
                continue

            filename = os.path.basename(filepath)

            # Parse date from filename
            from ..utils.download_tracker import parse_date_from_filename

            parsed = parse_date_from_filename(filename, station_id)
            if parsed is None:
                continue

            file_date, file_hour = parsed

            # Check date range
            if file_date < start_date or file_date > end_date:
                continue

            # Check if already tracked
            if _is_file_tracked(
                tracker, station_id, session_type, file_date, file_hour
            ):
                continue

            untracked.append((filepath, file_date, file_hour))

    return untracked


def _is_file_tracked(
    tracker: "FileTracker",
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
) -> bool:
    """Check if a file has a file_tracking entry (any status)."""
    if not tracker._conn:
        return False

    try:
        with tracker.read_cursor() as cur:
            if file_hour is None:
                cur.execute(
                    """SELECT 1 FROM file_tracking
                    WHERE sid = %s AND session_type = %s
                    AND file_date = %s AND file_hour IS NULL""",
                    (station_id, session_type, file_date),
                )
            else:
                cur.execute(
                    """SELECT 1 FROM file_tracking
                    WHERE sid = %s AND session_type = %s
                    AND file_date = %s AND file_hour = %s""",
                    (station_id, session_type, file_date, file_hour),
                )
            return cur.fetchone() is not None
    except Exception:
        return False


def _size_consistency_check(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    tracker: "FileTracker",
    tolerance_pct: float = 50.0,
) -> Dict[str, Any]:
    """Check file sizes against median for the station/session.

    Returns:
        Dict with:
        - total_checked: number of files checked
        - outliers: list of (file_date, file_hour, file_size, median_size) tuples
    """
    result: Dict[str, Any] = {"total_checked": 0, "outliers": []}

    if not tracker._conn:
        return result

    try:
        with tracker.read_cursor() as cur:
            # Get all file sizes for this station/session in the date range
            cur.execute(
                """SELECT file_date, file_hour, file_size
                FROM file_tracking
                WHERE sid = %s AND session_type = %s
                AND file_date >= %s AND file_date <= %s
                AND status IN ('downloaded', 'archived')
                AND file_size IS NOT NULL AND file_size > 0
                ORDER BY file_date, file_hour""",
                (station_id, session_type, start_date, end_date),
            )
            rows = cur.fetchall()

        if not rows:
            return result

        result["total_checked"] = len(rows)

        # Compute median file size
        sizes = [row[2] for row in rows]
        if len(sizes) < 3:
            # Too few files to compute meaningful median
            return result

        median_size = statistics.median(sizes)
        if median_size == 0:
            return result

        # Find outliers
        threshold = tolerance_pct / 100.0
        for file_date, file_hour, file_size in rows:
            deviation = abs(file_size - median_size) / median_size
            if deviation > threshold:
                result["outliers"].append(
                    (file_date, file_hour, file_size, int(median_size))
                )

        if result["outliers"]:
            logger.info(
                f"{station_id}/{session_type}: {len(result['outliers'])} size outliers "
                f"(median={int(median_size):,}, tolerance={tolerance_pct}%)"
            )

    except Exception as e:
        logger.debug(
            f"Size consistency check failed for {station_id}/{session_type}: {e}"
        )

    return result


def _get_remote_file_size(
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
    station_config: Dict[str, Any],
) -> Optional[int]:
    """Get file size from receiver via FTP SIZE or HTTP Content-Length.

    Cheap operation — no data transfer, just a SIZE check.

    Returns:
        Remote file size in bytes, or None if file not found/unavailable
    """
    receiver_type = station_config.get("receiver_type", "").lower()

    if receiver_type in ("polarx5", "mosaic-x5", ""):
        return _get_remote_size_ftp(
            station_id, session_type, file_date, file_hour, station_config
        )
    elif receiver_type in ("netr9", "netr5", "netrs"):
        return _get_remote_size_http(
            station_id, session_type, file_date, file_hour, station_config
        )
    elif receiver_type == "g10":
        return _get_remote_size_ftp(
            station_id, session_type, file_date, file_hour, station_config
        )
    else:
        logger.debug(f"Unknown receiver type for remote size check: {receiver_type}")
        return None


def _get_remote_size_ftp(
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
    station_config: Dict[str, Any],
) -> Optional[int]:
    """Get file size via FTP SIZE command."""
    from ftplib import FTP

    try:
        ip = station_config.get("router", {}).get("ip")
        if not ip:
            return None

        receiver_config = station_config.get("receiver", {})
        ftp_port = int(receiver_config.get("ftpport", 2160))

        ftp = None
        ftp = FTP()
        ftp.connect(ip, ftp_port, timeout=15)
        ftp.login()

        # Build remote path based on receiver type
        receiver_type = station_config.get("receiver_type", "").lower()
        if receiver_type == "g10":
            # Leica G10 uses SD Card path
            doy = (file_date - date(file_date.year, 1, 1)).days + 1
            session_letter = "a"  # daily
            if file_hour is not None and file_hour > 0:
                session_letter = chr(ord("a") + file_hour)
            remote_file = f"/SD Card/Data/{session_type}/{station_id}{doy:03d}{session_letter}.m00.zip"
        else:
            # PolaRX5 uses GPS week directories
            try:
                import gtimes.timefunc as gt

                dt = datetime.combine(file_date, datetime.min.time())
                if file_hour is not None:
                    dt = dt.replace(hour=file_hour)
                gps_week = gt.date2gpsWeek(dt)[0]

                session_map = {
                    "15s_24hr": ("a", "15s_24hr"),
                    "1Hz_1hr": ("b", "1Hz_1hr"),
                    "status_1hr": ("c", "status_1hr"),
                }
                letter, session_path = session_map.get(
                    session_type, ("a", session_type)
                )

                if "24hr" in session_type:
                    file_name = (
                        f"{station_id}{dt.strftime('%Y%m%d')}0000{letter}.sbf.gz"
                    )
                else:
                    file_name = (
                        f"{station_id}{dt.strftime('%Y%m%d%H')}00{letter}.sbf.gz"
                    )

                remote_file = f"/DSK1/SSN/{session_path}/{gps_week:05d}/{file_name}"
            except ImportError:
                logger.debug("gtimes not available for remote path construction")
                ftp.quit()
                return None

        try:
            size = ftp.size(remote_file)
            ftp.quit()
            return size
        except Exception:
            ftp.quit()
            return None

    except Exception as e:
        # Close FTP socket if connect() succeeded but a later call raised
        if ftp is not None:
            try:
                ftp.close()
            except Exception:
                pass
        logger.debug(f"FTP SIZE check failed for {station_id}: {e}")
        return None


def _get_remote_size_http(
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
    station_config: Dict[str, Any],
) -> Optional[int]:
    """Get file size via HTTP HEAD / Content-Length."""
    try:
        import requests

        ip = station_config.get("router", {}).get("ip")
        if not ip:
            return None

        receiver_config = station_config.get("receiver", {})
        http_port = int(receiver_config.get("httpport", 80))

        # Build remote path for Trimble
        if file_hour is not None:
            date_str = f"{file_date.strftime('%Y%m%d')}{file_hour:02d}00"
        else:
            date_str = f"{file_date.strftime('%Y%m%d')}0000"

        receiver_type = station_config.get("receiver_type", "").lower()
        if receiver_type == "netrs":
            ext = ".T00"
        else:
            ext = ".T02"

        filename = f"{station_id}{date_str}a{ext}"
        month_dir = file_date.strftime("%Y%m")
        remote_path = f"/Internal/{month_dir}/{session_type}/{filename}"
        url = f"http://{ip}:{http_port}/download{remote_path}"

        response = requests.head(url, timeout=15)
        if response.status_code == 200:
            content_length = response.headers.get("content-length")
            if content_length:
                return int(content_length)

        return None

    except Exception as e:
        logger.debug(f"HTTP HEAD check failed for {station_id}: {e}")
        return None


def _mark_all_integrity_checked(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    tracker: "FileTracker",
) -> None:
    """Mark all tracked files in range as integrity checked (batch update)."""
    if not tracker._conn:
        return

    try:
        with tracker._conn.cursor() as cur:
            cur.execute(
                """UPDATE file_tracking
                SET integrity_checked_at = NOW(), updated_at = NOW()
                WHERE sid = %s AND session_type = %s
                AND file_date >= %s AND file_date <= %s
                AND status IN ('downloaded', 'archived')
                AND integrity_checked_at IS NULL""",
                (station_id, session_type, start_date, end_date),
            )
            updated = cur.rowcount
        tracker._conn.commit()
        if updated > 0:
            logger.debug(
                f"Marked {updated} files as integrity checked: "
                f"{station_id}/{session_type}"
            )
    except Exception as e:
        logger.debug(f"Batch integrity mark failed: {e}")
        if tracker._conn:
            tracker._conn.rollback()
