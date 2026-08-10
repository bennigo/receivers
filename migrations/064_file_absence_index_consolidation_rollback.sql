-- Rollback 064: restore the two partial unique indexes on file_absence.
--
-- Note: this reinstates the state in which the table had ZERO usable indexes
-- (pgdev measured idx_scan = 0 across all four, 12.7e9 tuples seq-scanned).

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_absence_daily
    ON file_absence (source_location, sid, session_type, file_date)
    WHERE file_hour IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_absence_hourly
    ON file_absence (source_location, sid, session_type, file_date, file_hour)
    WHERE file_hour IS NOT NULL;

-- Dropping the constraint drops its backing index — 064 renamed it to the
-- constraint name. The second DROP only matters for a 064 that aborted
-- between CREATE INDEX and ADD CONSTRAINT, leaving the pre-rename name.
ALTER TABLE file_absence
    DROP CONSTRAINT IF EXISTS file_absence_slot_uniq;
DROP INDEX IF EXISTS idx_file_absence_slot;

COMMIT;

ANALYZE file_absence;
