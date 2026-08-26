"""Download task implementation for scheduler.

Downloads data from GPS receivers as a scheduled task.
Wraps the existing receiver download functionality with the ScheduledTask interface.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from ...utils.time_utils import calculate_download_time_range
from ..task_interface import ScheduledTask, TaskConfig, TaskResult, TaskType


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
class DownloadTask(ScheduledTask):
    """Scheduled download task for GPS receiver data.

    Downloads data from a receiver for a specific session type.
    Handles time parameter calculation, prerequisite validation,
    and execution with proper error handling.
    """

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize download task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
        """
        super().__init__(station_id, config, logger)
        self._receiver = None
        self._station_config = None

    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Calculate download time range based on session type and lookback_periods.

        Uses shared time_utils module for consistent time calculation between
        CLI and scheduler. Implements correct "previous complete period" logic.

        For hourly sessions (1Hz_1hr, status_1hr):
            Downloads last N hours based on lookback_periods
            Example: lookback_periods=24 downloads last 24 hours
            IMPORTANT: Ends at previous complete hour (not current incomplete hour)

        For daily sessions (15s_24hr):
            Downloads last N days based on lookback_periods
            Example: lookback_periods=7 downloads last 7 days

        Returns:
            Tuple of (start_time, end_time)
        """
        # Get lookback_periods from config (default to 1 if not specified)
        lookback_periods = getattr(self.config, "lookback_periods", 1)

        # Use shared time utility - single source of truth for time calculation
        return calculate_download_time_range(
            session_type=self.config.session_type, lookback_periods=lookback_periods
        )

    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that download can be performed.

        Checks:
        - Station configuration exists
        - Receiver can be created
        - Required parameters are present

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            # Import here to avoid circular dependencies
            from ...cli.main import get_station_config

            # Get station configuration
            self._station_config = get_station_config(self.station_id)
            if not self._station_config:
                return False, f"No configuration found for station {self.station_id}"

            # Check required fields
            required_fields = ["receiver_type"]
            missing = [f for f in required_fields if f not in self._station_config]
            if missing:
                return False, f"Missing required fields: {', '.join(missing)}"

            return True, None

        except Exception as e:
            return False, f"Validation failed: {str(e)}"

    def _create_receiver(self):
        """Create receiver instance for this station.

        Returns:
            Receiver instance

        Raises:
            ValueError: If receiver cannot be created
        """
        if self._receiver:
            return self._receiver

        from ...cli.main import create_receiver

        if not self._station_config:
            raise ValueError(
                "Station config not loaded - call validate_prerequisites first"
            )

        self._receiver = create_receiver(self.station_id, self._station_config)
        return self._receiver

    def execute(self) -> TaskResult:
        """Execute the download task.

        Performs complete download operation:
        1. Validates prerequisites
        2. Creates receiver instance
        3. Calculates time parameters
        4. Executes download
        5. Returns structured result

        Returns:
            TaskResult with download details
        """
        start_time_exec = time.time()

        try:
            # Validate prerequisites
            self.logger.info(
                f"Starting download: {self.station_id} ({self.config.session_type})"
            )

            valid, error = self.validate_prerequisites()
            if not valid:
                duration = time.time() - start_time_exec
                return TaskResult(
                    success=False,
                    status="validation_failed",
                    duration=duration,
                    message=f"Validation failed: {error}",
                    data={"station_id": self.station_id},
                    error=f"ValidationError: {error}",
                )

            # Create receiver
            receiver = self._create_receiver()

            # Get time parameters
            start_time, end_time = self.get_time_parameters()

            # Determine frequency based on session type
            if self.config.session_type == "15s_24hr":
                frequency = "1D"
            else:
                frequency = "1H"

            # Execute download with all Phase 1 features
            result = receiver.download_data(
                start=start_time,
                end=end_time,
                session=self.config.session_type,
                ffrequency=frequency,
                sync=True,  # Always sync in scheduled mode
                archive=True,  # Always archive
                immediate_archive=True,  # Use fault-tolerant immediate archiving
                clean_tmp=False,  # Keep partial files so FTP REST resume works
                compression=".gz",
                loglevel=logging.INFO,
            )

            # Calculate duration
            duration = time.time() - start_time_exec

            # Extract key metrics
            files_downloaded = result.get("files_downloaded", 0)
            bytes_downloaded = result.get("total_bytes", 0)
            errors = result.get("errors", 0)

            # Determine success
            success = result.get("status") == "completed" and errors == 0

            # Build result message
            if success:
                message = f"Downloaded {files_downloaded} files ({bytes_downloaded:,} bytes) in {duration:.1f}s"
            else:
                message = f"Download completed with {errors} errors"

            self.logger.info(
                f"Completed: {self.station_id} ({self.config.session_type}) - {message}"
            )

            return TaskResult(
                success=success,
                status=result.get("status", "completed"),
                duration=duration,
                message=message,
                data={
                    "station_id": self.station_id,
                    "session": self.config.session_type,
                    "files_downloaded": files_downloaded,
                    "bytes_downloaded": bytes_downloaded,
                    "errors": errors,
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                },
                metrics={
                    "connection_time": result.get("connection_time"),
                    "download_speed": bytes_downloaded / duration
                    if duration > 0
                    else 0,
                },
            )

        except Exception as e:
            duration = time.time() - start_time_exec
            error_msg = f"{type(e).__name__}: {str(e)}"

            self.logger.error(
                f"Download failed: {self.station_id} ({self.config.session_type}) - {error_msg}"
            )

            return TaskResult(
                success=False,
                status="error",
                duration=duration,
                message=f"Download failed: {str(e)}",
                data={
                    "station_id": self.station_id,
                    "session": self.config.session_type,
                },
                error=error_msg,
            )
