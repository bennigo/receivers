"""Icinga/Nagios check for health-monitoring liveness.

The detector for the failure mode that has no other signal: **the health
executor starving silently**.

On 2026-08-10 fleet monitoring went blind for 5.5 h and nothing alerted.
The 5-minute health job last ran at 16:21; downloads, archiving and RINEX
conversion continued normally the whole time, so every other indicator
stayed green. Grafana showed ``Online 0 / Offline 0`` against 181 stations
in the table. The APScheduler job store had all 857 jobs registered and
due in +0.4 min — the scheduler kept *submitting* health jobs and the
``health`` ThreadPoolExecutor never *ran* them. A ThreadPoolExecutor
**queues** rather than rejects, so pool exhaustion produces no error, no
warning, and no APScheduler "max instances" message. Pure silence.

It was found only because bgo looked at a dashboard and said "something is
not right". This check is what should have found it.

Two signals, because one cannot tell the two failures apart:

  1. **Freshness** — age of the newest ``block_ping_status`` row. This is
     the headline and it is what catches the incident. It works because the
     writer records unreachable stations too (``is_online = false`` rows
     exist for 101 stations in the dev DB), so rows keep arriving even when
     the whole fleet is down. Absence of rows therefore means the JOB
     stopped, not that the network did.
  2. **Coverage** — distinct stations reporting in the recent window. A
     job that runs but only manages a handful of stations before the pool
     stalls is still a starving job, and freshness alone would call it OK.

Threshold domain, stated because getting it wrong is a known trap (a
previous check compared a discrete hourly file LABEL to a sliding clock
window and reported 167 amber / 0 green on a healthy pipeline): both
signals here compare a row TIMESTAMP to wall-clock ``now()`` in the same
tz-aware domain, and the default 30-minute threshold is a deliberately
wide margin over the job's 5-minute cadence.

The check is STATELESS — Icinga owns renotification/dedup and (via the
pushed ``ttl``) staleness detection, so if this timer or the host stops
pushing, Icinga flags the service stale and alerts. Do not add custom
re-alert throttling here.

Also usable as a plain Nagios plugin (exit 0/1/2/3 + ``output | perfdata``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

NAGIOS_OK = 0
NAGIOS_WARNING = 1
NAGIOS_CRITICAL = 2
NAGIOS_UNKNOWN = 3

_LABEL = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}


@dataclass
class HealthFreshnessResult:
    """Worst-of across the evaluated signals, plus Nagios-shaped output."""

    exit_status: int
    summary: str
    perfdata: str = ""
    reasons: List[str] = field(default_factory=list)

    @property
    def plugin_output(self) -> str:
        return f"{_LABEL[self.exit_status]} - {self.summary}"


def _newest_ping(conn) -> Optional[datetime]:
    """Timestamp of the newest ``block_ping_status`` row, or None if empty.

    Single-table aggregate on purpose. Joining two ``block_*_status`` tables
    without a ``ts`` predicate on every alias is the documented cartesian
    footgun that took pgdev down on 2026-05-27.
    """
    from ..db.tx import read_only_cursor

    with read_only_cursor(conn) as cur:
        cur.execute("SELECT max(ts) FROM block_ping_status")
        row = cur.fetchone()
    return row[0] if row else None


def _stations_reporting_since(conn, since: datetime) -> int:
    """Distinct stations with a ping row at or after ``since``."""
    from ..db.tx import read_only_cursor

    with read_only_cursor(conn) as cur:
        cur.execute(
            "SELECT count(DISTINCT sid) FROM block_ping_status WHERE ts >= %s",
            (since,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def evaluate_health_freshness(
    conn,
    *,
    max_age_minutes: int = 30,
    coverage_window_minutes: int = 30,
    min_stations_warn: int = 100,
    min_stations_crit: int = 20,
    now: Optional[datetime] = None,
) -> HealthFreshnessResult:
    """Evaluate health-monitoring liveness from gps_health. Never raises on data.

    Args:
        conn: gps_health DB connection.
        max_age_minutes: health data is WARN past this age, CRIT at 2x. The
            job runs every 5 minutes, so 30 is a wide margin — the incident
            it exists to catch was 5.5 h stale.
        coverage_window_minutes: window for the stations-reporting count.
        min_stations_warn / min_stations_crit: fleet is ~176 stations; far
            fewer reporting means the pool stalled partway.
        now: evaluation time (defaults to ``datetime.now(timezone.utc)``).

    Returns:
        HealthFreshnessResult (worst-of all signals).
    """
    now = now or datetime.now(UTC)
    statuses: List[int] = []
    reasons: List[str] = []
    age_min: Optional[float] = None
    stations = 0

    # --- 1. freshness: is the health job running at all? ---------------------
    try:
        newest = _newest_ping(conn)
    except Exception as exc:  # query/DB error — UNKNOWN, never crash the timer
        logger.warning("health freshness query failed: %s", exc)
        return HealthFreshnessResult(
            exit_status=NAGIOS_UNKNOWN,
            summary=f"cannot read block_ping_status: {exc}",
            reasons=[str(exc)],
        )

    if newest is None:
        statuses.append(NAGIOS_CRITICAL)
        reasons.append("block_ping_status is empty — health has never written")
    else:
        if newest.tzinfo is None:  # compare in ONE domain, never mixed
            newest = newest.replace(tzinfo=UTC)
        age_min = (now - newest).total_seconds() / 60.0
        if age_min >= max_age_minutes * 2:
            statuses.append(NAGIOS_CRITICAL)
            reasons.append(
                f"health data {age_min:.0f} min old "
                f"(>= {max_age_minutes * 2} min) — the health job has stopped"
            )
        elif age_min >= max_age_minutes:
            statuses.append(NAGIOS_WARNING)
            reasons.append(
                f"health data {age_min:.0f} min old (>= {max_age_minutes} min)"
            )
        else:
            statuses.append(NAGIOS_OK)

    # --- 2. coverage: is it running for the whole fleet? ---------------------
    # A pool that stalls partway still writes SOME rows, so freshness alone
    # would call it OK. Skipped when the data is already stale — one finding
    # per fault, not two.
    if newest is not None and (age_min or 0) < max_age_minutes:
        try:
            stations = _stations_reporting_since(
                conn, now - timedelta(minutes=coverage_window_minutes)
            )
        except Exception as exc:
            logger.warning("health coverage query failed: %s", exc)
            statuses.append(NAGIOS_UNKNOWN)
            reasons.append(f"coverage query failed: {exc}")
        else:
            if stations < min_stations_crit:
                statuses.append(NAGIOS_CRITICAL)
                reasons.append(
                    f"only {stations} station(s) reported in "
                    f"{coverage_window_minutes} min (< {min_stations_crit})"
                )
            elif stations < min_stations_warn:
                statuses.append(NAGIOS_WARNING)
                reasons.append(
                    f"only {stations} station(s) reported in "
                    f"{coverage_window_minutes} min (< {min_stations_warn})"
                )
            else:
                statuses.append(NAGIOS_OK)

    exit_status = max(statuses) if statuses else NAGIOS_UNKNOWN
    age_txt = f"{age_min:.0f} min" if age_min is not None else "n/a"
    if exit_status == NAGIOS_OK:
        summary = f"health data {age_txt} old, {stations} station(s) reporting"
    else:
        summary = "; ".join(reasons)

    perfdata = (
        f"age_minutes={age_min:.0f};{max_age_minutes};{max_age_minutes * 2};0 "
        f"stations_reporting={stations};{min_stations_warn};{min_stations_crit};0"
        if age_min is not None
        else f"stations_reporting={stations};{min_stations_warn};{min_stations_crit};0"
    )
    return HealthFreshnessResult(
        exit_status=exit_status, summary=summary, perfdata=perfdata, reasons=reasons
    )


def push_to_icinga(
    result: HealthFreshnessResult,
    *,
    icinga_host: str,
    check_name: str = "Health monitoring",
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
    """Nagios-plugin entry: print ``output | perfdata`` and exit 0/1/2/3."""
    parser = argparse.ArgumentParser(
        description="Check that fleet health monitoring is actually running."
    )
    parser.add_argument("--max-age-minutes", type=int, default=30)
    parser.add_argument("--coverage-window-minutes", type=int, default=30)
    parser.add_argument("--min-stations-warn", type=int, default=100)
    parser.add_argument("--min-stations-crit", type=int, default=20)
    parser.add_argument(
        "--icinga", action="store_true", help="Push the result to Icinga."
    )
    parser.add_argument("--icinga-host", default="rek-d01")
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="Icinga staleness TTL in seconds (detects this timer dying).",
    )
    args = parser.parse_args()

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        # single_host: this is a local liveness probe, not a mirrored write.
        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            result = evaluate_health_freshness(
                conn,
                max_age_minutes=args.max_age_minutes,
                coverage_window_minutes=args.coverage_window_minutes,
                min_stations_warn=args.min_stations_warn,
                min_stations_crit=args.min_stations_crit,
            )
            if args.icinga:
                push_to_icinga(result, icinga_host=args.icinga_host, ttl=args.ttl)
    except Exception as exc:  # noqa: BLE001 — a plugin must always exit cleanly
        print(f"UNKNOWN - health freshness check failed: {exc}")
        sys.exit(NAGIOS_UNKNOWN)

    print(f"{result.plugin_output} | {result.perfdata}")
    sys.exit(result.exit_status)


if __name__ == "__main__":
    main()
