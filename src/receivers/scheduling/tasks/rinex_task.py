"""RINEX conversion task for the scheduler.

RINEXTask converts raw receiver files to RINEX format. This is typically
run after a download completes in a pipeline:
    Download → RINEX → Sync

Key characteristics:
- CPU-bound operation using CPU resource pool
- Receiver-agnostic: uses create_receiver() factory
- Produces RINEX observation files with header corrections
- Supports Hatanaka compression and various output formats
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


class RINEXTask(ScheduledTask):
    """Convert raw receiver files to RINEX format.

    This task is receiver-agnostic - it uses the receiver factory to
    get the appropriate converter for the receiver type:
    - PolaRX5: Uses SBFConverter (sbf2rin)
    - Trimble: Uses TrimbleConverter (runpkr00/teqc)
    - Leica: Uses LeicaConverter

    Typically runs at STANDARD priority as part of a pipeline.
    """

    # Default to STANDARD priority (processing after download)
    default_priority = TaskPriority.STANDARD

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None,
        input_files: Optional[List[str]] = None,
        output_dir: Optional[Path] = None,
        rinex_version: int = 3,
        apply_hatanaka: bool = True,
        apply_header_corrections: bool = True,
    ):
        """Initialize RINEX conversion task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
            input_files: List of raw file paths to convert
            output_dir: Output directory for RINEX files
            rinex_version: RINEX version (2 or 3)
            apply_hatanaka: Apply Hatanaka compression
            apply_header_corrections: Apply TOS metadata corrections
        """
        super().__init__(station_id, config, logger)
        self.input_files = input_files or []
        self.output_dir = output_dir
        self.rinex_version = rinex_version
        self.apply_hatanaka = apply_hatanaka
        self.apply_header_corrections = apply_header_corrections
        self._station_config: Optional[Dict[str, Any]] = None

    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Get time parameters for RINEX conversion.

        Uses the same logic as download tasks since conversion
        operates on recently downloaded data.

        Returns:
            Tuple of (start_time, end_time)
        """
        from ...utils.time_utils import calculate_download_time_range

        lookback_periods = getattr(self.config, "lookback_periods", 1)
        return calculate_download_time_range(
            session_type=self.config.session_type, lookback_periods=lookback_periods
        )

    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that RINEX conversion can be performed.

        Checks:
        - Station configuration exists
        - Input files exist (if provided)
        - RINEX converter tools are available

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
        """Execute the RINEX conversion.

        Converts raw files to RINEX format using the appropriate
        converter for the receiver type.

        Returns:
            TaskResult with conversion details
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting RINEX conversion: {self.station_id}")

            # Validate prerequisites
            valid, error = self.validate_prerequisites()
            if not valid:
                return self._create_failure_result(
                    start_time,
                    "validation_failed",
                    f"Validation failed: {error}",
                    f"ValidationError: {error}",
                )

            # Station config validated
            station_config = self._station_config
            if station_config is None:
                return self._create_failure_result(
                    start_time,
                    "error",
                    "Station config not loaded",
                    "InternalError: station config is None",
                )

            # Find input files if not provided
            if not self.input_files:
                self.input_files = self._find_raw_files()

            if not self.input_files:
                return TaskResult(
                    success=True,
                    status="no_files",
                    duration=time.time() - start_time,
                    message="No raw files found to convert",
                    data={"station_id": self.station_id, "files_converted": 0},
                )

            # Get converter for receiver type
            converter = self._get_converter(station_config)
            if converter is None:
                return self._create_failure_result(
                    start_time,
                    "error",
                    "No converter available for receiver type",
                    f"NoConverterError: {station_config.get('receiver_type', 'unknown')}",
                )

            # Convert files
            output_files = []
            successful = 0
            failed = 0

            for file_path in self.input_files:
                try:
                    result = converter.convert_file(
                        raw_file=file_path,
                        output_dir=self.output_dir,
                        force=False,
                    )

                    if result.success and result.rinex_file:
                        output_files.append(str(result.rinex_file))
                        successful += 1
                        self.logger.debug(
                            f"Converted: {file_path} -> {result.rinex_file}"
                        )
                    else:
                        failed += 1
                        self.logger.warning(
                            f"Conversion failed: {file_path} - {result.message}"
                        )

                except Exception as e:
                    failed += 1
                    self.logger.warning(f"Conversion error: {file_path} - {e}")

            duration = time.time() - start_time

            # Build result
            if failed == 0 and successful > 0:
                status = "completed"
                success = True
            elif successful > 0:
                status = "partial"
                success = True
            else:
                status = "failed"
                success = False

            message = f"Converted {successful}/{len(self.input_files)} files"
            if failed > 0:
                message += f" ({failed} failed)"

            self.logger.info(
                f"RINEX conversion complete: {self.station_id} - {message} ({duration:.1f}s)"
            )

            return TaskResult(
                success=success,
                status=status,
                duration=duration,
                message=message,
                data={
                    "station_id": self.station_id,
                    "files_converted": successful,
                    "files_failed": failed,
                    "input_files": self.input_files,
                },
                output_files=output_files,
                metrics={
                    "conversion_rate": successful / duration if duration > 0 else 0,
                    "rinex_version": self.rinex_version,
                    "hatanaka": self.apply_hatanaka,
                },
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"{type(e).__name__}: {str(e)}"
            self.logger.error(
                f"RINEX conversion failed: {self.station_id} - {error_msg}"
            )

            return self._create_failure_result(
                start_time, "error", f"RINEX conversion failed: {str(e)}", error_msg
            )

    def _find_raw_files(self) -> List[str]:
        """Find raw files to convert based on time parameters.

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
            raw_patterns = ["*.sbf", "*.sbf.gz", "*.T02", "*.T02.gz", "*.T00", "*.m00"]

            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue

                files = []
                for pattern in raw_patterns:
                    files.extend(search_dir.glob(pattern))

                if files:
                    return [str(f) for f in sorted(files)]

            return []

        except Exception as e:
            self.logger.warning(f"Could not find raw files: {e}")
            return []

    def _resolve_trimble_converter(self, fallback):
        """Return the native (Docker) Trimble converter, or ``fallback``.

        Mirrors ``async_converter._select_converter``: when
        ``[rinex] use_native_trimble`` is set and the Docker image is present,
        Trimble raw goes through the native converter. That is the only path
        that yields RINEX 3 for these receivers, and the only one that can read
        a .T02 whose payload is bzip2-compressed — runpkr00 exits 30 on those
        and leaves a 26-byte stub .dat.

        Falls back silently: a missing image or a Docker outage should degrade
        to the old path, not fail the task.
        """
        try:
            from ...config.receivers_config import get_receivers_config

            if (
                not get_receivers_config()
                .get_rinex_config()
                .get("use_native_trimble", False)
            ):
                return fallback

            from ...rinex.trimble_native_converter import TrimbleNativeConverter

            if TrimbleNativeConverter.is_available():
                return TrimbleNativeConverter
            self.logger.warning(
                "use_native_trimble is set but the Docker image is unavailable "
                "— falling back to runpkr00, which cannot read compressed .T02"
            )
        except Exception as exc:  # noqa: BLE001 - never fail conversion on this
            self.logger.debug(f"native Trimble converter unavailable: {exc}")
        return fallback

    def _get_converter(self, station_config: Dict[str, Any]):
        """Get the appropriate converter for the receiver type.

        Args:
            station_config: Station configuration dictionary

        Returns:
            Converter instance or None
        """
        from ...rinex.converter_base import OutputFormat, RinexVersion

        receiver_type = station_config.get("receiver_type", "").lower()

        # Determine output format based on hatanaka preference
        output_format = (
            OutputFormat.LEGACY if self.apply_hatanaka else OutputFormat.MODERN
        )

        session_type = self.config.session_type

        try:
            if receiver_type in ("polarx5", "mosaic-x5"):
                from ...rinex.sbf_converter import SBFConverter

                return SBFConverter(
                    station_id=self.station_id,
                    rinex_version=RinexVersion(self.rinex_version),
                    output_format=output_format,
                    apply_header_corrections=self.apply_header_corrections,
                    session_type=session_type,
                )
            elif receiver_type in ("netr9", "netrs", "netr5"):
                # Prefer the native (Docker/Wine) converter exactly as the live
                # path does in async_converter._select_converter. This branch
                # used to hardcode TrimbleConverter, so live downloads converted
                # natively while BACKFILL fell back to runpkr00 — which exits 30
                # on these .T02 (bzip2-compressed payload, 26-byte stub .dat).
                # Measured 2026-08-16: 566 distinct files retried, 1,132 failed
                # spawns per 3 h, and the gaps never filled. The native
                # converter is also the only one that produces RINEX 3 here.
                from ...rinex.trimble_converter import TrimbleConverter

                trimble_cls = self._resolve_trimble_converter(TrimbleConverter)

                return trimble_cls(
                    station_id=self.station_id,
                    rinex_version=RinexVersion(self.rinex_version),
                    output_format=output_format,
                    apply_header_corrections=self.apply_header_corrections,
                    session_type=session_type,
                )
            elif receiver_type in ("g10", "leica"):
                from ...rinex.leica_converter import LeicaConverter

                return LeicaConverter(
                    station_id=self.station_id,
                    rinex_version=RinexVersion(self.rinex_version),
                    output_format=output_format,
                    apply_header_corrections=self.apply_header_corrections,
                    session_type=session_type,
                )
            else:
                self.logger.warning(f"Unknown receiver type: {receiver_type}")
                return None

        except ImportError as e:
            self.logger.warning(f"Converter not available: {e}")
            return None

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
