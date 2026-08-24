"""Icinga/Nagios check for long-lived open transactions.

The runtime half of #143. ``dev/audits/tx_audit.py`` models "the transaction
is never ended" and is green — but it is blind by construction to the class
that actually hurt production: a function that DOES commit, eventually,
while interleaving DB statements with slow non-DB work. The audit
classifies those as writes and skips them, correctly by its own model.

Two were found by hand and fixed (``sync_archive_to_db`` 373 s,
``verify_archive_catalog`` 371 s). Nothing would have told us about a
third. This check is that telling.

Why it matters, concretely — an open transaction:

  * pins the vacuum ``xmin`` horizon, so dead tuples cannot be reclaimed
    and the table bloats;
  * blocks ``CREATE``/``DROP INDEX CONCURRENTLY``, which is what killed
    migration 065 **six times**;
  * holds a connection slot, on a host where exhaustion has been chronic
    since 2026-08-05 (``max_connections=100``).

Two signals:

  1. **Idle-in-transaction age** — the headline. A session in
     ``idle in transaction`` is doing NOTHING while holding all of the
     above. Any age beyond a few minutes is a bug, not load.
  2. **Oldest transaction age**, whatever its state. This is the actual
     xmin-horizon proxy: a genuinely long-running *active* query pins it
     just as hard, and would otherwise go unreported. Thresholded far
     more loosely, since a slow query is a performance question rather
     than a leak.

Visibility note: as a non-superuser Postgres shows full detail only for
your own role's sessions. That is the interesting set anyway — this exists
to catch *our* code leaking — but it means the check cannot see another
role's leak, and should not be read as a whole-cluster guarantee.

The check is STATELESS — Icinga owns renotification/dedup and (via the
pushed ``ttl``) staleness detection. Also usable as a plain Nagios plugin
(exit 0/1/2/3 + ``output | perfdata``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

NAGIOS_OK = 0
NAGIOS_WARNING = 1
NAGIOS_CRITICAL = 2
NAGIOS_UNKNOWN = 3

_LABEL = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}

#: States that hold a transaction open while doing no work at all.
IDLE_STATES = ("idle in transaction", "idle in transaction (aborted)")


@dataclass
class TxAgeResult:
    """Worst-of across the evaluated signals, plus Nagios-shaped output."""

    exit_status: int
    summary: str
    perfdata: str = ""
    reasons: List[str] = field(default_factory=list)

    @property
    def plugin_output(self) -> str:
        return f"{_LABEL[self.exit_status]} - {self.summary}"


def _worst_idle_in_transaction(conn):
    """``(age_seconds, pid, application_name, query)`` of the worst offender.

    Excludes this backend via ``pg_backend_pid()`` — the check's own read
    would otherwise be a candidate, which is both wrong and embarrassing.
    """
    from ..db.tx import read_only_cursor

    with read_only_cursor(conn) as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - xact_start)),
                   pid, coalesce(application_name, ''), coalesce(query, '')
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND pid <> pg_backend_pid()
               AND state = ANY(%s)
               AND xact_start IS NOT NULL
             ORDER BY xact_start ASC
             LIMIT 1
            """,
            (list(IDLE_STATES),),
        )
        return cur.fetchone()


