"""Septentrio PolaRX5 receiver implementation."""

import binascii
import logging
import os
import re
import time
from datetime import datetime, timedelta
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, Optional, Union

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

try:
    import progressbar2 as progressbar
except ImportError:
    progressbar = None

from ..base.exceptions import (
    ConfigurationError,
    ConnectionError,
)
from ..base.receiver import BaseReceiver


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

        # Session mapping for different data types
        self.session_map = {
            "15s_24hr": ("a", "log1_15s_24hr"),
            "1Hz_1hr": ("b", "log2_1hz_1hr"),
            "status_1hr": ("b", "log5_status_1hr"),
        }

    def _get_logger(self, level: int = logging.WARNING) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "[%(levelname)s] %(name)s: %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = False

        return logger

    def _setup_connection_info(self):
        """Extract and validate connection information from station_info."""
        try:
            self.ip_number = self.station_info["router"]["ip"]
            self.ip_port = int(self.station_info["receiver"]["ftpport"])

            # Determine passive mode based on IP pattern
            regexp = re.compile(r"10\.4\.[12]")
            self.pasv = not regexp.search(self.ip_number)

            self.logger.info(f"Station {self.station_id} - IP: {self.ip_number}:{self.ip_port}, PASV: {self.pasv}")

        except KeyError as e:
            raise ConfigurationError(f"Missing configuration key: {e}")
        except ValueError as e:
            raise ConfigurationError(f"Invalid port number: {e}")

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to receiver.

        Returns:
            Dictionary with router and receiver connection status
        """
        try:
            # Simple connection test
            ftp = FTP()
            ftp.connect(self.ip_number, self.ip_port, timeout=10)
            ftp.login("anonymous")
            ftp.set_pasv(self.pasv)
            ftp.quit()

            status = {
                "router": True,
                "receiver": True,
                "ip": self.ip_number,
                "port": self.ip_port,
                "timestamp": datetime.utcnow().isoformat(),
                "error": None
            }

        except Exception as e:
            status = {
                "router": False,
                "receiver": False,
                "ip": self.ip_number,
                "port": self.ip_port,
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }

        self.connection_status = status
        return status

    def download_data(
        self,
        start: Optional[Union[datetime, str]] = None,
        end: Optional[Union[datetime, str]] = None,
        session: str = "15s_24hr",
        ffrequency: str = "1D",
        afrequency: str = "15s",
        clean_tmp: bool = True,
        sync: bool = False,
        compression: str = ".gz",
        archive: bool = True,
        tmp_dir: str = "/home/bgo/tmp/download/",
        predir: str = "/DSK2/SSN/",
        loglevel: int = logging.WARNING,
    ) -> Dict[str, Any]:
        """Download data from PolaRX5 receiver.

        This is the main download function that handles file synchronization
        from the receiver to the local archive.

        Args:
            start: Start time for download period
            end: End time for download period
            session: Data session type
            ffrequency: File frequency (e.g., '1D', '1H')
            afrequency: Acquisition frequency
            clean_tmp: Clean temporary directory before download
            sync: Whether to actually sync files (False for dry run)
            compression: File compression type
            archive: Whether to archive downloaded files
            tmp_dir: Temporary download directory
            predir: Remote directory prefix
            loglevel: Logging level

        Returns:
            Dictionary with download results and file information
        """
        # Set logger level
        self.logger.setLevel(loglevel)

        # Set up directories
        tmp_dir_path = Path(tmp_dir) / self.station_id
        tmp_dir_path.mkdir(parents=True, exist_ok=True)

        # Handle time parameters
        start_time = time.time()
        start, end = self._process_time_parameters(start, end, session, ffrequency)

        self.logger.info(f"Checking {session} sessions from {start} to {end}")

        # Generate file lists
        file_datetime_list = gt.datepathlist(
            "#datelist", ffrequency, starttime=start, endtime=end,
            datelist=[], closed="both"
        )

        # Create archive and remote file paths
        archive_format = f"/data/%Y/#b/{self.station_id}/{session}/raw/{self.station_id}%Y%m%d%H00a.sbf{compression}"
        archive_file_list = gt.datepathlist(
            archive_format, ffrequency, datelist=file_datetime_list, closed="both"
        )

        igs_format = f"{self.station_id}#Rin2_{compression}"
        igs_file_list = gt.datepathlist(
            igs_format, ffrequency, datelist=file_datetime_list, closed="both"
        )

        file_date_dict = dict(zip(file_datetime_list, zip(archive_file_list, igs_file_list)))

        # Find missing files
        missing_file_dict = {
            key: value for (key, value) in file_date_dict.items()
            if not os.path.isfile(value[0])
        }

        if not missing_file_dict:
            self.logger.info("Archive is up to date")
            return {
                "status": "up_to_date",
                "files_checked": len(file_date_dict),
                "files_missing": 0,
                "files_downloaded": 0,
                "duration": time.time() - start_time
            }

        self.logger.info(f"Missing files: {len(missing_file_dict)}")

        downloaded_files_dict = {}
        if sync:
            downloaded_files_dict = self._sync_missing_files(
                missing_file_dict, tmp_dir_path, session, predir,
                ffrequency, clean_tmp, archive
            )

        return {
            "status": "completed" if sync else "dry_run",
            "files_checked": len(file_date_dict),
            "files_missing": len(missing_file_dict),
            "files_downloaded": len(downloaded_files_dict),
            "downloaded_files": list(downloaded_files_dict.values()),
            "duration": time.time() - start_time
        }

    def _process_time_parameters(self, start, end, session, ffrequency):
        """Process and validate time parameters."""
        # Handle hourly vs daily sessions
        hoursession = re.compile(r"1h", re.IGNORECASE)
        is_hourly = hoursession.search(session)

        if ffrequency.lower() == "1h" or is_hourly:
            # Hourly data processing
            if end is None:
                end = datetime.now() - timedelta(hours=1)
            if isinstance(end, str):
                end = datetime.fromisoformat(end)
            end = end.replace(minute=0, second=0, microsecond=0)

            if start is None:
                start = end - timedelta(hours=24)
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            start = start.replace(minute=0, second=0, microsecond=0)
        else:
            # Daily data processing
            if end is None:
                end = currDatetime(-1)
            if isinstance(end, str):
                end = datetime.fromisoformat(end)
            end = end.date()

            if start is None:
                start = end - timedelta(days=10)
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            start = start.date()

        return start, end

    def _sync_missing_files(
        self, missing_file_dict, tmp_dir, session, predir,
        ffrequency, clean_tmp, archive
    ):
        """Sync missing files from receiver to local archive."""
        # Get session info
        if session not in self.session_map:
            raise ConfigurationError(f"Unknown session type: {session}")

        session_info = self.session_map[session][1]
        remote_format = f"{predir}{session_info}/%y%j/"
        remote_path_list = gt.datepathlist(
            remote_format, ffrequency, datelist=list(missing_file_dict.keys()),
            closed="both"
        )

        # Create download dictionary
        download_file_dict = dict(
            zip([path[1] for path in missing_file_dict.values()], remote_path_list)
        )

        # Connect and download
        ftp = self._ftp_open_connection()
        if not ftp:
            raise ConnectionError(f"Could not connect to {self.ip_number}:{self.ip_port}")

        try:
            downloaded_files = self._ftp_download(
                download_file_dict, tmp_dir, clean_tmp=clean_tmp, ftp=ftp
            )

            downloaded_files_dict = dict(zip(missing_file_dict, downloaded_files))

            # Archive files
            if downloaded_files_dict and archive:
                self._archive_files(downloaded_files_dict, missing_file_dict)

            return downloaded_files_dict

        finally:
            ftp.close()

    def _ftp_open_connection(self, timeout: int = 10) -> Optional[FTP]:
        """Open FTP connection to receiver."""
        try:
            self.logger.info("Connecting to receiver...")
            ftp = FTP()
            ftp.connect(self.ip_number, self.ip_port, timeout=timeout)
            ftp.login("anonymous")
            ftp.set_pasv(self.pasv)
            self.logger.info("Connection successful!")
            return ftp
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            return None

    def _ftp_download(self, files_dict, local_dir, clean_tmp=True, ftp=None):
        """Download files via FTP with progress tracking."""
        downloaded_files = []

        for file_name, remote_dir in sorted(files_dict.items(), reverse=True):
            self.logger.info(f"Downloading {file_name}")

            local_file = local_dir / file_name
            if clean_tmp and local_file.exists():
                local_file.unlink()

            remote_file = f"{remote_dir}{file_name}"

            try:
                remote_file_size = ftp.size(remote_file)
                offset = local_file.stat().st_size if local_file.exists() else 0

                diff = self._download_with_progressbar(
                    ftp, remote_file, str(local_file), remote_file_size, offset
                )

                if diff == 0:
                    downloaded_files.append(str(local_file))
                    self.logger.info(f"Successfully downloaded {file_name}")
                else:
                    self.logger.warning(f"Size mismatch for {file_name}: {diff} bytes")

            except Exception as e:
                self.logger.error(f"Failed to download {file_name}: {e}")
                continue

        return downloaded_files

    def _download_with_progressbar(self, ftp, remote_file, local_file,
                                  remote_file_size, offset=0):
        """Download file with progress bar display."""
        if progressbar is None:
            # Fallback without progress bar
            with open(local_file, "ab") as f:
                ftp.retrbinary(f"RETR {remote_file}", f.write, rest=offset)
        else:
            # Use progress bar
            widgets = [
                f"Downloading {Path(remote_file).name}: ",
                progressbar.Percentage(), " ",
                progressbar.Bar(), " ",
                progressbar.ETA(), " ",
                progressbar.FileTransferSpeed(),
            ]

            pbar = progressbar.ProgressBar(
                min_value=offset, max_value=remote_file_size, widgets=widgets
            ).start()

            with open(local_file, "ab") as f:
                def callback(chunk):
                    f.write(chunk)
                    pbar.update(pbar.value + len(chunk))

                ftp.retrbinary(f"RETR {remote_file}", callback, rest=offset)
            pbar.finish()

        local_file_size = os.path.getsize(local_file)
        return local_file_size - remote_file_size

    def _archive_files(self, downloaded_files_dict, missing_file_dict):
        """Move downloaded files to archive locations."""
        for ddate, tmp_file in downloaded_files_dict.items():
            if not os.path.isfile(tmp_file):
                continue

            archive_path, _ = os.path.split(missing_file_dict[ddate][0])
            os.makedirs(archive_path, exist_ok=True)

            destination = missing_file_dict[ddate][0]
            if not os.path.isfile(destination):
                os.rename(tmp_file, destination)
                self.logger.info(f"Archived {os.path.basename(tmp_file)}")

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of PolaRX5 receiver.

        Returns:
            Dictionary with health status information
        """
        health = {
            "station_id": self.station_id,
            "receiver_type": "PolaRX5",
            "timestamp": datetime.utcnow().isoformat(),
            "connection": self.get_connection_status(),
            "data_flow": "N/A",  # TODO: Implement data flow check
            "storage": "N/A",    # TODO: Implement storage check
            "overall_status": "unknown"
        }

        # Determine overall status
        if health["connection"]["receiver"]:
            health["overall_status"] = "healthy"
        else:
            health["overall_status"] = "unhealthy"

        return health

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station configuration
        """
        return {
            "station_id": self.station_id,
            "receiver_type": "PolaRX5",
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
