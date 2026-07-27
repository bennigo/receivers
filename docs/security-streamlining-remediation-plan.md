# Security & Streamlining Remediation Plan (Fable review, 2026-07-27)

Execution plan for the S (security) + T (streamlining) findings from the Fable 5
full-codebase review. **Goal: execute the fixes.** Paths relative to
`src/receivers/`. This doc is the durable hand-off across a `/clear`.

## Root-cause insight (drives the ordering)

The three worst security findings (**S1 critical, S2/S3 high**) all stem from
**one cause**: `DatabaseConnectionFactory` `get_connection()`/`connection()`
returns a `_DualConnection` (writes fan to primary **and** the pgdev mirror,
best-effort) whenever `mirror_host` is set — and it is handed to code paths that
assume a **single, id-stable, read-only, or transactional** connection. The
Phase-0/1/2 catalog work already confirmed this live. Fixing the connection
layer (an explicit single-host mode + dual-write safety) resolves S1+S2+S3+T4
and the Phase-0 fan-out redundancy at once. **Do this first, coordinated.**

---

## GROUP 1 — Connection safety (CRITICAL · FIRST · coordinated, NOT parallel)

One focused change; touches shared connection machinery so must not run parallel
to anything. Model: **opus/sonnet** (subtle, security-critical). Own it on the
main thread or a single careful agent + review + tests + deploy.

Items & files:
- **S1 (CRITICAL)** `receivers db setup`/`restore` fan `DROP SCHEMA public CASCADE`
  to pgdev with no confirm. `cli/db.py:34-56`, `db/migrator.py:35-42`,
  `db/seeder.py:106-110`. → all `db` verbs use a **single-host** connection.
- **S2 (HIGH)** id-keyed writes fanned to mirror hit wrong rows.
  `cfg/discrepancy_log.py:183-215`, `health/file_tracker.py:2160-2185` & `2474-2494`,
  `archive/verify.py:249`. → forbid id-keyed writes on dual conns; use natural
  keys (pattern: `discrepancy_log.record_resolution:271-285`). Add mirror-failure
  **counter/metric**, not just `logger.warning` (`database_factory.py:183,194,274`).
- **S3 (HIGH)** `health-query` gate bypass + not read-only. `cli/health_query.py:134-143,216-230`.
  → reject multi-statement (`;`), `SET default_transaction_read_only=on` (opt-out
  `--write`), delegate `autocommit` through `_DualConnection` or force single-host.
- **S7 (MED)** no `statement_timeout`/`lock_timeout` on app conns.
  `database_factory.py:327-360`. → add `options: -c statement_timeout=… -c lock_timeout=…`
  (config-overridable) to `get_connection_params()`.
- **T4** two connection systems + ~13 inline imports → standardize on
  `db.connection.get_connection` with an explicit `single_host=`/`dual=` flag; this
  is where the S1/S2 guards live. `cli/archive_sync.py:22`, `cli/missing.py:51`
  re-implement `_get_conn` — unify.
- **Phase-0 cleanup** `archive/reindex.open_catalog_conns` element-0 currently
  resolves to the dual conn → redundant double pgdev write. Make element-0 a true
  single-host primary connection.

Verify: `receivers db setup` on a laptop must NOT touch pgdev; `health-query
"SELECT 1; DELETE …"` must be refused; existing tests green + new tests for each guard.

---

## GROUP 2 — Supply-chain / device security (PARALLEL · disjoint files)

Independent files, safe to run concurrently with Groups 3-4. Model: **sonnet**
(S4 needs care re TLS), **fable/haiku** ok for S5/S6/S8 (mechanical).

- **S4 (MED)** receiver TLS is `CERT_NONE`/`check_hostname=False`, creds in-band.
  `septentrio/tcp_client.py:104-105,139-141`, `septentrio/firmware_upgrade.py:129-167`,
  `health/polarx5_tcp_extractor.py:163-179,1249-1251`, `cli/main.py:3467-3468`.
  → pin device cert (store fingerprint on provisioning, verify after; alert on change).
  ⚠️ touches `cli/main.py` — coordinate with Group 5 (which splits main.py) or do S4 first.
- **S5 (MED)** paramiko `AutoAddPolicy` + password to routers.
  `cfg/conntrack_helper.py:114`. → per-fleet known_hosts, `RejectPolicy` after first-seen.
