"""Backfill status_1hr health data from PolaRX5 receivers.

Downloads historical status_1hr SBF files, extracts health metrics via RxTools,
and writes them to the PostgreSQL database. Progress is tracked in the
backfill_progress table so the process can resume after restarts.

Designed to run as a low-priority scheduled job alongside live downloads,
processing one day per station per invocation to limit FTP session duration.
"""

import logging
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("receivers.scheduler.backfill")

# Partial downloads older than this are dead (connection died, job was killed).
# FTP REST resume won't help — delete so the next attempt starts clean.
_STALE_TMP_HOURS = 4


def clean_stale_tmp(
    session_type: str,
    stale_hours: float = _STALE_TMP_HOURS,
) -> Tuple[int, List[str]]:
    """Delete partial tmp files that are too old to resume.

    Scans {tmp_dir}/*/{session_type}/ for files older than ``stale_hours``
    and removes them so the next backfill / live-download attempt starts a
    fresh FTP session instead of trying to resume a dead one.

    Returns:
        (count_deleted, list_of_station_ids_affected)
    """
    try:
        from ..config.receivers_config import ReceiversConfig

        tmp_root = Path(ReceiversConfig().get_tmp_dir())
    except Exception as e:
        logger.warning(f"clean_stale_tmp: cannot resolve tmp_dir: {e}")
        return 0, []

    deleted = 0
    affected: List[str] = []

    # Glob: tmp_root/{STATION}/{session_type}/*
    for station_dir in tmp_root.iterdir():
        if not station_dir.is_dir():
            continue
        sess_dir = station_dir / session_type
        if not sess_dir.is_dir():
            continue
        for f in sess_dir.iterdir():
            if not f.is_file():
                continue
            try:
                age_h = (datetime.now().timestamp() - f.stat().st_mtime) / 3600
                if age_h >= stale_hours:
                    station_id = station_dir.name
                    logger.info(
                        f"Removing stale tmp: {station_id}/{session_type}/{f.name} "
                        f"({age_h:.1f}h old)"
                    )
                    f.unlink()
                    deleted += 1
                    if station_id not in affected:
                        affected.append(station_id)
            except PermissionError as e:
                logger.warning(f"Cannot remove {f}: {e}")
            except OSError:
                pass  # file may have been removed by a concurrent job

    if deleted:
        logger.info(
            f"Stale tmp cleanup ({session_type}): removed {deleted} file(s) "
            f"from {len(affected)} station(s): {' '.join(sorted(affected))}"
        )
    return deleted, sorted(affected)


def _pick_backfill_stations(session_type: str, limit: int, strategy: str) -> list:
    """Pick up to ``limit`` distinct pending stations for a session type.

    ``gap_priority`` orders by fewest archived files (biggest gaps); the
    default ``round_robin`` orders by least-recently-run. Returns a list of
    (sid, next_date, backfill_start, backfill_end) tuples (possibly empty).
    Because the backfill job runs single-instance (max_instances=1), the N
    returned stations are distinct and processed without cross-tick races.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    if strategy == "gap_priority":
        rows = _pick_station_by_gap_count(session_type, limit=limit)
        if rows:
            return rows

    with DatabaseConnectionFactory.connection(single_host=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sid, next_date, backfill_start, backfill_end
                FROM backfill_progress
                WHERE session_type = %s
                  AND status IN ('pending', 'in_progress')
                ORDER BY last_run ASC NULLS FIRST, sid
                LIMIT %s
            """,
                (session_type, limit),
            )
            return cur.fetchall()


