# Long-Term Backfill — Design

**Status:** draft (bgo, 2026-08-08) — manual test in progress (KOSK/SARP). Track via
receivers todo [[#136]]. Related: [[#139]] (flaky-link guardrails), [[#59]] (EPOS
gap-reconciler). Sibling design: [`morning-recovery.md`](morning-recovery.md).

## 1. Goal

Recover multi-day/month data gaps when a station returns from an outage —
**DB-driven and classified**, running the **full ingestion pipeline**, and
**throttled to yield to the live RT runs**. This is the *long-term* pass; it is
deliberately distinct from:

- **subdaily backfill** (`gap_detection.days_back=7`, `:25–:55` self-gating) —
  trailing-week gap fill, working as designed.
- **morning_recovery** — yesterday's file, PolaRX5-focused, GAMIT-deadline-guarded.

The long-term pass generalizes `morning_recovery`'s shape (DB-classified missing
query + full-pipeline reuse) to a **long window** anchored to each station's true
gap start, triggered primarily by **reconnection**.

## 2. The pieces already exist — this is wiring

| Capability | Existing piece | State |
|---|---|---|
| Unified file index | `file_tracking` (status archived/downloaded/missing/…) | ✅ live |
| Receiver absence + auto-delete signal | `file_absence` (`terminal`, `confirmations`) | ✅ live |
| Absence API | `is_file_missing` / `mark_file_missing` / `record_file_absence('receiver')` (`health/file_tracker.py`) | ✅ |
| Gap classifier | `GapDetector.find_gaps(skip_missing_on_receiver=True)` | ✅ |
| DB-classified missing query (1 date) | `morning_recovery._query_stations_missing_yesterday` (buckets: queued/passive/already_ok/marked_missing) | ✅ — **generalize to a range + receiver-absence bucket** |
| Auto-delete frontier (oldest file held) | `receiver_horizon_probe` → `receiver_horizon` table (`upsert_receiver_horizon`); dynamic floor for `missing_on_receiver` | ⚠️ **disabled** — enabling is a prerequisite |
| Full live pipeline | `_download_station_data_job` (download→RINEX→archive→push→DB) | ✅ — reuse end-to-end |
| Load/yield signal | `load_monitoring` (`LoadMonitor`: CPU/network/jobs) | ⚠️ **disabled** — enabling is a prerequisite |
| Reconnection signal | `station_connectivity` (`state_since`, `is_online`, `packet_loss`), `block_ping_status` history | ✅ |
| Gateway push | `run_epos_disseminate_job(dates=…)` (`epos_disseminate`) | ✅ — call explicitly for recovered `in_epos` days |

