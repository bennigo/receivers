"""Centralized PostgreSQL connection management for GPS health monitoring.

Provides a single source of truth for database connection parameters,
replacing duplicated connection code across db_writer.py, file_tracker.py,
json_importer.py, bulk_scheduler.py, status_task.py, and main.py.

Configuration priority (highest to lowest):
    1. Environment variables (POSTGRES_HOST, etc.)
    2. Config file (~/.config/gpsconfig/database.cfg or $GPS_CONFIG_PATH/database.cfg)
    3. Built-in defaults

Connection limiting:
    The ``connection()`` context manager uses a bounded semaphore (default 20)
    to prevent PostgreSQL ``max_connections`` exhaustion when 95+ parallel
    download threads each make DB queries.  Threads beyond the limit block
    until a slot opens.  ``get_connection()`` is NOT limited — use it only
    for long-lived singleton connections (e.g. HealthDatabaseWriter).

Dual-write mode:
    Set ``mirror_host`` in ``[postgresql]`` to write to two databases simultaneously.
    The mirror is best-effort: failures are logged but never break the primary.

    **A dual connection is NOT safe for every caller.** Pass ``single_host=True``
    when the operation is destructive (schema DDL), id-keyed (surrogate ids are
    not stable across hosts — see :class:`_DualCursor`), read-only, or must be
    transactional on exactly one server.  ``receivers db`` verbs and
    ``health-query`` do this.

Usage:
    from receivers.health.database_factory import DatabaseConnectionFactory

    # Context manager (recommended — connection-limited)
    with DatabaseConnectionFactory.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

    # Explicitly single-host (never fans out to the mirror)
    with DatabaseConnectionFactory.connection(single_host=True) as conn:
        ...

    # Direct connection (NOT limited — for long-lived connections only)
    conn = DatabaseConnectionFactory.get_connection()
    try:
        ...
    finally:
        conn.close()

    # Just get params (for classes that manage their own connections)
    params = DatabaseConnectionFactory.get_connection_params()
"""

import configparser
import logging
import os
import re
import threading
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, Optional

logger = logging.getLogger(__name__)

# Type alias to avoid module-level psycopg2 import
Connection = Any

# Cache for parsed config file
_config_cache: Optional[Dict[str, str]] = None

# Limit concurrent DB connections to prevent PostgreSQL exhaustion.
# With 95+ parallel download threads each making 3-4 DB queries, the
# default max_connections (100) gets overwhelmed.  The semaphore caps
# pooled connections at _MAX_POOL_CONN, leaving headroom for health
# monitor, Grafana, and manual psql sessions.
_MAX_POOL_CONN = 20
_conn_semaphore = threading.BoundedSemaphore(_MAX_POOL_CONN)

# ── Connection pools ─────────────────────────────────────────────────────────
# The connection() context manager reuses pooled connections instead of opening
# a fresh psycopg2.connect() on every call.  Opening a connection is not free
# (TCP + auth/TLS negotiation), and the health/config paths open one on *every*
# station-op, so per-op reconnects were a measurable CPU cost (see the 2026-07-05
# GSSAPI incident — a slow KDC turned per-op reconnects into a CPU storm).
# Pools are keyed by database name; a separate pool is kept for the mirror host.
# get_connection() (long-lived singletons, e.g. HealthDatabaseWriter) is
# intentionally NOT pooled — it manages its own lifecycle.
_pool_lock = threading.RLock()
_primary_pools: Dict[str, Any] = {}
_mirror_pools: Dict[str, Any] = {}


def _ping(conn: Any) -> bool:
    """Cheap liveness pre-ping: True if the connection answers ``SELECT 1``.

    Guards against handing out a pooled connection the server has since dropped
    (network reset, restart).  Cost is one round-trip — negligible on localhost,
    and far cheaper than the reconnect it replaces.  Leaves no open transaction.
    """
    if getattr(conn, "closed", 0):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.rollback()
        return True
    except Exception:
        return False