def _enqueue_backfill(session_type: str, station_ids: list, days_back: int) -> int:
    """Queue stations with detected gaps into backfill_progress (self-refill).

    This is what makes gap_detection -> backfill self-sustaining: the worker
    only drains the queue, so without this the queue empties once and never
    refills. Called by the gap-detection job with the stations it found gaps
    for.

    Semantics (idempotent, safe to call every gap-detection run):
      * No row yet         -> INSERT a pending row covering the last days_back.
      * Row completed/failed -> RE-ACTIVATE it (status='pending') and widen its
        range to cover the gap window.
      * Row pending/in_progress -> LEFT UNTOUCHED (a backfill is already in
        flight for it; don't disturb its cursor).

    Returns the number of stations queued/refreshed.
    """
    if not station_ids:
        return 0
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO backfill_progress
                        (sid, session_type, backfill_start, next_date,
                         backfill_end, status)
                    VALUES (%s, %s, CURRENT_DATE - %s, CURRENT_DATE - %s,
                            CURRENT_DATE, 'pending')
                    ON CONFLICT (sid, session_type) DO UPDATE
                       SET next_date      = LEAST(backfill_progress.next_date,
                                                  EXCLUDED.next_date),
                           backfill_start = LEAST(backfill_progress.backfill_start,
                                                  EXCLUDED.backfill_start),
                           backfill_end   = GREATEST(backfill_progress.backfill_end,
                                                     EXCLUDED.backfill_end),
                           status         = 'pending',
                           updated_at     = NOW()
                     WHERE backfill_progress.status IN ('completed', 'failed')
                    """,
                    [(sid, session_type, days_back, days_back) for sid in station_ids],
                )
        logger.info(
            f"Backfill enqueue {session_type}: {len(station_ids)} station(s) "
            f"with gaps queued/refreshed"
        )
        return len(station_ids)
    except Exception as e:
        logger.warning(f"Backfill enqueue for {session_type} failed: {e}")
        return 0


def _backfill_next_station_for_session(
    session_type: str,
    window_start: int = 25,
    window_end: int = 55,
    archiving_mode: str = "bulk",
    run_rinex: bool = False,
    strategy: str = "round_robin",
    max_workers: int = 1,
) -> None:
    """Backfill up to ``max_workers`` stations concurrently for a session type.

    Self-gating: checks datetime.now().minute and returns immediately if
    outside the configured backfill window. Each picked station is processed
    for one day (its cursor advances in backfill_progress); the round-robin
    ordering cycles the fleet over successive ticks.

    Args:
        session_type: Session to backfill ('status_1hr', '1Hz_1hr', '15s_24hr')
        window_start: Minute of hour when backfill window opens (default 25)
        window_end: Minute of hour when backfill window closes (default 55)
        archiving_mode: 'bulk' (download all then archive) or 'immediate'
        run_rinex: Whether to run RINEX conversion after download
        strategy: 'round_robin' (by last_run) or 'gap_priority' (most gaps first)
        max_workers: How many stations to process concurrently per tick
    """
    # Self-gating: return immediately if outside backfill window
    now_minute = datetime.now().minute
    if not (window_start <= now_minute < window_end):
        logger.debug(
            f"Backfill {session_type}: outside window "
            f"(:{now_minute:02d} not in :{window_start:02d}-:{window_end:02d})"
        )
        return

    # Remove stale partial downloads before picking the next stations.
    # Files left from a killed/timed-out job can't be resumed — cleaning them
    # lets the next attempt start fresh instead of trying a dead FTP resume.
    clean_stale_tmp(session_type)

    use_immediate = archiving_mode != "bulk"

    def _process(row) -> None:
        station_id, current_dt, backfill_end = row[0], row[1], row[3]
        logger.info(f"Backfill {session_type}: {station_id} processing {current_dt}")
        try:
            has_more = _backfill_station_day_generic(
                station_id,
                current_dt,
                backfill_end,
                session_type,
                immediate_archive=use_immediate,
                run_rinex=run_rinex,
            )
            if not has_more:
                logger.info(f"Backfill {session_type} complete for {station_id}")
        except Exception as e:
            # One station's failure must not abort the rest of the batch.
            logger.error(
                f"Backfill {session_type} {station_id} error: "
                f"{type(e).__name__}: {e}"
            )

    try:
        limit = max(1, int(max_workers))
        rows = _pick_backfill_stations(session_type, limit, strategy)

        if not rows:
            logger.debug(f"No stations pending backfill for {session_type}")
            return

        if len(rows) == 1:
            _process(rows[0])
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(
                max_workers=len(rows), thread_name_prefix=f"bf-{session_type}"
            ) as ex:
                list(ex.map(_process, rows))

    except ImportError:
        logger.debug("psycopg2 not available - backfill disabled")
    except Exception as e:
        logger.error(f"Backfill {session_type} job error: {type(e).__name__}: {e}")


def _pick_station_by_gap_count(
    session_type: str,
    days_back: int = 30,
    limit: int = 1,
) -> list:
    """Pick the ``limit`` pending backfill stations with the most gaps.

    Joins backfill_progress (pending/in_progress) with file_tracking to count
    how many expected files are missing, returning the stations with the fewest
    archived files first (biggest gaps). Returns [] on failure.

    Args:
        session_type: Session type to check
        days_back: How many days back to count gaps
        limit: Max number of stations to return

    Returns:
        List of (sid, next_date, backfill_start, backfill_end) tuples.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH pending AS (
                        SELECT sid, next_date, backfill_start, backfill_end
                        FROM backfill_progress
                        WHERE session_type = %s
                          AND status IN ('pending', 'in_progress')
                    ),
                    gap_counts AS (
                        SELECT p.sid,
                               p.next_date,
                               p.backfill_start,
                               p.backfill_end,
                               COUNT(ft.id) AS archived_count
                        FROM pending p
                        LEFT JOIN file_tracking ft
                            ON ft.sid = p.sid
                           AND ft.session_type = %s
                           AND ft.status IN ('downloaded', 'archived')
                           AND ft.file_date >= CURRENT_DATE - %s
                        GROUP BY p.sid, p.next_date, p.backfill_start, p.backfill_end
                    )
                    SELECT sid, next_date, backfill_start, backfill_end
                    FROM gap_counts
                    ORDER BY archived_count ASC, sid
                    LIMIT %s
                """,
                    (session_type, session_type, days_back, limit),
                )

                return cur.fetchall()

    except Exception as e:
        logger.debug(f"Gap priority query failed for {session_type}: {e}")
        return []


def _backfill_station_day_generic(
    station_id: str,
    process_date: date,
    backfill_end: date,
    session_type: str,
    immediate_archive: bool = False,
    run_rinex: bool = False,
) -> bool:
    """Process one day of backfill for any session type.

    Downloads that day's files, optionally extracts health data (status_1hr only),
    optionally runs RINEX conversion (when run_rinex=True for 15s_24hr/1Hz_1hr),
    writes to DB, and advances the backfill_progress cursor.

    Args:
        station_id: Station identifier (e.g., 'ELDC')
        process_date: The date to process
        backfill_end: End date of backfill range
        session_type: Session type ('status_1hr', '1Hz_1hr', '15s_24hr')
        immediate_archive: If True, archive each file immediately.
                          If False (bulk), download all then archive.
        run_rinex: If True, run RINEX conversion after successful download.

    Returns:
        True if there's more work to do, False if backfill is complete
    """
    from ..health.database_factory import DatabaseConnectionFactory

    start_time = time.time()
    files_found = 0
    files_imported = 0
    files_missing = 0
    files_error = 0

    try:
        # Mark as in_progress
        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE backfill_progress
                    SET status = 'in_progress', updated_at = NOW()
                    WHERE sid = %s AND session_type = %s
                """,
                    (station_id, session_type),
                )

        # Download files for this single day
        result = _download_day_generic(
            station_id,
            process_date,
            session_type,
            immediate_archive=immediate_archive,
        )

        if result is None:
            files_error = 1
        else:
            status = result.get("status", "failed")
            downloaded_files = result.get("downloaded_files", [])
            files_downloaded = result.get("files_downloaded", 0)

            if status in ("failed", "unreachable"):
                files_error = 1
            elif files_downloaded == 0 and status in ("up_to_date", "completed"):
                files_missing = 1
            else:
                files_found = files_downloaded

                # Extract health data only for status_1hr
                if session_type == "status_1hr" and downloaded_files:
                    imported = _extract_and_store_health(
                        station_id, downloaded_files, logger
                    )
                    files_imported = imported

                # Run RINEX conversion for data sessions (15s_24hr, 1Hz_1hr)
                if run_rinex and downloaded_files and session_type != "status_1hr":
                    _run_backfill_rinex(station_id, session_type, downloaded_files)

        # Advance cursor
        next_date = process_date + timedelta(days=1)
        is_complete = next_date > backfill_end
        new_status = "completed" if is_complete else "in_progress"
        duration = time.time() - start_time

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE backfill_progress
                    SET next_date = %s,
                        status = %s,
                        files_found = files_found + %s,
                        files_imported = files_imported + %s,
                        files_missing = files_missing + %s,
                        files_error = files_error + %s,
                        last_run = NOW(),
                        last_duration_seconds = %s,
                        updated_at = NOW()
                    WHERE sid = %s AND session_type = %s
                """,
                    (
                        next_date,
                        new_status,
                        files_found,
                        files_imported,
                        files_missing,
                        files_error,
                        duration,
                        station_id,
                        session_type,
                    ),
                )

        logger.info(
            f"Backfill {session_type} {station_id}/{process_date}: "
            f"found={files_found} imported={files_imported} "
            f"missing={files_missing} errors={files_error} "
            f"({duration:.1f}s)"
        )

        return not is_complete

    except Exception as e:
        logger.error(f"Backfill error {session_type} {station_id}/{process_date}: {e}")
        try:
            with DatabaseConnectionFactory.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE backfill_progress
                        SET files_error = files_error + 1,
                            last_run = NOW(),
                            last_duration_seconds = %s,
                            updated_at = NOW()
                        WHERE sid = %s AND session_type = %s
                    """,
                        (time.time() - start_time, station_id, session_type),
                    )
        except Exception:
            pass
        return True


