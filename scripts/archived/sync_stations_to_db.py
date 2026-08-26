#!/usr/bin/env python3
"""
Sync all stations from stations.cfg to the database.

DEPRECATED: Use 'receivers db seed --only stations' instead.
This script is kept for backward compatibility but will be removed
in a future release.

This ensures all configured stations appear in the Grafana map,
even if they haven't been polled for health data yet. Stations
without health data will appear as grey markers.

Usage:
    python sync_stations_to_db.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

# Add gps_parser to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "gps_parser" / "src"))

try:
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    print("Error: gps_parser not available")

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


def get_stations_from_config():
    """Get all stations from stations.cfg with their metadata.

    Note: Coordinates are not stored in stations.cfg - they come from
    the XYZ coordinate file and are already in the database.
    """
    if not HAS_GPS_PARSER:
        return {}

    parser = gps_parser.ConfigParser()
    stations = {}

    # Get station list from config sections
    # Filter: only uppercase 4-letter station IDs, excluding config sections
    excluded_sections = {'DEFAULT', 'DEFAULTS', 'Configs', 'PATHS', 'FILES'}
    station_ids = [
        s for s in parser.config.sections()
        if s not in excluded_sections and s.isupper() and len(s) == 4
    ]

    for sid in station_ids:
        try:
            station_info = parser.getStationInfo(sid)
            if station_info:
                # Station data is nested under 'station' key
                station_data = station_info.get("station", {})
                stations[sid] = {
                    "receiver_type": station_data.get("receiver_type"),
                    "power_type": station_data.get("power_type"),
                }
        except Exception as e:
            print(f"Warning: Could not get info for {sid}: {e}")

    return stations


def sync_to_database(stations, dry_run=False):
    """Insert/update stations in the database."""
    if not HAS_PSYCOPG2:
        print("Error: psycopg2 not available")
        return False

    conn = psycopg2.connect(
        host="localhost",
        database="gps_health",
        user="bgo"
    )
    cursor = conn.cursor()

    inserted = 0
    updated = 0
    skipped = 0

    for sid, data in stations.items():
        # Check if station exists
        cursor.execute("SELECT sid FROM stations WHERE sid = %s", (sid,))
        exists = cursor.fetchone() is not None

        receiver_type = data.get("receiver_type") or "unknown"
        power_type = data.get("power_type")

        if dry_run:
            if exists:
                print(f"Would update {sid}: type={receiver_type}, power_type={power_type}")
                updated += 1
            else:
                print(f"Would insert {sid}: type={receiver_type}, power_type={power_type}")
                inserted += 1
        else:
            if exists:
                # Update existing station (only metadata, preserve coordinates)
                cursor.execute(
                    """
                    UPDATE stations
                    SET receiver_type = COALESCE(%s, receiver_type),
                        power_type = COALESCE(%s, power_type)
                    WHERE sid = %s
                    """,
                    (receiver_type, power_type, sid)
                )
                updated += 1
            else:
                # Insert new station (no coordinates - will show as grey on map)
                cursor.execute(
                    """
                    INSERT INTO stations (sid, receiver_type, power_type)
                    VALUES (%s, %s, %s)
                    """,
                    (sid, receiver_type, power_type)
                )
                inserted += 1

    if not dry_run:
        conn.commit()

    cursor.close()
    conn.close()

    print(f"\nSummary: {inserted} inserted, {updated} updated, {skipped} skipped")
    return True


def main():
    import warnings
    warnings.warn(
        "sync_stations_to_db.py is deprecated. Use 'receivers db seed --only stations' instead.",
        DeprecationWarning,
        stacklevel=2
    )
    print("WARNING: This script is deprecated. Use 'receivers db seed --only stations' instead.\n")
    parser = argparse.ArgumentParser(description="Sync stations from stations.cfg to database")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    print("Loading stations from stations.cfg...")
    stations = get_stations_from_config()
    print(f"Found {len(stations)} stations in configuration")

    if not stations:
        print("No stations found")
        return 1

    print(f"\nSyncing to database (dry_run={args.dry_run})...")
    success = sync_to_database(stations, dry_run=args.dry_run)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
