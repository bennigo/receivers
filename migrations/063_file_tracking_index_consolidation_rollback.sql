-- Rollback 063: restore the six partial indexes on file_tracking, drop the
-- consolidated uniqueness constraint.
--
-- Note: this reinstates the seq-scan behaviour documented in 063 — any query
-- that omits a file_hour / status predicate becomes unindexable again.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS idx_file_tracking_hourly
    ON file_tracking (sid, session_type, file_date, file_hour)
    WHERE file_hour IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_file_tracking_daily
    ON file_tracking (sid, session_type, file_date)
    WHERE file_hour IS NULL;

CREATE INDEX IF NOT EXISTS idx_file_tracking_not_imported
    ON file_tracking (sid, session_type, file_date)
    WHERE NOT imported_to_db;

CREATE INDEX IF NOT EXISTS idx_file_tracking_missing
    ON file_tracking (sid, session_type, file_date)
    WHERE status = 'missing';

CREATE INDEX IF NOT EXISTS idx_file_tracking_needs_integrity
    ON file_tracking (sid, session_type, file_date)
    WHERE status IN ('downloaded', 'archived') AND integrity_checked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_file_tracking_suspect
    ON file_tracking (sid, session_type)
    WHERE status = 'suspect';

-- Dropping the constraint drops its backing index — 063 renamed it to the
-- constraint name. The second DROP only matters for a 063 that aborted
-- between CREATE INDEX and ADD CONSTRAINT, leaving the pre-rename name.
ALTER TABLE file_tracking
    DROP CONSTRAINT IF EXISTS file_tracking_slot_uniq;
DROP INDEX IF EXISTS idx_file_tracking_slot;

COMMIT;

ANALYZE file_tracking;
