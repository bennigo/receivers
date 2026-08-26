"""Configuration utilities for the receivers package.

This module provides utility functions for retrieving station configurations
from gps_parser with complete integration and no hardcoded fallbacks.
All configuration data comes from the centralized gps_parser package.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import gtimes for path construction
try:
    import gtimes.timefunc as gt
except ImportError:
    raise ImportError(
        "gtimes package not found. Please install gtimes: pip install gtimes"
    )

# Import gps_parser with proper error handling
try:
    import gps_parser
except ImportError:
    try:
        # Try with path adjustment for development
        sys.path.append("../gps_parser/src")
        import gps_parser
    except ImportError:
        raise ImportError(
            "gps_parser package not found. Please install gps_parser or set up your environment:\n"
            "1. cd ../gps_parser && pip install -e .\n"
            "2. OR: export PYTHONPATH=../gps_parser/src:$PYTHONPATH\n"
            "3. Ensure config files exist: ~/.config/gpsconfig/stations.cfg"
        )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Passive-station schema (stations.cfg role/flag fields)
# ---------------------------------------------------------------------------
# station_role = active | passive marks data-source-only stations
# (reference-frame ties / regional context from the GLOBK processing) that
# IMO does not operate. Every consumer that enumerates ALL stations.cfg
# sections must skip passive entries: scheduler load, DB seeder, cfg
# reconcile. See gpslibrary/GLOBAL_SITES_investigation.md §4.4.
#
# The role parser is CANONICAL in gps_parser (single source of truth,
# fail-open to 'active' on unknown values) — re-exported here so
# receivers-internal callers keep one import site.
#
# NOTE: distinct from the pre-existing ``health_check = passive`` key
# ("operated, but don't actively poll health") — unrelated concept.

from gps_parser import (  # noqa: E402  (after the guarded gps_parser import)
    STATION_ROLE_PASSIVE,
    parse_station_role,
)

_BOOL_TRUE = {"true", "yes", "1", "on"}
_BOOL_FALSE = {"false", "no", "0", "off"}


def is_passive_role(value: Any) -> bool:
    """True when a raw ``station_role`` value marks a passive station."""
    return parse_station_role(value) == STATION_ROLE_PASSIVE


def _parse_cfg_bool(value: Any, default: bool) -> bool:
    """Lenient bool parse for station flags (bad values → default + warn)."""
    text = str(value or "").split("#")[0].strip().lower()
    if not text:
        return default
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    logger.warning("Not a boolean config value %r — using default %s", value, default)
    return default


def _learned_ftp_mode_override(station_id: str) -> Optional[str]:
    """Look up an open ftp_mode discrepancy for this station, return its observed value.

    Returns None when no observation exists, when the cfg_discrepancy table
    is unavailable (fresh install / migration not applied), or when the
    discrepancy_log machinery isn't importable. All failure modes fall
    through silently — station-config loading must never break because of
    this lookup.
    """
    try:
        from .cfg import discrepancy_log as _dlog  # type: ignore
    except Exception:
        return None

    try:
        records = _dlog.list_open(station_ids=[station_id], cfg_keys=["ftp_mode"])
    except Exception:
        return None

    if not records:
        return None
    # The receiver_value column carries the observed working mode (active/passive).
    return records[0].receiver_value


_AGENCY_RESOLVER: Any = None


def _resolve_operator_header(station_operator: Optional[str]) -> tuple[str, str]:
    """RINEX ``(observer, agency)`` for a ``station_operator`` code via agencies.yaml.

    This is the single de-hardcoded source for the header OBSERVER / AGENCY: the
    cfg ``station_operator`` code (``IMO`` / ``NATT`` / ``IES``) resolves to the
    generic team observer (``GNSSatIMO`` …) and the operating agency's English
    name — never the retired personal-initial ``rinex_observer`` / ``rinex_agency``
    fields. Falls back to IMO (the fleet operator) when the code is missing or
    unknown, so a station without ``station_operator`` still gets a generic
    observer, not initials. Never raises (config is optional infra).
    """
    global _AGENCY_RESOLVER
    info = None
    try:
        if _AGENCY_RESOLVER is None:
            from .dissemination.agencies import AgencyResolver

            _AGENCY_RESOLVER = AgencyResolver.load()
        info = _AGENCY_RESOLVER.resolve_by_code(station_operator)
        if info is None:
            info = (
                _AGENCY_RESOLVER.resolve_by_code("IMO")
                or _AGENCY_RESOLVER.operator_default()
            )
    except Exception as exc:  # noqa: BLE001 - agencies.yaml optional
        logger.debug(
            "operator header resolution failed for %r: %s", station_operator, exc
        )
    if info is None:
        return "GNSSatIMO", "Icelandic Meteorological Office"
    return (
        (info.observer or "GNSSatIMO").strip(),
        info.rinex_agency or "Icelandic Meteorological Office",
    )


def get_station_config(
    station_id: str, *, silent: bool = False
) -> Optional[Dict[str, Any]]:
    """Get complete station configuration from gps_parser.

    Args:
        station_id: Station identifier (e.g., 'ELDC', 'ORFC')

    Returns:
        Complete station configuration dictionary with all settings
        structured for receivers package compatibility, or None if not found

    Configuration includes:
        - Basic station info (name, type, connection details)
        - Timeout configuration (from TIMEOUT_CATEGORIES)
        - FTP mode configuration (from NETWORK_RULES or explicit)
        - System paths (from PATHS section)
        - Default values (from DEFAULTS section)
    """
    try:
        # Initialize gps_parser ConfigParser
        config_parser = gps_parser.ConfigParser()

        # Get raw station information
        station_info = config_parser.getStationInfo(station_id)
        if not station_info or "station" not in station_info:
            (logger.debug if silent else logger.error)(
                f"Station {station_id} not found in gps_parser configuration"
            )
            return None

        raw_config = station_info["station"]

        # Validate required fields (receiver_ftpport is optional - Trimble uses HTTP)
        required_fields = ["router_ip", "receiver_type"]
        missing_fields = [field for field in required_fields if field not in raw_config]
        if missing_fields:
            if raw_config.get("health_check") == "passive":
                logger.debug(
                    f"Station {station_id} is passive (no direct connection): skipping"
                )
            else:
                logger.error(
                    f"Station {station_id} missing required fields: {missing_fields}"
                )
            return None

        # Get enhanced configuration data from gps_parser
        router_ip = raw_config["router_ip"]

        # Get timeout configuration
        timeout_config = config_parser.getStationTimeout(station_id)

        # Get FTP mode
        ftp_mode = config_parser.getStationFtpMode(station_id, router_ip)

        # Apply runtime-observed override from cfg_discrepancy. The download
        # path records ftp_mode there with detected_by='ftp_handshake' when
        # the live handshake reveals cfg disagrees with what actually works.
        # Honoring the open observation here means subsequent runs start in
        # the working mode without repeating the passive→active dance.
        # Failures (DB unavailable, etc.) silently fall through to the cfg-
        # derived value — never blocks station-config loading.
        observed_ftp_mode = _learned_ftp_mode_override(station_id)
        if observed_ftp_mode and observed_ftp_mode != ftp_mode:
            ftp_mode = observed_ftp_mode

        # Get system paths - USE RECEIVERS.CFG AS SINGLE SOURCE OF TRUTH
        # Import ReceiversConfig to read from receivers.cfg (not postprocess.cfg!)
        from .config.receivers_config import get_receivers_config

        receivers_config = get_receivers_config()
        data_prepath = receivers_config.get_prepath()  # From receivers.cfg

        # Tool paths still from gps_parser (postprocess.cfg) until migrated
        bin2asc_path = config_parser.getSystemPath("bin2asc_path")
        receiver_base_path = config_parser.getSystemPath("receiver_base_path")

        # Get default values
        default_session = config_parser.getDefaultValue("default_session")
        default_compression = config_parser.getDefaultValue("default_compression")
        default_days_back = config_parser.getDefaultValue("default_days_back")

        # Resolve the RINEX OBSERVER / AGENCY from the operating agency
        # (station_operator → agencies.yaml), NOT the retired personal-initial
        # rinex_observer/rinex_agency cfg fields. Single source for every consumer
        # (convert path, corrector config path). Falls back to IMO.
        _observer, _agency = _resolve_operator_header(
            raw_config.get("station_operator")
        )

        # Create comprehensive configuration structure
        station_config = {
            # Basic station information
            "station_id": station_id,
            "station_name": raw_config.get("station_name", station_id),
            "receiver_type": raw_config["receiver_type"],
            # Network configuration
            "router": {
                "ip": router_ip,
                "type": raw_config.get("router_type", ""),
                "ftp_mode": ftp_mode,
            },
            # Receiver configuration
            "receiver": {
                "type": raw_config["receiver_type"],
                "ftpport": raw_config.get(
                    "receiver_ftpport"
                ),  # Optional, None if not configured
                "httpport": raw_config.get("receiver_httpport", "8060"),
                "controlport": raw_config.get("receiver_controlport", "28784"),
                # Authentication credentials (for HTTP Basic Auth, FTP login, etc.)
                "user": raw_config.get("receiver_user", ""),
                "pwd": raw_config.get("receiver_pwd", ""),
                # Firmware bug handling
                "firmware_underscore_pad": raw_config.get(
                    "receiver_firmware_underscore_pad", ""
                ).lower()
                in ["true", "1", "yes"],
                # Per-station path override (e.g., VARG uses %Y%m/%d since Jan 2026)
                "remote_date_format": raw_config.get("receiver_remote_date_format", ""),
                # Per-station storage root override: replaces the receiver-type
                # default (netr9 /Internal/, netrs /download/) for a station that
                # logs elsewhere, e.g. AKUR on /External/. Empty ⇒ use the default.
                "base_path": raw_config.get("receiver_base_path", ""),
                # Explicit NetR5 CACHEDIR download prefix override. Normally
                # auto-discovered; this is the rarely-used escape hatch. Distinct
                # key from base_path (which is the storage root) so the two
                # path pieces never collide.
                "cachedir_prefix": raw_config.get("receiver_cachedir_prefix", ""),
            },
            # Connection and timing configuration
            "connection": {
                "type": raw_config.get("connection_type", ""),
                "timeouts": timeout_config,
            },
            # System paths
            "paths": {
                "data_prepath": data_prepath,
                "bin2asc_path": bin2asc_path,
                "receiver_base_path": receiver_base_path,
            },
            # Default values
            "defaults": {
                "session": default_session,
                "compression": default_compression,
                "days_back": default_days_back,
            },
            # RINEX header metadata (from teqc configs)
            "rinex": {
                "marker_name": raw_config.get("rinex_marker_name", station_id),
                # No station-id fallback: MARKER NUMBER carries the IERS DOMES only
                # (the corrector's domes_or_skip guard strips a non-DOMES value).
                "marker_number": raw_config.get("rinex_marker_number", ""),
                # Resolved from station_operator via agencies.yaml (above), not the
                # retired rinex_observer/rinex_agency personal-initial fields.
                "observer": _observer,
                "agency": _agency,
                "run_by": raw_config.get("rinex_run_by", "") or _observer,
                "config_valid_from": raw_config.get("rinex_config_valid_from", ""),
            },
            # Antenna information (from teqc configs)
            "antenna": {
                "type": raw_config.get("antenna_type", ""),
                "radome": raw_config.get("antenna_radome", "NONE"),
                "serial": raw_config.get("antenna_serial", ""),
                "height": float(raw_config.get("antenna_height", 0) or 0),
                "east": float(raw_config.get("antenna_east", 0) or 0),
                "north": float(raw_config.get("antenna_north", 0) or 0),
            },
            # Station-level properties (unprefixed fields from stations.cfg)
            "power_type": raw_config.get("power_type", "battery"),
            "ntrip_importance": raw_config.get("ntrip_importance"),
            # Passive-station schema (see module header): role + flags
            "station_role": parse_station_role(raw_config.get("station_role")),
            "is_reference_site": _parse_cfg_bool(
                raw_config.get("is_reference_site"), False
            ),
            "is_in_iceland": _parse_cfg_bool(raw_config.get("is_in_iceland"), True),
            "station_status": raw_config.get("station_status"),
            "health_check": raw_config.get("health_check"),
            "station_owner": raw_config.get("station_owner"),
            "station_operator": raw_config.get("station_operator"),
            # Per-station FTP credentials (override anonymous login; used by fw 5.7+)
            "ftp_username": raw_config.get("ftp_username", ""),
            "ftp_password": raw_config.get("ftp_password", ""),
        }

        # Surface every raw stations.cfg key that the typed structure
        # didn't already claim. cfg reconcile / health identity drift
        # detection / future audit tooling all need to read fields like
        # receiver_serial, receiver_firmware_version, latitude,
        # longitude, height directly — they do not appear anywhere in
        # the nested layout above. setdefault preserves the typed
        # structure on overlap.
        for raw_key, raw_val in raw_config.items():
            station_config.setdefault(raw_key, raw_val)

        logger.debug(f"Successfully loaded configuration for {station_id}")
        return station_config

    except Exception as e:
        (logger.debug if silent else logger.error)(
            f"Failed to get configuration for {station_id}: {e}"
        )
        return None


def get_all_station_configs() -> Dict[str, Dict[str, Any]]:
    """Get configurations for all stations.

    Lives here rather than in ``cli/main.py`` because it is fleet *policy*, not
    presentation: which stations exist for the scheduler to operate on. The
    scheduling package imported it from the CLI module in ~8 places, which meant
    every APScheduler worker and RINEX pool child pulled in a 7,000-line command
    module (and its import-time side effects) to obtain one function. Same
    reasoning that already moved ``get_station_config`` here.

    ``cli.main`` re-exports this name, so existing imports — and the many tests
    that patch ``receivers.cli.main.get_all_station_configs`` by dotted path —
    keep working unchanged.

    Returns:
        Dictionary mapping station_id to configuration
    """
    try:
        import configparser

        parser = gps_parser.ConfigParser()
        config = configparser.ConfigParser()
        config.read(parser.get_stations_config_path())

        stations = {}
        passive_skipped = 0
        for section in config.sections():
            # Passive (data-source-only) stations are never operated: skip
            # before load so they generate neither jobs nor per-station
            # error logging. See GLOBAL_SITES_investigation.md §4.4.
            if is_passive_role(config.get(section, "station_role", fallback=None)):
                passive_skipped += 1
                continue
            try:
                station_config = get_station_config(section)
                if station_config:
                    stations[section] = station_config
            except Exception as e:
                logger.debug(f"Could not load config for {section}: {e}")

        if passive_skipped:
            logger.debug(
                f"Skipped {passive_skipped} passive (data-source-only) stations"
            )
        return stations
    except Exception as e:
        logger.error(f"Could not load all station configurations: {e}")
        return {}


def resolve_receiver_endpoint(args: Any, station_id: str) -> Optional[Dict[str, Any]]:
    """Return station config, using a direct-connection stub when --host is given.

    When *args.host* is set the function bypasses stations.cfg and builds an
    ad-hoc config with native receiver ports (21/FTP, 80/HTTP, 28784/control)
    instead of the router-forwarded ports used in the operational network.
    The dict carries ``_adhoc: True`` so callers can suppress side-effects such
    as DB writes and stations.cfg updates.

    When *args.host* is not set this is a transparent pass-through to
    :func:`get_station_config`.
    """
    host = getattr(args, "host", None)
    if not host:
        return get_station_config(station_id)

    receiver_type = getattr(args, "receiver_type", None) or "PolaRX5"
    control_port = int(getattr(args, "port", None) or 28784)

    return {
        "station_id": station_id,
        "station_name": station_id,
        "receiver_type": receiver_type,
        "router": {
            "ip": host,
            "type": "direct",
            "ftp_mode": "passive",
        },
        "receiver": {
            "type": receiver_type,
            "ftpport": "21",
            "httpport": "80",
            "controlport": str(control_port),
            "user": "",
            "pwd": "",
            "firmware_underscore_pad": False,
            "remote_date_format": "",
            "base_path": "",
            "cachedir_prefix": "",
        },
        "connection": {"type": "direct", "timeouts": {}},
        "paths": {
            "data_prepath": "",
            "bin2asc_path": "",
            "receiver_base_path": "",
        },
        "defaults": {
            "session": "15s_24hr",
            "compression": ".gz",
            "days_back": 10,
        },
        "rinex": {
            "marker_name": station_id,
            "marker_number": station_id,
            "observer": "GNSSatIMO",
            "agency": "Icelandic Meteorological Office",
            "run_by": "GNSSatIMO",
            "config_valid_from": "",
        },
        "antenna": {
            "type": "",
            "radome": "NONE",
            "serial": "",
            "height": 0.0,
            "east": 0.0,
            "north": 0.0,
        },
        "power_type": "unknown",
        "ntrip_importance": None,
        "station_status": None,
        "health_check": None,
        "station_owner": None,
        "station_operator": None,
        "_adhoc": True,
    }


def get_session_config(session_type: str) -> Dict[str, str]:
    """Get session configuration from gps_parser.

    Args:
        session_type: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')

    Returns:
        Session configuration dictionary with letter, path, receiver_path
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.getSessionConfig(session_type)
    except Exception as e:
        logger.error(f"Failed to get session config for {session_type}: {e}")
        raise


