"""Import JSON health data to PostgreSQL database.

Reads v2.0 daily health JSON files and imports timeseries data to the
checkcomm table for Grafana visualization.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .file_tracker import read_only_cursor

logger = logging.getLogger(__name__)


class HealthJsonImporter:
    """Import health JSON files to PostgreSQL database."""

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize importer.

        Args:
            connection_string: PostgreSQL connection string
                             (postgresql://user:pass@host:port/dbname)
                             If None, uses environment variables
        """
        self.connection_string = connection_string
        self._conn = None

    def connect(self, database: str = "gps_health") -> bool:
        """Connect to PostgreSQL database.

        Args:
            database: Database name (default: gps_health)

        Returns:
            True if connection successful
        """
        try:
            from .database_factory import DatabaseConnectionFactory

            self._conn = DatabaseConnectionFactory.get_connection(
                database=database,
                connection_string=self.connection_string,
            )
            logger.info("Connected to PostgreSQL database")
            return True

        except ImportError:
            logger.error("psycopg2 not installed - run: pip install psycopg2-binary")
            return False
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            return False

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _parse_timestamp(self, ts_str: str) -> datetime:
        """Parse ISO8601 timestamp string."""
        # Handle Z suffix
        ts_str = ts_str.rstrip("Z")
        return datetime.fromisoformat(ts_str)

    def _determine_status(self, voltage: Optional[float]) -> str:
        """Determine overall status based on voltage.

        Uses centralized MetricChecker thresholds from metrics.py.
        """
        from .metrics import MetricChecker

        checker = MetricChecker()
        result = checker.check_voltage(voltage)
        return result.status.value

    def import_json_file(
        self, json_path: Path, resolution: str = "minute"
    ) -> Tuple[int, int]:
        """Import a single JSON health file to block tables.

        All data is written to normalized block tables (block_power_status,
        block_receiver_status, etc.). The checkcomm view provides backward
        compatibility for Grafana queries.

        Args:
            json_path: Path to JSON file
            resolution: "minute" (all samples) or "hourly" (aggregate)

        Returns:
            Tuple of (rows_imported, rows_skipped)
        """
        from .db_writer import HealthDatabaseWriter

        try:
            with open(json_path) as f:
                data = json.load(f)

            station_id = data.get("station_id", "UNKN")
            receiver_type = data.get("receiver_type", "Unknown")
            timeseries = data.get("timeseries", [])

            if not timeseries:
                logger.warning(f"No timeseries data in {json_path.name}")
                return 0, 0

            logger.info(
                f"Importing {json_path.name}: {len(timeseries)} samples "
                f"for {station_id} ({receiver_type})"
            )

            # Use HealthDatabaseWriter to write to block tables
            # This ensures all data goes through the same path
            with HealthDatabaseWriter() as writer:
                rows_imported = writer.write_timeseries_batch(
                    station_id=station_id,
                    samples=timeseries,
                    receiver_type=receiver_type,
                    commit_interval=100,
                )

            rows_skipped = len(timeseries) - rows_imported

            logger.info(
                f"Imported {rows_imported} rows, skipped {rows_skipped} "
                f"from {json_path.name}"
            )
            return rows_imported, rows_skipped

        except Exception as e:
            logger.error(f"Failed to import {json_path}: {e}")
            return 0, 0

    def import_directory(
        self,
        json_dir: Path,
        station_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Tuple[int, int, int]:
        """Import all JSON files from a directory.

        Args:
            json_dir: Directory containing JSON health files
            station_id: Optional station filter
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            Tuple of (files_processed, total_rows, total_skipped)
        """
        if not json_dir.exists():
            logger.error(f"Directory not found: {json_dir}")
            return 0, 0, 0

        # Find all health JSON files
        pattern = f"{station_id}_*_health.json" if station_id else "*_health.json"
        json_files = sorted(json_dir.glob(pattern))

        if not json_files:
            logger.warning(f"No health JSON files found in {json_dir}")
            return 0, 0, 0

        # Filter by date if specified
        if start_date or end_date:
            filtered_files = []
            for f in json_files:
                # Extract date from filename: STATION_YYYYMMDD_health.json
                try:
                    parts = f.stem.split("_")
                    if len(parts) >= 2:
                        file_date = datetime.strptime(parts[1], "%Y%m%d")
                        if start_date and file_date < start_date:
                            continue
                        if end_date and file_date > end_date:
                            continue
                        filtered_files.append(f)
                except ValueError:
                    continue
            json_files = filtered_files

        logger.info(f"Found {len(json_files)} JSON files to import")

        total_rows = 0
        total_skipped = 0
        files_processed = 0

        for json_file in json_files:
            rows, skipped = self.import_json_file(json_file)
            total_rows += rows
            total_skipped += skipped
            files_processed += 1

        logger.info(
            f"Import complete: {files_processed} files, "
            f"{total_rows} rows imported, {total_skipped} skipped"
        )

        return files_processed, total_rows, total_skipped

    def import_health_data(
        self,
        health_data: Dict[str, Any],
        station_id: str,
        receiver_type: str = "Unknown",
    ) -> int:
        """Import health data dictionary directly to database.

        Uses HealthDatabaseWriter to write to block-aligned tables.

        Args:
            health_data: Health data dictionary with timeseries
            station_id: Station identifier
            receiver_type: Receiver type string

        Returns:
            Number of rows imported
        """
        from .db_writer import HealthDatabaseWriter

        timeseries = health_data.get("timeseries", [])
        if not timeseries:
            logger.warning(f"No timeseries data to import for {station_id}")
            return 0

        # Use HealthDatabaseWriter for block-aligned tables
        with HealthDatabaseWriter() as writer:
            rows_imported = writer.write_timeseries_batch(
                station_id=station_id,
                samples=timeseries,
                receiver_type=receiver_type,
                commit_interval=100,
            )

        return rows_imported

    def export_to_json(
        self,
        output_dir: Path,
        station_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Tuple[int, int]:
        """Export health data from database to JSON files.

        Creates daily JSON files in the v2.0 format.

        Args:
            output_dir: Directory to write JSON files
            station_id: Station identifier
            start_date: Start date for export
            end_date: End date for export

        Returns:
            Tuple of (files_written, total_rows)
        """
        if not self._conn:
            if not self.connect():
                return 0, 0

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build query
        query = """
            SELECT timestamp, recv_volt, recv_temp, recv_metrics, overall_status
            FROM checkcomm
            WHERE sid = %s
        """
        params: List[Any] = [station_id]

        if start_date:
            query += " AND timestamp >= %s"
            params.append(start_date)
        if end_date:
            query += " AND timestamp <= %s"
            params.append(end_date + timedelta(days=1))  # Include end date

        query += " ORDER BY timestamp"

        try:
            with read_only_cursor(self._conn) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()

            if not rows:
                logger.warning(f"No data found for {station_id}")
                return 0, 0

            # Group by date
            from collections import defaultdict

            daily_data: Dict[str, List[Dict]] = defaultdict(list)

            for row in rows:
                timestamp, voltage, temperature, metrics, status = row
                date_str = timestamp.strftime("%Y%m%d")

                # Parse metrics JSON if it's a string
                if isinstance(metrics, str):
                    metrics = json.loads(metrics)

                sample = {
                    "time": timestamp.isoformat() + "Z",
                    "voltage": (
                        {"value": voltage, "unit": "V", "status": "ok"}
                        if voltage
                        else {}
                    ),
                    "temperature": (
                        {"value": temperature, "unit": "C"} if temperature else {}
                    ),
                    "cpu_load": metrics.get("cpu_load", {}),
                    "disk_usage": metrics.get("disk", {}),
                    "satellites": metrics.get("satellites", {}),
                }
                daily_data[date_str].append(sample)

            # Write daily files
            files_written = 0
            total_rows = 0

            for date_str, samples in sorted(daily_data.items()):
                # Get receiver type from first sample's metrics or default
                receiver_type = "PolaRX5"  # Default

                output = {
                    "schema_version": "2.0",
                    "station_id": station_id,
                    "receiver_type": receiver_type,
                    "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                    "timeseries": samples,
                }

                output_file = output_dir / f"{station_id}_{date_str}_health.json"
                with open(output_file, "w") as f:
                    json.dump(output, f, indent=2, default=str)

                logger.info(f"Wrote {output_file.name} ({len(samples)} samples)")
                files_written += 1
                total_rows += len(samples)

            return files_written, total_rows

        except Exception as e:
            logger.error(f"Export failed: {e}")
            return 0, 0

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def import_station_json(
    station_id: str,
    json_dir: Optional[Path] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    database: str = "gps_health",
) -> Tuple[int, int, int]:
    """Convenience function to import JSON data for a station.

    Args:
        station_id: Station identifier
        json_dir: Directory with JSON files (auto-detected if None)
        start_date: Optional start date
        end_date: Optional end date
        database: Database name

    Returns:
        Tuple of (files_processed, total_rows, total_skipped)
    """
    # Auto-detect json_dir from data path if not provided
    if json_dir is None:
        from ..config.receivers_config import get_receivers_config

        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        # Try current month first
        now = datetime.now()
        month_abbr = now.strftime("%b").lower()
        year = now.strftime("%Y")

        json_dir = (
            Path(data_prepath) / year / month_abbr / station_id / "status_1hr" / "json"
        )

        if not json_dir.exists():
            logger.error(f"JSON directory not found: {json_dir}")
            return 0, 0, 0

    with HealthJsonImporter() as importer:
        importer.connect(database=database)
        return importer.import_directory(
            json_dir,
            station_id=station_id,
            start_date=start_date,
            end_date=end_date,
        )
