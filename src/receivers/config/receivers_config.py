"""Receivers configuration management.

This module handles loading and managing configuration for the receivers package,
including archive paths, session types, and receiver-specific settings.
"""

import ast
import configparser
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import gps_parser

    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False


class ReceiversConfig:
    """Configuration manager for receivers package.

    Loads configuration from receivers.cfg and provides structured access
    to archive paths, session types, and receiver-specific settings.
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration manager.

        Args:
            config_path: Optional path to receivers.cfg file
        """
        self.logger = logging.getLogger(__name__)
        self.config = configparser.ConfigParser()
        self.config_path = self._find_config_path(config_path)
        self._load_config()

    def _find_config_path(self, config_path: Optional[str] = None) -> str:
        """Find receivers.cfg configuration file.

        Args:
            config_path: Optional explicit path

        Returns:
            Path to configuration file

        Raises:
            FileNotFoundError: If configuration file not found
        """
        if config_path and os.path.isfile(config_path):
            return config_path

        # Check GPS_CONFIG_PATH environment variable first
        gps_config_dir = os.environ.get("GPS_CONFIG_PATH")
        if gps_config_dir:
            receivers_cfg = os.path.join(gps_config_dir, "receivers.cfg")
            if os.path.isfile(receivers_cfg):
                return receivers_cfg

        # Try gps_parser config directory
        if HAS_GPS_PARSER:
            try:
                parser_config = gps_parser.ConfigParser()
                gps_config_dir = parser_config.config_path
                receivers_cfg = os.path.join(gps_config_dir, "receivers.cfg")
                if os.path.isfile(receivers_cfg):
                    return receivers_cfg
            except Exception as e:
                self.logger.debug(f"Could not get config dir from gps_parser: {e}")

        # Try standard locations
        search_paths = [
            os.path.expanduser("~/.config/gpsconfig/receivers.cfg"),
            os.path.expanduser("~/.gpsconfig/receivers.cfg"),
            "./receivers.cfg",
            "../receivers.cfg",
        ]

        for path in search_paths:
            if os.path.isfile(path):
                return path

        # If not found, use default location
        default_path = os.path.expanduser("~/.config/gpsconfig/receivers.cfg")
        raise FileNotFoundError(
            f"receivers.cfg not found. Searched: {search_paths}. "
            f"Please create configuration at: {default_path}"
        )

    def _load_config(self) -> None:
        """Load configuration from receivers.cfg file."""
        try:
            self.config.read(self.config_path)
            self.logger.debug(f"Loaded receivers config from: {self.config_path}")
        except Exception as e:
            self.logger.error(f"Failed to load receivers config: {e}")
            raise

    def get_data_prepath(self) -> str:
        """Get base data directory path.

        Returns:
            Base directory path for data storage
        """
        try:
            data_prepath = self.config.get("archive_paths", "data_prepath")
            # Convert relative paths to absolute from project root
            if data_prepath.startswith("./"):
                # Get project root (where this config is being called from)
                project_root = os.getcwd()
                data_prepath = os.path.join(project_root, data_prepath[2:])
                data_prepath = os.path.abspath(data_prepath)
            return data_prepath
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Fallback to project-local tmp directory
            fallback = os.path.join(os.getcwd(), "tmp", "data")
            self.logger.warning(f"Using fallback data_prepath: {fallback}")
            return fallback

    def get_gps_config_data_repo(self) -> Optional[str]:
        """Return the configured gps-config-data clone path, or None.

        Read from ``[paths] gps_config_data_repo`` in receivers.cfg. This is the
        source-of-truth git repo that ``cfg ... --global`` writes + commits.
        Returns ``None`` when unset so the caller (``resolve_global_repo``) can
        apply its own env-var / hardcoded-default fallback chain.
        """
        try:
            value = self.config.get("paths", "gps_config_data_repo")
            return value.strip() or None
        except (configparser.NoSectionError, configparser.NoOptionError):
            return None

    def get_tos_corrections_repo(self) -> Optional[str]:
        """Return the configured gps-tos-corrections clone path, or None.

        Read from ``[paths] tos_corrections_repo`` in receivers.cfg — the local
        clone where TOS triage/correction files live (shared with tostools,
        which resolves the same key via ``archive.tos_corrections_dir``).
        Returns ``None`` when unset so the caller can apply the
        ``$TOS_TRIAGE_DIR`` → built-in-default fallback chain.
        """
        try:
            value = self.config.get("paths", "tos_corrections_repo")
            return value.strip() or None
        except (configparser.NoSectionError, configparser.NoOptionError):
            return None

    def get_sitelogs_repo(self) -> Optional[str]:
        """Return the configured gps-sitelogs clone path, or None.

        Read from ``[paths] sitelogs_repo`` in receivers.cfg — the local clone
        where generated IGS/M3G site logs are committed for EPOS dissemination.
        Returns ``None`` when unset so the caller can apply the built-in default
        (``~/git/gps-sitelogs``). Mirrors :meth:`get_tos_corrections_repo`.
        """
        try:
            value = self.config.get("paths", "sitelogs_repo")
            return value.strip() or None
        except (configparser.NoSectionError, configparser.NoOptionError):
            return None

    def get_catalog_hosts(self) -> list[str]:
        """Return the gps_health hosts the archive_catalog is written to.

        Read from ``[archive] catalog_hosts`` in receivers.cfg — a comma-separated
        list of gps_health hosts that MUST be kept identical (e.g.
        ``pgdev.vedur.is, rek-d01.vedur.is``). Archive catalog writes (reindex,
        archive-rm pruning) fan out to ALL of them so the operational catalog on
        rek-d01 and the reporting catalog on pgdev never diverge.

        Returns ``[]`` when unset — the caller then falls back to the single
        default gps_health connection (database.cfg host) and warns.
        """
        try:
            value = self.config.get("archive", "catalog_hosts")
        except (configparser.NoSectionError, configparser.NoOptionError):
            return []
        return [h.strip() for h in value.split(",") if h.strip()]

    def get_prepath(self) -> str:
        """DEPRECATED: Use get_data_prepath() instead.

        Kept for backward compatibility.
        """
        return self.get_data_prepath()

    def get_cold_archive_prepath(self) -> str:
        """Get the read-only base path for the long-term raw/RINEX archive.

        Distinct from :meth:`get_data_prepath`, which is the online working
        cache that receivers writes to. The cold archive is the long-term
        store consumed by historical-lookup tooling (e.g. tostools'
        RINEX-header-based device-history reconstruction, gap-fill for the
        device-warehouse work).

        Read-only. Callers must not write into this path.

        Returns:
            Cold-storage base path (e.g. ``/mnt/rawgpsdata`` on production,
            ``/mnt_data/rawgpsdata`` on developer laptops). When the entry
            is absent from cfg, probes known mount points and returns the
            first one that exists; falls back to the production default
            with a warning if no mount is detected.
        """
        try:
            cold = self.config.get("archive_paths", "cold_archive_prepath")
            if cold.startswith("./"):
                project_root = os.getcwd()
                cold = os.path.join(project_root, cold[2:])
                cold = os.path.abspath(cold)
            return cold
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Probe well-known mount points before giving up. Order is
            # production-first so a misconfigured prod host still resolves
            # to /mnt/rawgpsdata rather than silently picking the laptop
            # default.
            for cand in ("/mnt/rawgpsdata", "/mnt_data/rawgpsdata"):
                if os.path.isdir(cand):
                    self.logger.warning(
                        "cold_archive_prepath not set in cfg; "
                        f"using detected mount {cand}"
                    )
                    return cand
            fallback = "/mnt/rawgpsdata"
            self.logger.warning(
                "cold_archive_prepath not set in cfg and no mount detected; "
                f"using fallback {fallback}"
            )
            return fallback

    def get_tmp_dir(self) -> str:
        """Get temporary download directory path.

        Returns:
            Temporary directory path for downloads
        """
        try:
            tmp_dir = self.config.get("archive_paths", "tmp_dir")
            # Convert relative paths to absolute from project root
            if tmp_dir.startswith("./"):
                project_root = os.getcwd()
                tmp_dir = os.path.join(project_root, tmp_dir[2:])
                tmp_dir = os.path.abspath(tmp_dir)
            return tmp_dir
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Fallback to project-local tmp directory
            fallback = os.path.join(os.getcwd(), "tmp", "download")
            self.logger.warning(f"Using fallback tmp_dir: {fallback}")
            return fallback

    def get_archive_template(self) -> str:
        """Get archive path template.

        Returns:
            Archive path template with placeholders
        """
        try:
            return self.config.get("archive_paths", "archive_template")
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Fallback template
            return "{data_prepath}/%Y/#b/{station}/{session}/raw/{station}%Y%m%d%H00a{extension}"

    def get_session_types(self) -> Dict[str, Dict[str, Any]]:
        """Get session type definitions.

        Returns:
            Dictionary mapping session names to their properties
        """
        session_types = {}
        try:
            for session_name, session_config in self.config.items("session_types"):
                try:
                    # Parse CSV format: frequency,acquisition,description,file_frequency
                    parts = session_config.split(",")
                    if len(parts) >= 3:
                        session_data = {
                            "frequency": parts[0].strip(),
                            "acquisition": parts[1].strip(),
                            "description": parts[2].strip(),
                            "file_frequency": (
                                parts[3].strip() if len(parts) > 3 else "24hr"
                            ),
                        }
                        session_types[session_name] = session_data
                    else:
                        self.logger.warning(
                            f"Invalid session config format for {session_name}: {session_config}"
                        )
                except Exception as e:
                    self.logger.warning(
                        f"Could not parse session config for {session_name}: {e}"
                    )
                    continue
        except configparser.NoSectionError:
            # Fallback session types
            session_types = {
                "15s_24hr": {
                    "frequency": "1D",
                    "acquisition": "15s",
                    "description": "Daily 15-second data",
                },
                "1Hz_1hr": {
                    "frequency": "1H",
                    "acquisition": "1Hz",
                    "description": "Hourly 1Hz data",
                },
                "status_1hr": {
                    "frequency": "1H",
                    "acquisition": "status",
                    "description": "Hourly status data",
                },
            }
            self.logger.warning("Using fallback session types")

        return session_types

    def get_receiver_config(self, receiver_type: str) -> Dict[str, Any]:
        """Get configuration for specific receiver type.

        Args:
            receiver_type: Receiver type (e.g., 'septentrio', 'leica')

        Returns:
            Dictionary with receiver-specific configuration
        """
        receiver_config = {}

        # Get receiver defaults first
        try:
            for key, value in self.config.items("receiver_defaults"):
                try:
                    # Try to parse as Python literal (bool, int, etc.)
                    receiver_config[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    # Keep as string if not parseable
                    receiver_config[key] = value
        except configparser.NoSectionError:
            pass

        # Override with receiver-specific settings
        section_name = receiver_type.lower()
        try:
            for key, value in self.config.items(section_name):
                try:
                    # Try to parse as Python literal
                    receiver_config[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    # Keep as string if not parseable
                    receiver_config[key] = value
        except configparser.NoSectionError:
            self.logger.debug(
                f"No specific configuration found for receiver type: {receiver_type}"
            )

        return receiver_config

    def build_archive_path(
        self,
        station_id: str,
        session: str,
        dt,
        extension: str,
        session_letter: str = "a",
    ) -> str:
        """Build archive path for a specific file.

        DEPRECATED: Use BaseReceiver.build_path() instead for unified path building.
        This method is kept for backward compatibility but may be removed in future versions.

        Args:
            station_id: Station identifier
            session: Session type
            dt: datetime object
            extension: File extension (e.g., '.sbf.gz')
            session_letter: Session letter code (e.g., 'a', 'b', 'c')

        Returns:
            Complete archive path
        """
        template = self.get_archive_template()
        data_prepath = self.get_data_prepath()

        # Use gtimes to format the template with datetime
        try:
            import gtimes.timefunc as gt

            # Create template with our variables filled in
            filled_template = template.format(
                data_prepath=data_prepath,
                station=station_id,
                session=session,
                extension=extension,
                session_letter=session_letter,
            )

            # Use gtimes to handle the datetime formatting
            archive_paths = gt.datepathlist(
                filled_template,
                "1D",  # We're building for single datetime
                datelist=[dt],
                closed="both",
            )

            return archive_paths[0]

        except ImportError:
            # Fallback without gtimes
            self.logger.warning(
                "gtimes not available - using simple datetime formatting"
            )
            filled_template = template.format(
                data_prepath=data_prepath,
                station=station_id,
                session=session,
                extension=extension,
                session_letter=session_letter,
            )
            # Simple datetime substitution
            return dt.strftime(filled_template)

    def is_valid_session(self, session: str) -> bool:
        """Check if session type is valid.

        Args:
            session: Session type to check

        Returns:
            True if session is defined in configuration
        """
        session_types = self.get_session_types()
        return session in session_types

    def is_session_supported_by_receiver(
        self, receiver_type: str, session: str
    ) -> bool:
        """Check if a session type is supported by a specific receiver type.

        Args:
            receiver_type: Receiver type (e.g., 'polarx5', 'netr9', 'netrs', 'g10')
            session: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')

        Returns:
            True if the receiver type has a session_map entry for this session
        """
        receiver_config = self.get_receiver_config(receiver_type)
        # Session maps are stored as session_map_{session} (case-insensitive)
        session_key = f"session_map_{session.lower()}"
        return session_key in receiver_config

    def get_supported_sessions(self, receiver_type: str) -> list:
        """Get list of sessions supported by a specific receiver type.

        Args:
            receiver_type: Receiver type (e.g., 'polarx5', 'netr9')

        Returns:
            List of supported session names, in canonical casing.

        Session names are derived from receivers.cfg option keys, and
        ``configparser`` lower-cases every option name — so ``session_map_1Hz_1hr``
        reads back as ``session_map_1hz_1hr`` and the session would come out as
        ``1hz_1hr``. Everything else in the system (the CLI's ``--session``
        choices, the scheduler, archive path templates) uses ``1Hz_1hr``, so an
        exact-match check against the raw keys could never match and
        ``receivers download <PolaRX5 station> --session 1Hz_1hr`` was rejected
        as "not supported" for the entire Septentrio fleet. Map back through the
        receiver registry, which holds the canonical spelling.
        """
        from .receiver_registry import REGISTRY

        canonical = {
            session.lower(): session
            for capability in REGISTRY.values()
            for session in capability.sessions
        }

        receiver_config = self.get_receiver_config(receiver_type)
        sessions = []
        for key in receiver_config:
            if key.startswith("session_map_"):
                # Extract session name from key (e.g., "session_map_15s_24hr" -> "15s_24hr")
                session_name = key[len("session_map_") :]
                # Unknown sessions (a cfg-only session type) pass through as-is.
                sessions.append(canonical.get(session_name.lower(), session_name))
        return sessions

    def get_session_frequency(self, session: str) -> str:
        """Get frequency for session type.

        Args:
            session: Session type

        Returns:
            Frequency string (e.g., '1D', '1H')
        """
        session_types = self.get_session_types()

        # Handle case-insensitive lookup (configparser converts keys to lowercase)
        session_lower = session.lower()
        if session_lower in session_types:
            return session_types[session_lower].get("frequency", "1D")
        elif session in session_types:
            return session_types[session].get("frequency", "1D")
        return "1D"  # Default

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load_config()

    def get_rinex_default_naming(self) -> str:
        """Get default RINEX naming convention.

        Returns:
            'short' or 'long' naming convention (default: 'short')
        """
        try:
            naming = self.config.get("rinex", "default_naming")
            if naming.lower() in ("short", "long"):
                return naming.lower()
            self.logger.warning(
                f"Invalid rinex default_naming '{naming}', using 'short'"
            )
            return "short"
        except (configparser.NoSectionError, configparser.NoOptionError):
            return "short"  # Default to short naming

    def get_rinex_config(self) -> Dict[str, Any]:
        """Get all RINEX configuration settings.

        Returns:
            Dictionary with RINEX configuration options
        """
        rinex_config = {
            "default_naming": "short",
            "default_version": 3,
            "default_hatanaka": True,
            "default_compression": "gz",
            "apply_header_corrections": True,
            "use_tos_for_historical": True,
            "use_native_trimble": False,  # Requires Docker setup
            # NetRS receivers track L2 codeless, so native RINEX 3 conversion
            # emits the L2 range coded C2D, which GAMIT cannot map to P2 (every
            # observation is deleted with "no P2 range"). Pin NetRS to RINEX 2.11
            # (teqc) so the L2 range stays P2. Bound to receiver type: a station
            # upgraded off NetRS automatically returns to the default version.
            "netrs_rinex_version": 2,
            # THE global position-identity gate (metres): raw/header-derived
            # coordinates within this distance of the surveyed mark confirm
            # the station. One number for the converter identity gate,
            # archive-sort --check-station and the header-QC coordinate check.
            "position_gate_m": 10.0,
        }

        try:
            for key, value in self.config.items("rinex"):
                # Handle common boolean strings
                if value.lower() in ("true", "yes", "on", "1"):
                    rinex_config[key] = True
                elif value.lower() in ("false", "no", "off", "0"):
                    rinex_config[key] = False
                else:
                    try:
                        # Try to parse as Python literal (int, etc.)
                        rinex_config[key] = ast.literal_eval(value)
                    except (ValueError, SyntaxError):
                        # Keep as string if not parseable
                        rinex_config[key] = value
        except configparser.NoSectionError:
            self.logger.debug("No [rinex] section found, using defaults")

        return rinex_config

    def get_position_gate_m(self) -> float:
        """The global coordinate-identity tolerance (metres) — [rinex]
        position_gate_m, default 10. Config, not code (bgo 2026-07-06)."""
        try:
            return float(self.get_rinex_config().get("position_gate_m", 10.0))
        except (TypeError, ValueError):
            return 10.0

    def get_use_terminal_absence(self) -> bool:
        """Whether ``is_file_missing`` honours a TERMINAL file_absence as a skip.

        Default FALSE: terminal absence stays advisory (recorded, not skipped)
        until the served-gate (mig 061) is validated in production — flipping it
        prematurely risks skipping a whole station on an all-files-404 config
        error. Set ``[file_index] use_terminal_absence = true`` in receivers.cfg to
        activate, once the served-gate is trusted; the scheduler then stops
        re-fetching genuinely-gone files.
        """
        try:
            value = self.config.get("file_index", "use_terminal_absence")
            return value.strip().lower() in ("true", "yes", "on", "1")
        except (configparser.NoSectionError, configparser.NoOptionError):
            return False

    def get_storage_locations(self) -> list[Dict[str, Any]]:
        """Get storage location definitions from config.

        Reads [storage_locations] section from receivers.cfg. Each key is a
        location_id and the value is a comma-separated string:
            location_id = base_path, location_type, name [, is_primary]

        Example receivers.cfg:
            [storage_locations]
            local_archive = /home/bgo/tmp/gpsdata, local, Local development archive, true
            production_nfs = /mnt_data/gpsdata, nfs, Production NFS mount

        Returns:
            List of dicts with keys: location_id, base_path, location_type,
            name, is_primary, enabled
        """
        locations = []

        try:
            for location_id, value in self.config.items("storage_locations"):
                try:
                    parts = [p.strip() for p in value.split(",")]
                    if len(parts) < 3:
                        self.logger.warning(
                            f"Invalid storage_location format for {location_id}: "
                            f"expected 'base_path, type, name [, is_primary]'"
                        )
                        continue

                    base_path = os.path.expanduser(parts[0])
                    location_type = parts[1]
                    name = parts[2]
                    is_primary = (
                        parts[3].lower() in ("true", "yes", "1")
                        if len(parts) > 3
                        else False
                    )

                    # location_type is now a coarse legacy hint (the `protocol`
                    # column carries real transport semantics after mig 054), so
                    # accept any non-empty value rather than dropping registry
                    # rows the old CHECK forbade ('logical', 'remote', …).
                    if not location_type:
                        self.logger.warning(
                            f"Empty location_type for {location_id} — skipping"
                        )
                        continue

                    locations.append(
                        {
                            "location_id": location_id,
                            "base_path": base_path,
                            "location_type": location_type,
                            "name": name,
                            "is_primary": is_primary,
                            "enabled": True,
                        }
                    )

                except Exception as e:
                    self.logger.warning(
                        f"Could not parse storage_location {location_id}: {e}"
                    )

        except configparser.NoSectionError:
            # No [storage_locations] section — provide a sensible default
            # based on data_prepath
            data_prepath = self.get_data_prepath()
            locations.append(
                {
                    "location_id": "local_archive",
                    "base_path": data_prepath,
                    "location_type": "local",
                    "name": "Local archive",
                    "is_primary": True,
                    "enabled": True,
                }
            )

        return locations


def well_known_registry_locations(config: "ReceiversConfig") -> list[Dict[str, Any]]:
    """The unified-file-index registry rows (mig 054 / plan §3.1).

    One row per file server the receivers stack touches, beyond whatever the cfg
    ``[storage_locations]`` section defines: the local ring split into raw/rinex,
    the permanent IMO archive, the EPOS portal, and the receiver's own internal
    buffer (a logical upstream). ``base_path`` is NOT NULL, so remote/logical
    locations whose path is not a local mount carry a readable descriptor.
    Host/root_path are left NULL where unknown — enriched later (M4 rollout);
    the differential (M2) only needs the row to exist with its ``is_permanent``
    flag correct.
    """
    try:
        data_prepath = config.get_data_prepath()
    except Exception:  # noqa: BLE001 — best-effort; the ring rows still seed
        data_prepath = "(local ring)"

    # (location_id, name, base_path, location_type, protocol, host, root_path,
    #  is_primary, is_permanent)
    return [
        {
            "location_id": "local_raw",
            "name": "Local ring — raw",
            "base_path": data_prepath,
            "location_type": "local",
            "protocol": "local",
            "host": None,
            "root_path": data_prepath,
            "is_primary": False,
            "is_permanent": False,
        },
        {
            "location_id": "local_rinex",
            "name": "Local ring — rinex",
            "base_path": data_prepath,
            "location_type": "local",
            "protocol": "local",
            "host": None,
            "root_path": data_prepath,
            "is_primary": False,
            "is_permanent": False,
        },
        {
            "location_id": "imo_archive",
            "name": "IMO long-term archive (ananas via rawdata)",
            "base_path": "(imo long-term archive)",
            "location_type": "nfs",
            "protocol": "nfs-ro",
            "host": None,
            "root_path": None,
            "is_primary": False,
            "is_permanent": True,
        },
        {
            "location_id": "epos_portal",
            "name": "EPOS portal (data.epos-iceland.is)",
            "base_path": "(epos portal)",
            "location_type": "server",
            "protocol": "rsync",
            "host": None,
            "root_path": None,
            "is_primary": False,
            "is_permanent": False,
        },
        {
            "location_id": "receiver",
            "name": "Receiver internal buffer (logical upstream)",
            "base_path": "(receiver internal buffer)",
            "location_type": "logical",
            "protocol": "logical",
            "host": None,
            "root_path": None,
            "is_primary": False,
            "is_permanent": False,
        },
    ]


def _seed_storage_retention(conn, config_logger: logging.Logger) -> int:
    """Project scheduler.yaml ``[local_prune]`` retention into storage_retention.

    The yaml is the SINGLE source of truth for ring retention (see prune.py); this
    table is a query-friendly derived copy the differential (M2) reads instead of
    re-parsing yaml. Both local ring locations (raw + rinex) share the per-session
    ring floor. Idempotent (DO UPDATE) so each seed refreshes from the yaml.
    Returns the number of (location, session) rows written.
    """
    try:
        from ..scheduling.config_loader import load_scheduler_config

        prune = (load_scheduler_config() or {}).get("local_prune", {}) or {}
    except Exception as e:  # noqa: BLE001 — retention is optional at seed time
        config_logger.debug(f"No [local_prune] retention to seed: {e}")
        return 0

    retention = dict(prune.get("retention_days", {}) or {})
    emergency = dict(prune.get("emergency_retention_days", {}) or {})
    if not retention:
        return 0

    written = 0
    with conn.cursor() as cur:
        for session, days in retention.items():
            try:
                days_i = int(days)
            except (TypeError, ValueError):
                continue
            emerg = emergency.get(session)
            emerg_i = int(emerg) if emerg is not None else None
            for loc in ("local_raw", "local_rinex"):
                cur.execute(
                    """INSERT INTO storage_retention
                           (location_id, session_type, retention_days,
                            emergency_retention_days, updated_at)
                       VALUES (%s, %s, %s, %s, now())
                       ON CONFLICT (location_id, session_type) DO UPDATE SET
                           retention_days = EXCLUDED.retention_days,
                           emergency_retention_days = EXCLUDED.emergency_retention_days,
                           updated_at = now()""",
                    (loc, session, days_i, emerg_i),
                )
                written += 1
    return written


def seed_storage_locations(connection_string: Optional[str] = None) -> int:
    """Seed the storage_location registry + storage_retention (mig 054).

    Two sources merge into the registry:
      1. the cfg ``[storage_locations]`` section (environment-specific base paths);
      2. the well-known unified-file-index locations
         (:func:`well_known_registry_locations`).
    Both upsert with ``DO UPDATE`` so the enrichment columns (protocol/host/
    root_path/is_permanent) added in mig 054 are backfilled onto rows a prior
    ``DO NOTHING`` seed left thin. Then storage_retention is projected from
    scheduler.yaml ``[local_prune]``.

    Args:
        connection_string: PostgreSQL connection string. If None, uses env vars.

    Returns:
        Number of storage_location rows inserted (new rows only; updates and
        retention rows are logged, not counted).
    """
    config_logger = logging.getLogger(__name__)

    try:
        config = ReceiversConfig()
        cfg_locations = config.get_storage_locations()
    except Exception as e:
        config_logger.warning(f"Cannot load storage locations from config: {e}")
        return 0

    # Merge cfg locations with the well-known registry. cfg entries lack the mig
    # 054 columns → default them; well-known entries carry them explicitly. A
    # cfg entry for the same location_id wins on base_path (env-specific).
    merged: Dict[str, Dict[str, Any]] = {}
    for loc in well_known_registry_locations(config):
        merged[loc["location_id"]] = loc
    for loc in cfg_locations:
        loc.setdefault("protocol", loc.get("location_type"))
        loc.setdefault("host", None)
        loc.setdefault("root_path", loc.get("base_path"))
        loc.setdefault("is_permanent", False)
        merged[loc["location_id"]] = {**merged.get(loc["location_id"], {}), **loc}

    if not merged:
        return 0

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        conn = DatabaseConnectionFactory.get_connection(
            database="gps_health",
            connection_string=connection_string,
        )
    except Exception as e:
        config_logger.debug(
            f"Cannot connect to database for storage location seeding: {e}"
        )
        return 0

    inserted = 0
    try:
        with conn.cursor() as cur:
            for loc in merged.values():
                cur.execute(
                    """INSERT INTO storage_location
                        (location_id, name, base_path, location_type, protocol,
                         host, root_path, is_primary, is_permanent, enabled)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (location_id) DO UPDATE SET
                        name         = EXCLUDED.name,
                        location_type = EXCLUDED.location_type,
                        protocol     = COALESCE(EXCLUDED.protocol, storage_location.protocol),
                        host         = COALESCE(EXCLUDED.host, storage_location.host),
                        root_path    = COALESCE(EXCLUDED.root_path, storage_location.root_path),
                        is_permanent = EXCLUDED.is_permanent
                    RETURNING (xmax = 0) AS is_insert""",
                    (
                        loc["location_id"],
                        loc["name"],
                        loc["base_path"],
                        loc["location_type"],
                        loc.get("protocol"),
                        loc.get("host"),
                        loc.get("root_path"),
                        loc.get("is_primary", False),
                        loc.get("is_permanent", False),
                        loc.get("enabled", True),
                    ),
                )
                row = cur.fetchone()
                if row and row[0]:
                    inserted += 1

        retention_rows = _seed_storage_retention(conn, config_logger)
        conn.commit()
        if inserted:
            config_logger.info(f"Seeded {inserted} storage locations to database")
        if retention_rows:
            config_logger.info(
                f"Seeded {retention_rows} storage_retention rows from [local_prune]"
            )

    except Exception as e:
        config_logger.warning(f"Error seeding storage locations: {e}")
        conn.rollback()
    finally:
        conn.close()

    return inserted


# Global configuration instance
_global_config: Optional[ReceiversConfig] = None


def get_receivers_config() -> ReceiversConfig:
    """Get global receivers configuration instance.

    Returns:
        Shared ReceiversConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = ReceiversConfig()
    return _global_config


def reload_config() -> None:
    """Reload global configuration from file."""
    global _global_config
    if _global_config is not None:
        _global_config.reload()
    else:
        _global_config = ReceiversConfig()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via tempfile + ``os.replace``.

    Prevents the truncate-on-crash window in which a SIGKILL between
    ``write_text``'s open(W) and the actual write leaves the file at
    zero bytes (the scheduler's mtime watcher would then auto-inactive
    every station in the next tick).
    """
    import tempfile

    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


class _CfgLock:
    """``fcntl.flock`` context manager on a sidecar ``<path>.lock`` file.

    Serialises concurrent writers (multiple CLI invocations, scheduler
    reconciler vs. interactive ``move-device``) against the same
    stations.cfg. Non-recursive — callers must not nest the lock.
    """

    def __init__(self, path: Path) -> None:
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._fd: Optional[int] = None

    def __enter__(self) -> "_CfgLock":
        import fcntl

        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import fcntl

        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _update_cfg_field(cfg_path: Path, station_id: str, key: str, value: str) -> bool:
    """In-place update of one key=value line in a configparser-format file.

    Preserves comments, ordering, and formatting.  Returns True if modified.

    The read + compute + write cycle is held under ``_CfgLock`` and the
    write is atomic via tempfile + ``os.replace`` — concurrent writers
    are serialised, and a crash mid-write cannot truncate the file.
    """
    with _CfgLock(cfg_path):
        lines = cfg_path.read_text().splitlines(keepends=True)

        section_header = f"[{station_id}]"
        in_section = False
        key_line_idx = -1
        next_section_idx = len(lines)

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == section_header:
                in_section = True
                continue
            if in_section:
                if stripped.startswith("["):
                    next_section_idx = i
                    break
                parts = stripped.split("=", 1)
                if len(parts) == 2 and parts[0].strip().lower() == key.lower():
                    key_line_idx = i

        if not in_section:
            return False

        target_line = f"{key} = {value}\n"

        if key_line_idx >= 0:
            existing = lines[key_line_idx].split("=", 1)[1].strip().rstrip("\n\r")
            if existing == value:
                return False
            lines[key_line_idx] = target_line
        else:
            # Insert before next section, after last non-blank line in this section
            insert_idx = next_section_idx
            while insert_idx > 0 and not lines[insert_idx - 1].strip():
                insert_idx -= 1
            lines.insert(insert_idx, target_line)

        _atomic_write_text(cfg_path, "".join(lines))
        return True


def _remove_cfg_field(cfg_path: Path, station_id: str, key: str) -> bool:
    """Remove a key line from a station section in a configparser-format file.

    Preserves comments, ordering, and formatting.  Returns True if a line was
    removed, False if the key was not found. Lock + atomic-write semantics
    match :func:`_update_cfg_field`.
    """
    with _CfgLock(cfg_path):
        lines = cfg_path.read_text().splitlines(keepends=True)

        section_header = f"[{station_id}]"
        in_section = False
        key_line_idx = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == section_header:
                in_section = True
                continue
            if in_section:
                if stripped.startswith("["):
                    break
                parts = stripped.split("=", 1)
                if len(parts) == 2 and parts[0].strip().lower() == key.lower():
                    key_line_idx = i

        if key_line_idx < 0:
            return False

        del lines[key_line_idx]
        _atomic_write_text(cfg_path, "".join(lines))
        return True


def create_station_section(
    cfg_path: Path,
    sid: str,
    fields: Dict[str, str],
    header_comment: Optional[str] = None,
) -> None:
    """Append a new [SID] section to stations.cfg in one atomic write.

    Raises ValueError if the section already exists.
    header_comment, if given, is prepended as ``# …`` lines before the section.
    """
    import re

    with _CfgLock(cfg_path):
        text = cfg_path.read_text()
        if re.search(r"^\[" + re.escape(sid) + r"\]", text, re.MULTILINE):
            raise ValueError(f"Section [{sid}] already exists in {cfg_path}")

        lines: list[str] = [""]
        if header_comment:
            for part in header_comment.splitlines():
                lines.append(f"# {part}")
        lines.append(f"[{sid}]")
        for key, val in fields.items():
            lines.append(f"{key} = {val}")
        lines.append("")

        _atomic_write_text(cfg_path, text + "\n".join(lines) + "\n")


def update_station_identity_in_cfg(
    station_id: str,
    firmware_version: Optional[str] = None,
    receiver_model: Optional[str] = None,
    serial_number: Optional[str] = None,
) -> bool:
    """Persist receiver identity fields to stations.cfg for a station.

    Only writes fields that are provided and differ from current values.
    Returns True if any field was updated.
    """
    if not HAS_GPS_PARSER:
        return False

    try:
        import gps_parser as _gps

        cfg_path = Path(_gps.ConfigParser().get_stations_config_path())
    except Exception:
        return False

    updates: Dict[str, str] = {}
    if firmware_version:
        updates["receiver_firmware_version"] = firmware_version
    if receiver_model:
        # The PARAMETER is `receiver_model` because that is the key on the
        # probe identity dict, but the stations.cfg KEY is `receiver_type` —
        # see the FieldSpec in cfg/field_manifest.py, which is the authority on
        # cfg key names. This wrote "receiver_model" for years; nothing reads
        # that key, so every `health --update-cfg` receiver update silently
        # no-opped while leaving a stray key behind (6 stations had collected
        # one). Normalise through the manifest's own formatter too, so we write
        # the short cfg vocabulary ("PolaRX5") rather than an IGS-style name
        # ("SEPT POLARX5") that would break type detection; it passes unknown
        # values through unchanged.
        from ..cfg.field_manifest import _receiver_type_to_cfg

        updates["receiver_type"] = (
            _receiver_type_to_cfg(receiver_model) or receiver_model
        )
    if serial_number:
        updates["receiver_serial"] = serial_number

    results = [
        _update_cfg_field(cfg_path, station_id, k, v) for k, v in updates.items()
    ]
    return any(results)
