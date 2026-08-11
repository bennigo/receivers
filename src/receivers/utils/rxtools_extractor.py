"""
RxTools wrapper for extracting SBF blocks to CSV format.

This module provides a Python interface to the RxTools bin2asc utility
for extracting health monitoring data from SBF files.

Using bin2asc ensures we get the official Septentrio-validated values
without needing to manually parse the binary format.
"""

import csv
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .compression_detector import CompressionConverter, CompressionDetector

logger = logging.getLogger(__name__)

# GPS epoch: January 6, 1980 00:00:00 UTC
GPS_EPOCH = datetime(1980, 1, 6, 0, 0, 0)

# RxTools bin2asc location - find from PATH or fallback to default
BIN2ASC_PATH = shutil.which("bin2asc") or "/usr/local/rxtools/bin/bin2asc"

# Wall-clock ceilings for RxTools subprocesses.
#
# bin2asc can wedge on an input it does not like and never exit — observed
# 2026-08-11 on rek-d01, where DiskStatus blocks from one firmware revision
# left 77 bin2asc processes running for up to 2.9 h.  Without a ceiling the
# caller blocks forever holding a health worker, the 5-minute health cycle
# spawns more on top, and the pile-up starves the whole pipeline (health
# checks went ~3 h stale fleet-wide).  Always pass one of these.
#
# Two values because the call sites differ by orders of magnitude: a full
# daily SBF (~4 MB) legitimately takes a while, a single TCP-fetched block
# is a few dozen bytes and should be instant.
RXTOOLS_TIMEOUT_S = float(os.environ.get("RECEIVERS_RXTOOLS_TIMEOUT_S", "300"))
RXTOOLS_BYTES_TIMEOUT_S = float(
    os.environ.get("RECEIVERS_RXTOOLS_BYTES_TIMEOUT_S", "30")
)


def gps_time_to_datetime(tow_seconds: float, wnc: int) -> datetime:
    """Convert GPS Week Number and Time of Week to Python datetime."""
    return GPS_EPOCH + timedelta(weeks=wnc, seconds=tow_seconds)


def extract_sbf_message(
    sbf_file: Path,
    message_name: str,
    output_dir: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> Path:
    """
    Extract a specific SBF message type to CSV using bin2asc.

    Handles both compressed (.sbf.gz, .sbf.bz2) and uncompressed (.sbf) files.
    Compressed files are automatically decompressed to a temporary file before processing.

    Args:
        sbf_file: Path to SBF file (compressed or uncompressed)
        message_name: SBF message name (e.g., 'PowerStatus', 'ReceiverStatus2')
        output_dir: Optional output directory (default: temp directory)
        timeout: Seconds to allow bin2asc (default: RXTOOLS_TIMEOUT_S)

    Returns:
        Path to the generated CSV file

    Raises:
        RuntimeError: If bin2asc fails, or exceeds *timeout*
        FileNotFoundError: If bin2asc is not installed or file not found
    """
    if not Path(BIN2ASC_PATH).exists():
        raise FileNotFoundError(
            f"RxTools bin2asc not found at {BIN2ASC_PATH}. "
            "Please install RxTools from https://www.septentrio.com/"
        )

    if not sbf_file.exists():
        raise FileNotFoundError(f"SBF file not found: {sbf_file}")

    # Use output dir or temp
    if output_dir is None:
        output_dir = Path(tempfile.gettempdir())
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if file is compressed and decompress if needed
    detector = CompressionDetector()
    converter = CompressionConverter()

    compression_info = detector.detect_compression(sbf_file)
    temp_decompressed = None
    file_to_process = sbf_file

    if compression_info:
        # File is compressed - decompress to temp file
        format_name, _ = compression_info

        # Get base name without compression extension
        # If stem already ends with .sbf, use it; otherwise add .sbf
        base_name = sbf_file.stem
        if not base_name.endswith(".sbf"):
            base_name = f"{base_name}.sbf"

        temp_decompressed = output_dir / base_name

        if not converter.decompress_file(sbf_file, temp_decompressed):
            raise RuntimeError(f"Failed to decompress {format_name} file: {sbf_file}")

        file_to_process = temp_decompressed

    try:
        # Run bin2asc on uncompressed file
        # Output filename format: input_SBF_MessageName.txt
        cmd = [
            BIN2ASC_PATH,
            "-f",
            str(file_to_process),
            "-m",
            message_name,
            "-t",  # Include title columns
            "-p",
            str(output_dir),
        ]

        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=RXTOOLS_TIMEOUT_S if timeout is None else timeout,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"bin2asc failed for {message_name}:\n"
                f"  Command: {' '.join(cmd)}\n"
                f"  Error: {e.stderr}"
            )
        except subprocess.TimeoutExpired as e:
            # subprocess.run has already killed the child by the time this
            # is raised. Surface it as RuntimeError so every existing caller
            # degrades the same way it does for a bin2asc failure.
            logger.warning(
                "bin2asc timed out after %ss for %s (%s) — killed",
                e.timeout,
                message_name,
                file_to_process.name,
            )
            raise RuntimeError(
                f"bin2asc timed out after {e.timeout}s for {message_name}:\n"
                f"  Command: {' '.join(cmd)}"
            )

        # Find output file
        output_file = output_dir / f"{file_to_process.name}_SBF_{message_name}.txt"
        if not output_file.exists():
            raise RuntimeError(
                f"bin2asc did not create expected output file: {output_file}"
            )

        return output_file

    finally:
        # Clean up temporary decompressed file
        if temp_decompressed and temp_decompressed.exists():
            temp_decompressed.unlink()


