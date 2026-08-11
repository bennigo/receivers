"""
Per-block JSON writer for automatic SBF block extraction.

This module extracts ALL SBF blocks found in status files and writes
each block type to a separate JSON file for exploration purposes.

The main health JSON (`STATION_DATE_health.json`) contains only
curated/structured monitoring metrics. Per-block JSONs are used to
explore new metrics before promoting them to the main JSON.
"""

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..utils.rxtools_extractor import (
    RXTOOLS_TIMEOUT_S,
    detect_blocks_in_file,
    extract_block_with_metadata,
)


class BlockJsonWriter:
    """Writes per-block JSON files for SBF block exploration."""

    def __init__(self, station_id: str, output_dir: Path):
        """
        Initialize per-block JSON writer.

        Args:
            station_id: Station identifier (e.g., 'ELEY')
            output_dir: Directory for JSON output (creates blocks/ subdirectory)
        """
        self.station_id = station_id
        self.output_dir = Path(output_dir)
        self.blocks_dir = self.output_dir / "blocks"
        self.blocks_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(f"receivers.health.{station_id}")

    def extract_all_blocks(
        self,
        sbf_files: List[Path],
        target_date: date,
        skip_blocks: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """
        Extract all SBF blocks from files and write to per-block JSON files.

        Args:
            sbf_files: List of SBF files to process
            target_date: Date being extracted
            skip_blocks: Optional list of blocks to skip (e.g., already in main JSON)

        Returns:
            Dict of {block_name: sample_count}
        """
        if skip_blocks is None:
            # Default: skip blocks already handled in main health JSON
            skip_blocks = [
                "PowerStatus",
                "ReceiverStatus2",
                "DiskStatus",
                "QualityInd",
                "ChannelStatus",
                "WiFiAPStatus",
            ]

        self.logger.info(
            f"Extracting all blocks from {len(sbf_files)} files using bin2asc"
        )
        self.logger.debug(f"Skipping blocks: {', '.join(skip_blocks)}")

        # Collect all blocks across all files
        blocks_data = defaultdict(
            lambda: {"fields": {}, "samples": [], "file_count": 0}
        )

        import shutil
        import subprocess
        import tempfile

        from ..utils.compression_detector import (
            CompressionConverter,
            CompressionDetector,
        )
        from ..utils.rxtools_extractor import (
            clean_field_name,
            gps_time_to_datetime,
            parse_csv_to_dict,
        )

        BIN2ASC_PATH = shutil.which("bin2asc") or "/usr/local/rxtools/bin/bin2asc"

        detector = CompressionDetector()
        converter = CompressionConverter()

        # Process each SBF file
        for file_idx, sbf_file in enumerate(sbf_files, 1):
            try:
                self.logger.debug(
                    f"Processing file {file_idx}/{len(sbf_files)}: {sbf_file.name}"
                )

                # Handle compressed files
                compression_info = detector.detect_compression(sbf_file)
                temp_decompressed = None
                file_to_process = sbf_file

                if compression_info:
                    format_name, _ = compression_info
                    base_name = sbf_file.stem
                    if not base_name.endswith(".sbf"):
                        base_name = f"{base_name}.sbf"

                    temp_decompressed = (
                        Path(tempfile.gettempdir()) / f"{base_name}.{file_idx}"
                    )

                    if not converter.decompress_file(sbf_file, temp_decompressed):
                        raise RuntimeError(
                            f"Failed to decompress {format_name} file: {sbf_file}"
                        )

                    file_to_process = temp_decompressed

                # Create temp directory for bin2asc output
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)

                    # Run bin2asc -f file to extract ALL blocks at once
                    # -t: Show title columns for each output file
                    cmd = [
                        BIN2ASC_PATH,
                        "-f",
                        str(file_to_process),
                        "-p",
                        str(temp_path),
                        "-t",
                    ]
                    try:
                        result = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=RXTOOLS_TIMEOUT_S,
                        )
                    except subprocess.TimeoutExpired as e:
                        # Unbounded here let wedged bin2asc processes accumulate
                        # until the box was starved — see rxtools_extractor.
                        self.logger.warning(
                            f"bin2asc timed out after {e.timeout}s for "
                            f"{sbf_file.name} — killed"
                        )
                        continue

                    if result.returncode != 0:
                        self.logger.warning(
                            f"bin2asc failed for {sbf_file.name}: {result.stderr}"
                        )
                        continue

                    # Parse all generated CSV files
                    csv_files = list(
                        temp_path.glob(f"{file_to_process.name}_SBF_*.txt")
                    )
                    self.logger.debug(f"  Found {len(csv_files)} block CSVs")

                    for csv_file in csv_files:
                        # Extract block name from filename: FILENAME_SBF_BlockName.txt
                        block_name = csv_file.stem.split("_SBF_")[1]

                        if block_name in skip_blocks:
                            continue

                        try:
                            # Parse CSV
                            raw_data = parse_csv_to_dict(csv_file)

                            if not raw_data:
                                continue

                            # Build field metadata (first file wins)
                            if not blocks_data[block_name]["fields"]:
                                fields = {}
                                for raw_field in raw_data[0].keys():
                                    clean_name, unit = clean_field_name(raw_field)
                                    fields[clean_name] = {
                                        "raw_name": raw_field,
                                        "unit": unit,
                                    }
                                blocks_data[block_name]["fields"] = fields

                            # Clean up data
                            for row in raw_data:
                                cleaned_row = {}

                                # Add datetime if available
                                if "TOW [s]" in row and "WNc [w]" in row:
                                    try:
                                        cleaned_row["datetime"] = gps_time_to_datetime(
                                            row["TOW [s]"], int(row["WNc [w]"])
                                        )
                                    except Exception:
                                        pass

                                # Clean field names
                                for raw_field, value in row.items():
                                    clean_name, _ = clean_field_name(raw_field)
                                    cleaned_row[clean_name] = value

                                blocks_data[block_name]["samples"].append(cleaned_row)

                            blocks_data[block_name]["file_count"] += 1

                        except Exception as e:
                            self.logger.debug(f"  Error parsing {block_name}: {e}")
                            continue

                # Clean up temp decompressed file
                if temp_decompressed and temp_decompressed.exists():
                    temp_decompressed.unlink()

            except Exception as e:
                self.logger.warning(f"Error processing {sbf_file.name}: {e}")
                continue

        # Write per-block JSON files
        block_stats = {}
        for block_name, block_info in blocks_data.items():
            if not block_info["samples"]:
                continue

            sample_count = len(block_info["samples"])
            self.logger.info(
                f"  {block_name}: {sample_count} samples from {block_info['file_count']} files"
            )

            # Write JSON file
            self._write_block_json(
                block_name, target_date, block_info["fields"], block_info["samples"]
            )

            block_stats[block_name] = sample_count

        return block_stats

    def _write_block_json(
        self, block_name: str, target_date: date, fields: Dict, samples: List[Dict]
    ) -> Path:
        """
        Write a single block to JSON file.

        Args:
            block_name: SBF block name
            target_date: Date of data
            fields: Field metadata
            samples: List of sample dicts

        Returns:
            Path to written JSON file
        """
        date_str = target_date.strftime("%Y%m%d")
        output_file = (
            self.blocks_dir / f"{self.station_id}_{date_str}_{block_name}.json"
        )

        # Sort samples by datetime if available
        if samples and "datetime" in samples[0]:
            samples = sorted(samples, key=lambda s: s["datetime"])

        # Build JSON structure
        json_data = {
            "station_id": self.station_id,
            "date": target_date.isoformat(),
            "block_name": block_name,
            "sample_count": len(samples),
            "fields": fields,
            "timeseries": [],
        }

        # Add time range if available
        if samples and "datetime" in samples[0]:
            json_data["time_range"] = {
                "start": samples[0]["datetime"].isoformat() + "Z",
                "end": samples[-1]["datetime"].isoformat() + "Z",
            }

        # Convert samples to JSON-serializable format
        for sample in samples:
            json_sample = {}
            for key, value in sample.items():
                if key == "datetime":
                    json_sample["time"] = value.isoformat() + "Z"
                elif isinstance(value, datetime):
                    json_sample[key] = value.isoformat() + "Z"
                else:
                    json_sample[key] = value

            json_data["timeseries"].append(json_sample)

        # Write JSON file
        with open(output_file, "w") as f:
            json.dump(json_data, f, indent=2)

        self.logger.debug(f"Wrote {output_file}")

        return output_file
