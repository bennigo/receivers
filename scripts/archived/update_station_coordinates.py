#!/usr/bin/env python3
"""
Update station coordinates in the database from XYZ coordinate file.

DEPRECATED: Use 'receivers db seed --only coordinates' instead.
First run scripts/populate_stations_llh.py to write LLH to stations.cfg,
then use 'receivers db seed --only coordinates' to update the database.

Reads XYZ coordinates from station_coord.xyz, converts to lat/lon using
the geofunc library, and updates the stations table in the database.

NOTE: For future reference, station coordinates should be extracted from TOS
(the Icelandic GPS station metadata system) rather than this file.

Usage:
    python update_station_coordinates.py [--dry-run]
"""

import argparse
import math
import sys
from pathlib import Path

# Add geofunc to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "geofunc" / "src"))

try:
    from geofunc.geo import xyzell
    HAS_GEOFUNC = True
except ImportError:
    HAS_GEOFUNC = False
    print("Warning: geofunc not available, using fallback conversion")

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


def xyz_to_llh_fallback(x, y, z):
    """
    Fallback XYZ to LLH conversion using WGS84 ellipsoid.
    Returns (latitude, longitude, height) in degrees and meters.
    """
    # WGS84 parameters
    a = 6378137.0  # Semi-major axis
    f = 1 / 298.257223563  # Flattening
    b = a * (1 - f)  # Semi-minor axis
    e2 = (a**2 - b**2) / a**2  # First eccentricity squared
    ep2 = (a**2 - b**2) / b**2  # Second eccentricity squared

    # Longitude
    lon = math.atan2(y, x)

    # Iterative calculation for latitude and height
    p = math.sqrt(x**2 + y**2)
    lat = math.atan2(z, p * (1 - e2))  # Initial approximation

    for _ in range(10):  # Iterate for convergence
        N = a / math.sqrt(1 - e2 * math.sin(lat)**2)
        h = p / math.cos(lat) - N
        lat = math.atan2(z, p * (1 - e2 * N / (N + h)))

    # Final height calculation
    N = a / math.sqrt(1 - e2 * math.sin(lat)**2)
    h = p / math.cos(lat) - N

    # Convert to degrees
    lat_deg = math.degrees(lat)
    lon_deg = math.degrees(lon)

    return lat_deg, lon_deg, h


def parse_xyz_file(filepath):
    """Parse the XYZ coordinate file."""
    coordinates = {}
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 4:
                try:
                    x = float(parts[0])
                    y = float(parts[1])
                    z = float(parts[2])
                    station = parts[3].upper()
                    coordinates[station] = (x, y, z)
                except (ValueError, IndexError):
                    continue
    return coordinates


def convert_coordinates(xyz_coords):
    """Convert XYZ coordinates to lat/lon."""
    llh_coords = {}
    for station, (x, y, z) in xyz_coords.items():
        if HAS_GEOFUNC:
            try:
                # xyzell actually returns (lat, lon, height) despite docstring saying otherwise
                result = xyzell([x, y, z], radians=False)
                lat, lon, height = result
                # Convert numpy floats to Python floats for database compatibility
                llh_coords[station] = (float(lat), float(lon), float(height))
            except Exception as e:
                print(f"Warning: geofunc failed for {station}, using fallback: {e}")
                lat, lon, height = xyz_to_llh_fallback(x, y, z)
                llh_coords[station] = (lat, lon, height)
        else:
            lat, lon, height = xyz_to_llh_fallback(x, y, z)
            llh_coords[station] = (lat, lon, height)
    return llh_coords


def update_database(llh_coords, dry_run=False):
    """Update the stations table with coordinates."""
    if not HAS_PSYCOPG2:
        print("Error: psycopg2 not available")
        return False

    conn = psycopg2.connect(
        host="localhost",
        database="gps_health",
        user="bgo"
    )
    cursor = conn.cursor()

    updated = 0
    skipped = 0

    for station, (lat, lon, height) in llh_coords.items():
        # Check if station exists in database
        cursor.execute("SELECT sid FROM stations WHERE sid = %s", (station,))
        if cursor.fetchone() is None:
            skipped += 1
            continue

        if dry_run:
            print(f"Would update {station}: lat={lat:.6f}, lon={lon:.6f}, height={height:.2f}")
        else:
            cursor.execute(
                """
                UPDATE stations
                SET latitude = %s, longitude = %s, height = %s
                WHERE sid = %s
                """,
                (lat, lon, height, station)
            )
        updated += 1

    if not dry_run:
        conn.commit()

    cursor.close()
    conn.close()

    print(f"Updated: {updated}, Skipped (not in DB): {skipped}")
    return True


def main():
    import warnings
    warnings.warn(
        "update_station_coordinates.py is deprecated. Use 'receivers db seed --only coordinates' instead.",
        DeprecationWarning,
        stacklevel=2
    )
    print("WARNING: This script is deprecated. Use 'receivers db seed --only coordinates' instead.\n")
    parser = argparse.ArgumentParser(description="Update station coordinates from XYZ file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without making changes")
    parser.add_argument("--xyz-file", default="/home/bgo/work/projects/gpslibrary/gps-config-data/station_coord.xyz",
                        help="Path to XYZ coordinate file")
    args = parser.parse_args()

    print(f"Reading coordinates from: {args.xyz_file}")
    xyz_coords = parse_xyz_file(args.xyz_file)
    print(f"Found {len(xyz_coords)} stations in coordinate file")

    print("Converting XYZ to lat/lon...")
    llh_coords = convert_coordinates(xyz_coords)

    # Show a few examples
    print("\nSample conversions:")
    for i, (station, (lat, lon, height)) in enumerate(list(llh_coords.items())[:5]):
        print(f"  {station}: lat={lat:.6f}, lon={lon:.6f}, height={height:.2f}m")

    print(f"\nUpdating database (dry_run={args.dry_run})...")
    update_database(llh_coords, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
