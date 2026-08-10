"""File tracking for downloads and health imports.

Tracks file availability and import status to:
- Skip files known to be missing on receivers
- Avoid reimporting data already in the database
- Provide data availability statistics
"""

import hashlib
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..utils.canonical_key import find_by_canonical_key

logger = logging.getLogger(__name__)


@contextmanager
def read_only_cursor(conn):
    """Cursor for a read-only query that ALWAYS ends its transaction.

    psycopg2 opens a transaction on the first statement — a bare ``SELECT``
    counts — so a long-lived connection that only ever reads sits in
    ``idle in transaction`` until something ends it. Every write path here
    commits or rolls back; the read paths did neither, and that is the whole
    bug. Measured on rek-d01 2026-08-10: two ``FormatResolver`` connections
    parked for 68 and 59 minutes, plus a constant trickle from
    ``is_file_missing``.

    Why it matters more than it looks:

    * an open transaction pins the vacuum ``xmin`` horizon, so VACUUM cannot
      reclaim dead tuples on a table taking tens of millions of updates; and
    * ``CREATE``/``DROP INDEX CONCURRENTLY`` can never finish, because it waits
      for every transaction that could see the index and there was always one
      open — six attempts at migration 065 died on ``lock_timeout`` until the
      sessions were killed by hand.

    ``rollback()`` rather than ``commit()`` on purpose: if a future caller ever
    leaves a write pending on this connection, discarding it is the safer
    failure of the two.

    Use this for every read on a long-lived connection, including from outside
    the owning class (see :meth:`FileTracker.read_cursor`).
    """
    with conn.cursor() as cur:
        try:
            yield cur
        finally:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 — cleanup must never break the read
                pass


