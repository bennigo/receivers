"""Icinga/Nagios check for long-term archive-sync health.

The durable, server-side "watch it while I'm away" alert for the rek-d01 →
rawdata (ananas) archive pipeline. Runs on rek-d01 (which has local DB + log
access); a systemd timer invokes it periodically and pushes a passive check
result to Icinga, the IMO's existing alerting backbone — so notification reaches
operators through Icinga's configured channels without an MTA on the host.

Signals evaluated (all queryable from gps_health — no log scraping):

  1. **Sync freshness** — ``sync_state.last_success_ts`` age vs ``max_age_minutes``
     (the same threshold archive_sync uses). WARN past it, CRIT at 2x. This is the
     headline: it catches the archive sync silently stopping for ANY reason —
     scheduler down, rsync/ssh broken (the rc=255 class), watermark stuck.
  2. **Missing 15s dailies** — count of ``15s_24hr`` files still ``missing`` for
     yesterday (the GAMIT daily input). Count thresholds, NOT a station allowlist
     (which drifts): a handful is normal (known-bad + transient), a spike is real.

Corruption is deliberately NOT evaluated here: there is no DB signal for it (the
read-back verify logs ``ARCHIVE CORRUPT`` but does not persist a flag), and log
scraping is fragile. Corruption rides the verify pass's own logging until a
findings table exists (see todo: persist verify findings).

The check is STATELESS — Icinga owns renotification/dedup and (via the pushed
``ttl``) staleness detection, so if this timer or the host stops pushing, Icinga
flags the service stale and alerts. Do not add custom re-alert throttling here.

Also usable as a plain Nagios plugin (exit 0/1/2/3 + ``output | perfdata``), so
Icinga can call it directly via check_by_ssh as an alternative to the push model.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ..db.tx import read_only_cursor

logger = logging.getLogger("receivers.monitoring.archive_sync_check")

NAGIOS_OK = 0
NAGIOS_WARNING = 1
NAGIOS_CRITICAL = 2
NAGIOS_UNKNOWN = 3

_LABEL = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}


@dataclass
class ArchiveSyncResult:
    """Outcome of an archive-sync health evaluation."""

    exit_status: int
    summary: str  # one-line reason(s), worst-first
    perfdata: str  # Nagios performance data
    reasons: List[str] = field(default_factory=list)

    @property
    def plugin_output(self) -> str:
        return f"{_LABEL[self.exit_status]} - {self.summary}"


def evaluate_archive_sync(
    conn,
    *,
    target: str = "imo_archive",
    max_age_minutes: int = 120,
    missing_15s_warn: int = 5,
    missing_15s_crit: int = 15,
    now: Optional[datetime] = None,
) -> ArchiveSyncResult:
    """Evaluate archive-sync health from gps_health. Pure; never raises on data.

    Args:
        conn: gps_health DB connection.
        target: sync_state target name (the archive gateway).
        max_age_minutes: sync is WARN past this age, CRIT at 2x (matches the
            archive_sync ``max_age_minutes`` freshness threshold).
        missing_15s_warn / missing_15s_crit: count thresholds for yesterday's
            missing 15s_24hr dailies.
        now: evaluation time (defaults to ``datetime.now()``; the server runs UTC).

    Returns:
        ArchiveSyncResult (worst-of all signals).
    """
    from ..archive.state import get_last_success

    now = now or datetime.now()
    statuses: List[int] = []
    reasons: List[str] = []

    # --- 1. sync freshness ---------------------------------------------------
    last_success = get_last_success(conn, target)
    if last_success is None:
        age_min = None
        statuses.append(NAGIOS_CRITICAL)
        reasons.append(f"no successful '{target}' sync recorded")
    else:
        age_min = (now - last_success).total_seconds() / 60.0
        if age_min > 2 * max_age_minutes:
            statuses.append(NAGIOS_CRITICAL)
            reasons.append(
                f"archive sync stale {age_min:.0f}m "
                f"(>{2 * max_age_minutes}m; last {last_success:%Y-%m-%d %H:%M})"
            )
        elif age_min > max_age_minutes:
            statuses.append(NAGIOS_WARNING)
            reasons.append(f"archive sync aging {age_min:.0f}m (>{max_age_minutes}m)")
        else:
            statuses.append(NAGIOS_OK)

    # --- 2. missing 15s dailies (yesterday) ----------------------------------
    missing_15s = _count_missing_15s_yesterday(conn)
    if missing_15s is None:
        statuses.append(NAGIOS_UNKNOWN)
        reasons.append("could not query missing 15s_24hr count")
    elif missing_15s >= missing_15s_crit:
        statuses.append(NAGIOS_CRITICAL)
        reasons.append(f"{missing_15s} 15s_24hr dailies missing for yesterday")
    elif missing_15s >= missing_15s_warn:
        statuses.append(NAGIOS_WARNING)
        reasons.append(f"{missing_15s} 15s_24hr dailies missing for yesterday")
    else:
        statuses.append(NAGIOS_OK)

    exit_status = max(statuses) if statuses else NAGIOS_UNKNOWN

    age_perf = f"{age_min:.0f}" if age_min is not None else "U"
    missing_perf = str(missing_15s) if missing_15s is not None else "U"
    perfdata = (
        f"sync_age_min={age_perf};{max_age_minutes};{2 * max_age_minutes};0 "
        f"missing_15s={missing_perf};{missing_15s_warn};{missing_15s_crit};0"
    )

    if exit_status == NAGIOS_OK:
        fresh = f"{age_min:.0f}m" if age_min is not None else "?"
        summary = f"archive sync fresh ({fresh}), {missing_15s} 15s missing"
    else:
        summary = "; ".join(reasons)

    return ArchiveSyncResult(
        exit_status=exit_status, summary=summary, perfdata=perfdata, reasons=reasons
    )


def _count_missing_15s_yesterday(conn) -> Optional[int]:
    """Count 15s_24hr files still 'missing' for yesterday (UTC). None on error."""
    try:
        with read_only_cursor(conn) as cur:
            cur.execute(
                """SELECT count(*) FROM file_tracking
                   WHERE session_type = '15s_24hr'
                     AND file_date = CURRENT_DATE - 1
                     AND status = 'missing'"""
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # query/DB error — surface as UNKNOWN, don't crash
        logger.warning("missing-15s query failed: %s", exc)
        return None


def push_to_icinga(
    result: ArchiveSyncResult,
    *,
    icinga_host: str,
    check_name: str = "Archive sync",
    ttl: Optional[int] = None,
) -> bool:
    """Push the result to Icinga as a passive check. Returns True on success.

    ``ttl`` (seconds) makes Icinga mark the service stale if no fresh result
    arrives in time — that is the host/timer-down detector. Best-effort.
    """
    try:
        from .icinga_client import CheckResult, IcingaClient

        check = CheckResult(
            station=icinga_host,
            check_name=check_name,
            exit_status=result.exit_status,
            plugin_output=result.plugin_output,
            performance_data=result.perfdata,
            ttl=ttl,
        )
        resp = IcingaClient().send_check_result(check)
        ok = bool(resp.get("success")) if isinstance(resp, dict) else bool(resp)
        if not ok:
            logger.warning("Icinga push did not succeed: %s", resp)
        return ok
    except Exception as exc:
        logger.warning("Icinga push failed: %s", exc)
        return False


def main() -> None:
    """Nagios-plugin entry: print ``output | perfdata`` and exit 0/1/2/3.

    With --icinga, also push the result as a passive check. Intended to be run by
    the gps-archive-sync-alert systemd timer on rek-d01.
    """
    parser = argparse.ArgumentParser(
        description="Icinga/Nagios check for rek-d01 → rawdata archive-sync health"
    )
    parser.add_argument("--target", default="imo_archive", help="sync_state target")
    parser.add_argument("--max-age-minutes", type=int, default=120)
    parser.add_argument("--missing-15s-warn", type=int, default=5)
    parser.add_argument("--missing-15s-crit", type=int, default=15)
    parser.add_argument(
        "--icinga", action="store_true", help="also push a passive result to Icinga"
    )
    parser.add_argument(
        "--icinga-host",
        default="rek-d01",
        help="Icinga host object for the pushed service (default: rek-d01)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="Icinga staleness TTL in seconds (recommend ~3x the timer interval)",
    )
    args = parser.parse_args()

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            result = evaluate_archive_sync(
                conn,
                target=args.target,
                max_age_minutes=args.max_age_minutes,
                missing_15s_warn=args.missing_15s_warn,
                missing_15s_crit=args.missing_15s_crit,
            )
            if args.icinga:
                push_to_icinga(result, icinga_host=args.icinga_host, ttl=args.ttl)
    except Exception as exc:
        print(f"UNKNOWN - archive-sync check failed: {exc}")
        sys.exit(NAGIOS_UNKNOWN)

    print(f"{result.plugin_output} | {result.perfdata}")
    sys.exit(result.exit_status)


if __name__ == "__main__":
    main()
