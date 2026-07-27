"""Database migration runner for GPS receivers.

Applies SQL migrations in order, tracking which have been applied
via the schema_migrations table. On fresh databases, applies the
consolidated schema (000) and marks all individual migrations as done.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .connection import get_connection

logger = logging.getLogger(__name__)

# Project-relative migrations directory
MIGRATIONS_DIR = Path(__file__).parent.parent.parent.parent / "migrations"


class Migrator:
    """Manages database schema migrations.

    Checks the schema_migrations table to determine which migrations
    have already been applied, then applies pending ones in order.

    On a fresh database (no schema_migrations table), applies
    000_consolidated_schema.sql (marks 001-023 as applied), then
    applies any remaining migrations (024+) individually.
    """

    def __init__(
        self,
        host_override: str | None = None,
        migrations_dir: Path | None = None,
    ) -> None:
        self.host_override = host_override
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR

    def _get_conn(self):
        # Single-host: schema/seed writes must never fan out to the pgdev
        # mirror (a mirrored DDL/seed is a silent cross-host mutation).
        conn = get_connection(host_override=self.host_override, single_host=True)
        # Migrations are exempt from the app-wide statement/lock timeouts
        # (database_factory adds statement_timeout=600s, lock_timeout=30s to
        # every connection). DDL on a live 8.5M-row table legitimately runs
        # long and legitimately waits for ACCESS EXCLUSIVE while the scheduler
        # writes — a timeout there aborts the migration MID-DEPLOY, which is a
        # far worse failure than the slow query the ceiling exists to bound.
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 0")
                cur.execute("SET lock_timeout = 0")
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - non-fatal; keep the defaults
            logger.warning("could not clear migration timeouts: %s", exc)
            conn.rollback()
        return conn

    def _has_migrations_table(self, conn) -> bool:
        """Check if schema_migrations table exists."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'schema_migrations'
                )
            """)
            return cur.fetchone()[0]

    def _has_any_tables(self, conn) -> bool:
        """Check if any application tables exist (not just schema_migrations)."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'stations'
                )
            """)
            return cur.fetchone()[0]

    def _get_applied(self, conn) -> set[str]:
        """Get set of already-applied migration names."""
        if not self._has_migrations_table(conn):
            return set()
        with conn.cursor() as cur:
            cur.execute("SELECT migration_name FROM schema_migrations")
            return {row[0] for row in cur.fetchall()}

    def _apply_sql(self, conn, sql_path: Path) -> None:
        """Apply a single SQL file."""
        sql = sql_path.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)

    def _record_migration(self, conn, name: str) -> None:
        """Record a migration as applied."""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (migration_name) VALUES (%s) ON CONFLICT DO NOTHING",
                (name,),
            )

    def _get_migration_files(self) -> list[Path]:
        """Get sorted list of migration SQL files (excluding rollbacks)."""
        if not self.migrations_dir.is_dir():
            return []
        files = []
        for f in sorted(self.migrations_dir.glob("[0-9]*.sql")):
            if "_rollback" in f.name:
                continue
            files.append(f)
        return files

    def _migration_name(self, path: Path) -> str:
        """Extract migration name from filename (without .sql extension)."""
        return path.stem

    def status(self) -> dict:
        """Get migration status.

        Returns:
            Dict with 'applied' (set of names), 'pending' (list of names),
            'total_files' count, and 'is_fresh' boolean.
        """
        conn = self._get_conn()
        try:
            applied = self._get_applied(conn)
            has_tables = self._has_any_tables(conn)
            all_files = self._get_migration_files()

            pending = []
            for f in all_files:
                name = self._migration_name(f)
                if name not in applied:
                    pending.append(name)

            return {
                "applied": applied,
                "pending": pending,
                "total_files": len(all_files),
                "is_fresh": not has_tables,
            }
        finally:
            conn.close()

    def migrate(self, dry_run: bool = False) -> list[str]:
        """Apply pending migrations.

        On a fresh database:
          - Applies 000_consolidated_schema.sql (subsumes 001-023)
          - Then applies any remaining migrations (024+) individually

        On an existing database:
          - Applies only unapplied migrations in numeric order
          - Skips 000 if individual migrations are already applied

        Args:
            dry_run: If True, show what would be applied without doing it.

        Returns:
            List of migration names that were (or would be) applied.
        """
        conn = self._get_conn()
        applied_list: list[str] = []

        try:
            applied = self._get_applied(conn)
            has_tables = self._has_any_tables(conn)
            all_files = self._get_migration_files()

            # Fresh database: use consolidated schema, then apply remaining
            if not has_tables and not applied:
                consolidated = self.migrations_dir / "000_consolidated_schema.sql"
                if consolidated.exists():
                    name = self._migration_name(consolidated)
                    if dry_run:
                        logger.info("Would apply: %s (consolidated schema)", name)
                        print(f"  Would apply: {name} (consolidated schema)")
                        applied_list.append(name)
                    else:
                        logger.info("Applying consolidated schema: %s", name)
                        print(f"  Applying: {name} (consolidated schema)")
                        self._apply_sql(conn, consolidated)
                        conn.commit()
                        applied_list.append(name)
                        logger.info(
                            "Consolidated schema applied — all 001-023 marked as applied"
                        )
                    # Refresh applied set (000 marks 001-023 as applied)
                    # then fall through to apply any remaining migrations
                    applied = self._get_applied(conn)

            # Apply pending migrations in order
            for f in all_files:
                name = self._migration_name(f)
                # Skip consolidated schema on existing databases
                if name == "000_consolidated_schema":
                    continue
                if name in applied:
                    continue

                if dry_run:
                    logger.info("Would apply: %s", name)
                    print(f"  Would apply: {name}")
                else:
                    logger.info("Applying: %s", name)
                    print(f"  Applying: {name}")
                    self._apply_sql(conn, f)
                    self._record_migration(conn, name)
                    conn.commit()

                applied_list.append(name)

            if not applied_list:
                logger.info("All migrations already applied")

            return applied_list

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
