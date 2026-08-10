# Gap detection and backfill: window units and walk direction

**Status:** design agreed 2026-08-10 (bgo), not yet implemented.
Deferred until the Aug 8/9 hourly recovery completes — see "Sequencing" below.

Origin: the 2026-08-08/09 outage, where `1Hz_1hr` silently kept a ~5,100-file
hole that no mechanism would ever have retried. See
[[load-gate-starves-live-downloads]] and the incident notes in
`docs/deployment/`.

## The principle

Two mechanisms recover missing files, and they should sweep in **opposite
directions**, because they are racing different things:

| Job | Direction | Why |
|-----|-----------|-----|
| `gap_detection` → backfill | **newest → oldest** | keeps *current* data whole; a fresh hole should be filled in minutes, not after a walk through history |
| `long_term_backfill` | **oldest → newest** | races the receiver **ring buffer** — the oldest recoverable data is nearest the edge and is what disappears first |

Today both walk oldest-first. That is why, on 2026-08-10, gap detection
correctly re-queued 180 stations for `1Hz_1hr` and then began fetching
**2026-07-17** files while the at-risk Aug 8 data sat behind a ~90-day queue,
with the tightest-retention receiver holding only back to Aug 8.

## The four changes

### 1. Window unit must follow session cadence

`gap_detection.days_back: 7` on an hourly session means **168 files per
station** — which is why the first 3-session run reported
`1Hz_1hr: 8620 gaps / 30408 expected`. For a 1 Hz session the natural unit is
**hours**: `7` should mean 7 files.

Precedent already exists in the sessions block: `1Hz_1hr` carries
`lookback_periods: 3` — periods, not days. Gap detection never adopted that
vocabulary.

`GapDetector.get_gap_summary()` (`health/file_tracker.py:2318`) is day-granular:

```python
end_date   = date.today() - timedelta(days=1)   # Yesterday
start_date = end_date - timedelta(days=days_back - 1)
```

Needs an hour-granular path for hourly sessions.

### 2. Gap detection must be able to see *today*

`end_date = date.today() - timedelta(days=1)` means gap detection **structurally
cannot see the current day**. For a daily session that is right — today's 24 h
file does not exist yet. For an hourly session it is a bug: an hour missed this
morning stays invisible until tomorrow, which is precisely the failure the job
exists to catch.

This one is worth fixing on its own merits, independent of the rest.

### 3. Gap-driven backfill walks newest-first

Two separate things, easily confused:

- **Cursor direction** — `_process_one_station_date()` does
  `next_date = process_date + timedelta(days=1)`, i.e. always forward from the
  oldest queued date. This is the one that matters.
- **Within-window file order** — `reverse_chronological` (`backfill.py:509`,
  consumed at `septentrio/polarx5.py:989`) orders files *inside* one download
  call. It is currently `False`.

Both need to point newest-first for the gap-driven path. Note `_enqueue_backfill()`
sets `next_date = LEAST(existing, CURRENT_DATE - days_back)`, so a station whose
cursor already sat in May stays in May — widening the range cannot help until the
direction changes.

### 4. `long_term_backfill` walks oldest-first

Confirm and keep. It already reads `receiver_horizon` (`long_term_backfill.py:86`)
and buckets `confirmed_gone` for anything older than the horizon, so it has the
right information to prioritise the ring-buffer edge.

## Sequencing

Narrowing `1Hz_1hr` gap detection from 7 days to 7 hours **removes the automatic
coverage of the Aug 8/9 hole**, which the 7-day window is currently providing.
Order of operations:

1. Finish recovering Aug 8/9 (manual raw-only parallel fetch — raw is what the
   ring buffer threatens; RINEX regenerates from it).
2. Land change 4 (oldest-first long-term backfill) so deep history has an owner.
3. Then land 1–3 and narrow the hourly window.

## Related unfixed defect

**Converter concurrency has no throttle.** With `load_monitoring` disabled, 16
concurrent converters ran against a `CPUQuota=400%` and drove load to 91 on
2026-08-10. Trimble conversion is the expensive path — `convertToRINEX.exe` under
docker+wine measured at **~34 minutes for a single daily file**. Any redesign that
increases backfill throughput will make this worse before it makes it better.