def get_system_path(path_name: str) -> str:
    """Get system tool path from gps_parser.

    Args:
        path_name: Path identifier (e.g., 'bin2asc_path', 'data_prepath')

    Returns:
        Expanded path string
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.getSystemPath(path_name)
    except Exception as e:
        logger.error(f"Failed to get system path {path_name}: {e}")
        raise


def get_default_value(setting_name: str):
    """Get default value from gps_parser.

    Args:
        setting_name: Setting identifier (e.g., 'default_session', 'default_days_back')

    Returns:
        Default value with appropriate type conversion
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.getDefaultValue(setting_name)
    except Exception as e:
        logger.error(f"Failed to get default value {setting_name}: {e}")
        raise


def validate_station_config(station_id: str) -> Dict[str, Any]:
    """Validate station configuration using gps_parser.

    Args:
        station_id: Station identifier

    Returns:
        Validation results dictionary
    """
    try:
        config_parser = gps_parser.ConfigParser()
        return config_parser.validateStationConfig(station_id)
    except Exception as e:
        logger.error(f"Failed to validate configuration for {station_id}: {e}")
        raise


def build_data_paths(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime,
    compression: str = ".gz",
) -> List[str]:
    """Build data file paths using gtimes for proper GPS time handling.

    Args:
        station_id: Station identifier (e.g., 'ELDC')
        session_type: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')
        start_time: Start datetime
        end_time: End datetime
        compression: File compression extension

    Returns:
        List of complete file paths for downloading

    Uses gtimes datepathlist for accurate GPS time-based path construction.
    """
    try:
        # Get configuration
        station_config = get_station_config(station_id)
        if not station_config:
            raise ValueError(f"No configuration found for station {station_id}")

        session_config = get_session_config(session_type)
        data_prepath = station_config["paths"]["data_prepath"]

        # Determine frequency based on session type
        frequency_map = {
            "15s_24hr": "1D",  # Daily files
            "1Hz_1hr": "1H",  # Hourly files
            "status_1hr": "1H",  # Hourly status files
        }
        frequency = frequency_map.get(session_type, "1D")

        # Build path format using gtimes-compatible format
        # Format: /data/YYYY/MMM/STATION/SESSION/raw/STATION_DDDF.YYT.gz
        # Where: DDD=day of year, F=file sequence, YY=year, T=session type
        path_format = f"{data_prepath}/%Y/#b/{station_id}/{session_config['session_path']}/raw/{station_id}_%j0.%y{session_config['session_letter']}{compression}"

        # Use gtimes to generate path list
        paths = gt.datepathlist(
            stringformat=path_format,
            lfrequency=frequency,
            starttime=start_time,
            endtime=end_time,
            closed="left",
        )

        logger.debug(
            f"Generated {len(paths)} data paths for {station_id} session {session_type}"
        )
        return paths

    except Exception as e:
        logger.error(f"Failed to build data paths for {station_id}: {e}")
        raise


