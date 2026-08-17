-- Migration 067: let a per-station dead session earn terminal absence
--
-- Problem: file_absence records absences correctly (113,146 rows on rek-d01)
-- but almost nothing is ever promoted to terminal — 327 of 113,146, i.e. 0.3%.
-- 52,107 rows pass BOTH the age and confirmation thresholds and are blocked
-- solely by the serving gate. So the backfill re-requests the same provably
-- absent files every 6 h forever: 2,944 status_1hr stub files regenerated per
-- cycle, ~92k status_1hr download errors, and the :25-:55 backfill window
-- burned on days that cannot be filled instead of the June/July gaps that can
-- (21 stations still at May, 35 at June on 1Hz).
--
-- Root cause: a catch-22 in v_serving. Promotion requires a recent SUCCESSFUL
-- download of the SAME session at the SAME station:
--
--     v_serving := (p_source <> 'receiver') OR EXISTS (
--         SELECT 1 FROM file_tracking ft
--         WHERE ft.sid = p_sid AND ft.session_type = p_session_type
--           AND ft.status IN ('downloaded','archived') ...);
--
-- A station whose session has gone entirely dark can never satisfy that.
-- DYNY/status_1hr has 1,512 file_tracking rows, EVERY one 'missing' — it has
-- never once succeeded — so its absences (confirmations 7, age 6d18h) can never
-- go terminal. The stations generating the most futile retries are precisely
-- the ones the gate can never suppress.
--
-- Why the gate exists, and why it is NOT simply removed: it grants
-- config-error immunity. If a session's path template broke fleet-wide, every
-- station would look "missing", and marking those terminal would permanently
-- suppress real data. Relaxing to "serves ANY session" would lose that: on this
-- fleet, 0 stations lack a recent success in some session, so an any-session
-- gate is nearly a no-op and a fleet-wide break in ONE session would sail
-- straight through it.
--
-- Fix: keep same-session serving as the fast path, and add a second route that
-- preserves the immunity — the station is serving SOMETHING (so it is reachable
-- and configured) AND the session is healthy across the fleet (so this is one
-- station's dead session, not a global misconfiguration). status_1hr sits at
-- 122/180 = 68% serving today, so DYNY unblocks; a genuine fleet-wide break
-- drops that toward zero and the protection holds.
--
-- Performance: the fleet-health count costs ~375 ms on rek-d01 (no index covers
-- session_type + status + last_checked, and adding one to a 1M-row hot table is
-- a bigger change than this warrants). record_file_absence is called once per
-- absent file — 2,944 per cycle — so evaluating it inline would add ~18 min per
-- cycle. It is therefore cached in session_serving_health and recomputed at
-- most every 15 minutes. Fleet health moves on the order of hours; 15-minute
-- staleness cannot change a correct verdict into an incorrect one.

BEGIN;

-- The new signature adds a 9th parameter. CREATE OR REPLACE with a different
-- arity OVERLOADS rather than replaces, and the only caller passes 5 arguments
-- (`record_file_absence(sid, session, date, hour, 'receiver')`) — with both an
-- 8-arg and a 9-arg candidate, all remaining parameters defaulted, that call is
-- ambiguous and errors. Drop the old signature explicitly.
DROP FUNCTION IF EXISTS record_file_absence(
    varchar, varchar, date, smallint, varchar, integer, integer, integer
);

CREATE TABLE IF NOT EXISTS session_serving_health (
    session_type      varchar(32) PRIMARY KEY,
    serving_stations  integer     NOT NULL,
    total_stations    integer     NOT NULL,
    computed_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE session_serving_health IS
    'Cached count of stations recently serving each session_type. Read by '
    'record_file_absence to decide whether a session is healthy fleet-wide '
    'before letting a per-station dead session earn terminal absence. '
    'Recomputed at most every 15 min (migration 067).';


-- Refresh the cache for one session if the entry is missing or stale.
CREATE OR REPLACE FUNCTION refresh_session_serving_health(
    p_session_type       varchar,
    p_window_days        integer DEFAULT 2,
    p_max_age_minutes    integer DEFAULT 15
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_fresh   boolean;
    v_serving integer;
    v_total   integer;
BEGIN
    SELECT computed_at > now() - (p_max_age_minutes * INTERVAL '1 minute')
      INTO v_fresh
      FROM session_serving_health
     WHERE session_type = p_session_type;

    IF COALESCE(v_fresh, false) THEN
        RETURN;
    END IF;

    SELECT count(DISTINCT sid) FILTER (
               WHERE status IN ('downloaded', 'archived')
                 AND last_checked > now() - (p_window_days * INTERVAL '1 day')),
           count(DISTINCT sid)
      INTO v_serving, v_total
      FROM file_tracking
     WHERE session_type = p_session_type;

    INSERT INTO session_serving_health
        (session_type, serving_stations, total_stations, computed_at)
    VALUES (p_session_type, COALESCE(v_serving, 0), COALESCE(v_total, 0), now())
    ON CONFLICT (session_type) DO UPDATE
       SET serving_stations = EXCLUDED.serving_stations,
           total_stations   = EXCLUDED.total_stations,
           computed_at      = EXCLUDED.computed_at;
END;
$$;


CREATE OR REPLACE FUNCTION record_file_absence(
    p_sid                       varchar,
    p_session_type              varchar,
    p_date                      date,
    p_hour                      smallint DEFAULT NULL,
    p_source                    varchar  DEFAULT 'receiver',
    p_terminal_after_days       integer  DEFAULT 3,
    p_min_confirmations         integer  DEFAULT 3,
    p_serving_window_days       integer  DEFAULT 2,
    p_min_fleet_serving_pct     numeric  DEFAULT 0.50
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_serving   BOOLEAN;
    v_any       BOOLEAN;
    v_fleet_ok  BOOLEAN;
BEGIN
    -- Fast path, unchanged: this station is serving THIS session.
    v_serving := (p_source <> 'receiver') OR EXISTS (
        SELECT 1 FROM file_tracking ft
        WHERE ft.sid = p_sid
          AND ft.session_type = p_session_type
          AND ft.status IN ('downloaded', 'archived')
          AND ft.last_checked > now() - (p_serving_window_days * INTERVAL '1 day')
    );

    -- Second route (migration 067): the station is reachable and delivering
    -- SOMETHING, and this session is healthy across the fleet. That separates
    -- "one station's session is dead" (suppressible) from "the session is
    -- broken for everyone" (must stay retryable).
    IF NOT v_serving THEN
        v_any := EXISTS (
            SELECT 1 FROM file_tracking ft
            WHERE ft.sid = p_sid
              AND ft.status IN ('downloaded', 'archived')
              AND ft.last_checked > now() - (p_serving_window_days * INTERVAL '1 day')
        );

        IF v_any THEN
            PERFORM refresh_session_serving_health(
                p_session_type, p_serving_window_days
            );
            SELECT total_stations > 0
                   AND serving_stations::numeric / total_stations
                       >= p_min_fleet_serving_pct
              INTO v_fleet_ok
              FROM session_serving_health
             WHERE session_type = p_session_type;

            v_serving := COALESCE(v_fleet_ok, false);
        END IF;
    END IF;

    UPDATE file_absence
       SET confirmations     = confirmations + 1,
           last_confirmed_at = now(),
           terminal = terminal OR (
               v_serving
               AND now() - first_confirmed_at
                   >= (p_terminal_after_days * INTERVAL '1 day')
               AND confirmations + 1 >= p_min_confirmations
           )
     WHERE source_location = p_source
       AND sid = p_sid
       AND session_type = p_session_type
       AND file_date = p_date
       AND file_hour IS NOT DISTINCT FROM p_hour;

    IF NOT FOUND THEN
        BEGIN
            INSERT INTO file_absence
                (source_location, sid, session_type, file_date, file_hour)
            VALUES (p_source, p_sid, p_session_type, p_date, p_hour);
        EXCEPTION WHEN unique_violation THEN
            UPDATE file_absence
               SET confirmations = confirmations + 1, last_confirmed_at = now()
             WHERE source_location = p_source AND sid = p_sid
               AND session_type = p_session_type AND file_date = p_date
               AND file_hour IS NOT DISTINCT FROM p_hour;
        END;
    END IF;
END;
$$;

COMMIT;
