-- Rollback 065: recreate the two never-read indexes on file_tracking.
--
-- Note what this restores: idx_file_tracking_updated indexes a column that
-- upsert_file_tracking rewrites on every update, which makes HOT updates
-- structurally impossible for the whole table (measured 0.00-0.1 % HOT before
-- migration 065). Only run this if a query that actually needs one of them
-- has since been written.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_file_tracking_updated
    ON file_tracking (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_file_tracking_content_sha256
    ON file_tracking (content_sha256)
    WHERE content_sha256 IS NOT NULL;

COMMIT;

ANALYZE file_tracking;
