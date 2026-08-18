# Deterministic metadata extraction & reconstruction verbs

Status: design / playbook (2026-06-30). Captures the entity-extraction "sweeps"
run during the 2026-06 TOS-metadata reconstruction and the path to making every
one a **deterministic, config-driven CLI verb** — no hardcoded paths, no one-off
scripts.

## Why

The reconstruction (lifecycle cleanup, 28-station firmware histories, the 14/14
missing-receiver backlog, the BAUG/SNAE rove fixes) leaned on a mix of:

- existing, deterministic verbs (`tos audit fleet-sweep`, `cfg reconcile`, …) and
- **ad-hoc scratch scripts** (`fw_miner.py`, `serial_exists.py`, `fw_triage_gen.py`,
  `groupb_prep.py`) with **hardcoded archive paths** (`/mnt_data/rawgpsdata`).

The ad-hoc parts are not reproducible, not testable, and embed data locations that
differ per host. The goal: fold each into a verb that resolves its inputs from the
config that already exists, so a future operator (or the scheduler) reruns the same
extraction by name.

## The six authorities an extraction reconciles

| # | Source | Access | Truth it carries |
|---|--------|--------|------------------|
| 1 | `stations.cfg` | `GPS_CONFIG_PATH` → gps_parser | operator's *intended* config |
| 2 | TOS | `tostools.api` (`/tos/internal`) | the metadata DB of record |
| 3 | live receiver | probe (SBF 5902 / Trimble HTTP) | hardware *self-report* |
| 4 | cold RINEX archive | filesystem (read-only mount) | *empirical* history (REC/ANT/VERS headers) |
| 5 | **raw SBF** | `sbf2rin` decode of the archived raw | *empirical* truth where the header **cannot** answer |
| 6 | **GAMIT `station.info`** | `resolve_station_info()` (okada `/D/DATABASE`) | the **field record** — surveyed heights, curated models |

Determinism rule: a verb names which authorities it reads and resolves every path
from config; given the same TOS + archive state it always returns the same result.

**5 and 6 were added 2026-08-18.** Both existed and did real work; neither was
named here, and both broke the determinism rule in the same way — an
undocumented source whose path or presence you had to read the code to discover.

*Raw SBF* is the only authority that can answer the **2017-2025** span: those
archived headers are RINEX 2, which under-reports constellations, so absence in
them proves nothing. `tos audit constellations` decodes the day's raw when the
header is R2 or unparseable. The catch that makes naming it necessary: a decode
produces an R3 reading, so `version`/`reliable` are indistinguishable from a
genuine R3 header — the report would otherwise claim the *archive* recorded
something it never did. `ConstellationReading.origin` (`rinex_header` |
`sbf_decode`) now states which replied.

*station.info* was reachable only by hardcoding
`--station-info data/station_config/station.info.sopac.apr05` — a packaged 2005
snapshot — and **without that flag the audits did no enrichment at all**, so the
hardcoded path was load-bearing. It now resolves override → env → cfg → mount →
packaged, mirroring `cold_archive_prepath`, and reports its origin. okada
exported `/D/DATABASE` read-only on 2026-08-18, so `origin=mount` (the live file)
is the normal case.

**Both follow the same rule, and it is the general one: an authority must name
itself in its own output.** "Which copy did I read, and is it live?" has to be
answerable from the run, not from the source.

## Extraction catalogue — current form → target verb → path config

| Process | Authorities | Current form | Target verb | Path source |
|---|---|---|---|---|
| Fleet defect sweep (KRIV-class) | 1,2 | ✅ `tos audit fleet-sweep` | (done) | `GPS_CONFIG_PATH` |
| 3-way field reconcile (11 fields) | 1,2,3 | ✅ `cfg reconcile --all --source both` | (done) | cfg (router IPs) |
| Duplicate-serial detector (join-index) | 2 | ✅ `tos audit duplicate-serials` | (done) | TOS only |
| Cross-check TOS vs cold archive | 2,4 | ✅ `tos audit verify-from-rinex` | (extend, see below) | **hardcoded → config** |
| **Serial → entity/location lookup** | 2 | ⚠ `serial_exists.py` | **NEW `tos device find --serial`** | TOS only |
| **Archive header timeline (fw/rx/ant)** | 4 | ⚠ `fw_miner.py` | **NEW `tos audit rinex-timeline`** | **sync.yaml `source_root`** |
| **Firmware multi-period chain emit** | 4,2 | ⚠ `fw_triage_gen.py` | **NEW `tos audit firmware-chain --emit-triage`** | archive + TOS |
| Write a closed/open attr period | 2 | ✅ `apply add-attribute-period` | (done, this session) | — |
| Register a deployed device | 2,3 | ✅ `cfg add-receiver/-antenna/-monument` | (done; +`--triage/--commit/--model`) | cfg |
| **Station onboarding batch** | 1,2,3,4 | ⚠ `groupb_prep.py` | **NEW `cfg onboard-station`** (orchestrator) | all of the above |
| Roved/duplicate serial arbitration | 2,4 | manual (BAUG/SNAE) | **extend `duplicate-serials --arbitrate`** | archive |

## Proposed new verbs