def close_all_pools() -> None:
    """Close and drop every pooled connection (for shutdown / tests)."""
    with _pool_lock:
        for pool in list(_primary_pools.values()) + list(_mirror_pools.values()):
            try:
                pool.closeall()
            except Exception:
                pass
        _primary_pools.clear()
        _mirror_pools.clear()


def _load_config_file() -> Dict[str, str]:
    """Load PostgreSQL settings from database.cfg.

    Looks for database.cfg in:
        1. $GPS_CONFIG_PATH/database.cfg (if set)
        2. ~/.config/gpsconfig/database.cfg

    Returns:
        Dict with values from [postgresql] section, or empty dict.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_dir = os.getenv("GPS_CONFIG_PATH")
    if config_dir:
        config_path = Path(config_dir) / "database.cfg"
    else:
        config_path = Path.home() / ".config" / "gpsconfig" / "database.cfg"

    if not config_path.exists():
        _config_cache = {}
        return _config_cache

    parser = configparser.ConfigParser()
    parser.read(config_path)

    result: Dict[str, str] = {}
    if parser.has_section("postgresql"):
        for key in (
            "host",
            "port",
            "database",
            "user",
            "password",
            "mirror_host",
            "mirror_user",
            "mirror_password",
            "connect_timeout",
            "statement_timeout",
            "lock_timeout",
        ):
            if parser.has_option("postgresql", key):
                result[key] = parser.get("postgresql", key)

    logger.debug(
        "Loaded database config from %s (host=%s)", config_path, result.get("host")
    )
    _config_cache = result
    return _config_cache


# ── Dual-write observability ──────────────────────────────────────────────────


class MirrorMetrics:
    """Process-local counters for dual-write health.

    A mirror failure used to be a bare ``logger.warning`` — invisible unless
    somebody grepped the log, which is how the rek-d01/pgdev catalog divergence
    went unnoticed for months.  These counters give the scheduler (and any
    future exporter) something to *observe*.
    """

    _lock = threading.Lock()
    _counters: Dict[str, int] = {}

    @classmethod
    def incr(cls, name: str, amount: int = 1) -> None:
        with cls._lock:
            cls._counters[name] = cls._counters.get(name, 0) + amount

    @classmethod
    def snapshot(cls) -> Dict[str, int]:
        """Return a copy of the current counters (for logging/metrics export)."""
        with cls._lock:
            return dict(cls._counters)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._counters.clear()


# An ``id``-keyed UPDATE/DELETE must never fan out: surrogate ids are assigned
# per-host, so an id read from the primary addresses an unrelated row on the
# mirror.  This is not hypothetical — the mirror holds a fraction of the
# primary's rows (see the 2026-07 archive_catalog divergence), so a fanned
# ``UPDATE … WHERE id = %s`` corrupts whichever row happens to hold that id.
# Matches ``id = %s`` / ``id = %(name)s`` / ``id IN %s`` but not ``station_id``,
# ``format_id``, … (the lookbehind rejects a preceding word character).
_ID_KEYED_DML_RE = re.compile(
    r"^\s*(?:UPDATE|DELETE)\b.*\bWHERE\b.*(?<!\w)id\s*(?:=\s*(?:%s|%\(\w+\)s)"
    r"|\bIN\s*(?:%s|%\(\w+\)s|\())",
    re.IGNORECASE | re.DOTALL,
)

_reported_id_keyed: set = set()


def _is_id_keyed_dml(query: Any) -> bool:
    """True if ``query`` is an UPDATE/DELETE keyed on a bare surrogate ``id``."""
    if not isinstance(query, str):
        # psycopg2.sql.Composed and friends — render for inspection only.
        try:
            query = str(query)
        except Exception:
            return False
    return bool(_ID_KEYED_DML_RE.match(query))


# ── Dual-write wrappers ───────────────────────────────────────────────────────


class _DualCursor:
    """Cursor wrapper that executes writes on both primary and mirror.

    Reads (fetch*) only return results from the primary.
    Mirror failures are logged but never raised.

    **id-keyed writes are not fanned out.**  See :data:`_ID_KEYED_DML_RE`: an
    ``UPDATE/DELETE … WHERE id = %s`` addresses a different row on the mirror,
    so the mirror leg is skipped (loudly) rather than executed.  Callers that
    need such a write to reach both hosts must key it on a natural key instead.
    """

    def __init__(self, primary: Any, mirror: Any, mirror_host: str) -> None:
        self._primary = primary
        self._mirror = mirror
        self._mirror_host = mirror_host

    # ── Writes: execute on both ────────────────────────────────────────────

    def _refuse_id_keyed(self, query: Any) -> bool:
        """Log + count an id-keyed write; True when the mirror must be skipped."""
        if not _is_id_keyed_dml(query):
            return False
        MirrorMetrics.incr("id_keyed_writes_not_mirrored")
        key = " ".join(str(query).split())[:160]
        if key not in _reported_id_keyed:
            _reported_id_keyed.add(key)
            logger.error(
                "Mirror %s: refusing to fan out an id-keyed write (surrogate ids "
                "differ per host — it would hit an unrelated row). The mirror will "
                "DIVERGE for this statement; rewrite it on a natural key. SQL: %s",
                self._mirror_host,
                key,
            )
        return True

    def execute(self, query: Any, params: Any = None) -> None:
        self._primary.execute(query, params)
        if self._refuse_id_keyed(query):
            return
        try:
            self._mirror.execute(query, params)
        except Exception as exc:
            MirrorMetrics.incr("execute_failures")
            logger.warning("Mirror %s execute failed: %s", self._mirror_host, exc)
            try:
                self._mirror.connection.rollback()
            except Exception:
                pass

    def executemany(self, query: Any, params_seq: Any) -> None:
        self._primary.executemany(query, params_seq)
        if self._refuse_id_keyed(query):
            return
        try:
            self._mirror.executemany(query, params_seq)
        except Exception as exc:
            MirrorMetrics.incr("executemany_failures")
            logger.warning("Mirror %s executemany failed: %s", self._mirror_host, exc)
            try:
                self._mirror.connection.rollback()
            except Exception:
                pass

    # ── Reads: primary only ────────────────────────────────────────────────

    def fetchone(self) -> Any:
        return self._primary.fetchone()

    def fetchall(self) -> Any:
        return self._primary.fetchall()

    def fetchmany(self, size: Optional[int] = None) -> Any:
        return self._primary.fetchmany(size) if size else self._primary.fetchmany()

    # ── Properties from primary ────────────────────────────────────────────

    @property
    def rowcount(self) -> int:
        return self._primary.rowcount  # type: ignore[no-any-return]

    @property
    def description(self) -> Any:
        return self._primary.description

    @property
    def statusmessage(self) -> Any:
        return self._primary.statusmessage

    # ── Context manager ────────────────────────────────────────────────────

    def __enter__(self) -> "_DualCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._primary.close()
        try:
            self._mirror.close()
        except Exception:
            pass

    def __iter__(self) -> Any:
        return iter(self._primary)


class _DualConnection:
    """Connection wrapper that commits/rolls back on both primary and mirror.

    Transparent drop-in for psycopg2 connections.  Mirror failures are
    logged but never propagated — the primary is always authoritative.
    """

    #: Attributes stored on the wrapper itself; everything else is delegated.
    _OWN_ATTRS = frozenset({"_primary", "_mirror", "_mirror_host"})

    def __init__(self, primary: Any, mirror: Any, mirror_host: str) -> None:
        self._primary = primary
        self._mirror = mirror
        self._mirror_host = mirror_host

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        primary_cur = self._primary.cursor(*args, **kwargs)
        try:
            mirror_cur = self._mirror.cursor(*args, **kwargs)
        except Exception as exc:
            MirrorMetrics.incr("cursor_failures")
            logger.warning(
                "Mirror %s cursor failed, degrading to primary-only: %s",
                self._mirror_host,
                exc,
            )
            return primary_cur
        return _DualCursor(primary_cur, mirror_cur, self._mirror_host)

    def commit(self) -> None:
        self._primary.commit()
        try:
            self._mirror.commit()
        except Exception as exc:
            MirrorMetrics.incr("commit_failures")
            logger.warning("Mirror %s commit failed: %s", self._mirror_host, exc)

    def rollback(self) -> None:
        self._primary.rollback()
        try:
            self._mirror.rollback()
        except Exception:
            pass

    def close(self) -> None:
        self._primary.close()
        try:
            self._mirror.close()
        except Exception:
            pass

    # Delegate anything else (autocommit, notices, etc.) to primary.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Delegate attribute *writes* to both legs.

        Without this, ``conn.autocommit = True`` silently landed in the
        wrapper's ``__dict__`` and the real psycopg2 connections stayed in
        their default mode — so every session-level ``SET`` the caller thought
        it had applied (``statement_timeout``, ``lock_timeout``) sat in an
        uncommitted implicit transaction.  A no-op safety setting is worse than
        no safety setting, because it reads as protection.
        """
        if name in _DualConnection._OWN_ATTRS:
            object.__setattr__(self, name, value)
            return
        setattr(self._primary, name, value)
        try:
            setattr(self._mirror, name, value)
        except Exception as exc:
            MirrorMetrics.incr("setattr_failures")
            logger.warning(
                "Mirror %s: setting %s failed: %s", self._mirror_host, name, exc
            )


