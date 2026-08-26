"""Sync task implementation for remote file synchronization.

.. deprecated::
    DORMANT — not wired into the live pipeline (no scheduler.yaml sync section,
    no enqueue site) and its ``DEFAULT_PATH`` is stale (``/data/gps/archive`` —
    the real archive dest is ``gpsops@rawdata:~/gpsdata``). The archive feed is a
    *host-level batch delta sweep*, implemented in ``receivers.archive`` /
    ``receivers archive-sync`` (design 1781867391), NOT this per-station
    pipeline-triggered task. Do not enable this without reconciling against that
    engine — two rawdata-push paths would race on the same immutability rules.

SyncTask uses rsync to transfer files to permanent storage with
immutability rules to prevent accidental data loss.

Key characteristics:
- Uses rsync for efficient incremental transfers
- Raw files: --ignore-existing (NEVER overwrite)
- RINEX files: --update (only if newer)
- Network-bound operation using network resource pool
- Runs after download or RINEX conversion in pipeline
"""

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..task_interface import (
    ScheduledTask,
    TaskConfig,
    TaskPriority,
    TaskResult,
)


# ---------------------------------------------------------------------------
# ⚠️  NOT ON THE LIVE EXECUTION PATH (verified 2026-08-26)
#
# This class is written and registered with TaskFactory, but nothing in
# production instantiates it — TaskFactory.create() has no production call
# site. The live scheduler wires APScheduler directly to the module-level
# `_*_job` functions in bulk_scheduler.py. Of the five task classes only
# RINEXTask actually runs (bulk_scheduler.py:1147,1180).
#
# Treat edits here as edits to code that does not execute. A behaviour fixed
# only here will not reach production: StatusTask's offline-sample-on-failure
# path had to be ported by hand into _status_check_job (see the comment at
# bulk_scheduler.py:1450). If you need this behaviour live, change the job —
# or adopt the interface for the jobs in the same change.
#
# Background: docs/architecture-review-2026-08-26.md §4.6
# ---------------------------------------------------------------------------
class SyncTask(ScheduledTask):
    """Rsync files to permanent storage with immutability rules.

    Implements the raw immutability principle:
    - Raw files are NEVER overwritten (--ignore-existing)
    - RINEX files can be updated if regenerated (--update)
    - Orphan RINEX files (no raw source) are treated as immutable

    Typically runs at STANDARD priority after download/RINEX conversion.
    """

    # Default remote configuration (overridable via env vars)
    DEFAULT_HOST = os.getenv("SYNC_REMOTE_HOST", "gpsops@rawdata.vedur.is")
    DEFAULT_PATH = os.getenv("SYNC_REMOTE_PATH", "/data/gps/archive")

    # Default to STANDARD priority
    default_priority = TaskPriority.STANDARD

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None,
        input_files: Optional[List[str]] = None,
        sync_type: str = "raw",
        remote_host: Optional[str] = None,
        remote_path: Optional[str] = None,
        dry_run: bool = False,
    ):
        """Initialize sync task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
            input_files: List of file paths to sync
            sync_type: Type of sync ('raw' or 'rinex')
            remote_host: Remote host (user@host format)
            remote_path: Base path on remote server
            dry_run: Perform dry run without actual transfer
        """
        super().__init__(station_id, config, logger)
        self.input_files = input_files or []
        self.sync_type = sync_type
        self.remote_host = remote_host or self.DEFAULT_HOST
        self.remote_path = remote_path or self.DEFAULT_PATH
        self.dry_run = dry_run
        self._station_config: Optional[Dict[str, Any]] = None

    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Get time parameters for sync operation.

        Uses the same logic as download tasks.

        Returns:
            Tuple of (start_time, end_time)
        """
        from ...utils.time_utils import calculate_download_time_range

        lookback_periods = getattr(self.config, "lookback_periods", 1)
        return calculate_download_time_range(
            session_type=self.config.session_type, lookback_periods=lookback_periods
        )

    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that sync can be performed.

        Checks:
        - rsync is available
        - Input files exist (if provided)
        - SSH connectivity (basic check)

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            # Check rsync is available
            import shutil

            if not shutil.which("rsync"):
                return False, "rsync command not found"

            # Validate input files if provided
            for file_path in self.input_files:
                if not Path(file_path).exists():
                    return False, f"Input file not found: {file_path}"

            # Load station config
            from ...cli.main import get_station_config

            self._station_config = get_station_config(self.station_id)
            if not self._station_config:
                self.logger.warning(
                    f"No station config for {self.station_id}, using defaults"
                )

            return True, None

        except Exception as e:
            return False, f"Validation failed: {str(e)}"

    def execute(self) -> TaskResult:
        """Execute the sync operation.

        Runs rsync with appropriate options for the sync type.

        Returns:
            TaskResult with sync details
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting sync: {self.station_id} ({self.sync_type})")

            # Validate prerequisites
            valid, error = self.validate_prerequisites()
            if not valid:
                return self._create_failure_result(
                    start_time,
                    "validation_failed",
                    f"Validation failed: {error}",
                    f"ValidationError: {error}",
                )

            # Find files if not provided
            if not self.input_files:
                self.input_files = self._find_files_to_sync()

            if not self.input_files:
                return TaskResult(
                    success=True,
                    status="no_files",
                    duration=time.time() - start_time,
                    message="No files found to sync",
                    data={"station_id": self.station_id, "files_synced": 0},
                )

            # Build rsync command
            cmd = self._build_rsync_command()

            # Run rsync
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
            )

            duration = time.time() - start_time

            if result.returncode == 0:
                # Parse rsync output for stats
                files_transferred = self._parse_rsync_output(result.stdout)

                self.logger.info(
                    f"Sync complete: {self.station_id} - {files_transferred} files ({duration:.1f}s)"
                )

                return TaskResult(
                    success=True,
                    status="completed",
                    duration=duration,
                    message=f"Synced {files_transferred} files",
                    data={
                        "station_id": self.station_id,
                        "sync_type": self.sync_type,
                        "files_synced": files_transferred,
                        "input_files": self.input_files,
                        "remote_host": self.remote_host,
                        "dry_run": self.dry_run,
                    },
                    metrics={
                        "transfer_rate": files_transferred / duration
                        if duration > 0
                        else 0,
                    },
                )
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.logger.error(f"Rsync failed: {error_msg}")

                return self._create_failure_result(
                    start_time,
                    "failed",
                    f"Rsync failed with exit code {result.returncode}",
                    error_msg,
                )

        except subprocess.TimeoutExpired:
            return self._create_failure_result(
                start_time,
                "timeout",
                "Rsync timed out after 10 minutes",
                "TimeoutError",
            )
        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"{type(e).__name__}: {str(e)}"
            self.logger.error(f"Sync failed: {self.station_id} - {error_msg}")

            return self._create_failure_result(
                start_time, "error", f"Sync failed: {str(e)}", error_msg
            )

    def _build_rsync_command(self) -> List[str]:
        """Build rsync command with appropriate options.

        Returns:
            List of command arguments
        """
        cmd = ["rsync", "-avz", "--compress"]

        # Add immutability options based on sync type
        if self.sync_type == "raw":
            # Raw files: NEVER overwrite existing
            cmd.append("--ignore-existing")
        elif self.sync_type == "rinex":
            # RINEX files: only update if source is newer
            cmd.append("--update")

        # Add dry run if requested
        if self.dry_run:
            cmd.append("--dry-run")

        # Add verbose for progress tracking
        cmd.append("--stats")

        # Build remote destination
        session = self.config.session_type
        remote_dest = (
            f"{self.remote_host}:{self.remote_path}/{self.station_id}/{session}/"
        )

        # Add source files
        for file_path in self.input_files:
            cmd.append(file_path)

        # Add destination
        cmd.append(remote_dest)

        self.logger.debug(f"Rsync command: {' '.join(cmd)}")
        return cmd

    def _find_files_to_sync(self) -> List[str]:
        """Find files to sync based on time parameters.

        Returns:
            List of file paths
        """
        try:
            start_time, _ = self.get_time_parameters()

            # Look in archive directory
            archive_base = Path.home() / ".cache" / "gps_receivers" / "archive"
            session = self.config.session_type
            station_dir = archive_base / self.station_id / session

            # Try date-based subdirectory
            date_dir = (
                station_dir / start_time.strftime("%Y") / start_time.strftime("%j")
            )

            search_dirs = [date_dir, station_dir]

            # Patterns based on sync type
            if self.sync_type == "raw":
                patterns = ["*.sbf.gz", "*.sbf", "*.T02.gz", "*.T02", "*.T00", "*.m00"]
            else:  # rinex
                patterns = ["*.rnx.gz", "*.rnx", "*.[0-9][0-9]o.gz", "*.[0-9][0-9]d.gz"]

            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue

                files = []
                for pattern in patterns:
                    files.extend(search_dir.glob(pattern))

                if files:
                    return [str(f) for f in sorted(files)]

            return []

        except Exception as e:
            self.logger.warning(f"Could not find files to sync: {e}")
            return []

    def _parse_rsync_output(self, output: str) -> int:
        """Parse rsync output to get transfer count.

        Args:
            output: rsync stdout

        Returns:
            Number of files transferred
        """
        import re

        # Look for "Number of files transferred: X"
        match = re.search(r"Number of.*files transferred:\s*(\d+)", output)
        if match:
            return int(match.group(1))

        # Alternative: count lines that look like transfers
        transfer_lines = [
            line
            for line in output.split("\n")
            if line and not line.startswith(("building", "sending", "total", "sent"))
        ]
        return len(transfer_lines)

    def _create_failure_result(
        self,
        start_time: float,
        status: str,
        message: str,
        error: str,
    ) -> TaskResult:
        """Create a failure TaskResult."""
        return TaskResult(
            success=False,
            status=status,
            duration=time.time() - start_time,
            message=message,
            data={"station_id": self.station_id, "sync_type": self.sync_type},
            error=error,
        )
