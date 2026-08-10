-- Migration 064: the file_absence twin of 063
--
-- Same defect, and unlike 063's table this one is 100% unindexed TODAY.
-- Measured on pgdev 2026-08-10:
--
--   file_absence: 75,564 rows / 17 MB
--     seq_scan      = 319,488
--     seq_tup_read  = 12,668,748,056     (12.7 BILLION)
--     idx_scan      = 0                  <-- every index, every time
--
--   pg_stat_user_indexes: file_absence_pkey 0, uq_file_absence_daily 0,
--   uq_file_absence_hourly 0, idx_file_absence_terminal 0. Not one scan.
--
-- WHY: the identity of a slot is NULL-safe — daily rows carry file_hour NULL —
-- so 056/059/061 spell every lookup `file_hour IS NOT DISTINCT FROM p_hour`
-- (056:96,108,137,149; 059:54,65; 061:75,86). That predicate is not btree-
-- indexable, and it also denies the planner the proof it needs to use EITHER
-- partial unique index (`WHERE file_hour IS NULL` / `IS NOT NULL`). With no
-- non-partial index on the leading columns, there is nothing left to use.
--
-- WHAT: one `UNIQUE NULLS NOT DISTINCT` constraint over the full slot. It
-- enforces exactly what the daily+hourly partial pair enforced (proof as in
-- 063: source_location/sid/session_type/file_date are NOT NULL, so the pair
-- partitions all rows and NULLS NOT DISTINCT rejoins them), and — the point —
-- it is NOT partial, so the four leading equality columns index-scan and only
-- `file_hour` is left as a filter over the handful of rows that survive.
-- Verified free of duplicate slots on pgdev before writing this.
--
-- `record_file_absence`'s `EXCEPTION WHEN unique_violation` handler keeps
-- working unchanged: a NULLS NOT DISTINCT constraint raises the same SQLSTATE.
--
-- NOT DONE HERE (follow-up): rewriting the functions to branch on
-- `p_hour IS NULL` / `= p_hour` would let file_hour join the index scan too.
-- The index alone takes these lookups from a 75k-row scan to a few rows, which
-- is the incident-grade part; the rewrite touches four function bodies across
-- three migrations and deserves its own change.
--
-- `idx_file_absence_terminal` is deliberately KEPT: 40 kB, and dropping it is
-- not needed for the seq-scan fix. Minimal blast radius.
--
-- Applying to a SHARED server: do NOT use `receivers db migrate` for pgdev.
-- Follow docs/deployment/apply-index-consolidation.md (CONCURRENTLY by hand,
-- bounded lock_timeout). This file is a recorded no-op afterwards.
--
-- Usage (dedicated host):
--   psql -h localhost -U bgo -d gps_health -f migrations/064_file_absence_index_consolidation.sql

BEGIN;

DO $$
BEGIN
    IF current_setting('server_version_num')::int < 150000 THEN
        RAISE EXCEPTION
            'Migration 064 needs PostgreSQL 15+ (UNIQUE NULLS NOT DISTINCT); this server is %',
            current_setting('server_version');
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'file_absence_slot_uniq'
          AND conrelid = 'file_absence'::regclass
    ) THEN
        RAISE NOTICE 'Migration 064 already applied — nothing to do';
        RETURN;
    END IF;

    -- Create BEFORE the drops so the uniqueness guard is never absent.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_file_absence_slot
        ON file_absence (source_location, sid, session_type, file_date, file_hour)
        NULLS NOT DISTINCT;

    -- Renames the index to the constraint name.
    ALTER TABLE file_absence
        ADD CONSTRAINT file_absence_slot_uniq
        UNIQUE USING INDEX idx_file_absence_slot;

    DROP INDEX IF EXISTS uq_file_absence_daily;
    DROP INDEX IF EXISTS uq_file_absence_hourly;
END $$;

COMMENT ON CONSTRAINT file_absence_slot_uniq ON file_absence IS
    'One row per (source_location, station, session, date, hour); NULL hour = '
    'daily slot. Non-partial, so the leading equality columns stay indexable '
    'under the functions'' NULL-safe IS NOT DISTINCT FROM predicate — replaced '
    'the two partial uniques in migration 064.';

COMMIT;

ANALYZE file_absence;