def build_receiver_paths(
    station_id: str, session_type: str, start_time: datetime, end_time: datetime
) -> List[str]:
    """Build receiver-side file paths using gtimes and gps_parser configuration.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_time: Start datetime
        end_time: End datetime

    Returns:
        List of receiver-side paths for FTP download

    Uses session configuration from gps_parser for receiver path structure.
    """
    try:
        # Get session configuration
        session_config = get_session_config(session_type)
        receiver_base_path = get_system_path("receiver_base_path")

        # Determine frequency
        frequency_map = {"15s_24hr": "1D", "1Hz_1hr": "1H", "status_1hr": "1H"}
        frequency = frequency_map.get(session_type, "1D")

        # Build receiver path format
        # Format: /DSK1/SSN/STATION_DDDF.YYT
        receiver_path_format = f"{receiver_base_path}/{station_id}_%j0.%y{session_config['session_letter']}"

        # Generate receiver paths using gtimes
        receiver_paths = gt.datepathlist(
            stringformat=receiver_path_format,
            lfrequency=frequency,
            starttime=start_time,
            endtime=end_time,
            closed="left",
        )

        logger.debug(f"Generated {len(receiver_paths)} receiver paths for {station_id}")
        return receiver_paths

    except Exception as e:
        logger.error(f"Failed to build receiver paths for {station_id}: {e}")
        raise


