# CLAUDE.md - GPS Receivers Package

This document provides guidance for working with the `receivers` package in the gpslibrary_new collection.

## Package Overview

The `receivers` package provides GPS receiver management functionality for the Icelandic Met Office's 173-station GNSS network. It includes direct receiver communication, bulk download scheduling, production logging, and comprehensive validation.

## Key Features

### Core Functionality
- **Direct receiver communication** - FTP/TCP connections to GPS receivers
- **Bulk download management** - APScheduler-based distributed downloading
- **Production logging** - Structured output for automated systems
- **Immediate archiving** - Fault-tolerant file handling
- **Comprehensive validation** - Receiver type detection and configuration validation
- **Health monitoring** - ✅ RxTools-based SBF health data extraction (voltage, CPU, temperature, disk)

### Supported Receivers
- **Septentrio PolaRX5** - Full feature support with RxTools health monitoring
- **Leica/Trimble receivers** - Basic download support
- **Generic receivers** - Configurable via type detection system

## Command-Line Interface

### Desk/Bench Provisioning

```bash
# Bootstrap a fresh receiver on the bench (USB or WiFi AP):
receivers rec-provision BENCH --host 192.168.3.1  --bootstrap          # USB
receivers rec-provision BENCH --host 192.168.20.1 --bootstrap          # WiFi AP
receivers rec-provision BENCH --host 192.168.20.1 --bootstrap --dry-run

# With config push:
receivers rec-provision BENCH --host 192.168.20.1 --bootstrap \
  --apply-config path/to/TEST_PolaRx5_GPS_GLONASS_only.txt

# Provision deployed station (IP from stations.cfg):
receivers rec-provision GJAC
```

**Connection options:** USB gives `192.168.3.1`. Built-in WiFi AP gives `192.168.20.1`
(SSID: `PolaRx5-<serial>`). Both work identically. Laptop Ethernet internet stays active
when connecting via WiFi — the receiver AP uses a separate 192.168.20.x subnet.

**`--bootstrap`** fills in `--set-ip`/DNS from `receivers.cfg` (`desk_bootstrap_ip`,
`desk_dns1/2`) and requires `--host`. Do not use on deployed stations.

### Basic Commands

```bash
# Download data with sync and archiving
receivers download ELDC THOB --sync --archive

# Download specific time period
receivers download ELDC --start 20250905 --end 20250906 --session 1Hz_1hr

# Check receiver connection status
receivers status ELDC THOB

# Get comprehensive health information (uses RxTools to extract SBF health data)
receivers health THOB --verbose

# Health output options
receivers health THOB --json              # JSON output
receivers health THOB --save-json         # Save to JSON file
receivers health THOB --save-db           # Save to database

# Validate receiver configuration
receivers validate ELDC --verbose
```

### Configuration Reconciliation

Three-way comparison between `stations.cfg`, the live receiver, and TOS.
TOS is the canonical source; the live receiver is a validation source
that flags discrepancies. See `src/receivers/cfg/` and the "Cfg
Reconciliation — Future Work" TODO subsection below.

```bash
# Interactive review for one station (queries both receiver and TOS)
receivers cfg reconcile ELDC

# TOS-only inventory across all stations
receivers cfg reconcile --all --source tos --dry-run

# Auto-fill missing cfg values where sources agree (no prompts)
receivers cfg reconcile --all --auto-fill --field receiver_serial firmware

# JSON output for tooling / dashboards
receivers cfg reconcile --all --source tos --json --only-diffs

# Loosen position QC threshold (default 2 m)
receivers cfg reconcile --all --position-tolerance-m 5.0

# List reconcilable fields
receivers cfg reconcile --list-fields
```

**Reconcilable fields** (all 12 fields covered):
- Receiver identity (receiver-authoritative): `receiver_type`, `receiver_serial`, `receiver_firmware_version`
- Antenna metadata (flag-only — TOS canonical, receiver value used for QC mismatch only):
  `antenna_type`, `antenna_serial`, `antenna_radome`, `antenna_height`
- Position (flag-only — surveyed coords from TOS canonical, receiver PVT value confirms
  receiver is at the expected mark within `--position-tolerance-m`, default 2 m):
  `latitude`, `longitude`, `height`
- TOS-only: `station_name` (receiver MarkerName carries the 4-char ID),
  `rinex_marker_number` (IERS DOMES → RINEX MARKER NUMBER; TOS-canonical, cfg follows)

**Sync cfg from TOS — `cfg sync-from-tos STATION [STATION…] | --all`**: the inverse
direction of `reconcile` for **TOS-canonical** fields — after a TOS change, write the
current TOS values into stations.cfg (**no receiver probe**). Scope = the manifest fields
flagged `sync_from_tos=True` (receiver type/serial/firmware, antenna type/serial/radome,
`station_name`, `rinex_marker_number`) plus a single station's SIM `router_ip`; surveyed
position stays cfg-canonical (flag-only). A field is filled **only when TOS carries a
value** — an absent TOS DOMES never clobbers cfg. The surgical writer (`_update_cfg_field`)
rewrites just the target `[STATION]` section's key lines, leaving the rest of the file
byte-for-byte intact. Default dry-run; `--no-dry-run` applies; `--global --push` commits
to the gps-config-data repo. `--only FIELD…` restricts the sync to specific fields (e.g.
`--only rinex_marker_number` to fill just the IERS DOMES without touching receiver/antenna
metadata). `--all` refuses `--yes` (never blind-bulk-apply — TOS can be stale).
**`rinex_marker_number` is TOS-canonical-for-cfg but deliberately NOT `tos_writable`**
(no `tos_attribute_code`): cfg follows TOS, but `reconcile --push-tos` will never send cfg's
value up to TOS — cfg had the wrong 4-char marker for most of the fleet, and the RINEX
converter reads this key for MARKER NUMBER (the NYLA cross-contamination). This is the
workflow step to run after every TOS metadata change.

