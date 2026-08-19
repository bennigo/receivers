# Operational status model — stations.cfg ↔ TOS consolidation

Status: **proposed → attributes created 2026-08-19** (cfg + receivers wiring pending)

## Problem

`stations.cfg` carries seven operational-ish per-station fields that overlap
and are inconsistently consumed. Audited 2026-08-19:

| Field | Values | Code semantics | Consumed? |
|---|---|---|---|
| `station_role` | active/passive | `passive` = data-source-only (GLOBK reference ties), not operated | ✅ skip in bootstrap/seeder/reconcile/scheduler |
| `health_check` | NULL/active/passive | `passive` = operated but don't poll health | ✅ skip in bootstrap/gap/backfill/horizon/icinga |
| `station_status` | NULL/active/inactive/discontinued/suppressed | lifecycle; auto-`inactive` when `receiver_type` empty; `discontinued` = has end date | ✅ skip + DB sync |
| `acquisition_mode` | download/stream | transport (direct pull vs RTCM stream→RINEX) | ✅ stream scheduler |
| `download_method` | external/rinex_external | third-party pull | ❌ **dead — zero consumers** |
| `is_reference_site` | true/false | reference flag | ❌ parsed, never read |
| `is_in_iceland` | true/false | in-Iceland flag | ❌ parsed, never read |

Two words are overloaded (`passive` means different things in `station_role`
vs `health_check`), and three fields are dead/parsed-but-unused. The goal is a
single canonical source (TOS) from which `stations.cfg` can eventually be
*generated*.

## The model — two orthogonal axes

| Axis | Question | Values | Home |
|---|---|---|---|
| **operational_status** | who runs it / how data reaches us | `operated` · `external` · `reference` | TOS (new, id 189) |
| **lifecycle** | is it running | active · inactive · discontinued | TOS (existing: `date_end` + open receiver join) |

Lifecycle is deliberately **not** folded into `operational_status`:
`discontinued` ≡ `date_end` set, and `inactive` ≡ no open receiver join (or
`continuity = campaign`). Both are already derivable from TOS.

## New TOS attributes

### `operational_status` (station, text enum — id 189)

| Value | Icelandic | Meaning |
|---|---|---|
| `operated` | Innri stöð | IMO operates it: direct download + health monitoring (default) |
| `external` | Ytri stöð | third party operates it; we pull files from their server, no health polling |
| `reference` | Viðmiðunarstöð | data-source-only reference station (GLOBK ties); not operated, no monitoring |

`python_constraint = ^(operated|external|reference)$`

### `external_data_type` (station, text enum — id 190)

| Value | Meaning |
|---|---|
| `rinex` | we pull RINEX from the external source (common) |
| `raw` | we pull raw/SBF from the external source (rare) |

Only meaningful when `operational_status = external`.
`python_constraint = ^(rinex|raw)$`

`stream` is intentionally **not** a value — a stream station (e.g. SEY9) is
still `operated`; transport is `acquisition_mode`.

## Mapping (old cfg fields → new model)

| stations.cfg today | After |
|---|---|
| `health_check = passive` | → `operational_status = external` (or `reference`); "operated ⇒ monitor" becomes the rule |
| `download_method = external` / `rinex_external` | → `operational_status = external` + `external_data_type = raw` / `rinex` |
| `station_role = passive` | → `operational_status = reference` |
| `is_reference_site = true` | → `operational_status = reference` |
| `is_in_iceland` | keep (geographic, orthogonal) |
| `acquisition_mode = stream` | keep (transport, orthogonal to ownership) |
| `station_status = inactive` | derived (no open receiver join / campaign) |
| `station_status = discontinued` | derived (`date_end` set) |

Net: four scattered fields collapse to **one TOS attribute**, the two genuinely
orthogonal fields stay, and lifecycle becomes derived rather than hand-kept.

## Backfill mapping (per-station, from today's stations.cfg)

| old `download_method` | example | new |
|---|---|---|
| `rinex_external` | MYVA (NATT) | `operational_status=external`, `external_data_type=rinex` |
| `external` | KRAC, THRC, TORK (UI) | `operational_status=external`, `external_data_type=raw` — **confirm per station** (raw vs rinex is unverified; the dead field never distinguished it reliably) |

## Rollout

1. ✅ Create `operational_status` (id 189) + `external_data_type` (id 190) in TOS.
2. Add `operational_status` to `stations.cfg`; keep old fields as read-only
   aliases for one transition window, then drop them.
3. One-time backfill: set `operational_status` (+ `external_data_type`) on each
   station from today's cfg (confirm the `external`→raw mapping first).
4. Wire receivers to read `operational_status` (bootstrap/scheduler/gap/
   backfill/horizon-probe) and remove the `station_role`/`health_check` skip
   logic, replacing it with a single "is this station operated?" check.
5. Eventually generate `stations.cfg` from TOS (operational_status +
   acquisition_mode + device metadata), retiring the hand-maintained fields.

---

*Created 2026-08-19 (bgo, with pi). Attributes live in TOS; cfg + receivers
wiring are follow-ups.*
