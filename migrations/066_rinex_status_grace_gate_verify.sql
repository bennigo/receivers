-- Verification for migration 066 (safe on any host: fully transactional, ends
-- in ROLLBACK, leaves no rows behind).
--
--   psql -d gps_health -f migrations/066_rinex_status_grace_gate_verify.sql
--
-- Expected after 066:  ZZST -> 2 (PASS), ZZOK -> 0 (PASS)
-- Before 066:          ZZST -> 0 (FAIL) -- the bug being fixed.
--
-- rinex_24h_status only misreports inside the 00:00-02:00 grace window, so the
-- session TimeZone is shifted to put local time there. The offset is computed
-- from the current UTC hour (wrapped into -11..+12 so it is always a valid
-- Etc/GMT zone), which is why this works at any time of day. The synthetic
-- rows are inserted AFTER the shift so their CURRENT_DATE arithmetic and the
-- view's agree on which day it is.

BEGIN;

SELECT CASE
           WHEN off = 0 THEN 'UTC'
           WHEN off < 0 THEN 'Etc/GMT+' || (-off)::text
           ELSE              'Etc/GMT-' || off::text
       END AS zone
FROM (
    SELECT CASE WHEN m > 12 THEN m - 24 ELSE m END AS off
    FROM (
        SELECT ((1 - EXTRACT(HOUR FROM NOW() AT TIME ZONE 'UTC')::int) % 24 + 24) % 24 AS m
    ) a
) b
\gset

SET LOCAL TIME ZONE :'zone';

-- Two synthetic stations, both with fresh raw:
--   ZZST = 35 days stale RINEX (the SEY1 case)   -> must be 2 (Missing)
--   ZZOK =  2 days stale RINEX (nightly lag)     -> must keep the grace period
INSERT INTO stations (sid, model_mismatch) VALUES ('ZZST', false), ('ZZOK', false);
INSERT INTO file_tracking (sid, session_type, file_date, status) VALUES
    ('ZZST', '15s_24hr',       CURRENT_DATE - 1,  'archived'),
    ('ZZST', '15s_24hr_rinex', CURRENT_DATE - 35, 'archived'),
    ('ZZOK', '15s_24hr',       CURRENT_DATE - 1,  'archived'),
    ('ZZOK', '15s_24hr_rinex', CURRENT_DATE - 2,  'archived');

SELECT EXTRACT(HOUR FROM NOW())::int AS forced_hour,
       sid,
       rinex_24h_status,
       CASE WHEN sid = 'ZZST' AND rinex_24h_status = 2 THEN 'PASS: 35d stale = Missing'
            WHEN sid = 'ZZST' THEN 'FAIL: 35d stale reported ' || rinex_24h_status
            WHEN sid = 'ZZOK' AND rinex_24h_status = 0 THEN 'PASS: nightly lag keeps grace'
            ELSE 'FAIL: nightly lag reported ' || rinex_24h_status
       END AS verdict
FROM station_data_flow_status WHERE sid IN ('ZZST','ZZOK') ORDER BY sid;

ROLLBACK;
