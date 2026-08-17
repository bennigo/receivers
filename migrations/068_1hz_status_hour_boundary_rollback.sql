-- Rollback for migration 068: restore the 066 sliding-window 1Hz thresholds.
--
-- Re-applies the 066 view definition verbatim (CREATE OR REPLACE, so grants and
-- dependent views survive) and removes the 068 marker row.
--
-- WARNING: rolling back restores the defect 068 fixes -- the whole active fleet
-- reads amber on status_1hz / rinex_1hz_status again. Roll back only if 068
-- itself misbehaves, not to "quiet" the board.
--
-- Apply to BOTH rek-d01 and pgdev.vedur.is.

BEGIN;

CREATE OR REPLACE VIEW station_data_flow_status AS
WITH latest_raw_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_raw_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour::integer, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
latest_rinex_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_rinex_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour::integer, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
health_streak AS (
    SELECT recent.sid,
           COALESCE(MIN(recent.rn) FILTER (WHERE recent.overall_status <> 'critical'), 7) - 1
               AS consecutive_critical
    FROM (
        SELECT sid, overall_status,
               ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
        FROM block_health_summary
        WHERE ts > NOW() - INTERVAL '1 day'
    ) recent
    WHERE recent.rn <= 6
    GROUP BY recent.sid
),
ever_checked AS (
    SELECT s.sid
    FROM stations s
    WHERE EXISTS (
        SELECT 1 FROM block_health_summary bhs
        WHERE bhs.sid = s.sid LIMIT 1
    )
),
flow_health AS (
    SELECT s.sid,
           h.overall_status, h.status_details,
           sc.is_online, sc.connection_state,
           s.station_status, s.health_check, s.receiver_type
    FROM stations s
    LEFT JOIN LATERAL (
        SELECT overall_status, status_details
        FROM block_health_summary
        WHERE sid = s.sid AND ts > NOW() - INTERVAL '1 day'
        ORDER BY ts DESC LIMIT 1
    ) h ON true
    LEFT JOIN station_connectivity sc ON sc.sid = s.sid
),
latest_disk AS (
    SELECT DISTINCT ON (sid) sid, ts, total_mb, usage_percent
    FROM block_disk_status
    WHERE ts > NOW() - INTERVAL '1 day'
    ORDER BY sid, ts DESC
),
base AS (
    SELECT fh.sid,
        -- health_status (unchanged)
        CASE
            WHEN fh.station_status IS NOT NULL OR fh.health_check IS NOT NULL THEN -2
            WHEN fh.is_online = false THEN -1
            WHEN fh.overall_status = 'healthy'  THEN 0
            WHEN fh.overall_status = 'warning'  THEN 1
            WHEN fh.overall_status = 'critical'
                 AND fh.connection_state = 'online'
                 AND (fh.status_details IS NULL
                      OR fh.status_details !~* '(Voltage|Temperature|Disk|Satellite)') THEN 1
            WHEN fh.overall_status = 'critical'
                 AND COALESCE(hs.consecutive_critical, 1) >= 2 THEN 2
            WHEN fh.overall_status = 'critical' THEN 1
            ELSE -1
        END AS health_status,
        -- status_24h (unchanged)
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN fh.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND r24.file_date IS NULL THEN -1
            WHEN r24.file_date IS NULL AND fh.is_online = true
                 AND NOT (COALESCE(ds.completions, 0) = 0 AND COALESCE(ds.failures, 0) >= 3)
                 AND EXISTS (SELECT 1 FROM file_tracking ft
                             WHERE ft.sid = fh.sid AND ft.session_type = '15s_24hr'
                               AND ft.status = 'missing'
                               AND ft.file_date >= CURRENT_DATE - 1)
              THEN -1
            WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1 THEN 2
            WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date   THEN 1
            ELSE 0
        END AS status_24h,
        -- status_1hz (unchanged)
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false)
                 AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN r1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours'   THEN 1
            WHEN r1h.latest_ts IS NOT NULL                      THEN 2
            ELSE 2
        END AS status_1hz,
        -- rinex_24h_status (FIXED in 066 -- see header)
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN fh.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND x24.file_date IS NULL AND r24.file_date IS NULL THEN -1
            WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
            WHEN x24.file_date IS NULL AND r24.file_date IS NOT NULL THEN 2
            WHEN x24.file_date IS NULL THEN -1
            -- FIXED in 066: the grace period below is for a station exactly one
            -- day behind, waiting on tonight's conversion. Anything older than
            -- that is missing regardless of the hour -- without this guard a
            -- station 35 days stale fell through to ELSE 0 and read green
            -- every night between 00:00 and 02:00.
            WHEN x24.file_date < CURRENT_DATE - 2 THEN 2
            WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
            WHEN EXTRACT(HOUR FROM NOW()) >= 2  THEN 1
            ELSE 0
        END AS rinex_24h_status,
        -- rinex_1hz_status (unchanged)
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false)
                 AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND x1h.latest_ts IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN x1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '6 hours'   THEN 1
            ELSE 2
        END AS rinex_1hz_status,
        -- download_status (unchanged)
        CASE
            WHEN fh.station_status IS NOT NULL OR fh.health_check IS NOT NULL THEN -2
            WHEN ds.total_attempts IS NULL OR ds.total_attempts = 0 THEN -1
            WHEN ds.completions = 0 AND ds.failures >= 3 THEN 2
            WHEN ds.stalls >= 3 THEN 1
            WHEN ds.failures > ds.completions AND ds.failures >= 5 THEN 1
            ELSE 0
        END AS download_status,
        -- disk_status (unchanged)
        CASE
            WHEN fh.station_status IS NOT NULL OR fh.health_check IS NOT NULL THEN -2
            WHEN ld.ts IS NULL THEN -1
            WHEN ld.usage_percent IS NOT NULL AND ld.usage_percent = 0 THEN 2
            WHEN ld.total_mb IS NULL OR ld.total_mb = 0 THEN -1
            WHEN ld.usage_percent > 97 THEN 2
            WHEN ld.usage_percent > 90 THEN 1
            ELSE 0
        END AS disk_status,
        r24.file_date AS raw_24h_date,
        r1h.latest_ts AS raw_1hz_ts,
        x24.file_date AS rinex_24h_date,
        x1h.latest_ts AS rinex_1hz_ts,
        -- logging_15s / logging_1hz (FIXED in 049):
        -- discontinued/inactive/suppressed (station_status NOT NULL) or passive
        -- (health_check NOT NULL) stations are not actively logging anything we
        -- collect, so both flags are forced FALSE. Active stations unchanged.
        (fh.station_status IS NULL AND fh.health_check IS NULL
         AND (fh.receiver_type IS NOT NULL
              OR COALESCE(l.session_15s_24hr, false)
              OR r24.file_date IS NOT NULL)) AS logging_15s,
        (fh.station_status IS NULL AND fh.health_check IS NULL
         AND (COALESCE(l.session_1hz_1hr, false)
              OR r1h.latest_ts IS NOT NULL)) AS logging_1hz
    FROM flow_health fh
    LEFT JOIN station_logging_status l  ON l.sid = fh.sid
    LEFT JOIN health_streak hs          ON hs.sid = fh.sid
    LEFT JOIN ever_checked ec           ON ec.sid = fh.sid
    LEFT JOIN latest_raw_24h r24        ON r24.sid = fh.sid
    LEFT JOIN latest_raw_1hz r1h        ON r1h.sid = fh.sid
    LEFT JOIN latest_rinex_24h x24      ON x24.sid = fh.sid
    LEFT JOIN latest_rinex_1hz x1h      ON x1h.sid = fh.sid
    LEFT JOIN station_download_summary ds ON ds.sid = fh.sid
    LEFT JOIN latest_disk ld            ON ld.sid = fh.sid
)
SELECT sid,
    health_status, status_24h, status_1hz,
    rinex_24h_status, rinex_1hz_status,
    download_status, disk_status,
    raw_24h_date, raw_1hz_ts,
    rinex_24h_date, rinex_1hz_ts,
    logging_15s, logging_1hz,
    -- combined_status (unchanged)
    CASE
        WHEN health_status < 0 AND status_24h < 0 THEN -1
        WHEN disk_status = 2 THEN 2
        WHEN health_status = 2 AND status_24h = 2 THEN 2
        WHEN download_status = 2 THEN 1
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h   < 0 THEN 0 ELSE status_24h   END
        ) = 2 THEN 1
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h   < 0 THEN 0 ELSE status_24h   END
        ) = 1 THEN 1
        WHEN download_status = 1 THEN 1
        ELSE 0
    END AS combined_status
FROM base;

DELETE FROM schema_migrations WHERE migration_name = '068_1hz_status_hour_boundary';

COMMIT;
