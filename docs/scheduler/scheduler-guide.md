# GPS Receiver Bulk Download Scheduler Guide

## Overview

The bulk download scheduler provides automated, distributed downloading of GPS data from 173+ receiver stations using APScheduler. It distributes downloads across time windows to prevent network congestion while maintaining complete compatibility with manual operations.

## Installation

```bash
# Install with scheduler dependencies
pip install -e ".[scheduler]"

# Or install all optional dependencies
pip install -e ".[all]"
```

## Quick Start

### 1. Create Configuration

```bash
receivers scheduler config --create
```

Creates `~/.config/gps_receivers/scheduler.json` with default settings.

### 2. Test Setup

```bash
# Test with limited stations
receivers scheduler test --stations ELDC ORFC THOB --max-stations 3
```

### 3. Start Scheduler

```bash
# Start with limited stations for testing
receivers scheduler start --stations ELDC ORFC --max-workers 2 --verbose

# Production: all stations
receivers scheduler start --max-workers 5
```

### 4. Check Status

```bash
# Basic status
receivers scheduler status

# Detailed job information
receivers scheduler status --show-jobs
```

## Architecture

### Scheduling Strategy

#### Time Distribution
Downloads are distributed across time windows to prevent network congestion:

**15s_24hr (Daily Data)**:
- Schedule time: 00:10-00:19 (10-minute window)
- 173 stations → ~17 stations/minute
- Frequency: Once per day

**1Hz_1hr (Hourly Data)**:
- Schedule time: XX:15-XX:24 (10-minute window)
- 173 stations → ~17 stations/minute
- Frequency: Every hour

**status_1hr (Status Files)**:
- Schedule time: XX:25-XX:29 (5-minute window)
- 173 stations → ~35 stations/minute
- Frequency: Every hour

#### Example Distribution

With 173 stations and 10-minute window:
```
Minute 15: Stations 1-17
Minute 16: Stations 18-34
Minute 17: Stations 35-51
...
Minute 24: Stations 154-170
```

### Concurrent Execution

- **ThreadPoolExecutor**: Configurable worker pool (default: 5 workers)
- **Max instances**: 1 per job (prevents duplicate downloads)
- **Job persistence**: SQLite database survives scheduler restarts
- **Graceful shutdown**: Waits for running jobs to complete

### Fault Tolerance

- **Immediate archiving**: Files archived as downloaded (Phase 1)
- **Error isolation**: One station failure doesn't affect others
- **Retry logic**: Configurable per task type (future)
- **Audit logging**: All operations logged for monitoring

## Configuration

### Scheduler Configuration

**Location**: `~/.config/gps_receivers/scheduler.json`

```json
{
  "database_url": "sqlite:///~/.cache/gps_receivers/scheduler.db",
  "log_dir": "~/.cache/gps_receivers/logs",
  "production_mode": true,
  "max_workers": 5,
  "sessions": {
    "15s_24hr": {
      "enabled": true,
      "schedule_minute": 10,
      "distribution_window": 10,
      "frequency": "daily",
      "max_concurrent": 3,
      "timeout_minutes": 45
    },
    "1Hz_1hr": {
      "enabled": true,
      "schedule_minute": 15,
      "distribution_window": 10,
      "frequency": "hourly",
      "max_concurrent": 4,
      "timeout_minutes": 30
    },
    "status_1hr": {
      "enabled": true,
      "schedule_minute": 25,
      "distribution_window": 5,
      "frequency": "hourly",
      "max_concurrent": 5,
      "timeout_minutes": 15
    }
  }
}
```

### Session Configuration

Each session type has:
- **schedule_minute**: When to start (minute past hour/day)
- **distribution_window**: Minutes to spread downloads
- **frequency**: `"hourly"` or `"daily"`
- **max_concurrent**: Max simultaneous downloads for this session
- **timeout_minutes**: Task timeout

## Manual Operation Compatibility

**Key principle**: Scheduler and manual operations coexist perfectly.

### Manual Downloads Work Anytime

```bash
# Manual download works even while scheduler running
receivers download ELDC --sync --archive
```

### Same Configuration, Same Behavior

Both use:
- Same station configurations
- Same Phase 1 utilities (immediate archiving)
- Same validation logic
- Same error handling

### No Conflicts

- Manual downloads don't interfere with scheduled jobs
- Scheduled jobs don't block manual operations
- Both use file-level locking (via immediate archiving)

