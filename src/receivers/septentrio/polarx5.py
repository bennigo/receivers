"""Septentrio PolaRX5 receiver implementation."""

import binascii
import logging
import os
import time
from datetime import UTC, datetime, timedelta, timezone
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, Optional, Union

from gtimes.timefunc import currDatetime

try:
    from tqdm import tqdm

    progressbar_available = True
except ImportError:
    progressbar_available = False
    tqdm = None

from ..base.exceptions import (
    ConfigurationError,
    ConnectionError,
)
from ..base.receiver import BaseReceiver

# Phase 1 utilities (feature-flagged)
from ..utils.archive_validator import ArchiveValidator
from ..utils.compression_detector import needs_compression
from ..utils.file_archiver import ArchiveMode, FileArchiver
from ..utils.file_validator import FileValidator
from ..utils.performance_recorder import (
    create_performance_metrics,
    record_performance_metrics,
)
from ..utils.session_parser import parse_session_parameters
from ..utils.time_processor import TimeParameterProcessor


def _safe_resume_offset(local_file, remote_file_size, logger):
    """Return a REST offset that is safe to use against the current remote file.

    PolaRX5 FTP supports resume via REST + RETR, but rejects REST > current
    file size with `554 Restart offset … is too large for file size …`.
    Once that error fires, retries with the same partial keep failing — a
    permanent deadlock until something deletes the local file.

    This helper enforces the invariant: partial_size <= remote_file_size.
    If the local file is larger than what the server now serves (e.g. the
    file rotated to a smaller version, or the partial accumulated extra
    bytes from an earlier code revision / multi-mode-switch run), we
    delete it and return 0 so the caller restarts from byte 0.

    Args:
        local_file: Path to the partial file on disk (may not exist).
        remote_file_size: Current size of the file on the server (bytes).
        logger: Logger to record the oversize-detect/delete decision.

    Returns:
        Resume offset in bytes (0 if no partial or partial was oversized).
    """
    if not os.path.isfile(local_file):
        return 0
    partial_size = os.path.getsize(local_file)
    if partial_size == 0:
        return 0
    if partial_size > remote_file_size:
        logger.warning(
            f"⚠️  Oversized partial: local {partial_size} bytes > "
            f"remote {remote_file_size} bytes for {Path(local_file).name}. "
            f"Deleting partial and restarting from 0 (server would 554)."
        )
        try:
            os.unlink(local_file)
        except OSError as exc:
            logger.warning(f"Could not delete oversized partial: {exc}")
        return 0
    return partial_size


