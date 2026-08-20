-- Verification for migration 069 (safe on any host: fully transactional, ends
-- in ROLLBACK, leaves no rows behind).
--
--   psql -d gps_health -f migrations/069_external_rinex_data_status_verify.sql
--
-- Expected AFTER 069:   ZZE1 -> 0/0 (green)  ZZE2 -> 1/1 (yellow)  ZZE4 -> 2/2 (red)
-- BEFORE 069:           all three -> -2/-2 (N/A)  <- the bug being fixed
--
-- Three synthetic EXTERNAL stations model the daily-publish recency ladder. Each
-- has health_check = 'passive' and no receiver_type (an external station has no
-- receiver), and carries only a '15s_24hr_rinex' file_tracking row -- the exact
-- shape external_fetch.py records. No '15s_24hr' raw row is inserted, so the old
-- code path short-circuits to -2 on `receiver_type IS NULL AND no raw`.
--
--   ZZE1  "delivered yesterday"        -> status_24h 0, rinex_24h_status 0
--   ZZE2  "two days behind"            -> status_24h 1, rinex_24h_status 1
--   ZZE4  "four days dark"             -> status_24h 2, rinex_24h_status 2
--
-- A fourth synthetic ACTIVE station (receiver_type set, raw + rinex rows) is
-- included as a regression guard: it must still follow the normal raw->RINEX
-- ladder and read green, proving the new branch did not disturb operated
-- stations.

BEGIN;

INSERT INTO stations (sid, station_status, health_check, receiver_type)
VALUES ('ZZE1', NULL, 'passive', NULL),
       ('ZZE2', NULL, 'passive', NULL),
       ('ZZE4', NULL, 'passive', NULL),
       ('ZZE5', NULL, NULL, 'PolaRX5')
ON CONFLICT (sid) DO NOTHING;

-- ever_checked requires a block_health_summary row for the ACTIVE station, else
-- its CASE short-circuits to -1 before reaching the RINEX ladder. The external
-- stations do not need one (the new branch fires before the ever_checked test).
INSERT INTO block_health_summary (sid, ts, overall_status)
VALUES ('ZZE5', NOW(), 'healthy');

-- External stations: RINEX-only, aged 1 / 2 / 4 days back.
INSERT INTO file_tracking (sid, session_type, file_date, status)
SELECT v.sid, '15s_24hr_rinex', CURRENT_DATE - v.back, 'archived'
FROM (VALUES ('ZZE1', 1), ('ZZE2', 2), ('ZZE4', 4)) AS v(sid, back);

-- Active station: raw + rinex both present and current.
INSERT INTO file_tracking (sid, session_type, file_date, status)
VALUES ('ZZE5', '15s_24hr',       CURRENT_DATE, 'archived'),
       ('ZZE5', '15s_24hr_rinex', CURRENT_DATE, 'archived');

SELECT s.sid,
       s.status_24h,
       s.rinex_24h_status,
       s.raw_24h_date,
       s.rinex_24h_date,
       CASE
           WHEN s.sid = 'ZZE1' AND s.status_24h = 0 AND s.rinex_24h_status = 0 THEN 'PASS'
           WHEN s.sid = 'ZZE2' AND s.status_24h = 1 AND s.rinex_24h_status = 1 THEN 'PASS'
           WHEN s.sid = 'ZZE4' AND s.status_24h = 2 AND s.rinex_24h_status = 2 THEN 'PASS'
           WHEN s.sid = 'ZZE5' AND s.status_24h = 0 AND s.rinex_24h_status = 0 THEN 'PASS'
           ELSE 'FAIL'
       END AS result
FROM station_data_flow_status s
WHERE s.sid IN ('ZZE1', 'ZZE2', 'ZZE4', 'ZZE5')
ORDER BY s.sid;

ROLLBACK;