### `tos device find --serial <S> [--subtype gnss_receiver]`
The reliable serial→entity lookup. Walks the global **join index** (`build_join_index`),
*not* `basic_search` (which returns `None` for serials that exist → silent dup-mint, the
GRAN incident). Reports every entity with that open serial, its current open parent
(station / B9 / none), and join count → the create-vs-move-vs-reopen bucket. This is the
dup-guard every `add-*`/reconstruction must gate on. Read-only, no paths.
Source: `scratchpad/serial_exists.py`.

### `tos audit rinex-timeline <station> [--field firmware|receiver|antenna] [--archive-root PATH]`
The empirical-truth extractor. Reads RINEX header lines (`REC # / TYPE / VERS`,
`ANT # / TYPE`, `ANTENNA: DELTA H/E/N`) across the cold archive, day-narrows each
transition, and returns the segment timeline. Parallel header reads (NFS-latency-bound).
**Archive root from config** (see below), `--archive-root` override only.
Source: `scratchpad/fw_miner.py`. Consolidate the archive-walk with
`verify-from-rinex` (which already reads the archive) so both share one config-driven
reader instead of two hardcoded ones.

### `tos audit firmware-chain <station> [--emit-triage PATH] [--probe-value]`
Mines `rinex-timeline --field firmware`, **normalizes** (`_normalize_firmware_version`
collapses `NP x/SP y`→`x`, `5.50`→`5.5.0`) + merges equal consecutive periods, then
**tiers** (clean strictly-increasing → full chain; NetRS `1.x` headers unreconstructable
→ current-period-only via the probe value; non-monotonic → flag for manual verify).
Emits a `delete-attribute-value` + `add-attribute-period`-per-period triage.
Source: `scratchpad/fw_triage_gen.py`.

### `cfg onboard-station <station> [--emit-script]`
Orchestrator that, for a station missing devices, runs: `find --serial` (bucket) →
`rinex-timeline` (install date + antenna height + model) → `add-receiver`/`-antenna`/
`-monument` with `--commit`. Replaces the per-batch generator.
Source: `scratchpad/groupb_prep.py`.

### `tos audit duplicate-serials --arbitrate`
When a serial appears at ≥2 stations, compare each station's `rinex-timeline` to classify:
**rove** (one unit, sequential, non-overlapping — close old leg + create-join new, cf.
BAUG/SNAE), **collision** (overlapping → config/typo error), or **junk** (placeholder/
all-1000-01-01). Turns the manual BAUG/SNAE forensics into a verb.

## Config — no hardcoded paths

| Resource | Config source | Notes |
|---|---|---|
| Cold RINEX archive root | **`sync.yaml` archive-tier target `source_root`** | `/mnt_data/rawgpsdata`; per-host (read-only mount). **Not** `[archive_paths] data_prepath` — that is the *live-download* dir, a different tree. |
| stations.cfg | `GPS_CONFIG_PATH` → `~/.config/gpsconfig` | |
| TOS corrections repo | `receivers.cfg [paths] tos_corrections_repo` | added 2026-06; → `~/git/gps-tos-corrections` |
| sitelogs repo | `receivers.cfg [paths] sitelogs_repo` | added 2026-06; → `~/git/gps-sitelogs` |
| gps-config-data repo | `receivers.cfg [paths] gps_config_data_repo` | |
| TOS API host | `tostools` default `vi-api.vedur.is` / `--server` | |

**Add if needed:** the archive-reading verbs should resolve the cold-archive root via a
single helper, e.g. `archive.cold_archive_root()` that reads `load_sync_config()`'s
archive-tier `source_root` (with `$GPS_COLD_ARCHIVE_ROOT` env + `--archive-root` overrides).
If a non-sync consumer needs it, promote it to an explicit `receivers.cfg [paths]
cold_archive_root` key rather than re-deriving from sync.yaml. Every archive verb takes
`--archive-root` as the single escape hatch; no module-level `/mnt_data/...` constant.

## The deterministic reconstruction playbook

1. **Sweep** — `tos audit fleet-sweep` (+ `cfg reconcile --all --source both`) → flagged stations.
2. **Locate** — `tos device find --serial` per flagged serial → create / move / reopen / arbitrate bucket. **(dup-guard — never skip)**
3. **Empirically verify** — `tos audit rinex-timeline` / `verify-from-rinex` → the archive's segment truth (install dates, swaps, roves).
4. **Probe** when the archive is ambiguous/overlapping — the live receiver's self-reported serial is the tie-breaker (SNAE).
5. **Generate** the triage — `firmware-chain --emit-triage`, `onboard-station --emit-script`, or a hand `create-join`/`move`.
6. **Apply** — `tos audit apply … --apply --commit` (audit trail in `gps-tos-corrections/`; creations also in `additions/device_additions.jsonl`).
7. **Verify** — re-read TOS: exactly one open device per subtype (singular invariant), coherent join history, `fleet-sweep` clean.

## Retire the scratch scripts

`fw_miner.py`, `serial_exists.py`, `fw_triage_gen.py`, `groupb_prep.py` (in the session
scratchpad) are the prototypes for the verbs above. They are **not** to be re-run as
scripts — promote them, with the config-driven archive root and tests, then delete.