## Monitoring and Logging

### Log Files

- **Scheduler log**: `~/.cache/gps_receivers/logs/scheduler.log`
- **Receiver logs**: `~/.cache/gps_receivers/logs/receivers.log`
- **Audit trail**: `~/.cache/gps_receivers/logs/download_audit.jsonl`

### Audit Trail Format

Each download logged as JSON:
```json
{
  "event_type": "download_session",
  "station_id": "ELDC",
  "session": "1Hz_1hr",
  "status": "completed",
  "duration_seconds": 15.3,
  "files_downloaded": 2,
  "bytes_downloaded": 3500000,
  "errors": 0,
  "scheduled": true,
  "start_time": "2025-09-30T14:00:00",
  "end_time": "2025-09-30T15:00:00"
}
```

### Monitoring Integration

Audit trail can be consumed by:
- Log aggregation systems (Splunk, ELK)
- Monitoring tools (Prometheus, Grafana)
- Alerting systems (Icinga, Nagios)

## Extensibility: Task Interface

> ### ⚠️ Read this before following the pattern below
>
> **The live scheduler does not execute most of these classes.** As of
> 2026-08-26, `TaskFactory.create()` has **no production call site**. The
> scheduler wires APScheduler directly to module-level `_*_job` functions in
> `bulk_scheduler.py` (`_download_station_data_job`, `_status_check_job`,
> `_run_gap_detection_job`, `_run_archive_reconciler_job`,
> `_run_integrity_check_job`, ...).
>
> | Task class | Status |
> |---|---|
> | `RINEXTask` | **LIVE** — instantiated at `bulk_scheduler.py:1147,1180` (backfill path) |
> | `DownloadTask` | written, registered, **never instantiated** |
> | `StatusTask` | written, registered, **never instantiated** — `_status_check_job` re-implements it inline |
> | `HealthTask` | written, registered, **never instantiated** |
> | `SyncTask` | written, registered, **never instantiated** |
>
> **The registry is not even populated.** `_init_task_factory()`
> (`task_interface.py:331`) is *defined but never called* — the comment above it
> says "Deferred initialization ... Registration happens when tasks module is
> fully loaded", but nothing performs that registration. Verified:
>
> ```python
> >>> from receivers.scheduling.task_interface import TaskFactory
> >>> TaskFactory.get_registered_types()
> []                                    # empty at import
> >>> TaskFactory.register_builtin_tasks()   # only if you call it by hand
> >>> TaskFactory.get_registered_types()
> [TaskType.DOWNLOAD, TaskType.STATUS, TaskType.HEALTH, TaskType.RINEX, TaskType.SYNC]
> ```
>
> So `TaskFactory.create(TaskType.DOWNLOAD, ...)` would raise for an unregistered
> type in a fresh process. Anyone building on this interface has to discover that
> first.
>
> This matters because a parallel implementation drifts. `StatusTask`'s failure
> path carried an offline-sample-on-failure behaviour the live `_status_check_job`
> lacked; it was eventually ported by hand (see the comment at
> `bulk_scheduler.py:1450`, "status_task.py has done this on its own failure
> path all along"). A fix applied to a class nothing runs helps nobody.
>
> **The de facto extension seam is "module-level `_*_job` function + a
> `_schedule_*` registrar", not `ScheduledTask`.** If you add a task type, wire
> it the way the live jobs are wired, or adopt the interface for the existing
> jobs in the same change — do not add a sixth unexecuted class.
>
> Background: `docs/architecture-review-2026-08-26.md` §2 and §4.6.

### Architecture

The scheduler defines an extensible task interface intended for task types
beyond downloads. See the caveat above for how much of it is actually load-bearing.

### Current Implementation

```python
from receivers.scheduling.tasks import RINEXTask       # the one that actually runs
from receivers.scheduling.task_interface import TaskType, TaskConfig
```

### Adding New Task Types (aspirational — see the caveat above)

```python
from receivers.scheduling.task_interface import (
    ScheduledTask, TaskResult, TaskType, TaskFactory
)

class StatusTask(ScheduledTask):
    """Check receiver status."""

    def get_time_parameters(self):
        # Current time (status is real-time)
        now = datetime.utcnow()
        return now, now

    def validate_prerequisites(self):
        # Check receiver is reachable
        ...

    def execute(self):
        # Get receiver status
        receiver = self._create_receiver()
        status = receiver.get_connection_status()

        return TaskResult(
            success=status['receiver'],
            status='online' if status['receiver'] else 'offline',
            duration=1.0,
            message=f"Status: {status}",
            data={'status': status}
        )

# Register with factory
TaskFactory.register(TaskType.STATUS, StatusTask)

# Scheduler can now schedule status checks
```

### Benefits

1. **No scheduler changes needed** - TaskFactory handles registration
2. **Consistent interface** - All tasks use same patterns
3. **Easy testing** - Tasks are independent, mockable
4. **Clean separation** - Task logic separate from scheduling logic

## Testing

### Unit Tests

```bash
# Run all scheduler tests
pytest tests/test_scheduler_basic.py tests/test_scheduler_execution.py -v

# Test specific functionality
pytest tests/test_scheduler_basic.py::TestSchedulerTimeDistribution -v
```

### Integration Tests (Future)

```bash
# Test with real stations (limited)
pytest tests/integration/test_scheduler_live.py --stations ELDC ORFC -v
```

## Troubleshooting

### Scheduler Won't Start

```bash
# Check APScheduler installation
pip list | grep apscheduler

# Install if missing
pip install -e ".[scheduler]"

# Check database
ls -l ~/.cache/gps_receivers/scheduler.db

# Remove corrupted database
rm ~/.cache/gps_receivers/scheduler.db
receivers scheduler start
```

### No Jobs Scheduled

```bash
# Check station configurations
receivers scheduler test --verbose

# Verify sessions enabled in config
receivers scheduler config --show
```

### Jobs Not Executing

```bash
# Check scheduler status
receivers scheduler status --show-jobs

# Check logs
tail -f ~/.cache/gps_receivers/logs/scheduler.log

# Verify stations enabled in gps_parser config
```

### High CPU/Memory Usage

```bash
# Reduce concurrent workers
receivers scheduler start --max-workers 3

# Limit stations (testing)
receivers scheduler start --max-stations 50

# Check for stuck jobs
receivers scheduler status
```

## Production Deployment

### Systemd Service (Example)

```ini
[Unit]
Description=GPS Receiver Bulk Download Scheduler
After=network.target

[Service]
Type=simple
User=gpsuser
WorkingDirectory=/opt/receivers
ExecStart=/opt/receivers/venv/bin/receivers scheduler start --max-workers 5
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

### Deployment Checklist

- [ ] Install with scheduler dependencies
- [ ] Create configuration file
- [ ] Test with limited stations
- [ ] Verify logs rotating properly
- [ ] Set up monitoring alerts
- [ ] Configure systemd service
- [ ] Test restart/recovery
- [ ] Monitor for 24-48 hours
- [ ] Gradually increase station count

## Performance Metrics

### Expected Performance (173 stations)

- **15s_24hr**: ~173 downloads/day → ~10 minutes total
- **1Hz_1hr**: ~173 downloads/hour → ~10 minutes per hour
- **status_1hr**: ~173 downloads/hour → ~5 minutes per hour

### Resource Usage

- **Memory**: ~200-500 MB (5 workers)
- **CPU**: Minimal (waiting on network I/O)
- **Disk**: ~1-2 GB/day (compressed data)
- **Network**: Distributed across time windows

### Database Size

- **Scheduler DB**: <10 MB (job metadata only)
- **Audit log**: ~50-100 MB/month (JSON entries)
- **Main logs**: ~200-500 MB/month (rotated)

## FAQ

**Q: Can I run manual downloads while scheduler is running?**
A: Yes! Manual operations work independently and don't interfere with scheduled jobs.

**Q: What happens if scheduler is restarted?**
A: Jobs persist in SQLite database. Missed jobs within grace period (5 min) will execute on restart.

**Q: How do I temporarily disable a session type?**
A: Edit scheduler.json, set `"enabled": false` for that session, restart scheduler.

**Q: Can I change time windows?**
A: Yes, edit `schedule_minute` and `distribution_window` in config, restart scheduler.

**Q: How do I add a new station?**
A: Add to gps_parser config. Scheduler automatically picks it up on next start.

**Q: What if a download fails?**
A: Error logged to audit trail. Other stations unaffected. Can retry manually.

## See Also

- [Phase 3C Completion Document](../phase3c_complete.md)
- [Task Interface Documentation](../../src/receivers/scheduling/task_interface.py)
- [CLAUDE.md - Scheduler Section](../../CLAUDE.md)
