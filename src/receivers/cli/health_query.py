"""receivers health-query — safe psql equivalent for the gps_health DB.

Python implementation of the ``gps-health-q`` shell wrapper. Defends against the
2026-05-27 cartesian-join incident by EXPLAIN-ing first, parsing the maximum
row estimate across **all** plan nodes, and refusing to execute when the plan
exceeds configurable ceilings. Also sets ``statement_timeout=60s`` and
``lock_timeout=5s`` on the session as a belt-and-suspenders complement to the
server-side ``ALTER ROLE`` limits.

Usage::

    receivers health-query "SELECT count(*) FROM stations"
    receivers health-query -f path/to/query.sql
    receivers health-query --explain-only "SELECT ..."
    receivers health-query --no-explain "SELECT ..."
    receivers health-query --max-rows 1e9 "SELECT ..."
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("receivers.cli.health_query")

DEFAULT_MAX_ROWS = 100_000_000  # 1e8 — see gps-health-q + cartesian-incident note
DEFAULT_MAX_COST = 1_000_000_000  # 1e9 — fallback safety net
STATEMENT_TIMEOUT = "60s"
LOCK_TIMEOUT = "5s"

_ROWS_RE = re.compile(r"rows=(\d+)")
_COST_RE = re.compile(r"cost=[\d.]+\.\.([\d.]+)")

# Strip string literals, dollar-quoted bodies and comments before looking for a
# statement separator, so a `;` *inside* a literal doesn't trip the check.
_SQL_LITERALS_RE = re.compile(
    r"'(?:[^']|'')*'"  # single-quoted string (with '' escape)
    r'|"(?:[^"]|"")*"'  # quoted identifier
    r"|\$(\w*)\$.*?\$\1\$"  # dollar-quoted body
    r"|--[^\n]*"  # line comment
    r"|/\*.*?\*/",  # block comment
    re.DOTALL,
)


def strip_sql_noise(sql: str) -> str:
    """Blank out literals/comments so structural checks see only SQL syntax."""
    return _SQL_LITERALS_RE.sub(" ", sql)


def is_multi_statement(sql: str) -> bool:
    """True if ``sql`` contains more than one statement.

    The EXPLAIN gate only ever plans the FIRST statement, so
    ``SELECT 1; DELETE FROM stations`` used to sail through the gate and then
    execute both — psycopg2 happily runs a multi-statement string. Reject it
    outright; a caller with a genuine batch should use psql deliberately.
    """
    return ";" in strip_sql_noise(sql).rstrip().rstrip(";").rstrip()


def parse_explain_estimates(explain_text: str) -> tuple[int, float]:
    """Return ``(max_rows_across_all_nodes, top_of_plan_cost)``.

    The cartesian-detection signal is the maximum ``rows=`` value across every
    plan node — not the top aggregate's row count, which is typically 1 for
    COUNT/AVG queries even when the inner scan is enormous. The 2026-05-27
    incident's join node showed rows≈2.7×10¹⁰ while the top aggregate showed
    rows=1.

    Raises ``ValueError`` if the EXPLAIN output cannot be parsed.
    """
    row_estimates = [int(m) for m in _ROWS_RE.findall(explain_text)]
    if not row_estimates:
        raise ValueError("no row estimates found in EXPLAIN output")

    cost_match = _COST_RE.search(explain_text)
    if not cost_match:
        raise ValueError("no cost estimate found in EXPLAIN output")

    return max(row_estimates), float(cost_match.group(1))


def _coerce_ceiling(raw: str | float | int) -> float:
    """Parse argparse ceiling values ('1e8', '100000000', 1e8) to float."""
    try:
        return float(raw)  # accepts '1e8', '1.5e9', '100000000', int, float
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"invalid numeric ceiling: {raw!r}") from e


def _resolve_sql(args: argparse.Namespace) -> str | None:
    """Resolve SQL from ``-f FILE`` or positional args. ``None`` on user error."""
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print(f"receivers health-query: cannot read {path}", file=sys.stderr)
            return None
        return path.read_text()

    if args.sql:
        return " ".join(args.sql)

    print(
        "receivers health-query: no SQL provided. "
        "Pass SQL as positional args or via -f FILE.",
        file=sys.stderr,
    )
    return None


def _print_aligned(columns: list[str], rows: list[tuple]) -> None:
    """Write ``rows`` to stdout in a psql-style aligned table."""
    str_rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for r in str_rows:
        for i, v in enumerate(r):
            if len(v) > widths[i]:
                widths[i] = len(v)

    header = " | ".join(columns[i].ljust(widths[i]) for i in range(len(columns)))
    separator = "-+-".join("-" * w for w in widths)
    print(header)
    print(separator)
    for r in str_rows:
        print(" | ".join(r[i].ljust(widths[i]) for i in range(len(columns))))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def cmd_health_query(args: argparse.Namespace) -> int:
    """Run a query against ``gps_health`` behind the EXPLAIN gate."""
    sql = _resolve_sql(args)
    if sql is None:
        return 2

    sql_trimmed = sql.rstrip().rstrip(";").rstrip()
    if not sql_trimmed:
        print("receivers health-query: empty SQL", file=sys.stderr)
        return 2

    if is_multi_statement(sql_trimmed):
        print(
            "receivers health-query: refusing multi-statement SQL — the EXPLAIN "
            "gate only plans the first statement, so anything after the ';' "
            "would run ungated. Run one statement at a time.",
            file=sys.stderr,
        )
        logger.warning("health-query refused multi-statement SQL")
        return 2

    try:
        max_rows = _coerce_ceiling(args.max_rows)
        max_cost = _coerce_ceiling(args.max_cost)
    except argparse.ArgumentTypeError as e:
        print(f"receivers health-query: {e}", file=sys.stderr)
        return 2

    from ..db.connection import get_connection

    try:
        # single_host: a query tool must never fan its statements out to the
        # pgdev mirror. It also keeps `autocommit`/SET semantics honest — on a
        # dual connection the attribute assignment below used to be swallowed
        # by the wrapper, leaving both timeouts uncommitted and unenforced.
        conn = get_connection(host_override=args.host, single_host=True)
    except Exception as e:
        print(f"receivers health-query: cannot connect: {e}", file=sys.stderr)
        return 1

    # Autocommit lets the session-level SETs apply cleanly without an implicit
    # transaction wrapping every statement.
    conn.autocommit = True

    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            cur.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            if not args.write:
                # Read-only by default: the tool exists to *inspect* gps_health.
                # A typo'd UPDATE/DELETE is refused by the server, not by a
                # regex we would have to keep ahead of. --write opts out.
                cur.execute("SET default_transaction_read_only = on")
                cur.execute("SET transaction_read_only = on")

            if not args.no_explain:
                try:
                    cur.execute(f"EXPLAIN {sql_trimmed}")
                    explain_text = "\n".join(row[0] for row in cur.fetchall())
                except Exception as e:
                    print(
                        f"receivers health-query: EXPLAIN failed: {e}", file=sys.stderr
                    )
                    print("Refusing to execute.", file=sys.stderr)
                    logger.warning("health-query EXPLAIN failed: %s", e)
                    return 1

                try:
                    est_rows, est_cost = parse_explain_estimates(explain_text)
                except ValueError as e:
                    print(explain_text, file=sys.stderr)
                    print(
                        f"receivers health-query: could not parse EXPLAIN ({e}); "
                        "refusing to execute.",
                        file=sys.stderr,
                    )
                    return 1

                if est_rows > max_rows or est_cost > max_cost:
                    print("\n=== EXPLAIN ===", file=sys.stderr)
                    print(explain_text, file=sys.stderr)
                    print("\nREFUSED to execute.", file=sys.stderr)
                    print(
                        f"  estimate  max_rows={est_rows:,}  top_cost={est_cost:,.0f}",
                        file=sys.stderr,
                    )
                    print(
                        f"  ceiling   max_rows={max_rows:,.0f}  top_cost={max_cost:,.0f}",
                        file=sys.stderr,
                    )
                    print(
                        "  Rewrite the query, raise the ceiling with "
                        "--max-rows/--max-cost, or use --no-explain.",
                        file=sys.stderr,
                    )
                    logger.warning(
                        "health-query refused: est_rows=%d est_cost=%.0f "
                        "max_rows=%.0f max_cost=%.0f",
                        est_rows,
                        est_cost,
                        max_rows,
                        max_cost,
                    )
                    return 1

                if args.explain_only:
                    print(explain_text)
                    print(
                        f"\nreceivers health-query: --explain-only — not executing. "
                        f"(max_rows={est_rows:,} top_cost={est_cost:,.0f})",
                        file=sys.stderr,
                    )
                    logger.info(
                        "health-query explain-only: est_rows=%d est_cost=%.0f",
                        est_rows,
                        est_cost,
                    )
                    return 0

                print(
                    f"receivers health-query: EXPLAIN ok "
                    f"(max_rows={est_rows:,} top_cost={est_cost:,.0f}) — executing...",
                    file=sys.stderr,
                )
                logger.info(
                    "health-query passed gate: est_rows=%d est_cost=%.0f",
                    est_rows,
                    est_cost,
                )

            try:
                cur.execute(sql_trimmed)
            except Exception as e:
                print(f"receivers health-query: query failed: {e}", file=sys.stderr)
                logger.warning("health-query execution failed: %s", e)
                return 1

            if cur.description is None:
                # Non-SELECT statement (DDL / DML). cur.rowcount is -1 when
                # unavailable; suppress the number in that case.
                if cur.rowcount >= 0:
                    print(f"{cur.rowcount} row(s) affected", file=sys.stderr)
                else:
                    print("statement executed", file=sys.stderr)
                return 0

            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            _print_aligned(columns, rows)
        return 0

    finally:
        conn.close()


def create_health_query_parser(subparsers) -> argparse.ArgumentParser:
    """Register the ``health-query`` subcommand on ``subparsers``."""
    parser = subparsers.add_parser(
        "health-query",
        help="Safe psql equivalent for gps_health (EXPLAIN-first)",
        description=(
            "Query the gps_health database behind an EXPLAIN gate. Refuses to "
            "execute plans whose maximum row estimate across any plan node "
            f"exceeds {DEFAULT_MAX_ROWS:.0e}, or whose top-of-plan cost "
            f"exceeds {DEFAULT_MAX_COST:.0e}. Sets statement_timeout="
            f"{STATEMENT_TIMEOUT} and lock_timeout={LOCK_TIMEOUT} on the session."
        ),
    )
    parser.add_argument(
        "sql",
        nargs="*",
        help="SQL to execute (can span multiple shell args).",
    )
    parser.add_argument(
        "-f",
        "--file",
        metavar="PATH",
        help="Read SQL from a file instead of the command line.",
    )
    gate = parser.add_mutually_exclusive_group()
    gate.add_argument(
        "--explain-only",
        action="store_true",
        help="Show the EXPLAIN plan and exit without executing the query.",
    )
    gate.add_argument(
        "--no-explain",
        action="store_true",
        help="Bypass the EXPLAIN gate. Use with care.",
    )
    parser.add_argument(
        "--max-rows",
        default=str(DEFAULT_MAX_ROWS),
        help=(
            f"Maximum rows allowed across any plan node "
            f"(default: {DEFAULT_MAX_ROWS:.0e}). Accepts scientific notation."
        ),
    )
    parser.add_argument(
        "--max-cost",
        default=str(DEFAULT_MAX_COST),
        help=f"Maximum top-of-plan cost (default: {DEFAULT_MAX_COST:.0e}).",
    )
    parser.add_argument(
        "--host",
        help="PostgreSQL host (default: from receivers config).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Allow writes. Off by default: the session is set "
            "read-only so a mistyped UPDATE/DELETE is refused by the server."
        ),
    )
    parser.set_defaults(func=cmd_health_query)
    return parser
