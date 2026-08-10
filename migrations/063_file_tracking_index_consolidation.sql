-- Migration 063: consolidate the file_tracking (sid, session_type, …) indexes
--
-- WHY (measured on pgdev, 2026-08-10 — the DB kept falling over):
--   file_tracking held SIX indexes on the same leading columns, every one of
--   them PARTIAL. A partial index is only usable when the planner can PROVE
--   its predicate, so a query that does not mention file_hour / status could
--   use NONE of them:
--
--     idx_file_tracking_daily            (sid,session_type,file_date)        WHERE file_hour IS NULL
--     idx_file_tracking_hourly           (sid,session_type,file_date,file_hour) WHERE file_hour IS NOT NULL
--     idx_file_tracking_missing          (sid,session_type,file_date)        WHERE status = 'missing'
--     idx_file_tracking_needs_integrity  (sid,session_type,file_date)        WHERE status IN (…) AND …
--     idx_file_tracking_not_imported     (sid,session_type,file_date)        WHERE NOT imported_to_db
--     idx_file_tracking_suspect          (sid,session_type)                  WHERE status = 'suspect'
--
--   Result on a 934k-row / 209 MB table: 46.2M sequential scans and 6.18e12
--   tuples read. pg_stat_statements showed three seq-scanning shapes that this
--   migration alone fixes (no code change needed):
--     * absence_served_gate's EXISTS  (sid, session_type, status, last_checked)
--         318,968 calls x 163 ms  = 14.5 h  — no file_date predicate at all
--     * (sid, session_type, file_date, status IN (…))  107 ms and 98 ms/call
--   A fourth shape — 22.6M calls x 24 ms = 151 h, filtering on the unindexed
--   `filename` — is fixed in code (archive_reconciler._ensure_rinex_tracked).
--
-- WHAT:
--   One plain composite index replaces all six. Uniqueness is kept as a real
--   CONSTRAINT rather than two partial indexes: PG15+ UNIQUE NULLS NOT
--   DISTINCT treats the NULL file_hour of a daily row as a value, so a single
--   4-column constraint enforces exactly what the daily+hourly pair enforced,
--   AND serves (sid), (sid,session_type) and (sid,session_type,file_date)
--   prefix scans. Verified free of conflicts on pgdev before writing this.
--
-- NOTE — CREATE INDEX CONCURRENTLY is deliberately NOT used: the migrator runs
--   each file inside a transaction, where CONCURRENTLY is illegal. The table is
--   209 MB, so the plain build blocks writes for a few seconds. To avoid even
--   that, run the CREATE UNIQUE INDEX below by hand with CONCURRENTLY (outside
--   any transaction) first — the IF NOT EXISTS makes this file a no-op then.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/063_file_tracking_index_consolidation.sql

BEGIN;

DO $$
BEGIN
    IF current_setting('server_version_num')::int < 150000 THEN
        RAISE EXCEPTION
            'Migration 063 needs PostgreSQL 15+ (UNIQUE NULLS NOT DISTINCT); this server is %',
            current_setting('server_version');
    END IF;

    -- Idempotency guard. ADD CONSTRAINT … USING INDEX below RENAMES the index
    -- to the constraint name, so a second hand-run's `CREATE UNIQUE INDEX IF
    -- NOT EXISTS idx_file_tracking_slot` would not match the surviving index
    -- and would rebuild all 78 MB for nothing. This file's header invites a
    -- manual `psql -f`, so make the re-run a no-op.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'file_tracking_slot_uniq'
          AND conrelid = 'file_tracking'::regclass
    ) THEN
        RAISE NOTICE 'Migration 063 already applied — nothing to do';
        RETURN;
    END IF;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_file_tracking_slot
        ON file_tracking (sid, session_type, file_date, file_hour)
        NULLS NOT DISTINCT;

    ALTER TABLE file_tracking
        ADD CONSTRAINT file_tracking_slot_uniq
        UNIQUE USING INDEX idx_file_tracking_slot;

    DROP INDEX IF EXISTS idx_file_tracking_daily;
    DROP INDEX IF EXISTS idx_file_tracking_hourly;
    DROP INDEX IF EXISTS idx_file_tracking_missing;
    DROP INDEX IF EXISTS idx_file_tracking_needs_integrity;
    DROP INDEX IF EXISTS idx_file_tracking_not_imported;
    DROP INDEX IF EXISTS idx_file_tracking_suspect;
END $$;

-- The DO block above does the work in order: create the replacement FIRST
-- (so the uniqueness guard is never absent), promote it to a named constraint
-- so the invariant is declared rather than implied by an index someone may
-- later "clean up" (this migration is that cleanup), THEN drop the six
-- superseded partial indexes. ADD CONSTRAINT … USING INDEX renames the index
-- to the constraint name, so the surviving index is `file_tracking_slot_uniq`.

COMMENT ON CONSTRAINT file_tracking_slot_uniq ON file_tracking IS
    'One row per (station, session, date, hour); NULL hour = daily file. '
    'Also the general lookup index for the sid/session_type/file_date prefix — '
    'replaced six partial indexes in migration 063.';

COMMIT;

-- Post-migration: refresh planner stats so the new index is costed correctly
-- on the next query rather than after the next autoanalyze.
ANALYZE file_tracking;
