"""HTTP download client for Trimble NetRS receivers.

This module provides HTTP-based file download functionality for NetRS receivers,
using the receiver's HTTP API endpoints with the pattern:
http://station.gps.vedur.is:8060/download/YYYYMM/session_letter/filename.T00
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

import requests

from ..utils.file_validator import FileValidator
from ..utils.yield_guard import build_yield_guard
from .http_client import TrimbleHTTPClient


class ProgressBar:
    """Simple ASCII progress bar for file downloads."""

    def __init__(self, total_size: int, filename: str, width: int = 50):
        self.total_size = total_size
        self.filename = filename
        self.width = width
        self.current_size = 0
        self.start_time = time.time()
        self.last_update = 0

    def update(self, bytes_downloaded: int) -> None:
        """Update progress bar with current download progress."""
        self.current_size = bytes_downloaded
        current_time = time.time()

        # Only update display every 0.5 seconds to avoid flickering
        if current_time - self.last_update < 0.5 and bytes_downloaded < self.total_size:
            return

        self.last_update = current_time
        self._display_progress()

    def _display_progress(self) -> None:
        """Display the progress bar."""
        if self.total_size == 0:
            return

        # Calculate progress
        progress = min(self.current_size / self.total_size, 1.0)
        filled_width = int(progress * self.width)

        # Calculate speed and ETA
        elapsed_time = time.time() - self.start_time
        if elapsed_time > 0 and self.current_size > 0:
            speed_kbps = self.current_size / elapsed_time / 1024
            if progress > 0:
                eta_seconds = (elapsed_time / progress) - elapsed_time
                eta_str = (
                    f" ETA: {int(eta_seconds)}s" if eta_seconds > 0 else " ETA: --"
                )
            else:
                eta_str = " ETA: --"
        else:
            speed_kbps = 0
            eta_str = " ETA: --"

        # Build progress bar
        bar = "█" * filled_width + "░" * (self.width - filled_width)

        # Format sizes
        current_mb = self.current_size / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024)

        # Build complete progress line
        # Build complete progress line (keep it concise to avoid terminal wrapping)
        # Truncate filename if too long to prevent line wrapping
        display_filename = (
            self.filename[:15] if len(self.filename) > 15 else self.filename
        )

        progress_line = (
            f"\r{display_filename}: {bar} "
            f"{progress * 100:.0f}% "
            f"({current_mb:.1f}/{total_mb:.1f}MB) "
            f"{speed_kbps:.0f}KB/s{eta_str}"
        )

        # Write to stderr to avoid interfering with logging
        sys.stderr.write(progress_line)
        sys.stderr.flush()

    def finish(self) -> None:
        """Complete the progress bar and add newline."""
        self._display_progress()
        sys.stderr.write("\n")
        sys.stderr.flush()


class NetRSHTTPDownloader:
    """HTTP-based file downloader for NetRS receivers.

    Uses the NetRS's HTTP API for directory listing and file downloads,
    implementing the pattern: /download/YYYYMM/session_letter/filename.T00
    """

    def __init__(self, station_id: str, station_config: Dict[str, Any]):
        """Initialize HTTP downloader with station configuration.

        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
        """
        self.station_id = station_id.upper()
        self._station_config = station_config

        self.logger = logging.getLogger(f"{__name__}.{self.station_id}")

        # Initialize HTTP client
        self.http_client = TrimbleHTTPClient(station_id, station_config)

        # Initialize file validator for resume capability
        self.file_validator = FileValidator(self.logger)

        # Get timeout settings from configuration
        from ..config.receivers_config import get_receivers_config
        from ..utils.stall_timeout import get_stall_timeout

        receivers_config = get_receivers_config()
        netrs_config = receivers_config.get_receiver_config("netrs")
        # Increased defaults for slow/remote connections
        self.connect_timeout = netrs_config.get("http_timeout_connect", 60)
        cfg_stall = netrs_config.get("http_stall_timeout", 180)
        self.stall_timeout = get_stall_timeout(station_id, "netrs", default=cfg_stall)

        # Track connection time for metrics
        self._last_connection_time = 0.0

        # Track remote file sizes from HTTP Content-Length (filename -> size in bytes)
        self.remote_sizes: Dict[str, int] = {}

        self.logger.info(f"Initialized NetRS HTTP downloader for {self.station_id}")

    def download_file(
        self,
        remote_path: str,
        filename: str,
        local_path: Path,
        expected_size: Optional[int] = None,
        max_retries: int = 3,
        session_type: str = "unknown",
    ) -> bool:
        """Download a single file from NetRS receiver with retry and reconnection.

        Args:
            remote_path: Remote directory path (e.g., '/download/202509/a')
            filename: Filename to download
            local_path: Local file path to save to
            expected_size: Expected file size for validation
            max_retries: Maximum number of retry attempts (default: 3)
            session_type: Session type for download logging (e.g. '15s_24hr')

        Returns:
            True if download successful, False otherwise
        """
        from ..utils.stall_timeout import record_download

        # Timeout/connection error patterns that require reconnection
        timeout_patterns = [
            "timed out",
            "timeout",
            "cannot read from timed out",
            "connection reset",
            "broken pipe",
            "connection refused",
        ]

        initial_delay = 0.5

        # NetRS uses direct download path format: /download/YYYYMM/session_letter/filename.T00
        full_url = f"http://{self.http_client.ip}:{self.http_client.http_port}{remote_path}/{filename}"

        # Check if we should resume download
        should_resume, resume_offset = self.file_validator.should_resume_download(
            str(local_path), expected_size
        )

        # Ensure local_path is a Path object
        if isinstance(local_path, str):
            local_path = Path(local_path)

        if should_resume:
            self.logger.info(f"Resuming download from byte {resume_offset}: {filename}")
            # NetRS HTTP API doesn't support range requests, so we can't resume
            # Remove partial file and start fresh
            try:
                local_path.unlink()
                self.logger.info(f"Removed partial file for fresh download: {filename}")
            except OSError as e:
                self.logger.warning(f"Could not remove partial file {local_path}: {e}")

        # Retry loop with reconnection on timeout
        for attempt in range(max_retries + 1):
            # Download the file
            start_time = time.time()
            bytes_written = 0
            if attempt == 0:
                self.logger.info(f"Downloading {filename}")
            else:
                self.logger.info(
                    f"Downloading {filename} (attempt {attempt + 1}/{max_retries + 1})"
                )

            try:
                self.logger.debug(f"HTTP URL: {full_url}")

                # Use session (retry adapter) with auth credentials, matching NetR9 pattern
                response = self.http_client.session.get(
                    full_url,
                    stream=True,
                    timeout=(self.connect_timeout, self.stall_timeout),
                    auth=self.http_client.auth,
                )
                response.raise_for_status()

                # Get expected size from headers if not provided
                if not expected_size:
                    expected_size = int(response.headers.get("content-length", 0))

                # Store remote file size for tracking
                if expected_size and expected_size > 0:
                    self.remote_sizes[filename] = expected_size

                # Write response to file
                local_path.parent.mkdir(parents=True, exist_ok=True)
                last_progress_time = time.time()

                # Initialize progress bar if we have expected size
                progress_bar = None
                if expected_size and expected_size > 0:
                    progress_bar = ProgressBar(expected_size, filename)

                with open(local_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                            current_time = time.time()

                            # Reset progress timer when data is received
                            last_progress_time = current_time

                            # Update progress bar
                            if progress_bar:
                                progress_bar.update(bytes_written)

                        else:
                            # Check for stall timeout (no data received)
                            current_time = time.time()
                            if current_time - last_progress_time > self.stall_timeout:
                                # Clean up progress bar before raising error
                                if progress_bar:
                                    progress_bar.finish()
                                raise TimeoutError(
                                    f"Download stalled: no data received for {self.stall_timeout}s "
                                    f"(downloaded {bytes_written:,} bytes)"
                                )

                    # Ensure all data is flushed to disk before closing
                    f.flush()
                    os.fsync(f.fileno())

                # Complete progress bar
                if progress_bar:
                    progress_bar.finish()

                # Validate downloaded file and log detailed size comparison (like NetR9)
                download_time = time.time() - start_time
                local_file_size = local_path.stat().st_size  # Trust disk, not memory

                if expected_size is not None:
                    # Log file size comparison (matching NetR9 pattern)
                    self.logger.info(
                        f"Remote file size: {expected_size} bytes, Local file size: {local_file_size} bytes"
                    )
                    size_diff = local_file_size - expected_size
                    self.logger.info(
                        f"Difference between remote and downloaded file: {size_diff} bytes"
                    )

                    if size_diff == 0:
                        self.logger.info(
                            f"✅ Successfully downloaded {filename} ({local_file_size:,} bytes)"
                        )

                        # Validate file integrity
                        validation = self.file_validator.validate_file(
                            str(local_path), expected_size
                        )
                        if not validation["valid"]:
                            self.logger.warning(
                                f"Downloaded file failed validation: {validation['error']}"
                            )
                            self.logger.info(
                                f"Removing invalid downloaded file: {local_path}"
                            )
                            record_download(
                                self.station_id,
                                session_type,
                                "failed",
                                filename=filename,
                                duration_seconds=download_time,
                                bytes_downloaded=local_file_size,
                                file_size=expected_size,
                                stall_timeout_used=self.stall_timeout,
                                attempt=attempt + 1,
                                message=f"Validation failed: {validation.get('error', 'unknown')}",
                            )
                            try:
                                local_path.unlink()
                                return False
                            except OSError as e:
                                self.logger.error(
                                    f"Could not remove invalid file {local_path}: {e}"
                                )
                                return False
                        else:
                            self.logger.debug(
                                f"Downloaded file validated: {validation['compression']} compression, {validation['size']} bytes"
                            )

                        record_download(
                            self.station_id,
                            session_type,
                            "completed",
                            filename=filename,
                            duration_seconds=download_time,
                            bytes_downloaded=local_file_size,
                            file_size=expected_size,
                            stall_timeout_used=self.stall_timeout,
                            attempt=attempt + 1,
                        )
                        return True
                    else:
                        self.logger.error(
                            f"❌ Download incomplete for {filename}: size mismatch of {size_diff} bytes"
                        )
                        self.logger.error(
                            f"   Expected: {expected_size:,} bytes, Got: {local_file_size:,} bytes"
                        )
                        # NetRS HTTP doesn't support Range requests; the next
                        # attempt's should_resume_download() will detect the
                        # partial and delete it for a fresh re-download.
                        self.logger.info(
                            f"   Partial kept on disk; next retry will start fresh: {local_path}"
                        )
                        record_download(
                            self.station_id,
                            session_type,
                            "failed",
                            filename=filename,
                            duration_seconds=download_time,
                            bytes_downloaded=local_file_size,
                            file_size=expected_size,
                            stall_timeout_used=self.stall_timeout,
                            attempt=attempt + 1,
                            message=f"Size mismatch: got {local_file_size}, expected {expected_size}",
                        )
                        return False
                else:
                    # No expected size - just validate what we can
                    validation = self.file_validator.validate_file(str(local_path))
                    if validation["valid"]:
                        self.logger.info(
                            f"✅ Downloaded {filename} ({local_file_size:,} bytes) - integrity validated"
                        )
                        record_download(
                            self.station_id,
                            session_type,
                            "completed",
                            filename=filename,
                            duration_seconds=download_time,
                            bytes_downloaded=local_file_size,
                            stall_timeout_used=self.stall_timeout,
                            attempt=attempt + 1,
                        )
                        return True
                    else:
                        self.logger.error(
                            f"❌ Download validation failed for {filename}: {validation['error']}"
                        )
                        record_download(
                            self.station_id,
                            session_type,
                            "failed",
                            filename=filename,
                            duration_seconds=download_time,
                            bytes_downloaded=local_file_size,
                            stall_timeout_used=self.stall_timeout,
                            attempt=attempt + 1,
                            message=f"Validation failed: {validation.get('error', 'unknown')}",
                        )
                        try:
                            local_path.unlink()
                        except OSError:
                            pass
                        return False

            except (TimeoutError, requests.exceptions.RequestException, Exception) as e:
                error_msg = str(e).lower()
                duration = time.time() - start_time
                is_stall = isinstance(e, TimeoutError) or "stall" in error_msg
                outcome = "stall_timeout" if is_stall else "failed"

                record_download(
                    self.station_id,
                    session_type,
                    outcome,
                    filename=filename,
                    duration_seconds=duration,
                    bytes_downloaded=bytes_written,
                    file_size=expected_size,
                    stall_timeout_used=self.stall_timeout,
                    attempt=attempt + 1,
                    message=str(e)[:500],
                )

                # If this was the last attempt, give up
                if attempt >= max_retries:
                    self.logger.error(
                        f"❌ Download failed after {max_retries + 1} attempts: {e}"
                    )
                    return False

                # Log the failure
                self.logger.warning(f"⚠️  Download attempt {attempt + 1} failed: {e}")

                # Check if we need to reconnect (timeout/connection errors)
                if any(pattern in error_msg for pattern in timeout_patterns):
                    self.logger.info("🔄 Reconnecting HTTP client...")
                    try:
                        self.http_client = TrimbleHTTPClient(
                            self.station_id, self._station_config
                        )
                        self.logger.info("✅ HTTP client reconnected")
                    except Exception as reconnect_error:
                        self.logger.error(f"❌ Reconnection failed: {reconnect_error}")
                        # Continue anyway - might recover on next attempt

                # Exponential backoff
                delay = initial_delay * (attempt + 1)
                self.logger.info(f"🔄 Retrying in {delay:.1f}s...")
                time.sleep(delay)

        # All retries exhausted
        self.logger.error(
            f"❌ Download failed for {filename} after {max_retries + 1} attempts"
        )
        return False

    def download_files(
        self,
        files_dict: Dict[str, str],
        tmp_dir: Path,
        clean_tmp: bool = True,
        archive_files_dict: Optional[Dict[str, str]] = None,
        use_phase1_utilities: bool = False,
        session_type: str = "unknown",
        max_retries: int = 3,
    ) -> List[str]:
        """Download multiple files from NetRS receiver.

        Args:
            files_dict: Dictionary mapping filename -> remote_directory
            tmp_dir: Temporary download directory
            clean_tmp: Whether to clean temporary directory first
            archive_files_dict: Optional dict mapping filename -> archive_path (for immediate archiving)
            use_phase1_utilities: Whether to use Phase 1 FileArchiver for immediate archiving
            session_type: Session type for download logging (e.g. '15s_24hr')

        Returns:
            List of successfully downloaded/archived file paths
        """
        if clean_tmp and tmp_dir.exists():
            self.logger.info(f"Cleaning temporary directory: {tmp_dir}")
            files_removed = self.file_validator.clean_directory(str(tmp_dir))
            if files_removed > 0:
                self.logger.info(f"Removed {files_removed} files from tmp directory")

        # Ensure tmp directory exists
        tmp_dir.mkdir(parents=True, exist_ok=True)

        downloaded_files = []
        total_files = len(files_dict)

        # Log station connection details (matching NetR9 pattern)
        self.logger.info(
            f"Station connection: {self.http_client.ip}:{self.http_client.http_port}"
        )

        # Track unique paths to log each only once (matching NetR9 pattern)
        logged_paths = set()

        # Log remote paths like NetR9 does
        for filename, remote_dir in sorted(files_dict.items(), reverse=True):
            # Log remote directory path only once per unique path
            if remote_dir not in logged_paths:
                self.logger.info(f"Remote path: {remote_dir}")
                logged_paths.add(remote_dir)

        for filename, remote_dir in sorted(files_dict.items(), reverse=True):
            # Individual file download logging (matching NetR9 pattern)
            self.logger.info(f"Downloading {filename}")

            local_file_path = tmp_dir / filename

            # Check if file already exists and is complete (simple check for NetRS)
            if local_file_path.exists():
                validation = self.file_validator.validate_file(str(local_file_path))
                if validation["valid"]:
                    file_size = local_file_path.stat().st_size
                    self.logger.info(
                        f"📁 Local file already exists and is valid ({file_size:,} bytes): {filename}"
                    )
                    downloaded_files.append(str(local_file_path))
                    continue

            # Download the file (expected_size unknown for NetRS without directory listing)
            success = self.download_file(
                remote_dir,
                filename,
                local_file_path,
                expected_size=None,
                max_retries=max_retries,
                session_type=session_type,
            )

            if success:
                # Archive immediately after download if enabled
                if (
                    archive_files_dict
                    and filename in archive_files_dict
                    and use_phase1_utilities
                ):
                    archive_path = Path(archive_files_dict[filename])
                    self.logger.info(
                        f"📦 Archiving immediately after download: {filename}"
                    )

                    # Import FileArchiver only when needed
                    from ..utils.file_archiver import ArchiveMode, FileArchiver

                    with FileArchiver(
                        mode=ArchiveMode.IMMEDIATE,
                        logger=self.logger,
                        yield_guard=build_yield_guard(self.logger),
                    ) as archiver:
                        archive_success = archiver.archive_file(
                            local_file_path,
                            archive_path,
                            compress=True,
                            remove_tmp=True,
                        )

                    if archive_success:
                        # Add archive path to downloaded files list
                        downloaded_files.append(str(archive_path))
                    else:
                        self.logger.error(
                            f"❌ Failed to archive {filename} after download"
                        )
                        # Add tmp file path as fallback
                        downloaded_files.append(str(local_file_path))
                else:
                    # No immediate archiving - add tmp file path
                    downloaded_files.append(str(local_file_path))
            else:
                self.logger.error(f"❌ Failed to download {filename}")

        self.logger.info(
            f"Download complete: {len(downloaded_files)}/{total_files} files successful"
        )
        return downloaded_files

    def test_connection(self) -> Dict[str, Any]:
        """Test HTTP connection to NetRS receiver.

        Returns:
            Dictionary with connection test results
        """
        self.logger.info("Testing HTTP connection...")
        start_time = time.time()

        # Test basic connectivity
        basic_test = self.http_client.test_connection()

        if basic_test["success"]:
            # Test specific NetRS endpoints (similar to NetR9)
            endpoints_to_test = [
                "/prog/show?temperature",
                "/prog/show?voltages",
            ]

            endpoint_results = {}
            for endpoint in endpoints_to_test:
                success, response, error = self.http_client.get_url(endpoint)
                endpoint_results[endpoint] = {
                    "success": success,
                    "error": error,
                    "response_size": len(response) if response else 0,
                }

        connection_time = time.time() - start_time
        self._last_connection_time = connection_time

        result = {
            "success": basic_test["success"],
            "duration": connection_time,
            "basic_test": basic_test,
            "error": basic_test.get("error"),
        }

        if basic_test["success"]:
            result["endpoint_tests"] = endpoint_results

        return result

    def close(self):
        """Close HTTP connections."""
        if hasattr(self, "http_client"):
            self.http_client.close()

    def __del__(self):
        """Clean up resources."""
        self.close()