**What's actually missing (the build):**
1. A range + receiver-absence generalization of the morning_recovery query → `_query_long_term_gaps`.
2. A classified worker `_long_term_backfill_station` that runs the **full pipeline** per gap and **skips terminal-gone** days (the current `_backfill_station_day_generic` dumb-iterates dates and doesn't consult `find_gaps`/`is_file_missing`).
3. The **reconnection trigger** job + a daily backstop job + their `_schedule_*` wiring.
4. Driving `missing_on_receiver` from `receiver_horizon` + `file_absence` (slice-2b).
5. Fixed counter semantics (today "missing=1" conflates *already-archived* with *genuinely gone*).
6. Explicit EPOS push for recovered days > `epos_disseminate.days_back` (or hand to #59).

## 3. Trigger model

Two paths, both on the `backfill` executor:

- **Reconnection trigger (primary)** — `reconnection_backfill`, every ~10–15 min.
  Query `station_connectivity` for `is_online AND state_since > now()-15m`, join
  `block_ping_status` history to measure the preceding outage; for any station
  whose outage ≥ `min_outage_days_to_trigger`, enqueue a long-term backfill with
  `backfill_start = last_archived_date(sid, session) + 1` (the **true gap start**,
  not `today-7`). Cheap (DB-only); fires the heavy work only on real returns.
- **Daily backstop** — `long_term_backfill`, once daily in a low-activity slot
  (e.g. `04:00`, after the horizon probe at `04:40`-ish but before GAMIT guard).
  Runs `_query_long_term_gaps` across all active stations/sessions and enqueues
  anything with gaps. Catches whatever the reconnection trigger missed (e.g.
  scheduler downtime during the transition).

Enqueue reuses `backfill_progress` but writes the **gap-anchored** range; the
existing `LEAST/GREATEST` widening then preserves the earliest start across
re-enqueues (the fix to the 7-day ceiling).

## 4. Gap collection (classified)

Generalize `morning_recovery._query_stations_missing_yesterday` to a date **range**
with an extra receiver-absence dimension. Per `(sid, session)` over
`[last_archived+1 … yesterday]` (capped at `max_lookback_days` and by
`receiver_horizon`), bucket each expected file:

| Bucket | Meaning | Action |
|---|---|---|
| `queued` | not in archive, not confirmed gone | **download** (full pipeline) |
| `confirmed_gone` | `file_absence.terminal` **or** date < `receiver_horizon[sid]` | skip, report (auto-deleted) |
| `provisional_absent` | `file_absence` row, not terminal | low-priority retry (confirm → terminal) |
| `needs_rinex` | raw archived, no rinex | re-rinex only (from `missing_rinex` view) |
| `already_ok` | archived | skip |

Engine: `GapDetector.find_gaps(skip_missing_on_receiver=True)` already produces
the `queued` set; the extra buckets come from joining `file_absence` +
`receiver_horizon` in the generalized query.

## 5. Auto-delete-cycle check (= `receiver_horizon`)

A date older than `receiver_horizon[sid]` (the oldest file the receiver still
holds, probed daily by `receiver_horizon_probe`) is **permanently gone** — mark
`terminal`, never retry. This is the "data has run off the receiver auto-delete
cycle" signal. Layered with `file_absence.confirmations` (N independent
verified-`not_found` confirmations ⇒ terminal) so a single flaky-day listing
can't prematurely condemn data.

**Prerequisite:** enable `receiver_horizon_probe` (currently disabled) and drive
`missing_on_receiver` from `receiver_horizon` + `file_absence` (slice-2b; the view
is empty today because it uses a static floor with no horizon populated).

## 6. Worker (the core wiring fix)

New `_long_term_backfill_station(session, sid, gap_list)` iterates the
**classified gap list** (not a date range):

- `queued` → `_download_station_data_job` (the **full** pipeline: download →
  RINEX → archive → push-storage → DB). This is what morning_recovery already
  reuses, so behaviour stays identical to live ingestion.
- On a verified `not_found` (receiver reached, file absent) →
  `record_file_absence('receiver')`; promote to `terminal` after the confirmation
  threshold or once past `receiver_horizon`.
- `confirmed_gone` / `already_ok` → skip (correctly counted, not mislabeled).
- `needs_rinex` → `_run_rinex_conversion` only.
- **Gateway push**: for each recovered day on an `in_epos` station, call
  `run_epos_disseminate_job(dates=[that day])` explicitly (the daily
  `epos_disseminate` sweep only covers `days_back=7`, so older recovered days
  would otherwise orphan — see #59). Wrap gateway pushes behind a hook so future
  sinks slot in without touching the worker.

**Counter fix:** report `downloaded / confirmed_gone / provisional_absent /
needs_rinex / anomalous` distinctly — never count an already-archived day as
"missing".

## 7. Health oracle (Point 1, ties to #139)

- **Gate each attempt** on `station_connectivity.is_online` (recent `last_check`);
  if offline, skip the slot (the reconnection trigger will catch the return).
- **Scale retry aggressiveness by `packet_loss`**; add the reachability-gate
  retry from #139 (N probes with backoff before declaring unreachable) so a flaky
  link doesn't burn the only attempt.
- **Reconnection detection** itself *is* the health system: `state_since` +
  `block_ping_status` give the transition and outage length — no blind probing.

## 8. Resource discipline (yield to RT)

The manual test shows the work is **bandwidth-limited, not CPU-limited** (~130 s
per 2.6 MB file; 0.7% CPU). So the strain risk is *sustained* network/DB/RINEX
over hours, not bursts. Controls:

- `backfill` executor (shared), but the long-term job caps its **own** concurrency
  low (`max_workers: 2–3` stations, RINEX serialized 1–2 at a time) — it must not
  consume the whole pool.
- **`yield_to_rt: true`**: (a) skip the live `:01` window via the self-gate
  minute-check; (b) consult `_load_monitor` (`load_monitoring.enabled`) and **pause**
  when CPU/network/active-jobs exceed threshold; (c) long-term RINEX takes a
  lower-priority semaphore behind live RINEX.
- `coalesce=True`, `max_instances=1`, per-station `station_timeout_minutes`.
- Because it's slow, prefer **continuous low-rate** over a burst window — a
  returning station's multi-month gap drains over successive ticks without
  spiking.

## 9. Config additions (`scheduler.yaml`)

```yaml
receiver_horizon_probe:
  enabled: true              # PREREQUISITE: auto-delete frontier → missing_on_receiver floor
load_monitoring:
  enabled: true              # PREREQUISITE: yield_to_rt signal
long_term_backfill:
  enabled: false             # ship disabled; manual-trigger during validation
  reconnection_trigger_schedule: "15m"
  daily_backstop_schedule: "04:00"
  max_workers: 2             # LOW — bulk, yields to RT
  station_timeout_minutes: 20
  max_lookback_days: 365     # also bounded per-station by receiver_horizon
  min_outage_days_to_trigger: 3
  sessions: [15s_24hr, 1Hz_1hr]
  rinex: true
  push_storage: true
  push_epos: true            # explicit per-day push for recovered in_epos days
  yield_to_rt: true
```

## 10. Reporting

- Daily summary log per station/session: `downloaded / confirmed_gone /
  provisional_absent / needs_rinex / anomalous / terminal_skipped`.
- Persist per-run rows (extend `backfill_progress` or a small
  `long_term_backfill_runs` table) for Grafana + audit.
- **Flakiness escalation (#139)**: sustained-high-`packet_loss` / repeated
  transient-failure stations surfaced to ops (daily report + Icinga), not silent.

## 11. Prerequisites & rollout

1. Enable `receiver_horizon_probe` + `load_monitoring` (config only; both inert today).
2. Drive `missing_on_receiver` from `receiver_horizon` + `file_absence` (slice-2b).
3. Build `_query_long_term_gaps` (generalize morning_recovery) + the bucket joins.
4. Build `_long_term_backfill_station` (full pipeline, classified, skips terminal, fixed counters).
5. Wire `_schedule_reconnection_backfill` + `_schedule_long_term_backfill` (mirror existing `_schedule_*`; backfill executor, `max_instances=1`).
6. Reporting + Grafana panel.
7. EPOS explicit-push hook (coordinate with #59).

Rollout gate: manual test (this run) confirms download+archive recovery → confirm
classify path (`confirmed_gone` via horizon) → confirm full pipeline (RINEX+EPOS)
→ confirm RT unaffected under throttle → enable.
