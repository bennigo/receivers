"""Scheduler configuration loader.

Loads scheduler settings from YAML configuration file.
Provides defaults if config file doesn't exist.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml

    HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

from .bulk_scheduler import ScheduleConfig

logger = logging.getLogger(__name__)


def load_scheduler_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load scheduler configuration from YAML file.

    Args:
        config_path: Path to scheduler.yaml (default: respects GPS_CONFIG_PATH env var)

    Returns:
        Dictionary with scheduler configuration
    """
    if config_path is None:
        # Check for GPS_CONFIG_PATH environment variable first
        import os

        gps_config_dir = os.getenv("GPS_CONFIG_PATH")
        if gps_config_dir:
            config_path = Path(gps_config_dir) / "scheduler.yaml"
        else:
            config_path = Path.home() / ".config" / "gpsconfig" / "scheduler.yaml"

    # If YAML not available or file doesn't exist, return defaults
    if not HAS_YAML:
        logger.warning("PyYAML not installed - using default configuration")
        return get_default_config()

    if not config_path.exists():
        logger.info(f"Configuration file not found: {config_path}")
        logger.info("Using default configuration - create scheduler.yaml to customize")
        return get_default_config()

    # Load YAML configuration
    try:
        assert yaml is not None  # Guaranteed by HAS_YAML check above
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # yaml.safe_load returns None for empty files or comments-only files
        if config is None:
            logger.info(f"Empty configuration file {config_path}, using defaults")
            return get_default_config()

        logger.info(f"Loaded scheduler configuration from {config_path}")

        # Merge with defaults (in case YAML is incomplete)
        return merge_with_defaults(config)

    except Exception as e:
        logger.error(f"Failed to load configuration from {config_path}: {e}")
        logger.info("Falling back to default configuration")
        return get_default_config()


