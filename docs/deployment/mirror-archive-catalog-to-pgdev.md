# Runbook — mirror `archive_catalog` rek-d01 → pgdev (Phase 2)

**Status:** DRAFT for review + IT/DBA coordination. Do **not** execute against
pgdev without IT awareness and an agreed window. Read the whole doc first.

## Why

`archive_catalog` (in the `gps_health` DB) is meant to be identical across the
`catalog_hosts` set — **rek-d01** (operational primary) and **pgdev** (the
mirror Grafana/consumers read). It diverged badly because, until 2026-07-24,
**routine** catalog writes only hit the local default connection (rek-d01);
only explicit `--catalog-prod` reindex runs fanned out. That gap is now closed
(the sync engine fans every write out to both hosts — see
`archive.open_catalog_conns`), and rek-d01's historical rows have been date-
corrected (Phase 1). **Phase 2 back-fills pgdev so it becomes a true full
mirror of the corrected rek-d01 catalog.**

## Current state (2026-07-27)

| | rek-d01 (primary) | pgdev (mirror) |
|---|---|---|
| `archive_catalog` rows (total) | ~8.56 M | ~0.81 M |
| `imo_archive` rows | ~8.44 M | ~0.73 M |
| Short-name RINEX dates | corrected (Phase 1) | ~32 k still mis-dated + ~7.7 M missing |
| Fan-out writing it now? | yes | **yes** (live, since Phase 0) |

Both hosts now receive every *new* row (verified: identical push counts). The
job is the **historical gap** (~7.7 M rek-d01-only rows) plus **correcting the
~32 k stale rows** pgdev already has.

## Table facts that shape the approach

- **Self-contained:** `archive_catalog` has **no outgoing FKs**, and
  `file_tracking_id` is 100 % NULL — so we do **not** need to copy
  `file_tracking` / `file_locations` for integrity. One table.
- **Merge key:** unique `archive_catalog_logical_key
  (storage_location, session_type, file_category, canonical_key)` — the *same*
  key the fan-out upsert uses. `id` (PK) is host-local and must be excluded.
- **Size:** ~7.2 GB total (3.1 GB heap + ~4 GB indexes).
- **No `postgres_fdw` / `dblink`** on pgdev → the copy must **stream from
  rek-d01 into pgdev** (a piped `COPY`), not a cross-host SQL join.

## Chosen mechanism — online staging + upsert-merge (no downtime)

Because the fan-out writes pgdev **live**, a `TRUNCATE`+reload is rejected (it
takes `ACCESS EXCLUSIVE`, blocking pgdev's catalog for the whole load and
racing the fan-out). Instead:

1. **Snapshot for rollback** — `pg_dump` pgdev's current `archive_catalog` to a
   file (safety net; the merge itself is non-destructive but this is cheap
   insurance).
2. **Staging table on pgdev** — `CREATE UNLOGGED TABLE stg_archive_catalog
   (LIKE archive_catalog INCLUDING DEFAULTS)`; drop its `id` default is fine —
   we never read `id` from it. `UNLOGGED` = far less WAL for the bulk load.
3. **Stream rek-d01 → pgdev staging**, batched by `storage_location` (and, for
   `imo_archive`, by year) to bound each transaction:
   ```bash
   # one batch; repeat per (storage_location[, year])
   psql "$REK"   -c "\copy (SELECT storage_location,station,file_date,session_type,
        file_category,canonical_key,file_path,compression,file_size,content_sha256,
        is_rinexed,rinex_is_original,raw_available,file_hour,indexed_at,
        last_verified_at,compressed_sha256,md5checksum,md5uncompressed
        FROM archive_catalog WHERE storage_location='imo_archive'
          AND file_date >= '2021-01-01' AND file_date < '2022-01-01') TO STDOUT" \
   | psql "$PGDEV" -c "\copy stg_archive_catalog (storage_location,station,file_date,
        session_type,file_category,canonical_key,file_path,compression,file_size,
        content_sha256,is_rinexed,rinex_is_original,raw_available,file_hour,
        indexed_at,last_verified_at,compressed_sha256,md5checksum,md5uncompressed)
        FROM STDIN"
   ```
   (Column list is explicit and excludes `id` so it survives a schema drift.)