- **S6 (MED)** tool installer: no checksum + `extractall` (zip/tar-slip).
  `tools/tool_manager.py:400-404,461-464,502-508`. → pin SHA-256 in `TOOLS`;
  `tf.extractall(..., filter="data")`; validate zip member names.
- **S8 (LOW)** dynamic SQL identifiers (internal). `dissemination/epos_db.py:99,123,148,176-213`,
  `cli/db.py:363-370`. → `psycopg2.sql.Identifier`; quote-escape schema.
- **S9 (LOW)** deprecate plaintext `--password` CLI flags → prompt-only.
  `cli/cfg.py:4997,5127,5837,6016` (⚠️ overlaps Group 5's cfg.py split — sequence).

---

## GROUP 3 — Receiver-class dedup + dead code (ONE stream · heavy file overlap)

`trimble/*`, `septentrio/*`, `*_extractor.py`, `*_client.py`, receiver classes are
touched by S4/T1/T2/T9/T11 — **must be one sequential stream** (not internally
parallel). Big win, ~2000+ lines removed. Model: **sonnet**. Order within:

1. **T11** dead code (verified zero callers): `scheduling/backfill.py:316`,
   `cli/main.py:5042`, `LeicaG10._process_zip_files` (g10.py:874), `_archive_files`/
   `_validate_archived_file` in netr9/netrs/g10, `PolaRX5.make_file_name`/
   `_get_remote_file_path`/`_cleanup_empty_tmp_directories`; no-op stmts polarx5.py:447,
   netrs.py:433, g10.py:309; unused `ffrequency` polarx5.py:957.
2. **T2** delete dead download architecture (~1100+ lines, `BaseDownloadManager`
   never instantiated — verify no imports first): `base/download_manager.py`,
   `septentrio|trimble|leica/download_manager.py`, associated dead client paths
   (`trimble/http_download_client.py:668-675` is broken code).
3. **T9** shared receiver helpers: `ProgressBar` (3×: `http_download_client.py:42`,
   `netrs_http_download_client.py:22`, `leica_ftp_download_client.py:22`) → utils;
   retry/timeout loop (≥5×) → helper; `mark_downloaded`/`mark_missing` block (4×,
   only regex differs) → BaseReceiver; `_voltage/_temperature/_satellite/_overall_status`
   dup (`trimble_http_extractor.py:1524-1601`, `g10_http_extractor.py:739-774`,
   `base/receiver.py:340-363`) → base.
4. **T1** `NetRS` → subclass of `NetR9` (template: `trimble/netr5.py:21-129`);
   ~900 lines. Do LAST in this stream (after T11/T2/T9 settle the shared code).

---

## GROUP 4 — Archive/scheduler primitives + converters (PARALLEL-ish · own files)

Foundational helpers unblock later cleanups. Model: **sonnet**. Order:

1. **T7 (FIRST here)** centralize into `archive/path_parse.py`: session→day/hour +
   session-letter expansion (`archive_reconciler.py:254-259,423,491`,
   `integrity_checker.py:758-775`, `file_tracker.py:1990-1997,823`, `backfill.py:504`);
   DOY↔date (≥5×); date-from-filename (4×); raw-extension lists (use `rinex/raw_presence.py:35`).
2. **T8** converter base pull-ups into `RawToRinexConverter`: `_run_teqc` (3×),
   gfzrnx R2→R3 (2×), decompress-to-temp (3×), output discovery, temp cleanup; fix
   Leica bypassing base `_run_subprocess` (`leica_converter.py:283,415,484`).
3. **T10** move triplicated `_write_connectivity_status` (`cli/main.py:1088`,
   `bulk_scheduler.py:358`, `scheduling/tasks/status_task.py:233`) into
   `health/connectivity_writer.py`. ⚠️ touches main.py + bulk_scheduler — sequence
   vs Group 3/5.

---

## GROUP 5 — Big refactors (LAST · sequential · after helpers exist)

Highest risk, biggest files. Model: **sonnet**, careful review each.
- **T3** `bulk_scheduler.py` (3573): ~19 `_schedule_*` → declarative job registry +
  one `_register(spec)` (−600-800 lines).
- **T5** split `cli/cfg.py` (7645) → `cli/cfg_cmds/<verb>.py`; `create_cfg_parser`
  is ~2586 lines (`cfg.py:3852-6438`).
- **T6** split `cli/main.py` (6585) → `cli/cmds/{download,health,rec,rinex}.py`;
  and `health/file_tracker.py` (2572) → 5 modules (FileTracker/ArchiveFileChecker/
  FormatResolver/ProcessingStatusChecker/GapDetector); dedup 3× `_load_config`.
- **T12** shared `cli/_output.py` (dry-run, "would X/X" verbs) + `resolve_stations()`/
  `add_station_args()` (station-split dup in 6+ places).

---

## Execution mechanics

- **Order:** Group 1 → (Groups 2,3,4 in parallel via `isolation:"worktree"` agents,
  disjoint file sets) → Group 5 sequential.
- **Conflict guards:** `cli/main.py` (S4,T10,T11,T6), `cli/cfg.py` (S9,T5),
  `database_factory.py` (Group 1), `bulk_scheduler.py` (T3,T10) — never edit the same
  file in two concurrent agents. When overlap is unavoidable, sequence.
- Each stream = its own branch off `main`; after each: `ruff`+`black`+`mypy`+`pytest`
  (`.venv/bin/…`, explicit file args — shell mangles `$FILES`), then review + merge to
  main, then deploy the safety-relevant ones.
- **Deploy note:** rek-d01 venv is editable; `git pull` on main takes effect; restart
  scheduler as gpsops (`XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user restart
  gps-receivers-scheduler`; graceful stop ≤120s; reset-failed if start-limit tripped).
- **Full Fable findings** (S10 non-findings, exact scenarios) are in the session
  transcript; the actionable set is fully captured above.

## Status tracker
- [x] **Group 1 (S1,S2,S3,S7,T4,Phase0-cleanup)** — landed 2026-07-27, see below
- [ ] Group 2 (S4,S5,S6,S8,S9)
- [ ] Group 3 (T11,T2,T9,T1)
- [ ] Group 4 (T7,T8,T10)
- [ ] Group 5 (T3,T5,T6,T12)

### Group 1 — what landed (2026-07-27)

**Design decision that shaped everything: dual-write stays the DEFAULT.**
`mirror_host = pgdev.vedur.is` is live on rek-d01 (`gps-config-data/environments/
rek-d01.vedur.is.env:51`), so production health writes depend on the implicit
fan-out. Flipping the global default to single-host would have silently stopped
mirroring — a functional regression dressed as a security fix. Instead
`single_host=True` is an explicit opt-in at the dangerous call sites.

| Item | Change |
|------|--------|
| S1 | `single_host=` on `DatabaseConnectionFactory.get_connection()`/`connection()` + `db.connection.get_connection()`; every `db` verb, `Migrator`, `Seeder` use it. Confirmation now keys off the **resolved** host (`resolve_db_host`), not the CLI arg — the dangerous case was `--host` *omitted*, which skipped the prompt entirely. `drop-station` gained a confirm. |
| S2 | Natural-key rewrites: `discrepancy_log` (station_id+cfg_key+open), `file_tracker` ×2 (sid+session+date+hour), `verify.py` (the `archive_catalog_logical_key` UNIQUE — **not** `file_path`, which has no index). Plus a structural guard: `_DualCursor` refuses to fan out any `UPDATE/DELETE … WHERE id = %s` and counts it. |
| S3 | `health-query` is single-host, rejects multi-statement SQL (literal/comment aware), and sets `default_transaction_read_only` with a `--write` opt-out. |
| S7 | `statement_timeout` (600s) + `lock_timeout` (30s) via libpq `options` on every connection; config- and env-overridable, `0` disables. Also fixed: `connect_timeout` was read with `cfg.get()` but never loaded from database.cfg (missing from the key list) — the documented setting was inert. |
| T4 | `optional_connection()` in `db/connection.py` replaces the duplicated `_get_conn` in `cli/archive_sync.py` and `cli/missing.py`. |
| Phase-0 | `open_catalog_conns` element 0 is single-host **only when the resolved set has >1 host** — with a single-element set (laptop, no `catalog_hosts`) the implicit mirror is the only fan-out there is, so it is left alone. |

**Bug found while fixing S3, worth knowing:** `_DualConnection` defined `__getattr__`
but not `__setattr__`, so `conn.autocommit = True` landed in the wrapper's `__dict__`
and the real psycopg2 connections stayed transactional. Every session-level `SET`
health-query "applied" (`statement_timeout`, `lock_timeout`) sat in an uncommitted
implicit transaction — the whole timeout defense was a no-op whenever `mirror_host`
was set. Fixed by delegating attribute writes to both legs.

**Observability:** `MirrorMetrics` counters (connect/pool/checkout/cursor/execute/
executemany/setattr failures + `id_keyed_writes_not_mirrored`) replace bare
`logger.warning`s — the divergence went unnoticed for months precisely because
nothing counted.

**Verification:** `tests/test_connection_safety.py` (26 tests). Empirically, with
`mirror_host` pointed at an unroutable host, `receivers db status` took 10.7s on
`main` (it was reaching for the mirror) and 0.68s after (it isn't). `health-query`
refuses `"SELECT 1; DROP TABLE …"` and the server refuses `DELETE`/`CREATE` unless
`--write`. Pre-existing unrelated failure:
`test_archive_sync.py::TestEndToEndLocal::test_raw_immutable_rinex_updates`
(fails on `main` too). mypy output unchanged from baseline.

**Index-plan trap the natural-key rewrites walked into (fixed before merge).**
`IS NOT DISTINCT FROM` is not a btree-indexable operator, and the unique indexes
on the file_tracking grain are *partial* (`WHERE file_hour IS NULL` / `IS NOT NULL`).
Written the obvious way, the S2 rewrites would have demoted the key column from
Index Cond to Filter — trading a PK lookup for a near-full index scan, per row,
on 8.5M rows. Measured locally: cost 21.71 vs 8.44 on `archive_catalog`. Both
sites now pick `IS NULL` vs `= %s` in Python. `pg_indexes` confirms
`(sid, session_type, file_date, file_hour)` is unique, so the natural-key
UPDATEs remain single-row.

**Migrations are exempt from the new timeouts.** `Migrator._get_conn` issues
`SET statement_timeout = 0; SET lock_timeout = 0`. Deploy-time DDL on a live
8.5M-row table legitimately runs long and legitimately waits for ACCESS
EXCLUSIVE while the scheduler writes; aborting a migration mid-deploy is worse
than the slow query the ceiling bounds. The **seeder is not exempt** (short
row-level upserts) — if a `db seed` ever times out on lock contention, that is
the signal, not a bug to paper over.

**DEPLOYED to rek-d01 2026-07-27 10:26 UTC** (main `fb71719`, pushed to both remotes;
scheduler restarted as gpsops, `NRestarts=0`). Pre-deploy pipeline verification on the
laptop, all green:

| Pipeline stage | Result |
|---|---|
| Live receiver status/health (ELDC, THOB) | healthy, DB writes fine |
| Download → archive (THOB 15s_24hr, real FTP) | 4,885,140 B → `…/15s_24hr/raw/THOB202607260000a.sbf.gz` |
| `file_tracking` + `archive_catalog` writes | row written; catalog 10763 → 10764 (the fan-out path that changed) |
| RINEX conversion (sbf2rin + header corrections) | `THOB2070.26D.gz`, 1 converted / 0 failed |
| EPOS dissemination (`--dry-run`) | `pushed=2 cached=0 skipped=0 failed=0` |
| Dual-write path (mirror leg = 2nd real conn) | fan-out OK; autocommit reaches both legs; id-keyed UPDATE correctly hit primary only |

Post-deploy on rek-d01: `health-query` refuses multi-statement and refuses `CREATE TABLE`
(read-only); normal reads work; fleet health checks running; **mirroring intact** —
`block_receiver_status` newest ts identical on rek-d01 and pgdev (281 vs 280 rows in the
last 10 min = one in-flight write). No mirror failures, no id-keyed guard hits, no
DB-layer errors in the log; only ordinary unreachable-station HTTP timeouts.

**Still unexercised in production:** `db migrate` (the timeout-exemption path). The next
migration is the first real test — worth a human watching it.

**New operational knob:** `lock_timeout=30s` now applies to every app connection. Row-level
contention in the scheduler should stay far below that, but if a legitimate write ever
aborts on lock timeout, that is where to look (`POSTGRES_LOCK_TIMEOUT=0` disables).