def get_default_config() -> Dict[str, Any]:
    """Get default scheduler configuration (matches YAML defaults).

    Returns:
        Dictionary with default configuration
    """
    return {
        "scheduler": {
            "max_workers": 100,
            "log_level": "INFO",
            "job_defaults": {
                "coalesce": False,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        },
        "sessions": {
            "15s_24hr": {
                "enabled": True,
                "schedule_minute": 10,
                "distribution_window": 10,
                "batches": 2,
                "frequency": "daily",
                "lookback_periods": 1,
                "max_concurrent": 3,
                "timeout_minutes": 45,
                "retry_on_failure": True,
                "retry_delay_minutes": 30,
                "max_retries": 3,
                "clean_tmp": False,  # Keep partial files for resume
            },
            "1Hz_1hr": {
                "enabled": True,
                "schedule_minute": 15,
                "distribution_window": 10,
                "batches": 2,
                "frequency": "hourly",
                "lookback_periods": 1,
                "max_concurrent": 4,
                "timeout_minutes": 30,
                "retry_on_failure": True,
                "retry_delay_minutes": 15,
                "max_retries": 3,
                "clean_tmp": False,  # Keep partial files for resume
            },
            "status_1hr": {
                "enabled": True,
                "schedule_minute": 25,
                "distribution_window": 5,
                "batches": 2,
                "frequency": "hourly",
                "lookback_periods": 1,
                "max_concurrent": 5,
                "timeout_minutes": 15,
                "retry_on_failure": True,
                "retry_delay_minutes": 10,
                "max_retries": 2,
                "clean_tmp": False,  # Keep partial files for resume
            },
        },
        "stations": {},
        "recovery": {
            "auto_recovery_enabled": True,
            "max_recovery_days": 30,
            "backfill_enabled": False,
        },
        # Pipeline and resource pool configuration (new in scheduler enhancement)
        "resource_pools": {
            "network_workers": 10,  # I/O-bound: many concurrent OK
            "cpu_workers": 4,  # CPU-bound: limit for memory
        },
        "pipelines": {
            "15s_24hr": {
                "stages": ["download", "rinex", "sync"],
                "priority": "standard",
                "rinex_timing": "immediate",
                "sync_types": ["raw", "rinex"],
            },
            "1Hz_1hr": {
                "stages": ["download", "sync"],
                "priority": "realtime",
                "sync_types": ["raw"],
            },
            "status_1hr": {
                "stages": ["download", "health"],
                "priority": "standard",
                "health_targets": ["database"],
                "health_priority": "backfill",
            },
        },
        "status_monitoring": {
            "enabled": True,
            "schedule": "5m",  # Every 5 minutes
            "distribution_window": 3,  # Spread across 3 minutes
            "priority": "realtime",
            "targets": ["database", "icinga"],
        },
        "backfill": {
            "enabled": True,
            "window_start": 25,
            "window_end": 55,
            "schedule": "5m",
            "archiving_mode": "bulk",
            "strategy": "round_robin",
            "sessions": ["status_1hr", "1Hz_1hr", "15s_24hr"],
            # Stations processed concurrently per tick, per session. The backfill
            # executor has max(max_workers//5, 5) threads; this fans out within
            # one job instance so the queue drains in parallel, not one-at-a-time.
            "max_workers": 8,
        },
        "gap_detection": {
            "enabled": True,
            "schedule": "2h",
            "files_back": 7,
            "sessions": ["15s_24hr", "1Hz_1hr", "status_1hr"],
        },
        "archive_reconciler": {
            "enabled": True,
            "schedule": "6h",
            "files_back": 30,
            "sessions": ["15s_24hr", "1Hz_1hr"],
        },
        "integrity_checker": {
            "enabled": True,
            "schedule": "6h",
            "files_back": 7,
            "sessions": ["15s_24hr", "1Hz_1hr", "status_1hr"],
            "check_receiver": True,
            "size_tolerance_pct": 50.0,
            # Phase 4: lazily fill file_tracking.content_sha256 (mig 052) for up
            # to N present files per run, newest-first. 0 disables. Keeps the hot
            # download/archive path hash-free (Option B).
            "hash_fill_limit": 1000,
            # Phase 5: report-only stray/stacked RINEX header probe. Runs on
            # daily sessions only by default (identity_sessions=None → sessions
            # not ending in '1hr'); set identity_sessions to a list to override,
            # or check_identity=false to disable.
            "check_identity": True,
            "identity_sessions": None,
        },
        # Batch delta push to the long-term archive gateway (rawdata -> ananas).
        # Disabled by default: double-gated with `active: true` per target in
        # sync.yaml. The targets/cutover/excludes live in sync.yaml; this only
        # gates WHETHER and WHEN to run. See design 1781867391.
        "archive_sync": {
            "enabled": False,
            "schedule": ":45",
            "max_age_minutes": 120,
        },
        # EPOS dissemination sweep (T8): disseminate a trailing window of daily
        # files for every EPOS station to the active sync.yaml dissemination
        # target. Double-gated and inert by default — this flag AND a dissemination
        # target with active: true in sync.yaml. Runs after the archive-sync window.
        "epos_disseminate": {
            "enabled": False,
            "schedule": ":50",
            "days_back": 3,
            "no_qc": False,
        },
        # EPOS reactive sweep (T6): daily TOS-fingerprint diff that re-ETLs /
        # re-disseminates / stops only the stations whose TOS metadata or EPOS
        # eligibility changed. Same double gate as the sweep (this flag AND an
        # active dissemination target). backfill_days bounds the re-push window
        # for a changed/activated station (the convert-cache keeps re-runs cheap).
        "epos_reactive": {
            "enabled": False,
            "schedule": "06:30",
            "backfill_days": 365,
            "no_qc": False,
        },
        # Periodic archive integrity: re-hash archived files vs
        # archive_catalog.content_sha256 (read-back) + local cross-check.
        # Disabled by default. read_root must point at the archive's read-only
        # mount (rek-d01: /mnt/rawgpsdata) for read-back; without it only the
        # DB-only local-vs-archive cross-check runs. reverify_after_days re-checks
        # already-verified rows for slow bit-rot (null = never-verified only).
        "archive_verify": {
            "enabled": False,
            "schedule": "3h",
            "read_root": None,
            "storage_location": "imo_archive",
            "limit": 500,
            "reverify_after_days": None,
            # Immediate per-push read-back (write-through hooks): re-hash the
            # archive copy right after a push for these sessions and stamp
            # last_verified_at. Cheap, small-volume sessions only (15s_24hr is the
            # GAMIT daily input); requires read_root. 1Hz_1hr relies on the
            # session-prioritized COLD periodic verify instead (warm-cache
            # immediate read-back is largely redundant with rsync's own checksum).
            "push_verify_sessions": ["15s_24hr"],
        },
        # Local ring-buffer: age out local gpsdata copies whose long-term
        # archive copy is catalog-confirmed. Retention is CONFIG, not code —
        # raise it as /mnt/data grows. Disk guardrails (warn/min free GB)
        # tighten the ring and log WARNING/ERROR before the disk can fill.
        "local_prune": {
            "enabled": False,
            "schedule": "05:10",
            "retention_days": {
                "15s_24hr": 365,
                "1Hz_1hr": 21,
                "status_1hr": 90,
            },
            # Applied instead while free space is below min_free_gb.
            "emergency_retention_days": {
                "1Hz_1hr": 7,
                "status_1hr": 30,
            },
            "warn_free_gb": 150,
            "min_free_gb": 100,
            "require_catalog": True,
            "max_delete_per_run": 20000,
            # Days-to-full forecast: ERROR when a volume is on course to fill
            # within warn_days_to_full (IT expansion lead time ~3 weeks).
            # forecast_volumes = EXTRA volumes beyond the data root (e.g. the
            # long-term archive mount).
            "warn_days_to_full": 21,
            "forecast_volumes": [],
        },
        "load_monitoring": {
            "enabled": False,
            "max_cpu_load": 8.0,
            "max_network_mbps": 80,
            "max_active_jobs": 80,
            "check_interval": 10,
            "priority_thresholds": {
                "realtime": 1.0,
                "standard": 0.8,
                "backfill": 0.6,
                "maintenance": 0.4,
            },
        },
        "bootstrap": {
            "enabled": True,
            "distribution_window": 10,
            "initial_lookback_days": 3,
            "full_lookback_days": 30,
        },
        "priorities": {
            "realtime": {
                "level": 1,
                "sessions": ["1Hz_1hr"],
            },
            "standard": {
                "level": 5,
                "sessions": ["15s_24hr", "status_1hr"],
            },
            "backfill": {
                "level": 8,
                "max_concurrent": 2,
            },
        },
        "sync": {
            "remote_host": os.getenv("SYNC_REMOTE_HOST", "gpsops@rawdata.vedur.is"),
            "remote_path": os.getenv("SYNC_REMOTE_PATH", "/data/gps/archive"),
            "raw_options": "--ignore-existing",
            "rinex_options": "--update",
            "retry_count": 3,
        },
        "monitoring": {
            "database": {
                "enabled": True,
            },
            "icinga": {
                "enabled": True,
            },
        },
    }


def merge_with_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge loaded config with defaults (fill in missing values).

    Args:
        config: Loaded configuration from YAML

    Returns:
        Complete configuration with defaults filled in
    """
    defaults = get_default_config()

    # Deep merge scheduler section
    if "scheduler" not in config:
        config["scheduler"] = defaults["scheduler"]
    else:
        for key, value in defaults["scheduler"].items():
            if key not in config["scheduler"]:
                config["scheduler"][key] = value

    # Deep merge sessions section
    if "sessions" not in config:
        config["sessions"] = defaults["sessions"]
    else:
        for session_type, session_defaults in defaults["sessions"].items():
            if session_type not in config["sessions"]:
                config["sessions"][session_type] = session_defaults
            else:
                for key, value in session_defaults.items():
                    if key not in config["sessions"][session_type]:
                        config["sessions"][session_type][key] = value

    # Ensure stations section exists
    if "stations" not in config:
        config["stations"] = {}

    # Ensure recovery section exists
    if "recovery" not in config:
        config["recovery"] = defaults["recovery"]

    # Ensure new pipeline sections exist
    for section in [
        "resource_pools",
        "pipelines",
        "status_monitoring",
        "priorities",
        "sync",
        "monitoring",
        "backfill",
        "gap_detection",
        "archive_reconciler",
        "integrity_checker",
        "archive_verify",
        "local_prune",
        "epos_disseminate",
        "epos_reactive",
        "load_monitoring",
        "bootstrap",
    ]:
        if section not in config:
            config[section] = defaults.get(section, {})
        else:
            # Merge with defaults
            for key, value in defaults.get(section, {}).items():
                if key not in config[section]:
                    config[section][key] = value

    return config


def get_session_config(
    config: Dict[str, Any], session_type: str, station_id: Optional[str] = None
) -> "ScheduleConfig":
    """Get ScheduleConfig for a session, applying station overrides.

    .. deprecated::
        This function does not support the new flexible ``schedule`` format.
        BulkDownloadScheduler uses its own inline loading logic instead.

    Args:
        config: Loaded scheduler configuration
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
        station_id: Optional station ID for per-station overrides

    Returns:
        ScheduleConfig object
    """
    import warnings

    warnings.warn(
        "get_session_config() is deprecated and does not support the new "
        "flexible schedule format. Use BulkDownloadScheduler's inline config "
        "loading instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    # Start with session defaults
    session_cfg = config["sessions"].get(session_type, {})

    # Apply station-specific overrides
    if station_id and station_id in config.get("stations", {}):
        station_cfg = config["stations"][station_id]
        if "sessions" in station_cfg and session_type in station_cfg["sessions"]:
            override = station_cfg["sessions"][session_type]
            # Merge override with session defaults
            session_cfg = {**session_cfg, **override}

    # Support both new schedule format and legacy format
    schedule = session_cfg.get("schedule")
    schedule_minute = session_cfg.get("schedule_minute")
    frequency = session_cfg.get("frequency")

    from .bulk_scheduler import ScheduleConfig

    if schedule is not None:
        return ScheduleConfig(
            session_type=session_type,
            schedule=schedule,
            distribution_window=session_cfg.get("distribution_window", 10),
            enabled=session_cfg.get("enabled", True),
            max_concurrent=session_cfg.get("max_concurrent", 3),
            timeout_minutes=session_cfg.get("timeout_minutes", 30),
            midnight_offset=session_cfg.get("midnight_offset", 0),
        )
    else:
        return ScheduleConfig(
            session_type=session_type,
            schedule_minute=schedule_minute or 10,
            distribution_window=session_cfg.get("distribution_window", 10),
            frequency=frequency or "daily",
            enabled=session_cfg.get("enabled", True),
            max_concurrent=session_cfg.get("max_concurrent", 3),
            timeout_minutes=session_cfg.get("timeout_minutes", 30),
            midnight_offset=session_cfg.get("midnight_offset", 0),
        )


def create_default_config_file(output_path: Optional[Path] = None) -> Path:
    """Create default scheduler.yaml configuration file.

    Args:
        output_path: Where to write config (default: ~/.config/gpsconfig/scheduler.yaml)

    Returns:
        Path to created config file
    """
    if not HAS_YAML:
        raise ImportError(
            "PyYAML required to create config file. Install with: pip install pyyaml"
        )

    if output_path is None:
        # Check for GPS_CONFIG_PATH environment variable first
        import os

        gps_config_dir = os.getenv("GPS_CONFIG_PATH")
        if gps_config_dir:
            output_path = Path(gps_config_dir) / "scheduler.yaml"
        else:
            output_path = Path.home() / ".config" / "gpsconfig" / "scheduler.yaml"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        logger.warning(f"Configuration file already exists: {output_path}")
        backup_path = output_path.with_suffix(".yaml.backup")
        output_path.rename(backup_path)
        logger.info(f"Backed up existing config to {backup_path}")

    # Write YAML configuration with comments
    yaml_content = """# GPS Receiver Scheduler Configuration
# Location: ~/.config/gpsconfig/scheduler.yaml
#
# Hourly Timeline:
#   :00      cooldown ends
#   :01-:11  1Hz_1hr live downloads (hours 1-23)
#   :01-:16  15s_24hr live downloads (midnight only)
#   :15-:25  status_1hr live downloads
#   :16-:26  1Hz_1hr midnight downloads (hour 0 only)
#   :25-:55  BACKFILL: gap filling, RINEX reconciliation
#   :55-:00  cooldown
#   Health monitoring: every 5m on separate executor (always)
#
# Schedule syntax:
#   "00:01"  daily | ":01"  hourly | "6h"  interval | "cron: ..."  raw cron

scheduler:
  max_workers: 100
  log_level: INFO
  job_defaults:
    coalesce: true
    max_instances: 3
    misfire_grace_time: 300

sessions:
  # distribution_window: minutes to spread all stations across
  # batches: number of batch groups (group_size = stations/batches,
  #          group_delay = window*60/batches). Used by --parallel mode.
  15s_24hr:
    enabled: true
    schedule: "00:01"
    distribution_window: 10
    batches: 2
    lookback_periods: 1
    max_concurrent: 3
    timeout_minutes: 45
    rinex: true
    clean_tmp: false

  1Hz_1hr:
    enabled: true
    schedule: ":01"
    distribution_window: 10
    batches: 2
    midnight_offset: 15
    lookback_periods: 1
    max_concurrent: 4
    timeout_minutes: 30
    rinex: true
    clean_tmp: false

  status_1hr:
    enabled: true
    schedule: ":15"
    distribution_window: 10
    batches: 2
    lookback_periods: 1
    max_concurrent: 5
    timeout_minutes: 15
    clean_tmp: false

status_monitoring:
  enabled: true
  schedule: "5m"
  distribution_window: 3
  targets: [database, icinga]

backfill:
  enabled: true
  window_start: 25
  window_end: 55
  schedule: "5m"
  archiving_mode: bulk
  sessions: [status_1hr, 1Hz_1hr, 15s_24hr]

# How far back a job looks. Two keys, pick ONE per section — setting both is a
# hard startup error, never a silent precedence:
#
#   days_back:  N calendar days, whatever the session's file frequency.
#   files_back: N FILES — N days for a daily session (15s_24hr), N HOURS for an
#               hourly one (1Hz_1hr, status_1hr).
#
# The difference only matters for hourly sessions, where it is 24x: days_back: 7
# on 1Hz_1hr is 168 files per station, files_back: 7 is 7. Anything older is
# long_term_backfill's job.
#
# Sections that deliberately keep days_back:
#   epos_disseminate  — it is the ONLY backfill window EPOS has (no
#                       gap-reconciler yet), so a day that ages out of it is
#                       orphaned until someone runs epos-disseminate --force.
#   morning_recovery  — day-oriented by construction (it re-walks whole target
#                       dates, daily sessions only).
#
# The effective expansion is logged at startup, e.g.
#   "files_back=7 -> 15s_24hr: 7d, 1Hz_1hr: 7h"

gap_detection:
  enabled: true
  schedule: "2h"
  files_back: 7
  sessions: [15s_24hr, 1Hz_1hr, status_1hr]

archive_reconciler:
  enabled: true
  schedule: "6h"
  files_back: 30
  sessions: [15s_24hr, 1Hz_1hr]

integrity_checker:
  enabled: true
  schedule: "6h"
  files_back: 7
  sessions: [15s_24hr, 1Hz_1hr, status_1hr]
  check_receiver: true
  size_tolerance_pct: 50.0
  # Phase 4: lazily fill file_tracking.content_sha256 (mig 052), newest-first,
  # up to N files/run. 0 disables. Keeps the hot download/archive path hash-free.
  hash_fill_limit: 1000
  # Phase 5: report-only stray/stacked RINEX header probe. Daily sessions only
  # by default (hourly is 24x the files for no extra signal). Set
  # identity_sessions: [15s_24hr, 1Hz_1hr] to widen, or check_identity: false.
  check_identity: true
  # identity_sessions: [15s_24hr]

# Periodic archive integrity verify (read-back re-hash vs archive_catalog +
# local cross-check). Disabled by default. Set read_root to the archive's
# read-only mount (rek-d01: /mnt/rawgpsdata) to enable read-back; without it
# only the DB-only local-vs-archive cross-check runs.
archive_verify:
  enabled: false
  schedule: "3h"
  read_root: null
  storage_location: imo_archive
  limit: 500
  reverify_after_days: null
  # Immediate per-push read-back for these (small-volume) sessions: re-hash the
  # archive copy right after a write-through push and stamp last_verified_at.
  # Requires read_root. 1Hz_1hr deliberately omitted (high volume; the cold
  # periodic verify prioritizes it instead).
  push_verify_sessions: ["15s_24hr"]

# Local ring-buffer: delete local gpsdata copies older than the per-session
# retention, ONLY when the long-term archive copy is confirmed in
# archive_catalog. Retention is config — raise it as /mnt/data grows.
# Guardrails: WARNING logged under warn_free_gb; under min_free_gb the
# emergency retentions apply and ERROR is logged (ring tightens itself
# before the disk can reach 100%).
local_prune:
  enabled: false
  schedule: "05:10"
  retention_days:
    15s_24hr: 365
    1Hz_1hr: 21
    status_1hr: 90
  emergency_retention_days:
    1Hz_1hr: 7
    status_1hr: 30
  warn_free_gb: 150
  min_free_gb: 100
  require_catalog: true
  max_delete_per_run: 20000
  # Days-to-full forecast: ERROR-level warning when a volume fills within
  # warn_days_to_full at the observed rate (IT expansion lead time ~3 weeks).
  # forecast_volumes = extra volumes to watch beyond the data root, e.g. the
  # long-term archive mount:
  #   forecast_volumes: [/mnt/rawgpsdata]
  warn_days_to_full: 21
  forecast_volumes: []

stations: {}

recovery:
  auto_recovery_enabled: true
  max_recovery_days: 30
  backfill_enabled: false
"""

    with open(output_path, "w") as f:
        f.write(yaml_content)

    logger.info(f"Created scheduler configuration: {output_path}")
    return output_path