def _oldest_transaction_age(conn) -> Optional[float]:
    """Age in seconds of the oldest open transaction in ANY state.

    The xmin-horizon proxy: a long-running active query pins vacuum just as
    hard as an idle-in-transaction one.
    """
    from ..db.tx import read_only_cursor

    with read_only_cursor(conn) as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - min(xact_start)))
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND pid <> pg_backend_pid()
               AND xact_start IS NOT NULL
            """
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def evaluate_tx_age(
    conn,
    *,
    idle_warn_seconds: int = 120,
    idle_crit_seconds: int = 600,
    oldest_warn_seconds: int = 900,
    oldest_crit_seconds: int = 3600,
) -> TxAgeResult:
    """Evaluate open-transaction age. Never raises on data.

    Args:
        conn: gps_health DB connection.
        idle_warn_seconds / idle_crit_seconds: an ``idle in transaction``
            session is doing nothing while pinning vacuum and a connection
            slot, so these are deliberately tight. The two fixed offenders
            sat at 373 s and 371 s — both would have been CRIT-adjacent and
            well past WARN.
        oldest_warn_seconds / oldest_crit_seconds: any-state transaction
            age. Much looser: a slow query is a performance question, not a
            leak, but it pins the same horizon so it must still surface.

    Returns:
        TxAgeResult (worst-of all signals).
    """
    statuses: List[int] = []
    reasons: List[str] = []
    idle_age = 0.0
    oldest_age = 0.0

    try:
        worst = _worst_idle_in_transaction(conn)
        oldest = _oldest_transaction_age(conn)
    except Exception as exc:  # query/DB error — UNKNOWN, never crash the timer
        logger.warning("tx age query failed: %s", exc)
        return TxAgeResult(
            exit_status=NAGIOS_UNKNOWN,
            summary=f"cannot read pg_stat_activity: {exc}",
            reasons=[str(exc)],
        )

    # --- 1. idle in transaction — the leak signal ---------------------------
    if worst is not None:
        idle_age = float(worst[0] or 0.0)
        pid, app, query = worst[1], worst[2], (worst[3] or "").strip()
        who = f"pid {pid}" + (f" ({app})" if app else "")
        snippet = " ".join(query.split())[:90]
        if idle_age >= idle_crit_seconds:
            statuses.append(NAGIOS_CRITICAL)
        elif idle_age >= idle_warn_seconds:
            statuses.append(NAGIOS_WARNING)
        else:
            statuses.append(NAGIOS_OK)
        if statuses[-1] != NAGIOS_OK:
            reasons.append(
                f"{who} idle in transaction {idle_age:.0f}s — pins the vacuum "
                f"xmin horizon and blocks CREATE INDEX CONCURRENTLY"
                + (f"; last query: {snippet}" if snippet else "")
            )
    else:
        statuses.append(NAGIOS_OK)

    # --- 2. oldest transaction, any state — the xmin proxy ------------------
    if oldest is not None:
        oldest_age = oldest
        if oldest_age >= oldest_crit_seconds:
            statuses.append(NAGIOS_CRITICAL)
            reasons.append(
                f"oldest open transaction {oldest_age:.0f}s (>= {oldest_crit_seconds}s)"
            )
        elif oldest_age >= oldest_warn_seconds:
            statuses.append(NAGIOS_WARNING)
            reasons.append(
                f"oldest open transaction {oldest_age:.0f}s (>= {oldest_warn_seconds}s)"
            )
        else:
            statuses.append(NAGIOS_OK)

    exit_status = max(statuses) if statuses else NAGIOS_UNKNOWN
    if exit_status == NAGIOS_OK:
        summary = (
            f"no long transactions (worst idle-in-transaction {idle_age:.0f}s, "
            f"oldest {oldest_age:.0f}s)"
        )
    else:
        summary = "; ".join(reasons)

    perfdata = (
        f"idle_in_transaction_seconds={idle_age:.0f};"
        f"{idle_warn_seconds};{idle_crit_seconds};0 "
        f"oldest_transaction_seconds={oldest_age:.0f};"
        f"{oldest_warn_seconds};{oldest_crit_seconds};0"
    )
    return TxAgeResult(
        exit_status=exit_status, summary=summary, perfdata=perfdata, reasons=reasons
    )


def push_to_icinga(
    result: TxAgeResult,
    *,
    icinga_host: str,
    check_name: str = "Transaction age",
    ttl: Optional[int] = None,
) -> bool:
    """Push the result to Icinga as a passive check. Returns True on success."""
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
        description="Check for transactions left open against gps_health."
    )
    parser.add_argument("--idle-warn-seconds", type=int, default=120)
    parser.add_argument("--idle-crit-seconds", type=int, default=600)
    parser.add_argument("--oldest-warn-seconds", type=int, default=900)
    parser.add_argument("--oldest-crit-seconds", type=int, default=3600)
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

        # single_host: a local probe of THIS server's sessions, not a mirror.
        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            result = evaluate_tx_age(
                conn,
                idle_warn_seconds=args.idle_warn_seconds,
                idle_crit_seconds=args.idle_crit_seconds,
                oldest_warn_seconds=args.oldest_warn_seconds,
                oldest_crit_seconds=args.oldest_crit_seconds,
            )
            if args.icinga:
                push_to_icinga(result, icinga_host=args.icinga_host, ttl=args.ttl)
    except Exception as exc:  # noqa: BLE001 — a plugin must always exit cleanly
        print(f"UNKNOWN - transaction age check failed: {exc}")
        sys.exit(NAGIOS_UNKNOWN)

    print(f"{result.plugin_output} | {result.perfdata}")
    sys.exit(result.exit_status)


if __name__ == "__main__":
    main()
