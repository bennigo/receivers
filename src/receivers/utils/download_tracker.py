"""Shared download tracking utility for all receiver types.

Provides receiver-independent file tracking for:
- Skipping known missing files before download
- Marking files as downloaded after success
- Marking files as missing when not found on server
"""

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class DownloadTracker:
    """Track file downloads across all receiver types.

    Provides a unified interface for file tracking that can be used
    by PolaRX5, NetR9, NetRS, G10, and other receivers.
    """

    def __init__(self, station_id: str, session: str):
        """Initialize download tracker.

        Args:
            station_id: Station identifier (e.g., 'ISFS', 'MANA')
            session: Session type (e.g., '15s_24hr', '1Hz_1hr')
        """
        self.station_id = station_id
        self.session = session
        self.is_hourly = "1hr" in session.lower()
        self._tracker = None
        self._connected = False

    def connect(self) -> bool:
        """Connect to file tracking database.

        Returns:
            True if connected successfully
        """
        try:
            from ..health import FileTracker

            self._tracker = FileTracker()
            self._connected = self._tracker.connect()
            if self._connected:
                logger.debug(
                    f"Download tracker connected for {self.station_id}/{self.session}"
                )
            else:
                logger.debug("Download tracking disabled (database unavailable)")
            return self._connected
        except ImportError:
            logger.debug("Download tracking disabled (psycopg2 not installed)")
            return False
        except Exception as e:
            logger.debug(f"Download tracking disabled: {e}")
            return False

    def close(self):
        """Close database connection."""
        if self._tracker:
            self._tracker.close()
            self._tracker = None
            self._connected = False

    def is_file_missing(self, file_date: date, file_hour: Optional[int] = None) -> bool:
        """Check if a file is known to be missing.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily

        Returns:
            True if file is known to be missing (should skip)
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.is_file_missing(
            self.station_id, self.session, file_date, hour
        )

    def mark_downloaded(
        self,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        remote_file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully downloaded.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily
            filename: Original filename
            file_size: File size in bytes
            remote_file_size: File size reported by receiver (FTP SIZE / HTTP Content-Length)

        Returns:
            True if successfully recorded
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.mark_file_downloaded(
            self.station_id,
            self.session,
            file_date,
            hour,
            filename,
            file_size,
            remote_file_size=remote_file_size,
        )

    def mark_archived(
        self,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        remote_file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully archived.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily
            filename: Original filename
            file_size: File size in bytes
            remote_file_size: File size reported by receiver

        Returns:
            True if successfully recorded
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.mark_file_archived(
            self.station_id,
            self.session,
            file_date,
            hour,
            filename,
            file_size,
            remote_file_size=remote_file_size,
        )

    def record_horizon(self, remote_filenames: list) -> bool:
        """Record the oldest file this station still holds on the receiver.

        The dynamic receiver horizon (unified file index slice-2b.3): the oldest
        parseable (date, hour) across the receiver's CURRENT listing becomes the
        real fetch floor for ``missing_on_receiver`` (and, later, the retention
        floor for prune). Pass the FULL remote listing for the session, OR — the
        sanctioned adaptation used by the dedicated horizon probe — the listing of
        the CONFIRMED-OLDEST date directory: either way the minimum parseable
        (date, hour) is the true receiver floor, so a confirmed-oldest-dir subset
        is valid. What is NOT valid is a recent date-filtered subset (a normal
        download run), which would record a too-recent floor. Best-effort — never
        raises. Returns True if a horizon was recorded.
        """
        if not self._connected or not remote_filenames:
            return False
        return record_receiver_horizon(
            self._tracker._conn,
            self.station_id,
            self.session,
            self.is_hourly,
            remote_filenames,
        )

    def mark_missing(
        self,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Mark a file as missing on the receiver.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily
            filename: Expected filename

        Returns:
            True if successfully recorded
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.mark_file_missing(
            self.station_id, self.session, file_date, hour, filename
        )

    def filter_known_missing(
        self, file_datetime_dict: Dict[datetime, Any]
    ) -> Tuple[Dict[datetime, Any], List[datetime]]:
        """Filter out files known to be missing.

        Args:
            file_datetime_dict: Dictionary with datetime keys

        Returns:
            Tuple of (filtered_dict, skipped_datetimes)
        """
        if not self._connected:
            return file_datetime_dict, []

        filtered = {}
        skipped = []

        for dt, value in file_datetime_dict.items():
            file_date = dt.date() if hasattr(dt, "date") else dt
            file_hour = dt.hour if self.is_hourly and hasattr(dt, "hour") else None

            if self.is_file_missing(file_date, file_hour):
                skipped.append(dt)
                logger.info(f"⏭️  Skipping {dt} (known missing, not retrying)")
            else:
                filtered[dt] = value

        return filtered, skipped

    def track_download_results(
        self,
        requested_files: Dict[datetime, Any],
        downloaded_files: List[str],
        failed_files: Optional[Set[datetime]] = None,
    ) -> Dict[str, int]:
        """Track results of a download operation.

        Args:
            requested_files: Dict of datetime -> file info that were requested
            downloaded_files: List of successfully downloaded file paths
            failed_files: Optional set of datetimes that failed (404/missing)

        Returns:
            Dict with counts: {'downloaded': N, 'missing': M}
        """
        if not self._connected:
            return {"downloaded": 0, "missing": 0}

        downloaded_count = 0
        missing_count = 0

        # Track downloaded files
        for dt, value in requested_files.items():
            file_date = dt.date() if hasattr(dt, "date") else dt
            file_hour = dt.hour if self.is_hourly and hasattr(dt, "hour") else None

            # Check if this datetime's file was downloaded
            # Value structure varies by receiver, try to extract filename
            filename = None
            if isinstance(value, tuple) and len(value) >= 2:
                filename = value[1]  # (archive_path, filename) structure
            elif isinstance(value, str):
                filename = Path(value).name

            # Check if file was downloaded (by matching filename in downloaded_files)
            was_downloaded = False
            file_size = None
            if filename:
                for downloaded_path in downloaded_files:
                    if (
                        filename in downloaded_path
                        or Path(downloaded_path).name == filename
                    ):
                        was_downloaded = True
                        try:
                            file_size = Path(downloaded_path).stat().st_size
                        except OSError:
                            pass
                        break

            if was_downloaded:
                self.mark_downloaded(file_date, file_hour, filename, file_size)
                downloaded_count += 1
            elif failed_files and dt in failed_files:
                self.mark_missing(file_date, file_hour, filename)
                missing_count += 1

        return {"downloaded": downloaded_count, "missing": missing_count}

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def record_receiver_horizon(
    conn,
    station_id: str,
    session: str,
    is_hourly: bool,
    remote_filenames: list,
) -> bool:
    """Upsert the receiver horizon from a listing on a given DB connection.

    Shared upsert for both the hot download path (:meth:`DownloadTracker.
    record_horizon`) and the dedicated ``receiver_horizon_probe`` scheduler job,
    which reuses ONE connection across the whole fleet instead of opening one per
    (station, session).

    The oldest parseable ``(date, hour)`` across ``remote_filenames`` becomes the
    ``receiver_horizon`` row. Guard: a horizon with ``oldest_date`` in the FUTURE
    is a parse artefact (e.g. a day-of-year filename read against the wrong year)
    — recording it would push the fetch floor past today and silently drop every
    fetchable slot, so it is rejected and nothing is written (the static
    ``receiver_buffer_depth`` floor stays in force). Best-effort — never raises;
    returns True only when a row was written.
    """
    if conn is None or not remote_filenames:
        return False
    oldest = _oldest_from_listing(remote_filenames, station_id)
    if oldest is None:
        return False
    return upsert_receiver_horizon(
        conn, station_id, session, is_hourly, oldest[0], oldest[1]
    )


def upsert_receiver_horizon(
    conn,
    station_id: str,
    session: str,
    is_hourly: bool,
    oldest_date: Optional[date],
    oldest_hour: Optional[int],
) -> bool:
    """Write a pre-parsed ``(oldest_date, oldest_hour)`` horizon to the DB.

    The date-source-agnostic core of :func:`record_receiver_horizon`. Trimble/
    Leica derive ``(date, hour)`` from filenames (via ``_oldest_from_listing``);
    Septentrio derives the date from the ``%y%j`` day-directory name — the daily
    SBF/RINEX filenames there don't carry a full timestamp — and both funnel
    through here. Never raises; returns True only when a row was written.
    """
    if conn is None or oldest_date is None:
        return False
    # A future oldest_date can only be a parse artefact (e.g. a day-of-year name
    # read against the wrong year) — never a real receiver floor. Reject it so a
    # too-recent floor can never drop fetchable slots (unified file index
    # slice-2b.3 silent-under-report guard).
    if oldest_date > date.today():
        logger.debug(
            "record_horizon: rejecting future oldest_date %s for %s/%s "
            "(parse artefact)",
            oldest_date,
            station_id,
            session,
        )
        return False
    # daily sessions have no meaningful hour floor
    hour = oldest_hour if is_hourly else None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO receiver_horizon
                       (sid, session_type, oldest_date, oldest_hour, observed_at)
                   VALUES (%s, %s, %s, %s::smallint, now())
                   ON CONFLICT (sid, session_type) DO UPDATE SET
                       oldest_date = EXCLUDED.oldest_date,
                       oldest_hour = EXCLUDED.oldest_hour,
                       observed_at = now()""",
                (station_id, session, oldest_date, hour),
            )
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001 — best-effort; never break a download
        logger.debug(f"record_horizon failed for {station_id}: {e}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _oldest_from_listing(
    filenames: list, station_id: str
) -> Optional[Tuple[date, Optional[int]]]:
    """Oldest (date, hour) across a receiver listing, or None if none parse.

    Sorts by (date, hour) treating a daily file's NULL hour as earliest. Used to
    derive the receiver horizon (:meth:`DownloadTracker.record_horizon`).
    """
    parsed = []
    for fn in filenames:
        base = fn.rsplit("/", 1)[-1] if fn else fn
        p = parse_date_from_filename(base, station_id)
        if p is not None:
            parsed.append(p)
    if not parsed:
        return None
    return min(parsed, key=lambda p: (p[0], -1 if p[1] is None else p[1]))


