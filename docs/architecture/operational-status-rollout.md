# Operational-status rollout — receivers code-change map

Audited 2026-08-19. Maps every receivers consumer of the stations.cfg
operational fields to its required change under the new model
(`operational_status` + `external_data_type`, see `operational-status-model.md`).

## Consumers by field

### `station_role = passive` → `operational_status = reference` (keep cfg-only for now)

Reference stations stay in stations.cfg only (not TOS). No behavioural change
today; these skip-sites remain correct. Eventually the flag becomes
`operational_status = reference`.

| Location | What it does | Change |
|---|---|---|
| `bulk_scheduler.py:1798` | skip passive in station load | none (keep) |
| `cli/main.py:3897` (`get_all_station_configs`) | exclude passive | none (keep) |
| `cli/cfg.py:325` (reconcile) | exclude passive | none (keep) |
| `db/seeder.py:91` | skip passive | none (keep) |

### `health_check = passive` → `operational_status = external` (NEW: fetch externally)

Today `health_check=passive` means "skip download + skip health polling".
Under the new model it means **external station** — so it must be *fetched*
(not just skipped), and it is no longer the monitoring toggle.

| Location | What it does | Change |
|---|---|---|
| `bulk_scheduler.py:1805-1836` | load + normalize `health_check` | add `operational_status` / `external_url_template` to station dict |
| `bulk_scheduler.py:3377` (`_get_stations_for_session`) | skip passive from session download | keep skip (external has no receiver); external fetch is a separate job |
| `bulk_scheduler.py:3499-3503` (health selection) | skip passive from health poll | derive from `operational_status != operated` |
| `bootstrap.py:116` | skip passive | → `operational_status == external` |
| `gap_scheduler.py:50`, `archive_reconciler.py:121`, `long_term_backfill.py:387`, `receiver_horizon_probe.py:96`, `cli/scheduler.py:634` | skip passive from background jobs | → `operational_status == external` (or `!= operated`) |
| `db/seeder.py:164,214,235,256` | sync `health_check` to DB | keep column for now; add `operational_status` |
| `cli/cfg.py:549` | display `health_check` | display `operational_status` too |

### `station_status` (lifecycle) → keep; derive from TOS later

`inactive`/`discontinued` map to TOS (`date_end`, open-receiver). Keep the cfg
field for now — it's the lifecycle axis, orthogonal to `operational_status`.

| Location | Change |
|---|---|
| `bulk_scheduler.py:1802-1835,1869-1924` (auto-detect inactive + DB sync) | keep |
| `bootstrap.py:114`, `integrity_checker.py:88`, `long_term_backfill.py:386` | keep |

### `download_method` — dead

Zero consumers. Remove from stations.cfg (superseded by
`operational_status` + `external_url_template` + `external_data_type`).

### `acquisition_mode` — keep (transport, orthogonal)

`stream_scheduler.py` + `bulk_scheduler.py` — unchanged. Note MYVA is a
**hybrid**: 15s RINEX3 via external fetch (LMI), 1Hz via RTCM/BNC stream
(legacy `rtcm2rinex.sh` on rek2, outside receivers). Both paths coexist.

## NEW code required

1. **Scheduler external-fetch job** — a periodic job (backfill executor) that
   enumerates `operational_status = external` (or `external_url_template`
   present) stations and calls `receivers.external_fetch.fetch_external_station`,
   writing RINEX straight to `data_prepath` (no conversion — `external_data_type = rinex`).
2. **`operational_status` on the station dict** in `bulk_scheduler` station
   loading, so jobs can branch on it.
3. **Health-poll skip** — replace `health_check == "passive"` with
   `operational_status != "operated"` (external + reference don't get polled;
   operated always does).

## Sequencing

1. ✅ external fetcher + `--external` flag + MYVA config.
2. Scheduler external-fetch job + `operational_status` on station dict.
3. Replace `health_check == passive` checks with `operational_status`.
4. Retire `download_method`; retire rek2 getmyva.sh (new path proven).
5. KRAC/THRC/TORK (UI) — on ice (source layout unknown).
