"""Centralized database connection for GPS receivers.

Thin wrapper around DatabaseConnectionFactory that provides a simple
interface with optional host override for CLI commands.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)


def get_connection(
    host_override: str | None = None,
    database: str | None = None,
    single_host: bool = False,
) -> Any:
    """Get a database connection using centralized config.

    Uses DatabaseConnectionFactory for connection params with optional
    host override for pointing at different servers.

    **Mirroring.** With no ``host_override`` and a configured ``mirror_host``,
    the returned connection fans every write out to the mirror as well.  Pass
    ``single_host=True`` for anything destructive, id-keyed, read-only, or that
    must be transactional on exactly one server — a fanned ``DROP SCHEMA`` or
    ``… WHERE id = %s`` on a mirror that holds different rows is a data-loss
    bug, not a resilience feature.  ``host_override`` already implies a single
    host (it opens a direct connection to that server).

    When ``host_override`` targets the configured ``mirror_host``, the
    connection uses that host's declared identity (``mirror_user`` + its
    ``~/.pgpass`` credential), NOT the primary user — so a ``--catalog-prod``
    reindex reaches the mirror the same way the mirror writer does. Credential
    resolution lives in :meth:`DatabaseConnectionFactory.get_connection_params_for_host`
    (database.cfg is the single source of truth for per-host access).

    Args:
        host_override: Override the configured host (e.g., 'pgdev.vedur.is').
        database: Override the database name (default: gps_health).
        single_host: Never return a dual-write connection (see above).

    Returns:
        psycopg2 connection object.

    Raises:
        ImportError: If psycopg2 is not installed.
        psycopg2.OperationalError: If connection fails.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    if host_override:
        # Single direct connection to the specific host, resolving that
        # host's credentials from database.cfg (mirror_host → mirror_user).
        return DatabaseConnectionFactory.connect_to_host(
            host_override, database=database or "gps_health"
        )

    return DatabaseConnectionFactory.get_connection(
        database=database or "gps_health", single_host=single_host
    )


def get_single_host_connection(
    host_override: str | None = None,
    database: str | None = None,
) -> Any:
    """Open a connection that is guaranteed never to fan out to the mirror.

    Convenience wrapper over :func:`get_connection` for the destructive /
    id-keyed / read-only call sites, so intent is visible at the call.
    """
    return get_connection(
        host_override=host_override, database=database, single_host=True
    )


def optional_connection(
    host_override: str | None = None,
    *,
    required: bool = True,
    database: str | None = None,
    single_host: bool = False,
    log: logging.Logger | None = None,
) -> Any:
    """Open a gps_health connection, tolerating absence when not required.

    The "a dev laptop may have no gps_health, and a dry run can proceed
    without one" pattern, which ``cli/archive_sync.py`` and ``cli/missing.py``
    each re-implemented.  Returns ``None`` (after a warning) instead of raising
    when ``required`` is False.
    """
    try:
        return get_connection(
            host_override=host_override, database=database, single_host=single_host
        )
    except Exception as exc:  # noqa: BLE001 - dev laptops may lack gps_health
        if required:
            raise
        (log or logger).warning(
            "no gps_health connection (%s) — proceeding without indexing", exc
        )
        return None


@contextmanager
def managed_connection(
    host_override: str | None = None,
    database: str | None = None,
    single_host: bool = False,
) -> Generator:
    """Context manager for safe connection lifecycle.

    Commits on success, rolls back on exception, always closes.

    Args:
        host_override: Override the configured host.
        database: Override the database name.
        single_host: Never return a dual-write connection.

    Yields:
        psycopg2 connection object.
    """
    conn = get_connection(
        host_override=host_override, database=database, single_host=single_host
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