def _download_day_generic(
    station_id: str,
    process_date: date,
    session_type: str,
    immediate_archive: bool = True,
) -> Optional[Dict[str, Any]]:
    """Download files for a single day and session type.

    Args:
        station_id: Station identifier
        process_date: Date to download
        session_type: Session type ('status_1hr', '1Hz_1hr', '15s_24hr')
        immediate_archive: Whether to archive immediately after each file

    Returns:
        Download result dictionary, or None on setup error
    """
    try:
        from ..cli.main import create_receiver, get_station_config

        station_config = get_station_config(station_id)
        if not station_config:
            logger.warning(f"No config for {station_id}")
            return None

        receiver = create_receiver(station_id, station_config)

        start_time = datetime.combine(process_date, datetime.min.time()).replace(
            tzinfo=UTC
        )

        if session_type == "15s_24hr":
            frequency = "1D"
            end_time = start_time + timedelta(days=1)
        else:
            frequency = "1H"
            end_time = start_time + timedelta(days=1)

        result = receiver.download_data(
            start=start_time,
            end=end_time,
            session=session_type,
            ffrequency=frequency,
            sync=True,
            archive=True,
            immediate_archive=immediate_archive,
            clean_tmp=False,  # Keep partial files so FTP REST resume works
            compression=".gz",
            reverse_chronological=False,
            loglevel=logging.INFO,
        )

        return result

    except Exception as e:
        logger.error(f"Download error {session_type} {station_id}/{process_date}: {e}")
        return None