**MARKER NUMBER policy (bgo, 2026-07-13): the RINEX `MARKER NUMBER` field carries the IERS
DOMES number and nothing else — absent a real DOMES the line is skipped/stripped, never
filled with the 4-char station id** (`MARKER NAME` already carries that). Enforced in one
place — `tostools.rinex.domes.domes_or_skip` (regex `^\d{5}[A-Z]\d{3}$`) — across every
write and compare path: the archive converter (`corrector.py` config + TOS branches, which
now emit a `STRIP_LINE` sentinel), the EPOS finalizer (`convert.finalize_epos_header` →
`_set_header_records` `None`=strip), and the QC/compare side (`validator.compare_rinex_to_tos`,
`gps_rinex`, `qc_gate`) so a `--fix-headers` run *removes* a stale id instead of re-injecting
it. A 4-char value in cfg is treated as "no DOMES" and skipped, so the policy is correct
fleet-wide even before `sync-from-tos --only rinex_marker_number` fills the ~32 real DOMES.

**Behaviour change**: as of this feature, `receivers health <SID>` no
longer silently writes to `stations.cfg`. Discrepancies are logged with
a hint to run `receivers cfg reconcile <SID>`. Pass `--update-cfg` to
restore the legacy in-place write for the rare cases that need it.

**Install-attribute fill on `cfg move-device --to STATION`**: a station-
destination move now also fills the station's **position** attributes
(`latitude`/`longitude`/`height` → TOS station entity `lat`/`lon`/`altitude`)
into TOS from `stations.cfg` — stations.cfg is the ground truth for surveyed
coordinates here (the inverse of `cfg reconcile`, where TOS is authoritative).
Behaviour: adds missing TOS values (confirm `y/N`, or `-y/--yes`); no-op when
TOS already matches within `--position-tolerance-m` (default 2 m); on a genuine
cfg≠TOS difference it requires an explicit intent — `--change` (Pattern 2
transition, records history) or `--correct` (Pattern 1 in-place, no history),
same semantics as `cfg update-device` — and prompts `[c]hange/[f]ix/[s]kip`
when neither flag is given. Disable with `--no-install-attrs`; skipped for
warehouse moves and in `--json` mode. **Scope note**: only the position group
is filled. The receiver-derived attrs (`sampling_interval`, FTP/HTTP/CTRL
ports, `ip_address`) have **no TOS attribute code** — there is nowhere to
write them — so they are out of scope (see vault todo #29). `antenna_height`
is a stations.cfg *composite* (antenna ARP + monument height) that TOS splits
across two entities, hence non-writable from one cfg number; the antenna group
belongs to `cfg replace-antenna` (below), monument height to `cfg add-monument`.

**Antenna/radome swaps — `cfg replace-antenna`, `cfg replace-radome`, `cfg
close-join`**: `add-antenna` is intake-only and refuses when an antenna is already
open — two open antennas make `current_session()`, and therefore every RINEX
header, the stream SKL and station.info, ambiguous. `replace-antenna` is the
swap: retire the open antenna, create + join the new one, vitjun, cfg antenna
group. The radome is screwed to the antenna, so it **follows the antenna by
default** (old join closed, new unit created carrying the same model — the ~95%
case); `--radome CODE` names a different type, `--radome NONE` means bare, and
`--keep-radome` covers re-fitting the same physical unit. `replace-radome` is the
radome-only swap (antenna stays; touches cfg `antenna_radome` only, never the
height composite). The cfg `antenna_height` composite is derived from the open
monument — `replace-antenna` raises rather than write a bare ARP. `close-join` is
the non-destructive `delete-join`: it *ends* a real join instead of removing a row
that shouldn't exist. Full walkthrough + gotchas:
[`docs/architecture/station-onboarding.md`](docs/architecture/station-onboarding.md) §2b–2c.

### Production Mode

```bash
# Production logging with JSON output
receivers download ELDC --sync --archive --production --json-log

# Test connection before downloading
receivers download ELDC --test-connection --sync

# Phase 1 utilities are always enabled (default)
receivers download ELDC --sync --archive
```

### Bulk Scheduler

```bash
# Test scheduler configuration
receivers scheduler test

# Test with subset of stations (laptop testing)
receivers scheduler test --stations OLKE ELDC THOB --max-stations 2

# Start scheduler with limited stations
receivers scheduler start --stations OLKE ELDC --max-workers 2 --verbose

# Check scheduler status
receivers scheduler status --show-jobs

# Create/manage configuration
receivers scheduler config --create
receivers scheduler config --show

# Manual backfill
receivers scheduler backfill --session 15s_24hr --days 30
receivers scheduler backfill --session status_1hr --stations ELDC THOB

# SBF→RINEX reconciliation
receivers scheduler reconcile --days 30 --dry-run
receivers scheduler reconcile --stations ELDC THOB

# File integrity checking
receivers scheduler integrity --session 15s_24hr --days 7
receivers scheduler integrity --session all --days 30 --no-receiver
receivers scheduler integrity --stations ENTC ELDC --tolerance 20

# Full-archive identity audit (stray + stacked; report-only, emits fix commands)
receivers archive-audit NYLA --identity --years 2022      # stray/stacked sweep
receivers archive-audit NYLA --identity --deep --check-version  # + corruption + R2
```

## Architecture

### Phase 1 Utilities (Always Enabled)
**Status**: ✅ Default in all receiver types (PolaRX5, NetR9, NetRS, G10) - Phase 3B complete

The receivers package uses modular Phase 1 utilities for core functionality:

#### Time Parameter Processor
- **Purpose**: Parse and validate session parameters (start, end, session type)
- **Usage**: Converts user input to datetime lists for file generation
- **Benefits**: Single source of truth for time processing, comprehensive validation

#### Archive Validator
- **Purpose**: Validate archive file integrity (gzip, size, corruption)
- **Usage**: Check files before/after archiving operations
- **Benefits**: Detect corrupt archives, validate downloads

#### File Archiver (IMMEDIATE Mode)
- **Purpose**: Archive files immediately after download/processing
- **Mode**: IMMEDIATE - archive one file at a time for fault tolerance
- **Benefits**: Prevents data loss on crashes, incremental progress tracking

**Phase 1 utilities are always enabled** (no configuration needed):
```bash
receivers download STATION --sync --archive  # Phase 1 is always active
```

**Why immediate archiving?**
- **Fault tolerance**: Already-downloaded files are safely archived if process crashes
- **Slow connections**: Progress saved incrementally during long downloads
- **Production reliability**: Minimizes data loss during network issues
- **Better monitoring**: Clear file-by-file progress tracking

### Scheduling System
- **Hourly timeline** (optimized distribution):
  - `:01-:06` — `1Hz_1hr` live downloads (hours 1-23, `distribution_window: 5`)
  - `:01-:06` — `15s_24hr` live downloads (midnight only)
  - `:11-:16` — `1Hz_1hr` midnight downloads (hour 0, offset clears 15s_24hr's :01-:06)
  - `:15-:20` — `status_1hr` live downloads
  - `:30-:55` — BACKFILL WINDOW (self-gating, gap detection, SBF→RINEX reconciler at :30)
  - `:55-:00` — cooldown
  - Health monitoring: every 5m on separate executor (always)
- **Three executors**: `default` (live downloads), `health` (monitoring), `backfill` (gap fill + reconciler)
- **Distribution windows**: Stations spread evenly across time to prevent burst load
- **Midnight offset**: 1Hz_1hr uses `midnight_offset: 10` (hour 0 starts at 00:11) to clear 15s_24hr's :01-:06 window
- **Multi-session backfill**: All three sessions backfilled via self-gating interval jobs
- **Gap detection**: Periodic scan for missing files (every 2h, configurable)
- **Archive reconciler**: SBF→RINEX conversion for orphaned raw files (every 6h)
- **Integrity checker**: Validates archives, detects untracked files, flags size anomalies (every 6h). Also runs a **report-only identity probe** on the window's RINEX (header-only, no `teqc`): flags **stray** files (`APPROX POSITION XYZ` closest to a *different* station — the FAGC class) and **stacked** files (multiple RINEX documents concatenated in one `.Z`). Never auto-repairs — logs under `receivers.scheduler.integrity` with the fix command (`archive-sort` for a stray, re-rinex for a stacked file). **Scoped to daily sessions only** by default (`identity_sessions=None` → non-`1hr`): hourly `1Hz_1hr` is ~24× the files for no extra signal, so at `days_back=7` this is ~1 local decompress/station/day (~a minute). Widen via `identity_sessions: [...]` or disable via `check_identity: false`. Shared probe: `receivers/archive/file_identity.py`.
- **Persistence**: SQLite job store survives restarts
- **Manual compatibility**: All manual operations remain fully functional
- **Extensibility**: Task interface allows scheduling any operation type (status, health, validation)
- **Testing**: 43+ comprehensive test cases (Phase 3C)

#### Scheduler Extensibility (Phase 3C)

**Task Interface Architecture**:
```python
from receivers.scheduling.task_interface import ScheduledTask, TaskType
from receivers.scheduling.tasks import DownloadTask

# Current: DownloadTask implements ScheduledTask
# Future: StatusTask, HealthTask, ValidateTask
```

**Adding New Task Types** (Future):
```python
class StatusTask(ScheduledTask):
    def execute(self) -> TaskResult:
        # Check receiver status
        ...
```

See `docs/scheduler/scheduler-guide.md` for complete details.

### Path Building System
- **Unified approach**: Single `build_path()` method handles all path generation using gtimes templates
- **Dynamic frequency**: Respects session frequency (1H for hourly, 1D for daily) instead of hardcoded values
- **Consistent formatting**: Both remote and archive paths use same gtimes-based datetime formatting
- **Separation of concerns**: Path generation completely separated from download mechanics
- **Year-future-proof**: Automatic year handling prevents hardcoding bugs (e.g., `.25_` format)
- **Multiple input types**: Supports single datetime, datetime lists, or start/end time ranges
- **IGS filename accuracy**: Uses gtimes `#Rin2` format for correct hour-to-letter mapping

### Archive Format & Multi-Location System
**Status**: ✅ Implemented — migration `021_archive_format.sql`

Table-driven system for RINEX metadata, path templates, and multi-location file tracking. New file formats can be added without code changes.

#### Database Tables

| Table | Purpose |
|-------|---------|
| `archive_format` | Format definitions: session type, RINEX metadata, path/filename templates |
| `storage_location` | Storage locations with environment-specific base paths |
| `file_locations` | Many-to-many: which files exist at which locations |
| `file_tracking.format_id` | Nullable FK linking tracked files to their format (backward compatible) |

#### archive_format Seed Data (PolaRX5)

| format_id | category | frequency | RINEX | naming | Hatanaka | ext |
|-----------|----------|-----------|-------|--------|----------|-----|
| `polarx5_15s_24hr_raw` | raw | 1D | — | — | — | `.sbf.gz` |
| `polarx5_15s_24hr_rinex` | rinex | 1D | 3.04 | short | yes | `.d.Z` |
| `polarx5_1hz_1hr_raw` | raw | 1H | — | — | — | `.sbf.gz` |
| `polarx5_1hz_1hr_rinex` | rinex | 1H | 3.04 | short | yes | `.d.Z` |
| `polarx5_status_1hr_raw` | raw | 1H | — | — | — | `.sbf.gz` |

#### Path Templates

Format definitions store separate `dir_template` and `filename_template` with placeholders:
- `{station}`, `{session_letter}` — substituted before gtimes
- `%Y`, `%m`, `%d`, `%H`, `#b`, `#Rin2`, `#hourl` — handled by `gtimes.datepathlist()`

Example templates and resolved paths:
```
dir_template:      %Y/#b/{station}/15s_24hr/rinex/
filename_template: {station}#Rin2d.Z
→ /data/2026/feb/ELDC/15s_24hr/rinex/ELDC0410.26d.Z

dir_template:      %Y/#b/{station}/1Hz_1hr/raw/
filename_template: {station}%Y%m%d%H00{session_letter}.sbf.gz
→ /data/2026/feb/THOB/1Hz_1hr/raw/THOB202602101400b.sbf.gz
```

#### FormatResolver Class (`file_tracker.py`)

```python
from receivers.health.file_tracker import FormatResolver

with FormatResolver() as resolver:
    # Build full path from format + station + datetime
    path = resolver.build_path('polarx5_15s_24hr_rinex', 'ELDC', dt,
                               base_path='/data')

    # Find format by criteria (receiver-specific first, then universal)
    fmt = resolver.find_format('15s_24hr', 'rinex', receiver_type='polarx5')

    # List all RINEX formats
    rinex_formats = resolver.list_formats(file_category='rinex')

    # Record file at a storage location
    resolver.record_file_location(tracking_id, 'local_archive',
                                  file_path='/data/...', file_size=12345)
```

- Loads `archive_format` and `storage_location` from PostgreSQL into memory (cached)
- `build_path()` / `build_directory()` — construct paths via gtimes
- `find_format()` — lookup by session_type + file_category + receiver_type
- `record_file_location()` — upsert to `file_locations` join table
- Archive reconciler uses FormatResolver when available, falls back to glob

#### Storage Locations

Base paths are environment-specific — defined in `receivers.cfg` and seeded to DB:

```ini
[storage_locations]
local_archive = /home/bgo/tmp/gpsdata, local, Local development archive, true
production_nfs = /mnt_data/gpsdata, nfs, Production NFS mount
```

Falls back to `[archive_paths] data_prepath` when no `[storage_locations]` section exists.

Seed to database: `seed_storage_locations()` from `receivers.config.receivers_config`.

### Unified Logging System
**Status**: ✅ Implemented — `src/receivers/logging_config.py`

Single `setup_logging()` function replaces all previous logging setup. All loggers use the `receivers.*` hierarchy.

#### Setup
```python
from receivers.logging_config import setup_logging

# Basic usage — returns a logger under receivers.*
logger = setup_logging(component='scheduler')  # → receivers.scheduler

# With options
logger = setup_logging(
    level=logging.DEBUG,
    json_output=True,       # JSON on console (for monitoring pipelines)
    log_dir=Path('/tmp'),   # Custom log directory
    component='download',   # → receivers.download
)
```

- **Idempotent**: safe to call multiple times (second call is a no-op)
- **Console handler**: `ProductionFormatter` with emoji level icons (stderr)
- **File handler**: JSON, rotating (20 MB, 3 backups) → `receivers.log`
- **Third-party suppression**: urllib3, ftplib, gps_parser, apscheduler → WARNING
- **Audit trail**: Separate `receivers.audit` logger → `download_audit.jsonl`

#### Logger Naming Hierarchy

| Logger Name | Used By |
|-------------|---------|
| `receivers` | Root — all receivers output |
| `receivers.download.{station}` | Download jobs (CLI and scheduler) |
| `receivers.health.{station}` | All health extractors (TCP, HTTP, FTP) |
| `receivers.scheduler` | Scheduler core (`bulk_scheduler.py`) |
| `receivers.scheduler.backfill` | Backfill jobs |
| `receivers.scheduler.gaps` | Gap detection |
| `receivers.scheduler.reconciler` | Archive reconciler |
| `receivers.scheduler.integrity` | Integrity checker |
| `receivers.pipeline.{station}` | Pipeline tracking |
| `receivers.task.{station}` | Task interface |
| `receivers.audit` | Audit trail (separate file, no propagation) |
| `receivers.cli.*` | CLI modules (via `__name__`) |
| `receivers.health.*` | Health modules (via `__name__`) |
| `receivers.monitoring.*` | Monitoring modules (via `__name__`) |

#### Per-Component Level Overrides

Add to `database.cfg`:
```ini
[logging]
# Override levels for specific components (optional)
# receivers.health = DEBUG
# receivers.scheduler = WARNING
# receivers.download = INFO
```

#### For New Code

```python
# Module-level (preferred for most files):
import logging
logger = logging.getLogger(__name__)  # e.g. receivers.health.db_writer

# Station-specific (for extractors/jobs):
logger = logging.getLogger(f"receivers.health.{station_id}")
logger = logging.getLogger(f"receivers.download.{station_id}")
```

#### Key Files
- **`src/receivers/logging_config.py`** — Unified setup function (single source of truth)
- **`src/receivers/base/production_logging.py`** — Formatters (`ProductionFormatter`, `JSONFormatter`), `AuditLogger`, backward-compatible `ProductionLoggingConfig` wrapper

### File Management
- **Immediate archiving**: Files archived after each download for fault tolerance
- **Compression**: Automatic .gz compression
- **Sync strategy**: Only download new/partial files
- **Clean restart**: Option to clear partial downloads

## Configuration

**Config architecture**: See `docs/architecture/config-data-flow.md` for the full design,
including the config sync system and the future TOS/tostools integration vision.

**Station onboarding**: See `docs/architecture/station-onboarding.md` for the end-to-end
TOS device-intake walkthrough (add-receiver/move-device → add-antenna → add-monument →
telemetry → stream-flip/download), with the session-split / monument_height /
find_station-reindex / legacy-router gotchas.

**Source of truth**: `gps-config-data` repo (`git.vedur.is/bgo/gps-config-data`).
**RULE (bgo, 2026-07-06): ALL config changes are made in gps-config-data and propagate
outward — never edit a deployed file directly.** Commit + push immediately; an
uncommitted laptop edit is invisible to the server (the 30S sync.yaml block sat
uncommitted for a day — exactly this failure mode). The sync timer
(`gps-config-sync.timer`) propagates within ~10 minutes; install.sh Phase 5 deploys
the full set (`stations.cfg receivers.cfg scheduler.yaml database.cfg icinga.cfg
station_areas.yaml sync.yaml agencies.yaml`). **`receivers.cfg` is now tracked**
(2026-07-14): the repo is internal-only and its creds are shared/non-sensitive, so
it's a normal committed file like the others — canonical = rek-d01 production config,
propagated by the sync timer. **Only `database.cfg` stays gitignored per-host** (edit it
inside the SERVER'S gps-config-data clone `~/git/gps-config-data/`, plus the committed
`.template`/`environments/*.env` so fresh installs match). Note: install.sh sed-patches
`data_prepath`/`tmp_dir` in receivers.cfg at deploy time; because the tracked canonical
now carries the production `data_prepath` (`/mnt/data/gpsdata/`), that patch is a no-op on
rek-d01 — keep the canonical's path at the production value or the raw-cp sync would push a
bad path to gpsops.

**Finalizing cfg from TOS — `cfg ... --global`**: the cfg verbs write the **local/deployed**
config by default; `--global` instead writes the **gps-config-data repo** (resolved from
`receivers.cfg [paths] gps_config_data_repo` → `$GPS_CONFIG_DATA_REPO` → `~/git/gps-config-data`)
and commits it. `--push` (required for a real commit) pushes so the sync timer ff-pulls it to
rek-d01. **`--global` is a laptop-side tool** (bgo/Hildur run it; technicians use the future
rek_new web UI). A non-dry-run `--global` commit **requires `--push`** and refuses if the clone
isn't even with origin — an unpushed local commit would leave the clone ahead of origin and
break the server's `git pull --ff-only`, silently halting config sync. Use `--global --dry-run`
to preview. The divergence preflight runs before any write, so a refusal leaves no dirty tree.

### Station Configuration
```bash
# Configuration loaded from gps_parser package
# Uses ~/.config/gpsconfig/stations.cfg and postprocess.cfg
# Environment: GPS_CONFIG_PATH or default paths
```

#### Station Lifecycle Fields

Two separate fields in `stations.cfg` control station visibility:

| Field | Purpose | Values | Default (NULL) |
|-------|---------|--------|----------------|
| `station_status` | Station lifecycle | `inactive`, `discontinued` | active |
| `health_check` | Monitoring mode | `passive` | active (directly checked) |

- **`station_status`**: Controls whether the station is operational
  - `inactive` — no receiver installed or temporarily out of service
  - `discontinued` — station decommissioned, no longer operational
  - Not set (NULL) — active, fully operational
- **`health_check`**: Controls how the station is monitored
  - `passive` — data arrives externally, not directly health-checked
  - Not set (NULL) — active, scheduler runs health checks

A station can have both fields set (e.g., GRVM: `station_status = inactive` + `health_check = passive`).

**Config change detection**: The scheduler watches `stations.cfg` for changes (mtime-based, every 1 minute) and automatically syncs `station_status` and `health_check` to the PostgreSQL database. A detected change also re-runs the stream config refresh + supervise (when `stream_capture.enabled`), so an `acquisition_mode=stream` flip takes effect within ~1 min instead of waiting for the daily 06:00 `stream_config_refresh`. No scheduler restart needed for config changes.

**Auto-detection**: Stations with `receiver_type` set to None/empty/unknown are automatically flagged as `station_status = inactive` by the scheduler.

See `stations.cfg` header comments for the complete field reference.

### Scheduler Configuration
```bash
# Create default configuration
receivers scheduler config --create

# Configuration location (respects GPS_CONFIG_PATH environment variable):
# - If GPS_CONFIG_PATH is set: $GPS_CONFIG_PATH/scheduler.yaml
# - Otherwise: ~/.config/gpsconfig/scheduler.yaml
# Database: ~/.cache/gps_receivers/scheduler.db
# Logs: ~/.cache/gps_receivers/logs/

# Key config sections:
# - scheduler: max_workers, job_defaults (coalesce, max_instances)
# - sessions: 15s_24hr, 1Hz_1hr, status_1hr (schedule, distribution_window, midnight_offset)
# - backfill: window_start/end, schedule, archiving_mode, sessions
# - gap_detection: schedule, days_back, sessions
# - archive_reconciler: schedule, days_back, sessions
# - integrity_checker: schedule, days_back, sessions, check_receiver, size_tolerance_pct
# - status_monitoring: schedule, distribution_window, targets
```

#### Flexible Schedule Syntax

The scheduler now supports flexible schedule formats in addition to the legacy `schedule_minute` + `frequency` format:

**Supported formats:**
1. **Single time (daily)**: `schedule: "00:10"` - Runs daily at 00:10
2. **Hourly at minute**: `schedule: ":15"` - Runs every hour at :15
3. **Interval (hours)**: `schedule: "6h"` - Runs every 6 hours
4. **Interval (minutes)**: `schedule: "45m"` - Runs every 45 minutes
5. **Multiple times**: `schedule: ["06:00", "14:00", "22:00"]` - Runs 3 times daily
6. **Raw cron**: `schedule: "cron: */15 * * * *"` - Full cron expression support

**Examples:**
```yaml
sessions:
  15s_24hr:
    schedule: "00:10"              # Daily at 00:10
    distribution_window: 10

  1Hz_1hr:
    schedule: ":15"                # Hourly at :15
    distribution_window: 10

  custom_6h:
    schedule: "6h"                 # Every 6 hours
    distribution_window: 10

  rush_hour:
    schedule: ["06:00", "12:00", "18:00"]  # Three times daily
    distribution_window: 5

  business_hours:
    schedule: "cron: 0 8-17 * * 1-5"  # Every hour, 8am-5pm, Mon-Fri
    distribution_window: 5
```

**Legacy format (still supported):**
```yaml
sessions:
  15s_24hr:
    schedule_minute: 10
    frequency: daily
    distribution_window: 10
```

**Note**: The `distribution_window` spreads stations evenly across time to avoid network congestion. For example, with 3 stations and a 10-minute window starting at :15, they schedule at :15, :18, and :21.

## Development

### Package Installation
```bash
cd receivers
pip install -e .

# Dependencies
pip install apscheduler sqlalchemy  # For scheduler functionality
```

### Testing
```bash
# Test receiver communication
python -m pytest tests/ -v

# Test scheduler (Phase 3C)
pytest tests/test_scheduler_basic.py tests/test_scheduler_execution.py -v

# Test scheduler without starting
receivers scheduler test --stations TEST

# Test production logging
receivers download TEST --production --json-log --test-connection
```

### Environment Setup
```bash
# Required PYTHONPATH for development
export PYTHONPATH=../gtimes/src:../gps_parser/src:src

# Configuration directory
mkdir -p ~/.config/gpsconfig
# Add stations.cfg and postprocess.cfg from gps_parser
```

## Integration Points

### Dependencies
- **gps_parser**: Station configuration and path management
- **gtimes**: GPS time calculations and conversions
- **APScheduler**: Job scheduling and persistence
- **SQLAlchemy**: Database backend for job storage

### Monitoring Integration
- **Grafana Dashboards** (`docs/grafana/`), see `docs/grafana/README.md`
  - Runs via Docker Compose (`deployment/docker-dev/docker-compose.yml`) on port 3001
  - **Overview**: `gps_health_dashboard.json` — count boxes, station table, map panel
  - **Map**: `gps_map_dashboard.json` — full-screen geomap with count boxes
  - **Station Detail**: `gps_station_detail_dashboard.json` — per-station deep dive
  - Datasource: PostgreSQL `gps_health` database
  - Auto-provisioned dashboards and datasources via YAML configs
  - To update: edit the JSON file and `docker restart gps-grafana-dev`
  - Default refresh: 10s, filters: Area, Station, Receiver (NONE=missing), Antenna (NONE=missing), Status Filter
  - "Reset Filters" button in nav bar resets all filters to defaults
  - **Ping tolerance**: `station_connectivity` view requires 2 consecutive failed pings before reporting offline (prevents false-offline on lossy 3G/4G links)
- **Icinga 2**: Health data can be sent to monitoring endpoints
- **JSON logging**: Structured output for log aggregation systems
- **Email alerts**: Integration with gps-validation@vedur.is
- **Audit trails**: Performance metrics and failure analysis

### Manual Operation Compatibility
All scheduler functionality maintains complete compatibility with manual operations:
- Single station downloads work alongside scheduled operations
- Configuration changes apply to both manual and scheduled downloads
- Same validation and error handling for both modes
- Shared logging and audit systems

## Deployment (rek-d01.vedur.is)

**SSH targets**: `bgo@rek-d01.vedur.is` (code/install), `gpsops@rek-d01.vedur.is` (operational checks)

**Role split**: bgo owns venv + code (`~/git/receivers/`); gpsops owns config + data + DB + logs.
bgo is in the gpsops group — can read/write gpsops-owned dirs without owning them.

**Deploy flow**:
1. Merge branch to main
2. On rek-d01 as bgo: `cd ~/git/receivers && git pull`
3. Reinstall: `sudo bash deployment/server/install.sh` (idempotent — safe to re-run; skips protected files like `database.cfg`)

**install.sh stops the scheduler for the duration of the run** (Phase 4 → Phase 10
restart) — every deploy therefore has a short downtime window. It used to stop only
on `--wipe*`, which meant ordinary deploys swapped packages under a live process:
`receivers` is an **editable** install, so the working tree from step 2 *is* the
live code, and imports are lazy. If the run aborts between the two, the scheduler
stays down and the script says so loudly — restart with
`systemctl --user start gps-receivers-scheduler` as gpsops.

**Phase 4 preflights the dependency tree** (`pip install --dry-run -e .`, ~10 s)
*before* the uninstall on line ~530. That uninstall is destructive by design (pip
won't re-fetch a direct-URL dep when the installed version already satisfies it, so
a same-version SHA bump needs it), which means a failed resolve used to leave the
venv with **no tostools** and the deploy worse than it found things — exactly what
happened on 2026-07-31. **Sibling `git+https` pins for the same package must match
EXACTLY across repos**; two differing direct URLs (even pinned-vs-unpinned at the
same commit) are a hard `ResolutionImpossible`. Skipped under `--dev`.

**Config source**: install.sh Phase 5 checks `~/git/gps-config-data/<file>` first, then `config/defaults/<file>`.
`~/git/gps-config-data/receivers.cfg` must exist (non-template) for TCP credentials to deploy correctly.

**PGDATA location (gps_health DB)**: the Postgres cluster data dir lives on the
large data volume — `/mnt/data/postgresql/17/main`, **not** the Debian default
`/var/lib/postgresql/17/main` (relocated 2026-07-09, todo #62; `/var` is a small
24 GB OS volume the growing DB was filling). PGDATA is governed by
`postgresql.conf`'s `data_directory` (an OS/packaging concern), **not** by
`receivers.cfg` — do not try to move it from app config. install.sh Phase 6 only
*warns* if PGDATA is on a different volume than the GPS data root (`data_prepath`);
the move itself is the gated manual runbook
[`docs/deployment/relocate-pgdata-to-mnt-data.md`](docs/deployment/relocate-pgdata-to-mnt-data.md).
A `RequiresMountsFor=/mnt/data` systemd drop-in guarantees boot ordering.

**Log locations** (read as gpsops or bgo via group membership):
- Main log: `~/.cache/gps_receivers/logs/receivers.log`
- Audit: `~/.cache/gps_receivers/logs/download_audit.jsonl`

**Quick health check**: `ssh gpsops@rek-d01.vedur.is 'receivers health GJAC'`

**Scheduler service (systemd `--user`, owned by gpsops)**: the production scheduler
runs as a **user-level** unit `gps-receivers-scheduler.service` (installed to
`~gpsops/.config/systemd/user/`, unit file `deployment/systemd/gps-receivers-scheduler.service`;
install.sh sets it up). **Linger is enabled** (`loginctl enable-linger gpsops`) so it runs
with no active gpsops TTY. Manage it **as gpsops, no sudo** — over SSH set
`XDG_RUNTIME_DIR=/run/user/$(id -u)` first:
```bash
systemctl --user restart gps-receivers-scheduler        # canonical restart
systemctl --user status  gps-receivers-scheduler
journalctl --user-unit   gps-receivers-scheduler -f     # live logs
receivers scheduler stop | restart | status | load-status | backfill-status
```
- `Restart=always`, `RestartSec=30s` → **never `kill` the process** (systemd respawns in 30s);
  stop/restart only via `systemctl --user` or `receivers scheduler stop` (graceful, 120s timeout).
- **`max_workers` in `scheduler.yaml` is the single concurrency knob** — ExecStart has **no
  `--max-workers` flag** by design, so a CLI override won't match the service. To throttle,
  edit `max_workers` in **gps-config-data** `scheduler.yaml`, push (sync timer propagates in
  ~10 min), then `systemctl --user restart`. A local edit on rek-d01 gets reverted by the sync.
- Caps: `CPUQuota=400%`, `MemoryMax=4G`/`MemoryHigh=3G` (only enforced with the
  `user@.service.d/delegate.conf` cgroup-delegation drop-in install.sh lays down). The 400%
  quota is the ~365% CPU ceiling seen under load.
- A **legacy system-level** `gps-receivers-scheduler.service` was removed by install.sh; if you
  see it `not-found/failed` under `systemctl` (system scope), that's the removed unit — the live
  one is the **user** unit above.
- `load_monitoring` (adaptive concurrency backoff) exists but is **disabled** in scheduler.yaml
  by default; `receivers scheduler load-status` reports whether it's on.

**Gotcha**: After a PolaRX5 fw upgrade, `sis` resets to `secure` (TLS-only, port 28784 closes).
Any provisioning changes made on the laptop must be committed to gps-config-data or they will be
lost on the next `install.sh` run.

## Local Development vs Production Grafana

The local Grafana container (`gps-grafana-dev` on `localhost:3001`) reads the laptop's
**local** `gps_health` Postgres. That DB only contains data the laptop has produced —
manually triggered `receivers download …` runs, occasional `receivers scheduler test …`,
ad-hoc backfills. It does **not** mirror rek-d01.

So dashboard counts on the laptop are expected to look alarming. "Missing Raw 23 / Missing
RINEX 62" means "23 stations the laptop hasn't recently downloaded raw data for", not
"23 stations are broken in production". For the real picture, check rek-d01:
- `ssh gpsops@rek-d01.vedur.is 'receivers health <SID>'` (CLI)
- `https://grafana.vedur.is/` (production dashboards, backed by pgdev)

**Laptop config is independent of `gps-config-data`.** The `~/.config/gpsconfig/`
files on the laptop come from the user's dotfiles repo (with credentials encrypted
locally), not from the IMO server config repo. `data_prepath` in particular should
point somewhere reboot-persistent (e.g. `~/tmp/gpsdata/`) — `/tmp/...` is tmpfs.
See "Configuration → Source of truth" above for the production sync flow.

## Querying gps_health

The `gps_health` database (on `pgdev.vedur.is` in production, localhost in dev) has a
cartesian-join footgun: any query that joins two or more `block_*_status` tables on
`USING (sid)` without an additional `ts` predicate will produce a multi-billion-row
plan that can take `pgdev` down. This actually happened on 2026-05-27 and IT had to
kill the query manually. See vault note
[[1779904424-gps-health-cartesian-incident-session]] for the incident write-up.

**Defense in depth, in order of how likely each layer is to save you:**

1. **Server-side timeouts** (always on): `ALTER ROLE bgo IN DATABASE gps_health` sets
   `statement_timeout=60s`, `lock_timeout=5s`, `idle_in_transaction_session_timeout=30s`.
   Even raw `psql` cannot run a 30-minute cartesian — postgres kills it at 60s.
2. **`receivers health-query`** — Python EXPLAIN-gated entry point (this package):
   ```bash
   receivers health-query "SELECT count(*) FROM stations"
   receivers health-query -f path/to/query.sql
   receivers health-query --explain-only "SELECT ..."
   receivers health-query --no-explain "SELECT ..."     # bypass gate, use with care
   receivers health-query --max-rows 1e9 "SELECT ..."   # raise the ceiling
   receivers health-query --host pgdev.vedur.is "SELECT ..."
   ```
   Refuses to execute when the plan's **maximum row estimate across any plan node**
   exceeds `1e8`, or top-of-plan cost exceeds `1e9`. Sets `statement_timeout=60s` and
   `lock_timeout=5s` on the session. Pass/refuse decisions are logged under
   `receivers.cli.health_query`.
3. **`~/.local/bin/gps-health-q`** — shell wrapper with the same enforcement. Available
   on bgo's laptop only; use `receivers health-query` on any other host.

**The join rule** — when joining two or more `block_*_status` tables:

```sql
-- WRONG: cartesian estimate ≈ rows(a) × rows(b)
SELECT … FROM block_power_status p
JOIN block_receiver_status r USING (sid)
WHERE ts > now() - interval '7 days';

-- RIGHT: time alignment in the join, ts predicate on EVERY alias
SELECT … FROM block_power_status p
JOIN block_receiver_status r USING (sid, ts)
WHERE p.ts > now() - interval '7 days'
  AND r.ts > now() - interval '7 days';
```

Prefer the pre-built views in `migrations/004`, `migrations/029`, and `sql/health_views.sql`
over hand-rolling multi-block joins.

## Troubleshooting

### Common Issues
```bash
# APScheduler not available
pip install apscheduler sqlalchemy

# Station configuration not found
export GPS_CONFIG_PATH=~/.config/gpsconfig

# Connection failures
receivers download STATION --test-connection --verbose

# Scheduler debugging
receivers scheduler test --stations STATION --verbose

# Debug verbose output
receivers download STATION --sync --archive -v

# Check Phase 1 utilities are working (look for these log messages):
# "Using Phase 1 TimeParameterProcessor"
# "Using Phase 1 FileArchiver (IMMEDIATE mode)"
# "Archiving complete: X/Y files archived"
```

### Log Locations
- **Main logs**: `~/.cache/gps_receivers/logs/receivers.log` (JSON, rotating 20 MB × 3)
- **Audit trail**: `~/.cache/gps_receivers/logs/download_audit.jsonl` (JSON, rotating 50 MB × 5)
- **Console output**: `ProductionFormatter` (emoji icons) or JSON (`--json-log`)
- Scheduler and all components write to the same `receivers.log` — filter by `logger` field in JSON

## Performance Notes

- **Concurrent downloads**: Default 5 workers, configurable via `--max-workers`
- **Station limits**: Use `--max-stations` for testing subsets
- **Network efficiency**: Time-distributed scheduling prevents congestion
- **Fault tolerance**: Immediate archiving prevents data loss on failures
- **Resource usage**: Production logging optimized for automated systems

## Phase 1 Integration Status

**Status**: ✅ Complete - All 4 receiver types integrated and tested

### Completed Receivers
- ✅ **PolaRX5** - Phase 1 default, tested with ELDC, OLKE, THOB
- ✅ **NetR9** - Phase 1 default, tested with MANA
- ✅ **NetRS** - Phase 1 default, tested with BLEI
- ✅ **G10** - Phase 1 default, tested with SKFC

### Implementation Pattern
All receivers use Phase 1 utilities by default:

1. **Time Processing**: TimeParameterProcessor validates and parses session parameters
2. **Download**: Protocol-specific clients (FTP/HTTP/TCP) download files
3. **Immediate Archiving**: FileArchiver archives each file right after download/processing
4. **Validation**: ArchiveValidator checks file integrity before and after archiving

### Benefits
- **Code Consolidation**: ~540 lines of duplicate code eliminated (Phase 3B)
- **Fault Tolerance**: Immediate archiving prevents data loss on crashes
- **Maintainability**: Single source of truth for common operations
- **Testing**: 72 comprehensive unit tests for Phase 1 utilities
- **Simplicity**: No feature flags, single code path

### Documentation
- **Scheduler guide**: `docs/scheduler/scheduler-guide.md` - Complete operational guide
- Architecture diagrams: `docs/receivers/diagrams/`
- Phase completion notes archived to `docs/archived/`

---

**Last updated**: 2026-02-24
**Package version**: Development (gpslibrary_new)
**Phase Status**: Phase 3C Complete - Distribution window optimization, midnight offset, multi-session backfill, gap detection, archive reconciler, integrity checker, archive format system, unified logging, adaptive download timeouts

## TODO / Known Issues

**Canonical todo list lives in the vault**, not in this file:
[[1778505454-receivers-todos|Receivers Todos]] in `1.Projects/Work_GPS_Receivers/`.
Add new items with `/project-todo Work_GPS_Receivers <task>`. This section keeps
only design context needed to interpret todos (the *why*, not the *what to do*).

### Cfg Reconciliation — TOS Write Mechanics (reference)

`receivers cfg reconcile STATION --push-tos` writes corrected values back to TOS via `tostools.api.tos_writer.TOSWriter`. This is built on TOS's temporal attribute store model:

| Pattern | Use case | Mechanism |
|---------|---------|-----------|
| **1 — Correct in-place** | Fix wrong value, same period | `PATCH /attribute_value/{id_attribute_value}` with new `value` only — dates unchanged |
| **2 — Record change** | New instrument, FW update | Close old period (`PATCH date_to=install_date`) + open new (`POST`) |
| **3 — New attribute** | Attribute never set | `POST /attribute_values` with date_from = station install date |
| **4 — Historical fix** | Correct a closed period | Same PATCH as Pattern 1 but targeting a specific historical `id_attribute_value` |

`--push-tos` currently implements Pattern 1 only — correct the open (currently active) value in-place. Interactive prompt `T` (uppercase) triggers the push; corrected value passes through `to_igs_*()` for IGS name conversion, then PATCHed on TOS.

Key facts:
- `TOSWriter` is dry-run by default — `--dry-run` is safe, `--push-tos` without `--dry-run` sends live writes
- Date format: `"YYYY-MM-DDTHH:MM:SS"` only — no timezone (handled internally by `_tos_date()`)
- TOS stores IGS equipment names; health system reports abbreviated names — conversion via `tostools.standards.igs_equipment`
- Credentials: configure `[tos]` section in `database.cfg` to avoid interactive prompts
- Full reference: `tostools/docs/architecture/tos-write-api.md`

Pattern 2 (instrument change), Pattern 4 (historical fixes), device entity writes, and the scheduled TOS consistency sweep are all tracked as todos — see [[1778505454-receivers-todos#Cfg reconciliation / TOS|todos file]].

### Resolved items (historical reference)

The following high-priority issues were resolved during dashboard/health monitoring development; kept here as context for future AI sessions:

- **Trimble health check (BRIK false CRITICAL)** — `build_health_status()` caps non-HTTP ports at WARNING for NetR*/NetRS/NetR5
- **Protocol-agnostic data model** — `ftp_open=NULL`/`control_open=NULL` for Trimble is semantically correct (N/A)
- **Status value vocabulary** — migration 034 + `connectivity_writer.py` Path 2 fallback now writes `'ok'` (both `'ok'` and `'open'` accepted downstream)
- **Receiver capability awareness** — migration 034 fixed `health_good` CTE NULL bug `(NOT dp.X OR h.X)` → `(dp.X IS NOT TRUE OR h.X IS TRUE)`

Full code-review history (50+ resolved items): `docs/CODE_REVIEW_TRACKER.md`.

**Maintainer**: Veðurstofa Íslands GPS Team