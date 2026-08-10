"""CLI subcommand for database management.

Provides `receivers db` commands for setup, migration, seeding,
status, dump, restore, and station removal for the gps_health database.

Usage:
    receivers db setup [--host HOST]
    receivers db migrate [--host HOST] [--dry-run]
    receivers db seed [--only stations|coordinates|areas] [--dry-run]
    receivers db status [--host HOST]
    receivers db dump
    receivers db restore FILE [--host HOST]
    receivers db drop-station STATION [--dry-run] [--force] [--host HOST]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Project-relative paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
DUMPS_DIR = PROJECT_ROOT / "dumps"

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


# ── Target resolution + safety ────────────────────────────────────────────────
#
# Every ``receivers db`` verb opens a SINGLE-HOST connection. These verbs are
# schema-level and destructive (``DROP SCHEMA public CASCADE``, station purges);
# on a host with ``mirror_host`` configured the default connection fans writes
# out to the mirror, so ``receivers db setup`` on a laptop would have dropped
# the schema on pgdev too — with no prompt, because the old guard only fired
# when ``--host`` was passed explicitly.


def resolve_db_host(host: str | None) -> str:
    """Return the host a ``db`` verb will actually connect to.

    ``--host`` when given, otherwise the resolved primary from database.cfg /
    ``POSTGRES_HOST`` — never the CLI arg alone, which is ``None`` in exactly
    the case that used to skip the confirmation prompt.
    """
    if host:
        return host
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        return DatabaseConnectionFactory.get_connection_params()["host"]
    except Exception:  # noqa: BLE001 - fall back to a name that forces a prompt
        return "<unresolved>"


def confirm_destructive(action: str, host: str | None, force: bool = False) -> bool:
    """Prompt before a destructive ``db`` verb against a non-local host."""
    target = resolve_db_host(host)
    if target in LOCAL_HOSTS:
        return True
    if force:
        logger.warning("%s on %s proceeding without prompt (--force)", action, target)
        return True
    confirm = input(f"Type 'gps_health' to confirm {action} on {target}: ")
    if confirm != "gps_health":
        print("Aborted.")
        return False
    return True


def db_connection(host: str | None, database: str | None = None):
    """Open the single-host connection every ``db`` verb must use."""
    from ..db.connection import get_connection

    return get_connection(host_override=host, database=database, single_host=True)


# ── Command handlers ──────────────────────────────────────────────────────────


def cmd_db_setup(args: argparse.Namespace) -> int:
    """Drop schema, apply consolidated migration, seed all data."""
    host = getattr(args, "host", None)

    if not confirm_destructive("DROP + SETUP", host):
        return 1

    print("=== GPS Health Database Setup ===\n")
    print(f"Target: {resolve_db_host(host)} (single host — mirror NOT touched)\n")

    # Step 1: Drop schema
    print("--- Dropping existing schema ---")
    try:
        conn = db_connection(host)
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.commit()
        conn.close()
        print("Schema dropped and recreated.\n")
    except Exception as e:
        print(f"Error dropping schema: {e}")
        return 1

    # Step 2: Apply consolidated migration
    print("--- Applying consolidated schema ---")
    try:
        from ..db.migrator import Migrator

        migrator = Migrator(host_override=host)
        applied = migrator.migrate()
        if applied:
            print(f"Applied {len(applied)} migration(s).\n")
        else:
            print("No migrations to apply.\n")
    except Exception as e:
        print(f"Error applying migrations: {e}")
        return 1

    # Step 3: Seed all data
    print("--- Seeding data ---")
    try:
        from ..db.seeder import Seeder

        seeder = Seeder(host_override=host)
        results = seeder.seed_all()
        print("\n=== Setup complete ===")
        _print_seed_summary(results)
    except Exception as e:
        print(f"Error seeding data: {e}")
        return 1

    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    """Apply pending database migrations."""
    host = getattr(args, "host", None)
    dry_run = getattr(args, "dry_run", False)

    from ..db.migrator import Migrator

    migrator = Migrator(host_override=host)

    print("=== Database Migration ===\n")

    if dry_run:
        print("(dry run — no changes will be made)\n")

    try:
        applied = migrator.migrate(dry_run=dry_run)
        if applied:
            print(
                f"\n{'Would apply' if dry_run else 'Applied'} {len(applied)} migration(s)"
            )
        else:
            print("All migrations already applied.")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        logger.exception("Migration failed")
        return 1


def cmd_db_seed(args: argparse.Namespace) -> int:
    """Seed database with station data."""
    host = getattr(args, "host", None)
    dry_run = getattr(args, "dry_run", False)
    only = getattr(args, "only", None)

    from ..db.seeder import Seeder

    seeder = Seeder(host_override=host)

    if dry_run:
        print("(dry run — no changes will be made)\n")

    try:
        if only == "stations":
            seeder.seed_stations(dry_run=dry_run)
        elif only == "coordinates":
            seeder.seed_coordinates(dry_run=dry_run)
        elif only == "areas":
            seeder.seed_areas(dry_run=dry_run)
        elif only == "storage":
            if dry_run:
                print("Storage location seeding does not support dry-run.")
            else:
                seeder.seed_storage_locations()
        else:
            results = seeder.seed_all(dry_run=dry_run)
            _print_seed_summary(results)
        return 0
    except Exception as e:
        print(f"Error: {e}")
        logger.exception("Seeding failed")
        return 1


def cmd_db_status(args: argparse.Namespace) -> int:
    """Show database status: tables, rows, migration state."""
    host = getattr(args, "host", None)

    from ..db.migrator import Migrator

    print("=== Database Status ===\n")

    try:
        conn = db_connection(host)
    except Exception as e:
        print(f"Cannot connect: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            # Database size
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            size = cur.fetchone()[0]
            print(f"Database size: {size}")

            # Connection info
            cur.execute(
                "SELECT current_database(), inet_server_addr(), inet_server_port()"
            )
            db, addr, port = cur.fetchone()
            print(f"Connected to: {db} @ {addr or 'localhost'}:{port or 5432}\n")

            # Table row counts
            cur.execute(
                """
                SELECT relname AS table_name, n_live_tup AS rows
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                ORDER BY n_live_tup DESC
            """
            )
            rows = cur.fetchall()
            if rows:
                print("Tables:")
                max_name = max(len(r[0]) for r in rows)
                for name, count in rows:
                    print(f"  {name:<{max_name}}  {count:>8} rows")
            else:
                print("No tables found.")

            # Views
            cur.execute(
                """
                SELECT table_name FROM information_schema.views
                WHERE table_schema = 'public' ORDER BY table_name
            """
            )
            views = [r[0] for r in cur.fetchall()]
            if views:
                print(f"\nViews ({len(views)}):")
                for v in views:
                    print(f"  {v}")

        # Migration status
        migrator = Migrator(host_override=host)
        status = migrator.status()
        print(
            f"\nMigrations: {len(status['applied'])} applied, {len(status['pending'])} pending"
        )
        if status["pending"]:
            print("Pending:")
            for name in status["pending"]:
                print(f"  - {name}")

        conn.close()
        return 0

    except Exception as e:
        print(f"Error: {e}")
        conn.close()
        return 1


#: Tables whose primary↔mirror divergence has actually bitten us. Counted by
#: default; ``--tables`` overrides, ``--all`` sweeps every public table.
PARITY_TABLES = ("file_tracking", "file_absence", "archive_catalog")


#: Grouping column used by default where a table has it. A whole-table count
#: is a MISLEADING parity signal: drift in opposite directions cancels out.
#: Measured on the real hosts 2026-08-10 — per session_type the divergence in
#: file_tracking was 32,340 rows (rek-d01 ahead on status_1hr/1Hz, pgdev ahead
#: on 15s_24hr), while the whole-table delta read 24,598. The plain count
#: understated it by 24%, and a fully cancelling drift would have read "ok" on
#: a table that was wrong on both sides.
PARITY_GROUP_COLUMN = "session_type"


def _count_rows(host: str | None, tables: list[str]) -> dict[str, int | None]:
    """Exact row counts for ``tables`` on one host. None where the table is absent."""
    counts: dict[str, int | None] = {}
    conn = db_connection(host)
    try:
        with conn.cursor() as cur:
            for t in tables:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",))
                if not cur.fetchone()[0]:
                    counts[t] = None
                    continue
                # Identifier is from a curated list / an explicit operator flag,
                # never untrusted input — but quote it anyway.
                cur.execute(f'SELECT count(*) FROM "{t}"')
                counts[t] = cur.fetchone()[0]
        conn.rollback()  # reads must not park the connection in a transaction
    finally:
        conn.close()
    return counts


def _count_rows_grouped(
    host: str | None, tables: list[str], by: str
) -> dict[str, dict[str, int] | None]:
    """Per-group counts for ``tables`` on one host, keyed by ``by``.

    Returns ``None`` for a table that is absent OR that lacks the grouping
    column, so the caller can fall back to a whole-table count for it rather
    than dropping it from the report.
    """
    out: dict[str, dict[str, int] | None] = {}
    conn = db_connection(host)
    try:
        with conn.cursor() as cur:
            for t in tables:
                cur.execute(
                    """SELECT EXISTS (
                           SELECT 1 FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = %s AND column_name = %s
                       )""",
                    (t, by),
                )
                if not cur.fetchone()[0]:
                    out[t] = None
                    continue
                cur.execute(f'SELECT "{by}"::text, count(*) FROM "{t}" GROUP BY 1')
                out[t] = {(g if g is not None else "∅"): n for g, n in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()
    return out


def cmd_db_parity(args: argparse.Namespace) -> int:
    """Compare row counts between the primary and the dual-write mirror.

    The mirror is best-effort: `_DualCursor` logs a failed mirror leg and drops
    the statement — no retry, no queue, no reconciliation. So every pgdev blip
    is a permanent, silent divergence, and it drifts in BOTH directions
    (maintenance run with single_host=True deletes on the primary only, leaving
    orphans on the mirror). Measured 2026-08-10: file_tracking 961,020 vs
    936,423.

    This does not fix that — it makes it visible. Exits non-zero past the
    tolerance so cron or Icinga can alarm on it.
    """
    from ..health.database_factory import _load_config_file

    cfg = _load_config_file()
    mirror = getattr(args, "mirror", None) or cfg.get("mirror_host")
    primary = resolve_db_host(getattr(args, "host", None))

    if not mirror:
        print("No mirror_host configured — nothing to compare.")
        return 0
    if mirror == primary:
        print(f"Mirror and primary are the same host ({primary}) — nothing to compare.")
        return 0

    if getattr(args, "all", False):
        conn = db_connection(primary)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT relname FROM pg_stat_user_tables "
                    "WHERE schemaname = 'public' ORDER BY relname"
                )
                tables = [r[0] for r in cur.fetchall()]
            conn.rollback()
        finally:
            conn.close()
    elif getattr(args, "tables", None):
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    else:
        tables = list(PARITY_TABLES)

    by = getattr(args, "by", PARITY_GROUP_COLUMN)
    if getattr(args, "no_group", False):
        by = None

    try:
        pri = _count_rows(primary, tables)
        mir = _count_rows(mirror, tables)
        gpri = _count_rows_grouped(primary, tables, by) if by else {}
        gmir = _count_rows_grouped(mirror, tables, by) if by else {}
    except Exception as e:
        print(f"Cannot compare: {e}")
        return 1

    tolerance = float(getattr(args, "tolerance_pct", 0.5) or 0.5)
    rows = []
    breached = False
    for t in tables:
        p, m = pri.get(t), mir.get(t)
        if p is None or m is None:
            rows.append(
                {
                    "table": t,
                    "primary": p,
                    "mirror": m,
                    "status": "absent",
                    "net": None,
                    "divergence": None,
                    "pct": None,
                    "groups": [],
                }
            )
            continue

        gp, gm = gpri.get(t), gmir.get(t)
        if gp is not None and gm is not None:
            # Group-level truth: opposite-direction drift must NOT cancel.
            worst = []
            divergence = 0
            for key in sorted(set(gp) | set(gm)):
                d = gm.get(key, 0) - gp.get(key, 0)
                if d:
                    divergence += abs(d)
                    worst.append(
                        {
                            "group": key,
                            "primary": gp.get(key, 0),
                            "mirror": gm.get(key, 0),
                            "delta": d,
                        }
                    )
            worst.sort(key=lambda g: abs(g["delta"]), reverse=True)
            grouped = True
        else:
            divergence = abs(m - p)
            worst = []
            grouped = False

        net = m - p
        pct = (divergence / p * 100) if p else (0.0 if not divergence else 100.0)
        over = pct > tolerance
        breached = breached or over
        rows.append(
            {
                "table": t,
                "primary": p,
                "mirror": m,
                "net": net,
                "divergence": divergence,
                "pct": pct,
                "grouped_by": by if grouped else None,
                "groups": worst,
                "status": "OVER" if over else "ok",
            }
        )

    if getattr(args, "json", False):
        import json as _json

        print(
            _json.dumps(
                {
                    "primary": primary,
                    "mirror": mirror,
                    "grouped_by": by,
                    "tolerance_pct": tolerance,
                    "breached": breached,
                    "tables": [
                        {
                            **r,
                            "pct": None if r["pct"] is None else round(r["pct"], 3),
                        }
                        for r in rows
                    ],
                },
                indent=2,
            )
        )
        return 1 if breached else 0

    print(f"=== Mirror parity: {primary} (primary) vs {mirror} (mirror) ===")
    if by:
        print(f"    grouped by {by} — divergence is Σ|delta| per group, not the net\n")
    else:
        print("    whole-table counts — opposite drift CANCELS, see --by\n")
    print(
        f"{'table':<20}{'primary':>13}{'mirror':>13}{'net':>10}"
        f"{'divergence':>12}{'pct':>8}  status"
    )
    for r in rows:
        if r["status"] == "absent":
            p, m = r["primary"], r["mirror"]
            print(
                f"{r['table']:<20}{'—' if p is None else f'{p:,}':>13}"
                f"{'—' if m is None else f'{m:,}':>13}{'':>10}{'':>12}{'':>8}"
                "  absent on a host"
            )
            continue
        print(
            f"{r['table']:<20}{r['primary']:>13,}{r['mirror']:>13,}"
            f"{r['net']:>+10,}{r['divergence']:>12,}{r['pct']:>7.2f}%  {r['status']}"
        )
        # The groups are the actionable part: they say WHERE it drifted.
        for g in r["groups"][:5]:
            print(
                f"    {g['group']:<16}{g['primary']:>13,}{g['mirror']:>13,}"
                f"{g['delta']:>+10,}"
            )
        if len(r["groups"]) > 5:
            print(f"    … {len(r['groups']) - 5} more group(s)")

    if breached:
        print(
            f"\nDivergence above {tolerance}%. This only REPORTS — the mirror has no "
            "reconciliation path, so nothing here syncs anything. See vault todo "
            "#142 (sweep vs real replication)."
        )

    if getattr(args, "icinga", False):
        _push_parity_to_icinga(
            rows,
            breached=breached,
            tolerance=tolerance,
            icinga_host=getattr(args, "icinga_host", None) or "rek-d01",
            ttl=getattr(args, "ttl", None),
        )

    return 1 if breached else 0


def _push_parity_to_icinga(
    rows: list[dict],
    *,
    breached: bool,
    tolerance: float,
    icinga_host: str,
    ttl: int | None,
) -> bool:
    """Push the parity verdict to Icinga as a passive check. Best-effort.

    Without this the timer's finding lands only in the journal, which nobody
    reads — the same "silent" failure mode the check exists to end. ``ttl``
    makes Icinga flag the service stale if the timer itself stops, so a dead
    check is distinguishable from a healthy one.
    """
    try:
        from ..monitoring.icinga_client import CheckResult, IcingaClient

        compared = [r for r in rows if r["status"] != "absent"]
        worst = max(compared, key=lambda r: r["pct"], default=None)
        if worst is None:
            status, output = 3, "parity: no comparable tables"
        elif breached:
            status = 1  # WARNING: divergence is chronic, not an outage
            output = (
                f"mirror divergence {worst['pct']:.2f}% on {worst['table']} "
                f"({worst['divergence']:,} rows) — above {tolerance}%"
            )
        else:
            status = 0
            output = f"mirror within {tolerance}% (worst {worst['pct']:.2f}%)"

        perf = " ".join(f"{r['table']}_divergence={r['divergence']}" for r in compared)
        resp = IcingaClient().send_check_result(
            CheckResult(
                station=icinga_host,
                check_name="Mirror parity",
                exit_status=status,
                plugin_output=output,
                performance_data=perf,
                ttl=ttl,
            )
        )
        ok = bool(resp.get("success")) if isinstance(resp, dict) else bool(resp)
        if not ok:
            logger.warning("Icinga parity push did not succeed: %s", resp)
        return ok
    except Exception as exc:  # noqa: BLE001 — alerting must never break the check
        logger.warning("Icinga parity push failed: %s", exc)
        return False


def cmd_db_dump(args: argparse.Namespace) -> int:
    """Dump database to SQL file."""
    import os

    DUMPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    db_name = os.environ.get("POSTGRES_DB", "gps_health")
    db_user = os.environ.get("POSTGRES_USER", os.environ.get("USER", "postgres"))
    dump_file = DUMPS_DIR / f"{db_name}_{timestamp}.sql"

    print(f"Dumping {db_name} to {dump_file}...")

    try:
        subprocess.run(
            [
                "pg_dump",
                "-h",
                "localhost",
                "-U",
                db_user,
                "-d",
                db_name,
                "--no-owner",
                "--no-privileges",
                "--clean",
                "--if-exists",
                "-f",
                str(dump_file),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        size = dump_file.stat().st_size
        print(f"Dump complete: {dump_file} ({size / 1024:.0f} KB)")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"pg_dump failed: {e.stderr}")
        return 1
    except FileNotFoundError:
        print("Error: pg_dump not found. Is PostgreSQL client installed?")
        return 1


def cmd_db_restore(args: argparse.Namespace) -> int:
    """Restore database from SQL dump file."""
    import os

    dump_file = Path(args.file)
    host = getattr(args, "host", None) or "localhost"

    if not dump_file.exists():
        print(f"Error: File not found: {dump_file}")
        return 1

    # Safety check for remote hosts
    if host not in ("localhost", "127.0.0.1"):
        confirm = input(f"Type 'gps_health' to confirm RESTORE on {host}: ")
        if confirm != "gps_health":
            print("Aborted.")
            return 1

    db_name = os.environ.get("POSTGRES_DB", "gps_health")
    db_user = os.environ.get("POSTGRES_USER", os.environ.get("USER", "postgres"))

    print(f"Restoring {dump_file} to {db_name}@{host}...")

    try:
        subprocess.run(
            [
                "psql",
                "-h",
                host,
                "-U",
                db_user,
                "-d",
                db_name,
                "-f",
                str(dump_file),
                "--single-transaction",
                "-v",
                "ON_ERROR_STOP=1",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Restore complete.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"psql restore failed: {e.stderr}")
        return 1
    except FileNotFoundError:
        print("Error: psql not found. Is PostgreSQL client installed?")
        return 1


def cmd_db_drop_station(args: argparse.Namespace) -> int:
    """Remove a station and all its data from the database."""
    station_id = args.station_id.upper()
    host = getattr(args, "host", None)
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)

    if not dry_run and not confirm_destructive(
        f"DROP STATION {station_id}", host, force=force
    ):
        return 1

    try:
        conn = db_connection(host)
    except Exception as e:
        print(f"Cannot connect: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            # Verify station exists
            cur.execute("SELECT sid FROM stations WHERE sid = %s", (station_id,))
            if not cur.fetchone():
                print(f"Station '{station_id}' not found in database.")
                conn.close()
                return 1

            # Find all tables with a sid column (except stations itself)
            cur.execute(
                """
                SELECT table_name FROM information_schema.columns
                WHERE column_name = 'sid' AND table_schema = 'public'
                  AND table_name != 'stations'
                ORDER BY table_name
            """
            )
            tables = [row[0] for row in cur.fetchall()]

            # Count rows per table. The table names come from information_schema,
            # but they are still identifiers we cannot bind — compose them with
            # sql.Identifier so psycopg2 quotes them; the label and the sid stay
            # bound parameters (two per branch, in table order).
            if tables:
                from psycopg2 import sql

                union_sql = sql.SQL(" UNION ALL ").join(
                    sql.SQL(
                        "SELECT %s AS tbl, COUNT(*) FROM {tbl} WHERE sid = %s"
                    ).format(tbl=sql.Identifier(t))
                    for t in tables
                )
                params = tuple(v for t in tables for v in (t, station_id))
                cur.execute(union_sql, params)
                counts = [(row[0], row[1]) for row in cur.fetchall()]
            else:
                counts = []

            # Display summary
            total = sum(c for _, c in counts)
            non_zero = [(t, c) for t, c in counts if c > 0]

            print(f"Station: {station_id}")
            print(f"Tables with data: {len(non_zero)} / {len(counts)}")
            if non_zero:
                max_name = max(len(t) for t, _ in non_zero)
                for tbl, cnt in sorted(non_zero, key=lambda x: -x[1]):
                    print(f"  {tbl:<{max_name}}  {cnt:>8} rows")
            print(f"  {'TOTAL':<20}  {total:>8} rows")
            print("  + 1 row in stations")

            if dry_run:
                print("\n(dry run — no changes made)")
                conn.close()
                return 0

            # Confirmation
            if not force:
                confirm = input(f"\nType '{station_id}' to confirm deletion: ")
                if confirm.strip().upper() != station_id:
                    print("Aborted.")
                    conn.close()
                    return 1

            # Delete station_area_members explicitly (no FK cascade)
            if "station_area_members" in tables:
                cur.execute(
                    "DELETE FROM station_area_members WHERE sid = %s",
                    (station_id,),
                )

            # Delete from stations (cascades to block_* and other FK tables)
            cur.execute("DELETE FROM stations WHERE sid = %s", (station_id,))

        conn.commit()
        conn.close()
        print(f"\nDeleted station {station_id} and {total} related rows.")
        return 0

    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Error: {e}")
        logger.exception("drop-station failed")
        return 1


# ── Parser registration ───────────────────────────────────────────────────────


def cmd_db_list_suppressed(args: argparse.Namespace) -> int:
    """List stations suppressed because they were removed from stations.cfg."""
    host = getattr(args, "host", None)
    try:
        conn = db_connection(host)
    except Exception as e:
        print(f"Cannot connect: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sid, station_name, receiver_type, updated_at
                FROM stations
                WHERE station_status = 'suppressed'
                ORDER BY updated_at DESC
                """
            )
            rows = cur.fetchall()

        if not rows:
            print("No suppressed stations.")
            return 0

        print(f"{'SID':<6}  {'Name':<30}  {'Receiver':<12}  Suppressed at")
        print("-" * 72)
        for sid, name, rtype, ts in rows:
            name_str = name or ""
            rtype_str = rtype or ""
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else ""
            print(f"{sid:<6}  {name_str:<30}  {rtype_str:<12}  {ts_str}")

        print(
            f"\n{len(rows)} suppressed station(s). Use 'receivers db drop-station SID' to remove permanently."
        )
        return 0

    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        conn.close()