def build_archive_paths(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime,
    compression: str = ".gz",
) -> List[str]:
    """Build archive destination paths using gtimes and gps_parser configuration.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_time: Start datetime
        end_time: End datetime
        compression: File compression extension

    Returns:
        List of archive destination paths

    Uses gtimes for proper GPS time-based directory structure.
    """
    try:
        # Get configuration
        station_config = get_station_config(station_id)
        session_config = get_session_config(session_type)
        data_prepath = station_config["paths"]["data_prepath"]

        # Determine frequency
        frequency_map = {"15s_24hr": "1D", "1Hz_1hr": "1H", "status_1hr": "1H"}
        frequency = frequency_map.get(session_type, "1D")

        # Build archive path format
        # Format: /data/YYYY/MMM/STATION/SESSION/STATION_DDDF.YYT.gz
        archive_path_format = f"{data_prepath}/%Y/#b/{station_id}/{session_config['session_path']}/{station_id}_%j0.%y{session_config['session_letter']}{compression}"

        # Generate archive paths using gtimes
        archive_paths = gt.datepathlist(
            stringformat=archive_path_format,
            lfrequency=frequency,
            starttime=start_time,
            endtime=end_time,
            closed="left",
        )

        logger.debug(f"Generated {len(archive_paths)} archive paths for {station_id}")
        return archive_paths

    except Exception as e:
        logger.error(f"Failed to build archive paths for {station_id}: {e}")
        raise
