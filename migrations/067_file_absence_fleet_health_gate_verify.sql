-- Verify 067. Run AFTER applying the migration.
--   psql -d gps_health -f migrations/067_file_absence_fleet_health_gate_verify.sql

\echo '== 1. exactly ONE record_file_absence signature (arity ambiguity check) =='
SELECT count(*) AS should_be_1, string_agg(pg_get_function_identity_arguments(oid), ' | ')
FROM pg_proc WHERE proname = 'record_file_absence';

\echo '== 2. the 5-arg call the application makes still resolves =='
SELECT record_file_absence('__VERIFY__', 'status_1hr', current_date - 400,
                           12::smallint, 'receiver');
SELECT count(*) AS verify_row_created FROM file_absence WHERE sid = '__VERIFY__';
DELETE FROM file_absence WHERE sid = '__VERIFY__';

\echo '== 3. fleet-health cache populated and fresh =='
SELECT session_type, serving_stations, total_stations,
       round(serving_stations::numeric / NULLIF(total_stations,0), 3) AS frac,
       age(now(), computed_at) AS cache_age
FROM session_serving_health ORDER BY session_type;

\echo '== 4. terminal promotion (expect a large jump from the 0.3% baseline) =='
SELECT session_type, terminal, count(*) FROM file_absence GROUP BY 1,2 ORDER BY 1,2;

\echo '== 5. still-blocked rows: age+confirmations met but not terminal =='
SELECT session_type, count(*) AS blocked
FROM file_absence
WHERE NOT terminal AND confirmations >= 3
  AND now() - first_confirmed_at >= interval '3 days'
GROUP BY 1 ORDER BY 1;
