"""Morning recovery — daily catch-up download for stations missing yesterday's file.

Driving incident (2026-05-08): the primary 00:01 window and the 00:36
second-chance retry both miss a class of PolaRX5 stations whose receivers
need 30–90 min after midnight UTC before reliably serving the daily 24h
file via FTP. This job reruns the same `_download_station_data_job` for
those stations at 01:30 UTC — late enough that receivers have settled,
early enough to land before GAMIT processing starts at 03:00–04:00 UTC.

Coexists with the 00:36 second-chance retry. They serve different cases:

* 00:36 retry: catches transient-at-midnight failures (passive→active
  mode flip, brief receiver glitches).
* 01:30 morning recovery: catches receivers that needed an hour or more.

Reuses `_download_station_data_job` end-to-end. Confirms each station's
success via `file_tracking` (status IN ('downloaded', 'archived')).

Configured via `morning_recovery:` section in `scheduler.yaml`. Disabled
by default in package; enabled in the operational deployment via
gps-config-data.

See `docs/design/morning-recovery.md` for the full design rationale.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta, timezone
from typing import List, Tuple

logger = logging.getLogger("receivers.scheduler.morning_recovery")

# Hard cutoff hour (UTC) — stop iterating new dates once we're within 15 min
# of this hour, so morning recovery cannot run into downstream GAMIT processing.
# When the loop hits this guard, remaining dates are reported as deferred so an
# operator can see what was skipped.
_DEADLINE_HOUR_UTC = 3
_DEADLINE_GUARD_MINUTES = 15


def _query_stations_missing_yesterday(
    session: str,
    target_date: date,
    bypass_known_missing: bool,
    stale_missing_window_minutes: int = 120,
) -> List[str]:
    """Return station IDs whose `session` file for `target_date` is missing.

    Excludes:
      - Passive stations (`station_data_flow_status.health_status < 0`)
      - Stations that already have the file in `file_tracking` (downloaded/archived)
      - Stations marked `status='missing'` in `file_tracking` for this
        (sid, session, date) tuple AND whose last_attempt is older than
        `stale_missing_window_minutes` ago — UNLESS `bypass_known_missing=True`.

    The age guard exists because a `missing` row can be written by a
    transient connection failure during the primary 00:01 window; if morning
    recovery skipped any station with such a row, those transient failures
    would never get a second attempt before the GAMIT deadline. Fresh rows
    (within the window) stay queued so morning_recovery can confirm or
    refute the marker.

    Logs a per-exclusion-reason count so the operator can tell *why* a
    station isn't in the retry queue. Without this, "BAUG didn't show up in
    morning_recovery" is opaque — the queue logic is invisible.

    Args:
        session: Session type (e.g. '15s_24hr').
        target_date: Date to check (typically yesterday).
        bypass_known_missing: If True, retry even stations marked 'missing'
            regardless of how stale the marker is.
        stale_missing_window_minutes: A `missing` row counts as
            'marked_missing' only when its last_attempt is older than this
            many minutes. Default 120 (2 h). Set to 0 to revert to the
            old "any missing row excludes" semantics.

    Returns:
        List of station IDs to retry, sorted alphabetically.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    # Single diagnostic query that reports each row's bucket:
    #   queued     — will be retried
    #   passive    — health_status < 0
    #   already_ok — file_tracking has 'downloaded'/'archived' row
    #   marked_missing — file_tracking has 'missing' status (locks out unless bypass)
    #   not_targeted — status_24h != 2 (e.g. file actually present, dashboard view says so)
    sql = """
      WITH categorized AS (
        SELECT
          s.sid,
          CASE
            WHEN s.health_status < 0 THEN 'passive'
            WHEN ft.sid IS NOT NULL THEN 'already_ok'
            WHEN s.status_24h <> 2 THEN 'not_targeted'
            WHEN EXISTS (
              SELECT 1 FROM file_tracking ft2
              WHERE ft2.sid = s.sid
                AND ft2.session_type = %(sess)s
                AND ft2.file_date = %(date)s
                AND ft2.status = 'missing'
                AND ft2.last_attempt < NOW() - (%(stale_minutes)s * INTERVAL '1 minute')
            ) THEN 'marked_missing'
            ELSE 'queued'
          END AS bucket
        FROM station_data_flow_status s
        LEFT JOIN file_tracking ft
               ON ft.sid = s.sid
              AND ft.session_type = %(sess)s
              AND ft.file_date = %(date)s
              AND ft.status IN ('downloaded', 'archived')
      )
      SELECT bucket, array_agg(sid ORDER BY sid) AS sids, count(*) AS n
      FROM categorized
      GROUP BY bucket
    """
    buckets: dict[str, tuple[list[str], int]] = {}
    with DatabaseConnectionFactory.connection(single_host=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                {
                    "sess": session,
                    "date": target_date,
                    "stale_minutes": stale_missing_window_minutes,
                },
            )
            for bucket, sids, n in cur.fetchall():
                buckets[bucket] = (list(sids), int(n))

    queued, queued_n = buckets.get("queued", ([], 0))
    if bypass_known_missing and "marked_missing" in buckets:
        # Promote stations locked out by the 'missing' TTL into the retry
        # queue when the operator opts in. Keeps the bucket counts honest
        # in the log line below (they reflect the promotion).
        promoted_sids, promoted_n = buckets["marked_missing"]
        queued = sorted(queued + promoted_sids)
        queued_n += promoted_n
        buckets["marked_missing"] = ([], 0)

    # Compact summary so the operator can scan it at a glance. Hide buckets
    # with zero stations to keep the line readable in the common case.
    summary_parts = [f"queued={queued_n}"]
    for label in ("passive", "already_ok", "not_targeted", "marked_missing"):
        _sids, n = buckets.get(label, ([], 0))
        if n:
            summary_parts.append(f"{label}={n}")
    logger.info(
        f"🌅 Morning recovery {session} ({target_date}) filter summary: "
        + ", ".join(summary_parts)
        + f" (stale_missing_window={stale_missing_window_minutes}m)"
    )

    # If any non-trivial bucket exists, surface the SIDs so post-hoc audit
    # is possible without re-querying the DB. Capped at 15 per line to
    # avoid swamping the log when most of the fleet is up to date.
    for label in ("marked_missing", "not_targeted"):
        sids, n = buckets.get(label, ([], 0))
        if n:
            preview = ", ".join(sids[:15]) + (f" [+{n - 15} more]" if n > 15 else "")
            logger.info(
                f"   ↪ {label}: {preview}"
                + (
                    " (use bypass_known_missing=True to override)"
                    if label == "marked_missing"
                    else ""
                )
            )

    return queued


