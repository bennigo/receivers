# Applying migrations 063 / 064 to a shared server

**Do not run `receivers db migrate --host pgdev.vedur.is` for these two.**
Use the hand sequence below on pgdev. On rek-d01's own database the normal
migrator path is fine (dedicated host, scheduler stopped during install.sh).

## Why the migrator path is wrong for pgdev

`pgdev.vedur.is` is shared — other teams' databases live on it, and it was
taken down once already (2026-05-27, cartesian join, killed by hand by IT).

Both migrations do their work inside one transaction, which means:

| Statement | Lock on the table | Held until |
|---|---|---|
| `CREATE UNIQUE INDEX` (plain) | SHARE — blocks writes | COMMIT |
| `ALTER TABLE … ADD CONSTRAINT … USING INDEX` | **ACCESS EXCLUSIVE — blocks everything, readers included** | COMMIT |
| `DROP INDEX` ×N | ACCESS EXCLUSIVE (already held) | COMMIT |

The index build itself is fast (~2–5 s for file_tracking's 935k rows / 209 MB
with `maintenance_work_mem` = 2 GB). **The build duration is not the hazard.**
The hazard is acquiring the ACCESS EXCLUSIVE lock: the request parks at the
head of the lock queue, and from that moment every later query on the table
queues behind it. One idle-in-transaction session upstream and the table is
frozen for everybody, indefinitely.

`Migrator._get_conn()` used to make this unbounded by running
`SET lock_timeout = 0`, overriding the role guardrail
(`ALTER ROLE bgo IN DATABASE gps_health SET lock_timeout = 5s`) that exists
because of that 2026 incident. That is fixed — the migrator now sets a bounded
5 s lock timeout (`MIGRATION_LOCK_TIMEOUT` overrides) — but on a shared server
the `CONCURRENTLY` sequence below is still the right tool, because it avoids
the write-blocking SHARE lock during the build entirely.

A lock timeout is a *clean* failure: the file's transaction rolls back whole,
nothing is half-applied, and you retry in a quiet moment. Retry later beats
wedging the shared box.

## Apply sequence (pgdev), migration 063 — file_tracking

Run as `bgo` in `psql`, leaving the role timeouts in place.

```sql
-- Step 1 — outside any transaction (CONCURRENTLY is illegal inside one).
SET statement_timeout = 0;        -- a concurrent build waits on snapshots; do not cap it
SET lock_timeout = '5s';
CREATE UNIQUE INDEX CONCURRENTLY idx_file_tracking_slot
    ON file_tracking (sid, session_type, file_date, file_hour) NULLS NOT DISTINCT;
```

**Verify before continuing** — a failed/cancelled `CONCURRENTLY` build leaves
an INVALID index behind, and the migration would then skip creation
(`IF NOT EXISTS`) and fail on the ALTER:

```sql
SELECT indisvalid FROM pg_index WHERE indexrelid = 'idx_file_tracking_slot'::regclass;
-- false  ->  DROP INDEX CONCURRENTLY idx_file_tracking_slot;  and redo step 1.
```

```sql
-- Step 2 — short transaction, bounded AEL wait. On 55P03 (lock_not_available),
-- just retry; nothing was applied.
BEGIN;
SET LOCAL lock_timeout = '2s';
ALTER TABLE file_tracking
    ADD CONSTRAINT file_tracking_slot_uniq UNIQUE USING INDEX idx_file_tracking_slot;
COMMIT;   -- AEL held for the catalog swap only, ~milliseconds

-- Step 3 — each outside any transaction; per-drop AEL is momentary and bounded.
DROP INDEX CONCURRENTLY idx_file_tracking_daily;
DROP INDEX CONCURRENTLY idx_file_tracking_hourly;
DROP INDEX CONCURRENTLY idx_file_tracking_missing;
DROP INDEX CONCURRENTLY idx_file_tracking_needs_integrity;
DROP INDEX CONCURRENTLY idx_file_tracking_not_imported;
DROP INDEX CONCURRENTLY idx_file_tracking_suspect;

ANALYZE file_tracking;
```

## Apply sequence (pgdev), migration 064 — file_absence

Same shape, smaller table (75k rows / 17 MB):

```sql
SET statement_timeout = 0;
SET lock_timeout = '5s';
CREATE UNIQUE INDEX CONCURRENTLY idx_file_absence_slot
    ON file_absence (source_location, sid, session_type, file_date, file_hour)
    NULLS NOT DISTINCT;

SELECT indisvalid FROM pg_index WHERE indexrelid = 'idx_file_absence_slot'::regclass;

BEGIN;
SET LOCAL lock_timeout = '2s';
ALTER TABLE file_absence
    ADD CONSTRAINT file_absence_slot_uniq UNIQUE USING INDEX idx_file_absence_slot;
COMMIT;

DROP INDEX CONCURRENTLY uq_file_absence_daily;
DROP INDEX CONCURRENTLY uq_file_absence_hourly;

ANALYZE file_absence;
```

`idx_file_absence_terminal` is deliberately kept.

## Afterwards — record it

Once the *constraint* exists, both migration files are recorded no-ops (their
`DO` blocks check `pg_constraint` and return). Run the migrator normally so
`schema_migrations` gets the row:

```bash
receivers db migrate --host pgdev.vedur.is
```

**Hand-applying only step 1 is not enough.** `CREATE UNIQUE INDEX IF NOT
EXISTS` is the only statement the pre-created index neutralizes; the migrator
would still take ACCESS EXCLUSIVE for the ALTER and the DROPs. Go all the way
through step 3 before letting the migrator near the table.

## Verify the result

```sql
-- One index where six were; constraint present and valid.
SELECT indexname FROM pg_indexes WHERE tablename = 'file_tracking' ORDER BY 1;

-- The shapes that were seq-scanning must now index-scan.
EXPLAIN SELECT 1 FROM file_tracking ft
 WHERE ft.sid = 'SEY2' AND ft.session_type = '1Hz_1hr_rinex'
   AND ft.status IN ('downloaded','archived')
   AND ft.last_checked > now() - interval '6 hours';

-- Counters should stop climbing (compare two samples a few minutes apart).
SELECT relname, seq_scan, seq_tup_read, idx_scan
  FROM pg_stat_user_tables WHERE relname IN ('file_tracking','file_absence');
```

## Order relative to the code deploy

Apply the migrations **first**, on **both** hosts (rek-d01's local database is
the primary; pgdev receives every write through the dual-write mirror). The
reconciler's new prefetch query has no `file_hour`/`status` predicate, so under
the old partial-only indexes it is itself unindexable — deploying the code
first would trade 22.6M point queries for ~1,000 full scans per hour. Still an
improvement, but there is no reason to take it.

## Rollback

`migrations/06{3,4}_*_rollback.sql` recreate the partial indexes and drop the
constraint. If run against pgdev, apply the same CONCURRENTLY treatment.
