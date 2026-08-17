-- Verification for migration 068 (safe on any host: fully transactional, ends
-- in ROLLBACK, leaves no rows behind).
--
--   psql -d gps_health -f migrations/068_1hz_status_hour_boundary_verify.sql
--
-- Expected AFTER 068:   ZZHL -> 0 (PASS)   ZZBH -> 1 (PASS)   ZZDD -> 2 (PASS)
-- BEFORE 068:           ZZHL -> 1 (FAIL)  <- the bug being fixed
--
-- The three synthetic stations model the three real populations measured on
-- rek-d01 2026-08-17:
--
--   ZZHL  "healthy, late in the hour"  -- newest 1Hz label = the last COMPLETE
--         hour. Under the old sliding 1.5 h window this read amber for most of
--         every hour (the 151-station cluster at 108-140 min). Must be GREEN.
--   ZZBH  "one cycle behind"           -- newest label is one hour further back.
--         Must stay AMBER; this is the case a mere threshold raise would have
--         swallowed, since its band (128-200 min) overlaps ZZHL's (68-140).
--   ZZDD  "dark for days"              -- must stay RED.
--
-- No TimeZone shifting is needed (unlike 066): the fix is hour-boundary
-- arithmetic, so it behaves identically at every time of day. The one instant
-- the test deliberately avoids asserting on is the ~10 min after an hour rolls
-- and before the new file lands, when a healthy station is legitimately amber
-- ("being produced") -- ZZHL is seeded against date_trunc so it is always on
-- the correct side of that.

BEGIN;

INSERT INTO stations (sid, station_status, health_check, receiver_type)
VALUES ('ZZHL', NULL, NULL, 'PolaRX5'),
       ('ZZBH', NULL, NULL, 'PolaRX5'),
       ('ZZDD', NULL, NULL, 'PolaRX5')
ON CONFLICT (sid) DO NOTHING;

-- ever_checked requires a block_health_summary row, else the CASE short-circuits
-- to -1 before any threshold is reached.
INSERT INTO block_health_summary (sid, ts, overall_status)
VALUES ('ZZHL', NOW(), 'healthy'),
       ('ZZBH', NOW(), 'healthy'),
       ('ZZDD', NOW(), 'healthy');

-- Newest 1Hz raw + rinex per station, expressed against the hour boundary so
-- the test is time-of-day independent.
INSERT INTO file_tracking (sid, session_type, file_date, file_hour, status)
SELECT v.sid, t.session_type,
       (date_trunc('hour', NOW()) - v.back)::date,
       EXTRACT(HOUR FROM date_trunc('hour', NOW()) - v.back)::smallint,
       'archived'
FROM (VALUES
        ('ZZHL', INTERVAL '1 hour'),    -- last complete hour  -> green
        ('ZZBH', INTERVAL '2 hours'),   -- one cycle behind    -> amber
        ('ZZDD', INTERVAL '72 hours')   -- three days dark     -> red
     ) AS v(sid, back),
     (VALUES ('1Hz_1hr'), ('1Hz_1hr_rinex')) AS t(session_type);

-- ZZHL only DISCRIMINATES between the old and new rule once we are far enough
-- into the hour that its label is more than 90 minutes old -- i.e. from :30
-- past the hour onward. Before :30 the old sliding window also calls it green,
-- so a PASS there proves nothing. Unlike 066 this cannot be fixed by shifting
-- the session TimeZone: TZ offsets are whole hours, so they move NOW() and
-- date_trunc('hour', NOW()) together and leave minute-of-hour untouched.
--
-- The report therefore prints what the OLD rule would have said next to the new
-- status, and downgrades ZZHL to INCONCLUSIVE rather than claiming a PASS it
-- has not earned. Re-run after :30 for the discriminating case.
SELECT s.sid,
       s.status_1hz,
       s.rinex_1hz_status,
       (s.rinex_1hz_ts >= NOW() - INTERVAL '1.5 hours') AS old_rule_green,
       EXTRACT(MINUTE FROM NOW())::int AS minute_of_hour,
       CASE
           WHEN s.sid = 'ZZHL' AND s.status_1hz = 0 AND s.rinex_1hz_status = 0
                AND s.rinex_1hz_ts < NOW() - INTERVAL '1.5 hours'
               THEN 'PASS (discriminating: old rule said amber)'
           WHEN s.sid = 'ZZHL' AND s.status_1hz = 0 AND s.rinex_1hz_status = 0
               THEN 'INCONCLUSIVE (before :30 -- old rule agrees; re-run later)'
           WHEN s.sid = 'ZZBH' AND s.status_1hz = 1 AND s.rinex_1hz_status = 1 THEN 'PASS'
           WHEN s.sid = 'ZZDD' AND s.status_1hz = 2 AND s.rinex_1hz_status = 2 THEN 'PASS'
           ELSE 'FAIL'
       END AS result
FROM station_data_flow_status s
WHERE s.sid IN ('ZZHL', 'ZZBH', 'ZZDD')
ORDER BY s.sid;

ROLLBACK;