class PolaRX5(BaseReceiver):
    """Septentrio PolaRX5 receiver implementation.

    This class handles data download and health monitoring for Septentrio
    PolaRX5 GNSS receivers used in the Icelandic Met Office GPS network.
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize PolaRX5 receiver.

        Args:
            station_id: Station identifier (e.g., 'REYK', 'HOFN')
            station_info: Station configuration dictionary with router/receiver info
        """
        super().__init__(station_id, station_info)

        # Set up logging
        self.logger = self._get_logger()

        # Extract connection info from station_info
        self._setup_connection_info()

        # Configuration from shared ConfigManager (BaseReceiver provides self.config_manager)
        self.session_map = self.config_manager.get_session_map("polarx5")
        # Configuration available via BaseReceiver initialization
        # Get Septentrio-specific configuration
        self.septentrio_config = self.receivers_config.get_receiver_config("polarx5")

        # System paths from ConfigManager
        self.base_path = self.config_manager.get_system_path("receiver_base_path")
        self.sbf2rin_path = self.config_manager.get_system_path("sbf2rin_path")
        self.teqc_path = self.config_manager.get_system_path("teqc_path")

        # Initialize file validator for download integrity checking
        self.file_validator = FileValidator(self.logger)

        # Phase 1 utilities (always enabled - Phase 3B)
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)
        # FileArchiver will be created per-download with appropriate mode

        # Timeout configuration based on station network type
        self._setup_timeouts()

    def _get_logger(self, level: int = logging.WARNING) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        # Use parent logger's configuration for consistent formatting
        return logger

    def _setup_timeouts(self):
        """Setup timeout configuration using gps_parser centralized configuration."""
        try:
            # Try to load timeout configuration from gps_parser
            import sys

            sys.path.append("../gps_parser/src")
            import gps_parser

            parser = gps_parser.ConfigParser()
            timeout_config = parser.getStationTimeout(self.station_id)

            self.connection_timeout = timeout_config["connection_timeout"]
            self.inactivity_timeout = timeout_config["inactivity_timeout"]
            self.progress_timeout = timeout_config["progress_timeout"]
            self.min_speed_threshold = timeout_config["min_speed_threshold"]
            # Data transfer timeout - how long to wait for initial data before failing
            # 10s default handles FTP PASV setup under concurrent load
            # Can be per-station in future via gps_parser config
            self.data_transfer_timeout = timeout_config.get("data_transfer_timeout", 10)

            self.logger.info(
                f"Timeout config from gps_parser - Connection: {self.connection_timeout}s, "
                f"Inactivity: {self.inactivity_timeout}s, Progress: {self.progress_timeout}s, "
                f"Data transfer: {self.data_transfer_timeout}s, Min speed: {self.min_speed_threshold} B/s"
            )

        except Exception as e:
            # Fallback to default timeout configuration if gps_parser fails
            log = (
                self.logger.debug
                if self.station_info.get("_adhoc")
                else self.logger.warning
            )
            log(f"Could not load timeout config from gps_parser: {e}")
            self.logger.info("Using fallback timeout configuration")

            # Default fallback timeouts (mobile category as reasonable default)
            self.connection_timeout = 20
            self.inactivity_timeout = 60
            self.progress_timeout = 300
            self.min_speed_threshold = 2048
            self.data_transfer_timeout = 10  # Allow time for FTP PASV setup under load

            self.logger.info(
                f"Fallback timeout config - Connection: {self.connection_timeout}s, "
                f"Inactivity: {self.inactivity_timeout}s, Progress: {self.progress_timeout}s, "
                f"Data transfer: {self.data_transfer_timeout}s"
            )

        # Per-station stall timeout override (DB > receivers.cfg > gps_parser default)
        # For PolaRX5: overrides progress_timeout (hard ceiling per attempt).
        # PolaRX5 supports FTP resume so killing a slow download is cheap.
        # inactivity_timeout stays at gps_parser default (typically 60s).
        from ..utils.stall_timeout import get_stall_timeout

        self.progress_timeout = get_stall_timeout(
            self.station_id, "polarx5", default=self.progress_timeout
        )

    def _get_effective_timeout(
        self,
        session_type: Optional[str] = None,
        expected_file_size: Optional[int] = None,
    ) -> int:
        """Get progress timeout, adapted for this station's speed if data available.

        Uses the full priority chain: DB override > adaptive > receivers.cfg > default.
        When session_type is provided, the adaptive tier queries download_log for
        historical speed and computes a data-driven timeout.

        Args:
            session_type: Session type for adaptive lookup (e.g. '15s_24hr').
            expected_file_size: Remote file size for adaptive calculation.

        Returns:
            Timeout in seconds.
        """
        from ..utils.stall_timeout import get_stall_timeout

        return get_stall_timeout(
            self.station_id,
            "polarx5",
            default=self.progress_timeout,
            session_type=session_type,
            expected_file_size=expected_file_size,
        )

    def _setup_connection_info(self):
        """Extract and validate connection information from station_info."""
        try:
            self.ip_number = self.station_info["router"]["ip"]
            # Default to 2160 for PolaRX5 if not specified (standard Septentrio FTP port)
            self.ip_port = int(self.station_info["receiver"].get("ftpport") or 2160)

            # Use gps_parser for FTP mode determination.
            # Read from station_info["router"]["ftp_mode"] — that's where
            # config_utils.get_station_config() writes the value (cfg_utils:159),
            # which already includes the cfg_discrepancy override applied at
            # cfg_utils:129-131. Reading from "receiver" instead (the previous
            # behaviour) silently ignored the learned override on every run —
            # the recording side wrote to cfg_discrepancy but the application
            # side never saw it, so the passive→active mode-flip dance repeated
            # on every download even after the working mode was identified.
            ftp_mode = self.station_info.get("router", {}).get("ftp_mode", "auto")
            if ftp_mode == "active":
                self.pasv = False
            elif ftp_mode == "passive":
                self.pasv = True
            else:
                # Default to passive — almost all stations are behind NAT
                # routers where active mode PORT commands fail (IP mismatch).
                self.pasv = True

            # FTP credentials — firmware version is the primary auth gate.
            # Priority:
            #   1. Per-station ftp_username in stations.cfg (override; only
            #      needed when this station differs from receivers.cfg defaults)
            #   2. fw >= 5.7.0  → tcp_username / tcp_password from receivers.cfg
            #   3. fw  < 5.7.0  → anonymous
            #   4. ftp_anonymous_login = "force" in receivers.cfg overrides #2
            #      to anonymous (rare; for testing or open receivers)
            self.ftp_anonymous = True
            self.ftp_username: Optional[str] = None
            self.ftp_password: Optional[str] = None

            # Per-station override (ftp_username in stations.cfg)
            _per_station_user = self.station_info.get("ftp_username", "")
            if _per_station_user:
                self.ftp_anonymous = False
                self.ftp_username = _per_station_user
                self.ftp_password = self.station_info.get("ftp_password", "") or ""
            else:
                try:
                    from ..config.receivers_config import get_receivers_config
                    from ..health.polarx5_tcp_extractor import _firmware_requires_auth

                    rec_cfg = get_receivers_config().get_receiver_config("polarx5")

                    # Resolve firmware from stations.cfg (raw, since gps_parser
                    # processes the value and we need the literal string).
                    fw_ver: Optional[str] = None
                    try:
                        import gps_parser as _gps

                        raw = _gps.ConfigParser().getStationInfo(self.station_id)
                        station_raw = (
                            raw.get("station", {}) if isinstance(raw, dict) else {}
                        )
                        fw_ver = station_raw.get("receiver_firmware_version") or None
                    except Exception:
                        pass

                    fw_needs_auth = fw_ver is not None and _firmware_requires_auth(
                        fw_ver
                    )

                    # Allow operators to force anonymous on a 5.7.0+ receiver
                    # via `ftp_anonymous_login = force` (rare).
                    anon_force = str(
                        rec_cfg.get("ftp_anonymous_login", "")
                    ).lower() in ("force", "always")

                    if fw_needs_auth and not anon_force:
                        self.ftp_anonymous = False
                        self.ftp_username = rec_cfg.get("tcp_username") or None
                        self.ftp_password = rec_cfg.get("tcp_password") or None
                except Exception:
                    pass

            self.logger.info(
                f"Station {self.station_id} - Address: {self.ip_number}:{self.ip_port}, "
                f"FTP Passive: {self.pasv}, anonymous: {self.ftp_anonymous}"
            )

        except KeyError as e:
            raise ConfigurationError(f"Missing configuration key: {e}")
        except ValueError as e:
            raise ConfigurationError(f"Invalid port number: {e}")

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to receiver.

        Tests router connectivity (ping) and receiver HTTP web interface (port 8060).
        This is consistent with Icinga monitoring which uses HTTP as the primary
        receiver test since all receivers have web interfaces.

        Returns:
            Dictionary with router and receiver connection status
        """
        import socket
        import subprocess

        router_ok = False
        receiver_ok = False
        error_msg = None

        # Test 1: Router connectivity (ping)
        try:
            # Ping with 2 second timeout, 3 packets (tolerate lossy links)
            result = subprocess.run(
                ["ping", "-c", "3", "-W", "2", self.ip_number],
                capture_output=True,
                timeout=8,
            )
            router_ok = result.returncode == 0
        except Exception as e:
            error_msg = f"Router ping failed: {e}"

        # Test 2: Receiver HTTP web interface (port 8060)
        sock = None
        try:
            # Try to connect to HTTP port 8060
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((self.ip_number, 8060))
            receiver_ok = result == 0
            if not receiver_ok and not error_msg:
                error_msg = "HTTP port 8060 not responding"
        except Exception as e:
            if not error_msg:
                error_msg = f"Receiver test failed: {e}"
        finally:
            if sock is not None:
                sock.close()

        status = {
            "router": router_ok,
            "receiver": receiver_ok,
            "ip": self.ip_number,
            "port": self.ip_port,  # Keep original FTP port in config
            "http_port": 8060,  # Add HTTP port
            "timestamp": datetime.now(UTC).isoformat(),
            "error": error_msg if not (router_ok and receiver_ok) else None,
        }

        self.connection_status = status
        return status

    def get_file_extension(self) -> str:
        """Get Septentrio file extension from configuration.

        Returns:
            File extension with compression (e.g., '.sbf.gz')
        """
        return self.septentrio_config.get("file_extension", ".sbf.gz")

    def get_session_letter(self, session: str) -> str:
        """Get session letter for Septentrio receiver.

        Args:
            session: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')

        Returns:
            Session letter code from session mapping ('a', 'b', 'c')
        """
        if session in self.session_map:
            return self.session_map[session][0]  # First element is the letter
        return "a"  # Default fallback

    def _build_remote_template(self, session: str, compression: str) -> str:
        """Build the gtimes template for remote (on-receiver) file paths.

        Default Septentrio fleet layout::

            {base_path}{LOG-session-dir}/%y%j/{STATION}#Rin2_{compression}

        The session directory comes from ``session_map[session][1]`` (e.g.
        ``LOG1_15s_24hr``) and filenames use IGS ``#Rin2`` naming.

        Subclasses (e.g. :class:`~receivers.septentrio.mosaic_x5.MosaicX5`)
        override this to support non-standard on-disk layouts — a custom session
        directory and/or a different filename pattern.
        """
        session_dir = self.session_map[session][1]
        return f"{self.base_path}{session_dir}/%y%j/{self.station_id}#Rin2_{compression}"

    def download_data(
        self,
        start: Optional[Union[datetime, str]] = None,
        end: Optional[Union[datetime, str]] = None,
        session: str = "15s_24hr",
        sync: bool = False,
        clean_tmp: bool = True,
        archive: bool = True,
        reverse_chronological: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        """Download data from PolaRX5 receiver with comprehensive validation.

        This is the main download function that handles file synchronization
        from the receiver to the local archive with integrity checking and
        fault-tolerant archiving.

        Args:
            start: Start time for download period
            end: End time for download period
            session: Data session type (e.g., '15s_24hr', '1Hz_1hr')
            sync: Whether to actually sync files (False for dry run)
            clean_tmp: Whether to clean temporary directory before download
            archive: Whether to archive downloaded files
            reverse_chronological: Download newest files first (True for -D flag routine downloads,
                                  False for --start/--end backfilling). Default True.
            **kwargs: Additional parameters (ffrequency, afrequency, etc.)

        Returns:
            Dictionary with download results and file information
        """
        # Extract legacy parameters from kwargs for backward compatibility
        loglevel = kwargs.get("loglevel", logging.WARNING)
        ffrequency = kwargs.get("ffrequency", "1D")
        compression = kwargs.get("compression", ".gz")
        immediate_archive = kwargs.get("immediate_archive", True)
        predir = kwargs.get("predir", "/DSK2/SSN/")

        # Immediate retry configuration (Level 1 retries)
        max_retries = kwargs.get("max_retries", 3)
        retry_initial_delay = kwargs.get("retry_initial_delay", 0.5)
        retry_missing = kwargs.get("retry_missing", False)

        # Set logger level
        self.logger.setLevel(loglevel)

        # Get centralized configuration paths
        # Include session in tmp path to prevent filename collisions between sessions
        tmp_dir = self.receivers_config.get_tmp_dir()
        tmp_dir_path = Path(tmp_dir) / self.station_id / session
        tmp_dir_path.mkdir(parents=True, exist_ok=True)

        # Clean tmp directory if requested
        if clean_tmp:
            self.logger.info(f"Cleaning temporary directory: {tmp_dir_path}")
            files_removed = self.file_validator.clean_directory(str(tmp_dir_path))
            if files_removed > 0:
                self.logger.info(f"Removed {files_removed} files from tmp directory")

        # Handle time parameters and performance tracking
        start_time = time.time()

        # Quick reachability check to skip offline stations fast
        if not self._quick_ping():
            self.logger.warning(
                f"Station {self.station_id} is unreachable (ping failed), skipping download"
            )
            return {
                "station_id": self.station_id,
                "receiver_type": self.get_receiver_type(),
                "status": "unreachable",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": "Station unreachable (ping failed)",
                "duration": time.time() - start_time,
            }
        tcp_result = self._quick_tcp_check(self.ip_port, return_details=True)
        if isinstance(tcp_result, dict):
            tcp_ok = bool(tcp_result.get("success"))
            tcp_msg: str = (
                tcp_result.get("message") or f"FTP port {self.ip_port} not responding"
            )
        else:
            tcp_ok = bool(tcp_result)
            tcp_msg = f"FTP port {self.ip_port} not responding"
        if not tcp_ok:
            self.logger.warning(
                f"Station {self.station_id} FTP port {self.ip_port} not responding — {tcp_msg}"
            )
            return {
                "station_id": self.station_id,
                "receiver_type": self.get_receiver_type(),
                "status": "unreachable",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error_message": tcp_msg,
                "duration": time.time() - start_time,
            }

        start, end = self._process_time_parameters(start, end, session)

        # Initialize performance metrics
        performance_metrics = {
            "success": False,
            "duration": 0.0,
            "bytes_downloaded": 0,
            "avg_speed": 0.0,
            "had_timeout": False,
            "timeout_type": None,
            "connection_time": 0.0,
        }

        self.logger.info(f"Checking {session} sessions from {start} to {end}")

        # Generate datetime list using unified build_path method
        file_datetime_list = self.build_path(
            None, "#datelist", session, ffrequency, start, end
        )
        self.logger.info(f"Generated {len(file_datetime_list)} timestamps")

        # Create archive paths using unified method
        archive_template = self.receivers_config.get_archive_template()
        data_prepath = self.receivers_config.get_data_prepath()
        extension = self.get_file_extension()

        # Build archive template
        full_archive_template = archive_template.format(
            data_prepath=data_prepath,
            station="{station}",
            session="{session}",
            extension=extension,
            session_letter="{session_letter}",
        )
        archive_file_list = self.build_path(
            file_datetime_list, full_archive_template, session, ffrequency
        )

        # Create remote paths with filenames using unified method.
        # Built via _build_remote_template() so subclasses can override the
        # on-receiver directory / filename layout (non-standard setups).
        remote_template = self._build_remote_template(session, compression)
        remote_full_paths = self.build_path(
            file_datetime_list, remote_template, session, ffrequency
        )

        # Extract remote directories and IGS filenames
        remote_path_list = []
        igs_file_list = []
        for full_path in remote_full_paths:
            remote_dir = os.path.dirname(full_path) + "/"  # Add trailing slash for FTP
            igs_filename = os.path.basename(full_path)
            remote_path_list.append(remote_dir)
            igs_file_list.append(igs_filename)

        file_date_dict = dict(
            zip(file_datetime_list, zip(archive_file_list, igs_file_list))
        )

        # Create remote path mapping for later use in _sync_missing_files
        remote_path_dict = dict(zip(file_datetime_list, remote_path_list))

        # Find missing and invalid files using comprehensive validation
        self.logger.debug("Using Phase 1 ArchiveValidator")
        (
            all_missing_files,
            validated_files,
            corrupted_files_removed,
            files_archived_from_tmp,
        ) = self._validate_files_phase1(file_date_dict, tmp_dir_path)

        # Common logging after validation
        if corrupted_files_removed > 0:
            self.logger.info(
                f"Removed {corrupted_files_removed} corrupted/incomplete files"
            )

        if files_archived_from_tmp > 0:
            self.logger.info(
                f"Archived {files_archived_from_tmp} files from tmp directory"
            )

        self.logger.info(f"Validated {validated_files} existing files")

        if not all_missing_files:
            archive_dir = (
                Path(archive_file_list[0]).parent if archive_file_list else None
            )
            self.logger.info(f"Archive is up to date ({archive_dir})")

            # Register validated archived files in file_tracking
            # (ensures files found on disk appear in the dashboard)
            self._register_archived_files(file_date_dict, session)

            # Record performance metrics for successful up-to-date case
            performance_metrics.update(
                {
                    "success": True,
                    "duration": time.time() - start_time,
                    "bytes_downloaded": 0,
                    "avg_speed": 0.0,
                }
            )
            self._record_performance_metrics(performance_metrics)

            return {
                "status": "up_to_date",
                "files_checked": len(file_date_dict),
                "files_missing": 0,
                "files_downloaded": 0,
                "duration": time.time() - start_time,
            }

        self.logger.info(f"Missing files: {len(all_missing_files)}")

        downloaded_files_dict = {}
        total_bytes = 0
        sync_success = True

        # For dry run, try connection first - run diagnostics only if it fails
        if not sync and all_missing_files:
            self.logger.info("Testing FTP connection for dry run...")

            # Optimistic approach: try connection first
            try:
                from ftplib import FTP

                ftp = FTP()
                ftp.connect(self.ip_number, self.ip_port, timeout=3)
                ftp.close()
                self.logger.info(
                    f"✅ Connection test OK: {self.ip_number}:{self.ip_port}"
                )
            except Exception as e:
                # Connection failed - now run diagnostics to understand why
                try:
                    from ..base.download_diagnostics import DownloadDiagnosticsAnalyzer

                    diagnostics = DownloadDiagnosticsAnalyzer(
                        self.station_id, self.logger
                    )
                    network_check = diagnostics.classify_network_failure(self.ip_number)
                except ImportError:
                    # Diagnostics module not yet implemented - use basic error handling
                    network_check = {"classification": "unknown"}

                if network_check["classification"] == "invalid_ip":
                    self.logger.critical(
                        f"❌ INVALID IP: {self.ip_number} - likely configuration typo"
                    )
                    self.logger.critical(
                        "💡 SUGGESTED FIX: Check station configuration for correct IP address"
                    )
                elif "connection refused" in str(e).lower():
                    self.logger.error(
                        f"❌ FTP CONNECTION REFUSED: {self.ip_number}:{self.ip_port}"
                    )
                    if network_check["classification"] == "network_ok":
                        self.logger.error(
                            f"💡 Router responds but FTP refused - Wrong port ({self.ip_port}) or port forwarding issue"
                        )
                    else:
                        self.logger.error(
                            f"💡 LIKELY ISSUE: Wrong FTP port ({self.ip_port}) or port forwarding not configured"
                        )
                    self.logger.error(
                        f"💡 SUGGESTED ACTION: Verify ftpport={self.ip_port} in station configuration"
                    )
                else:
                    self.logger.warning(f"⚠️ Connection test failed: {e}")
                    if network_check.get("analysis"):
                        self.logger.info(
                            f"Network analysis: {network_check['analysis']}"
                        )

        try:
            if sync:
                downloaded_files_dict = self._sync_missing_files(
                    all_missing_files,
                    tmp_dir_path,
                    session,
                    predir,
                    ffrequency,
                    archive,
                    immediate_archive,
                    compression,
                    remote_path_dict,
                    max_retries,
                    retry_initial_delay,
                    reverse_chronological,
                    retry_missing,
                )

                # Calculate total bytes downloaded
                for file_path in downloaded_files_dict.values():
                    if os.path.isfile(file_path):
                        total_bytes += os.path.getsize(file_path)

        except Exception as e:
            sync_success = False
            self._last_error = e  # Store error for status reporting

            # Handle ConfigurationError (like invalid IP) with concise output
            from ..base.exceptions import ConfigurationError

            if isinstance(e, ConfigurationError):
                # For configuration errors, provide minimal output focused on the fix
                self.logger.critical(f"❌ Configuration Error: {e}")
                performance_metrics.update(
                    {
                        "success": False,
                        "duration": time.time() - start_time,
                        "error_type": "configuration_error",
                        "error_category": e.category.value
                        if hasattr(e, "category")
                        else "unknown",
                        "validation_needed": True,
                    }
                )
                self._record_performance_metrics(performance_metrics)

                return {
                    "status": "configuration_error",
                    "error": str(e),
                    "error_type": "invalid_ip_range",
                    "suggested_fix": getattr(
                        e, "suggested_fix", "Check station configuration"
                    ),
                    "files_checked": len(file_date_dict),
                    "files_missing": len(all_missing_files),
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                    "validation_triggered": True,
                }

            # For other errors, log normally with emoji style
            error_type = type(e).__name__
            self.logger.error(f"❌ Sync failed: {error_type}: {e}")

            # Check if this was a timeout-related error
            if any(
                timeout_word in str(e).lower()
                for timeout_word in ["timeout", "timed out"]
            ):
                performance_metrics["had_timeout"] = True
                if "connection" in str(e).lower():
                    performance_metrics["timeout_type"] = "connection"
                elif "inactivity" in str(e).lower():
                    performance_metrics["timeout_type"] = "inactivity"
                else:
                    performance_metrics["timeout_type"] = "progress"

            pass

        # Record final performance metrics
        final_duration = time.time() - start_time
        performance_metrics.update(
            {
                "success": sync_success and len(downloaded_files_dict) > 0,
                "duration": final_duration,
                "bytes_downloaded": total_bytes,
                "avg_speed": total_bytes / final_duration
                if final_duration > 0
                else 0.0,
                "connection_time": getattr(self, "_last_connection_time", 0.0),
            }
        )
        self._record_performance_metrics(performance_metrics)

        # Determine final status based on sync_success and actual results
        if not sync_success:
            # Sync raised an exception - determine error category
            error_msg = "Connection error"
            if performance_metrics.get("had_timeout"):
                timeout_type = performance_metrics.get("timeout_type", "unknown")
                error_msg = f"Timeout ({timeout_type})"
            elif hasattr(self, "_last_error"):
                error_msg = str(self._last_error)

            return {
                "status": "failed",
                "error_message": error_msg,
                "files_checked": len(file_date_dict),
                "files_missing": len(all_missing_files),
                "files_downloaded": len(downloaded_files_dict),
                "downloaded_files": list(downloaded_files_dict.values()),
                "duration": final_duration,
                "had_timeout": performance_metrics.get("had_timeout", False),
                "timeout_type": performance_metrics.get("timeout_type"),
            }

        # If sync was requested and there were missing files but none downloaded,
        # all individual file downloads failed (e.g., watchdog timeouts).
        # Surface the dominant per-file error and connection target so the DB
        # log entry is self-explanatory (no need to grep stations/{SID}.log).
        if sync and all_missing_files and not downloaded_files_dict:
            _last_err = getattr(self, "_last_file_error", None)
            _msg = f"All file downloads failed (0 of {len(all_missing_files)}) @ {self.ip_number}:{self.ip_port}"
            if _last_err:
                _msg += f" — last: {_last_err}"
            return {
                "status": "failed",
                "error_message": _msg,
                "files_checked": len(file_date_dict),
                "files_missing": len(all_missing_files),
                "files_downloaded": 0,
                "downloaded_files": [],
                "duration": final_duration,
            }

        return {
            "status": "completed" if sync else "dry_run",
            "files_checked": len(file_date_dict),
            "files_missing": len(all_missing_files),
            "files_downloaded": len(downloaded_files_dict),
            "downloaded_files": list(downloaded_files_dict.values()),
            "duration": final_duration,
        }

    def _validate_files_phase1(self, file_date_dict, tmp_dir_path):
        """Validate files using Phase 1 ArchiveValidator.

        Args:
            file_date_dict: Dict mapping datetime -> (archive_path, igs_filename)
            tmp_dir_path: Path to temporary download directory

        Returns:
            Tuple of (all_missing_files, validated_files, corrupted_files_removed, files_archived_from_tmp)
        """
        # Convert format for ArchiveValidator
        # ArchiveValidator expects: filename -> remote_path (for missing files)
        # and filename -> archive_path (for archive files)
        files_dict = {}  # filename -> remote_path mapping
        archive_paths_dict = {}  # filename -> archive_path mapping

        for dt, (archive_path, igs_filename) in file_date_dict.items():
            files_dict[igs_filename] = (
                archive_path  # Use archive path as "remote" for validation
            )
            archive_paths_dict[igs_filename] = archive_path

        # Use batch validation (now returns files_in_tmp_dict as 4th element)
        missing_files_dict, found_count, validated_count, files_in_tmp_dict = (
            self.archive_validator.batch_validate_archives(
                files_dict, archive_paths_dict, tmp_dir_path
            )
        )

        # Archive files from tmp if found
        files_archived_from_tmp = 0
        if files_in_tmp_dict:
            self.logger.info(
                f"Archiving {len(files_in_tmp_dict)} files from tmp directory..."
            )
            from ..utils.file_archiver import ArchiveMode, FileArchiver

            with FileArchiver(mode=ArchiveMode.BULK, logger=self.logger) as archiver:
                for igs_filename, tmp_path in files_in_tmp_dict.items():
                    # Find the archive destination
                    archive_dest = archive_paths_dict.get(igs_filename)
                    if archive_dest:
                        success = archiver.archive_file(
                            tmp_path,
                            Path(archive_dest),
                            compress=False,  # Already compressed
                            remove_tmp=True,
                        )
                        if success:
                            files_archived_from_tmp += 1

            stats = archiver.get_statistics()
            self.logger.info(
                f"Archived {stats['successful']}/{len(files_in_tmp_dict)} files from tmp to archive"
            )

        # Convert back to expected format: datetime -> (archive_path, igs_filename)
        all_missing_files = {}
        for igs_filename in missing_files_dict.keys():
            # Find corresponding datetime key
            for dt, (arch_path, igs_file) in file_date_dict.items():
                if igs_file == igs_filename:
                    all_missing_files[dt] = (arch_path, igs_file)
                    break

        self.logger.info(
            f"Phase 1 validation: {validated_count} files checked, {found_count} found, {len(missing_files_dict)} missing"
        )

        return (
            all_missing_files,
            validated_count,
            0,
            files_archived_from_tmp,
        )  # missing, validated, corrupted_removed, archived_from_tmp

    def _register_archived_files(self, file_date_dict: dict, session: str) -> None:
        """Register already-archived files in file_tracking.

        When --sync finds all files already in the archive, we still need
        to ensure they have file_tracking entries so the dashboard shows them.
        """
        try:
            from ..health import FileTracker

            file_tracker = FileTracker()
            if not file_tracker.connect():
                self.logger.debug("Could not connect to DB for archive registration")
                return

            is_hourly = "1hr" in session.lower()
            registered = 0

            for dt, (archive_path, igs_filename) in file_date_dict.items():
                file_date = dt.date() if hasattr(dt, "date") else dt
                file_hour = dt.hour if is_hourly else None

                # Get file size from the archive on disk
                archive_file = Path(archive_path)
                file_size = (
                    archive_file.stat().st_size if archive_file.exists() else None
                )

                # Record the LOCAL archive filename, not the FTP-side IGS name,
                # so file_tracking.filename matches what's on disk and consumers
                # that join on filename see consistent rows.
                if file_tracker.mark_file_archived(
                    self.station_id,
                    session,
                    file_date,
                    file_hour,
                    archive_file.name,
                    file_size,
                ):
                    registered += 1

            file_tracker.close()

            if registered:
                self.logger.info(
                    f"Registered {registered} archived files in file_tracking"
                )
        except Exception as e:
            self.logger.debug(f"Could not register archived files: {e}")

    def _process_time_parameters(self, start, end, session):
        """Process and validate time parameters using Phase 1 TimeParameterProcessor."""
        self.logger.debug("Using Phase 1 TimeParameterProcessor")
        return self.time_processor.process_time_parameters(start, end, session)

    def _sync_missing_files(
        self,
        missing_file_dict,
        tmp_dir,
        session,
        predir,
        ffrequency,
        archive,
        immediate_archive,
        compression=".gz",
        remote_path_dict=None,
        max_retries=3,
        retry_initial_delay=0.5,
        reverse_chronological=True,
        retry_missing=False,
    ):
        """Sync missing files from receiver to local archive with retry configuration.

        Args:
            reverse_chronological: If True, download newest files first (for routine downloads).
                                  If False, download oldest files first (for long-term backfilling).
        """
        # Simple approach: use pre-built paths and extract IGS filenames

        # Sort missing_file_dict by datetime - order depends on use case
        # -D flag (routine): reverse=True downloads Oct 7 → Oct 1 (prioritize latest data)
        # --start/--end (backfilling): reverse=False downloads Oct 1 → Oct 7 (chronological fill)
        sorted_missing_items = sorted(
            missing_file_dict.items(), reverse=reverse_chronological
        )

        # Extract IGS filenames and get corresponding remote paths
        download_file_dict = {}
        for dt, (_archive_path, igs_filename) in sorted_missing_items:
            if remote_path_dict and dt in remote_path_dict:
                remote_path = remote_path_dict[dt]
            else:
                # Fallback to old method if remote_path_dict not available
                raise ValueError("Remote path dictionary is required but not provided")
            download_file_dict[igs_filename] = remote_path

        # Create properly ordered missing_file_dict for immediate archiving
        updated_missing_file_dict = dict(sorted_missing_items)

        # Connect and download
        ftp = self._ftp_open_connection()
        if not ftp:
            raise ConnectionError(
                f"Could not connect to {self.ip_number}:{self.ip_port}"
            )

        try:
            downloaded_files = self._ftp_download(
                download_file_dict,
                tmp_dir,
                ftp=ftp,
                archive=archive,
                immediate_archive=immediate_archive,
                missing_file_dict=updated_missing_file_dict,
                max_retries=max_retries,
                retry_initial_delay=retry_initial_delay,
                session=session,
                retry_missing=retry_missing,
            )

            # Build dict by matching file paths to datetime keys (not positional)
            # zip() misaligns when files are skipped (known missing, 550, errors)
            downloaded_files_dict = {}
            _path_to_dt = {}
            for dt_key, (arch_path, igs_filename) in updated_missing_file_dict.items():
                _path_to_dt[arch_path] = dt_key
                _path_to_dt[igs_filename] = dt_key
            for dl_path in downloaded_files:
                dt_key = _path_to_dt.get(dl_path) or _path_to_dt.get(
                    os.path.basename(dl_path)
                )
                if dt_key is not None:
                    downloaded_files_dict[dt_key] = dl_path

            # Archive files (only if not using immediate archiving)
            if downloaded_files_dict and archive and not immediate_archive:
                self._archive_files(downloaded_files_dict, updated_missing_file_dict)

            return downloaded_files_dict

        finally:
            ftp.close()

    def _ftp_open_connection(
        self, timeout: Optional[int] = None, skip_ping_check: bool = False
    ) -> Optional[FTP]:
        """Open FTP connection to receiver with fast-fail checks.

        Args:
            timeout: Optional connection timeout override
            skip_ping_check: If True, skip the initial connectivity checks (for retries where we already know network is up)
        """
        connection_start = time.time()

        if not skip_ping_check:
            # Fast-fail check 1: Ping (detect unreachable hosts)
            # Send 3 packets to tolerate lossy 3G/4G links
            import subprocess

            self.logger.info(f"Checking connectivity to {self.ip_number}...")
            ping_success = False

            try:
                ping_result = subprocess.run(
                    ["ping", "-c", "3", "-W", "2", self.ip_number],
                    capture_output=True,
                    timeout=8,
                )
                ping_success = ping_result.returncode == 0
            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                self.logger.debug(f"Ping check error: {e}")
                ping_success = True  # Skip ping on error, let FTP handle it

            if not ping_success:
                self.logger.error(
                    f"❌ Network unreachable: {self.ip_number} - ping failed"
                )
                self.logger.error(
                    "💡 Check network connectivity or wait for receiver to come online"
                )
                self._last_connection_time = time.time() - connection_start
                return None

            # Fast-fail check 2: TCP port check with retries
            import socket

            self.logger.info(f"Checking FTP port {self.ip_port}...")
            port_retries = 3
            port_delay = 1.0
            port_success = False

            for port_attempt in range(port_retries):
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(3)
                    result = sock.connect_ex((self.ip_number, self.ip_port))
                    if result == 0:
                        port_success = True
                        break
                    else:
                        if port_attempt < port_retries - 1:
                            self.logger.warning(
                                f"⚠️  Port check attempt {port_attempt + 1}/{port_retries} failed, "
                                f"retrying in {port_delay}s..."
                            )
                            time.sleep(port_delay)
                except TimeoutError:
                    if port_attempt < port_retries - 1:
                        self.logger.warning(
                            f"⚠️  Port check attempt {port_attempt + 1}/{port_retries} timed out, "
                            f"retrying in {port_delay}s..."
                        )
                        time.sleep(port_delay)
                except Exception as e:
                    self.logger.debug(f"Port check error: {e}")
                    port_success = True  # Skip on error, let FTP handle it
                    break
                finally:
                    if sock is not None:
                        sock.close()

            if not port_success:
                self.logger.error(
                    f"❌ FTP port {self.ip_port} not responding after {port_retries} attempts"
                )
                self.logger.error("💡 Receiver may be down or FTP port misconfigured")
                self._last_connection_time = time.time() - connection_start
                return None

        # Proceed with FTP connection (should be fast now since port is verified)
        if timeout is None:
            timeout = self.connection_timeout
        self.logger.info(f"Connecting to FTP {self.ip_number}:{self.ip_port}...")

        ftp = None
        try:
            ftp = FTP()
            ftp.connect(self.ip_number, self.ip_port, timeout=timeout)
            if self.ftp_anonymous:
                ftp.login("anonymous")
            else:
                ftp.login(self.ftp_username or "anonymous", self.ftp_password or "")
            ftp.set_pasv(self.pasv)
            connection_time = time.time() - connection_start
            self.logger.info(f"✅ Connected in {connection_time:.2f}s")

            # Store connection time in instance variable for performance tracking
            self._last_connection_time = connection_time

            return ftp
        except Exception as e:
            # Close the partially-open FTP socket to prevent zombie connections.
            # If ftp.connect() succeeded but login() failed, the TCP socket is
            # in ESTAB state and will linger indefinitely without explicit close.
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass

            connection_time = time.time() - connection_start
            error_str = str(e).lower()

            # Connection failed - now run intelligent diagnostics to determine why
            self.logger.info(
                f"Connection failed after {connection_time:.2f}s - running diagnostics..."
            )

            try:
                from ..base.download_diagnostics import DownloadDiagnosticsAnalyzer

                diagnostics = DownloadDiagnosticsAnalyzer(self.station_id, self.logger)
                # Quick network classification to understand the failure
                network_check = diagnostics.classify_network_failure(self.ip_number)
            except ImportError:
                # Diagnostics module not yet implemented - use basic error handling
                self.logger.debug(
                    "Diagnostic module not available, using basic error handling"
                )
                network_check = {"classification": "unknown"}

            # If it's an invalid IP range, provide critical error immediately
            if network_check["classification"] == "invalid_ip":
                self.logger.critical(
                    f"❌ INVALID IP: {self.ip_number} - likely configuration typo"
                )
                self.logger.critical(
                    "💡 SUGGESTED FIX: Check station configuration for correct IP address"
                )

                from ..base.exceptions import ConfigurationError, FailureCategory

                raise ConfigurationError(
                    message=f"Invalid IP range {self.ip_number} - likely configuration typo",
                    station_id=self.station_id,
                    category=FailureCategory.DNS_FAILURE,
                    config_field="router_ip",
                    actual_value=self.ip_number,
                    suggested_fix="Verify IP address in station configuration",
                )

            # Provide intelligent error analysis based on failure type and network status
            if "connection refused" in error_str:
                self.logger.error(
                    f"❌ FTP CONNECTION REFUSED: {self.ip_number}:{self.ip_port}"
                )
                if network_check["classification"] == "network_ok":
                    self.logger.error(
                        f"💡 Router responds but FTP refused - Wrong port ({self.ip_port}) or port forwarding issue"
                    )
                else:
                    self.logger.error(
                        f"💡 LIKELY ISSUE: Wrong FTP port ({self.ip_port}) or port forwarding not configured"
                    )
                self.logger.error(
                    f"💡 SUGGESTED ACTION: Verify ftpport={self.ip_port} in station configuration"
                )
            elif "timeout" in error_str or "timed out" in error_str:
                if network_check["classification"] == "network_ok":
                    self.logger.error(
                        "⚠️ RECEIVER TIMEOUT: Router responds but receiver doesn't on FTP port"
                    )
                    self.logger.error(
                        "💡 LIKELY ISSUE: Receiver down, ethernet broken, or firewall blocking FTP"
                    )
                else:
                    self.logger.error(
                        f"⚠️ NETWORK TIMEOUT: {network_check.get('analysis', 'No analysis available')}"
                    )
            elif (
                "530" in error_str
                or "login incorrect" in error_str
                or "login failed" in error_str
            ):
                self.logger.error(
                    f"❌ FTP authentication failed for {self.station_id}: {e}"
                )
                if self.ftp_anonymous:
                    self.logger.error(
                        "💡 FTP is using anonymous login — if this receiver requires credentials, "
                        "set ftp_anonymous_login = false in receivers.cfg"
                    )
                else:
                    self.logger.error(
                        f"💡 Check tcp_username/tcp_password in receivers.cfg for {self.station_id}"
                    )
            else:
                self.logger.error(f"Connection failed: {e}")
                if network_check.get("analysis"):
                    self.logger.info(f"Network analysis: {network_check['analysis']}")

            self._last_connection_time = connection_time
            return None

    @staticmethod
    def _disk_filename(fname, missing_file_dict):
        """Return the local archive basename for an FTP filename.

        file_tracking.filename must match what's written to disk, not the
        FTP-side template name (e.g. AFST145j.26_.gz). The local archive
        path is stored alongside the IGS name in missing_file_dict; fall
        back to the FTP name only if we can't resolve it.
        """
        if not missing_file_dict:
            return fname
        for _dt_key, (arch_path, igs_filename) in missing_file_dict.items():
            if fname == igs_filename:
                return Path(arch_path).name
        return fname

    def _ftp_download(
        self,
        files_dict,
        local_dir,
        ftp=None,
        archive=True,
        immediate_archive=True,
        missing_file_dict=None,
        max_retries=3,
        retry_initial_delay=0.5,
        session=None,
        retry_missing=False,
    ):
        """Download files via FTP with progress tracking and immediate retries.

        Args:
            files_dict: Dictionary of {filename: remote_dir}
            local_dir: Local directory for downloads
            ftp: FTP connection (optional, will create if None)
            archive: Whether to archive downloaded files
            immediate_archive: Whether to archive immediately after each file
            missing_file_dict: Dictionary mapping datetimes to (archive_path, filename)
            max_retries: Maximum immediate retries on transient failures (default: 3)
            retry_initial_delay: Initial retry delay in seconds (default: 0.5)
            session: Session type for file tracking (e.g., '15s_24hr', '1Hz_1hr')
        """
        downloaded_files = []
        # Reset before each sync so the wrapper at sync_data() shows the
        # dominant per-file error (Connection refused / Broken pipe / 404 / ...).
        self._last_file_error: Optional[str] = None

        # Initialize file tracker for download tracking
        file_tracker = None
        try:
            from ..health import FileTracker

            file_tracker = FileTracker()
            if not file_tracker.connect():
                file_tracker = None
                self.logger.debug("File tracking disabled (database unavailable)")
        except ImportError:
            self.logger.debug("File tracking disabled (psycopg2 not installed)")

        # Log station connection details once at the beginning
        self.logger.info(f"Station connection: {self.ip_number}:{self.ip_port}")

        # Track unique paths to log each only once
        logged_paths = set()

        # For immediate archiving, we need to track which datetime each file corresponds to
        # This will be populated from the calling code

        # Helper to find datetime for a file from missing_file_dict
        def get_file_datetime(fname):
            """Find datetime key for a file from missing_file_dict."""
            if not missing_file_dict:
                return None
            for dt_key, (_arch_path, igs_filename) in missing_file_dict.items():
                if fname == igs_filename:
                    return dt_key
            return None

        def disk_filename(fname):
            return self._disk_filename(fname, missing_file_dict)

        # Determine if session is hourly (for file_hour tracking)
        is_hourly_session = session and "1hr" in session.lower()

        # Iterate in the order provided by _sync_missing_files (already sorted by datetime)
        # DO NOT re-sort here - filename-based sorting breaks year boundary ordering
        for file_name, remote_dir in files_dict.items():
            # Guard: reconnect if FTP connection was killed by error handler or watchdog
            if ftp is None or getattr(ftp, "sock", None) is None:
                self.logger.warning("FTP connection dead, reconnecting...")
                try:
                    if ftp is not None:
                        ftp.close()
                except Exception:
                    pass
                ftp = self._ftp_open_connection(skip_ping_check=True)
                if not ftp:
                    self.logger.error(
                        "Failed to reconnect FTP - aborting remaining files"
                    )
                    break
                self.logger.info("FTP reconnected after dead connection")

            # Check if file is known to be missing (skip download attempt)
            # Skip this filter when retry_missing=True (scheduler always retries)
            if file_tracker and session and not retry_missing:
                file_dt = get_file_datetime(file_name)
                if file_dt:
                    file_date = file_dt.date() if hasattr(file_dt, "date") else file_dt
                    file_hour = file_dt.hour if is_hourly_session else None
                    if file_tracker.is_file_missing(
                        self.station_id, session, file_date, file_hour
                    ):
                        self.logger.info(
                            f"⏭️  Skipping {file_name} (known missing, not retrying)"
                        )
                        continue

            # Log remote directory path only once per unique path
            if remote_dir not in logged_paths:
                self.logger.info(f"Remote path: {remote_dir}")
                logged_paths.add(remote_dir)

            self.logger.info(f"Downloading {file_name}")

            local_file = local_dir / file_name
            # Initialize offset for download resumption
            offset = 0
            size_mismatch_retried = False
            _dl_start = None
            remote_file_size = None

            remote_file = f"{remote_dir}{file_name}"

            try:
                # Check if remote file exists and get size (like getSeptentrio3)
                try:
                    remote_file_size = ftp.size(remote_file)
                except Exception as e:
                    # Check if it's a "file not found" vs "connection error"
                    error_msg = str(e).lower()
                    if (
                        "550" in error_msg
                        or "not found" in error_msg
                        or "no such file" in error_msg
                    ):
                        # Remote file is missing - check local file for archiving
                        remote_file_size = None
                        if local_file.exists():
                            local_size = local_file.stat().st_size
                            if local_size > 0:
                                self.logger.info(
                                    f"📁 Remote file {file_name} missing, but local copy exists ({local_size:,} bytes)"
                                )
                                self.logger.info(
                                    f"   Adding existing local file to archive queue: {local_file}"
                                )
                                # Add to downloaded_files for archiving (it's a complete file from previous download)
                                downloaded_files.append(str(local_file))
                            else:
                                # Zero-size file should be removed
                                self.logger.warning(
                                    f"🗑️ Removing zero-size local file: {local_file}"
                                )
                                local_file.unlink()
                            continue
                        else:
                            self.logger.error(
                                f"❌ Remote file {file_name} not found on server"
                            )
                            # Mark file as missing in tracker
                            if file_tracker and session:
                                file_dt = get_file_datetime(file_name)
                                if file_dt:
                                    track_date = (
                                        file_dt.date()
                                        if hasattr(file_dt, "date")
                                        else file_dt
                                    )
                                    track_hour = (
                                        file_dt.hour if is_hourly_session else None
                                    )
                                    file_tracker.mark_file_missing(
                                        self.station_id,
                                        session,
                                        track_date,
                                        track_hour,
                                        disk_filename(file_name),
                                    )
                            continue  # No local, no remote - nothing to do
                    else:
                        # Connection/server error - can't determine file status
                        error_str = str(e).lower()

                        # Check if this is a connection timeout error
                        if (
                            "timed out" in error_str
                            or "cannot read from timed out" in error_str
                        ):
                            self.logger.warning(
                                f"⚠️  FTP connection timed out while checking {file_name}"
                            )
                            self.logger.info(
                                "🔄 Attempting to reconnect FTP session..."
                            )

                            # Close dead connection
                            try:
                                ftp.close()
                            except:
                                pass

                            # Reconnect (skip ping check - we know network was up)
                            ftp = self._ftp_open_connection(skip_ping_check=True)
                            if not ftp:
                                self.logger.error(
                                    "❌ Failed to reconnect - skipping remaining files"
                                )
                                break  # Exit the download loop

                            self.logger.info("✅ FTP reconnected successfully")

                            # Try to get file size again with new connection
                            try:
                                remote_file_size = ftp.size(remote_file)
                            except Exception as retry_e:
                                retry_error_str = str(retry_e).lower()
                                if (
                                    "550" in retry_error_str
                                    or "not found" in retry_error_str
                                ):
                                    # File really doesn't exist
                                    remote_file_size = None
                                    if local_file.exists():
                                        local_size = local_file.stat().st_size
                                        if local_size > 0:
                                            self.logger.info(
                                                f"📁 Remote file {file_name} missing, but local copy exists ({local_size:,} bytes)"
                                            )
                                            downloaded_files.append(str(local_file))
                                        else:
                                            self.logger.warning(
                                                f"🗑️ Removing zero-size local file: {local_file}"
                                            )
                                            local_file.unlink()
                                    else:
                                        self.logger.error(
                                            f"❌ Remote file {file_name} not found on server"
                                        )
                                        # Mark file as missing in tracker
                                        if file_tracker and session:
                                            file_dt = get_file_datetime(file_name)
                                            if file_dt:
                                                track_date = (
                                                    file_dt.date()
                                                    if hasattr(file_dt, "date")
                                                    else file_dt
                                                )
                                                track_hour = (
                                                    file_dt.hour
                                                    if is_hourly_session
                                                    else None
                                                )
                                                file_tracker.mark_file_missing(
                                                    self.station_id,
                                                    session,
                                                    track_date,
                                                    track_hour,
                                                    disk_filename(file_name),
                                                )
                                    continue
                                else:
                                    self.logger.error(
                                        f"⚠️  Cannot check remote file after reconnect: {retry_e}"
                                    )
                                    remote_file_size = None
                        else:
                            # Other connection/server errors
                            self.logger.error(
                                f"⚠️  Cannot check remote file {file_name}: {e}"
                            )
                            remote_file_size = None

                # Now that we have remote file size, check if we should resume existing local file
                if local_file.exists():
                    should_resume, resume_offset = (
                        self.file_validator.should_resume_download(
                            str(local_file), remote_file_size
                        )
                    )
                    if should_resume:
                        offset = resume_offset
                        self.logger.info(f"📄 Resuming download from {offset:,} bytes")
                    else:
                        self.logger.info(
                            "🔄 Starting fresh download (invalid tmp file removed)"
                        )

                if remote_file_size is not None:
                    # Use progress bar download with immediate retry logic
                    from ..utils.stall_timeout import record_download

                    _dl_start = time.time()
                    diff, ftp = self._download_with_immediate_retry(
                        ftp,
                        remote_file,
                        str(local_file),
                        remote_file_size,
                        offset,
                        max_retries=max_retries,
                        initial_delay=retry_initial_delay,
                        session_type=session,
                    )
                    _dl_duration = time.time() - _dl_start

                    # Validate download completeness (like getSeptentrio3)
                    local_file_size = local_file.stat().st_size
                    self.logger.info(
                        f"Remote file size: {remote_file_size} bytes, Local file size: {local_file_size} bytes"
                    )
                    self.logger.info(
                        f"Difference between remote and downloaded file: {diff} bytes"
                    )

                    if diff == 0:
                        self._handle_successful_download(
                            file_name,
                            local_file,
                            local_file_size,
                            remote_file_size,
                            session,
                            _dl_duration,
                            downloaded_files,
                            file_tracker,
                            is_hourly_session,
                            immediate_archive,
                            archive,
                            missing_file_dict,
                            record_download,
                            get_file_datetime=get_file_datetime,
                        )
                    else:
                        self.logger.error(
                            f"❌ Download incomplete for {file_name}: size mismatch of {diff} bytes"
                        )
                        self.logger.error(
                            f"   Expected: {remote_file_size:,} bytes, Got: {local_file_size:,} bytes"
                        )

                        # Delete corrupt file and retry once clean (no resume)
                        if not size_mismatch_retried:
                            self.logger.info(
                                "🔄 Deleting corrupt file and retrying clean download..."
                            )
                            try:
                                os.unlink(local_file)
                            except OSError:
                                pass
                            size_mismatch_retried = True

                            _dl_start = time.time()
                            try:
                                diff, ftp = self._download_with_immediate_retry(
                                    ftp,
                                    remote_file,
                                    str(local_file),
                                    remote_file_size,
                                    0,
                                    max_retries=1,
                                    initial_delay=1.0,
                                    session_type=session,
                                )
                            except Exception as retry_e:
                                self.logger.error(
                                    f"❌ Clean retry failed for {file_name}: {retry_e}"
                                )
                                _file_dt = get_file_datetime(file_name)
                                _err_msg = (
                                    f"Size mismatch clean retry failed: {retry_e}"
                                )
                                self._last_file_error = _err_msg
                                record_download(
                                    self.station_id,
                                    session or "unknown",
                                    "failed",
                                    file_date=_file_dt.date() if _file_dt else None,
                                    filename=file_name,
                                    duration_seconds=time.time() - _dl_start,
                                    file_size=remote_file_size,
                                    stall_timeout_used=getattr(
                                        self,
                                        "_last_effective_timeout",
                                        self.progress_timeout,
                                    ),
                                    message=_err_msg,
                                )
                                continue

                            _dl_duration = time.time() - _dl_start
                            local_file_size = (
                                local_file.stat().st_size if local_file.exists() else 0
                            )

                            if diff == 0:
                                self._handle_successful_download(
                                    file_name,
                                    local_file,
                                    local_file_size,
                                    remote_file_size,
                                    session,
                                    _dl_duration,
                                    downloaded_files,
                                    file_tracker,
                                    is_hourly_session,
                                    immediate_archive,
                                    archive,
                                    missing_file_dict,
                                    record_download,
                                    get_file_datetime=get_file_datetime,
                                )
                            else:
                                _file_dt = get_file_datetime(file_name)
                                _err_msg = f"Size mismatch after clean retry: got {local_file_size}, expected {remote_file_size}"
                                self._last_file_error = _err_msg
                                record_download(
                                    self.station_id,
                                    session or "unknown",
                                    "failed",
                                    file_date=_file_dt.date() if _file_dt else None,
                                    filename=file_name,
                                    duration_seconds=_dl_duration,
                                    bytes_downloaded=local_file_size,
                                    file_size=remote_file_size,
                                    stall_timeout_used=getattr(
                                        self,
                                        "_last_effective_timeout",
                                        self.progress_timeout,
                                    ),
                                    message=_err_msg,
                                )
                        else:
                            _file_dt = get_file_datetime(file_name)
                            _err_msg = f"Size mismatch: got {local_file_size}, expected {remote_file_size}"
                            self._last_file_error = _err_msg
                            record_download(
                                self.station_id,
                                session or "unknown",
                                "failed",
                                file_date=_file_dt.date() if _file_dt else None,
                                filename=file_name,
                                duration_seconds=_dl_duration,
                                bytes_downloaded=local_file_size,
                                file_size=remote_file_size,
                                stall_timeout_used=getattr(
                                    self,
                                    "_last_effective_timeout",
                                    self.progress_timeout,
                                ),
                                message=_err_msg,
                            )

                else:
                    # Fallback to simple download without progress
                    file_mode = "ab" if offset > 0 else "wb"
                    ftp.timeout = self.data_transfer_timeout
                    with open(local_file, file_mode) as f:
                        ftp.retrbinary(f"RETR {remote_file}", f.write, rest=offset)

                    # Without remote size, just check if file grew
                    if local_file.exists() and local_file.stat().st_size > offset:
                        # Validate downloaded file integrity
                        validation_result = self.file_validator.validate_file(
                            str(local_file)
                        )
                        if not validation_result["valid"]:
                            self.logger.warning(
                                f"Downloaded file failed validation: {validation_result['error']}"
                            )
                            self.logger.info(
                                f"Removing invalid downloaded file: {local_file}"
                            )
                            try:
                                os.unlink(local_file)
                                continue  # Skip this file
                            except OSError as e:
                                self.logger.error(
                                    f"Could not remove invalid file {local_file}: {e}"
                                )
                                continue

                        # Mark file as downloaded in tracker (fallback path)
                        if file_tracker and session:
                            file_dt = get_file_datetime(file_name)
                            if file_dt:
                                track_date = (
                                    file_dt.date()
                                    if hasattr(file_dt, "date")
                                    else file_dt
                                )
                                track_hour = file_dt.hour if is_hourly_session else None
                                fallback_size = (
                                    local_file.stat().st_size
                                    if local_file.exists()
                                    else None
                                )
                                file_tracker.mark_file_downloaded(
                                    self.station_id,
                                    session,
                                    track_date,
                                    track_hour,
                                    disk_filename(file_name),
                                    fallback_size,
                                    remote_file_size=remote_file_size,
                                )

                        # Immediate archiving if enabled (same logic as progress bar path)
                        if immediate_archive and archive and missing_file_dict:
                            # Find the datetime key for this file by matching the downloaded filename
                            file_datetime = None
                            for dt_key, (
                                _arch_path,
                                igs_filename,
                            ) in missing_file_dict.items():
                                if file_name == igs_filename:
                                    file_datetime = dt_key
                                    self.logger.info(
                                        f"✅ Found match: {file_name} -> {dt_key}"
                                    )
                                    break

                            if file_datetime and self._archive_single_file(
                                str(local_file),
                                file_datetime,
                                {file_datetime: missing_file_dict[file_datetime]},
                            ):
                                # File successfully archived - mark as archived with remote size
                                archive_path_fb = missing_file_dict[file_datetime][0]
                                downloaded_files.append(archive_path_fb)
                                if file_tracker and session:
                                    file_dt = get_file_datetime(file_name)
                                    if file_dt:
                                        track_date = (
                                            file_dt.date()
                                            if hasattr(file_dt, "date")
                                            else file_dt
                                        )
                                        track_hour = (
                                            file_dt.hour if is_hourly_session else None
                                        )
                                        archive_size_fb = (
                                            Path(archive_path_fb).stat().st_size
                                            if Path(archive_path_fb).exists()
                                            else None
                                        )
                                        file_tracker.mark_file_archived(
                                            self.station_id,
                                            session,
                                            track_date,
                                            track_hour,
                                            Path(archive_path_fb).name,
                                            archive_size_fb,
                                            remote_file_size=remote_file_size,
                                        )
                                self.logger.info(
                                    f"Successfully downloaded and archived {file_name} (fallback path)"
                                )
                            else:
                                # Archive failed - add tmp file path
                                downloaded_files.append(str(local_file))
                                self.logger.info(
                                    f"Successfully downloaded {file_name} (fallback path, archiving failed)"
                                )
                        else:
                            # No immediate archiving - add tmp file path
                            downloaded_files.append(str(local_file))
                            self.logger.info(
                                f"Successfully downloaded {file_name} (size validation not available, integrity validated)"
                            )
                    else:
                        self.logger.warning(f"Download may have failed for {file_name}")

            except Exception as e:
                self.logger.error(f"Failed to download {file_name}: {e}")
                _exc_duration = (
                    (time.time() - _dl_start) if _dl_start is not None else 0.0
                )
                error_msg = str(e).lower()
                is_stall = (
                    isinstance(e, TimeoutError)
                    or "stall" in error_msg
                    or "watchdog" in error_msg
                )
                from ..utils.stall_timeout import record_download as _rec_dl

                _file_dt = get_file_datetime(file_name)
                _err_msg = str(e)[:500]
                self._last_file_error = _err_msg
                _rec_dl(
                    self.station_id,
                    session or "unknown",
                    "stall_timeout" if is_stall else "failed",
                    file_date=_file_dt.date() if _file_dt else None,
                    filename=file_name,
                    duration_seconds=_exc_duration,
                    file_size=remote_file_size,
                    stall_timeout_used=getattr(
                        self, "_last_effective_timeout", self.progress_timeout
                    ),
                    message=_err_msg,
                )
                # Guard: reconnect if the exception killed the FTP connection.
                # Without this, the next file's ftp.size() crashes with
                # "'NoneType' object has no attribute 'sendall'".
                if ftp is None or getattr(ftp, "sock", None) is None:
                    self.logger.warning(
                        "FTP connection dead after exception, reconnecting..."
                    )
                    try:
                        if ftp is not None:
                            ftp.close()
                    except Exception:
                        pass
                    ftp = self._ftp_open_connection(skip_ping_check=True)
                    if ftp:
                        self.logger.info("✅ FTP reconnected after exception")
                    else:
                        self.logger.error(
                            "Failed to reconnect FTP after exception - aborting remaining files"
                        )
                        break
                continue

        # Close file tracker connection
        if file_tracker:
            file_tracker.close()

        return downloaded_files

    def _handle_successful_download(
        self,
        file_name,
        local_file,
        local_file_size,
        remote_file_size,
        session,
        _dl_duration,
        downloaded_files,
        file_tracker,
        is_hourly_session,
        immediate_archive,
        archive,
        missing_file_dict,
        record_download,
        get_file_datetime=None,
    ) -> bool:
        """Validate, record, track, and archive a successfully downloaded file.

        Returns True if file was handled (valid or removed), False if validation
        failed and file could not be removed.
        """
        self.logger.info(
            f"✅ Successfully downloaded {file_name} ({local_file_size:,} bytes)"
        )

        # Resolve file_date once for both code paths so download_log carries it.
        _file_dt = get_file_datetime(file_name) if get_file_datetime else None
        _file_date = _file_dt.date() if _file_dt and hasattr(_file_dt, "date") else None

        # Validate downloaded file integrity
        validation_result = self.file_validator.validate_file(str(local_file))
        if not validation_result["valid"]:
            self.logger.warning(
                f"Downloaded file failed validation: {validation_result['error']}"
            )
            self.logger.info(f"Removing invalid downloaded file: {local_file}")
            _err_msg = f"Validation failed: {validation_result.get('error', 'unknown')}"
            self._last_file_error = _err_msg
            record_download(
                self.station_id,
                session or "unknown",
                "failed",
                file_date=_file_date,
                filename=file_name,
                duration_seconds=_dl_duration,
                bytes_downloaded=local_file_size,
                file_size=remote_file_size,
                stall_timeout_used=getattr(
                    self, "_last_effective_timeout", self.progress_timeout
                ),
                message=_err_msg,
            )
            try:
                os.unlink(local_file)
            except OSError as e:
                self.logger.error(f"Could not remove invalid file {local_file}: {e}")
            return True  # Handled (skip this file)

        self.logger.debug(
            f"Downloaded file validated: {validation_result['compression']} compression, {validation_result['size']} bytes"
        )
        record_download(
            self.station_id,
            session or "unknown",
            "completed",
            file_date=_file_date,
            filename=file_name,
            duration_seconds=_dl_duration,
            bytes_downloaded=local_file_size,
            file_size=remote_file_size,
            stall_timeout_used=getattr(
                self, "_last_effective_timeout", self.progress_timeout
            ),
        )

        # Mark file as downloaded in tracker
        if file_tracker and session and get_file_datetime:
            file_dt = get_file_datetime(file_name)
            if file_dt:
                track_date = file_dt.date() if hasattr(file_dt, "date") else file_dt
                track_hour = file_dt.hour if is_hourly_session else None
                file_tracker.mark_file_downloaded(
                    self.station_id,
                    session,
                    track_date,
                    track_hour,
                    self._disk_filename(file_name, missing_file_dict),
                    local_file_size,
                    remote_file_size=remote_file_size,
                )

        # Immediate archiving if enabled
        if immediate_archive and archive and missing_file_dict:
            file_datetime = None
            for dt_key, (_arch_path, igs_filename) in missing_file_dict.items():
                if file_name == igs_filename:
                    file_datetime = dt_key
                    self.logger.info(f"✅ Found match: {file_name} -> {dt_key}")
                    break

            if file_datetime and self._archive_single_file(
                str(local_file),
                file_datetime,
                {file_datetime: missing_file_dict[file_datetime]},
            ):
                archive_path = missing_file_dict[file_datetime][0]
                downloaded_files.append(archive_path)

                # Per-file RINEX callback (set by --rinex flag)
                on_archived = getattr(self, "_on_file_archived", None)
                if on_archived:
                    try:
                        on_archived(archive_path)
                    except Exception:
                        pass  # RINEX must never fail a download

                if file_tracker and session and get_file_datetime:
                    file_dt = get_file_datetime(file_name)
                    if file_dt:
                        track_date = (
                            file_dt.date() if hasattr(file_dt, "date") else file_dt
                        )
                        track_hour = file_dt.hour if is_hourly_session else None
                        archive_size = (
                            Path(archive_path).stat().st_size
                            if Path(archive_path).exists()
                            else local_file_size
                        )
                        file_tracker.mark_file_archived(
                            self.station_id,
                            session,
                            track_date,
                            track_hour,
                            Path(archive_path).name,
                            archive_size,
                            remote_file_size=remote_file_size,
                        )
            else:
                downloaded_files.append(str(local_file))
        else:
            downloaded_files.append(str(local_file))

        return True

    def _download_with_progressbar(
        self,
        ftp,
        remote_file,
        local_file,
        remote_file_size,
        offset=0,
        session_type=None,
    ):
        """Download file with progress bar display and intelligent timeout handling.

        Implements station-specific timeout handling:
        - Progress-based timeouts (don't timeout if making progress)
        - Inactivity timeouts (timeout if no progress at all)
        - Speed-based timeouts (timeout if too slow overall)
        - Station-specific thresholds for mobile/remote stations
        """
        # Compute effective timeout: adaptive (data-driven) when possible,
        # otherwise falls back to self.progress_timeout from _setup_timeouts().
        effective_timeout = self._get_effective_timeout(
            session_type=session_type, expected_file_size=remote_file_size
        )
        # Store for callers that need to log which timeout was actually used
        self._last_effective_timeout = effective_timeout
        if effective_timeout != self.progress_timeout:
            self.logger.info(
                f"Adaptive timeout: {effective_timeout}s "
                f"(default {self.progress_timeout}s) for {Path(remote_file).name}"
            )
        if not progressbar_available:
            # Fallback without progress bar
            file_mode = "ab" if offset > 0 else "wb"
            # Set timeout for data socket (used when creating PASV connection)
            ftp.timeout = self.data_transfer_timeout
            with open(local_file, file_mode) as f:
                ftp.retrbinary(f"RETR {remote_file}", f.write, rest=offset)
        else:
            # Use tqdm progress bar with intelligent timeout monitoring
            filename = Path(remote_file).name
            desc = f"Downloading {filename}"

            # Progress monitoring variables
            last_progress_time = time.time()
            start_time = time.time()
            timeout_extended = (
                False  # One-time extension flag for near-complete downloads
            )
            _first_chunk_logged = False  # Diagnostic: confirm recv path is firing

            # Shared state for watchdog thread
            import threading

            # Initialise at `offset` so the value reflects the absolute byte
            # position in the file (matching how the watchdog compares to
            # `offset`). Tracked independently of pbar.n — pbar.update() is a
            # no-op when tqdm has disable=True (which is the case in the
            # scheduler context, stderr is not a TTY), so pbar.n never advances
            # past `initial=offset` there.
            bytes_received = [offset]  # Mutable container for thread communication
            download_done = threading.Event()
            watchdog_killed = [False]
            data_socket = [None]  # Store data socket so watchdog can close it

            # Packet-loss-aware watchdog: stations with high packet loss
            # need longer watchdog timeouts to avoid premature kills
            try:
                from ..utils.stall_timeout import get_packet_loss_factor

                _loss_factor = get_packet_loss_factor(self.station_id)
            except Exception:
                _loss_factor = 1.0

            # Effective zero-progress watchdog timeout. The 60 s floor was
            # calibrated 2026-05-10 (THOB/ENTC) — the receiver needs time
            # to open the file and (on resume) seek to REST. Computed once
            # here so the error messages below report the value actually
            # used, not the raw `data_transfer_timeout` (10 s default).
            watchdog_effective_timeout = (
                max(self.data_transfer_timeout, 60) * _loss_factor
            )

            import sys as _sys

            with tqdm(
                total=remote_file_size,
                initial=offset,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=desc,
                disable=not _sys.stderr.isatty(),
            ) as pbar:
                file_mode = "ab" if offset > 0 else "wb"
                with open(local_file, file_mode) as f:

                    def watchdog():
                        """Kill connection if stuck at 0% for too long.

                        Floor of 60 s for first byte: the receiver needs time
                        to open the file and (on resume) seek to the REST
                        position. Active-mode-after-passive-failure burns
                        ~5-10 s on the dance itself, leaving little margin
                        before any data arrives. Calibrated empirically on
                        2026-05-10 — THOB and ENTC succeed at 60 s, fail at
                        10-30 s for the 5 MB daily file when active mode is
                        the working path. Resume case kept compatible (was
                        already 30 s minimum).
                        """
                        # Use the pre-computed effective timeout (floor + loss factor)
                        # so the watchdog and the TimeoutError messages report the
                        # same value.
                        timeout = watchdog_effective_timeout
                        check_interval = 0.5  # Check every 500ms
                        elapsed = 0

                        while not download_done.is_set():
                            if download_done.wait(timeout=check_interval):
                                break  # Download completed normally

                            elapsed += check_interval

                            # Only kill if stuck at initial offset (0% progress).
                            # bytes_received is now an independent counter
                            # (advances per chunk in the recv loop regardless
                            # of pbar disabled state), and on_disk is a
                            # belt-and-suspenders check via os.path.getsize
                            # which works for both str and Path (callers
                            # convert local_file to str before passing — see
                            # _download_with_immediate_retry call site).
                            on_disk = offset
                            try:
                                on_disk = os.path.getsize(local_file)
                            except Exception:
                                pass
                            if (
                                bytes_received[0] <= offset
                                and on_disk <= offset
                                and elapsed >= timeout
                            ):
                                self.logger.warning(
                                    f"⚠️  Watchdog: No data received in {elapsed:.1f}s, killing connection "
                                    f"(bytes_received={bytes_received[0]}, on_disk={on_disk}, offset={offset})"
                                )
                                watchdog_killed[0] = True
                                # Close DATA socket (this is what actually unblocks recv)
                                try:
                                    if data_socket[0]:
                                        data_socket[0].close()
                                except:
                                    pass
                                # Also close control socket
                                try:
                                    ftp.close()
                                except:
                                    pass
                                break

                    # Start watchdog thread for 0% stall detection
                    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
                    watchdog_thread.start()

                    try:
                        import select

                        # Use transfercmd directly so we have access to the data socket
                        # Set short timeout on BOTH control and data sockets
                        ftp.timeout = self.data_transfer_timeout
                        if ftp.sock:
                            ftp.sock.settimeout(
                                self.data_transfer_timeout
                            )  # Control socket too!
                        ftp.voidcmd("TYPE I")  # Set binary mode
                        conn = ftp.transfercmd(f"RETR {remote_file}", rest=offset)
                        data_socket[0] = conn  # Store for watchdog access
                        conn.setblocking(False)  # Non-blocking for select()

                        # Manual recv loop with select() for fast timeout
                        while True:
                            # Check if watchdog killed us
                            if watchdog_killed[0]:
                                raise TimeoutError(
                                    f"No data received within {watchdog_effective_timeout:.0f}s - connection killed by watchdog"
                                )

                            # Use select with short timeout to poll socket
                            ready, _, _ = select.select([conn], [], [], 0.5)

                            if ready:
                                try:
                                    chunk = conn.recv(8192)
                                except (OSError, ConnectionError):
                                    # Socket was closed by watchdog
                                    if watchdog_killed[0]:
                                        raise TimeoutError(
                                            f"No data received within {watchdog_effective_timeout:.0f}s - connection killed by watchdog"
                                        )
                                    raise

                                if not chunk:
                                    break

                                # Write chunk and update progress.
                                # pbar.update is a no-op when tqdm.disable=True
                                # (scheduler context has no TTY), so do not
                                # source bytes_received from pbar.n — track
                                # independently.
                                f.write(chunk)
                                pbar.update(len(chunk))
                                bytes_received[0] += len(chunk)

                                # Diagnostic: log the FIRST chunk per attempt so
                                # we can confirm the recv path is delivering
                                # bytes during the watchdog window.
                                if not _first_chunk_logged:
                                    try:
                                        _disk_now = os.path.getsize(local_file)
                                    except Exception:
                                        _disk_now = -1
                                    self.logger.info(
                                        f"📥 First chunk: {len(chunk)} bytes "
                                        f"(bytes_received={bytes_received[0]}, "
                                        f"on_disk={_disk_now}, offset={offset}, "
                                        f"elapsed={time.time() - start_time:.1f}s)"
                                    )
                                    _first_chunk_logged = True

                                # Reset progress timer when we receive data
                                last_progress_time = time.time()

                            # ALWAYS check timeout conditions (even when no data received)
                            # This ensures we detect stalls at any progress level
                            current_time = time.time()
                            time_since_last_progress = current_time - last_progress_time

                            # Check for inactivity timeout (no progress)
                            if time_since_last_progress > self.inactivity_timeout:
                                raise ConnectionError(
                                    f"Download timed out: no progress for {time_since_last_progress:.1f}s"
                                )

                            # Hard ceiling on single download attempt.
                            # PolaRX5 supports FTP resume, so retry will continue
                            # from where it left off — killing is cheap.
                            total_time = current_time - start_time
                            if total_time > effective_timeout:
                                current_bytes = bytes_received[0]

                                # One-time extension: if >70% done, let it finish
                                if (
                                    not timeout_extended
                                    and (remote_file_size - offset) > 0
                                ):
                                    progress_pct = (
                                        (current_bytes - offset)
                                        / (remote_file_size - offset)
                                        * 100
                                    )
                                    if progress_pct > 70:
                                        extension = effective_timeout * 0.5
                                        effective_timeout += extension
                                        timeout_extended = True
                                        self.logger.info(
                                            f"⏱️  Timeout extended by {extension:.0f}s "
                                            f"(progress {progress_pct:.0f}%, "
                                            f"new limit {effective_timeout:.0f}s)"
                                        )
                                        continue  # Re-check with new limit

                                avg_speed = (
                                    (current_bytes - offset) / total_time
                                    if total_time > 0
                                    else 0
                                )
                                raise ConnectionError(
                                    f"Download timed out after {total_time:.0f}s "
                                    f"(limit {effective_timeout}s, "
                                    f"speed {avg_speed:.0f} B/s, "
                                    f"{current_bytes - offset} bytes received)"
                                )

                        conn.close()
                        ftp.voidresp()  # Get transfer complete response

                    except Exception:
                        download_done.set()
                        watchdog_thread.join(timeout=1)
                        # Force-close data socket — watchdog only handles 0%-stall,
                        # progress_timeout fires when data IS flowing (just slowly)
                        try:
                            if data_socket[0]:
                                data_socket[0].close()
                        except Exception:
                            pass
                        try:
                            ftp.close()
                        except Exception:
                            pass
                        # If watchdog killed the connection, raise TimeoutError instead
                        if watchdog_killed[0]:
                            raise TimeoutError(
                                f"No data received within {watchdog_effective_timeout:.0f}s - connection killed by watchdog"
                            ) from None
                        raise  # Re-raise original error if not watchdog-related
                    else:
                        download_done.set()
                        watchdog_thread.join(timeout=1)

        local_file_size = os.path.getsize(local_file)
        return local_file_size - remote_file_size

    def _download_with_immediate_retry(
        self,
        ftp,
        remote_file,
        local_file,
        remote_file_size,
        offset=0,
        max_retries=3,
        initial_delay=0.5,
        session_type=None,
    ):
        """Download with immediate retries on transient failures.

        Retries transient connection/timeout errors with increasing delays:
        - Attempt 1: immediate
        - Attempt 2: after 0.5s delay (with reconnection)
        - Attempt 3: after 1.0s delay (with reconnection)
        - Attempt 4: after 1.5s delay (with reconnection)

        Args:
            ftp: FTP connection (will be reconnected on timeout)
            remote_file: Remote file path
            local_file: Local file path
            remote_file_size: Remote file size in bytes
            offset: Resume offset
            max_retries: Maximum number of retries (default: 3)
            initial_delay: Initial retry delay in seconds (default: 0.5)
            session_type: Session type for adaptive timeout (optional)

        Returns:
            Tuple of (download_result, ftp_connection):
            - download_result: Bytes difference between remote and local file
            - ftp_connection: FTP connection (may be reconnected)

        Raises:
            Non-retryable errors (AuthenticationError, file not found)
        """
        from ..base.exceptions import AuthenticationError

        # Define non-retryable error patterns
        non_retryable_patterns = [
            "530",  # Authentication failed
            "550",  # File not found
            "not found",
            "no such file",
            "authentication",
            "login",
        ]

        # Timeout/connection error patterns that need reconnection
        timeout_patterns = [
            "timed out",
            "timeout",
            "cannot read from timed out",
            "connection reset",
            "broken pipe",
            "watchdog",  # Watchdog killed stalled connection
            "sendall",  # Dead socket: ftp.sock = None after close
            "nonetype",  # Any NoneType attribute error on ftp object
        ]

        last_exception = None

        for attempt in range(max_retries + 1):  # +1 for initial attempt
            try:
                # Delegate to existing FTP mode retry logic
                result, ftp = self._download_with_progressbar_and_retry(
                    ftp,
                    remote_file,
                    local_file,
                    remote_file_size,
                    offset,
                    session_type=session_type,
                )
                return (
                    result,
                    ftp,
                )  # Return both result and (potentially new) connection

            except AuthenticationError:
                # Don't retry authentication failures
                raise

            except Exception as e:
                error_msg = str(e).lower()
                last_exception = e

                # FTP 554 "Restart offset … is too large for file size …":
                # the partial on disk overshoots the current remote file,
                # most likely because we're resuming against a freshly-rotated
                # smaller version. Server even tells us "Restart offset reset
                # to 0" — but we ignore that and keep using the same partial.
                # Self-heal: drop the oversized partial, force offset=0 so the
                # next attempt starts fresh. Without this, the failure is
                # permanent — every attempt RESTs to the same too-large value.
                if "554" in error_msg and "too large" in error_msg:
                    if os.path.isfile(local_file):
                        try:
                            os.unlink(local_file)
                            self.logger.warning(
                                "⚠️  Server returned 554 (REST > remote size). "
                                "Deleted oversized partial to break the loop; "
                                "next attempt starts from 0."
                            )
                        except OSError as exc:
                            self.logger.warning(
                                f"Could not delete oversized partial: {exc}"
                            )
                    offset = 0

                # Check if this is a non-retryable error
                if any(pattern in error_msg for pattern in non_retryable_patterns):
                    # File not found or authentication - don't retry
                    raise

                # This is a retryable error (connection, timeout, etc.)
                if attempt < max_retries:
                    # Calculate delay with increasing backoff
                    delay = initial_delay * (attempt + 1)
                    self.logger.warning(
                        f"⚠️  Download attempt {attempt + 1} failed: {e}"
                    )

                    # Update offset to resume from where we left off.
                    # PolaRX5 FTP supports REST (resume), so partial data is not wasted.
                    # _safe_resume_offset checks the partial isn't oversized vs.
                    # the current remote file (otherwise REST > size yields 554
                    # and locks us into a permanent failure loop).
                    new_offset = _safe_resume_offset(
                        local_file, remote_file_size, self.logger
                    )
                    if new_offset > offset:
                        self.logger.info(
                            f"📦 Partial download: {new_offset} bytes on disk, "
                            f"will resume from byte {new_offset}"
                        )
                        offset = new_offset
                    elif new_offset == 0 and os.path.isfile(local_file) is False:
                        # Helper deleted an oversized partial — start fresh
                        offset = 0

                    # Check if we need to reconnect.  Two triggers:
                    # 1. Error pattern matches known timeout/connection issues
                    # 2. FTP socket is dead (ftp.close() was called by a lower
                    #    layer, e.g. watchdog or _download_with_progressbar
                    #    exception handler, setting sock=None)
                    ftp_dead = ftp is None or getattr(ftp, "sock", None) is None
                    if ftp_dead or any(
                        pattern in error_msg for pattern in timeout_patterns
                    ):
                        self.logger.info(
                            "🔄 Closing dead FTP connection and reconnecting..."
                        )
                        try:
                            if ftp is not None:
                                ftp.close()
                        except Exception:
                            pass  # Ignore errors closing dead connection

                        # Reconnect (skip ping check - we know network was up)
                        ftp = self._ftp_open_connection(skip_ping_check=True)
                        if not ftp:
                            self.logger.error(
                                "❌ Failed to reconnect - aborting retries"
                            )
                            raise ConnectionError("Could not reconnect to FTP server")

                        self.logger.info("✅ FTP reconnected successfully")

                    self.logger.info(
                        f"🔄 Retrying in {delay:.1f}s (attempt {attempt + 2}/{max_retries + 1})..."
                    )
                    time.sleep(delay)
                else:
                    # Final attempt failed
                    self.logger.error(
                        f"❌ Download failed after {max_retries + 1} attempts: {e}"
                    )
                    raise last_exception

    def _download_with_progressbar_and_retry(
        self,
        ftp,
        remote_file,
        local_file,
        remote_file_size,
        offset=0,
        session_type=None,
    ):
        """Download with progress bar and intelligent FTP mode retry on connection issues.

        Returns:
            Tuple of (size_diff, ftp_connection) — ftp_connection may be a new
            connection if a mode-switch reconnect was needed.
        """
        try:
            # Try with current FTP mode first
            result = self._download_with_progressbar(
                ftp,
                remote_file,
                local_file,
                remote_file_size,
                offset,
                session_type=session_type,
            )
            return result, ftp
        except Exception as e:
            error_msg = str(e).lower()

            # Check if it's a data connection issue that might benefit from mode switching
            connection_errors = [
                "connection refused",
                "errno 111",
                "data connection",
                "port",
                "won't open a connection",  # FTP passive mode NAT mismatch
                "i won't open",  # Variation of NAT mismatch error
                "500 i won't",  # FTP 500 error from passive mode
                # Watchdog-killed zero-byte stall: the FTP control channel
                # established and we know the file size, but the data channel
                # never delivered a single byte before the watchdog timed out.
                # Same root cause as the explicit FTP errors above (data
                # channel dead — typically broken nf_conntrack_ftp helper on
                # the station's NAT router for non-standard ports). Without
                # this trigger every retry stays in passive mode and stalls
                # the same way, dragging the live-window tail by 30+ minutes
                # across the fleet of NAT'd stations.
                "killed by watchdog",
                "no data received within",
            ]
            if any(err in error_msg for err in connection_errors):
                self.logger.warning(
                    f"⚠️  Data connection failed with {self._get_ftp_mode_description()}: {e}"
                )

                # Try switching FTP mode — reconnect first since the 500
                # error typically kills the control connection.
                original_pasv = ftp.passiveserver
                new_pasv = not original_pasv
                try:
                    try:
                        ftp.close()
                    except Exception:
                        pass

                    self.logger.info(
                        f"🔄 Reconnecting with {self._get_ftp_mode_description(new_pasv)} mode..."
                    )
                    ftp_new = self._ftp_open_connection(skip_ping_check=True)
                    if not ftp_new:
                        self.logger.error("❌ Failed to reconnect for mode switch")
                        raise e

                    ftp_new.set_pasv(new_pasv)

                    # Preserve any bytes already on disk and resume from there.
                    # Original code deleted the file with the rationale that
                    # "passive data may be corrupt after mode switch". In
                    # practice TCP guarantees in-order delivery, so received
                    # bytes are correct as far as they go.
                    #
                    # Why preserve: when the network forces a passive→active
                    # dance on every attempt (2026-05-08 outage shape), deleting
                    # on every switch nukes accumulated progress every time.
                    #
                    # Safety guard: the partial may be larger than the current
                    # remote file (e.g. accumulated under a previous code
                    # revision, or the receiver rotated to a smaller file
                    # version). Resuming with REST > remote size yields a 554
                    # error from the server and locks us into a permanent
                    # failure loop. Detect and reset to fresh in that case.
                    resume_offset = _safe_resume_offset(
                        local_file, remote_file_size, self.logger
                    )
                    if resume_offset > 0:
                        self.logger.info(
                            f"📦 Mode-switch resume: keeping {resume_offset} "
                            f"bytes on disk, will RETR with REST {resume_offset}"
                        )

                    # Retry download on the fresh connection, resuming from
                    # whatever's already on disk.
                    result = self._download_with_progressbar(
                        ftp_new,
                        remote_file,
                        local_file,
                        remote_file_size,
                        offset=resume_offset,
                        session_type=session_type,
                    )

                    # Update our internal mode preference for this station so
                    # remaining files in this batch don't repeat the dance.
                    self.pasv = new_pasv

                    # Persist the observation so the NEXT scheduler run starts
                    # in the working mode. Recorded into cfg_discrepancy with
                    # detected_by='ftp_handshake' — operator can review via
                    # `receivers cfg list --field ftp_mode` and promote to
                    # stations.cfg when ready. The whole block is best-effort:
                    # any failure (no station_info on a unit-test fixture, DB
                    # unavailable, missing migration) is logged at debug and
                    # never blocks the download.
                    try:
                        new_mode = "active" if not new_pasv else "passive"
                        station_info = getattr(self, "station_info", {}) or {}
                        cfg_mode = station_info.get("router", {}).get(
                            "ftp_mode"
                        ) or station_info.get("receiver", {}).get("ftp_mode")
                        if cfg_mode != new_mode:
                            from ..cfg import discrepancy_log as _dlog

                            _dlog.record_detection(
                                self.station_id,
                                "ftp_mode",
                                cfg_value=cfg_mode,
                                receiver_value=new_mode,
                                tos_value=None,
                                verdict="conflict",
                                detected_by=_dlog.DETECTED_BY_FTP_HANDSHAKE,
                            )
                            self.logger.info(
                                f"✅ Success with {self._get_ftp_mode_description(new_pasv)} mode "
                                f"(cfg_mode={cfg_mode!r} → observed={new_mode!r}, "
                                f"recorded to cfg_discrepancy)"
                            )
                        else:
                            self.logger.info(
                                f"✅ Success with {self._get_ftp_mode_description(new_pasv)} mode"
                            )
                    except Exception as _exc:
                        self.logger.debug(
                            f"Could not record ftp_mode observation: {_exc}"
                        )

                    # Return the NEW working connection so the caller can
                    # use it for subsequent files
                    return result, ftp_new

                except Exception as retry_e:
                    self.logger.error(
                        f"❌ Both FTP modes failed. Original: {e}, Retry: {retry_e}"
                    )
                    raise e
            else:
                # Not a connection error we can fix with mode switching
                raise e

    def _get_ftp_mode_description(self, pasv=None):
        """Get human-readable FTP mode description."""
        if pasv is None:
            pasv = getattr(self, "pasv", True)
        return "passive" if pasv else "active"

    def _archive_single_file(
        self, tmp_file_path: str, file_datetime, missing_file_dict
    ) -> bool:
        """Archive a single file immediately after download using Phase 1 FileArchiver.

        Args:
            tmp_file_path: Path to the temporary downloaded file
            file_datetime: The datetime key for this file
            missing_file_dict: Dict mapping datetime to (archive_path, remote_path) tuples

        Returns:
            True if archiving succeeded, False otherwise
        """
        self.logger.debug("Using Phase 1 FileArchiver (IMMEDIATE mode)")
        destination = missing_file_dict[file_datetime][0]

        # Check if file is already compressed by reading magic bytes
        # PolaRX5 downloads .sbf.gz files (gzip compressed) - don't compress again!
        should_compress = needs_compression(Path(tmp_file_path))

        with FileArchiver(mode=ArchiveMode.IMMEDIATE, logger=self.logger) as archiver:
            success = archiver.archive_file(
                Path(tmp_file_path),
                Path(destination),
                compress=should_compress,  # Only compress if not already compressed
                remove_tmp=True,
            )

        return success

    def _archive_files(self, downloaded_files_dict, missing_file_dict):
        """Move downloaded files to archive locations using Phase 1 FileArchiver (BULK mode)."""
        self.logger.debug("Using Phase 1 FileArchiver (BULK mode)")

        with FileArchiver(mode=ArchiveMode.BULK, logger=self.logger) as archiver:
            for ddate, tmp_file in downloaded_files_dict.items():
                if not os.path.isfile(tmp_file):
                    continue
                destination = missing_file_dict[ddate][0]

                # Check if file is already compressed by reading magic bytes
                # PolaRX5 downloads .sbf.gz files (gzip compressed) - don't compress again!
                should_compress = needs_compression(Path(tmp_file))

                archiver.archive_file(
                    Path(tmp_file),
                    Path(destination),
                    compress=should_compress,  # Only compress if not already compressed
                    remove_tmp=True,
                )
            # Auto-flushes on context exit

        stats = archiver.get_statistics()
        self.logger.info(
            f"Archiving complete: {stats['successful']}/{stats['total_files']} files archived"
        )
        return stats["successful"]

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of PolaRX5 receiver.

        Tries live TCP extraction first, falls back to local SBF file extraction
        if TCP is unavailable.

        Returns:
            Dictionary with health status information following health-data-spec.md
        """
        from ..health.connection_checker import ConnectionChecker
        from ..health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        # Step 1: Try live health extraction via TCP
        metrics = None
        data_quality = None
        connection_data = {}
        extraction_source = None
        port_status = (
            None  # Track port status separately to ensure it's always captured
        )
        extractor = None
        ping_result = None  # Track ICMP ping result separately
        receiver_identity = None  # Receiver model/firmware/serial from SBF

        host = self.ip_number
        if host:
            # Build port configuration from station config
            receiver_config = self.station_info.get("receiver", {})
            port_config = {
                "ftp": int(receiver_config.get("ftpport") or 2160),
                "http": int(receiver_config.get("httpport") or 8060),
                "control": int(receiver_config.get("controlport") or 28784),
            }
            control_port = port_config["control"]

            # Step 1a: Check ICMP ping first (router/network reachability)
            try:
                checker = ConnectionChecker(host, self.station_id)
                ping_result = checker.check_ping(count=3, timeout=2)
                if ping_result.accessible:
                    connection_data["router_ping"] = {
                        "status": "ok",
                        "host": host,
                        "accessible": True,
                        "response_time_ms": ping_result.response_time_ms,
                        "packet_loss": ping_result.details.get("packet_loss", 0)
                        if ping_result.details
                        else 0,
                    }
                    self.logger.debug(
                        f"Router ping successful: {host} ({ping_result.response_time_ms:.1f}ms)"
                    )
                else:
                    connection_data["router_ping"] = {
                        "status": "failed",
                        "host": host,
                        "accessible": False,
                        "error": ping_result.error_message or "ping failed",
                    }
                    self.logger.debug(
                        f"Router ping failed: {host} - {ping_result.error_message}"
                    )
            except Exception as e:
                self.logger.debug(f"Ping check failed: {e}")
                connection_data["router_ping"] = {
                    "status": "error",
                    "host": host,
                    "accessible": False,
                    "error": str(e),
                }

            # Step 1b: Check port status (only if ping succeeded — no point
            # probing ports on an unreachable host, and the timeouts are expensive)
            ping_ok = connection_data.get("router_ping", {}).get("accessible", False)
            if not ping_ok:
                self.logger.debug(f"Ping failed for {host} — skipping port checks")
                connection_data["tcp"] = {
                    "status": "failed",
                    "host": host,
                    "error": "ping failed - host unreachable",
                }

            if ping_ok:
                try:
                    extractor = PolaRX5TCPExtractor(
                        host,
                        self.station_id,
                        port=control_port,
                        port_config=port_config,
                    )
                    port_status = extractor._check_port_status()

                    # Determine TCP status based on whether any port is reachable
                    ftp_open = port_status.get("ftp", {}).get("open", False)
                    http_open = port_status.get("http", {}).get("open", False)
                    control_open = port_status.get("control", {}).get("open", False)

                    if ftp_open or http_open or control_open:
                        connection_data["tcp"] = {"status": "ok", "host": host}
                    else:
                        connection_data["tcp"] = {
                            "status": "failed",
                            "host": host,
                            "error": "all ports unreachable",
                        }
                except Exception as e:
                    self.logger.debug(f"Port check failed: {e}")
                    connection_data["tcp"] = {
                        "status": "failed",
                        "host": host,
                        "error": str(e),
                    }

                # Step 1c: Try full data extraction via TCP control port.
                # Always attempt extraction regardless of port check result —
                # on lossy 3G/4G links the socket check can return "refused"
                # or "timeout" spuriously while the actual TCP session works.
                if port_status and extractor:
                    control_ok = port_status.get("control", {}).get("open", False)
                    try:
                        live_data = extractor.extract_health_data()

                        if live_data.get("metrics"):
                            metrics = live_data["metrics"]
                            # Self-correct: TCP extraction succeeded, so control port
                            # is definitely open (fixes false refused/timeout on lossy links)
                            ctrl_entry = port_status.get("control", {})
                            if ctrl_entry and not ctrl_entry.get("open"):
                                self.logger.info(
                                    f"Self-correcting control port status for {host}: "
                                    f"{ctrl_entry.get('detail')} -> open (TCP extraction succeeded)"
                                )
                                port_status["control"] = {
                                    "port": ctrl_entry.get(
                                        "port", port_config.get("control")
                                    ),
                                    "open": True,
                                    "status": "ok",
                                    "detail": "open",
                                }
                            # Merge port status into metrics
                            if "ports" not in metrics:
                                metrics["ports"] = port_status
                            data_quality = live_data.get("data_quality")
                            extraction_source = "tcp_live"
                            self.logger.info(
                                f"Extracted live health data via TCP from {host}"
                            )

                        # Capture receiver identity if available
                        if live_data.get("receiver_identity"):
                            receiver_identity = live_data["receiver_identity"]
                    except Exception as e:
                        self.logger.debug(f"TCP data extraction failed: {e}")

                    # If no metrics yet but we have port status, use that
                    if not metrics and port_status:
                        metrics = {"ports": port_status}
                        extraction_source = "port_check_only"
                        if not control_ok:
                            self.logger.warning(
                                f"Control port {control_port} not responding on {host} - "
                                f"cannot extract live data"
                            )

        # Step 2: Fall back to SBF file extraction if TCP failed
        if not metrics:
            self.logger.debug("Falling back to SBF file extraction")
            sbf_result = self._get_health_from_sbf_files()
            if sbf_result:
                metrics = sbf_result.get("metrics")
                data_quality = sbf_result.get("data_quality")
                extraction_source = "sbf_file"
                # Merge port_status if we have it from earlier check
                if port_status and metrics and "ports" not in metrics:
                    metrics["ports"] = port_status

        # If we still have no metrics but have port_status, create minimal metrics
        if not metrics and port_status:
            metrics = {"ports": port_status}
            extraction_source = "port_check_only"

        # Step 3: Build standardized health status structure
        health_status = self.build_health_status(
            connection_data=connection_data,
            metrics=metrics,
            data_quality=data_quality,
            receiver_specific=receiver_identity,
        )

        # Also store identity at top level for DB writer
        if receiver_identity:
            health_status["receiver_identity"] = receiver_identity

        # Add extraction source info
        if extraction_source:
            health_status["extraction_metadata"] = {
                "extraction_time": health_status.get("timestamp"),
                "data_source": extraction_source,
                "tool_version": "0.2.0",
            }

        # Step 4: Override overall status if all service ports are closed
        # This indicates the receiver is offline/unreachable even if host pings
        if port_status:
            ftp_open = port_status.get("ftp", {}).get("open", False)
            http_open = port_status.get("http", {}).get("open", False)
            control_open = port_status.get("control", {}).get("open", False)

            if not ftp_open and not http_open and not control_open:
                # All service ports closed - station is effectively offline
                health_status["overall_status"] = "critical"
                health_status["status_details"] = (
                    "all ports closed - receiver unreachable"
                )
                # Update status summary
                if "status_summary" in health_status:
                    health_status["status_summary"]["critical"] = (
                        health_status["status_summary"].get("critical", 0) + 1
                    )

        return health_status

    def _get_health_from_sbf_files(self) -> Optional[Dict[str, Any]]:
        """Extract health data from local SBF files.

        Returns:
            Dictionary with health data or None if not available
        """
        from ..health import RxToolsExtractor, RxToolsNotFoundError

        try:
            status_file = self._find_latest_status_file()

            if status_file:
                extractor = RxToolsExtractor(station_id=self.station_id)

                if extractor.check_rxtools_available():
                    health_data = extractor.extract_health_from_sbf(status_file)
                    self.logger.info(f"Extracted health data from {status_file}")
                    return health_data
                else:
                    self.logger.warning("RxTools not available for SBF extraction")
            else:
                self.logger.warning(
                    f"No status_1hr SBF file found for {self.station_id}"
                )

        except RxToolsNotFoundError as e:
            self.logger.warning(f"RxTools not available: {e}")
        except Exception as e:
            self.logger.error(f"Error extracting from SBF: {e}")

        return None

    def _find_latest_status_file(self) -> Optional[Path]:
        """Find the latest status_1hr SBF file for this station.

        Returns:
            Path to latest SBF file or None if not found
        """
        from datetime import datetime
        from pathlib import Path

        # Build status_1hr path using configuration
        year = datetime.now(UTC).year
        month = datetime.now(UTC).strftime("%b").lower()

        status_dir = (
            Path(self.data_prepath)
            / str(year)
            / month
            / self.station_id
            / "status_1hr"
            / "raw"
        )

        if not status_dir.exists():
            return None

        # Find most recent SBF file
        sbf_files = sorted(status_dir.glob("*.sbf"), reverse=True)
        if not sbf_files:
            sbf_files = sorted(status_dir.glob("*.sbf.gz"), reverse=True)

        return sbf_files[0] if sbf_files else None

    def _record_performance_metrics(self, performance_metrics: Dict[str, Any]) -> None:
        """
        Record performance metrics using gps_parser adaptive learning system.

        Args:
            performance_metrics: Dictionary with performance data
        """
        try:
            # Try to load gps_parser and record performance data
            import sys

            sys.path.append("../gps_parser/src")
            import gps_parser

            # NOTE: record_performance_data() method not yet implemented in gps_parser.ConfigParser
            # This is reserved for future integration with adaptive timeout system
            # For now, performance metrics are logged locally in receivers package
            # parser = gps_parser.ConfigParser()
            # parser.record_performance_data(self.station_id, performance_metrics)

            self.logger.debug(
                f"Performance metrics for {self.station_id}: {performance_metrics}"
            )

        except Exception as e:
            self.logger.debug(f"Could not record performance metrics: {e}")
            # Fail silently - don't break downloads if performance tracking fails

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station configuration
        """
        return {
            "station_id": self.station_id,
            "receiver_type": self.get_receiver_type(),
            "ip": self.ip_number,
            "port": self.ip_port,
            "pasv_mode": self.pasv,
            "configuration": self.station_info,
        }

    @staticmethod
    def is_gz_file(filepath: Union[str, Path]) -> bool:
        """Check if a file is gzipped.

        Args:
            filepath: Path to file to check

        Returns:
            True if file is gzipped, False otherwise
        """
        try:
            with open(filepath, "rb") as f:
                return binascii.hexlify(f.read(2)) == b"1f8b"
        except OSError:
            return False
