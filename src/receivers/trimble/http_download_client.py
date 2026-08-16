"""HTTP download client for Trimble NetR9 receivers.

This module provides HTTP-based file download functionality for NetR9 receivers,
replacing FTP-based downloads with the receiver's HTTP API endpoints.

Supports both NetR9 and NetR5 receivers with automatic detection of URL structure:
- NetR9: /Internal/path/file.T02
- NetR5: /CACHEDIR*/download/Internal/path/file.T02
"""

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

import requests

from ..utils.file_validator import FileValidator
from ..utils.yield_guard import build_yield_guard
from .http_client import TrimbleHTTPClient


def classify_download_exception(exc: BaseException) -> str:
    """Map a download-time exception to a `file_outcomes` value.

    Returns ``"not_found"`` only when the receiver explicitly responded
    HTTP 404 — the analogue of FTP "550 / file not found". Every other
    exception (timeout, connection refused, 5xx, other 4xx, …) is
    classified as ``"transport_error"``: the file's state on the
    receiver is unknown and must NOT be promoted to a `file_tracking`
    `status='missing'` row.
    """
    http_status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError) and http_status == 404:
        return "not_found"
    return "transport_error"


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
        progress_line = (
            f"\r{self.filename}: {bar} "
            f"{progress * 100:.1f}% "
            f"({current_mb:.1f}/{total_mb:.1f}MB) "
            f"@ {speed_kbps:.1f} KB/s{eta_str}"
        )

        # Write to stderr to avoid interfering with logging
        sys.stderr.write(progress_line)
        sys.stderr.flush()

    def finish(self) -> None:
        """Complete the progress bar and add newline."""
        self._display_progress()
        sys.stderr.write("\n")
        sys.stderr.flush()


