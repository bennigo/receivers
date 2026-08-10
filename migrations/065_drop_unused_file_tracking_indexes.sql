-- Migration 065: drop two never-read indexes on file_tracking
--
-- These look like a 294 MB housekeeping win. They are not — they are a WRITE
-- problem. Measured 2026-08-10:
--
--   pgdev    n_tup_upd = 74,955,000   n_tup_hot_upd =  80,524   -> 0.1 % HOT
--   rek-d01  n_tup_upd =  5,893,348   n_tup_hot_upd =      13   -> 0.00 % HOT
--   pgdev    autovacuum_count = 51,483 on this one table
--
-- A HOT (heap-only tuple) update skips every index on the table. PostgreSQL
-- can only use it when NO indexed column's value changes. `idx_file_tracking_
-- updated` indexes `updated_at`, and `upsert_file_tracking` sets
-- `updated_at = NOW()` on EVERY update — so the indexed value always changes
-- and HOT is structurally impossible. Every one of those ~75M updates
-- therefore writes a new entry into every index and leaves a dead one behind.
-- That is where the vacuum load comes from.
--
-- What the two indexes bought us for that price:
--
--   idx_file_tracking_updated         220 MB   idx_scan = 3 (pgdev), 0 (rek-d01)
--   idx_file_tracking_content_sha256   74 MB   idx_scan = 0 on both
--
-- Neither backs a query in the tree:
--   * nothing orders file_tracking by updated_at — the only
--     `ORDER BY updated_at DESC` is on the STATIONS table (cli/db.py:498),
--     and pipeline.py:524 is SQLite, not this database;
--   * the sole reader of content_sha256 (archive/verify.py:200) SELECTs the
--     column but looks up by (sid, session_type, file_date, file_hour) — it
--     never searches BY hash. Migration 052 built the index for "dedup lookups
--     by content hash", a feature that was never written.
--
-- KEPT: idx_file_tracking_needs_hash (230 scans — drives the lazy hash
-- backfill) and idx_file_tracking_status (222,991 scans on pgdev; 0 on
-- rek-d01 because it is the Grafana dashboards that use it).
--
-- Removing the updated_at index removes the structural HOT blocker. The
-- realized gain still depends on free space within each heap page, so a
-- follow-up `ALTER TABLE file_tracking SET (fillfactor = 90)` may be worth
-- measuring — deliberately NOT bundled here, since it only affects pages
-- written after it and wants its own before/after.
--
-- Applying to a SHARED or BUSY server: use DROP INDEX CONCURRENTLY by hand,
-- see docs/deployment/apply-index-consolidation.md. Note that a CONCURRENTLY
-- drop that hits its lock_timeout leaves the index indisvalid=false but still
-- LIVE — the planner stops using it while writes still maintain it, i.e. all
-- of the cost and none of the benefit. Always re-check pg_index.indisvalid
-- and finish the job.
--
-- Usage (dedicated host, quiet):
--   psql -h localhost -U bgo -d gps_health -f migrations/065_drop_unused_file_tracking_indexes.sql

BEGIN;

DROP INDEX IF EXISTS idx_file_tracking_updated;
DROP INDEX IF EXISTS idx_file_tracking_content_sha256;

COMMIT;

ANALYZE file_tracking;