def _run_backfill_rinex(
    station_id: str,
    session_type: str,
    downloaded_files: List[str],
) -> None:
    """Run RINEX conversion on backfilled files.

    Reuses the same _run_rinex_conversion() from bulk_scheduler to maintain
    consistent behavior between live downloads and backfill.

    Args:
        station_id: Station identifier
        session_type: Session type ('15s_24hr', '1Hz_1hr')
        downloaded_files: List of downloaded raw file paths
    """
    try:
        from ..cli.main import get_station_config
        from .bulk_scheduler import _run_rinex_conversion

        station_config = get_station_config(station_id)
        if not station_config:
            logger.warning(f"Backfill RINEX: no config for {station_id}")
            return

        _run_rinex_conversion(
            station_id,
            session_type,
            downloaded_files,
            station_config,
            logger,
        )

    except ImportError as e:
        logger.debug(f"Backfill RINEX not available: {e}")
    except Exception as e:
        logger.warning(f"Backfill RINEX failed for {station_id}: {e}")


def _extract_and_store_health(
    station_id: str,
    file_paths: List[str],
    log: logging.Logger,
) -> int:
    """Extract time-series health data from SBF files and write to database.

    Uses TimeSeriesHealthExtractor to get full timeseries with proper GPS
    timestamps, then HealthJsonImporter to write to block_* tables.

    Args:
        station_id: Station identifier
        file_paths: List of archive file paths (may be .sbf, .sbf.gz)
        log: Logger instance

    Returns:
        Number of files successfully imported
    """
    imported_count = 0

    try:
        from ..health.json_importer import HealthJsonImporter
        from ..health.timeseries_extractor import TimeSeriesHealthExtractor
    except ImportError as e:
        log.warning(f"Health extraction dependencies not available: {e}")
        return 0

    try:
        extractor = TimeSeriesHealthExtractor(
            station_id=station_id,
            receiver_type="PolaRX5",
        )

        sbf_files = [Path(p) for p in file_paths if Path(p).exists()]
        if not sbf_files:
            return 0

        # Extract timeseries from all files at once (groups by day internally)
        file_date = _extract_date_from_path(sbf_files[0])
        extract_date = (
            datetime.combine(file_date, datetime.min.time())
            if file_date
            else datetime.now(UTC)
        )

        health_data = extractor.extract_daily_health(sbf_files, extract_date)
        sample_count = health_data.get("sample_count", 0)

        if sample_count == 0:
            log.debug(f"No health samples in {len(sbf_files)} files for {station_id}")
            return 0

        # Write to database using the JSON importer (handles proper timestamps)
        with HealthJsonImporter() as importer:
            if importer.connect(database="gps_health"):
                rows = importer.import_health_data(health_data, station_id, "PolaRX5")
                if rows > 0:
                    imported_count = len(sbf_files)
                    log.debug(
                        f"Imported {rows} rows from {len(sbf_files)} files for {station_id}"
                    )
            else:
                log.warning("Failed to connect to database for health import")

    except Exception as e:
        log.error(f"Health extraction error for {station_id}: {e}")

    return imported_count


def _extract_date_from_path(file_path: Path) -> Optional[date]:
    """Extract date from archive file path.

    Archive paths contain date info in the filename, e.g.:
    ELDC20260115_status.sbf.gz or similar patterns with YYYYMMDD.

    Args:
        file_path: Path to archived file

    Returns:
        Extracted date or None
    """
    import re

    name = file_path.name
    # Try YYYYMMDD pattern in filename (common in SBF archives)
    match = re.search(r"(\d{4})(\d{2})(\d{2})", name)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass

    # Try YYDOY pattern (2-digit year + day of year)
    match = re.search(r"(\d{2})(\d{3})", name)
    if match:
        try:
            year = 2000 + int(match.group(1))
            doy = int(match.group(2))
            if 1 <= doy <= 366:
                return (datetime(year, 1, 1) + timedelta(days=doy - 1)).date()
        except ValueError:
            pass

    return None