def _is_hourly_session(session_type: Optional[str]) -> bool:
    """True for an hourly archive session (e.g. ``1Hz_1hr``, ``status_1hr``).

    Hourly session names end in ``_1hr``; daily sessions end in ``_24hr``. The
    session type is the only thing that disambiguates a short-name letter ``a``:
    hour 0 of an hourly file vs. the daily file of a daily/Leica session — the
    filename alone cannot tell them apart.
    """
    return session_type is not None and session_type.endswith("_1hr")


def parse_date_from_filename(
    filename: str,
    station_id: str,
    default_year: Optional[int] = None,
    session_type: Optional[str] = None,
) -> Optional[Tuple[date, Optional[int]]]:
    """Parse date and hour from various filename formats.

    Supports formats:
    - Septentrio: ISFS202601170000a.sbf.gz -> 2026-01-17, None (daily)
    - Septentrio hourly: ISFS202601170100b.sbf.gz -> 2026-01-17, 1
    - Leica: SKFC266a.m00 -> day 266 of current year
    - Trimble: Similar patterns

    Short day-of-year names (RINEX-2 short ``DDD<letter>.YY``, Leica ``.m00``)
    carry only the day-of-year — the 4-digit year is NOT recoverable from the
    name (the ``.YY`` suffix is century-ambiguous). Pass ``default_year`` (and,
    for hourly hour-0 disambiguation, ``session_type``) whenever the caller
    knows them from context — e.g. the archive ``YYYY/…/SESSION/`` path — so the
    timestamp is anchored, not guessed as "now". This is what keeps a re-rinex
    of *historical* data from being catalogued under the current year.

    Args:
        filename: Filename to parse
        station_id: Station ID for validation
        default_year: Authoritative 4-digit year for day-of-year names. When
            ``None`` (live FTP listings, in-flight downloads) the current year
            is assumed, as before.
        session_type: Archive session (e.g. ``1Hz_1hr``). Disambiguates the
            short-name letter ``a`` — hour 0 for an hourly session, the daily
            file otherwise. When ``None`` the legacy behaviour (``a`` = daily)
            is preserved.

    Returns:
        Tuple of (date, hour) or None if cannot parse
    """
    # Try Septentrio format: SSSS20260117HHMM[a|b].sbf.gz
    # a = daily, b = hourly
    septentrio_match = re.match(
        rf"^{re.escape(station_id)}(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})([ab])",
        filename,
        re.IGNORECASE,
    )
    if septentrio_match:
        year = int(septentrio_match.group(1))
        month = int(septentrio_match.group(2))
        day = int(septentrio_match.group(3))
        hour = int(septentrio_match.group(4))
        session_type = septentrio_match.group(6)  # 'a' = daily, 'b' = hourly
        file_date = date(year, month, day)
        file_hour = hour if session_type == "b" else None
        return file_date, file_hour

    # Try Leica / RINEX-2 short name: SSSS[DDD][f].YY[t] (or Leica .m00)
    # DDD = day of year; f = file-sequence char: '0' = the DAILY file,
    # a..x = hourly (hours 0..23). '0' is a digit, so match it explicitly.
    leica_match = re.match(
        rf"^{re.escape(station_id)}(\d{{3}})([a-x0])",
        filename,
        re.IGNORECASE,
    )
    if leica_match:
        day_of_year = int(leica_match.group(1))
        session_letter = leica_match.group(2).lower()
        # Anchor the year to the caller-supplied context (the archive YYYY/
        # directory) — the name's own ".YY" suffix is century-ambiguous, so
        # fall back to "now" only when the caller has no context. Using the
        # real year also makes the day-of-year offset leap-year-correct.
        year = default_year if default_year is not None else datetime.now().year
        try:
            file_date = date(year, 1, 1) + timedelta(days=day_of_year - 1)
            # File-sequence char → hour: '0' is the RINEX-2 daily file (no
            # hour). a..x map to hours 0..23 for an hourly session; for a daily
            # session (or no session context) the letter 'a' also denotes the
            # daily file, so it stays hourless. b..x still map to 1..23.
            if session_letter == "0" or (
                session_letter == "a" and not _is_hourly_session(session_type)
            ):
                file_hour = None
            else:
                file_hour = ord(session_letter) - ord("a")
            return file_date, file_hour
        except ValueError:
            pass

    return None
