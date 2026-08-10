"""Long-term backfill — recover multi-day/month gaps when a station returns.

DB-driven and **classified**: query ``file_tracking`` / ``file_absence`` /
``receiver_horizon`` to build a per-station worklist bucketed as:

  ``queued``             — not in archive (raw *or* rinex), not confirmed gone
                           → download (full pipeline)
  ``confirmed_gone``     — ``file_absence.terminal`` OR older than ``receiver_horizon``
                           → skip (ran off the receiver auto-delete cycle)
  ``provisional_absent`` — ``file_absence`` row, not yet terminal → low-priority retry
  ``already_ok``         — archived → skip

This module is the **read-only classification engine** (this file) plus, later,
the worker that runs the full pipeline per ``queued`` day. The scheduler wiring
(reconnection trigger + daily backstop) is added separately — see
``docs/design/long-term-backfill.md``. Track via receivers todo #136.

The classification deliberately distinguishes "couldn't reach" (no signal here —
the health oracle gates that in the worker) from "reached and confirmed absent"
(``confirmed_gone``): a transient connection failure never records an absence,
so it never pollutes this worklist.

Read-only: never calls ``sync_archive_to_db`` (``sync_first=False``), so it
cannot mutate ``file_tracking``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta
from typing import Any, Optional

logger = logging.getLogger("receivers.scheduler.long_term_backfill")


@dataclass
class LongTermGapReport:
    """Classified long-term gap for one (station, session)."""

    sid: str
    session: str
    start: date
    end: date
    last_archived: Optional[date] = None
    receiver_horizon: Optional[date] = None  # oldest file the receiver still holds
    queued: list[Any] = field(default_factory=list)  # download candidates (GapInfo)
    confirmed_gone: int = 0  # terminal-absent or past the horizon
    provisional_absent: int = 0  # absent, not yet terminal
    already_ok: int = 0  # present in archive within the window

    @property
    def total_window_days(self) -> int:
        return (self.end - self.start).days + 1 if self.start <= self.end else 0

    def summary(self) -> str:
        hz = self.receiver_horizon.isoformat() if self.receiver_horizon else "unknown"
        la = self.last_archived.isoformat() if self.last_archived else "none"
        return (
            f"{self.sid} {self.session} [{self.start} → {self.end}] "
            f"({self.total_window_days}d) | last_archived={la} horizon={hz} | "
            f"queued={len(self.queued)} confirmed_gone={self.confirmed_gone} "
            f"provisional_absent={self.provisional_absent} already_ok={self.already_ok}"
        )


def _last_archived_date(sid: str, session: str) -> Optional[date]:
    """Most recent file_date with a present status for sid/session."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT max(file_date) FROM file_tracking "
                "WHERE sid=%s AND session_type=%s "
                "AND status IN ('archived','downloaded')",
                (sid, session),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("last_archived_date %s/%s: %s", sid, session, e)
        return None


