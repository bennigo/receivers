"""``receivers missing`` — query the unified-file-index differential worklists.

Read-only surface over the migration-058/060 views: what raw is missing on the
receiver, what rinex is un-generated, what needs re-pulling from the archive.
This is the "query the DB to drive backfill, never ``ls`` a directory" entry
point (plan ask #2).

D9 (standalone operation): this targets the LOCAL ``gps_health`` by default — the
server's worklists must not depend on the central/pgdev copy. ``--host`` overrides
for ad-hoc inspection of another catalog host.

The worklists are ADVISORY at this stage (static receiver-buffer floor; terminal
absence not yet driving skips) — see the view comments. Use for visibility and
review, not an automated fetch/re-rinex loop, until slice-2b lands.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

from ..db.tx import read_only_cursor

logger = logging.getLogger("receivers.cli.missing")

# worklist key -> (view name, column list). One place to add a worklist.
_WORKLISTS = {
    "on-receiver": (
        "missing_on_receiver",
        ["station", "session_type", "file_date", "file_hour"],
    ),
    "rinex": (
        "missing_rinex",
        [
            "station",
            "session_type",
            "file_date",
            "file_hour",
            "raw_local",
            "raw_archive",
        ],
    ),
    "repull": (
        "needs_repull_from_archive",
        ["station", "session_type", "file_date", "file_hour"],
    ),
}


def _get_conn(host):
    from ..db.connection import optional_connection

    return optional_connection(host, required=True, log=logger)


def _refresh(conn) -> None:
    """Refresh the materialized differential views (best-effort, logged)."""
    for fn in ("refresh_file_coverage", "refresh_missing_on_receiver"):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {fn}()")
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s failed: %s", fn, exc)
            conn.rollback()


def _query(conn, view, columns, *, stations, sessions, limit):
    where, params = [], []
    if stations:
        where.append("station = ANY(%s)")
        params.append(list(stations))
    if sessions:
        where.append("session_type = ANY(%s)")
        params.append(list(sessions))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT {', '.join(columns)} FROM {view}{clause} "
        f"ORDER BY station, session_type, file_date, file_hour"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [dict(zip(columns, r)) for r in rows]


def _count(conn, view, *, stations, sessions) -> int:
    where, params = [], []
    if stations:
        where.append("station = ANY(%s)")
        params.append(list(stations))
    if sessions:
        where.append("session_type = ANY(%s)")
        params.append(list(sessions))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with read_only_cursor(conn) as cur:
        cur.execute(f"SELECT count(*) FROM {view}{clause}", params)
        return cur.fetchone()[0]


def _write_manifest(path: Path, rows: list[dict]) -> None:
    """Write the known-missing manifest as JSON or CSV (by extension)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # dates → ISO strings for portability
    serial = [
        {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
        for r in rows
    ]
    if path.suffix.lower() == ".csv":
        cols = list(serial[0].keys()) if serial else []
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(serial)
    else:
        path.write_text(json.dumps(serial, indent=2))


def cmd_missing(args: argparse.Namespace) -> int:
    conn = _get_conn(args.host)
    try:
        if args.refresh:
            _refresh(conn)

        selected = [
            k
            for k in ("on-receiver", "rinex", "repull")
            if getattr(args, k.replace("-", "_"))
        ]

        # No worklist flag → summary of all.
        if not selected:
            summary = {
                k: _count(conn, v[0], stations=args.station, sessions=args.session)
                for k, v in _WORKLISTS.items()
            }
            if args.json:
                print(json.dumps(summary))
            else:
                print("Unified file index — missing worklists (advisory):")
                for k, n in summary.items():
                    print(f"  {k:<12} {n}")
                print("  (query-only; no directory listing)")
            return 0

        # One or more worklists → rows.
        all_rows: list[dict] = []
        for key in selected:
            view, cols = _WORKLISTS[key]
            rows = _query(
                conn,
                view,
                cols,
                stations=args.station,
                sessions=args.session,
                limit=args.limit,
            )
            for r in rows:
                r["worklist"] = key
            all_rows.extend(rows)

        if args.manifest:
            _write_manifest(Path(args.manifest), all_rows)
            print(f"wrote {len(all_rows)} rows → {args.manifest}")
        elif args.json:
            print(json.dumps(all_rows, default=str))
        else:
            if not all_rows:
                print("(no missing files in the selected worklist(s))")
            for r in all_rows:
                hour = "" if r.get("file_hour") is None else f":{r['file_hour']:02d}"
                print(
                    f"  [{r['worklist']:<11}] {r['station']} {r['session_type']} "
                    f"{r['file_date']}{hour}"
                )
            print(f"  total: {len(all_rows)}")
        return 0
    finally:
        conn.close()


def create_missing_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "missing",
        help="Query the unified-file-index differential worklists (no ls)",
        description=(
            "Query the DB for what is missing where — raw missing on the receiver, "
            "rinex un-generated, or needing a re-pull from the archive — instead of "
            "listing directories. Targets the LOCAL gps_health (D9 standalone). "
            "Worklists are ADVISORY at this stage (static receiver floor; terminal "
            "absence not yet driving skips). No flag → summary counts."
        ),
    )
    parser.add_argument(
        "--on-receiver",
        action="store_true",
        help="Raw slots missing on the receiver (fetch worklist)",
    )
    parser.add_argument(
        "--rinex",
        action="store_true",
        help="Observations with a raw root but no rinex (re-rinex)",
    )
    parser.add_argument(
        "--repull",
        action="store_true",
        help="Raw in archive but not local (copy from archive)",
    )
    parser.add_argument(
        "--station", nargs="+", metavar="SID", help="Limit to these stations"
    )
    parser.add_argument(
        "--session", nargs="+", metavar="S", help="Limit to these session types"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the materialized views before querying",
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        help="Write the selected rows to a known-missing manifest "
        "(.json or .csv by extension)",
    )
    parser.add_argument("--limit", type=int, help="Cap rows per worklist")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON")
    parser.add_argument("--host", help="gps_health host override (default: local)")
    parser.set_defaults(func=cmd_missing)
    return parser
