"""Health task implementation for historical health extraction.

HealthTask processes downloaded status_1hr files to extract health metrics
and write them to the PostgreSQL database. This is a BACKGROUND task that
runs after status_1hr downloads complete.

Key characteristics:
- BACKFILL priority: Runs when resources are available
- Processes downloaded SBF files (not live data)
- Extracts timeseries health data for historical analysis
- Lower priority than real-time StatusTask

Distinction from StatusTask:
- StatusTask: Live check every 15 min, REALTIME priority
- HealthTask: Process downloaded files, BACKFILL priority
"""

import logging
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
class HealthTask(ScheduledTask):
    """Background health extraction from downloaded status files.

    Processes status_1hr downloads to extract health timeseries:
    1. Locates downloaded status files for the target time
    2. Extracts health metrics using RxTools (PolaRX5) or protocol-specific parsers
    3. Writes timeseries data to PostgreSQL

    This task runs at BACKFILL priority so it doesn't block real-time operations.
    """

    # Default to BACKFILL priority for historical processing
    default_priority = TaskPriority.BACKFILL

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None,
        input_files: Optional[List[str]] = None,
        send_to_database: bool = True,
        send_to_icinga: bool = False,
    ):
        """Initialize health extraction task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
            input_files: List of input file paths (from pipeline)
            send_to_database: Write health data to PostgreSQL
            send_to_icinga: Send to Icinga (usually False for historical)
        """
        super().__init__(station_id, config, logger)
        self.input_files = input_files or []
        self.send_to_database = send_to_database
        self.send_to_icinga = send_to_icinga
        self._station_config: Optional[Dict[str, Any]] = None

    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Get time parameters for health extraction.

        Uses the same logic as download tasks - previous complete hour.

        Returns:
            Tuple of (start_time, end_time)
        """
        from ...utils.time_utils import calculate_download_time_range

        lookback_periods = getattr(self.config, "lookback_periods", 1)
        return calculate_download_time_range(
            session_type=self.config.session_type, lookback_periods=lookback_periods
        )

    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that health extraction can be performed.

        Checks:
        - Station configuration exists
        - Input files exist (if provided)
        - Required extraction tools available

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            from ...cli.main import get_station_config

            self._station_config = get_station_config(self.station_id)
            if not self._station_config:
                return False, f"No configuration found for station {self.station_id}"

            # Validate input files if provided
            for file_path in self.input_files:
                if not Path(file_path).exists():
                    return False, f"Input file not found: {file_path}"

            return True, None

        except Exception as e:
            return False, f"Validation failed: {str(e)}"

    def execute(self) -> TaskResult:
        """Execute the health extraction task.

        Processes status files and extracts health timeseries.

        Returns:
            TaskResult with extraction details
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting health extraction: {self.station_id}")

            # Validate prerequisites
            valid, error = self.validate_prerequisites()
            if not valid:
                return self._create_failure_result(
                    start_time,
                    "validation_failed",
                    f"Validation failed: {error}",
                    f"ValidationError: {error}",
                )

            # Find input files if not provided
            if not self.input_files:
                self.input_files = self._find_status_files()

            if not self.input_files:
                return TaskResult(
                    success=True,
                    status="no_files",
                    duration=time.time() - start_time,
                    message="No status files found to process",
                    data={"station_id": self.station_id, "files_processed": 0},
                )

            # Process files and extract health data
            samples_written = 0
            files_processed = 0

            for file_path in self.input_files:
                try:
                    samples = self._extract_health_samples(Path(file_path))
                    if samples and self.send_to_database:
                        written = self._write_samples_to_database(samples)
                        samples_written += written
                    files_processed += 1
                except Exception as e:
                    self.logger.warning(f"Failed to process {file_path}: {e}")

            duration = time.time() - start_time
            self.logger.info(
                f"Health extraction complete: {self.station_id} - "
                f"{files_processed} files, {samples_written} samples ({duration:.1f}s)"
            )

            return TaskResult(
                success=True,
                status="completed",
                duration=duration,
                message=f"Extracted {samples_written} samples from {files_processed} files",
                data={
                    "station_id": self.station_id,
                    "files_processed": files_processed,
                    "samples_written": samples_written,
                    "input_files": self.input_files,
                },
                metrics={
                    "extraction_rate": samples_written / duration
                    if duration > 0
                    else 0,
                },
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"{type(e).__name__}: {str(e)}"
            self.logger.error(
                f"Health extraction failed: {self.station_id} - {error_msg}"
            )

            return self._create_failure_result(
                start_time, "error", f"Health extraction failed: {str(e)}", error_msg
            )

    def _find_status_files(self) -> List[str]:
        """Find status files to process based on time parameters.

        Returns:
            List of file paths
        """
        try:
            start_time, _ = self.get_time_parameters()

            # Try to find archive directory using gps_parser paths
            archive_base = Path.home() / ".cache" / "gps_receivers" / "archive"
            station_dir = archive_base / self.station_id / "status_1hr"

            # Try date-based subdirectory
            date_dir = (
                station_dir / start_time.strftime("%Y") / start_time.strftime("%j")
            )

            search_dirs = [date_dir, station_dir]
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue

                # Find .sbf or .sbf.gz files
                files = []
                for pattern in ["*.sbf", "*.sbf.gz"]:
                    files.extend(search_dir.glob(pattern))

                if files:
                    return [str(f) for f in sorted(files)]

            self.logger.debug(f"No status files found in {station_dir}")
            return []

        except Exception as e:
            self.logger.warning(f"Could not find status files: {e}")
            return []

    def _extract_health_samples(self, file_path: Path) -> List[Dict[str, Any]]:
        """Extract health samples from a status file.

        Args:
            file_path: Path to status file

        Returns:
            List of sample dictionaries with timestamps and metrics
        """
        if self._station_config is None:
            self.logger.warning("Station config not loaded")
            return []

        receiver_type = self._station_config.get("receiver_type", "PolaRX5")
        if isinstance(receiver_type, str):
            receiver_type = receiver_type.lower()

        if receiver_type in ("polarx5", "mosaic-x5"):
            return self._extract_from_sbf(file_path)
        else:
            # For other receiver types, use generic extraction
            self.logger.debug(f"No specific extractor for {receiver_type}")
            return []

    def _extract_from_sbf(self, file_path: Path) -> List[Dict[str, Any]]:
        """Extract health samples from SBF file.

        Args:
            file_path: Path to SBF file

        Returns:
            List of sample dictionaries
        """
        # SBF file parsing for health extraction
        # Note: Full SBF parsing requires RxTools (sbf2asc/sbf2stf)
        # For now, we log a debug message and return empty
        # Future: implement proper SBF block parsing
        self.logger.debug(f"SBF extraction from file not yet implemented: {file_path}")
        return []

    def _write_samples_to_database(self, samples: List[Dict[str, Any]]) -> int:
        """Write health samples to PostgreSQL.

        Args:
            samples: List of sample dictionaries

        Returns:
            Number of samples written
        """
        if self._station_config is None:
            self.logger.warning("Station config not loaded")
            return 0

        try:
            from ...health.db_writer import HealthDatabaseWriter

            with HealthDatabaseWriter() as db:
                written = 0
                for sample in samples:
                    if db.write_health_data(sample):
                        written += 1
                return written

        except ImportError:
            self.logger.warning("PostgreSQL writer not available")
            return 0
        except Exception as e:
            self.logger.error(f"Database write failed: {e}")
            return 0

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
            data={"station_id": self.station_id},
            error=error,
        )
