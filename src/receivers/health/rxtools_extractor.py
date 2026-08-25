"""RxTools-based health data extractor for Septentrio PolaRX5 receivers.

This module uses Septentrio's RxTools bin2asc command to extract health data
from SBF (Septentrio Binary Format) files downloaded in status_1hr sessions.

Health Messages Extracted (SBF blocks):
- 4101 PowerStatus: Power supply information (voltage, power source)
- 4059 DiskStatus: Internal storage status (free space, usage %)
- 4014 ReceiverStatus: Overall receiver status (CPU load, uptime, error codes)
- 4082 QualityInd: Data quality indicators

This module uses the verified rxtools_extractor utility from receivers.utils
which wraps bin2asc with proper CSV parsing.
"""

import logging
import os
import tempfile
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Import our verified rxtools utility
from ..utils.rxtools_extractor import (
    BIN2ASC_PATH,
    RXTOOLS_HEALTH_TIMEOUT_S,
    extract_sbf_blocks,
)


class RxToolsNotFoundError(Exception):
    """Raised when RxTools bin2asc is not found in PATH."""

    pass


class RxToolsExtractor:
    """Extract health data from SBF files using RxTools bin2asc.

    This class uses the verified rxtools_extractor utility functions
    which provide proper CSV parsing of bin2asc output with validated
    voltage scaling factors.
    """

    def __init__(self, station_id: str = "UNKNOWN", config: dict | None = None):
        """Initialize RxTools extractor.

        Args:
            station_id: Station identifier for logging
            config: Optional threshold config dict. If provided, keys like
                'power_type' are forwarded to MetricChecker.
        """
        self.station_id = station_id
        self.logger = logging.getLogger(f"receivers.health.rxtools.{station_id}")

        from .metrics import MetricChecker, load_thresholds

        power_type = (config or {}).get("power_type")
        thresholds = load_thresholds(power_type=power_type)
        self._checker = MetricChecker(thresholds)

    def check_rxtools_available(self) -> bool:
        """Check if RxTools bin2asc is available.

        Returns:
            True if bin2asc is found, False otherwise
        """
        return Path(BIN2ASC_PATH).exists()

    def extract_health_from_sbf(
        self, sbf_file: Path, output_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Extract health data from SBF file using verified rxtools utilities.

        Args:
            sbf_file: Path to SBF file (can be .sbf or .sbf.gz)
            output_dir: Optional directory for temp files (not used, kept for compatibility)

        Returns:
            Dictionary with extracted health data

        Raises:
            RxToolsNotFoundError: If bin2asc is not available
            FileNotFoundError: If SBF file doesn't exist
        """
        if not self.check_rxtools_available():
            raise RxToolsNotFoundError(
                f"RxTools bin2asc not found at {BIN2ASC_PATH}. "
                "Please install RxTools from Septentrio: "
                "https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools"
            )

        if not sbf_file.exists():
            raise FileNotFoundError(f"SBF file not found: {sbf_file}")

        self.logger.info(f"Extracting health data from {sbf_file}")

        # Decompress if needed
        sbf_path = self._decompress_if_needed(sbf_file)

        try:
            # Extract health data using verified utility functions
            health_data = self._extract_all_health_blocks(sbf_path)
            return health_data
        finally:
            # Clean up decompressed file if we created it
            if sbf_path != sbf_file and sbf_path.exists():
                sbf_path.unlink()

    def _decompress_if_needed(self, sbf_file: Path) -> Path:
        """Decompress SBF file if compressed.

        Uses CompressionConverter which handles gzip, bzip2, xz, and
        double-compression automatically.

        Args:
            sbf_file: Path to potentially compressed SBF file

        Returns:
            Path to decompressed SBF file (temp file if decompression needed)
        """
        from ..utils.compression_detector import (
            CompressionConverter,
            CompressionDetector,
        )

        detector = CompressionDetector()
        if not detector.detect_compression(sbf_file):
            return sbf_file

        fd, tmp_name = tempfile.mkstemp(suffix=".sbf")
        os.close(fd)
        temp_sbf = Path(tmp_name)
        converter = CompressionConverter()
        if not converter.decompress_file(sbf_file, temp_sbf):
            raise ValueError(f"Failed to decompress: {sbf_file}")

        # Verify SBF magic bytes
        with open(temp_sbf, "rb") as f:
            if f.read(2) != b"$@":
                temp_sbf.unlink()
                raise ValueError(f"Not a valid SBF file: {sbf_file}")

        return temp_sbf

    def _extract_all_health_blocks(self, sbf_file: Path) -> Dict[str, Any]:
        """Extract all health blocks using verified utility functions.

        One ``bin2asc`` pass covers all four blocks.  Extracting them one at a
        time cost four subprocesses AND four stacked timeout ceilings on the
        same health-executor thread — the pile-up that starved fleet monitoring
        on 2026-08-11.  Blocks are still parsed independently, so a malformed
        one loses only itself.

        Args:
            sbf_file: Path to decompressed SBF file

        Returns:
            Dictionary with structured health data
        """
        health_data = {
            "extraction_time": datetime.now(UTC).isoformat(),
            "metrics": {},
            "data_quality": {},
            "receiver_specific": {},
        }

        try:
            blocks = extract_sbf_blocks(
                sbf_file,
                ("PowerStatus", "ReceiverStatus2", "DiskStatus", "QualityInd"),
                timeout=RXTOOLS_HEALTH_TIMEOUT_S,
            )
        except Exception as e:
            # Whole-file failure (bin2asc wedged, killed, or unreadable input).
            # Return the empty structure so the caller degrades to port-only
            # health rather than losing the health row altogether.
            self.logger.warning(f"Failed to extract health blocks: {e}")
            return health_data

        # Extract PowerStatus (4101)
        try:
            power_data = blocks["PowerStatus"]
            if power_data:
                latest = power_data[-1]  # Get most recent sample
                voltage = latest.get("Vin Voltage [V]")
                if voltage is not None:
                    health_data["metrics"]["power"] = {
                        "voltage": voltage,
                        "unit": "V",
                        "status": self._checker.check_voltage(voltage).status.value,
                        "timestamp": (
                            latest.get("datetime").isoformat()
                            if latest.get("datetime")
                            else None
                        ),
                    }
                self.logger.debug(f"Extracted PowerStatus: {voltage}V")
        except Exception as e:
            self.logger.warning(f"Failed to extract PowerStatus: {e}")

        # Extract ReceiverStatus (4014) - ReceiverStatus2
        try:
            receiver_data = blocks["ReceiverStatus2"]
            if receiver_data:
                latest = receiver_data[-1]  # Get most recent sample

                # CPU load
                cpu_load = latest.get("CPULoad [%]")
                if cpu_load is not None:
                    health_data["metrics"]["cpu_load"] = {
                        "percent": cpu_load,
                        "status": self._checker.check_cpu_load(
                            int(cpu_load)
                        ).status.value,
                    }

                # Temperature
                temp = latest.get("Temperature_degC [°C]")
                if temp is not None:
                    health_data["metrics"]["temperature"] = {
                        "value": temp,
                        "unit": "C",
                        "status": self._checker.check_temperature(temp).status.value,
                    }

                # Uptime
                uptime = latest.get("Up time [s]")
                if uptime is not None:
                    health_data["receiver_specific"]["uptime_seconds"] = uptime

                self.logger.debug(
                    f"Extracted ReceiverStatus: CPU={cpu_load}%, Temp={temp}°C"
                )
        except Exception as e:
            self.logger.warning(f"Failed to extract ReceiverStatus: {e}")

        # Extract DiskStatus (4059)
        try:
            disk_data = blocks["DiskStatus"]
            if disk_data:
                latest = disk_data[-1]  # Get most recent sample

                # Disk usage percentage — field name from bin2asc is 'DiskUsagePercent [%]'
                disk_usage = latest.get("DiskUsagePercent [%]")
                disk_size_mb = latest.get("DiskSize [MB]")

                if disk_usage is not None:
                    disk_info: dict = {
                        "usage_percent": float(disk_usage),
                        "status": self._checker.check_disk_usage(
                            float(disk_usage)
                        ).status.value,
                    }
                    if disk_size_mb is not None:
                        total_mb = float(disk_size_mb)
                        disk_info["total_mb"] = round(total_mb, 1)
                        disk_info["used_mb"] = round(
                            total_mb * float(disk_usage) / 100, 1
                        )
                    health_data["data_quality"]["disk_usage"] = disk_info

                self.logger.debug(f"Extracted DiskStatus: {disk_usage}% used")
        except Exception as e:
            self.logger.warning(f"Failed to extract DiskStatus: {e}")

        # Extract QualityInd (4082)
        try:
            quality_data = blocks["QualityInd"]
            if quality_data:
                latest = quality_data[-1]  # Get most recent sample

                # Number of tracked satellites
                n_sats = latest.get("N")
                if n_sats is not None:
                    health_data["data_quality"]["tracked_satellites"] = n_sats

                self.logger.debug(f"Extracted QualityInd: {n_sats} satellites")
        except Exception as e:
            self.logger.warning(f"Failed to extract QualityInd: {e}")

        return health_data