def parse_csv_to_dict(csv_file: Path, skip_separator: bool = True) -> List[Dict]:
    """
    Parse bin2asc CSV output to list of dictionaries.

    Args:
        csv_file: Path to CSV file from bin2asc
        skip_separator: Skip the separator line (default: True)

    Returns:
        List of dictionaries with field names as keys
    """
    data = []

    with open(csv_file) as f:
        reader = csv.DictReader(f)

        for row in reader:
            # Skip separator line (usually starts with dashes)
            if skip_separator and any(v.startswith("---") for v in row.values()):
                continue

            # Convert numeric fields
            parsed_row = {}
            for key, value in row.items():
                # Try to convert to float, keep as string if fails
                try:
                    parsed_row[key] = float(value)
                except (ValueError, TypeError):
                    parsed_row[key] = value

            data.append(parsed_row)

    return data


def _enrich_rows_with_datetime(rows: List[Dict]) -> None:
    """Add a ``datetime`` key to each row that has GPS time fields (TOW + WNc)."""
    for row in rows:
        if "TOW [s]" in row and "WNc [w]" in row and row["WNc [w]"] != "":
            row["datetime"] = gps_time_to_datetime(row["TOW [s]"], int(row["WNc [w]"]))


def _extract_simple_block(sbf_file: Path, message_name: str) -> List[Dict]:
    """Extract an SBF message, parse CSV, enrich with datetime, clean up."""
    csv_file = extract_sbf_message(sbf_file, message_name)
    data = parse_csv_to_dict(csv_file)
    _enrich_rows_with_datetime(data)
    csv_file.unlink()
    return data


def extract_power_status(sbf_file: Path) -> List[Dict]:
    """Extract PowerStatus data from SBF file.

    Returns:
        List of dicts with keys: TOW, WNc, PowerSource, VinVoltage, datetime
    """
    return _extract_simple_block(sbf_file, "PowerStatus")


def extract_receiver_status(sbf_file: Path) -> List[Dict]:
    """Extract ReceiverStatus2 data from SBF file.

    Returns:
        List of dicts with receiver status fields including datetime
    """
    return _extract_simple_block(sbf_file, "ReceiverStatus2")


def extract_disk_status(sbf_file: Path) -> List[Dict]:
    """Extract DiskStatus data from SBF file."""
    return _extract_simple_block(sbf_file, "DiskStatus")


def extract_quality_ind(sbf_file: Path) -> List[Dict]:
    """Extract QualityInd data from SBF file."""
    return _extract_simple_block(sbf_file, "QualityInd")