4. **Merge staging → live** on pgdev, in batches, by the logical key:
   ```sql
   INSERT INTO archive_catalog AS a (storage_location,station,file_date,session_type,
     file_category,canonical_key,file_path,compression,file_size,content_sha256,
     is_rinexed,rinex_is_original,raw_available,file_hour,indexed_at,last_verified_at,
     compressed_sha256,md5checksum,md5uncompressed)
   SELECT storage_location,station,file_date,session_type,file_category,canonical_key,
     file_path,compression,file_size,content_sha256,is_rinexed,rinex_is_original,
     raw_available,file_hour,indexed_at,last_verified_at,compressed_sha256,
     md5checksum,md5uncompressed
   FROM stg_archive_catalog
   ON CONFLICT ON CONSTRAINT archive_catalog_logical_key DO UPDATE SET
     station=EXCLUDED.station, file_date=EXCLUDED.file_date, file_path=EXCLUDED.file_path,
     compression=EXCLUDED.compression, file_size=EXCLUDED.file_size,
     content_sha256=EXCLUDED.content_sha256, is_rinexed=EXCLUDED.is_rinexed,
     rinex_is_original=EXCLUDED.rinex_is_original, raw_available=EXCLUDED.raw_available,
     file_hour=EXCLUDED.file_hour, compressed_sha256=EXCLUDED.compressed_sha256,
     md5checksum=EXCLUDED.md5checksum, md5uncompressed=EXCLUDED.md5uncompressed;
   -- NOTE: deliberately do NOT overwrite indexed_at/last_verified_at on UPDATE
   -- (keep pgdev's own provenance); include them only on INSERT (they come from
   -- the SELECT list above, used only when the row is new).
   ```
   This **inserts** the ~7.7 M missing rows and **corrects** the ~32 k stale
   ones, matched on the logical key — the identical key the live fan-out uses,
   so a concurrent write on the same row simply wins/loses cleanly and both
   sides now carry correct data.
5. **Drop the staging table.**

### Why upsert, not `INSERT ... WHERE NOT EXISTS`
The plain "insert missing" leaves pgdev's ~32 k already-mis-dated rows wrong.
The upsert corrects them in the same pass.

## Sequencing & load

- Run **off-peak**, IT-aware (7.7 M upserts → meaningful WAL + I/O on pgdev).
- Batch the copy+merge by `storage_location`, then for `imo_archive` by
  calendar year (≈ a dozen batches of a few-hundred-k rows). Bounded txns,
  restartable, and each batch is independently idempotent.
- Between batches, `ANALYZE archive_catalog` once at the end (not per batch).

## Verification (must pass before declaring parity)

```sql
-- 1. total + per-dimension counts match rek-d01
SELECT storage_location, file_category, count(*) FROM archive_catalog GROUP BY 1,2 ORDER BY 1,2;
-- 2. zero mis-dated short-name rinex on pgdev (same detector as rek-d01)
SELECT count(*) FROM archive_catalog WHERE file_category='rinex'
  AND canonical_key ~ '\.[0-9]{2}d$' AND file_date IS NOT NULL
  AND (2000 + CAST(substring(canonical_key from '\.([0-9]{2})d$') AS int)) <> EXTRACT(year FROM file_date)::int;  -- expect 0
-- 3. NULL-date residual matches rek-d01's (the 258 misfiled strays)
SELECT count(*) FROM archive_catalog WHERE file_category='rinex'
  AND canonical_key ~ '\.[0-9]{2}d$' AND file_date IS NULL;
```
Compare each against rek-d01. **pgdev-only rows:** rek-d01 is the superset
primary, so pgdev should hold no logical key rek-d01 lacks; if the post-merge
total exceeds rek-d01's, anti-join staging↔live to find and investigate the
excess before any delete (never blind-delete on a live mirror).

## Rollback

The merge is non-destructive (insert/upsert-to-correct only) — there is no
delete. If something looks wrong, stop; the pre-merge `pg_dump` from step 1 is
the restore point for pgdev's `archive_catalog`.

## After parity — Phase 3 (separate)

Add a periodic rek-d01 ↔ pgdev parity check (count-by-dimension + the mis-dated
detector) so any future drift is caught early — the fan-out should keep them
in lockstep, but a guard makes the "identical set" invariant observable.

---

*Owner: bgo. Prereqs: IT/DBA awareness of a ~7.7 M-row off-peak load on pgdev;
`psql` access to both hosts with the `gps_health` catalog credentials
(database.cfg mirror creds for pgdev). See also `archive.open_catalog_conns`
(Phase 0 fan-out) and the incident memory
`archive-catalog-misdating-and-pgdev-divergence`.*