class FileTracker:
    """Track file availability and import status.

    Uses the file_tracking table in the gps_health database.
    """

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize file tracker.

        Args:
            connection_string: PostgreSQL connection string.
                             If None, uses environment variables.
        """
        self.connection_string = connection_string
        self._conn = None
        self._data_prepath_cache: Optional[str] = None
        self._use_terminal_cache: Optional[bool] = None

    def _use_terminal_absence(self) -> bool:
        """Whether to honour terminal file_absence as a skip (config, cached).

        Default False (advisory) until the served-gate is trusted — see
        ReceiversConfig.get_use_terminal_absence. Resolved once; a config error
        degrades to the safe default.
        """
        if self._use_terminal_cache is None:
            try:
                from ..config.receivers_config import get_receivers_config

                self._use_terminal_cache = (
                    get_receivers_config().get_use_terminal_absence()
                )
            except Exception:  # noqa: BLE001 — safe default
                self._use_terminal_cache = False
        return self._use_terminal_cache

    def _data_prepath(self) -> Optional[str]:
        """Local ring root (data_prepath), resolved once and cached.

        Used to reconstruct the on-disk path for the durable local catalog
        write. Returns None if config is unavailable (the catalog hook then
        no-ops — best-effort).
        """
        if self._data_prepath_cache is None:
            try:
                from ..config.receivers_config import get_receivers_config

                self._data_prepath_cache = get_receivers_config().get_data_prepath()
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.debug(f"Cannot resolve data_prepath for catalog hook: {e}")
                return None
        return self._data_prepath_cache

    def _catalog_local_archived(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int],
        filename: Optional[str],
        file_size: Optional[int],
    ) -> None:
        """Durable local catalog write for a just-archived file (unified index M1).

        Runs AFTER the file_tracking write is committed, in its own transaction,
        fully isolated: a catalog failure rolls back only itself and never
        touches the operational record. Hashes are deferred (lazy-filled by the
        integrity checker), so this adds no decompression to the archive path.
        """
        if not filename:
            return
        data_prepath = self._data_prepath()
        if not data_prepath or not self._conn:
            return
        try:
            from ..archive.catalog import catalog_local_file

            ok = catalog_local_file(
                self._conn,
                ft_session_type=session_type,
                station=station_id,
                file_date=file_date,
                file_hour=file_hour,
                filename=filename,
                file_size=file_size,
                data_prepath=data_prepath,
            )
            if ok:
                self._conn.commit()
            else:
                self._conn.rollback()
        except Exception as e:  # noqa: BLE001 — best-effort; never break archiving
            logger.debug(f"local catalog hook failed for {station_id}/{filename}: {e}")
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def connect(self, database: str = "gps_health") -> bool:
        """Connect to PostgreSQL database.

        Args:
            database: Database name

        Returns:
            True if connection successful
        """
        try:
            from .database_factory import DatabaseConnectionFactory

            self._conn = DatabaseConnectionFactory.get_connection(
                database=database,
                connection_string=self.connection_string,
            )
            logger.debug("Connected to PostgreSQL database")
            return True

        except ImportError:
            logger.warning("psycopg2 not installed - file tracking disabled")
            return False
        except Exception as e:
            logger.warning(f"Database connection failed: {e} - file tracking disabled")
            return False

    def read_cursor(self):
        """Read cursor on this tracker's long-lived connection.

        Public so callers that reach in for ``tracker._conn`` (the integrity
        checker does) get the same transaction hygiene. See
        :func:`read_only_cursor` for why a read must end its transaction.
        """
        return read_only_cursor(self._conn)

    def is_file_missing(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
    ) -> bool:
        """Check if a file is known to be missing.

        Args:
            station_id: Station identifier
            session_type: Session type ('15s_24hr', '1Hz_1hr', 'status_1hr')
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily

        Returns:
            True if file is known to be missing (skip download)
        """
        if not self._conn:
            if not self.connect():
                return False  # Can't check, don't skip

        try:
            with self.read_cursor() as cur:
                cur.execute(
                    "SELECT is_file_missing(%s, %s, %s, %s::smallint, %s)",
                    (
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                        self._use_terminal_absence(),
                    ),
                )
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.debug(f"Error checking file status: {e}")
            return False

    def is_health_imported(
        self,
        station_id: str,
        file_date: date,
        checksum: Optional[str] = None,
        min_completeness: float = 0.95,
    ) -> bool:
        """Check if health data is already imported AND complete.

        Args:
            station_id: Station identifier
            file_date: Date of the health data
            checksum: Optional checksum to verify data hasn't changed
            min_completeness: Minimum fraction of expected samples (default 0.95 = 95%)
                             Expected samples = 24 hours * 60 samples/hour = 1440

        Returns:
            True if data is already imported AND has sufficient completeness
        """
        if not self._conn:
            if not self.connect():
                return False  # Can't check, allow import

        try:
            with self.read_cursor() as cur:
                # First check if marked as imported in tracking table
                cur.execute(
                    "SELECT is_health_imported(%s, %s, %s)",
                    (station_id, file_date, checksum),
                )
                result = cur.fetchone()
                is_marked_imported = result[0] if result else False

                if not is_marked_imported:
                    return False

                # Also verify actual data completeness in block_power_status
                # Expected: 24 hours * 60 samples = 1440 samples per day
                expected_samples = 1440
                min_samples = int(expected_samples * min_completeness)

                cur.execute(
                    """
                    SELECT COUNT(*) FROM block_power_status
                    WHERE sid = %s
                    AND ts >= %s::date
                    AND ts < %s::date + interval '1 day'
                    """,
                    (station_id, file_date, file_date),
                )
                count_result = cur.fetchone()
                actual_samples = count_result[0] if count_result else 0

                if actual_samples < min_samples:
                    logger.info(
                        f"Health data incomplete for {station_id}/{file_date}: "
                        f"{actual_samples}/{expected_samples} samples ({100 * actual_samples / expected_samples:.1f}%)"
                    )
                    return False

                return True
        except Exception as e:
            logger.debug(f"Error checking import status: {e}")
            return False

    def mark_file_missing(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Mark a file as missing on the receiver.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files
            filename: Expected filename

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'missing')""",
                    (station_id, session_type, file_date, file_hour, filename),
                )
            self._conn.commit()
            logger.debug(
                f"Marked file as missing: {station_id}/{session_type}/{file_date}"
            )
            # Durable confirmed-absent ledger (unified index M2): a reachable-but
            # -absent (404/550) result. Best-effort + isolated so a pre-056 DB or
            # any failure never undoes the operational missing-mark above.
            self._record_receiver_absence(
                station_id, session_type, file_date, file_hour
            )
            return True
        except Exception as e:
            logger.debug(f"Error marking file as missing: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def _record_receiver_absence(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int],
    ) -> None:
        """Log one reachable-but-absent confirmation to file_absence (mig 056).

        Runs AFTER the file_tracking missing-mark is committed, in its own
        transaction: terminal promotion is time-spanned inside
        ``record_file_absence`` (never same-day count-only). Best-effort — a DB
        without mig 056 (function absent) or any error is swallowed, leaving the
        operational record and the 24 h transient TTL intact.
        """
        if not self._conn:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT record_file_absence(%s, %s, %s, %s::smallint, 'receiver')",
                    (station_id, session_type, file_date, file_hour),
                )
            self._conn.commit()
        except Exception as e:  # noqa: BLE001 — best-effort; never break the mark
            logger.debug(
                f"record_file_absence failed for "
                f"{station_id}/{session_type}/{file_date}: {e}"
            )
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def mark_file_downloaded(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        remote_file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully downloaded.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files
            filename: Filename
            file_size: File size in bytes
            remote_file_size: File size reported by receiver (FTP SIZE / HTTP Content-Length)

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'downloaded', %s, NULL, NULL, NULL, NULL, %s)""",
                    (
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                        filename,
                        file_size,
                        remote_file_size,
                    ),
                )
            self._conn.commit()
            logger.debug(
                f"Marked file as downloaded: {station_id}/{session_type}/{file_date}"
            )
            return True
        except Exception as e:
            logger.debug(f"Error marking file as downloaded: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def mark_file_archived(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        remote_file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully archived.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files
            filename: Filename
            file_size: File size in bytes
            remote_file_size: File size reported by receiver (FTP SIZE / HTTP Content-Length)

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'archived', %s, NULL, NULL, NULL, NULL, %s)""",
                    (
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                        filename,
                        file_size,
                        remote_file_size,
                    ),
                )
            self._conn.commit()
            logger.debug(
                f"Marked file as archived: {station_id}/{session_type}/{file_date}"
            )
            self._catalog_local_archived(
                station_id, session_type, file_date, file_hour, filename, file_size
            )
            return True
        except Exception as e:
            logger.debug(f"Error marking file as archived: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def mark_file_suspect(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Mark a file as suspect (failed integrity check).

        Suspect files are preserved on disk but flagged in the database.
        Dashboard views filter on status IN ('downloaded', 'archived') so
        suspect files appear as "missing data" (correct — they shouldn't be trusted).

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files
            filename: Filename
            file_size: File size in bytes
            reason: Why the file is suspect (stored in last_error)

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'suspect', %s, NULL, NULL, NULL, %s)""",
                    (
                        station_id,
                        session_type,
                        file_date,
                        file_hour,
                        filename,
                        file_size,
                        reason,
                    ),
                )
            self._conn.commit()
            logger.debug(
                f"Marked file as suspect: {station_id}/{session_type}/{file_date} — {reason}"
            )
            return True
        except Exception as e:
            logger.debug(f"Error marking file as suspect: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def mark_integrity_checked(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
    ) -> bool:
        """Mark a file as having passed integrity check.

        Updates integrity_checked_at timestamp without changing status.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files

        Returns:
            True if successfully updated
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                if file_hour is None:
                    cur.execute(
                        """UPDATE file_tracking SET integrity_checked_at = NOW(), updated_at = NOW()
                        WHERE sid = %s AND session_type = %s AND file_date = %s AND file_hour IS NULL""",
                        (station_id, session_type, file_date),
                    )
                else:
                    cur.execute(
                        """UPDATE file_tracking SET integrity_checked_at = NOW(), updated_at = NOW()
                        WHERE sid = %s AND session_type = %s AND file_date = %s AND file_hour = %s""",
                        (station_id, session_type, file_date, file_hour),
                    )
            self._conn.commit()
            return True
        except Exception as e:
            logger.debug(f"Error marking integrity checked: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def mark_health_imported(
        self,
        station_id: str,
        file_date: date,
        samples_imported: int,
        checksum: Optional[str] = None,
        json_path: Optional[str] = None,
    ) -> bool:
        """Mark health data as imported to database.

        Args:
            station_id: Station identifier
            file_date: Date of the health data
            samples_imported: Number of samples imported
            checksum: Checksum of the data
            json_path: Path to JSON file if written

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(
                        %s, 'status_1hr', %s, NULL, NULL, 'downloaded',
                        NULL, %s, %s, %s
                    )""",
                    (station_id, file_date, samples_imported, checksum, json_path),
                )
            self._conn.commit()
            logger.debug(
                f"Marked health as imported: {station_id}/{file_date} ({samples_imported} samples)"
            )
            return True
        except Exception as e:
            logger.debug(f"Error marking health as imported: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def get_data_availability(
        self,
        station_id: Optional[str] = None,
        session_type: str = "status_1hr",
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get data availability summary.

        Args:
            station_id: Optional station filter
            session_type: Session type to check
            days: Number of days to look back

        Returns:
            Dictionary with availability statistics
        """
        if not self._conn:
            if not self.connect():
                return {}

        try:
            with self.read_cursor() as cur:
                if station_id:
                    cur.execute(
                        """
                        SELECT
                            COUNT(*) FILTER (WHERE status = 'downloaded') as downloaded,
                            COUNT(*) FILTER (WHERE status = 'missing') as missing,
                            COUNT(*) FILTER (WHERE imported_to_db) as imported,
                            AVG(samples_imported) FILTER (WHERE samples_imported IS NOT NULL) as avg_samples
                        FROM file_tracking
                        WHERE sid = %s
                          AND session_type = %s
                          AND file_date > CURRENT_DATE - %s
                          AND file_hour IS NULL
                        """,
                        (station_id, session_type, days),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            sid,
                            COUNT(*) FILTER (WHERE status = 'downloaded') as downloaded,
                            COUNT(*) FILTER (WHERE status = 'missing') as missing,
                            COUNT(*) FILTER (WHERE imported_to_db) as imported
                        FROM file_tracking
                        WHERE session_type = %s
                          AND file_date > CURRENT_DATE - %s
                          AND file_hour IS NULL
                        GROUP BY sid
                        ORDER BY sid
                        """,
                        (session_type, days),
                    )

                rows = cur.fetchall()
                if station_id and rows:
                    row = rows[0]
                    return {
                        "downloaded": row[0] or 0,
                        "missing": row[1] or 0,
                        "imported": row[2] or 0,
                        "avg_samples": float(row[3]) if row[3] else None,
                    }
                elif rows:
                    return {
                        row[0]: {
                            "downloaded": row[1] or 0,
                            "missing": row[2] or 0,
                            "imported": row[3] or 0,
                        }
                        for row in rows
                    }
                return {}
        except Exception as e:
            logger.debug(f"Error getting data availability: {e}")
            return {}

    def get_download_stats(
        self,
        station_id: str,
        session_type: str = "15s_24hr",
        days: int = 7,
    ) -> Dict[str, Any]:
        """Get download statistics for Icinga check.

        Args:
            station_id: Station identifier
            session_type: Session type ('15s_24hr', '1Hz_1hr', etc.)
            days: Number of days to look back

        Returns:
            Dictionary with download stats:
            - hours_since_download: Hours since last successful download
            - latest_download: Timestamp of last download
            - downloads_expected: Expected downloads in period
            - downloads_successful: Successful downloads
            - downloads_missing: Known missing files
            - error_count: Total errors in period
        """
        if not self._conn:
            if not self.connect():
                return {}

        try:
            with self.read_cursor() as cur:
                # Get latest successful download
                cur.execute(
                    """
                    SELECT
                        last_checked,
                        EXTRACT(EPOCH FROM (NOW() - last_checked)) / 3600 as hours_ago
                    FROM file_tracking
                    WHERE sid = %s
                      AND session_type = %s
                      AND status = 'downloaded'
                    ORDER BY last_checked DESC
                    LIMIT 1
                    """,
                    (station_id, session_type),
                )
                latest = cur.fetchone()

                # Get counts for recent period
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'downloaded') as successful,
                        COUNT(*) FILTER (WHERE status = 'missing') as missing,
                        COALESCE(SUM(error_count), 0) as total_errors,
                        COUNT(*) as total_tracked
                    FROM file_tracking
                    WHERE sid = %s
                      AND session_type = %s
                      AND file_date >= CURRENT_DATE - %s
                    """,
                    (station_id, session_type, days),
                )
                counts = cur.fetchone()

                result = {
                    "hours_since_download": None,
                    "latest_download": None,
                    "downloads_expected": days,  # Rough estimate
                    "downloads_successful": 0,
                    "downloads_missing": 0,
                    "error_count": 0,
                }

                if latest:
                    result["latest_download"] = (
                        latest[0].isoformat() if latest[0] else None
                    )
                    result["hours_since_download"] = (
                        float(latest[1]) if latest[1] else None
                    )

                if counts:
                    result["downloads_successful"] = counts[0] or 0
                    result["downloads_missing"] = counts[1] or 0
                    result["error_count"] = int(counts[2]) if counts[2] else 0

                return result

        except Exception as e:
            logger.debug(f"Error getting download stats: {e}")
            return {}

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


# Minimum file size (bytes) for archive scanning — skip empty/corrupt files
MIN_ARCHIVE_FILE_SIZE = 50


class ArchiveFileChecker:
    """Check archive file system for expected files.

    Used by Icinga checks to verify files exist on the archive.
    """

    def __init__(self, data_prepath: Optional[str] = None):
        """Initialize archive file checker.

        Args:
            data_prepath: Base archive path. If None, tries to load from config.
        """
        self.data_prepath = data_prepath
        self._config = None

    def _load_config(self):
        """Load receivers configuration."""
        if self._config is not None:
            return

        try:
            from ..config.receivers_config import ReceiversConfig

            self._config = ReceiversConfig()

            if self.data_prepath is None:
                self.data_prepath = self._config.get_data_prepath()
        except Exception as e:
            logger.debug(f"Could not load receivers config: {e}")
            # Fall back to common paths
            if self.data_prepath is None:
                for path in ["/mnt/gpsdata", "/tmp/gpsdata"]:
                    if os.path.isdir(path):
                        self.data_prepath = path
                        break

    def _get_archive_template(self) -> str:
        """Get archive template from config or use default."""
        self._load_config()
        if self._config:
            try:
                return self._config.get_archive_template()
            except Exception:
                pass
        # Default template
        return "{data_prepath}/%Y/#b/{station}/{session}/raw/{station}%Y%m%d%H00{session_letter}{extension}"

    def _get_session_letter(self, session_type: str) -> str:
        """Get session letter for session type."""
        session_letters = {
            "15s_24hr": "a",
            "1Hz_1hr": "b",
            "status_1hr": "c",
            "15s_24hr_rinex": "a",
            "1Hz_1hr_rinex": "b",
            "20Hz_1hr": "d",
            "50Hz_1hr": "e",
        }
        return session_letters.get(session_type, "a")

    def _get_extension(self, receiver_type: Optional[str] = None) -> str:
        """Get file extension based on receiver type."""
        from ..config.receiver_registry import get_raw_extension

        if receiver_type:
            return get_raw_extension(receiver_type)
        return ".sbf.gz"  # Default

    def build_archive_path(
        self,
        station_id: str,
        session_type: str,
        dt: datetime,
        receiver_type: Optional[str] = None,
    ) -> str:
        """Build expected archive path for a file.

        Args:
            station_id: Station ID (e.g., 'THOB')
            session_type: Session type (e.g., '15s_24hr')
            dt: Datetime for the file
            receiver_type: Optional receiver type for extension

        Returns:
            Expected archive path
        """
        self._load_config()

        template = self._get_archive_template()
        session_letter = self._get_session_letter(session_type)
        extension = self._get_extension(receiver_type)

        # Substitute placeholders
        path = template.format(
            data_prepath=self.data_prepath or "/mnt/gpsdata",
            station=station_id,
            session=session_type,
            session_letter=session_letter,
            extension=extension,
        )

        # Use gtimes for date formatting
        try:
            import gtimes.timefunc as gt

            paths = gt.datepathlist(path, "1D", datelist=[dt])
            return paths[0] if paths else path
        except Exception:
            # Fallback: manual formatting
            path = path.replace("%Y", dt.strftime("%Y"))
            path = path.replace("%m", dt.strftime("%m"))
            path = path.replace("%d", dt.strftime("%d"))
            path = path.replace("%H", dt.strftime("%H"))
            path = path.replace("%j", dt.strftime("%j"))
            path = path.replace("#b", dt.strftime("%b"))
            return path

    def get_archive_directory(
        self,
        station_id: str,
        session_type: str,
        year: Optional[int] = None,
        month: Optional[str] = None,
    ) -> str:
        """Get archive directory path for a station/session.

        Args:
            station_id: Station ID (e.g., 'THOB')
            session_type: Session type (e.g., '15s_24hr', '15s_24hr_rinex')
            year: Optional year (default: current year)
            month: Optional month abbreviation (default: current month)

        Returns:
            Archive directory path
        """
        self._load_config()

        now = datetime.now()
        if year is None:
            year = now.year
        if month is None:
            month = now.strftime("%b").lower()  # Use lowercase month (jan, feb, etc.)

        # RINEX session types use rinex/ subdir instead of raw/
        if session_type.endswith("_rinex"):
            base_session = session_type[:-6]  # strip '_rinex'
            subdir = "rinex"
        else:
            base_session = session_type
            subdir = "raw"

        # Build directory path: {data_prepath}/{year}/{month}/{station}/{session}/{subdir}/
        return os.path.join(
            self.data_prepath or "/mnt/gpsdata",
            str(year),
            month,
            station_id,
            base_session,
            subdir,
        )

    def check_file_status(
        self,
        station_id: str,
        session_type: str,
        days_back: int = 2,
        receiver_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Check file status by searching archive directories.

        Searches the archive directory structure for files and reports
        on file count and age. Only counts files within the days_back period.

        Args:
            station_id: Station ID (e.g., 'THOB')
            session_type: Session type (e.g., '15s_24hr')
            days_back: Number of days to check for expected files
            receiver_type: Optional receiver type (for future use)

        Returns:
            Dict with:
            - files_found: Number of files found within days_back period
            - files_expected: Expected files based on days_back
            - latest_file: Path to most recent file found
            - latest_mtime: Modification time of most recent file
            - hours_since_file: Hours since most recent file
            - archive_dir: Archive directory searched
            - dir_exists: Whether the archive directory exists
        """
        import glob

        self._load_config()

        now = datetime.now()
        cutoff_date = now - timedelta(days=days_back)
        all_files = []
        searched_dirs = set()

        # Search all unique month directories that could contain files in the period
        # Generate all dates in the range to find unique year/month combinations
        unique_months = set()
        for day_offset in range(days_back + 1):  # +1 to include today
            check_date = now - timedelta(days=day_offset)
            unique_months.add((check_date.year, check_date.strftime("%b").lower()))

        for year, month in unique_months:
            archive_dir = self.get_archive_directory(
                station_id,
                session_type,
                year=year,
                month=month,
            )
            searched_dirs.add(archive_dir)

            if os.path.isdir(archive_dir):
                # Find all files in the directory
                pattern = os.path.join(archive_dir, f"{station_id}*")
                for filepath in glob.glob(pattern):
                    if os.path.isfile(filepath):
                        fsize = os.path.getsize(filepath)
                        if fsize < MIN_ARCHIVE_FILE_SIZE:
                            continue  # skip empty/corrupt files
                        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                        # Only count files within the days_back period
                        if mtime >= cutoff_date:
                            all_files.append((filepath, mtime))

        # Sort by modification time (newest first)
        all_files.sort(key=lambda x: x[1], reverse=True)

        # Count files and find latest
        files_found = len(all_files)
        latest_file = None
        latest_mtime = None
        if all_files:
            latest_file, latest_mtime = all_files[0]

        # Calculate hours since latest file
        hours_since_file = None
        if latest_mtime:
            hours_since_file = (now - latest_mtime).total_seconds() / 3600

        # Expected files based on session type
        if session_type in ("15s_24hr", "15s_24hr_rinex"):
            files_expected = days_back  # 1 file per day
        else:
            files_expected = days_back * 24  # 1 file per hour

        # Get current archive directory for reference
        archive_dir = self.get_archive_directory(station_id, session_type)

        # Check if any archive directory was found
        dir_exists = any(os.path.isdir(d) for d in searched_dirs) or files_found > 0

        return {
            "files_found": files_found,
            "files_expected": files_expected,
            "latest_file": latest_file,
            "latest_mtime": latest_mtime.isoformat() if latest_mtime else None,
            "hours_since_file": hours_since_file,
            "archive_dir": archive_dir,
            "dir_exists": dir_exists,  # False if session not configured for station
        }