def extract_channel_status(sbf_file: Path) -> List[Dict]:
    """Extract ChannelStatus data and aggregate satellite counts by GNSS system.

    Returns:
        List of dicts with datetime and satellite counts by GNSS system:
        {
            'datetime': datetime,
            'GPS': int,
            'GLONASS': int,
            'Galileo': int,
            'BeiDou': int,
            'QZSS': int,
            'total': int
        }
    """
    from collections import defaultdict

    csv_file = extract_sbf_message(sbf_file, "ChannelStatus")
    data = parse_csv_to_dict(csv_file)

    # Group by timestamp and count satellites by GNSS system
    timestamps = defaultdict(
        lambda: {
            "GPS": 0,
            "GLONASS": 0,
            "Galileo": 0,
            "BeiDou": 0,
            "QZSS": 0,
            "IRNSS": 0,
            "SBAS": 0,
        }
    )

    # GNSS system mapping based on SVID prefix
    gnss_map = {
        "G": "GPS",
        "R": "GLONASS",
        "E": "Galileo",
        "C": "BeiDou",
        "J": "QZSS",
        "I": "IRNSS",
        "S": "SBAS",
    }

    _enrich_rows_with_datetime(data)

    for row in data:
        dt = row.get("datetime")
        if dt is None:
            continue

        svid = row.get("SVID", "")

        # Check if satellite is being tracked (not just visible)
        # TrackingStatus Sig 1 should be "Tracking" (not "Idle", "Search", or "Not Used")
        tracking_status = row.get("TrackingStatus Sig 1", "")
        pvt_status = row.get("PVTStatus Sig 1", "")

        # Count satellite if it's being tracked and used in solution
        if tracking_status == "Tracking" and pvt_status == "Used":
            if svid and len(svid) > 0:
                prefix = svid[0]
                gnss_system = gnss_map.get(prefix)
                if gnss_system:
                    timestamps[dt][gnss_system] += 1

    # Convert to list of dicts
    result = []
    for dt, counts in sorted(timestamps.items()):
        counts["datetime"] = dt
        counts["total"] = sum(counts[sys] for sys in gnss_map.values())
        result.append(counts)

    csv_file.unlink()
    return result


def extract_pvt_geodetic(sbf_file: Path) -> List[Dict]:
    """Extract PVTGeodetic2 position data from SBF file.

    Returns:
        List of dicts with datetime and position/accuracy info:
        {
            'datetime': datetime,
            'latitude': float (degrees),
            'longitude': float (degrees),
            'height': float (meters),
            'h_accuracy': float (meters),
            'v_accuracy': float (meters),
            'nr_sv': int (satellites used),
            'fix_type': str ('Fixed', 'Float', etc.)
        }
    """
    import math

    csv_file = extract_sbf_message(sbf_file, "PVTGeodetic2")
    data = parse_csv_to_dict(csv_file)
    _enrich_rows_with_datetime(data)

    result = []
    for row in data:
        dt = row.get("datetime")
        if dt is None:
            continue

        # Extract latitude (convert from radians if needed)
        lat = None
        for key in ["Latitude [rad]", "Latitude [deg]", "Latitude"]:
            if key in row:
                try:
                    val = float(row[key])
                    if math.isnan(val):
                        break
                    # Convert radians to degrees if needed
                    if "[rad]" in key or abs(val) < math.pi:
                        val = math.degrees(val)
                    if abs(val) <= 90:
                        lat = round(val, 8)
                    break
                except (ValueError, TypeError):
                    pass

        # Extract longitude (convert from radians if needed)
        lon = None
        for key in ["Longitude [rad]", "Longitude [deg]", "Longitude"]:
            if key in row:
                try:
                    val = float(row[key])
                    if math.isnan(val):
                        break
                    # Convert radians to degrees if needed
                    if "[rad]" in key or abs(val) < math.pi:
                        val = math.degrees(val)
                    if abs(val) <= 180:
                        lon = round(val, 8)
                    break
                except (ValueError, TypeError):
                    pass

        # Extract height
        height = None
        for key in ["Height [m]", "Height"]:
            if key in row:
                try:
                    val = float(row[key])
                    if not math.isnan(val):
                        height = round(val, 3)
                    break
                except (ValueError, TypeError):
                    pass

        # Extract accuracy
        h_accuracy = None
        for key in ["HAccuracy [m]", "HAccuracy"]:
            if key in row:
                try:
                    val = float(row[key])
                    if not math.isnan(val) and val < 65535:
                        h_accuracy = round(val, 3)
                    break
                except (ValueError, TypeError):
                    pass

        v_accuracy = None
        for key in ["VAccuracy [m]", "VAccuracy"]:
            if key in row:
                try:
                    val = float(row[key])
                    if not math.isnan(val) and val < 65535:
                        v_accuracy = round(val, 3)
                    break
                except (ValueError, TypeError):
                    pass

        # Extract number of satellites
        nr_sv = None
        for key in ["NrSV", "NrSv"]:
            if key in row:
                try:
                    nr_sv = int(row[key])
                    break
                except (ValueError, TypeError):
                    pass

        # Extract fix type
        fix_type = None
        for key in ["Type", "Mode"]:
            if key in row:
                fix_type = str(row[key]).strip()
                break

        record = {
            "datetime": dt,
            "latitude": lat,
            "longitude": lon,
            "height": height,
            "h_accuracy": h_accuracy,
            "v_accuracy": v_accuracy,
            "nr_sv": nr_sv,
            "fix_type": fix_type,
        }
        result.append(record)

    csv_file.unlink()
    return result