# ── Factory ────────────────────────────────────────────────────────────────────


class DatabaseConnectionFactory:
    """Centralized PostgreSQL connection management.

    Connection parameters are resolved in priority order:
        1. Environment variables (POSTGRES_HOST, POSTGRES_PORT, etc.)
        2. Config file (~/.config/gpsconfig/database.cfg [postgresql] section)
        3. Built-in defaults (localhost:5432/gps_health)

    To switch databases, edit database.cfg and comment/uncomment the host line.
    Environment variables override the config file for CI/scripting use.

    Dual-write mode:
        Set ``mirror_host`` in the ``[postgresql]`` section of database.cfg to
        replicate every write to a second database host.  Remove or comment out
        ``mirror_host`` to disable.

    Environment Variables:
        POSTGRES_HOST: Database host
        POSTGRES_PORT: Database port
        POSTGRES_DB: Database name
        POSTGRES_USER: Database user
        POSTGRES_PASSWORD: Database password
    """

    # Mirror retry cooldown: after a failure, skip mirror for 1 hour then retry.
    # Prevents permanent mirror disable in the long-running scheduler.
    _mirror_failed_until: float = 0.0

    @classmethod
    def get_connection_params(cls, database: Optional[str] = None) -> Dict[str, str]:
        """Get connection parameters from config file and environment.

        Priority: env var > database.cfg > built-in default.

        Args:
            database: Override database name. If None, uses env/config/default.

        Returns:
            Dict with host, port, database, user, password keys.
        """
        cfg = _load_config_file()

        params = {
            "host": os.getenv("POSTGRES_HOST", cfg.get("host", "localhost")),
            "port": os.getenv("POSTGRES_PORT", cfg.get("port", "5432")),
            "database": database
            or os.getenv("POSTGRES_DB", cfg.get("database", "gps_health")),
            "user": os.getenv(
                "POSTGRES_USER",
                cfg.get("user", os.getenv("USER", "postgres")),
            ),
            "password": os.getenv("POSTGRES_PASSWORD", cfg.get("password", "")),
            # Bound every connect so a degraded remote host (e.g. the pgdev mirror
            # over a flaky VPN) fails FAST instead of hanging. Without this, the
            # routine catalog fan-out on the hot push path — which opens a mirror
            # connection per push under a bounded semaphore — could stall the
            # daily downloads waiting on libpq's default (indefinite) connect.
            # Best-effort fan-out only catches connect *errors*; a hang needs a
            # ceiling. Localhost connects are instant, so this is a no-op there.
            "connect_timeout": os.getenv(
                "POSTGRES_CONNECT_TIMEOUT", cfg.get("connect_timeout", "10")
            ),
        }

        # Server-side ceilings on EVERY app connection. Without them a single
        # runaway plan pins the server indefinitely — exactly the 2026-05-27
        # cartesian-join incident, where IT had to kill the query by hand.
        # The defaults are deliberately generous (a materialized-view refresh
        # over 8.5M catalog rows is legitimately slow); they exist to bound
        # "forever", not to police normal work. Set either to 0 to disable.
        options = cls._timeout_options(cfg)
        if options:
            params["options"] = options

        return params

    @staticmethod
    def _timeout_options(cfg: Dict[str, str]) -> str:
        """Build the libpq ``options`` string for statement/lock timeouts."""
        statement_timeout = os.getenv(
            "POSTGRES_STATEMENT_TIMEOUT", cfg.get("statement_timeout", "600s")
        )
        lock_timeout = os.getenv(
            "POSTGRES_LOCK_TIMEOUT", cfg.get("lock_timeout", "30s")
        )
        parts = []
        for name, value in (
            ("statement_timeout", statement_timeout),
            ("lock_timeout", lock_timeout),
        ):
            value = (value or "").strip()
            if value and value not in ("0", "0s", "off"):
                parts.append(f"-c {name}={value}")
        return " ".join(parts)

    @classmethod
    def get_connection_params_for_host(
        cls, host: Optional[str], database: Optional[str] = None
    ) -> Dict[str, str]:
        """Resolve connection params for a SPECIFIC host.

        ``database.cfg`` is the single source of truth for *how each host is
        reached*, so every code path that targets a particular server — the
        mirror writer, a ``--catalog-prod`` reindex, a CLI ``host_override`` —
        resolves the same credentials for the same host, with no ambiguity:

        * ``host`` is the primary ``[postgresql] host`` (or ``None``) →
          primary ``user`` / ``password``.
        * ``host`` is the configured ``mirror_host`` → ``mirror_user``
          (+ ``mirror_password`` if set, else the primary password is dropped
          so libpq consults ``~/.pgpass`` — the conventional way to supply a
          separate mirror credential without checking it in).
        * ``host`` is neither → primary credentials with the host swapped,
          plus a WARNING: that host's access identity is not declared in
          database.cfg, so it is a guess.
        """
        params = cls.get_connection_params(database)
        if not host or host == params["host"]:
            return params

        cfg = _load_config_file()
        if host == cfg.get("mirror_host"):
            host_params = {**params, "host": host}
            mirror_user = cfg.get("mirror_user")
            if mirror_user:
                host_params["user"] = mirror_user
            mirror_password = cfg.get("mirror_password")
            if mirror_password is not None:
                host_params["password"] = mirror_password
            elif mirror_user and mirror_user != params.get("user"):
                # Primary's password is meaningless for a different user on a
                # different server — drop it so ~/.pgpass is consulted.
                host_params.pop("password", None)
            return host_params

        logger.warning(
            "get_connection_params_for_host: %s is neither the primary nor the "
            "mirror_host in database.cfg — falling back to primary credentials "
            "(user=%s). Its access identity is not declared; add it as "
            "mirror_host (or extend the config) to make this unambiguous.",
            host,
            params.get("user"),
        )
        return {**params, "host": host}

    @classmethod
    def connect_to_host(cls, host: str, database: Optional[str] = None) -> Any:
        """Open a single (non-mirrored) connection to a specific host, using
        that host's database.cfg-declared credentials.

        Used by CLI ``host_override`` paths (e.g. per-catalog-host reindex)
        that must target one server explicitly — no dual-write wrapper.
        """
        import psycopg2

        return psycopg2.connect(**cls.get_connection_params_for_host(host, database))

    @classmethod
    def _build_mirror_params(cls, database: Optional[str] = None) -> Optional[tuple]:
        """Resolve (mirror_host, mirror_params) or None if no mirror configured.

        Shared by both the direct mirror connection (``_get_mirror_connection``)
        and the mirror pool.  Returns None when ``mirror_host`` is unset or would
        mirror to the primary host itself. Credential resolution is delegated to
        :meth:`get_connection_params_for_host` so the mirror writer and every
        ``host_override`` path agree on how ``mirror_host`` is reached.
        """
        cfg = _load_config_file()
        mirror_host = cfg.get("mirror_host")
        if not mirror_host:
            return None

        if mirror_host == cls.get_connection_params(database)["host"]:
            return None  # Don't mirror to self

        return mirror_host, cls.get_connection_params_for_host(mirror_host, database)

    @classmethod
    def _get_mirror_connection(cls, database: Optional[str] = None) -> Optional[Any]:
        """Create a mirror connection if mirror_host is configured.

        Returns None if no mirror is configured or connection fails.
        After a failure, retries after 1 hour (not permanently disabled).
        """
        if cls._mirror_failed_until > _time.monotonic():
            return None

        built = cls._build_mirror_params(database)
        if built is None:
            return None
        mirror_host, mirror_params = built

        import psycopg2

        try:
            conn = psycopg2.connect(**mirror_params)
            logger.info("Mirror connection established -> %s", mirror_host)
            return conn
        except Exception as exc:
            cls._mirror_failed_until = _time.monotonic() + 3600  # retry after 1 hour
            MirrorMetrics.incr("connect_failures")
            logger.warning(
                "Mirror connection to %s failed (will retry after 1h): %s",
                mirror_host,
                exc,
            )
            return None

    @classmethod
    def _primary_pool(cls, database: Optional[str] = None) -> Any:
        """Lazily create/return the ThreadedConnectionPool for the primary DB."""
        key = cls.get_connection_params(database)["database"]
        pool = _primary_pools.get(key)
        if pool is None:
            with _pool_lock:
                pool = _primary_pools.get(key)
                if pool is None:
                    from psycopg2 import pool as _pg_pool

                    params = cls.get_connection_params(database)
                    pool = _pg_pool.ThreadedConnectionPool(1, _MAX_POOL_CONN, **params)
                    _primary_pools[key] = pool
        return pool

    @classmethod
    def _mirror_pool(cls, database: Optional[str] = None) -> Optional[Any]:
        """Lazily create/return the mirror pool, or None if unconfigured/failed.

        Honours the same 1-hour failure cooldown as ``_get_mirror_connection``.
        """
        if cls._mirror_failed_until > _time.monotonic():
            return None
        built = cls._build_mirror_params(database)
        if built is None:
            return None
        mirror_host, mirror_params = built

        key = cls.get_connection_params(database)["database"]
        pool = _mirror_pools.get(key)
        if pool is None:
            with _pool_lock:
                pool = _mirror_pools.get(key)
                if pool is None:
                    from psycopg2 import pool as _pg_pool

                    try:
                        pool = _pg_pool.ThreadedConnectionPool(
                            1, _MAX_POOL_CONN, **mirror_params
                        )
                        _mirror_pools[key] = pool
                        logger.info("Mirror pool established -> %s", mirror_host)
                    except Exception as exc:
                        cls._mirror_failed_until = _time.monotonic() + 3600
                        MirrorMetrics.incr("pool_failures")
                        logger.warning(
                            "Mirror pool to %s failed (will retry after 1h): %s",
                            mirror_host,
                            exc,
                        )
                        return None
        return pool

    @staticmethod
    def _checkout(pool: Any, ping: bool = True) -> Any:
        """Borrow a live connection from ``pool``, discarding dead ones.

        Pre-pings up to 3 times; a dead connection is closed (freeing a pool
        slot so ``getconn`` mints a fresh one) rather than handed to the caller.
        """
        import psycopg2

        last_exc: Optional[Exception] = None
        for _ in range(3):
            conn = pool.getconn()
            if not ping or _ping(conn):
                return conn
            try:
                pool.putconn(conn, close=True)  # discard dead connection
            except Exception as exc:
                last_exc = exc
        raise psycopg2.OperationalError(
            f"no live pooled connection available ({last_exc})"
        )

    @staticmethod
    def _return(pool: Any, conn: Any, discard: bool = False) -> None:
        """Return a connection to ``pool`` (or discard it if poisoned)."""
        try:
            pool.putconn(conn, close=discard)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    @classmethod
    def get_connection(
        cls,
        database: Optional[str] = None,
        connection_string: Optional[str] = None,
        single_host: bool = False,
    ) -> Connection:
        """Get a new database connection.

        If ``mirror_host`` is configured in database.cfg, returns a
        _DualConnection that writes to both primary and mirror.

        Args:
            database: Override database name.
            connection_string: Full connection string (overrides env vars).
            single_host: Never wrap in a _DualConnection — return a plain
                connection to the primary only.  Required for destructive DDL
                (``receivers db``), id-keyed writes, read-only sessions, and
                anything that must be transactional on exactly one host.

        Returns:
            psycopg2 connection object (or _DualConnection wrapper).

        Raises:
            ImportError: If psycopg2 is not installed.
            psycopg2.OperationalError: If primary connection fails.
        """
        import psycopg2

        if connection_string:
            return psycopg2.connect(dsn=connection_string)

        params = cls.get_connection_params(database)
        primary = psycopg2.connect(**params)

        if single_host:
            return primary

        mirror = cls._get_mirror_connection(database)
        if mirror:
            cfg = _load_config_file()
            return _DualConnection(primary, mirror, cfg["mirror_host"])

        return primary

    @classmethod
    @contextmanager
    def connection(
        cls,
        database: Optional[str] = None,
        connection_string: Optional[str] = None,
        single_host: bool = False,
    ) -> Generator[Connection, None, None]:
        """Context manager for safe connection lifecycle.

        Commits on success, rolls back on exception.  Connections are borrowed
        from a per-database pool (pre-pinged for liveness) and returned on exit
        rather than opened/closed per call; a connection whose operation raised
        is discarded instead of returned.  If mirror_host is configured, both
        databases are committed/rolled back.

        Uses a bounded semaphore to limit concurrent connections and prevent
        PostgreSQL ``max_connections`` exhaustion under parallel downloads.

        Args:
            database: Override database name.
            connection_string: Full connection string (overrides env vars).
            single_host: Never wrap in a _DualConnection (see
                :meth:`get_connection`).  The pooled primary is still used.

        Yields:
            psycopg2 connection object (or _DualConnection wrapper).
        """
        _conn_semaphore.acquire()
        try:
            # Full-DSN path is rare and one-off — not pooled.
            if connection_string:
                conn = cls.get_connection(connection_string=connection_string)
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
                return

            primary_pool = cls._primary_pool(database)
            mirror_pool = None if single_host else cls._mirror_pool(database)
            primary = None
            mirror = None
            poisoned = False
            try:
                primary = cls._checkout(primary_pool, ping=True)
                if mirror_pool is not None:
                    try:
                        mirror = cls._checkout(mirror_pool, ping=True)
                    except Exception as exc:
                        MirrorMetrics.incr("checkout_failures")
                        logger.warning(
                            "Mirror checkout failed, degrading to primary-only: %s",
                            exc,
                        )
                        mirror = None

                if mirror is not None:
                    mirror_host = _load_config_file()["mirror_host"]
                    conn = _DualConnection(primary, mirror, mirror_host)
                else:
                    conn = primary

                try:
                    yield conn
                    conn.commit()
                except Exception:
                    # A failed op may leave the connection mid-transaction —
                    # discard it rather than return a poisoned connection.
                    poisoned = True
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
            finally:
                if primary is not None:
                    cls._return(primary_pool, primary, discard=poisoned)
                if mirror is not None:
                    cls._return(mirror_pool, mirror, discard=poisoned)
        finally:
            _conn_semaphore.release()