@dataclass
class ArchiveFormat:
    """In-memory representation of an archive_format row."""

    format_id: str
    session_type: str
    file_category: str  # 'raw', 'rinex', 'nav', 'timeseries'
    receiver_type: Optional[str]
    frequency: str  # '1D', '1H'
    rinex_version: Optional[str]
    naming_convention: Optional[str]  # 'short', 'long', None
    hatanaka: Optional[bool]
    compression: Optional[str]  # 'Z', 'gz', None
    file_extension: str
    dir_template: str
    filename_template: str
    description: Optional[str] = None


@dataclass
class StorageLocation:
    """In-memory representation of a storage_location row."""

    location_id: str
    name: str
    base_path: str
    location_type: str  # 'local', 'nfs', 'server'
    is_primary: bool = False
    enabled: bool = True


class FormatResolver:
    """Resolve archive formats and build file paths from DB-driven templates.

    Loads archive_format and storage_location definitions from PostgreSQL
    and uses gtimes.datepathlist() to construct full paths.

    Designed for use alongside ArchiveFileChecker — this class handles
    format-aware path construction while ArchiveFileChecker handles
    config-template-based paths for backward compatibility.
    """

    # Session letter mapping (same as ArchiveFileChecker._get_session_letter)
    SESSION_LETTERS: Dict[str, str] = {
        "15s_24hr": "a",
        "1Hz_1hr": "b",
        "status_1hr": "c",
        "15s_24hr_rinex": "a",
        "1Hz_1hr_rinex": "b",
        "20Hz_1hr": "d",
        "50Hz_1hr": "e",
    }

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize format resolver.

        Args:
            connection_string: PostgreSQL connection string.
                             If None, uses environment variables.
        """
        self.connection_string = connection_string
        self._conn = None
        self._formats: Dict[str, ArchiveFormat] = {}
        self._locations: Dict[str, StorageLocation] = {}
        self._loaded = False

    def connect(self, database: str = "gps_health") -> bool:
        """Connect to PostgreSQL database.

        Args:
            database: Database name

        Returns:
            True if connection successful
        """
        try:
            from .database_factory import DatabaseConnectionFactory

            self._conn = DatabaseConnectionFactory.get_connection(
                database=database,
                connection_string=self.connection_string,
            )
            return True
        except ImportError:
            logger.warning("psycopg2 not installed - format resolver disabled")
            return False
        except Exception as e:
            logger.warning(
                f"Database connection failed: {e} - format resolver disabled"
            )
            return False

    def _ensure_loaded(self) -> bool:
        """Ensure format and location data is loaded from DB.

        Returns:
            True if data is available
        """
        if self._loaded:
            return bool(self._formats)

        if not self._conn:
            if not self.connect():
                return False

        try:
            self._load_formats()
            self._load_locations()
            self._loaded = True
            return bool(self._formats)
        except Exception as e:
            logger.warning(f"Failed to load format data: {e}")
            return False

    def _load_formats(self) -> None:
        """Load all archive_format rows into memory."""
        with read_only_cursor(self._conn) as cur:
            cur.execute(
                """SELECT format_id, session_type, file_category, receiver_type,
                          frequency, rinex_version, naming_convention, hatanaka,
                          compression, file_extension, dir_template,
                          filename_template, description
                   FROM archive_format
                   ORDER BY format_id"""
            )
            self._formats = {}
            for row in cur.fetchall():
                fmt = ArchiveFormat(
                    format_id=row[0],
                    session_type=row[1],
                    file_category=row[2],
                    receiver_type=row[3],
                    frequency=row[4],
                    rinex_version=row[5],
                    naming_convention=row[6],
                    hatanaka=row[7],
                    compression=row[8],
                    file_extension=row[9],
                    dir_template=row[10],
                    filename_template=row[11],
                    description=row[12],
                )
                self._formats[fmt.format_id] = fmt

        logger.debug(f"Loaded {len(self._formats)} archive formats")

    def _load_locations(self) -> None:
        """Load all storage_location rows into memory."""
        with read_only_cursor(self._conn) as cur:
            cur.execute(
                """SELECT location_id, name, base_path, location_type,
                          is_primary, enabled
                   FROM storage_location
                   WHERE enabled = true
                   ORDER BY location_id"""
            )
            self._locations = {}
            for row in cur.fetchall():
                loc = StorageLocation(
                    location_id=row[0],
                    name=row[1],
                    base_path=row[2],
                    location_type=row[3],
                    is_primary=row[4],
                    enabled=row[5],
                )
                self._locations[loc.location_id] = loc

        logger.debug(f"Loaded {len(self._locations)} storage locations")

    def get_format(self, format_id: str) -> Optional[ArchiveFormat]:
        """Get a format definition by ID.

        Args:
            format_id: Format identifier (e.g., 'polarx5_15s_24hr_rinex')

        Returns:
            ArchiveFormat or None if not found
        """
        if not self._ensure_loaded():
            return None
        return self._formats.get(format_id)

    def get_location(self, location_id: str) -> Optional[StorageLocation]:
        """Get a storage location by ID.

        Args:
            location_id: Location identifier (e.g., 'local_archive')

        Returns:
            StorageLocation or None if not found
        """
        if not self._ensure_loaded():
            return None
        return self._locations.get(location_id)

    def get_primary_location(self) -> Optional[StorageLocation]:
        """Get the primary storage location.

        Returns:
            Primary StorageLocation or first enabled location, or None
        """
        if not self._ensure_loaded():
            return None

        for loc in self._locations.values():
            if loc.is_primary:
                return loc

        # Fall back to first enabled location
        if self._locations:
            return next(iter(self._locations.values()))
        return None

    def list_formats(
        self,
        session_type: Optional[str] = None,
        file_category: Optional[str] = None,
        receiver_type: Optional[str] = None,
    ) -> List[ArchiveFormat]:
        """List formats matching optional filters.

        Args:
            session_type: Filter by session type (e.g., '15s_24hr')
            file_category: Filter by category (e.g., 'rinex')
            receiver_type: Filter by receiver type (e.g., 'polarx5')

        Returns:
            List of matching ArchiveFormat objects
        """
        if not self._ensure_loaded():
            return []

        result = list(self._formats.values())

        if session_type is not None:
            result = [f for f in result if f.session_type == session_type]
        if file_category is not None:
            result = [f for f in result if f.file_category == file_category]
        if receiver_type is not None:
            result = [f for f in result if f.receiver_type == receiver_type]

        return result

    def find_format(
        self,
        session_type: str,
        file_category: str,
        receiver_type: Optional[str] = None,
    ) -> Optional[ArchiveFormat]:
        """Find a single format matching the given criteria.

        Tries receiver-specific format first, falls back to universal (NULL receiver_type).

        Args:
            session_type: Session type (e.g., '15s_24hr')
            file_category: File category (e.g., 'raw', 'rinex')
            receiver_type: Receiver type (e.g., 'polarx5')

        Returns:
            Best matching ArchiveFormat or None
        """
        if not self._ensure_loaded():
            return None

        # Try receiver-specific first
        if receiver_type:
            for fmt in self._formats.values():
                if (
                    fmt.session_type == session_type
                    and fmt.file_category == file_category
                    and fmt.receiver_type == receiver_type.lower()
                ):
                    return fmt

        # Fall back to universal format (receiver_type IS NULL)
        for fmt in self._formats.values():
            if (
                fmt.session_type == session_type
                and fmt.file_category == file_category
                and fmt.receiver_type is None
            ):
                return fmt

        return None

    def build_path(
        self,
        format_id: str,
        station: str,
        dt: datetime,
        location_id: Optional[str] = None,
        base_path: Optional[str] = None,
    ) -> Optional[str]:
        """Build a full file path from format template and datetime.

        Uses gtimes.datepathlist() for date formatting. Either location_id
        or base_path must be provided.

        Args:
            format_id: Format identifier (e.g., 'polarx5_15s_24hr_rinex')
            station: Station ID (e.g., 'ELDC')
            dt: Datetime for the file
            location_id: Storage location ID (looks up base_path from DB)
            base_path: Direct base path (overrides location_id)

        Returns:
            Full file path string, or None on error

        Example:
            >>> resolver.build_path('polarx5_15s_24hr_rinex', 'ELDC',
            ...     datetime(2026, 2, 10), base_path='/home/bgo/tmp/gpsdata')
            '/home/bgo/tmp/gpsdata/2026/feb/ELDC/15s_24hr/rinex/ELDC0410.26d.Z'
        """
        fmt = self.get_format(format_id)
        if fmt is None:
            logger.warning(f"Unknown format: {format_id}")
            return None

        # Resolve base path
        if base_path is None:
            if location_id:
                loc = self.get_location(location_id)
            else:
                loc = self.get_primary_location()

            if loc is None:
                logger.warning(f"No storage location available for {format_id}")
                return None
            base_path = loc.base_path

        return self._build_path_from_format(fmt, station, dt, base_path)

    def _build_path_from_format(
        self,
        fmt: ArchiveFormat,
        station: str,
        dt: datetime,
        base_path: str,
    ) -> Optional[str]:
        """Internal: build path from format object.

        Args:
            fmt: ArchiveFormat definition
            station: Station ID
            dt: Datetime for the file
            base_path: Base directory path

        Returns:
            Full file path string, or None on error
        """
        session_letter = self.SESSION_LETTERS.get(fmt.session_type, "a")

        # Combine: base_path / dir_template / filename_template
        # Ensure no double slashes
        base = base_path.rstrip("/")
        dir_part = fmt.dir_template.strip("/")
        filename_part = fmt.filename_template

        template = f"{base}/{dir_part}/{filename_part}"

        # Substitute our placeholders before passing to gtimes
        template = template.replace("{station}", station)
        template = template.replace("{session_letter}", session_letter)

        # Use gtimes for date formatting
        try:
            import gtimes.timefunc as gt

            paths = gt.datepathlist(template, fmt.frequency, datelist=[dt])
            return paths[0] if paths else None
        except ImportError:
            logger.warning("gtimes not available — falling back to strftime")
            # Minimal fallback for common codes
            result = dt.strftime(template)
            result = result.replace("#b", dt.strftime("%b").lower())
            return result
        except Exception as e:
            logger.warning(f"Path construction failed for {fmt.format_id}: {e}")
            return None

    def build_directory(
        self,
        format_id: str,
        station: str,
        dt: datetime,
        location_id: Optional[str] = None,
        base_path: Optional[str] = None,
    ) -> Optional[str]:
        """Build only the directory path (without filename) from format template.

        Args:
            format_id: Format identifier
            station: Station ID
            dt: Datetime for the directory
            location_id: Storage location ID
            base_path: Direct base path (overrides location_id)

        Returns:
            Directory path string, or None on error
        """
        fmt = self.get_format(format_id)
        if fmt is None:
            return None

        if base_path is None:
            if location_id:
                loc = self.get_location(location_id)
            else:
                loc = self.get_primary_location()
            if loc is None:
                return None
            base_path = loc.base_path

        session_letter = self.SESSION_LETTERS.get(fmt.session_type, "a")
        base = base_path.rstrip("/")
        dir_part = fmt.dir_template.strip("/")
        template = f"{base}/{dir_part}"
        template = template.replace("{station}", station)
        template = template.replace("{session_letter}", session_letter)

        try:
            import gtimes.timefunc as gt

            paths = gt.datepathlist(template, fmt.frequency, datelist=[dt])
            return paths[0] if paths else None
        except ImportError:
            result = dt.strftime(template)
            result = result.replace("#b", dt.strftime("%b").lower())
            return result
        except Exception as e:
            logger.warning(f"Directory construction failed for {format_id}: {e}")
            return None

    def record_file_location(
        self,
        file_tracking_id: int,
        location_id: str,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> bool:
        """Record that a file exists at a storage location.

        Args:
            file_tracking_id: file_tracking.id
            location_id: storage_location.location_id
            file_path: Full path at this location
            file_size: File size in bytes

        Returns:
            True if recorded successfully
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO file_locations
                        (file_tracking_id, location_id, file_path, file_size, stored_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (file_tracking_id, location_id)
                    DO UPDATE SET
                        file_path = COALESCE(EXCLUDED.file_path, file_locations.file_path),
                        file_size = COALESCE(EXCLUDED.file_size, file_locations.file_size),
                        verified_at = NOW()
                    """,
                    (file_tracking_id, location_id, file_path, file_size),
                )
            self._conn.commit()
            return True
        except Exception as e:
            logger.debug(f"Error recording file location: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def reload(self) -> None:
        """Reload format and location data from database."""
        self._loaded = False
        self._formats.clear()
        self._locations.clear()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


class ProcessingStatusChecker:
    """Check processing status from time series files.

    Used by Icinga checks to verify 24hr processing has completed.
    """

    def __init__(self, timeseries_prepath: Optional[str] = None):
        """Initialize processing status checker.

        Args:
            timeseries_prepath: Base path for time series files.
                              If None, tries to load from config.
        """
        self.timeseries_prepath = timeseries_prepath
        self._config = None

    def _load_config(self):
        """Load receivers configuration."""
        if self._config is not None:
            return

        try:
            from ..config.receivers_config import ReceiversConfig

            self._config = ReceiversConfig()

            if self.timeseries_prepath is None:
                # Try to get from config
                try:
                    self.timeseries_prepath = self._config.config.get(
                        "processing", "timeseries_prepath", fallback=None
                    )
                except Exception:
                    pass

            # Fallback to common paths
            if self.timeseries_prepath is None:
                for path in ["/mnt_data/gpsdata", "/mnt/gpsdata"]:
                    if os.path.isdir(path):
                        self.timeseries_prepath = path
                        break
        except Exception as e:
            logger.debug(f"Could not load config: {e}")

    def get_timeseries_path(self, station_id: str) -> str:
        """Get time series file path for a station.

        Args:
            station_id: Station ID (e.g., 'THOB')

        Returns:
            Path to time series file
        """
        self._load_config()
        prepath = self.timeseries_prepath or "/mnt_data/gpsdata"
        return os.path.join(prepath, f"mb_{station_id}_TOT.dat1")

    def check_24hr_processing(
        self,
        station_id: str,
        expected_by_hour: int = 6,
    ) -> Dict[str, Any]:
        """Check if 24hr processing completed for yesterday.

        Reads the time series file and checks if the latest entry
        is from yesterday (or more recent).

        Args:
            station_id: Station ID (e.g., 'THOB')
            expected_by_hour: Hour by which processing should complete

        Returns:
            Dict with:
            - status: 'ok', 'warning', 'critical', 'unknown'
            - latest_yearf: Latest year fraction in file
            - latest_date: Latest date as datetime
            - days_behind: How many days behind the processing is
            - file_exists: Whether the time series file exists
            - message: Human-readable status message
        """
        self._load_config()

        filepath = self.get_timeseries_path(station_id)

        # Check if file exists
        if not os.path.exists(filepath):
            return {
                "status": "unknown",
                "latest_yearf": None,
                "latest_date": None,
                "days_behind": None,
                "file_exists": False,
                "message": f"Time series file not found: {filepath}",
            }

        try:
            # Read the last line of the file
            with open(filepath) as f:
                lines = f.readlines()

            if not lines:
                return {
                    "status": "unknown",
                    "latest_yearf": None,
                    "latest_date": None,
                    "days_behind": None,
                    "file_exists": True,
                    "message": "Time series file is empty",
                }

            # Parse the last non-empty line
            last_line = None
            for line in reversed(lines):
                line = line.strip()
                if line and not line.startswith("#"):
                    last_line = line
                    break

            if not last_line:
                return {
                    "status": "unknown",
                    "latest_yearf": None,
                    "latest_date": None,
                    "days_behind": None,
                    "file_exists": True,
                    "message": "No data in time series file",
                }

            # Parse year fraction from first column
            parts = last_line.split()
            if not parts:
                return {
                    "status": "unknown",
                    "latest_yearf": None,
                    "latest_date": None,
                    "days_behind": None,
                    "file_exists": True,
                    "message": "Could not parse time series data",
                }

            latest_yearf = float(parts[0])

            # Convert year fraction to datetime using gtimes
            try:
                import gtimes.timefunc as gt

                latest_dt: datetime = gt.TimefromYearf(latest_yearf)  # type: ignore[assignment]
            except Exception as e:
                logger.debug(f"Could not convert year fraction: {e}")
                return {
                    "status": "unknown",
                    "latest_yearf": latest_yearf,
                    "latest_date": None,
                    "days_behind": None,
                    "file_exists": True,
                    "message": f"Could not convert year fraction {latest_yearf}",
                }

            # Count data points in the last 7 days
            now = datetime.now()
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            week_ago = today - timedelta(days=7)

            days_with_data = set()
            try:
                import gtimes.timefunc as gt

                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        line_parts = line.split()
                        if line_parts:
                            try:
                                yearf = float(line_parts[0])
                                dt: datetime = gt.TimefromYearf(yearf)  # type: ignore[assignment]
                                if dt >= week_ago:
                                    days_with_data.add(dt.strftime("%Y-%m-%d"))
                            except (ValueError, TypeError):
                                continue
            except Exception:
                pass  # If we can't count, just continue with latest check

            days_in_week = 7
            days_missing = days_in_week - len(days_with_data)

            # Calculate how many days behind
            # 24hr processing adds yesterday's point, so:
            # - latest from yesterday (or today) = OK (on schedule)
            # - latest from 2 days ago = 1 day late
            # - latest from 3 days ago = 2 days late, etc.
            yesterday = today - timedelta(days=1)
            latest_day = latest_dt.replace(hour=0, minute=0, second=0, microsecond=0)

            # days_late: 0 = on schedule, 1 = 1 day late, etc.
            days_late = (yesterday - latest_day).days

            # Determine status based on latest data point
            latest_date_str = latest_dt.strftime("%Y-%m-%d")

            # Build gap info for message
            gap_info = ""
            if days_missing > 0:
                gap_info = f", {days_missing} gaps in last 7 days"

            if days_late <= 0:
                # Latest data is from yesterday or more recent - OK (on schedule)
                status = "ok"
                message = f"24hr processing OK - latest: {latest_date_str}{gap_info}"
                days_behind = 0
            elif days_late == 1:
                # One day late - check if we're past expected time
                if now.hour >= expected_by_hour:
                    status = "warning"
                    message = f"24hr processing delayed - latest: {latest_date_str} (1 day late){gap_info}"
                    days_behind = 1
                else:
                    # Still early, processing might be running
                    status = "ok"
                    message = f"24hr processing OK - latest: {latest_date_str} (pending){gap_info}"
                    days_behind = 0
            elif days_late <= 3:
                status = "warning"
                message = f"24hr processing behind - latest: {latest_date_str} ({days_late} days late){gap_info}"
                days_behind = days_late
            else:
                status = "critical"
                message = f"24hr processing CRITICAL - latest: {latest_date_str} ({days_late} days late){gap_info}"
                days_behind = days_late

            return {
                "status": status,
                "latest_yearf": latest_yearf,
                "latest_date": latest_dt.isoformat(),
                "days_behind": days_behind,
                "days_missing_7d": days_missing,
                "file_exists": True,
                "message": message,
            }

        except Exception as e:
            logger.debug(f"Error checking 24hr processing: {e}")
            return {
                "status": "unknown",
                "latest_yearf": None,
                "latest_date": None,
                "days_behind": None,
                "file_exists": True,
                "message": f"Error reading time series: {e}",
            }

    def check_archive_status(self, station_id: str) -> Dict[str, Any]:
        """Check archive status from file_tracking database.

        Queries the file_tracking table for the latest raw and RINEX file
        dates, providing an accurate view of what data we actually have
        archived (as opposed to check_24hr_processing which reads production
        GAMIT timeseries).

        Args:
            station_id: Station ID (e.g., 'THOB')

        Returns:
            Dict with:
            - has_data: Whether any file_tracking entries exist
            - latest_raw_date: Latest 15s_24hr raw file date (or None)
            - latest_rinex_date: Latest 15s_24hr_rinex file date (or None)
            - status: 'ok', 'warning', 'critical', 'unknown'
            - message: Human-readable status message
        """
        try:
            from ..health.database_factory import DatabaseConnectionFactory

            conn = DatabaseConnectionFactory.get_connection(database="gps_health")
            cur = conn.cursor()

            cur.execute(
                """
                SELECT session_type, MAX(file_date) as latest_date
                FROM file_tracking
                WHERE sid = %s
                  AND session_type IN ('15s_24hr', '15s_24hr_rinex')
                GROUP BY session_type
                """,
                (station_id,),
            )

            dates: Dict[str, Any] = {}
            for row in cur.fetchall():
                dates[row[0]] = row[1]

            conn.close()

            latest_raw = dates.get("15s_24hr")
            latest_rinex = dates.get("15s_24hr_rinex")

            if not latest_raw:
                return {
                    "has_data": False,
                    "latest_raw_date": None,
                    "latest_rinex_date": None,
                    "status": "unknown",
                    "message": "no data in archive",
                }

            # Determine status based on how current the data is
            from datetime import date as date_type

            today = date_type.today()
            yesterday = today - timedelta(days=1)
            days_behind = (yesterday - latest_raw).days

            raw_str = latest_raw.strftime("%Y-%m-%d")
            rinex_str = latest_rinex.strftime("%Y-%m-%d") if latest_rinex else "none"

            if days_behind <= 0:
                status = "ok"
                message = f"latest: {raw_str}, rinex: {rinex_str}"
            elif days_behind == 1:
                status = "warning"
                message = f"latest: {raw_str} (1 day behind), rinex: {rinex_str}"
            elif days_behind <= 3:
                status = "warning"
                message = (
                    f"latest: {raw_str} ({days_behind} days behind), rinex: {rinex_str}"
                )
            else:
                status = "critical"
                message = (
                    f"latest: {raw_str} ({days_behind} days behind), rinex: {rinex_str}"
                )

            return {
                "has_data": True,
                "latest_raw_date": latest_raw.isoformat(),
                "latest_rinex_date": latest_rinex.isoformat() if latest_rinex else None,
                "days_behind": days_behind,
                "status": status,
                "message": message,
            }

        except Exception as e:
            logger.debug(f"Archive status check failed for {station_id}: {e}")
            return {
                "has_data": False,
                "latest_raw_date": None,
                "latest_rinex_date": None,
                "status": "unknown",
                "message": f"DB check failed: {e}",
            }


def compute_checksum(data: Dict[str, Any]) -> str:
    """Compute checksum for health data.

    Args:
        data: Health data dictionary

    Returns:
        SHA256 checksum string
    """
    import json

    # Create stable JSON string (sorted keys)
    json_str = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(json_str.encode()).hexdigest()[:16]


@dataclass
class GapInfo:
    """Information about a detected gap (missing file)."""

    station_id: str
    session_type: str
    file_date: date
    file_hour: Optional[int]
    reason: str  # 'not_in_archive', 'not_in_db', 'removed_from_archive'
    expected_path: Optional[str] = None


@dataclass
class SyncResult:
    """Result of archive-to-database sync operation."""

    files_found: int
    files_added: int
    files_updated: int
    files_removed: int  # Detected as removed from archive
    errors: int


class GapDetector:
    """Detect gaps in downloaded files by comparing archive, database, and expected files.

    Combines ArchiveFileChecker and FileTracker to:
    1. Generate expected files for a date range
    2. Check archive for existing files
    3. Check DB for files marked as 'missing' or 'downloaded'
    4. Return files that need downloading (expected - archived - known_missing)
    5. Sync archive state to database
    6. Detect files that disappeared from archive
    """

    def __init__(
        self,
        data_prepath: Optional[str] = None,
        connection_string: Optional[str] = None,
    ):
        """Initialize gap detector.

        Args:
            data_prepath: Base archive path. If None, loads from config.
            connection_string: PostgreSQL connection string. If None, uses env vars.
        """
        self.archive_checker = ArchiveFileChecker(data_prepath)
        self.file_tracker = FileTracker(connection_string)
        self._config = None

    def _load_config(self):
        """Load receivers configuration."""
        if self._config is not None:
            return

        try:
            from ..config.receivers_config import ReceiversConfig

            self._config = ReceiversConfig()
        except Exception as e:
            logger.debug(f"Could not load receivers config: {e}")

    def _generate_expected_files(
        self,
        station_id: str,
        session_type: str,
        start_date: date,
        end_date: date,
    ) -> list[tuple[date, Optional[int]]]:
        """Generate list of expected files for a date range.

        Args:
            station_id: Station identifier
            session_type: Session type ('15s_24hr', '1Hz_1hr', 'status_1hr',
                         '15s_24hr_rinex', '1Hz_1hr_rinex')
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of (file_date, file_hour) tuples.
            file_hour is None for daily files, 0-23 for hourly files.
        """
        expected = []
        current = start_date

        while current <= end_date:
            if session_type in ("15s_24hr", "15s_24hr_rinex"):
                # Daily file
                expected.append((current, None))
            else:
                # Hourly file
                for hour in range(24):
                    expected.append((current, hour))
            current += timedelta(days=1)

        return expected

    def _check_archive_for_file(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        receiver_type: Optional[str] = None,
    ) -> tuple[bool, Optional[str], Optional[int]]:
        """Check if a file exists in the archive.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of file
            file_hour: Hour for hourly files
            receiver_type: Optional receiver type for extension detection

        Returns:
            Tuple of (exists, filepath, file_size)
        """
        # Build the datetime for the file
        if file_hour is not None:
            dt = datetime.combine(file_date, datetime.min.time()).replace(
                hour=file_hour
            )
        else:
            dt = datetime.combine(file_date, datetime.min.time())

        # Build expected path
        expected_path = self.archive_checker.build_archive_path(
            station_id, session_type, dt, receiver_type
        )

        # Check if file exists at the exact expected name (fast path).
        if os.path.isfile(expected_path):
            file_size = os.path.getsize(expected_path)
            return True, expected_path, file_size

        # Fall back to a compression-/case-invariant lookup: the archive may
        # hold the same data under a different compression suffix or extension
        # case (e.g. NetRS raw is uncompressed ``.T00`` in the long-term archive
        # while rek_new emits ``.T00.gz``). This also catches ``.Z``/``.bz2`` and
        # the reverse case (expected ``.gz`` but the plain file is present) that
        # the old ``+ ".gz"`` fallback missed. See utils/canonical_key.py.
        match = find_by_canonical_key(
            os.path.dirname(expected_path), os.path.basename(expected_path)
        )
        if match is not None:
            return True, match, os.path.getsize(match)

        return False, expected_path, None

    def sync_archive_to_db(
        self,
        station_id: str,
        session_type: str,
        start_date: date,
        end_date: date,
        receiver_type: Optional[str] = None,
    ) -> SyncResult:
        """Sync archive state to database.

        Scans archive for files and updates database:
        - Files found in archive: mark as 'archived'
        - Files in DB as 'downloaded' but not in archive: mark as 'removed'

        Args:
            station_id: Station identifier
            session_type: Session type
            start_date: Start date
            end_date: End date
            receiver_type: Optional receiver type

        Returns:
            SyncResult with counts of changes made
        """
        if not self.file_tracker.connect():
            logger.warning("Cannot connect to database for sync")
            return SyncResult(0, 0, 0, 0, 1)

        files_found = 0
        files_added = 0
        files_updated = 0
        files_removed = 0
        errors = 0

        # Get expected files for date range
        expected_files = self._generate_expected_files(
            station_id, session_type, start_date, end_date
        )

        try:
            conn = self.file_tracker._conn
            with conn.cursor() as cur:
                for file_date, file_hour in expected_files:
                    try:
                        # Check archive
                        exists, filepath, file_size = self._check_archive_for_file(
                            station_id,
                            session_type,
                            file_date,
                            file_hour,
                            receiver_type,
                        )

                        if exists:
                            files_found += 1
                            filename = os.path.basename(filepath) if filepath else None

                            # Check current DB status
                            if file_hour is None:
                                cur.execute(
                                    """SELECT id, status FROM file_tracking
                                    WHERE sid = %s AND session_type = %s
                                    AND file_date = %s AND file_hour IS NULL""",
                                    (station_id, session_type, file_date),
                                )
                            else:
                                cur.execute(
                                    """SELECT id, status FROM file_tracking
                                    WHERE sid = %s AND session_type = %s
                                    AND file_date = %s AND file_hour = %s""",
                                    (station_id, session_type, file_date, file_hour),
                                )

                            row = cur.fetchone()

                            if row is None:
                                # New file - add to DB as archived
                                cur.execute(
                                    """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'archived', %s)""",
                                    (
                                        station_id,
                                        session_type,
                                        file_date,
                                        file_hour,
                                        filename,
                                        file_size,
                                    ),
                                )
                                files_added += 1
                            elif row[1] != "archived":
                                # Existing file - update to archived
                                cur.execute(
                                    """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'archived', %s)""",
                                    (
                                        station_id,
                                        session_type,
                                        file_date,
                                        file_hour,
                                        filename,
                                        file_size,
                                    ),
                                )
                                files_updated += 1
                        else:
                            # File not in archive - check if it was previously marked as archived/downloaded
                            if file_hour is None:
                                cur.execute(
                                    """SELECT id, status FROM file_tracking
                                    WHERE sid = %s AND session_type = %s
                                    AND file_date = %s AND file_hour IS NULL
                                    AND status IN ('archived', 'downloaded')""",
                                    (station_id, session_type, file_date),
                                )
                            else:
                                cur.execute(
                                    """SELECT id, status FROM file_tracking
                                    WHERE sid = %s AND session_type = %s
                                    AND file_date = %s AND file_hour = %s
                                    AND status IN ('archived', 'downloaded')""",
                                    (station_id, session_type, file_date, file_hour),
                                )

                            row = cur.fetchone()
                            if row is not None:
                                # File was marked as archived/downloaded but is now missing.
                                # Keyed on the natural key, NOT the id just read:
                                # this connection may fan out to the pgdev mirror,
                                # where that id belongs to an unrelated file — an
                                # id-keyed UPDATE would mark the WRONG file removed.
                                update_sql = """UPDATE file_tracking SET
                                        status = 'removed',
                                        last_error = 'File removed from archive',
                                        updated_at = NOW()
                                    WHERE sid = %s AND session_type = %s
                                      AND file_date = %s AND file_hour {hour_pred}
                                      AND status IN ('archived', 'downloaded')"""
                                if file_hour is None:
                                    cur.execute(
                                        update_sql.format(hour_pred="IS NULL"),
                                        (station_id, session_type, file_date),
                                    )
                                else:
                                    cur.execute(
                                        update_sql.format(hour_pred="= %s"),
                                        (
                                            station_id,
                                            session_type,
                                            file_date,
                                            file_hour,
                                        ),
                                    )
                                files_removed += 1
                                logger.warning(
                                    f"File removed from archive: {station_id}/{session_type}/"
                                    f"{file_date}"
                                    + (
                                        f"/{file_hour:02d}"
                                        if file_hour is not None
                                        else ""
                                    )
                                )

                    except Exception as e:
                        logger.debug(f"Error syncing file: {e}")
                        errors += 1

                conn.commit()

        except Exception as e:
            logger.error(f"Error during archive sync: {e}")
            if self.file_tracker._conn:
                self.file_tracker._conn.rollback()
            errors += 1

        return SyncResult(
            files_found, files_added, files_updated, files_removed, errors
        )

    def find_gaps(
        self,
        station_id: str,
        session_type: str,
        start_date: date,
        end_date: date,
        receiver_type: Optional[str] = None,
        sync_first: bool = True,
        skip_missing_on_receiver: bool = True,
    ) -> list[GapInfo]:
        """Find gaps in downloaded files.

        Identifies files that:
        1. Are expected based on date range
        2. Are NOT in the archive
        3. Are NOT marked as 'missing' on the receiver (unless skip_missing_on_receiver=False)

        Args:
            station_id: Station identifier
            session_type: Session type ('15s_24hr', '1Hz_1hr', 'status_1hr')
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            receiver_type: Optional receiver type for archive path detection
            sync_first: Whether to sync archive to DB first (recommended)
            skip_missing_on_receiver: Skip files known to be missing on receiver

        Returns:
            List of GapInfo objects representing files that need downloading
        """
        gaps = []

        # Optionally sync archive state to DB first
        if sync_first:
            sync_result = self.sync_archive_to_db(
                station_id, session_type, start_date, end_date, receiver_type
            )
            logger.debug(
                f"Archive sync: found={sync_result.files_found}, "
                f"added={sync_result.files_added}, updated={sync_result.files_updated}, "
                f"removed={sync_result.files_removed}"
            )

        # Connect to database
        db_connected = self.file_tracker.connect()

        # Generate expected files
        expected_files = self._generate_expected_files(
            station_id, session_type, start_date, end_date
        )

        for file_date, file_hour in expected_files:
            # Check archive
            exists, expected_path, _ = self._check_archive_for_file(
                station_id, session_type, file_date, file_hour, receiver_type
            )

            if exists:
                # File exists in archive - no gap
                continue

            # File not in archive - check if we should skip it
            if db_connected and skip_missing_on_receiver:
                # Check if file is known to be missing on receiver
                if self.file_tracker.is_file_missing(
                    station_id, session_type, file_date, file_hour
                ):
                    # Skip - file confirmed missing on receiver
                    logger.debug(
                        f"Skipping {station_id}/{session_type}/{file_date}"
                        + (f"/{file_hour:02d}" if file_hour is not None else "")
                        + " - known missing on receiver"
                    )
                    continue

            # This is a gap - file should be downloaded
            gap = GapInfo(
                station_id=station_id,
                session_type=session_type,
                file_date=file_date,
                file_hour=file_hour,
                reason="not_in_archive",
                expected_path=expected_path,
            )
            gaps.append(gap)

        return gaps

    def get_gap_summary(
        self,
        station_ids: list[str],
        session_type: str,
        days_back: int = 7,
        receiver_types: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Get summary of gaps across multiple stations.

        Args:
            station_ids: List of station IDs
            session_type: Session type
            days_back: Number of days to check
            receiver_types: Optional dict of station_id -> receiver_type

        Returns:
            Dictionary with gap summary:
            - total_expected: Total expected files
            - total_archived: Total archived files
            - total_gaps: Total gaps (need download)
            - total_missing_on_receiver: Total confirmed missing on receiver
            - stations: Dict of station_id -> gap count
        """
        end_date = date.today() - timedelta(days=1)  # Yesterday
        start_date = end_date - timedelta(days=days_back - 1)

        summary = {
            "total_expected": 0,
            "total_archived": 0,
            "total_gaps": 0,
            "total_missing_on_receiver": 0,
            "stations": {},
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

        for station_id in station_ids:
            receiver_type = None
            if receiver_types:
                receiver_type = receiver_types.get(station_id)

            # Generate expected files
            expected = self._generate_expected_files(
                station_id, session_type, start_date, end_date
            )
            summary["total_expected"] += len(expected)

            # Find gaps
            gaps = self.find_gaps(
                station_id,
                session_type,
                start_date,
                end_date,
                receiver_type=receiver_type,
                sync_first=True,
                skip_missing_on_receiver=True,
            )

            archived = len(expected) - len(gaps)
            summary["total_archived"] += archived
            summary["total_gaps"] += len(gaps)
            summary["stations"][station_id] = {
                "expected": len(expected),
                "archived": archived,
                "gaps": len(gaps),
            }

        return summary

    @staticmethod
    def _parse_rinex_filename(
        filename: str, station_id: str
    ) -> tuple[Optional[date], Optional[int]]:
        """Parse a RINEX filename (short or long naming) to extract date and hour.

        Delegates to RinexNamer.parse_date_hour() for the shared parsing
        logic; kept as a static method to preserve the existing call-site
        API (self._parse_rinex_filename(...)).
        """
        from receivers.rinex.rinex_namer import RinexNamer

        return RinexNamer.parse_date_hour(filename, station_id=station_id)

    def scan_rinex_files(
        self,
        station_id: str,
        session_type: str,
        start_date: date,
        end_date: date,
        format_id: Optional[str] = None,
    ) -> tuple[int, int]:
        """Glob RINEX directory, parse dates from RINEX 2 short names, upsert to file_tracking.

        RINEX 2 short naming convention:
          SSSSdddS.YYt.Z  where SSSS=station, ddd=DOY, S=session letter, YY=year, t=type
          Session letter: '0' = daily, 'a'-'x' = hourly (a=00, b=01, ...)

        Cannot reuse sync_archive_to_db() because build_archive_path() generates SBF
        filenames, not RINEX names.

        Args:
            station_id: Station identifier
            session_type: RINEX session type (e.g., '15s_24hr_rinex', '1Hz_1hr_rinex')
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            format_id: Optional archive_format.format_id to tag scanned files with

        Returns:
            Tuple of (files_found, files_added) counts
        """
        import glob as glob_mod

        if not self.file_tracker.connect():
            logger.warning("Cannot connect to database for RINEX scan")
            return 0, 0

        files_found = 0
        files_added = 0

        # Collect unique year/month combinations in the date range
        unique_months: set[tuple[int, str]] = set()
        current = start_date
        while current <= end_date:
            unique_months.add(
                (
                    current.year,
                    datetime.combine(current, datetime.min.time())
                    .strftime("%b")
                    .lower(),
                )
            )
            current += timedelta(days=1)

        try:
            conn = self.file_tracker._conn
            with conn.cursor() as cur:
                for year, month in unique_months:
                    archive_dir = self.archive_checker.get_archive_directory(
                        station_id,
                        session_type,
                        year=year,
                        month=month,
                    )

                    if not os.path.isdir(archive_dir):
                        continue

                    # Glob for RINEX files: station*.??d.Z, station*.??o.Z, etc.
                    pattern = os.path.join(archive_dir, f"{station_id}*")
                    for filepath in glob_mod.glob(pattern):
                        if not os.path.isfile(filepath):
                            continue

                        fsize = os.path.getsize(filepath)
                        if fsize < MIN_ARCHIVE_FILE_SIZE:
                            continue

                        filename = os.path.basename(filepath)

                        try:
                            file_date, file_hour = self._parse_rinex_filename(
                                filename, station_id
                            )
                            if file_date is None:
                                continue

                            # Check date is in range
                            if file_date < start_date or file_date > end_date:
                                continue

                            files_found += 1

                            # Upsert to file_tracking
                            cur.execute(
                                """SELECT upsert_file_tracking(%s, %s, %s, %s::smallint, %s, 'archived', %s)""",
                                (
                                    station_id,
                                    session_type,
                                    file_date,
                                    file_hour,
                                    filename,
                                    fsize,
                                ),
                            )
                            result = cur.fetchone()
                            tracking_id = result[0] if result else None
                            files_added += 1

                            # Set format_id if provided. Keyed on the natural key,
                            # NOT the id upsert_file_tracking returned: that id is
                            # the PRIMARY's, and on a dual (mirror) connection it
                            # addresses an unrelated row on the mirror.
                            if format_id and tracking_id:
                                # The unique indexes on this grain are PARTIAL
                                # (idx_file_tracking_daily WHERE file_hour IS
                                # NULL / _hourly WHERE NOT NULL), so the hour
                                # predicate must be spelled `IS NULL` or `= %s`
                                # — `IS NOT DISTINCT FROM` matches neither and
                                # would fall back to a scan.
                                fmt_sql = """UPDATE file_tracking SET format_id = %s
                                    WHERE sid = %s AND session_type = %s
                                      AND file_date = %s AND file_hour {hour_pred}
                                      AND format_id IS DISTINCT FROM %s"""
                                if file_hour is None:
                                    cur.execute(
                                        fmt_sql.format(hour_pred="IS NULL"),
                                        (
                                            format_id,
                                            station_id,
                                            session_type,
                                            file_date,
                                            format_id,
                                        ),
                                    )
                                else:
                                    cur.execute(
                                        fmt_sql.format(hour_pred="= %s"),
                                        (
                                            format_id,
                                            station_id,
                                            session_type,
                                            file_date,
                                            file_hour,
                                            format_id,
                                        ),
                                    )

                        except (ValueError, IndexError):
                            continue  # skip unparseable filenames

                conn.commit()

        except Exception as e:
            logger.error(
                f"Error scanning RINEX files for {station_id}/{session_type}: {e}"
            )
            if self.file_tracker._conn:
                self.file_tracker._conn.rollback()

        return files_found, files_added

    def close(self):
        """Close database connection."""
        self.file_tracker.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def bootstrap_file_absence(conn, *, dry_run: bool = False) -> int:
    """Seed file_absence from existing file_tracking ``status='missing'`` rows.

    A one-time head start for the durable absence ledger (mig 056). Rows are
    seeded NON-TERMINAL (``terminal=false``): file_tracking has no terminal
    state — every ``missing`` row past the 24 h TTL is precisely the one mig 046
    means to RETRY (the "receiver caught up later" case), so freezing them here
    would be silent data loss. Terminal is earned only by go-forward,
    time-spanned confirmations via ``record_file_absence``.

    ``first_confirmed_at`` carries the file_tracking ``first_checked`` so the
    observed miss-span is preserved; only RAW download sessions are seeded
    (receiver absence is about the raw file, never the derived rinex). NULL-safe
    on file_hour; idempotent (skips slots already present). Returns rows seeded.
    """
    count_sql = (
        "SELECT count(*) FROM file_tracking ft WHERE ft.status='missing' "
        "AND ft.session_type NOT LIKE %s AND NOT EXISTS ("
        "  SELECT 1 FROM file_absence fa WHERE fa.source_location='receiver'"
        "    AND fa.sid=ft.sid AND fa.session_type=ft.session_type"
        "    AND fa.file_date=ft.file_date"
        "    AND fa.file_hour IS NOT DISTINCT FROM ft.file_hour)"
    )
    insert_sql = """
        INSERT INTO file_absence
            (source_location, sid, session_type, file_date, file_hour,
             confirmations, first_confirmed_at, last_confirmed_at, terminal)
        SELECT 'receiver', ft.sid, ft.session_type, ft.file_date, ft.file_hour,
               1, ft.first_checked, ft.last_checked, false
        FROM file_tracking ft
        WHERE ft.status = 'missing'
          AND ft.session_type NOT LIKE %s
          AND NOT EXISTS (
              SELECT 1 FROM file_absence fa
              WHERE fa.source_location = 'receiver'
                AND fa.sid = ft.sid
                AND fa.session_type = ft.session_type
                AND fa.file_date = ft.file_date
                AND fa.file_hour IS NOT DISTINCT FROM ft.file_hour
          )
    """
    with conn.cursor() as cur:
        if dry_run:
            cur.execute(count_sql, (r"%\_rinex",))
            return cur.fetchone()[0]
        cur.execute(insert_sql, (r"%\_rinex",))
        seeded = cur.rowcount
    conn.commit()
    logger.info("bootstrap_file_absence: seeded %d non-terminal rows", seeded)
    return seeded
