"""Database seeder for GPS receivers.

Consolidates all station data seeding into one module:
- seed_stations(): Station metadata from stations.cfg
- seed_coordinates(): Lat/lon/height from stations.cfg
- seed_areas(): Volcanic and regional areas from station_areas.yaml
- seed_storage_locations(): Delegates to existing receivers_config function

Replaces the separate scripts:
- scripts/sync_stations_to_db.py
- scripts/update_station_coordinates.py
- scripts/sync_areas_to_db.py
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Optional

from .connection import get_connection

logger = logging.getLogger(__name__)

# Project-relative config directory — the *bundled default* station_areas.yaml.
# The canonical copy lives in gps-config-data and is deployed to the runtime
# config dir (see :func:`resolve_areas_yaml`); this repo copy is the bootstrap
# fallback only.
CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"

# Sections in stations.cfg that are NOT station IDs
EXCLUDED_SECTIONS = {"DEFAULT", "DEFAULTS", "Configs", "PATHS", "FILES"}


def resolve_areas_yaml() -> Path:
    """Locate ``station_areas.yaml``, preferring the synced config over the repo.

    ``station_areas.yaml`` is config, not code: edit it in gps-config-data, push,
    and the sync timer deploys it to the runtime config dir — exactly like
    ``stations.cfg``. Resolution mirrors
    :func:`receivers.cfg.operations._resolve_cfg_path`:

        1. ``$GPS_CONFIG_DATA_REPO/station_areas.yaml`` (the source-of-truth
           clone, for laptop-side editing) when it exists;
        2. the gps_parser runtime config dir (usually
           ``~/.config/gpsconfig/``) — the sync-deployed copy on rek;
        3. the bundled :data:`CONFIG_DIR` default (bootstrap fallback).
    """
    repo = os.environ.get("GPS_CONFIG_DATA_REPO")
    if repo:
        candidate = Path(repo).expanduser() / "station_areas.yaml"
        if candidate.exists():
            return candidate
    try:
        import gps_parser  # type: ignore

        deployed = (
            Path(gps_parser.ConfigParser().get_stations_config_path()).parent
            / "station_areas.yaml"
        )
        if deployed.exists():
            return deployed
    except Exception:  # noqa: BLE001 — gps_parser absent/misconfigured → fall through
        pass
    return CONFIG_DIR / "defaults" / "station_areas.yaml"


def _is_station_id(section: str) -> bool:
    """Check if a config section name looks like a station ID."""
    return section not in EXCLUDED_SECTIONS and section.isupper() and len(section) == 4


def _seedable_station_ids(parser) -> list[str]:
    """Station sections to seed: 4-char IDs, minus passive stations.

    Passive (``station_role = passive``) sections are data-source-only
    descriptors for the GLOBK processing — not operated stations. Seeding
    them would silently inflate the gps_health station tables and every
    dashboard/fleet count built on them (see
    GLOBAL_SITES_investigation.md §4.4).
    """
    from ..config_utils import is_passive_role

    station_ids = []
    passive_skipped = 0
    for section in parser.config.sections():
        if not _is_station_id(section):
            continue
        if is_passive_role(parser.config.get(section, "station_role", fallback=None)):
            passive_skipped += 1
            continue
        station_ids.append(section)
    if passive_skipped:
        logger.info(
            "Seeder: skipped %d passive (data-source-only) stations",
            passive_skipped,
        )
    return station_ids


class Seeder:
    """Seeds the gps_health database with station metadata, coordinates, and areas."""

    def __init__(self, host_override: str | None = None) -> None:
        self.host_override = host_override

    def _get_conn(self):
        # Single-host: schema/seed writes must never fan out to the pgdev
        # mirror (a mirrored DDL/seed is a silent cross-host mutation).
        return get_connection(host_override=self.host_override, single_host=True)

    def seed_stations(self, dry_run: bool = False) -> dict[str, int]:
        """Seed stations from gps_parser ConfigParser.

        Reads ALL station sections (including external/inactive ones without
        receiver_type) using gps_parser.ConfigParser() directly, rather than
        get_station_config() which validates receiver_type as required.

        Uses UPSERT with COALESCE to preserve existing values.

        Returns:
            Dict with 'inserted', 'updated', 'skipped' counts.
        """
        try:
            import gps_parser
        except ImportError:
            logger.error("gps_parser not available — cannot seed stations")
            print("Error: gps_parser not available")
            return {"inserted": 0, "updated": 0, "skipped": 0}

        parser = gps_parser.ConfigParser()
        station_ids = _seedable_station_ids(parser)

        if not station_ids:
            print("No stations found in configuration")
            return {"inserted": 0, "updated": 0, "skipped": 0}

        conn = self._get_conn()
        inserted = updated = skipped = 0

        try:
            with conn.cursor() as cur:
                for sid in station_ids:
                    try:
                        station_info = parser.getStationInfo(sid)
                        if not station_info:
                            skipped += 1
                            continue

                        raw = station_info.get("station", {})

                        # Extract all fields (None if missing)
                        receiver_type = raw.get("receiver_type") or None
                        power_type = raw.get("power_type") or None
                        antenna_type = raw.get("antenna_type") or None
                        marker_name = raw.get("rinex_marker_name") or None
                        marker_number = raw.get("rinex_marker_number") or None
                        observer = raw.get("rinex_observer") or None
                        agency = raw.get("rinex_agency") or None
                        station_name = raw.get("station_name") or None
                        station_status = raw.get("station_status") or None
                        health_check = raw.get("health_check") or None

                        # Station owner logic (same as db_writer._ensure_station)
                        station_owner = raw.get("station_owner") or None
                        if not station_owner and agency and agency != "IMO":
                            station_owner = agency
                        if not station_owner:
                            station_owner = "IMO"

                        # Don't store SID as station_name
                        if station_name == sid:
                            station_name = None

                        # IP address — resolve hostname if needed
                        ip_address = None
                        ip_raw = raw.get("router_ip") or None
                        if ip_raw:
                            try:
                                ip_address = socket.gethostbyname(ip_raw)
                            except socket.gaierror:
                                ip_address = None

                        # HTTP port
                        http_port = None
                        http_port_raw = raw.get("receiver_httpport")
                        if http_port_raw is not None:
                            try:
                                http_port = int(http_port_raw)
                            except (ValueError, TypeError):
                                pass

                        # Coordinates from stations.cfg (if populated)
                        latitude = _safe_float(raw.get("latitude"))
                        longitude = _safe_float(raw.get("longitude"))
                        height = _safe_float(raw.get("height"))

                        if dry_run:
                            print(
                                f"  {'Update' if self._station_exists(cur, sid) else 'Insert'} "
                                f"{sid}: type={receiver_type}, power={power_type}"
                            )
                            updated += 1
                            continue

                        cur.execute(
                            """
                            INSERT INTO stations (
                                sid, receiver_type, power_type, antenna_type,
                                marker_name, marker_number, observer, agency,
                                ip_address, http_port, station_name, station_owner,
                                station_status, health_check,
                                latitude, longitude, height
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s,
                                %s::inet, %s, %s, %s, %s, %s,
                                %s, %s, %s
                            )
                            ON CONFLICT (sid) DO UPDATE SET
                                receiver_type = COALESCE(EXCLUDED.receiver_type, stations.receiver_type),
                                power_type = COALESCE(EXCLUDED.power_type, stations.power_type),
                                antenna_type = COALESCE(EXCLUDED.antenna_type, stations.antenna_type),
                                marker_name = COALESCE(EXCLUDED.marker_name, stations.marker_name),
                                marker_number = COALESCE(EXCLUDED.marker_number, stations.marker_number),
                                observer = COALESCE(EXCLUDED.observer, stations.observer),
                                agency = COALESCE(EXCLUDED.agency, stations.agency),
                                ip_address = COALESCE(EXCLUDED.ip_address, stations.ip_address),
                                http_port = COALESCE(EXCLUDED.http_port, stations.http_port),
                                station_name = COALESCE(EXCLUDED.station_name, stations.station_name),
                                station_owner = COALESCE(EXCLUDED.station_owner, stations.station_owner),
                                station_status = EXCLUDED.station_status,
                                health_check = EXCLUDED.health_check,
                                latitude = COALESCE(EXCLUDED.latitude, stations.latitude),
                                longitude = COALESCE(EXCLUDED.longitude, stations.longitude),
                                height = COALESCE(EXCLUDED.height, stations.height),
                                updated_at = NOW()
                            RETURNING (xmax = 0) AS is_insert
                        """,
                            (
                                sid,
                                receiver_type,
                                power_type,
                                antenna_type,
                                marker_name,
                                marker_number,
                                observer,
                                agency,
                                ip_address,
                                http_port,
                                station_name,
                                station_owner,
                                station_status,
                                health_check,
                                latitude,
                                longitude,
                                height,
                            ),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            inserted += 1
                        else:
                            updated += 1

                    except Exception as e:
                        logger.warning("Could not seed station %s: %s", sid, e)
                        skipped += 1
                        conn.rollback()

            # Suppress stations that are in the DB but no longer in stations.cfg.
            # Only touch active stations (station_status IS NULL) to avoid
            # overwriting deliberately set inactive/discontinued flags.
            suppressed = 0
            if not dry_run and station_ids:
                placeholders = ",".join(["%s"] * len(station_ids))
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE stations
                        SET station_status = 'suppressed', updated_at = NOW()
                        WHERE sid NOT IN ({placeholders})
                          AND station_status IS NULL
                        RETURNING sid
                        """,
                        station_ids,
                    )
                    suppressed_rows = cur.fetchall()
                    suppressed = len(suppressed_rows)
                    if suppressed:
                        sids = ", ".join(r[0] for r in suppressed_rows)
                        logger.warning(
                            "Suppressed %d station(s) no longer in stations.cfg: %s",
                            suppressed,
                            sids,
                        )
                        print(f"Suppressed (removed from cfg): {sids}")

            if not dry_run:
                conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        counts = {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "suppressed": suppressed,
        }
        print(
            f"Stations: {inserted} inserted, {updated} updated, {skipped} skipped, {suppressed} suppressed"
        )
        return counts

    def seed_coordinates(self, dry_run: bool = False) -> dict[str, int]:
        """Seed station coordinates from stations.cfg.

        Reads latitude, longitude, height fields directly from stations.cfg.
        Only updates stations that have coordinate data in the config.

        Returns:
            Dict with 'updated' and 'skipped' counts.
        """
        try:
            import gps_parser
        except ImportError:
            logger.error("gps_parser not available — cannot seed coordinates")
            print("Error: gps_parser not available")
            return {"updated": 0, "skipped": 0}

        parser = gps_parser.ConfigParser()
        station_ids = _seedable_station_ids(parser)

        conn = self._get_conn()
        updated = skipped = 0

        try:
            with conn.cursor() as cur:
                for sid in station_ids:
                    try:
                        station_info = parser.getStationInfo(sid)
                        if not station_info:
                            continue
                        raw = station_info.get("station", {})

                        lat = _safe_float(raw.get("latitude"))
                        lon = _safe_float(raw.get("longitude"))
                        height = _safe_float(raw.get("height"))

                        if lat is None or lon is None:
                            skipped += 1
                            continue

                        if dry_run:
                            print(
                                f"  {sid}: lat={lat:.6f}, lon={lon:.6f}, height={height:.2f}"
                            )
                            updated += 1
                            continue

                        cur.execute(
                            "UPDATE stations SET latitude = %s, longitude = %s, height = %s WHERE sid = %s",
                            (lat, lon, height, sid),
                        )
                        if cur.rowcount > 0:
                            updated += 1
                        else:
                            skipped += 1

                    except Exception as e:
                        logger.warning(
                            "Could not update coordinates for %s: %s", sid, e
                        )
                        skipped += 1

            if not dry_run:
                conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        counts = {"updated": updated, "skipped": skipped}
        print(f"Coordinates: {updated} updated, {skipped} skipped")
        return counts

    def seed_areas(
        self,
        areas_file: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Seed station areas from station_areas.yaml.

        Clears existing area data and reloads from YAML.
        Uses ON CONFLICT DO NOTHING for member inserts.

        Args:
            areas_file: Path to station_areas.yaml. Defaults to config/station_areas.yaml.
            dry_run: Show what would be done without changes.

        Returns:
            Dict with 'areas' and 'members' counts.
        """
        import yaml

        yaml_path = areas_file or resolve_areas_yaml()
        if not yaml_path.exists():
            logger.error("Areas file not found: %s", yaml_path)
            print(f"Error: {yaml_path} not found")
            return {"areas": 0, "members": 0}

        config = yaml.safe_load(yaml_path.read_text())
        area_count = 0
        member_count = 0

        if dry_run:
            for area_type in ("volcanic_areas", "regional_areas"):
                type_label = area_type.replace("_areas", "")
                for area_id, area_data in config.get(area_type, {}).items():
                    stations = area_data.get("stations", [])
                    print(f"  {type_label}/{area_id}: {len(stations)} stations")
                    area_count += 1
                    member_count += len(stations)
            print(f"Areas: {area_count} areas, {member_count} members (dry run)")
            return {"areas": area_count, "members": member_count}

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Clear existing data
                cur.execute("DELETE FROM station_area_members")
                cur.execute("DELETE FROM station_areas")

                for area_type_key, db_type in [
                    ("volcanic_areas", "volcanic"),
                    ("regional_areas", "regional"),
                ]:
                    for area_id, area_data in config.get(area_type_key, {}).items():
                        cur.execute(
                            """
                            INSERT INTO station_areas (area_id, area_name, area_type, description)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (area_id) DO UPDATE SET
                                area_name = EXCLUDED.area_name,
                                description = EXCLUDED.description
                        """,
                            (
                                area_id,
                                area_data["name"],
                                db_type,
                                area_data.get("description", ""),
                            ),
                        )
                        area_count += 1

                        for station_entry in area_data.get("stations", []):
                            sid = (
                                station_entry.split()[0]
                                if isinstance(station_entry, str)
                                else station_entry
                            )
                            cur.execute(
                                """
                                INSERT INTO station_area_members (area_id, sid)
                                VALUES (%s, %s) ON CONFLICT DO NOTHING
                            """,
                                (area_id, sid),
                            )
                            member_count += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        counts = {"areas": area_count, "members": member_count}
        print(f"Areas: {area_count} areas, {member_count} members seeded")
        return counts

    def seed_storage_locations(self) -> int:
        """Seed storage locations from receivers.cfg.

        Delegates to the existing seed_storage_locations() function.

        Returns:
            Number of locations inserted.
        """
        try:
            from ..config.receivers_config import seed_storage_locations

            count = seed_storage_locations()
            if count:
                print(f"Storage locations: {count} inserted")
            else:
                print("Storage locations: all already exist (0 inserted)")
            return count
        except Exception as e:
            logger.warning("Could not seed storage locations: %s", e)
            print(f"Storage locations: error — {e}")
            return 0

    def seed_all(self, dry_run: bool = False) -> dict:
        """Run all seed operations in order.

        Returns:
            Dict with results from each seed operation.
        """
        results: dict = {}

        print("\n--- Seeding stations ---")
        results["stations"] = self.seed_stations(dry_run=dry_run)

        print("\n--- Seeding coordinates ---")
        results["coordinates"] = self.seed_coordinates(dry_run=dry_run)

        print("\n--- Seeding areas ---")
        results["areas"] = self.seed_areas(dry_run=dry_run)

        if not dry_run:
            print("\n--- Seeding storage locations ---")
            results["storage_locations"] = self.seed_storage_locations()

        return results

    @staticmethod
    def _station_exists(cur, sid: str) -> bool:
        """Check if a station exists in the database."""
        cur.execute("SELECT 1 FROM stations WHERE sid = %s", (sid,))
        return cur.fetchone() is not None


def _safe_float(value) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
