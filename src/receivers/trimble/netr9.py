"""Trimble NetR9 receiver implementation.

Modern implementation of Trimble NetR9 receiver support, ported from the legacy
system with modern BaseReceiver interface, HTTP client, and FTP download capabilities.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple, Union

import gtimes.timefunc as gt

from ..base.exceptions import ConfigurationError, ConnectionError
from ..base.receiver import BaseReceiver

# Phase 1 utilities (feature-flagged)
from ..utils.archive_validator import ArchiveValidator
from ..utils.download_tracker import DownloadTracker
from ..utils.file_archiver import ArchiveMode, FileArchiver
from ..utils.performance_recorder import (
    create_performance_metrics,
    record_performance_metrics,
)
from ..utils.session_parser import parse_session_parameters
from ..utils.time_processor import TimeParameterProcessor
from .health_parser import TrimbleHealthParser
from .http_client import TrimbleHTTPClient
from .http_download_client import NetR9HTTPDownloader


class NetR9(BaseReceiver):
    """Trimble NetR9 receiver implementation.

    Provides HTTP-based health monitoring and FTP-based data download
    for Trimble NetR9 GNSS receivers.
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize NetR9 receiver.

        Args:
            station_id: Station identifier
            station_info: Station configuration dictionary
        """
        super().__init__(station_id, station_info)

        # Set up logging
        self.logger = self._get_logger()

        # Validate required configuration
        self._validate_config()

        # Get NetR9-specific configuration
        self.netr9_config = self.receivers_config.get_receiver_config("netr9")

        # Initialize HTTP client for health monitoring
        self.http_client = TrimbleHTTPClient(station_id, station_info)

        # Initialize HTTP downloader for data downloads
        self.http_downloader = NetR9HTTPDownloader(station_id, station_info)

        # Initialize health parser
        self.health_parser = TrimbleHealthParser(station_id, "NetR9")

        # data_prepath is now handled by BaseReceiver via ConfigManager
        self.tmp_dir = self.receivers_config.get_tmp_dir()

        # Phase 1 utilities (always enabled - Phase 3B)
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)
        # FileArchiver will be created per-download with appropriate mode

        # NetR9 HTTP API endpoints (from old system)
        self.endpoints = {
            "voltage": "/prog/show?Voltages",
            "temperature": "/prog/show?Temperature",
            "sessions": "/prog/show?sessions",
            "position": "/prog/show?position",
            "tracking": "/prog/show?trackingstatus",
            "firmware": "/prog/show?firmwareversion",
            "directory": "/prog/show?directory&path=/{path}",
        }

        self.logger.info(f"Initialized NetR9 receiver for {self.station_id}")

    def _get_logger(self, level: int = logging.INFO) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        # Use parent logger's configuration for consistent formatting
        return logger

    def _validate_config(self):
        """Validate required configuration parameters."""
        try:
            router_config = self.station_info["router"]
            receiver_config = self.station_info["receiver"]

            if not router_config.get("ip"):
                raise ConfigurationError(
                    f"Missing router IP for station {self.station_id}"
                )

            # Check for HTTP port (can be httpport or receiver_httpport depending on config format)
            http_port = receiver_config.get("httpport") or receiver_config.get(
                "receiver_httpport"
            )
            if not http_port:
                self.logger.warning(
                    f"Missing HTTP port for {self.station_id}, using default 8060"
                )
                receiver_config["httpport"] = 8060
            else:
                receiver_config["httpport"] = int(http_port)

            # FTP port is optional for Trimble - downloads use HTTP
            # If specified, it can be used for connection health checks

        except KeyError as e:
            raise ConfigurationError(
                f"Invalid station configuration for {self.station_id}: {e}"
            )

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to NetR9 receiver.

        Returns:
            Dictionary with connection status information
        """
        try:
            self.logger.debug(f"Testing connection to {self.station_id}")

            # Test HTTP connection
            http_test = self.http_client.test_connection()

            # Test HTTP download connection
            download_test = self.http_downloader.test_connection()

            # Update internal connection status
            self.connection_status = {
                "router": http_test["success"],
                "receiver": http_test["success"],
            }

            return {
                "station_id": self.station_id,
                "ip": self.station_info["router"]["ip"],
                "port": self.station_info["receiver"]["httpport"],
                "router": http_test["success"],
                "receiver": http_test["success"],
                "http_test": http_test,
                "download_test": download_test,
                "error": http_test.get("error"),
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

    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status from NetR9 receiver.

        Uses standardized health checking from BaseReceiver with NetR9-specific
        health data extraction via HTTP API (/prog/show? endpoints).

        Returns:
            Dictionary with health status information following health-data-spec.md
        """
        from ..health import TrimbleHTTPExtractor

        # Get configuration from station_info
        host = self.station_info.get("router", {}).get("ip")
        receiver_config = self.station_info.get("receiver", {})
        http_port = int(receiver_config.get("httpport", 8060))
        ftp_port_raw = receiver_config.get("ftpport")
        ftp_port = int(ftp_port_raw) if ftp_port_raw is not None else None
        http_user = receiver_config.get("user")
        http_pass = receiver_config.get("pwd")

        # Step 1: Check connection health at all levels
        connection_data = self.check_connection_health(
            http_port=http_port,
            protocol_type="http",
            protocol_port=http_port,
            host=host,
        )

        # Step 2: Extract instrument-specific health data via HTTP API
        metrics = None
        data_quality = None
        network = None
        receiver_identity = None

        # Check if ping succeeded before attempting HTTP extraction
        ping_ok = connection_data.get("router_ping", {}).get("accessible", False)

        if not ping_ok:
            self.logger.debug(f"Ping failed for {host} — skipping HTTP extraction")
        elif not host:
            self.logger.warning(
                f"No router IP configured for {self.station_id} - "
                "connection health only"
            )
        else:
            try:
                # Extract health data using TrimbleHTTPExtractor
                extractor = TrimbleHTTPExtractor(
                    host=host,
                    station_id=self.station_id,
                    port=http_port,
                    receiver_type=self.get_receiver_type(),
                    username=http_user,
                    password=http_pass,
                    ftp_port=ftp_port,
                )
                health_data = extractor.extract_health_data()

                # Map extracted data to standardized sections
                metrics = health_data.get("metrics", {})
                data_quality = health_data.get("data_quality", {})
                network = health_data.get("network", {})

                self.logger.info(
                    f"Extracted health data from {host}:{http_port} via HTTP API"
                )

                # Capture receiver identity if available
                receiver_identity = health_data.get("receiver_identity")

            except Exception as e:
                self.logger.error(f"Error extracting health data via HTTP: {e}")

        # Step 3: Build standardized health status structure
        result = self.build_health_status(
            connection_data=connection_data,
            metrics=metrics,
            data_quality=data_quality,
            network=network,
        )
        if receiver_identity:
            result["receiver_identity"] = receiver_identity
        return result

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
        """Download data from NetR9 receiver for specified time period.

        Args:
            start: Start time for data download
            end: End time for data download
            session: Data session type (e.g., '15s_24hr', '1Hz_1hr')
            sync: Whether to sync missing files
            clean_tmp: Whether to clean temporary download directory
            archive: Whether to archive downloaded files
            **kwargs: Additional receiver-specific parameters

        Returns:
            Dictionary with download results and file information
        """
        # Extract legacy parameters from kwargs for backward compatibility
        loglevel = kwargs.get(
            "loglevel", logging.INFO
        )  # Default to INFO for detailed logging
        # Per-file download retry budget (propagated from CLI --max-retries)
        max_retries = kwargs.get("max_retries", 3)

        # Set logger level
        self.logger.setLevel(loglevel)

        start_time = time.time()

        # Quick reachability check to skip offline stations fast.
        # Some routers block ICMP — fall back to TCP port check before giving up.
        http_port = self.station_info["receiver"]["httpport"]
        if not self._quick_ping() and not self._quick_tcp_check(http_port):
            self.logger.warning(
                f"Station {self.station_id} is unreachable (ping failed), skipping download"
            )
            return {
                "station_id": self.station_id,
                "receiver_type": "NetR9",
                "status": "unreachable",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": "Station unreachable (ping failed)",
                "duration": time.time() - start_time,
            }
        if not self._quick_tcp_check(http_port):
            self.logger.warning(
                f"Station {self.station_id} HTTP port {http_port} not responding, skipping download"
            )
            return {
                "station_id": self.station_id,
                "receiver_type": "NetR9",
                "status": "unreachable",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": f"HTTP port {http_port} not responding",
                "duration": time.time() - start_time,
            }

        try:
            self.logger.info(f"Starting download for NetR9 {self.station_id}")

            # Process time parameters
            start, end = self._process_time_parameters(start, end, session)

            # Log session info (matching PolaRX5 pattern)
            self.logger.info(f"Checking {session} sessions from {start} to {end}")

            # Set up directories
            # Include session in tmp path to prevent filename collisions between sessions
            tmp_dir_path = Path(self.tmp_dir) / self.station_id / session
            tmp_dir_path.mkdir(parents=True, exist_ok=True)

            # Generate file list based on session type and time range
            files_dict, archive_files_dict = self._generate_file_list(
                start, end, session, **kwargs
            )

            # Log file generation info (matching PolaRX5 pattern)
            self.logger.info(f"Generated {len(files_dict)} timestamps")

            if not files_dict:
                self.logger.warning(f"No files to download for {self.station_id}")
                return {
                    "station_id": self.station_id,
                    "receiver_type": "NetR9",
                    "status": "no_files",
                    "files_downloaded": 0,
                    "downloaded_files": [],
                    "duration": time.time() - start_time,
                }

            # Use Phase 1 batch validation - checks archive AND tmp (Fix #1)
            # See netrs.py for the rationale: pass None for tmp_dir so a
            # partial .T02 doesn't get auto-flushed as complete. The Trimble
            # binary format has no embedded length we can cheaply verify in
            # _validate_tmp_file_integrity, so any partial with a valid
            # header would slip through.
            (
                missing_files_dict,
                files_found_in_archive,
                validated_files,
                files_in_tmp_dict,
            ) = self.archive_validator.batch_validate_archives(
                files_dict, archive_files_dict, None
            )

            # Archive files from tmp if found and archive flag is set (Fix #1)
            files_archived_from_tmp = 0
            if files_in_tmp_dict and archive:
                self.logger.info(
                    f"Archiving {len(files_in_tmp_dict)} files from tmp directory..."
                )

                with FileArchiver(
                    mode=ArchiveMode.BULK, logger=self.logger
                ) as archiver:
                    for filename, tmp_path in files_in_tmp_dict.items():
                        archive_dest = archive_files_dict.get(filename)
                        if archive_dest:
                            archiver.archive_file(
                                tmp_path,
                                Path(archive_dest),
                                compress=False,  # Files already compressed
                                remove_tmp=True,
                            )

                stats = archiver.get_statistics()
                files_archived_from_tmp = stats["successful"]
                self.logger.info(
                    f"Archived {stats['successful']}/{len(files_in_tmp_dict)} files from tmp to archive"
                )

            # Log validation results (matching PolaRX5 pattern)
            self.logger.info(f"Validated {validated_files} existing files")
            if files_archived_from_tmp > 0:
                self.logger.info(
                    f"Archived {files_archived_from_tmp} files from tmp directory"
                )

            archive_dir = (
                Path(next(iter(archive_files_dict.values()))).parent
                if archive_files_dict
                else None
            )
            if not missing_files_dict:
                self.logger.info(f"Archive is up to date ({archive_dir})")
                # Track validated files as downloaded so dashboard reflects current state
                self._track_validated_files(files_dict, session)
                return {
                    "station_id": self.station_id,
                    "receiver_type": "NetR9",
                    "status": "up_to_date",
                    "files_checked": len(files_dict),
                    "files_missing": 0,
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                }

            self.logger.info(f"Missing files: {len(missing_files_dict)}")

            # Filter out known missing files using download tracker
            # Skip this filter when retry_missing=True (scheduler always retries)
            import re

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
                                # Parse date from NetR9 filename: MANA202601170000A.T02
                                match = re.match(
                                    rf"^{re.escape(self.station_id)}_*(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})",
                                    filename,
                                    re.IGNORECASE,
                                )
                                if match:
                                    from datetime import date

                                    file_date = date(
                                        int(match.group(1)),
                                        int(match.group(2)),
                                        int(match.group(3)),
                                    )
                                    file_hour = int(match.group(4))
                                    # Hourly session has file_hour, daily has None
                                    track_hour = (
                                        file_hour if "1hr" in session.lower() else None
                                    )
                                    if tracker.is_file_missing(file_date, track_hour):
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

            # Log connection info and remote paths (matching PolaRX5 pattern)
            if missing_files_dict:
                # Log station connection details
                router_ip = self.station_info["router"]["ip"]
                http_port = self.station_info["receiver"]["httpport"]
                self.logger.info(f"Station connection: {router_ip}:{http_port}")

                # Log remote paths (unique paths only)
                logged_paths = set()
                for filename, remote_dir in sorted(
                    missing_files_dict.items(), reverse=True
                ):
                    if remote_dir not in logged_paths:
                        self.logger.info(f"Remote path: {remote_dir}")
                        logged_paths.add(remote_dir)

            # Download files if sync is enabled
            downloaded_files = []
            if sync:
                if missing_files_dict:
                    # Pass archive info for immediate archiving after each download
                    downloaded_files = self.http_downloader.download_files(
                        missing_files_dict,
                        tmp_dir_path,
                        clean_tmp,
                        archive_files_dict=archive_files_dict if archive else None,
                        use_phase1_utilities=archive,  # Always use Phase 1 when archiving
                        session_type=session,
                        max_retries=max_retries,
                    )
                else:
                    self.logger.info(
                        f"Archive is up to date ({archive_dir}) - no files to download"
                    )
            else:
                self.logger.info("Sync disabled - skipping actual download")

            # Files are archived inline by download client when archive=True
            # downloaded_files contains archive paths when archiving is enabled

            # Record performance metrics using abstracted utility
            duration = time.time() - start_time
            performance_metrics = create_performance_metrics(
                success=len(downloaded_files) > 0 if sync else True,
                duration=duration,
                bytes_downloaded=sum(
                    Path(f).stat().st_size for f in downloaded_files if Path(f).exists()
                ),
                connection_time=getattr(
                    self.http_downloader, "_last_connection_time", 0.0
                ),
            )
            record_performance_metrics(
                self.station_id, performance_metrics, self.logger
            )

            # Track downloaded files in database (only when sync is enabled)
            if sync:
                try:
                    # Collect remote file sizes from HTTP downloader (if available)
                    http_remote_sizes = getattr(
                        self.http_downloader, "remote_sizes", {}
                    )

                    with DownloadTracker(self.station_id, session) as tracker:
                        if tracker._connected:
                            from datetime import date

                            # Track successful downloads and collect downloaded dates
                            downloaded_dates = set()  # Set of (date, hour) tuples
                            for file_path in downloaded_files:
                                filename = Path(file_path).name
                                # Look up remote size by filename (original name without .gz)
                                remote_size = http_remote_sizes.get(filename)
                                if not remote_size:
                                    base_name = filename.removesuffix(".gz")
                                    remote_size = http_remote_sizes.get(base_name)
                                # Parse date from NetR9 filename: MANA202601170000A.T02 or .T02.gz
                                match = re.match(
                                    rf"^{re.escape(self.station_id)}_*(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})",
                                    filename,
                                    re.IGNORECASE,
                                )
                                if match:
                                    file_date = date(
                                        int(match.group(1)),
                                        int(match.group(2)),
                                        int(match.group(3)),
                                    )
                                    file_hour = int(match.group(4))
                                    track_hour = (
                                        file_hour if "1hr" in session.lower() else None
                                    )
                                    file_size = (
                                        Path(file_path).stat().st_size
                                        if Path(file_path).exists()
                                        else None
                                    )
                                    tracker.mark_downloaded(
                                        file_date,
                                        track_hour,
                                        filename,
                                        file_size,
                                        remote_file_size=remote_size,
                                    )
                                    downloaded_dates.add((file_date, track_hour))

                            # Track missing files: only mark file_tracking
                            # 'missing' when the receiver explicitly said the
                            # file isn't on disk (HTTP 404). Connection /
                            # transport errors leave the row absent so the
                            # next morning_recovery / scheduled run can retry
                            # without being blocked by a known_missing skip.
                            file_outcomes = getattr(
                                self.http_downloader, "file_outcomes", {}
                            )
                            for req_filename in missing_files_dict.keys():
                                if file_outcomes.get(req_filename) != "not_found":
                                    continue
                                match = re.match(
                                    rf"^{re.escape(self.station_id)}_*(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})",
                                    req_filename,
                                    re.IGNORECASE,
                                )
                                if match:
                                    file_date = date(
                                        int(match.group(1)),
                                        int(match.group(2)),
                                        int(match.group(3)),
                                    )
                                    file_hour = int(match.group(4))
                                    track_hour = (
                                        file_hour if "1hr" in session.lower() else None
                                    )
                                    # Only mark as missing if this date/hour wasn't downloaded
                                    if (file_date, track_hour) not in downloaded_dates:
                                        tracker.mark_missing(
                                            file_date, track_hour, req_filename
                                        )
                except Exception as e:
                    self.logger.debug(f"File tracking failed: {e}")

            # All files failed — don't report "completed" with 0 downloads.
            # Surface dominant per-file error + connection target so the DB log
            # is self-explanatory.
            if sync and missing_files_dict and not downloaded_files:
                _last_err = getattr(self.http_downloader, "last_file_error", None)
                _ip = self.station_info["router"]["ip"]
                _port = self.station_info["receiver"]["httpport"]
                _msg = f"All file downloads failed (0 of {len(missing_files_dict)}) @ {_ip}:{_port}"
                if _last_err:
                    _msg += f" — last: {_last_err}"
                return {
                    "station_id": self.station_id,
                    "receiver_type": "NetR9",
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
                "receiver_type": "NetR9",
                "status": "completed",
                "files_downloaded": len(downloaded_files),
                "downloaded_files": downloaded_files,
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
                "receiver_type": "NetR9",
                "status": "failed",
                "error_message": error_msg,
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": error_msg,
                "duration": duration,
            }

    def _track_validated_files(self, files_dict: Dict, session: str) -> None:
        """Register already-archived files in file_tracking database.

        This ensures the dashboard reflects files that were validated in the
        archive even if they were downloaded before the tracker was active.
        """
        import re

        try:
            with DownloadTracker(self.station_id, session) as tracker:
                if tracker._connected:
                    from datetime import date

                    tracked = 0
                    for filename in files_dict.keys():
                        match = re.match(
                            rf"^{re.escape(self.station_id)}_*(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})",
                            filename,
                            re.IGNORECASE,
                        )
                        if match:
                            file_date = date(
                                int(match.group(1)),
                                int(match.group(2)),
                                int(match.group(3)),
                            )
                            file_hour = int(match.group(4))
                            track_hour = file_hour if "1hr" in session.lower() else None
                            tracker.mark_archived(file_date, track_hour, filename)
                            tracked += 1
                    if tracked:
                        self.logger.debug(
                            f"Registered {tracked} archived files in file_tracking"
                        )
        except Exception as e:
            self.logger.debug(f"File tracking for validated files failed: {e}")

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station information
        """
        return {
            "station_id": self.station_id,
            "receiver_type": "NetR9",
            "router_ip": self.station_info["router"]["ip"],
            "http_port": self.station_info["receiver"]["httpport"],
            "ftp_port": self.station_info["receiver"].get("ftpport", 21),
            "connection_type": self.station_info["station"].get(
                "connection_type", "unknown"
            ),
            "timeout_category": self.station_info["receiver"].get(
                "timeout_category", "mobile"
            ),
            "configuration": self.station_info,
        }

    def get_firmware_version(self) -> Dict[str, Any]:
        """Get firmware version from NetR9 receiver.

        Returns:
            Dictionary with firmware information
        """
        try:
            success, response, error = self.http_client.get_url(
                self.endpoints["firmware"]
            )
            if success and response:
                return {
                    "success": True,
                    "firmware_version": response.strip(),
                    "raw_response": response,
                }
            else:
                return {"success": False, "error": error or "No response"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_position(self) -> Dict[str, Any]:
        """Get current position from NetR9 receiver.

        Returns:
            Dictionary with position information
        """
        try:
            success, response, error = self.http_client.get_url(
                self.endpoints["position"]
            )
            if success and response:
                return {
                    "success": True,
                    "raw_response": response,
                    "note": "Position parsing not yet implemented",
                }
            else:
                return {"success": False, "error": error or "No response"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _process_time_parameters(
        self, start: Union[datetime, str], end: Union[datetime, str], session: str
    ) -> Tuple[datetime, datetime]:
        """Process and validate time parameters.

        Args:
            start: Start time
            end: End time
            session: Session type

        Returns:
            Tuple of processed start and end datetime objects
        """
        self.logger.debug("Using Phase 1 TimeParameterProcessor")
        return self.time_processor.process_time_parameters(start, end, session)

    def _resolve_base_path(self) -> str:
        """Storage root for remote paths (the ``/Internal/`` segment).

        A per-station ``receiver_base_path`` in stations.cfg (mapped to
        ``station_info['receiver']['base_path']``) takes precedence over the
        global ``[netr9] base_path`` default — e.g. a station logging to
        ``/External/`` instead of ``/Internal/``. This is the storage root, NOT
        the NetR5 CACHEDIR download prefix (that is ``cachedir_prefix``, resolved
        independently in the HTTP client), so the two URL pieces never collide.
        """
        return self.station_info.get("receiver", {}).get(
            "base_path"
        ) or self.netr9_config.get("base_path", "/Internal/")

    def _generate_file_list(
        self, start: datetime, end: datetime, session: str, **kwargs
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Generate list of files to download based on time range and session.

        Args:
            start: Start time
            end: End time
            session: Session type
            **kwargs: Additional parameters

        Returns:
            Tuple of (files_dict, archive_files_dict)
            files_dict maps filename -> remote_directory
            archive_files_dict maps filename -> archive_path
        """
        # Parse session parameters using abstracted utility
        afrequency, ffrequency, gt_frequency = parse_session_parameters(session)

        # Generate datetime list using unified build_path approach
        file_datetime_list = self.build_path(
            None, "#datelist", session, gt_frequency, start, end
        )

        # Adjust timestamps to match NetR9 file creation times
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

        # Generate remote file paths (NetR9 specific format)
        files_dict = {}
        archive_files_dict = {}

        # Storage root: a per-station receiver_base_path overrides the global
        # netr9 default (see _resolve_base_path). Same precedence pattern as
        # session_map / remote_date_format below.
        base_path = self._resolve_base_path()

        for file_dt in adjusted_datetime_list:
            # Get session mapping — per-station override in stations.cfg takes
            # precedence over the global netr9 config (same pattern as remote_date_format)
            session_key = session.lower()
            session_map_key = f"session_map_{session_key}"
            session_mapping = self.station_info.get("receiver", {}).get(
                session_map_key
            ) or self.netr9_config.get(session_map_key, "a,unknown")
            letter_code, remote_subdir = session_mapping.split(",")

            # NetR9 filename format: STATIONYYYYMMDDHHMM{session_letter}.T02
            # Timestamps are already adjusted to match file creation times
            filename_format = self.netr9_config.get(
                "remote_filename_format", "{station}%Y%m%d%H%M{session_letter}.T02"
            )

            # Handle firmware bug: some NetR5 receivers pad station ID with underscores
            # Example: ISAF (4 chars) becomes ISAF______ (10 chars total)
            station_id_for_filename = self.station_id
            if self.station_info.get("receiver", {}).get("firmware_underscore_pad"):
                # Pad to 10 characters with underscores
                station_id_for_filename = self.station_id.ljust(10, "_")
                self.logger.debug(
                    f"Applying underscore padding: {self.station_id} -> {station_id_for_filename}"
                )

            filename = file_dt.strftime(filename_format).format(
                station=station_id_for_filename, session_letter=letter_code
            )

            # Remote directory format: /Internal/YYYYMM/session_directory/
            # Per-station override (e.g., VARG uses %Y%m/%d since Jan 2026)
            date_format = self.station_info.get("receiver", {}).get(
                "remote_date_format"
            ) or self.netr9_config.get("remote_date_format", "%Y%m")
            remote_dir = f"{base_path.rstrip('/')}/{file_dt.strftime(date_format)}/{remote_subdir}"

            files_dict[filename] = remote_dir

        # Generate archive paths using unified approach
        archive_template = self.receivers_config.get_archive_template()
        # NetR9 files are archived as compressed .T02.gz
        raw_extension = self.get_file_extension()  # .T02
        archived_extension = raw_extension + ".gz"  # .T02.gz
        full_archive_template = archive_template.format(
            data_prepath=self.data_prepath,
            station="{station}",
            session="{session}",
            extension=archived_extension,
            session_letter="{session_letter}",
        )

        # Use adjusted datetime list to ensure archive timestamps match filename timestamps
        archive_file_list = self.build_path(
            adjusted_datetime_list, full_archive_template, session, gt_frequency
        )

        # Map filenames to archive paths
        for i, filename in enumerate(files_dict.keys()):
            if i < len(archive_file_list):
                archive_files_dict[filename] = archive_file_list[i]

        return files_dict, archive_files_dict

    def get_file_extension(self) -> str:
        """Get file extension for NetR9 files.

        Returns:
            File extension for NetR9 receiver files
        """
        return self.netr9_config.get("file_extension", ".T02")

    def get_session_letter(self, session: str) -> str:
        """Get session letter for NetR9 receiver type and session.

        Args:
            session: Session type (e.g., '15s_24hr', '1Hz_1hr')

        Returns:
            Session letter code for NetR9
        """
        session_key = session.lower()
        session_map_key = f"session_map_{session_key}"
        session_mapping = self.station_info.get("receiver", {}).get(
            session_map_key
        ) or self.netr9_config.get(session_map_key, "a,unknown")
        return session_mapping.split(",")[0]

    def __del__(self):
        """Clean up resources."""
        if hasattr(self, "http_client"):
            self.http_client.close()
        if hasattr(self, "http_downloader"):
            self.http_downloader.close()
