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
from datetime import date, timedelta
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
        with DatabaseConnectionFactory.connection() as conn, conn.cursor() as cur:
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
        with DatabaseConnectionFactory.connection() as conn, conn.cursor() as cur:
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
        with DatabaseConnectionFactory.connection() as conn, conn.cursor() as cur:
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
        with DatabaseConnectionFactory.connection() as conn, conn.cursor() as cur:
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
                sid, session, start, end,
                receiver_type=receiver_type, sync_first=False,
                skip_missing_on_receiver=True,
            )
            rinex_gaps = det.find_gaps(
                sid, f"{session}_rinex", start, end,
                receiver_type=receiver_type, sync_first=False,
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
        g for g in raw_gaps
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
