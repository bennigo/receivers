#!/usr/bin/env python3
"""Sync station_areas.yaml to database.

DEPRECATED: Use 'receivers db seed --only areas' instead.
"""
import warnings
warnings.warn(
    "sync_areas_to_db.py is deprecated. Use 'receivers db seed --only areas' instead.",
    DeprecationWarning,
    stacklevel=2
)
print("WARNING: This script is deprecated. Use 'receivers db seed --only areas' instead.\n")
import yaml
import psycopg2

# Load YAML
with open('/home/bgo/work/projects/gpslibrary/receivers/config/station_areas.yaml') as f:
    config = yaml.safe_load(f)

conn = psycopg2.connect(
    host="localhost", database="gps_health",
    user="bgo"
)
cursor = conn.cursor()

# Clear existing data
cursor.execute("DELETE FROM station_area_members")
cursor.execute("DELETE FROM station_areas")
print("Cleared existing area data")

# Insert volcanic areas
for area_id, area_data in config.get('volcanic_areas', {}).items():
    cursor.execute("""
        INSERT INTO station_areas (area_id, area_name, area_type, description)
        VALUES (%s, %s, 'volcanic', %s)
        ON CONFLICT (area_id) DO UPDATE SET area_name = EXCLUDED.area_name, description = EXCLUDED.description
    """, (area_id, area_data['name'], area_data.get('description', '')))
    
    for sid in area_data.get('stations', []):
        # Extract just the station ID (remove comments)
        sid = sid.split()[0] if isinstance(sid, str) else sid
        cursor.execute("""
            INSERT INTO station_area_members (area_id, sid) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, (area_id, sid))
    print(f"  volcanic/{area_id}: {len(area_data.get('stations', []))} stations")

# Insert regional areas
for area_id, area_data in config.get('regional_areas', {}).items():
    cursor.execute("""
        INSERT INTO station_areas (area_id, area_name, area_type, description)
        VALUES (%s, %s, 'regional', %s)
        ON CONFLICT (area_id) DO UPDATE SET area_name = EXCLUDED.area_name, description = EXCLUDED.description
    """, (area_id, area_data['name'], area_data.get('description', '')))
    
    for sid in area_data.get('stations', []):
        sid = sid.split()[0] if isinstance(sid, str) else sid
        cursor.execute("""
            INSERT INTO station_area_members (area_id, sid) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, (area_id, sid))
    print(f"  regional/{area_id}: {len(area_data.get('stations', []))} stations")

conn.commit()
cursor.close()
conn.close()
print("\nSync complete!")
