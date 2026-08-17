-- Rollback 067: restore the same-session-only serving gate.
--
-- Reverts to the 8-arg signature, so the 9-arg version must be dropped first
-- for the same arity/ambiguity reason the forward migration documents.
-- session_serving_health is left in place: it is inert once nothing reads it,
-- and dropping it would discard the cache for no benefit.

BEGIN;

DROP FUNCTION IF EXISTS record_file_absence(
    varchar, varchar, date, smallint, varchar, integer, integer, integer, numeric
);

CREATE OR REPLACE FUNCTION record_file_absence(
    p_sid                   varchar,
    p_session_type          varchar,
    p_date                  date,
    p_hour                  smallint DEFAULT NULL,
    p_source                varchar  DEFAULT 'receiver',
    p_terminal_after_days   integer  DEFAULT 3,
    p_min_confirmations     integer  DEFAULT 3,
    p_serving_window_days   integer  DEFAULT 2
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_serving BOOLEAN;
BEGIN
    v_serving := (p_source <> 'receiver') OR EXISTS (
        SELECT 1 FROM file_tracking ft
        WHERE ft.sid = p_sid
          AND ft.session_type = p_session_type
          AND ft.status IN ('downloaded', 'archived')
          AND ft.last_checked > now() - (p_serving_window_days * INTERVAL '1 day')
    );

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