def extract_wifi_status(sbf_file: Path) -> List[Dict]:
    """Extract WiFi AP status (active/disabled) from SBF file.

    Returns:
        List of dicts with:
            - datetime: timestamp
            - wifi_enabled: bool (True if WiFi AP is active)
            - status: str (raw status value for debugging)
    """
    csv_file = extract_sbf_message(sbf_file, "WiFiAPStatus")
    data = parse_csv_to_dict(csv_file)
    _enrich_rows_with_datetime(data)

    result = []
    for row in data:
        dt = row.get("datetime")
        if dt is None:
            continue

        # Extract status field - typical values might be:
        # "Running", "Active", "Enabled" = WiFi is on
        # "Disabled", "Stopped", "Inactive" = WiFi is off
        status_raw = row.get("Status", row.get("State", "Unknown"))

        # Determine if WiFi is enabled based on status value
        enabled = status_raw.lower() in ["running", "active", "enabled", "on"]

        result.append({"datetime": dt, "wifi_enabled": enabled, "status": status_raw})

    csv_file.unlink()
    return result


def parse_sbf_bytes(sbf_data: bytes, message_name: str) -> List[Dict]:
    """Parse raw SBF bytes through bin2asc and return parsed CSV records.

    Saves sbf_data to a temp file, runs bin2asc, and parses the CSV output.
    Handles the full bin2asc subprocess lifecycle.

    Args:
        sbf_data: Raw SBF binary data
        message_name: SBF message name (e.g., 'DiskStatus', 'ReceiverStatus2')

    Returns:
        List of parsed record dicts (empty if no data or bin2asc fails)

    Raises:
        FileNotFoundError: If bin2asc is not available
    """
    if not Path(BIN2ASC_PATH).exists():
        raise FileNotFoundError(f"bin2asc not found at {BIN2ASC_PATH}")

    tmp_dir = tempfile.mkdtemp(prefix="sbf_parse_")
    try:
        sbf_path = Path(tmp_dir) / "data.sbf"
        sbf_path.write_bytes(sbf_data)

        csv_file = extract_sbf_message(
            sbf_path,
            message_name,
            output_dir=Path(tmp_dir),
            # A single fetched block is tens of bytes; anything that takes
            # longer than this is wedged, not working.
            timeout=RXTOOLS_BYTES_TIMEOUT_S,
        )
        data = parse_csv_to_dict(csv_file)
        _enrich_rows_with_datetime(data)
        return data
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def list_available_messages() -> List[str]:
    """
    Get list of all SBF message types supported by bin2asc.

    Returns:
        List of message names
    """
    if not Path(BIN2ASC_PATH).exists():
        raise FileNotFoundError(f"RxTools bin2asc not found at {BIN2ASC_PATH}")

    cmd = [BIN2ASC_PATH, "-l"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=RXTOOLS_BYTES_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("bin2asc -l timed out after %ss — killed", e.timeout)
        return []

    # Parse output to get message names
    messages = []
    for line in result.stdout.split("\n"):
        line = line.strip()
        if line.startswith("-"):
            # Message line format: "- MessageName"
            msg_name = line[1:].strip()
            if msg_name:
                messages.append(msg_name)

    return messages


def detect_blocks_in_file(sbf_file: Path) -> List[str]:
    """
    Detect which SBF blocks are actually present in a file.

    Uses sbfanalyzer to list blocks in the file.

    Args:
        sbf_file: Path to SBF file (compressed or uncompressed)

    Returns:
        List of block names present in the file
    """
    if not sbf_file.exists():
        raise FileNotFoundError(f"SBF file not found: {sbf_file}")

    # sbfanalyzer can be sbfanalyzer or sbfblocks
    sbfanalyzer_path = (
        shutil.which("sbfanalyzer")
        or shutil.which("sbfblocks")
        or "/usr/local/rxtools/bin/sbfanalyzer"
    )

    if not Path(sbfanalyzer_path).exists():
        raise FileNotFoundError(f"RxTools sbfanalyzer not found at {sbfanalyzer_path}")

    # Handle compressed files
    detector = CompressionDetector()
    converter = CompressionConverter()
    compression_info = detector.detect_compression(sbf_file)

    temp_decompressed = None
    file_to_process = sbf_file

    if compression_info:
        format_name, _ = compression_info
        base_name = sbf_file.stem
        if not base_name.endswith(".sbf"):
            base_name = f"{base_name}.sbf"

        temp_decompressed = Path(tempfile.gettempdir()) / base_name

        if not converter.decompress_file(sbf_file, temp_decompressed):
            raise RuntimeError(f"Failed to decompress {format_name} file: {sbf_file}")

        file_to_process = temp_decompressed

    try:
        # Run sbfanalyzer to list blocks
        cmd = [sbfanalyzer_path, str(file_to_process)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=RXTOOLS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            logger.warning(
                "sbfanalyzer timed out after %ss for %s — killed",
                e.timeout,
                file_to_process.name,
            )
            raise RuntimeError(
                f"sbfanalyzer timed out after {e.timeout}s for {file_to_process.name}"
            )

        # Parse output to extract block names
        # Output format varies, but typically shows block names
        blocks = set()
        for line in result.stdout.split("\n"):
            # Look for block names (usually capitalized words)
            # sbfanalyzer output shows block IDs and names
            parts = line.strip().split()
            if len(parts) >= 2:
                # Try to find block name (usually second column)
                potential_block = parts[1] if len(parts) > 1 else parts[0]
                if potential_block and potential_block[0].isupper():
                    blocks.add(potential_block)

        return sorted(list(blocks))

    finally:
        if temp_decompressed and temp_decompressed.exists():
            temp_decompressed.unlink()


def clean_field_name(field_name: str) -> tuple[str, Optional[str]]:
    """
    Clean up bin2asc field names by extracting units.

    Examples:
        'TOW [s]' -> ('TOW', 's')
        'Vin Voltage [V]' -> ('VinVoltage', 'V')
        'Power Source' -> ('PowerSource', None)

    Returns:
        Tuple of (clean_name, unit)
    """
    import re

    # Extract unit from brackets
    unit_match = re.search(r"\[([^\]]+)\]", field_name)
    unit = unit_match.group(1) if unit_match else None

    # Remove unit brackets
    clean = re.sub(r"\s*\[[^\]]+\]", "", field_name)

    # Remove spaces
    clean = clean.replace(" ", "")

    return clean, unit


def extract_block_with_metadata(sbf_file: Path, block_name: str) -> Dict:
    """
    Extract an SBF block with field metadata (units, etc.).

    Args:
        sbf_file: Path to SBF file
        block_name: SBF block name (e.g., 'WiFiAPStatus')

    Returns:
        Dict with:
            - 'fields': Dict of field metadata {field_name: {'unit': str, 'raw_name': str}}
            - 'data': List of dicts with cleaned field names and datetime
    """
    csv_file = extract_sbf_message(sbf_file, block_name)

    # Read CSV with original field names
    raw_data = parse_csv_to_dict(csv_file)

    if not raw_data:
        csv_file.unlink()
        return {"fields": {}, "data": []}

    # Build field metadata
    fields = {}
    sample_row = raw_data[0]
    for raw_field in sample_row.keys():
        clean_name, unit = clean_field_name(raw_field)
        fields[clean_name] = {"raw_name": raw_field, "unit": unit}

    # Enrich with datetime before cleaning field names
    _enrich_rows_with_datetime(raw_data)

    # Clean up data
    cleaned_data = []
    for row in raw_data:
        cleaned_row = {}

        if "datetime" in row:
            cleaned_row["datetime"] = row["datetime"]

        # Clean field names
        for raw_field, value in row.items():
            clean_name, _ = clean_field_name(raw_field)
            cleaned_row[clean_name] = value

        cleaned_data.append(cleaned_row)

    csv_file.unlink()

    return {"fields": fields, "data": cleaned_data}


def main():
    """Example usage."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python rxtools_extractor.py <sbf_file>")
        print("\nThis script demonstrates extracting SBF data using RxTools bin2asc")
        sys.exit(1)

    sbf_file = Path(sys.argv[1])

    print(f"Extracting data from: {sbf_file}\n")

    # Extract PowerStatus
    print("=== PowerStatus ===")
    power_data = extract_power_status(sbf_file)
    print(f"Found {len(power_data)} PowerStatus records")
    if power_data:
        for i, record in enumerate(power_data[:3]):
            print(
                f"  [{i}] {record['datetime']}: "
                f"{record.get('Vin Voltage [V]', 'N/A')}V "
                f"({record.get('Power Source', 'N/A')})"
            )

    # Extract ReceiverStatus
    print("\n=== ReceiverStatus2 ===")
    receiver_data = extract_receiver_status(sbf_file)
    print(f"Found {len(receiver_data)} ReceiverStatus2 records")
    if receiver_data:
        for i, record in enumerate(receiver_data[:3]):
            print(f"  [{i}] {record['datetime']}")
            # Print available fields
            for key in list(record.keys())[:5]:
                if key != "datetime":
                    print(f"      {key}: {record[key]}")


if __name__ == "__main__":
    main()
