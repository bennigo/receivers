"""Leica G10 GNSS receiver implementation.

This module provides support for Leica G10 receivers via FTP downloads of compressed
.m00.zip files from the receiver's SD Card storage path.

Based on the legacy getLeica script pattern:
- FTP anonymous login to port 2160
- Files stored at: /SD Card/Data/15s_24hr/
- Remote format: {STATION}{DOY}a.m00.zip
- Archive format: {STATION}YYYYMMDDHHMM0000a.m00.gz
"""

import gzip
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from ..base.exceptions import ConfigurationError, ConnectionError, DownloadError
from ..base.receiver import BaseReceiver

# Phase 1 utilities (feature-flagged)
from ..utils.archive_validator import ArchiveValidator
from ..utils.download_tracker import DownloadTracker
from ..utils.performance_recorder import (
    create_performance_metrics,
    record_performance_metrics,
)
from ..utils.session_parser import parse_session_parameters
from ..utils.time_processor import TimeParameterProcessor
from ..utils.yield_guard import build_yield_guard
from .leica_ftp_download_client import LeicaFTPDownloader


class LeicaG10(BaseReceiver):
    """Leica G10 GNSS receiver implementation.

    Provides FTP-based file downloads from Leica G10 receivers with:
    - Anonymous FTP access to SD Card storage
    - Automatic unzipping of compressed .m00.zip files
    - Progress tracking and error handling
    - Archive management with compression
    """

    def __init__(
        self,
        station_id: str,
        station_info: Dict[str, Any],
        loglevel: int = logging.INFO,
    ):
        """Initialize Leica G10 receiver.

        Args:
            station_id: Station identifier
            station_info: Station configuration from gps_parser
            loglevel: Logging level
        """
        super().__init__(station_id, station_info)

        self.loglevel = loglevel
        self.logger = self._get_logger(loglevel)
        self.logger.info(f"Initialized Leica G10 receiver for {station_id}")

        # Get tmp_dir from centralized configuration
        self.tmp_dir = self.receivers_config.get_tmp_dir()

        # Get G10-specific configuration
        self.leica_config = self.receivers_config.get_receiver_config("g10")

        # Validate station configuration
        self._validate_station_config()

        # Initialize FTP downloader with same log level
        self.ftp_downloader = LeicaFTPDownloader(station_id, station_info, loglevel)

        # Connection status
        self.connection_status = {"router": False, "receiver": False}

        # Phase 1 utilities (always enabled - Phase 3B)
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)

    def _get_logger(self, level: int = logging.INFO) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        # Use parent logger's configuration for consistent formatting
        return logger

    def _validate_station_config(self):
        """Validate station configuration for Leica G10."""
        try:
            # Check for required configuration keys
            # Handle both new format and legacy gps_parser format
            if "router" in self.station_info and "receiver" in self.station_info:
                # New format - already validated in parent class
                pass
            elif "station" in self.station_info:
                # Legacy gps_parser format - convert to expected format
                station_config = self.station_info["station"]
                router_ip = station_config.get("router_ip")
                if not router_ip:
                    raise ConfigurationError(
                        f"Missing router_ip for station {self.station_id}"
                    )

                # Leica uses FTP port from config
                ftp_port = self.leica_config.get("ftp_port", 2160)

                # Create expected structure
                self.station_info["router"] = {"ip": router_ip}
                self.station_info["receiver"] = {"ftpport": int(ftp_port)}
            else:
                raise ConfigurationError(
                    f"Invalid station configuration structure for {self.station_id}"
                )

        except (KeyError, ValueError) as e:
            raise ConfigurationError(
                f"Invalid station configuration for {self.station_id}: {e}"
            )

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to Leica G10 receiver.

        Returns:
            Dictionary with connection status information
        """
        try:
            self.logger.debug(f"Testing connection to {self.station_id}")

            # Test FTP connection
            ftp_test = self.ftp_downloader.test_connection()

            # Update internal connection status
            self.connection_status = {
                "router": ftp_test["success"],
                "receiver": ftp_test["success"],
            }

            return {
                "station_id": self.station_id,
                "ip": self.station_info["router"]["ip"],
                "port": self.station_info["receiver"]["ftpport"],
                "router": ftp_test["success"],
                "receiver": ftp_test["success"],
                "ftp_test": ftp_test,
                "error": ftp_test.get("error"),
            }

        except Exception as e:
            error_msg = f"Connection test failed: {e}"
            self.logger.error(error_msg)

            self.connection_status = {"router": False, "receiver": False}

            return {
                "station_id": self.station_id,
                "router": False,
                "receiver": False,
                "error": error_msg,
            }

    def download_data(
        self,
        start: Union[datetime, str],
        end: Union[datetime, str],
        session: str = "15s_24hr",
        sync: bool = True,
        clean_tmp: bool = True,
        archive: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        """Download data from Leica G10 receiver for specified time period.

        Args:
            start: Start time for data download
            end: End time for data download
            session: Data session type (only '15s_24hr' supported by Leica G10)
            sync: Whether to sync missing files
            clean_tmp: Whether to clean temporary download directory
            archive: Whether to archive downloaded files
            **kwargs: Additional receiver-specific parameters (including loglevel)

        Returns:
            Dictionary with download results and file information
        """
        # Per-file download retry budget (propagated from CLI --max-retries)
        max_retries = kwargs.get("max_retries", 3)

        start_time = time.time()

        # Quick reachability check to skip offline stations fast
        if not self._quick_ping():
            self.logger.warning(
                f"Station {self.station_id} is unreachable (ping failed), skipping download"
            )
            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "status": "unreachable",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": "Station unreachable (ping failed)",
                "duration": time.time() - start_time,
            }
        ftp_port = int(self.station_info.get("receiver", {}).get("ftpport", 2160))
        if not self._quick_tcp_check(ftp_port):
            self.logger.warning(
                f"Station {self.station_id} FTP port {ftp_port} not responding, skipping download"
            )
            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "status": "unreachable",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": f"FTP port {ftp_port} not responding",
                "duration": time.time() - start_time,
            }

        # Check if loglevel was passed and create new FTP downloader with correct level
        loglevel = kwargs.get("loglevel", self.loglevel)
        if loglevel != self.loglevel:
            self.logger.debug(f"Updating log level from {self.loglevel} to {loglevel}")
            self.ftp_downloader = LeicaFTPDownloader(
                self.station_id, self.station_info, loglevel
            )

        try:
            self.logger.info(f"Starting download for Leica G10 {self.station_id}")

            # Parse start/end times
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))

            # Use the same time range processing as other receivers
            self.logger.info(f"Checking {session} sessions from {start} to {end}")

            # Set up directories
            # Include session in tmp path to prevent filename collisions between sessions
            tmp_dir_path = Path(self.tmp_dir) / self.station_id / session
            tmp_dir_path.mkdir(parents=True, exist_ok=True)

            # Generate file list based on session type and time range
            files_dict, archive_files_dict = self._generate_file_list(
                start, end, session, **kwargs
            )

            # Log file generation info
            self.logger.info(f"Generated {len(files_dict)} timestamps")

            if not files_dict:
                self.logger.info("No files to check for this time range")
                return {
                    "station_id": self.station_id,
                    "receiver_type": "G10",
                    "status": "no_files",
                    "files_downloaded": 0,
                    "downloaded_files": [],
                    "duration": time.time() - start_time,
                }

            # See netrs.py for the rationale: pass None for tmp_dir so a
            # partial .m00.zip doesn't get auto-flushed as complete. Same
            # class of bug as the Trimble .T00/.T02 case; the gzip-only
            # truncation check added in PR #41 doesn't apply to .m00 archives.
            (
                missing_files_dict,
                files_found_in_archive,
                validated_files,
                files_in_tmp_dict,
            ) = self.archive_validator.batch_validate_archives(
                files_dict, archive_files_dict, None
            )

            # Archive files from tmp if found and archive flag is set
            if files_in_tmp_dict and archive:
                self.logger.info(
                    f"Found {len(files_in_tmp_dict)} files in tmp directory that need archiving"
                )
                self.logger.info(
                    f"Archiving {len(files_in_tmp_dict)} files from tmp directory..."
                )

                from ..utils.file_archiver import ArchiveMode, FileArchiver

                with FileArchiver(
                    mode=ArchiveMode.BULK,
                    logger=self.logger,
                    yield_guard=build_yield_guard(self.logger),
                ) as archiver:
                    for filename, tmp_path in files_in_tmp_dict.items():
                        archive_dest = archive_files_dict.get(filename)
                        if archive_dest:
                            archiver.archive_file(
                                tmp_path,
                                Path(archive_dest),
                                compress=False,  # Files already compressed (.m00.zip format)
                                remove_tmp=True,
                            )

                stats = archiver.get_statistics()
                self.logger.info(
                    f"Archived {stats['successful']}/{len(files_in_tmp_dict)} files from tmp to archive"
                )

            # Log validation results
            archive_dir = (
                Path(next(iter(archive_files_dict.values()))).parent
                if archive_files_dict
                else None
            )
            self.logger.info(f"Validated {validated_files} files total")
            if files_found_in_archive > 0:
                self.logger.info(
                    f"Found {files_found_in_archive} files already archived in {archive_dir}, skipping re-download"
                )

            if not missing_files_dict:
                self.logger.info(f"Archive is up to date ({archive_dir})")
                self._track_validated_files(files_dict, session, start)
                return {
                    "station_id": self.station_id,
                    "receiver_type": "G10",
                    "status": "up_to_date",
                    "files_checked": len(files_dict),
                    "files_missing": 0,
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                }

            self.logger.info(
                f"Missing files: {len(missing_files_dict)} (out of {len(files_dict)} total)"
            )

            # Filter out known missing files using download tracker
            # Skip this filter when retry_missing=True (scheduler always retries)
            import re
            from datetime import date, timedelta

            retry_missing = kwargs.get("retry_missing", False)
            if retry_missing:
                self.logger.debug("Retry mode: skipping known-missing filter")
            else:
                try:
                    with DownloadTracker(self.station_id, session) as tracker:
                        if tracker._connected:
                            filtered_missing = {}
                            skipped_count = 0
                            for filename, remote_dir in missing_files_dict.items():
                                # Parse date from filename
                                match = re.match(
                                    rf"^{self.station_id}(\d{{3}})([a-x])",
                                    filename,
                                    re.IGNORECASE,
                                )
                                if match:
                                    day_of_year = int(match.group(1))
                                    session_letter = match.group(2).lower()
                                    file_year = (
                                        start.year
                                        if hasattr(start, "year")
                                        else datetime.now().year
                                    )
                                    file_date = date(file_year, 1, 1) + timedelta(
                                        days=day_of_year - 1
                                    )
                                    file_hour = (
                                        None
                                        if session_letter == "a"
                                        else ord(session_letter) - ord("a")
                                    )
                                    if tracker.is_file_missing(file_date, file_hour):
                                        self.logger.info(
                                            f"⏭️  Skipping {filename} (known missing, not retrying)"
                                        )
                                        skipped_count += 1
                                        continue
                                filtered_missing[filename] = remote_dir
                            if skipped_count > 0:
                                self.logger.info(
                                    f"Skipped {skipped_count} known missing files"
                                )
                            missing_files_dict = filtered_missing
                except Exception as e:
                    self.logger.debug(f"File tracking check failed: {e}")

            # Download files if sync is enabled
            downloaded_files = []
            if sync:
                if missing_files_dict:
                    # Create callback for immediate unzip+archive when archiving enabled
                    process_callback = None
                    if archive:

                        def immediate_process_callback(zip_path: str) -> Optional[str]:
                            """Unzip and archive immediately after download."""
                            # Unzip the file
                            unzipped = self._unzip_single_file(zip_path)
                            if not unzipped:
                                return None

                            # Archive the unzipped .m00 file
                            m00_filename = Path(unzipped).name
                            if m00_filename in archive_files_dict:
                                archive_path = Path(archive_files_dict[m00_filename])

                                from ..utils.file_archiver import (
                                    ArchiveMode,
                                    FileArchiver,
                                )

                                with FileArchiver(
                                    mode=ArchiveMode.IMMEDIATE,
                                    logger=self.logger,
                                    yield_guard=build_yield_guard(self.logger),
                                ) as archiver:
                                    success = archiver.archive_file(
                                        Path(unzipped),
                                        archive_path,
                                        compress=True,
                                        remove_tmp=True,
                                    )

                                if success:
                                    return str(archive_path)

                            return unzipped

                        process_callback = immediate_process_callback

                    downloaded_files = self.ftp_downloader.download_files(
                        missing_files_dict,
                        tmp_dir_path,
                        clean_tmp,
                        process_callback,
                        max_retries=max_retries,
                    )
                else:
                    self.logger.info(
                        f"Archive is up to date ({archive_dir}) - no files to download"
                    )
            else:
                self.logger.info("Sync disabled - skipping actual download")

            # Files are processed (unzipped+archived) inline by callback when archive=True
            # downloaded_files contains archive paths when archiving is enabled
            processed_files = downloaded_files
            archived_files = []
            if archive and processed_files:
                # After archiving, create list of archived file paths
                for file_path in processed_files:
                    file_path_obj = Path(file_path)
                    filename = file_path_obj.name  # SKFC266a.m00
                    if filename in archive_files_dict:
                        archived_files.append(archive_files_dict[filename])

                # Use archived files for reporting if archiving was successful
                final_files = archived_files if archived_files else processed_files
            else:
                final_files = processed_files

            # Calculate bytes from final file locations
            final_bytes = 0
            for f in final_files:
                try:
                    if Path(f).exists():
                        final_bytes += Path(f).stat().st_size
                except OSError:
                    pass

            # Record performance metrics
            duration = time.time() - start_time
            performance_metrics = create_performance_metrics(
                success=len(final_files) > 0 if sync else True,
                duration=duration,
                bytes_downloaded=final_bytes,
                connection_time=getattr(
                    self.ftp_downloader, "_last_connection_time", 0.0
                ),
            )
            record_performance_metrics(
                self.station_id, performance_metrics, self.logger
            )

            # Track downloaded files in database (only when sync is enabled)
            if sync:
                try:
                    # Collect remote file sizes from FTP downloader (if available)
                    ftp_remote_sizes = getattr(self.ftp_downloader, "remote_sizes", {})

                    with DownloadTracker(self.station_id, session) as tracker:
                        if tracker._connected:
                            import re
                            from datetime import date, timedelta

                            # Track successful downloads and collect downloaded dates
                            downloaded_dates = set()  # Set of (date, hour) tuples
                            for file_path in final_files:
                                filename = Path(file_path).name
                                # Look up remote size by original filename (may be .zip name)
                                remote_size = ftp_remote_sizes.get(filename)

                                # Try archive format first: SKFC202601180000a.m00.gz (YYYYMMDDHHMM + session)
                                match = re.match(
                                    rf"^{self.station_id}(\d{{4}})(\d{{2}})(\d{{2}})(\d{{4}})([a-x])",
                                    filename,
                                    re.IGNORECASE,
                                )
                                if match:
                                    year = int(match.group(1))
                                    month = int(match.group(2))
                                    day = int(match.group(3))
                                    hhmm = match.group(4)  # e.g., "2000" for hour 20
                                    file_date = date(year, month, day)
                                    file_hour_from_name = int(hhmm[:2])
                                    file_hour = (
                                        None
                                        if file_hour_from_name == 0
                                        else file_hour_from_name
                                    )
                                    file_size = (
                                        Path(file_path).stat().st_size
                                        if Path(file_path).exists()
                                        else None
                                    )
                                    tracker.mark_downloaded(
                                        file_date,
                                        file_hour,
                                        filename,
                                        file_size,
                                        remote_file_size=remote_size,
                                    )
                                    downloaded_dates.add((file_date, file_hour))
                                else:
                                    # Try original Leica format: SKFC018a.m00 (DOY + session)
                                    match = re.match(
                                        rf"^{self.station_id}(\d{{3}})([a-x])",
                                        filename,
                                        re.IGNORECASE,
                                    )
                                    if match:
                                        day_of_year = int(match.group(1))
                                        session_letter = match.group(2).lower()
                                        file_year = (
                                            start.year
                                            if hasattr(start, "year")
                                            else datetime.now().year
                                        )
                                        file_date = date(file_year, 1, 1) + timedelta(
                                            days=day_of_year - 1
                                        )
                                        file_hour = (
                                            None
                                            if session_letter == "a"
                                            else ord(session_letter) - ord("a")
                                        )
                                        file_size = (
                                            Path(file_path).stat().st_size
                                            if Path(file_path).exists()
                                            else None
                                        )
                                        tracker.mark_downloaded(
                                            file_date,
                                            file_hour,
                                            filename,
                                            file_size,
                                            remote_file_size=remote_size,
                                        )
                                        downloaded_dates.add((file_date, file_hour))

                            # Track missing files: only mark file_tracking
                            # 'missing' when the receiver explicitly said the
                            # file isn't on disk (FTP 550). Connection /
                            # transport errors leave the row absent.
                            file_outcomes = getattr(
                                self.ftp_downloader, "file_outcomes", {}
                            )
                            for req_filename in missing_files_dict.keys():
                                if file_outcomes.get(req_filename) != "not_found":
                                    continue
                                # Parse date from original Leica format: SKFC016a.m00
                                match = re.match(
                                    rf"^{self.station_id}(\d{{3}})([a-x])",
                                    req_filename,
                                    re.IGNORECASE,
                                )
                                if match:
                                    day_of_year = int(match.group(1))
                                    session_letter = match.group(2).lower()
                                    file_year = (
                                        start.year
                                        if hasattr(start, "year")
                                        else datetime.now().year
                                    )
                                    file_date = date(file_year, 1, 1) + timedelta(
                                        days=day_of_year - 1
                                    )
                                    file_hour = (
                                        None
                                        if session_letter == "a"
                                        else ord(session_letter) - ord("a")
                                    )
                                    # Only mark as missing if this date/hour wasn't downloaded
                                    if (file_date, file_hour) not in downloaded_dates:
                                        tracker.mark_missing(
                                            file_date, file_hour, req_filename
                                        )
                except Exception as e:
                    self.logger.debug(f"File tracking failed: {e}")

            # All files failed — don't report "completed" with 0 downloads.
            # Surface dominant per-file error + connection target so the DB log
            # is self-explanatory.
            if sync and missing_files_dict and not final_files:
                _last_err = getattr(self.ftp_downloader, "last_file_error", None)
                _ip = self.station_info["router"]["ip"]
                _port = self.station_info["receiver"].get("ftpport", "?")
                _msg = f"All file downloads failed (0 of {len(missing_files_dict)}) @ {_ip}:{_port}"
                if _last_err:
                    _msg += f" — last: {_last_err}"
                return {
                    "station_id": self.station_id,
                    "receiver_type": "G10",
                    "status": "failed",
                    "error_message": _msg,
                    "files_checked": len(files_dict),
                    "files_missing": len(missing_files_dict),
                    "files_downloaded": 0,
                    "downloaded_files": [],
                    "duration": duration,
                }

            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "status": "completed",
                "files_downloaded": len(final_files),
                "downloaded_files": final_files,
                "duration": duration,
                "start_time": start,
                "end_time": end,
                "session": session,
            }

        except Exception as e:
            duration = time.time() - start_time
            error_type = type(e).__name__
            error_msg = f"{error_type}: {e}"
            self.logger.error(f"❌ Download failed: {error_msg}")

            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "status": "failed",
                "error_message": error_msg,
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": error_msg,
                "duration": duration,
            }

    def _generate_file_list(
        self, start: datetime, end: datetime, session: str, **kwargs
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Generate file lists for Leica G10 downloads.

        Args:
            start: Start time
            end: End time
            session: Session type (only 15s_24hr supported)
            **kwargs: Additional parameters

        Returns:
            Tuple of (files_dict, archive_files_dict)
            files_dict maps filename -> remote_directory (not used for Leica)
            archive_files_dict maps filename -> archive_path
        """
        # Parse session parameters
        afrequency, ffrequency, gt_frequency = parse_session_parameters(session)

        # Get session mapping from configuration
        # Normalize session name to lowercase for config lookup (1Hz_1hr -> 1hz_1hr)
        session_key = session.lower()
        session_mapping = self.leica_config.get(f"session_map_{session_key}")
        if not session_mapping:
            raise ValueError(
                f"Unsupported session type for Leica G10: {session} (looked for session_map_{session_key})"
            )

        session_letter, remote_directory = session_mapping.split(",", 1)
        remote_directory = remote_directory.strip()
        session_letter = session_letter.strip()

        self.logger.info(
            f"Using session mapping: {session} -> letter={session_letter}, directory={remote_directory}"
        )

        # Generate datetime list using unified build_path method (same as other receivers)
        file_datetime_list = self.build_path(
            None, "#datelist", session, gt_frequency, start, end
        )

        # Create remote template using unified approach for Leica filename convention
        # Different templates based on session type:
        # - Daily files (15s_24hr): Use session letter (a)
        # - Hourly files (1Hz_1hr): Use #hourl for hour letters (a,b,c...x)
        file_extension = self.get_file_extension()  # .m00
        base_path = self.leica_config.get("base_path", "/SD Card/Data/")

        if session == "15s_24hr":
            # Daily files: directly under /SD Card/Data/15s_24hr/ (no year/month/day subdirectories)
            remote_template = f"{base_path}{remote_directory}/{self.station_id}%j{session_letter}{file_extension}.zip"
        else:
            # Hourly files: nested structure /SD Card/Data/1s_1hr/STATION/year/month/day/
            remote_template = f"{base_path}{remote_directory}/{self.station_id}/%Y/%m/%d/{self.station_id}%j#hourl{file_extension}.zip"

        # Generate remote full paths using unified method with gtimes frequency for #Rin2 expansion
        remote_full_paths = self.build_path(
            file_datetime_list, remote_template, session, gt_frequency
        )

        # Extract remote directories and filenames for files_dict
        files_dict = {}
        for full_path in remote_full_paths:
            remote_dir = os.path.dirname(full_path) + "/"  # Add trailing slash for FTP
            filename = os.path.basename(full_path)
            files_dict[filename] = remote_dir

        # Generate archive paths using unified approach
        archive_template = self.receivers_config.get_archive_template()

        # Leica files are archived as .m00.gz (compressed)
        raw_extension = self.get_file_extension()  # .m00
        archived_extension = raw_extension + ".gz"  # .m00.gz

        full_archive_template = archive_template.format(
            data_prepath=self.data_prepath,
            station="{station}",
            session="{session}",
            extension=archived_extension,
            session_letter="{session_letter}",
        )

        # Adjust timestamps to match Leica G10 file creation times (same pattern as NetR9/NetRS)
        # Daily files (15s_24hr): created at midnight (00:00)
        # Hourly files (1Hz_1hr): created at hour boundaries (01:00, 02:00, etc.)
        adjusted_datetime_list = []
        for dt in file_datetime_list:
            if ffrequency == "24hr":
                # Daily files always created at midnight
                adjusted_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                # Hourly files created at hour boundaries
                adjusted_dt = dt.replace(minute=0, second=0, microsecond=0)
            adjusted_datetime_list.append(adjusted_dt)

        archive_file_list = self.build_path(
            adjusted_datetime_list, full_archive_template, session, gt_frequency
        )

        # Map filenames to archive paths
        # For Leica, we need to map both the .zip filename and the final .m00 filename
        archive_files_dict = {}
        filenames = list(files_dict.keys())
        for i, filename in enumerate(filenames):
            if i < len(archive_file_list):
                # Map the zip filename: SKFC268b.m00.zip -> archive path
                archive_files_dict[filename] = archive_file_list[i]
                # Also map the unzipped filename: SKFC268b.m00 -> archive path
                unzipped_filename = filename.replace(".zip", "")
                archive_files_dict[unzipped_filename] = archive_file_list[i]

        return files_dict, archive_files_dict

    def _unzip_single_file(self, zip_file_path: str) -> Optional[str]:
        """Unzip a single .zip file and return the path to the unzipped file.

        Args:
            zip_file_path: Path to the .zip file

        Returns:
            Path to unzipped .m00 file, or None if unzipping failed
        """
        zip_path = Path(zip_file_path)
        if not zip_path.exists():
            self.logger.warning(f"File not found: {zip_file_path}")
            return None

        if not str(zip_path).endswith(".zip"):
            self.logger.warning(f"File is not a zip file: {zip_file_path}")
            return zip_file_path  # Return as-is

        # Unzip the file
        try:
            self.logger.info(f"📦 Unzipping: {zip_path.name}")

            # Run unzip command in the same directory
            result = subprocess.run(
                ["unzip", "-o", str(zip_path)],
                cwd=zip_path.parent,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                # Find the unzipped .m00 file
                base_name = zip_path.stem  # Remove .zip extension
                m00_file = zip_path.parent / base_name

                if m00_file.exists():
                    self.logger.info(f"✅ Unzipped: {base_name}")

                    # Remove the zip file to save space
                    zip_path.unlink()
                    self.logger.debug(f"🧹 Removed zip file: {zip_path.name}")

                    return str(m00_file)
                else:
                    self.logger.error(f"❌ Unzipped file not found: {base_name}")
                    return None
            else:
                self.logger.error(
                    f"❌ Unzip failed for {zip_path.name}: {result.stderr}"
                )
                return None

        except subprocess.TimeoutExpired:
            self.logger.error(f"❌ Unzip timeout for {zip_path.name}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Unzip error for {zip_path.name}: {e}")
            return None

    def get_file_extension(self) -> str:
        """Get file extension for Leica G10 files.

        Returns:
            File extension (.m00)
        """
        return self.leica_config.get("file_extension", ".m00")

    def get_session_letter(self, session: str) -> str:
        """Get session letter for Leica G10 session type.

        Args:
            session: Session type (e.g., '15s_24hr')

        Returns:
            Session letter (e.g., 'a' for 15s_24hr, 'b' for 1Hz_1hr)
        """
        # Normalize session name to lowercase for config lookup (1Hz_1hr -> 1hz_1hr)
        session_key = session.lower()
        session_map = self.leica_config.get(f"session_map_{session_key}", "a,15s_24hr")
        session_letter, _ = session_map.split(",", 1)
        return session_letter

    def _track_validated_files(
        self, files_dict: Dict, session: str, start: Any
    ) -> None:
        """Register already-archived files in file_tracking database."""
        import re
        from datetime import date, datetime, timedelta

        try:
            with DownloadTracker(self.station_id, session) as tracker:
                if tracker._connected:
                    tracked = 0
                    for filename in files_dict.keys():
                        # Try archive format: SKFC202601180000a.m00.gz
                        match = re.match(
                            rf"^{self.station_id}(\d{{4}})(\d{{2}})(\d{{2}})(\d{{4}})([a-x])",
                            filename,
                            re.IGNORECASE,
                        )
                        if match:
                            file_date = date(
                                int(match.group(1)),
                                int(match.group(2)),
                                int(match.group(3)),
                            )
                            hhmm = match.group(4)  # e.g., "2000" for hour 20
                            file_hour_from_name = int(hhmm[:2])
                            # Daily files have HHMM=0000; hourly files have actual hour
                            file_hour = (
                                None
                                if file_hour_from_name == 0
                                else file_hour_from_name
                            )
                        else:
                            # Try Leica format: SKFC018a.m00
                            match = re.match(
                                rf"^{self.station_id}(\d{{3}})([a-x])",
                                filename,
                                re.IGNORECASE,
                            )
                            if match:
                                day_of_year = int(match.group(1))
                                session_letter = match.group(2).lower()
                                file_year = (
                                    start.year
                                    if hasattr(start, "year")
                                    else datetime.now().year
                                )
                                file_date = date(file_year, 1, 1) + timedelta(
                                    days=day_of_year - 1
                                )
                                file_hour = (
                                    None
                                    if session_letter == "a"
                                    else ord(session_letter) - ord("a")
                                )
                            else:
                                continue
                        tracker.mark_archived(file_date, file_hour, filename)
                        tracked += 1
                    if tracked:
                        self.logger.debug(
                            f"Registered {tracked} archived files in file_tracking"
                        )
        except Exception as e:
            self.logger.debug(f"File tracking for validated files failed: {e}")

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information for this receiver.

        Returns:
            Station information dictionary
        """
        return self.station_info

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status from Leica G10 receiver.

        Tries HTTP extraction first (rich data from web interface AJAX endpoints),
        then falls back to FTP-based health inference.

        Returns:
            Dictionary with health status information following health-data-spec.md
        """
        # Get receiver host from station info
        host = self.station_info.get("ip", self.station_info.get("host"))
        if not host:
            host = self.station_info.get("router", {}).get("ip")

        http_port = int(self.station_info.get("receiver", {}).get("httpport", 8060))
        ftp_port = int(self.station_info.get("receiver", {}).get("ftpport", 2160))

        # Step 1: Check connection health (ping + port checks with fail_fast)
        connection_data = self.check_connection_health(
            http_port=http_port,
            protocol_type="ftp",
            protocol_port=ftp_port,
        )

        ping_ok = connection_data.get("router_ping", {}).get("accessible", False)

        if not ping_ok:
            self.logger.debug(f"Ping failed for {host} — skipping HTTP/FTP extraction")
            return self.build_health_status(connection_data=connection_data)

        # Step 2: Try HTTP extraction first (rich data from web interface)
        if host:
            try:
                from ..health import G10HTTPExtractor

                extractor = G10HTTPExtractor(
                    host=host,
                    station_id=self.station_id,
                    port=http_port,
                    timeout=10,
                    ftp_port=ftp_port,
                )
                health_data = extractor.extract_health_data()
                if health_data and health_data.get("metrics"):
                    # Merge router_ping from our ICMP check into the
                    # extractor's connection data so connectivity_writer
                    # can determine online status correctly.
                    health_data.setdefault("connection", {})["router_ping"] = (
                        connection_data.get("router_ping", {})
                    )
                    self.logger.info(
                        f"Extracted health via HTTP from {host}:{http_port}"
                    )
                    return health_data
            except Exception as e:
                self.logger.warning(f"HTTP extraction failed, falling back to FTP: {e}")

        # Step 3: Fall back to FTP-based health inference
        from ..health import G10FTPHealthInferrer

        data_quality = None
        receiver_specific = None

        try:
            if host:
                inferrer = G10FTPHealthInferrer(
                    host=host, station_id=self.station_id, timeout=10
                )

                username = self.station_info.get("ftp_user", "anonymous")
                password = self.station_info.get("ftp_pass", "")
                ftp_path = self.station_info.get("ftp_path", "/data")

                health_data = inferrer.infer_health_from_ftp(
                    ftp_path=ftp_path, username=username, password=password
                )

                data_quality = health_data.get("data_quality", {})
                receiver_specific = health_data.get("receiver_specific", {})

                self.logger.info(
                    f"Inferred health status from FTP file analysis on {host}"
                )
            else:
                self.logger.warning(
                    f"No host/IP configured for {self.station_id} - "
                    "connection health only"
                )

        except Exception as e:
            self.logger.error(f"Error inferring health data from FTP: {e}")

        return self.build_health_status(
            connection_data=connection_data,
            data_quality=data_quality,
            receiver_specific=receiver_specific,
        )

    def close(self):
        """Close connections to receiver."""
        if hasattr(self, "ftp_downloader"):
            self.ftp_downloader.close()

    def __del__(self):
        """Clean up resources."""
        self.close()