def create_db_parser(subparsers) -> None:
    """Add db subcommands to the main parser."""
    db_parser = subparsers.add_parser(
        "db",
        help="Manage GPS health database",
        description="Database setup, migration, seeding, and maintenance",
    )

    db_subparsers = db_parser.add_subparsers(
        dest="db_command",
        help="Database commands",
    )

    # setup
    setup_parser = db_subparsers.add_parser(
        "setup",
        help="Drop + migrate + seed (fresh install)",
    )
    setup_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    setup_parser.set_defaults(func=cmd_db_setup)

    # migrate
    migrate_parser = db_subparsers.add_parser(
        "migrate",
        help="Apply pending migrations",
    )
    migrate_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be applied"
    )
    migrate_parser.set_defaults(func=cmd_db_migrate)

    # seed
    seed_parser = db_subparsers.add_parser(
        "seed",
        help="Seed database with station data",
    )
    seed_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    seed_parser.add_argument(
        "--only",
        choices=["stations", "coordinates", "areas", "storage"],
        help="Only run a specific seed operation",
    )
    seed_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done"
    )
    seed_parser.set_defaults(func=cmd_db_seed)

    # status
    status_parser = db_subparsers.add_parser(
        "status",
        help="Show database status",
    )
    status_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    status_parser.set_defaults(func=cmd_db_status)

    # dump
    dump_parser = db_subparsers.add_parser(
        "dump",
        help="Dump database to SQL file",
    )
    dump_parser.set_defaults(func=cmd_db_dump)

    # restore
    restore_parser = db_subparsers.add_parser(
        "restore",
        help="Restore database from dump file",
    )
    restore_parser.add_argument("file", help="SQL dump file to restore")
    restore_parser.add_argument("--host", help="PostgreSQL host (default: localhost)")
    restore_parser.set_defaults(func=cmd_db_restore)

    # list-suppressed
    list_sup_parser = db_subparsers.add_parser(
        "list-suppressed",
        help="List stations suppressed because they were removed from stations.cfg",
    )
    list_sup_parser.add_argument(
        "--host", help="PostgreSQL host (default: from config)"
    )
    list_sup_parser.set_defaults(func=cmd_db_list_suppressed)

    # parity
    parity_parser = db_subparsers.add_parser(
        "parity",
        help="Compare row counts between the primary and the dual-write mirror",
    )
    parity_parser.add_argument(
        "--tables",
        help=f"Comma-separated tables (default: {','.join(PARITY_TABLES)})",
    )
    parity_parser.add_argument(
        "--all", action="store_true", help="Compare every public table"
    )
    parity_parser.add_argument(
        "--tolerance-pct",
        type=float,
        default=0.5,
        help="Exit non-zero above this divergence (default: 0.5)",
    )
    parity_parser.add_argument(
        "--by",
        default=PARITY_GROUP_COLUMN,
        help=(
            f"Group counts by this column where a table has it "
            f"(default: {PARITY_GROUP_COLUMN}). Grouping is the honest signal — "
            "a whole-table count lets opposite drift cancel out"
        ),
    )
    parity_parser.add_argument(
        "--no-group",
        action="store_true",
        help="Whole-table counts only (understates divergence; see --by)",
    )
    parity_parser.add_argument("--json", action="store_true", help="JSON output")
    parity_parser.add_argument(
        "--icinga",
        action="store_true",
        help="also push the verdict to Icinga as a passive check",
    )
    parity_parser.add_argument(
        "--icinga-host", default="rek-d01", help="Icinga host object (default: rek-d01)"
    )
    parity_parser.add_argument(
        "--ttl",
        type=int,
        help="Icinga staleness TTL in seconds — flags the service if the timer dies",
    )
    parity_parser.add_argument(
        "--mirror", help="Mirror host (default: mirror_host from database.cfg)"
    )
    parity_parser.add_argument("--host", help="Primary host (default: from config)")
    parity_parser.set_defaults(func=cmd_db_parity)

    # drop-station
    drop_parser = db_subparsers.add_parser(
        "drop-station",
        help="Remove a station and all its data",
    )
    drop_parser.add_argument("station_id", help="Station ID to remove (e.g. SFEH)")
    drop_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be deleted"
    )
    drop_parser.add_argument(
        "--force", action="store_true", help="Skip confirmation prompt"
    )
    drop_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    drop_parser.set_defaults(func=cmd_db_drop_station)


def handle_db_command(args: argparse.Namespace) -> int:
    """Handle db subcommands."""
    if not hasattr(args, "db_command") or not args.db_command:
        print("No db command specified.")
        print(
            "Available commands: setup, migrate, seed, status, dump, restore, drop-station"
        )
        print("Run 'receivers db <command> --help' for details.")
        return 1

    return args.func(args)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _print_seed_summary(results: dict) -> None:
    """Print a summary of seed results."""
    print("\nSeed summary:")
    if "stations" in results:
        s = results["stations"]
        print(
            f"  Stations:    {s.get('inserted', 0)} inserted, {s.get('updated', 0)} updated"
        )
    if "coordinates" in results:
        c = results["coordinates"]
        print(f"  Coordinates: {c.get('updated', 0)} updated")
    if "areas" in results:
        a = results["areas"]
        print(
            f"  Areas:       {a.get('areas', 0)} areas, {a.get('members', 0)} members"
        )
    if "storage_locations" in results:
        print(f"  Storage:     {results['storage_locations']} inserted")