class NetR9HTTPDownloader:
    """HTTP-based file downloader for NetR9 receivers.

    Uses the NetR9's HTTP API for directory listing and file downloads,
    implementing the same pattern as the legacy rek_scripts.
    """

    def __init__(self, station_id: str, station_config: Dict[str, Any]):
        """Initialize HTTP downloader with station configuration.

        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
        """
        self.station_id = station_id.upper()

        self.logger = logging.getLogger(f"{__name__}.{self.station_id}")

        # Initialize HTTP client
        self.http_client = TrimbleHTTPClient(station_id, station_config)

        # Initialize file validator for resume capability
        self.file_validator = FileValidator(self.logger)

        # Get timeout settings from configuration
        from ..config.receivers_config import get_receivers_config
        from ..utils.stall_timeout import get_stall_timeout

        receivers_config = get_receivers_config()
        netr9_config = receivers_config.get_receiver_config("netr9")
        # Increased defaults for slow/remote connections
        self.connect_timeout = netr9_config.get("http_timeout_connect", 60)
        cfg_stall = netr9_config.get("http_stall_timeout", 180)
        self.stall_timeout = get_stall_timeout(station_id, "netr9", default=cfg_stall)

        # Track connection time for metrics
        self._last_connection_time = 0.0

        # Track remote file sizes from HTTP directory listing (filename -> size in bytes)
        self.remote_sizes: Dict[str, int] = {}

        # Last per-file error during a download_files() call. Read by the
        # NetR9/NetRS wrapper so "All file downloads failed" surfaces the
        # actual cause (404, broken pipe, connection refused, ...).
        self.last_file_error: Optional[str] = None

        # Per-file outcome of the most recent download_files() call. Read by
        # the receiver wrapper to decide whether to write file_tracking
        # status='missing'. Only "not_found" is a verified-absent signal;
        # everything else (transport_error, downloaded) means we should NOT
        # mark the file missing on the receiver — connection failures must
        # not be promoted to "operator-known-bad" state.
        self.file_outcomes: Dict[str, str] = {}  # filename -> outcome

        # CACHEDIR download-prefix handling for NetR5 (hybrid approach). This is
        # the on-receiver /CACHEDIR<n>/download prefix, NOT the storage root
        # (/Internal/ vs /External/) — that is `base_path`, consumed by the NetR9
        # path builder. Keyed as `cachedir_prefix` so the two never collide.
        # Normally auto-discovered; an explicit value is the rare escape hatch.
        receiver_config = station_config.get("receiver", {})
        explicit_cachedir_prefix = self._explicit_cachedir_prefix(receiver_config)

        if explicit_cachedir_prefix:
            # Explicit configuration - use directly
            self.base_path = explicit_cachedir_prefix
            self._base_path_discovered = True
            self.logger.info(
                f"Using explicit CACHEDIR prefix from config: {self.base_path}"
            )
        else:
            # Auto-discovery mode - will discover on first request
            self.base_path = None
            self._base_path_discovered = False
            self.logger.debug(
                "CACHEDIR prefix will be auto-discovered on first request"
            )

        self.logger.info(f"Initialized NetR9 HTTP downloader for {self.station_id}")

    @staticmethod
    def _explicit_cachedir_prefix(receiver_config: Dict[str, Any]) -> Optional[str]:
        """Explicit NetR5 CACHEDIR download-prefix override, if configured.

        Read from ``cachedir_prefix`` ONLY — never ``base_path``, which is the
        storage root consumed by the path builder. Keeping them separate is what
        prevents a station's ``/External/`` storage root from hijacking the
        CACHEDIR slot. None ⇒ auto-discover the prefix on first request.
        """
        return receiver_config.get("cachedir_prefix") or None

    def _discover_base_path(self) -> str:
        """Auto-discover base path for NetR5 CACHEDIR prefix.

        NetR9 receivers use standard /Internal/ paths, but NetR5 receivers
        with downgraded firmware use /CACHEDIR*/download/ prefix.

        This method attempts to detect which structure is in use:
        1. Try standard /Internal/ path with directory listing
        2. If that fails or returns error, fetch root page and parse for CACHEDIR links
        3. Cache discovered path for subsequent requests

        Returns:
            Base path prefix (empty string for NetR9, CACHEDIR path for NetR5)
        """
        if self._base_path_discovered:
            return self.base_path or ""

        self.logger.debug("Auto-discovering base path for receiver...")

        # Test 1: Try standard NetR9 path
        test_path = "/Internal/"
        endpoint = f"/prog/show?directory&path={quote(test_path)}"

        success, response, error = self.http_client.get_url(endpoint)

        # Check if NetR9 directory listing actually works
        # NetR5 returns HTTP 200 but with "ERROR: Unknown Command" in the body
        if success and response and "ERROR" not in response.upper():
            # Standard NetR9 path works - no prefix needed
            self.base_path = ""
            self._base_path_discovered = True
            self.logger.info("✅ Detected NetR9 receiver (standard /Internal/ paths)")
            return ""

        # Test 2: NetR9 path failed - likely NetR5 with CACHEDIR prefix
        self.logger.debug(
            "Standard /Internal/ path failed - checking for NetR5 CACHEDIR prefix..."
        )

        # Fetch root page to find CACHEDIR links
        success, response, error = self.http_client.get_url("/")

        if not success or not response:
            self.logger.warning(f"Failed to discover base path: {error}")
            # Fall back to no prefix
            self.base_path = ""
            self._base_path_discovered = True
            return ""

        # Parse HTML for CACHEDIR links
        # Look for patterns like: href="CACHEDIR656804383/..." (relative paths without leading slash)
        # NetR5 download URLs use: /CACHEDIR{number}/download/Internal/...
        cachedir_pattern = r"CACHEDIR\d+"
        match = re.search(cachedir_pattern, response)

        if match:
            cachedir_name = match.group(0)  # e.g., "CACHEDIR656804383"
            # Build complete base path with /download suffix
            discovered_path = f"/{cachedir_name}/download"
            self.base_path = discovered_path
            self._base_path_discovered = True
            self.logger.info(
                f"✅ Detected NetR5 receiver with CACHEDIR prefix: {discovered_path}"
            )
            return discovered_path
        else:
            # No CACHEDIR found - fall back to standard path
            self.logger.warning(
                "Could not detect CACHEDIR prefix - using standard paths"
            )
            self.base_path = ""
            self._base_path_discovered = True
            return ""

    def get_directory_listing(self, remote_path: str) -> List[Tuple[str, int]]:
        """Get directory listing from NetR9/NetR5 receiver.

        Args:
            remote_path: Remote directory path (e.g., '/Internal/202509/15s_24h')

        Returns:
            List of (filename, filesize) tuples
        """
        # Discover base path if not yet done (for file downloads)
        self._discover_base_path()

        # IMPORTANT: Directory listings do NOT use CACHEDIR prefix (NetR5 firmware limitation)
        # The /prog/show?directory endpoint only works with standard /Internal/ paths
        # CACHEDIR prefix is only used for file downloads
        endpoint = f"/prog/show?directory&path={quote(remote_path)}"

        self.logger.debug(f"Getting directory listing for {remote_path}")
        success, response, error = self.http_client.get_url(endpoint)

        if not success or not response:
            self.logger.error(f"Failed to get directory listing: {error}")
            return []

        # Parse the directory response (based on actual NetR9 response format)
        files = []
        try:
            for line in response.split("\n"):
                line = line.strip()
                if "name=" in line and "size=" in line and self.station_id in line:
                    # NetR9 format: name=SJUK202509010000a.T02 size=2982914  ctime=1440720000 attr=0000818
                    # Extract filename (after 'name=')
                    name_start = line.find("name=") + 5
                    name_end = line.find(" ", name_start)
                    filename = line[name_start:name_end]

                    # Extract size (after 'size=')
                    size_start = line.find("size=") + 5
                    size_end = line.find(" ", size_start)
                    filesize = int(line[size_start:size_end])

                    # Filter out .T0B files (as in rek_scripts)
                    if not filename.endswith(".T0B"):
                        files.append((filename, filesize))

        except (ValueError, IndexError) as e:
            self.logger.warning(f"Error parsing directory listing: {e}")

        self.logger.debug(f"Found {len(files)} files in {remote_path}")
        return files

    def download_file(
        self,
        remote_path: str,
        filename: str,
        local_path: Path,
        expected_size: Optional[int] = None,
        max_retries: int = 3,
        session_type: str = "unknown",
    ) -> bool:
        """Download a single file from NetR9/NetR5 receiver with retry and reconnection.

        Args:
            remote_path: Remote directory path
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

        # Discover base path if not yet done
        base_path = self._discover_base_path()

        # Build full path with base_path prefix for NetR5 support
        full_remote_path = f"{remote_path.rstrip('/')}/{filename}"
        if base_path:
            # NetR5 with CACHEDIR: use base_path directly (already includes /download)
            endpoint = f"{base_path}{full_remote_path}"
        else:
            # NetR9: use standard /download prefix
            endpoint = f"/download{full_remote_path}"

        # Check if we should resume download
        should_resume, resume_offset = self.file_validator.should_resume_download(
            str(local_path), expected_size
        )

        # Ensure local_path is a Path object
        if isinstance(local_path, str):
            local_path = Path(local_path)

        if should_resume:
            self.logger.info(f"Resuming download from byte {resume_offset}: {filename}")
            # NetR9 HTTP API doesn't support range requests, so we can't resume
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
                self.logger.info(f"Downloading {filename} from {remote_path}")
            else:
                self.logger.info(
                    f"Downloading {filename} (attempt {attempt + 1}/{max_retries + 1})"
                )

            try:
                full_url = f"http://{self.http_client.ip}:{self.http_client.http_port}{endpoint}"
                self.logger.debug(f"Downloading from: {full_url}")

                # Use progress-based timeout: only timeout if no data received for stall_timeout seconds
                # Use authenticated session with HTTP Basic Auth support
                response = self.http_client.session.get(
                    full_url,
                    auth=self.http_client.auth,  # Include authentication credentials
                    stream=True,
                    timeout=(self.connect_timeout, self.stall_timeout),
                )
                response.raise_for_status()

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

                # Validate downloaded file and log detailed size comparison (like PolaRX5)
                download_time = time.time() - start_time
                local_file_size = local_path.stat().st_size  # Trust disk, not memory

                if expected_size is not None:
                    # Log file size comparison (matching PolaRX5 pattern)
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

                        # Validate file integrity (matching PolaRX5 pattern)
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
                            _err_msg = f"Validation failed: {validation.get('error', 'unknown')}"
                            self.last_file_error = _err_msg
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
                                message=_err_msg,
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
                        self.file_outcomes[filename] = "downloaded"
                        return True
                    else:
                        self.logger.error(
                            f"❌ Download incomplete for {filename}: size mismatch of {size_diff} bytes"
                        )
                        self.logger.error(
                            f"   Expected: {expected_size:,} bytes, Got: {local_file_size:,} bytes"
                        )
                        # NetR9 HTTP doesn't support Range requests; the next
                        # attempt's should_resume_download() will detect the
                        # partial and delete it for a fresh re-download.
                        self.logger.info(
                            f"   Partial kept on disk; next retry will start fresh: {local_path}"
                        )
                        _err_msg = f"Size mismatch: got {local_file_size}, expected {expected_size}"
                        self.last_file_error = _err_msg
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
                            message=_err_msg,
                        )
                        # Size mismatch means the file IS on the receiver but
                        # the transfer was incomplete — never a "not_found".
                        self.file_outcomes[filename] = "transport_error"
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
                        self.file_outcomes[filename] = "downloaded"
                        return True
                    else:
                        self.logger.error(
                            f"❌ Download validation failed for {filename}: {validation['error']}"
                        )
                        _err_msg = (
                            f"Validation failed: {validation.get('error', 'unknown')}"
                        )
                        self.last_file_error = _err_msg
                        record_download(
                            self.station_id,
                            session_type,
                            "failed",
                            filename=filename,
                            duration_seconds=download_time,
                            bytes_downloaded=local_file_size,
                            stall_timeout_used=self.stall_timeout,
                            attempt=attempt + 1,
                            message=_err_msg,
                        )
                        try:
                            local_path.unlink()
                        except OSError:
                            pass
                        # Bytes arrived but content was invalid — file IS on
                        # receiver, transfer is what failed.
                        self.file_outcomes[filename] = "transport_error"
                        return False

            except (TimeoutError, requests.exceptions.RequestException, Exception) as e:
                error_msg = str(e).lower()
                duration = time.time() - start_time
                is_stall = isinstance(e, TimeoutError) or "stall" in error_msg
                outcome = "stall_timeout" if is_stall else "failed"

                # Classify for file_outcomes: only HTTP 404 counts as
                # verified-absent. Everything else is "unknown receiver
                # state" — see classify_download_exception() for details.
                outcome_class = classify_download_exception(e)
                self.file_outcomes[filename] = outcome_class
                is_not_found = outcome_class == "not_found"

                _err_msg = str(e)[:500]
                self.last_file_error = _err_msg
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
                    message=_err_msg,
                )

                # 404 is authoritative — receiver explicitly said the file
                # isn't on disk. Don't burn additional attempts re-asking.
                # (Mirrors PolaRX5's behavior on FTP "550 / not found".)
                if is_not_found:
                    self.logger.info(
                        f"📄 Receiver reports {filename} not present (HTTP 404)"
                    )
                    return False

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
                        # Reinitialize the HTTP client to get fresh session
                        from .http_client import TrimbleHTTPClient

                        self.http_client = TrimbleHTTPClient(
                            self.station_id,
                            self.ip,
                            self.http_port,
                            self.username,
                            self.password,
                            self.logger,
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
        """Download multiple files from NetR9 receiver.

        Args:
            files_dict: Dictionary mapping filename -> remote_directory
            tmp_dir: Temporary download directory
            clean_tmp: Whether to clean temporary directory first
            archive_files_dict: Optional dict mapping filename -> archive_path (for immediate archiving)
            use_phase1_utilities: Whether to use Phase 1 FileArchiver for immediate archiving

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

        # Reset before each batch so the wrapper surfaces this run's last error.
        self.last_file_error = None
        self.file_outcomes = {}

        downloaded_files = []
        total_files = len(files_dict)

        # Log station connection details (matching PolaRX5 pattern)
        self.logger.info(
            f"Station connection: {self.http_client.ip}:{self.http_client.http_port}"
        )

        # Track unique paths to log each only once (matching PolaRX5 pattern)
        logged_paths = set()

        # Log remote paths like PolaRX5 does
        for filename, remote_dir in sorted(files_dict.items(), reverse=True):
            # Log remote directory path only once per unique path
            if remote_dir not in logged_paths:
                self.logger.info(f"Remote path: {remote_dir}")
                logged_paths.add(remote_dir)

        for filename, remote_dir in sorted(files_dict.items(), reverse=True):
            # Individual file download logging (matching PolaRX5 pattern)
            self.logger.info(f"Downloading {filename}")

            # Get directory listing to find file size
            dir_files = self.get_directory_listing(remote_dir)
            file_info = next((f for f in dir_files if f[0] == filename), None)
            expected_size = file_info[1] if file_info else None

            # Store remote file size for tracking
            if expected_size and expected_size > 0:
                self.remote_sizes[filename] = expected_size

            local_file_path = tmp_dir / filename

            # Check if file already exists and is complete
            if local_file_path.exists() and expected_size:
                validation = self.file_validator.validate_file(
                    str(local_file_path), expected_size
                )
                if validation["valid"]:
                    self.logger.info(
                        f"📁 Local file already exists and is valid ({expected_size:,} bytes): {filename}"
                    )
                    downloaded_files.append(str(local_file_path))
                    continue

            # Download the file
            success = self.download_file(
                remote_dir,
                filename,
                local_file_path,
                expected_size,
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
        """Test HTTP connection to NetR9/NetR5 receiver.

        Returns:
            Dictionary with connection test results
        """
        self.logger.info("Testing HTTP connection...")
        start_time = time.time()

        # Test basic connectivity
        basic_test = self.http_client.test_connection()

        if basic_test["success"]:
            # Discover base path (will be cached for future use)
            base_path = self._discover_base_path()

            # Test specific NetR9/NetR5 endpoints
            # Build Internal path with discovered base_path
            internal_path = f"{base_path}/Internal/" if base_path else "/Internal/"

            endpoints_to_test = [
                "/prog/show?temperature",
                "/prog/show?voltages",
                f"/prog/show?directory&path={quote(internal_path)}",
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
            # Include discovered base path in results
            result["base_path"] = self.base_path

        return result

    def close(self):
        """Close HTTP connections."""
        if hasattr(self, "http_client"):
            self.http_client.close()

    def __del__(self):
        """Clean up resources."""
        self.close()