def _receiver_horizon(sid: str, session: str) -> Optional[date]:
    """Oldest file date the receiver still holds (the auto-delete frontier)."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT oldest_date FROM receiver_horizon "
                "WHERE sid=%s AND session_type=%s "
                "ORDER BY observed_at DESC LIMIT 1",
                (sid, session),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        logger.debug("receiver_horizon %s/%s: %s", sid, session, e)
        return None


def _absence_counts(sid: str, session: str, start: date, end: date) -> tuple[int, int]:
    """Return (terminal_gone, provisional_absent) counts in [start, end]."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT count(*) FILTER (WHERE terminal), "
                "       count(*) FILTER (WHERE NOT terminal) "
                "FROM file_absence "
                "WHERE sid=%s AND session_type=%s AND file_date BETWEEN %s AND %s",
                (sid, session, start, end),
            )
            term, prov = cur.fetchone()
            return int(term or 0), int(prov or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("absence_counts %s/%s: %s", sid, session, e)
        return 0, 0


def _archived_dates(sid: str, session: str, start: date, end: date) -> set:
    """Set of file_dates with a present status for sid/session in [start, end]."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT file_date FROM file_tracking "
                "WHERE sid=%s AND session_type=%s "
                "AND status IN ('archived','downloaded') "
                "AND file_date BETWEEN %s AND %s",
                (sid, session, start, end),
            )
            return {r[0] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        logger.warning("archived_dates %s/%s: %s", sid, session, e)
        return set()


def query_long_term_gaps(
    sid: str,
    session: str,
    lookback_days: int = 365,
    receiver_type: Optional[str] = None,
) -> LongTermGapReport:
    """Classify the long-term gap for one station/session.

    Scans the FULL ``lookback_days`` window (not just trailing) so mid-range
    holes — e.g. a month missing between present data, like SARP's June — are
    found. Read-only; safe to run any time.

    Args:
        sid: Station id, e.g. ``"SARP"``.
        session: Session type, e.g. ``"15s_24hr"``.
        lookback_days: Hard cap on how far back to look.
        receiver_type: Optional, passed to ``GapDetector.find_gaps`` for the
            correct archive path/extension per receiver family. Auto-looked-up
            from station config when None (required — the PolaRX5 default path
            would false-gap every NetRS/NetR9 day).
    """
    from ..health.file_tracker import GapDetector

    sid = sid.upper()
    last_archived = _last_archived_date(sid, session)
    horizon = _receiver_horizon(sid, session)

    if receiver_type is None:
        try:
            from ..cli.main import get_station_config

            receiver_type = (get_station_config(sid) or {}).get("receiver_type")
        except Exception:  # noqa: BLE001
            pass

    end = date.today() - timedelta(days=1)  # yesterday
    start = end - timedelta(days=lookback_days - 1)

    report = LongTermGapReport(
        sid=sid,
        session=session,
        start=start,
        end=end,
        last_archived=last_archived,
        receiver_horizon=horizon,
    )
    if start > end:
        return report  # fully up to date

    # Download candidates: not in archive AND not known-missing-on-receiver.
    # skip_missing_on_receiver=True excludes confirmed absences so we don't
    # waste a slot re-probing files the receiver has aged out.
    # A day needs downloading only if BOTH raw and rinex are absent: rinex-on-disk
    # (even when raw was pruned from the local ring-buffer) means the data product
    # exists, so it is not a real gap. find_gaps checks the filesystem per session,
    # so this is robust to file_tracking's rinex rows being incomplete.
    try:
        with GapDetector() as det:
            raw_gaps = det.find_gaps(
                sid,
                session,
                start,
                end,
                receiver_type=receiver_type,
                sync_first=False,
                skip_missing_on_receiver=True,
            )
            rinex_gaps = det.find_gaps(
                sid,
                f"{session}_rinex",
                start,
                end,
                receiver_type=receiver_type,
                sync_first=False,
                skip_missing_on_receiver=False,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("find_gaps %s/%s: %s", sid, session, e)
        raw_gaps, rinex_gaps = [], []

    term, prov = _absence_counts(sid, session, start, end)
    report.confirmed_gone = term
    report.provisional_absent = prov

    rinex_missing = {g.file_date for g in rinex_gaps}
    horizon_floor = horizon or date.min
    report.queued = [
        g
        for g in raw_gaps
        if g.file_date in rinex_missing and g.file_date >= horizon_floor
    ]

    return report


def format_report(report: LongTermGapReport, max_queued: int = 12) -> str:
    """Human-readable rendering of a :class:`LongTermGapReport`."""
    lines = [report.summary()]
    if report.queued:
        lines.append(f"  queued (download candidates), first {max_queued}:")
        for g in report.queued[:max_queued]:
            hr = "" if g.file_hour is None else f" {g.file_hour:02d}h"
            lines.append(f"    {g.file_date}{hr}  ({g.reason})")
        rest = len(report.queued) - max_queued
        if rest > 0:
            lines.append(f"    … and {rest} more")
    else:
        lines.append("  queued: (none — nothing recoverable in window)")
    return "\n".join(lines)


def run_long_term_backfill_station(
    sid: str,
    session: str,
    lookback_days: int = 365,
    run_rinex: bool = True,
    dry_run: bool = False,
    max_days: Optional[int] = None,
    receiver_type: Optional[str] = None,
) -> LongTermGapReport:
    """Recover one station's classified gaps through the full per-day pipeline.

    Classify via :func:`query_long_term_gaps` (skips ``confirmed_gone``), then for
    each ``queued`` day run download → RINEX → archive → DB via the shared
    per-day backfill primitive. The download client records a receiver-absence
    on a verified ``not_found`` (never on a transient unreachable), so days the
    receiver has aged out get confirmed rather than re-attempted forever.

    Gateway push (archive_sync / EPOS) is deliberately downstream — see
    ``docs/design/long-term-backfill.md`` §6 — not in this per-day call.

    Args:
        sid, session, lookback_days, receiver_type: as for :func:`query_long_term_gaps`.
        run_rinex: run RINEX conversion after download (default True).
        dry_run: classify only, no downloads.
        max_days: cap how many queued days to process this run (throttle).
    """
    report = query_long_term_gaps(
        sid, session, lookback_days, receiver_type=receiver_type
    )
    n = len(report.queued)
    if n == 0:
        logger.info(
            "Long-term backfill %s/%s: nothing queued — %s",
            sid,
            session,
            report.summary(),
        )
        return report

    queued = report.queued if not max_days else report.queued[:max_days]
    logger.info(
        "Long-term backfill %s/%s: %d/%d day(s) to recover%s",
        sid,
        session,
        len(queued),
        n,
        " [DRY RUN]" if dry_run else "",
    )
    if dry_run:
        return report

    from .backfill import _backfill_station_day_generic

    recovered = failed = 0
    for gap in queued:
        try:
            _backfill_station_day_generic(
                sid,
                gap.file_date,
                gap.file_date,  # backfill_end = this day → marks the day complete
                session,
                immediate_archive=True,
                run_rinex=run_rinex,
            )
            recovered += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning(
                "Long-term backfill %s/%s/%s failed: %s", sid, session, gap.file_date, e
            )
    logger.info(
        "Long-term backfill %s/%s done: recovered=%d failed=%d",
        sid,
        session,
        recovered,
        failed,
    )
    return report


def _load_monitor_overloaded() -> bool:
    """True if the system load monitor says RT is under pressure (yield signal).

    Lazy-imports the module-level monitor from bulk_scheduler; any failure is
    treated as "not overloaded" so a missing monitor never blocks recovery.
    """
    try:
        from .bulk_scheduler import _load_monitor

        if _load_monitor is None:
            return False
        load = _load_monitor.get_load()
        return load.cpu_load_1m > _load_monitor.max_cpu_load
    except Exception:  # noqa: BLE001
        return False


def _run_long_term_backfill_job(
    sessions=None,
    lookback_days: int = 365,
    max_workers: int = 2,
    max_days_per_station: Optional[int] = None,
    run_rinex: bool = True,
) -> None:
    """APScheduler **daily backstop**: classify every active station, recover gaps.

    Throttled (low ``max_workers``) and yields to RT via :func:`_load_monitor_overloaded`.
    The worker is idempotent (sync-skips present files), so a daily run is safe.
    This is the backstop; the reconnection trigger is the primary path.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ..cli.main import get_all_station_configs

    sessions = sessions or ["15s_24hr", "1Hz_1hr"]
    active = [
        sid
        for sid, cfg in get_all_station_configs().items()
        if cfg.get("enabled", True)
        and cfg.get("station_status") not in ("discontinued", "inactive")
        and cfg.get("health_check") != "passive"
    ]
    logger.info(
        "Long-term backfill(daily): %d stations, sessions=%s, lookback=%dd",
        len(active),
        sessions,
        lookback_days,
    )

    def _one(sid: str, session: str):
        if _load_monitor_overloaded():
            logger.info("Long-term backfill: load high — deferring %s/%s", sid, session)
            return None
        try:
            report = run_long_term_backfill_station(
                sid,
                session,
                lookback_days=lookback_days,
                run_rinex=run_rinex,
                max_days=max_days_per_station,
            )
            return len(report.queued)
        except Exception as e:  # noqa: BLE001
            logger.warning("Long-term backfill %s/%s: %s", sid, session, e)
            return None

    tasks = [(sid, s) for sid in active for s in sessions]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = [
            f.result()
            for f in as_completed(ex.submit(_one, sid, s) for sid, s in tasks)
        ]
    logger.info(
        "Long-term backfill(daily) done: %d station/session had queued gaps",
        sum(1 for r in results if r),
    )


def _run_reconnection_backfill_job(
    min_outage_days: int = 3,
    lookback_days: int = 365,
    run_rinex: bool = True,
    max_days_per_station: Optional[int] = None,
    reconnection_window_minutes: int = 20,
) -> None:
    """APScheduler **reconnection trigger**: recover stations that just came online.

    Queries ``station_connectivity`` for stations whose ``state_since`` is in the
    last window (recently reconnected); for each with a real outage (a queued gap
    older than ``min_outage_days``), runs the worker. This is the primary trigger;
    the daily job is the backstop. Yields to RT between stations.
    """
    from datetime import datetime, timedelta, timezone

    from ..health.database_factory import DatabaseConnectionFactory

    since = datetime.now(UTC) - timedelta(minutes=reconnection_window_minutes)
    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT sid FROM station_connectivity "
                "WHERE is_online = TRUE AND state_since > %s ORDER BY sid",
                (since,),
            )
            candidates = [r[0] for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("reconnection_backfill: connectivity query failed: %s", e)
        return

    if not candidates:
        return
    logger.info(
        "Long-term backfill(reconnect): %d station(s) came online recently: %s",
        len(candidates),
        ",".join(candidates),
    )

    floor = date.today() - timedelta(days=min_outage_days)
    for sid in candidates:
        if _load_monitor_overloaded():
            logger.info(
                "Long-term backfill(reconnect): load high — deferring remaining"
            )
            break
        try:
            report = query_long_term_gaps(sid, "15s_24hr", lookback_days=lookback_days)
            # only act on a real outage: a queued day older than min_outage_days,
            # so a brief flap doesn't trigger a heavy multi-month recovery.
            if report.queued and any(g.file_date <= floor for g in report.queued):
                run_long_term_backfill_station(
                    sid,
                    "15s_24hr",
                    lookback_days=lookback_days,
                    run_rinex=run_rinex,
                    max_days=max_days_per_station,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Long-term backfill(reconnect) %s: %s", sid, e)