def _confirm_recovered(sid: str, session: str, target_date: date) -> bool:
    """True if `file_tracking` shows the file as downloaded/archived after retry."""
    from ..health.database_factory import DatabaseConnectionFactory

    with DatabaseConnectionFactory.connection(single_host=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM file_tracking
                WHERE sid = %s AND session_type = %s
                  AND file_date = %s
                  AND status IN ('downloaded', 'archived')
                LIMIT 1
                """,
                (sid, session, target_date),
            )
            return cur.fetchone() is not None


def _retry_station(
    sid: str,
    session: str,
    target_date: date,
    timeout_minutes: int,
    run_rinex: bool,
    production_mode: bool,
) -> Tuple[str, bool]:
    """Run a single-station download attempt, return (sid, recovered)."""
    try:
        # Late import to avoid circulars during module load
        from .bulk_scheduler import _download_station_data_job

        _download_station_data_job(
            sid,
            session,
            production_mode=production_mode,
            timeout_minutes=timeout_minutes,
            run_rinex=run_rinex,
        )
        return sid, _confirm_recovered(sid, session, target_date)
    except Exception as exc:
        logger.warning(f"Morning recovery {session} {sid}: {exc}")
        return sid, False


def _run_morning_recovery_job(
    sessions: List[str],
    days_back: int = 1,
    max_workers: int = 4,
    station_timeout_minutes: int = 8,
    bypass_known_missing: bool = False,
    stale_missing_window_minutes: int = 120,
) -> None:
    """APScheduler job: re-download yesterday's missing files for missing stations.

    Args:
        sessions: Session types to recover (initially just ['15s_24hr']).
        days_back: How many days back to target. 1 = yesterday.
        max_workers: Concurrent station downloads (default 4).
        station_timeout_minutes: Hard ceiling per station so a stuck FTP
            doesn't block the whole job.
        bypass_known_missing: If True, retry even stations that file_tracking
            has marked status='missing'.
        stale_missing_window_minutes: A `missing` row counts as
            'marked_missing' (skip) only when its last_attempt is older than
            this many minutes. Default 120 (2 h) — fresh transient-failure
            marks stay queued; long-stale marks remain skipped.
    """
    today = datetime.now(UTC).date()
    # Most-recent-first so newest gaps get retried before the deadline if time runs out.
    target_dates = [today - timedelta(days=n) for n in range(1, days_back + 1)]

    # Resolve scheduler context to mirror the regular daily job. Falls back to
    # safe defaults when this is invoked outside a running scheduler (e.g. tests).
    production_mode = True
    try:
        from .bulk_scheduler import _scheduler_instance

        if _scheduler_instance is not None:
            production_mode = _scheduler_instance.production_mode
    except Exception:
        pass

    if days_back > 1:
        logger.info(
            f"🌅 Morning recovery starting: sessions={sessions} "
            f"days_back={days_back} dates={[d.isoformat() for d in target_dates]}"
        )

    deferred_dates: List[date] = []

    for target_date in target_dates:
        # Deadline guard: stop the loop if there's less than _DEADLINE_GUARD_MINUTES
        # until the next _DEADLINE_HOUR_UTC. Any unprocessed dates are surfaced in
        # the final log line so an operator can see what was deferred.
        # When invoked outside the 01:30 UTC window (e.g. manual dry-run at 14:00),
        # the "next" deadline is tomorrow, so the loop proceeds normally.
        now = datetime.now(UTC)
        deadline_today = now.replace(
            hour=_DEADLINE_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        next_deadline = (
            deadline_today
            if now < deadline_today
            else deadline_today + timedelta(days=1)
        )
        if (next_deadline - now) < timedelta(minutes=_DEADLINE_GUARD_MINUTES):
            remaining = target_dates[target_dates.index(target_date) :]
            deferred_dates.extend(remaining)
            logger.warning(
                f"🌅 Morning recovery deadline guard hit at {now.strftime('%H:%M:%S')} UTC "
                f"(< {_DEADLINE_GUARD_MINUTES}min before {_DEADLINE_HOUR_UTC:02d}:00) "
                f"— deferring dates: {[d.isoformat() for d in remaining]}"
            )
            break

        for session in sessions:
            sids = _query_stations_missing_yesterday(
                session,
                target_date,
                bypass_known_missing,
                stale_missing_window_minutes=stale_missing_window_minutes,
            )
            if not sids:
                logger.info(
                    f"🌅 Morning recovery {session} ({target_date}): "
                    "nothing to retry — all stations have this date's file"
                )
                continue

            # Resolve run_rinex from the live scheduler config. Silent
            # fallback to a hardcoded value risks diverging from the
            # operator's actual policy — if the scheduler instance isn't
            # reachable, log a clear warning and use the documented default.
            run_rinex = False
            config_resolved = False
            try:
                from .bulk_scheduler import _scheduler_instance

                if _scheduler_instance is not None:
                    cfg = _scheduler_instance.schedule_configs.get(session)
                    if cfg is not None:
                        run_rinex = cfg.rinex
                        config_resolved = True
            except Exception as exc:
                logger.warning(
                    f"Morning recovery: could not resolve schedule config for "
                    f"{session}: {exc}"
                )

            if not config_resolved:
                run_rinex = session == "15s_24hr"
                logger.warning(
                    f"Morning recovery: schedule_configs unavailable for "
                    f"{session} — falling back to default run_rinex={run_rinex}. "
                    f"Verify scheduler config if this is unexpected."
                )

            sid_preview = ", ".join(sids[:10]) + (
                f" [+{len(sids) - 10} more]" if len(sids) > 10 else ""
            )
            logger.info(
                f"🌅 Morning recovery {session} ({target_date}): "
                f"{len(sids)} stations queued — {sid_preview}"
            )

            recovered: List[str] = []
            still_failing: List[str] = []
            workers = min(max_workers, len(sids))

            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="morn_rec"
            ) as pool:
                futures = {
                    pool.submit(
                        _retry_station,
                        sid,
                        session,
                        target_date,
                        station_timeout_minutes,
                        run_rinex,
                        production_mode,
                    ): sid
                    for sid in sids
                }
                for fut in as_completed(futures):
                    sid, success = fut.result()
                    (recovered if success else still_failing).append(sid)

            recovered.sort()
            still_failing.sort()
            recovered_preview = ", ".join(recovered[:15]) + (
                f" [+{len(recovered) - 15} more]" if len(recovered) > 15 else ""
            )
            failing_preview = ", ".join(still_failing[:15]) + (
                f" [+{len(still_failing) - 15} more]" if len(still_failing) > 15 else ""
            )
            logger.info(
                f"🌅 Morning recovery {session} ({target_date}) complete: "
                f"{len(recovered)}/{len(sids)} recovered"
                + (f" — {recovered_preview}" if recovered else "")
                + (f" | still failing: {failing_preview}" if still_failing else "")
            )

    if deferred_dates and days_back > 1:
        logger.info(
            f"🌅 Morning recovery summary: {len(target_dates) - len(deferred_dates)}/"
            f"{len(target_dates)} dates processed, "
            f"deferred={[d.isoformat() for d in deferred_dates]}"
        )
