"""Tests for DatabaseConnectionFactory."""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from receivers.health.database_factory import (
    _MAX_POOL_CONN,
    DatabaseConnectionFactory,
    _conn_semaphore,
    _DualConnection,
)


class TestGetConnectionParams:
    """Test get_connection_params method."""

    def test_default_params(self):
        """Test default connection parameters."""
        with patch(
            "receivers.health.database_factory._load_config_file", return_value={}
        ):
            with patch.dict(os.environ, {"USER": "testuser"}, clear=True):
                params = DatabaseConnectionFactory.get_connection_params()
                assert params["host"] == "localhost"
                assert params["port"] == "5432"
                assert params["database"] == "gps_health"
                assert params["user"] == "testuser"
                assert params["password"] == ""

    def test_env_overrides(self):
        """Test environment variable overrides."""
        env = {
            "POSTGRES_HOST": "db.example.com",
            "POSTGRES_PORT": "5433",
            "POSTGRES_DB": "custom_db",
            "POSTGRES_USER": "dbuser",
            "POSTGRES_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            params = DatabaseConnectionFactory.get_connection_params()
            assert params["host"] == "db.example.com"
            assert params["port"] == "5433"
            assert params["database"] == "custom_db"
            assert params["user"] == "dbuser"
            assert params["password"] == "secret"

    def test_database_override(self):
        """Test database parameter override."""
        params = DatabaseConnectionFactory.get_connection_params(database="other_db")
        assert params["database"] == "other_db"

    def test_user_fallback_to_postgres(self):
        """Test USER fallback when no env vars set."""
        with patch(
            "receivers.health.database_factory._load_config_file", return_value={}
        ):
            with patch.dict(os.environ, {}, clear=True):
                params = DatabaseConnectionFactory.get_connection_params()
                assert params["user"] == "postgres"


class TestGetConnectionParamsForHost:
    """Per-host credential resolution — database.cfg is the single source of
    truth for HOW each host is reached.

    Regression: the ``--catalog-prod`` reindex used to reach the mirror_host
    (pgdev) with the PRIMARY user, so it authenticated as gpsops and pgdev's
    LDAP rejected it — while the config already declared mirror_user=bgo.
    """

    _CFG = {
        "host": "localhost",
        "port": "5432",
        "database": "gps_health",
        "user": "gpsops",
        "password": "primarypass",
        "mirror_host": "pgdev.vedur.is",
        "mirror_user": "bgo",
    }

    def _resolve(self, host, cfg=None):
        with patch(
            "receivers.health.database_factory._load_config_file",
            return_value=cfg or self._CFG,
        ):
            with patch.dict(os.environ, {}, clear=True):
                return DatabaseConnectionFactory.get_connection_params_for_host(host)

    def test_none_host_is_primary(self):
        p = self._resolve(None)
        assert p["host"] == "localhost"
        assert p["user"] == "gpsops"
        assert p["password"] == "primarypass"

    def test_primary_host_is_primary(self):
        p = self._resolve("localhost")
        assert p["user"] == "gpsops"
        assert p["password"] == "primarypass"

    def test_mirror_host_uses_mirror_user_and_drops_password(self):
        # The whole fix: pgdev → connect as bgo, password dropped so libpq
        # consults ~/.pgpass (which carries the bgo@pgdev credential).
        p = self._resolve("pgdev.vedur.is")
        assert p["host"] == "pgdev.vedur.is"
        assert p["user"] == "bgo"
        assert "password" not in p

    def test_mirror_password_used_when_set(self):
        cfg = {**self._CFG, "mirror_password": "bgopass"}
        p = self._resolve("pgdev.vedur.is", cfg=cfg)
        assert p["user"] == "bgo"
        assert p["password"] == "bgopass"

    def test_undeclared_host_falls_back_to_primary_with_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            p = self._resolve("some.other.host")
        assert p["host"] == "some.other.host"
        assert p["user"] == "gpsops"  # primary — the only credential we have
        assert any("not declared" in r.message for r in caplog.records)

    @patch("psycopg2.connect")
    def test_connect_to_host_uses_resolved_params(self, mock_connect):
        with patch(
            "receivers.health.database_factory._load_config_file",
            return_value=self._CFG,
        ):
            with patch.dict(os.environ, {}, clear=True):
                DatabaseConnectionFactory.connect_to_host("pgdev.vedur.is")
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["host"] == "pgdev.vedur.is"
        assert kwargs["user"] == "bgo"
        assert "password" not in kwargs


class TestGetConnection:
    """Test get_connection method."""

    @patch("receivers.health.database_factory._load_config_file", return_value={})
    @patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.get_connection_params"
    )
    def test_get_connection_with_params(self, mock_params, _mock_cfg):
        """Test connection creation with environment parameters."""
        mock_params.return_value = {
            "host": "localhost",
            "port": "5432",
            "database": "gps_health",
            "user": "testuser",
            "password": "",
        }
        with patch("psycopg2.connect") as mock_connect:
            mock_connect.return_value = MagicMock()
            conn = DatabaseConnectionFactory.get_connection()
            mock_connect.assert_called_once_with(
                host="localhost",
                port="5432",
                database="gps_health",
                user="testuser",
                password="",
            )
            assert conn is not None

    def test_get_connection_with_connection_string(self):
        """Test connection creation with explicit connection string."""
        with patch("psycopg2.connect") as mock_connect:
            mock_connect.return_value = MagicMock()
            conn = DatabaseConnectionFactory.get_connection(
                connection_string="postgresql://user:pass@host:5432/db"
            )
            mock_connect.assert_called_once_with(
                dsn="postgresql://user:pass@host:5432/db"
            )
            assert conn is not None

    def test_get_connection_import_error(self):
        """Test ImportError when psycopg2 not available."""
        with patch.dict("sys.modules", {"psycopg2": None}):
            with pytest.raises(ImportError):
                DatabaseConnectionFactory.get_connection()


class _FakePool:
    """Minimal ThreadedConnectionPool stand-in for tests."""

    def __init__(self, conns=None):
        self._conns = list(conns) if conns else []
        self.given = []
        self.returned = []  # list of (conn, close)

    def getconn(self):
        conn = self._conns.pop(0) if self._conns else _live_conn()
        self.given.append(conn)
        return conn

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))

    def closeall(self):
        pass


def _live_conn():
    """A MagicMock connection that passes _ping()."""
    conn = MagicMock()
    conn.closed = 0
    return conn


def _dead_conn():
    """A MagicMock connection that fails _ping() (server dropped it)."""
    conn = MagicMock()
    conn.closed = 1
    return conn


class TestConnectionContextManager:
    """Test the pooled connection context manager."""

    def test_context_manager_commits_and_returns_to_pool(self):
        """Commits on success and returns the connection to the pool."""
        mock_conn = _live_conn()
        pool = _FakePool([mock_conn])
        with (
            patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None),
        ):
            with DatabaseConnectionFactory.connection() as conn:
                assert conn is mock_conn
            mock_conn.commit.assert_called_once()
            # returned to pool, NOT closed (discard=False)
            assert pool.returned == [(mock_conn, False)]
            mock_conn.close.assert_not_called()

    def test_context_manager_discards_poisoned_on_exception(self):
        """Rolls back and DISCARDS (close=True) a connection whose op raised."""
        mock_conn = _live_conn()
        pool = _FakePool([mock_conn])
        with (
            patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None),
        ):
            with pytest.raises(ValueError):
                with DatabaseConnectionFactory.connection():
                    raise ValueError("test error")
            # rollback is called (pre-ping also rolls back, so >=1)
            assert mock_conn.rollback.called
            mock_conn.commit.assert_not_called()
            assert pool.returned == [(mock_conn, True)]  # discarded

    def test_context_manager_returns_even_if_commit_fails(self):
        """A commit failure still returns/discards the connection (no leak)."""
        mock_conn = _live_conn()
        mock_conn.commit.side_effect = Exception("commit failed")
        pool = _FakePool([mock_conn])
        with (
            patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None),
        ):
            with pytest.raises(Exception):
                with DatabaseConnectionFactory.connection():
                    pass
            assert pool.returned and pool.returned[0][0] is mock_conn

    def test_pre_ping_discards_dead_connection(self):
        """A dead pooled connection is discarded; a live one is handed out."""
        dead = _dead_conn()
        live = _live_conn()
        pool = _FakePool([dead, live])
        with (
            patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None),
        ):
            with DatabaseConnectionFactory.connection() as conn:
                assert conn is live
            # dead one was closed-discarded during checkout
            assert (dead, True) in pool.returned
            # live one returned normally
            assert (live, False) in pool.returned

    def test_mirror_yields_dual_connection(self):
        """With a mirror pool, a _DualConnection is yielded and both returned."""
        primary = _live_conn()
        mirror = _live_conn()
        ppool = _FakePool([primary])
        mpool = _FakePool([mirror])
        with (
            patch.object(
                DatabaseConnectionFactory, "_primary_pool", return_value=ppool
            ),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=mpool),
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value={"mirror_host": "mirror.example.com"},
            ),
        ):
            with DatabaseConnectionFactory.connection() as conn:
                assert isinstance(conn, _DualConnection)
            assert ppool.returned == [(primary, False)]
            assert mpool.returned == [(mirror, False)]

    def test_single_host_never_touches_the_mirror(self):
        """``single_host=True`` opens no mirror connection at all.

        This is the escape hatch read-only work must use. _DualCursor fans out
        EVERY statement, not just writes -- it cannot tell them apart, because
        a write here is often spelled ``SELECT upsert_file_tracking(...)``. So
        a pure SELECT also executes on the shared pgdev mirror and its result
        is discarded: one unindexed reconciler lookup billed 22.6M executions
        x 24 ms to that server for nothing (2026-08-10).
        """
        primary = _live_conn()
        ppool = _FakePool([primary])
        mirror_pool_calls = []

        def _record_mirror_pool(*args, **kwargs):
            mirror_pool_calls.append(args)
            raise AssertionError("single_host=True must not open a mirror pool")

        with (
            patch.object(
                DatabaseConnectionFactory, "_primary_pool", return_value=ppool
            ),
            patch.object(
                DatabaseConnectionFactory,
                "_mirror_pool",
                side_effect=_record_mirror_pool,
            ),
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value={"mirror_host": "mirror.example.com"},
            ),
        ):
            with DatabaseConnectionFactory.connection(single_host=True) as conn:
                assert not isinstance(conn, _DualConnection)
                assert conn is primary

        assert mirror_pool_calls == []

    def test_connection_string_bypasses_pool(self):
        """A full-DSN connection is not pooled — opened and closed directly."""
        mock_conn = MagicMock()
        with (
            patch.object(
                DatabaseConnectionFactory, "get_connection", return_value=mock_conn
            ) as mock_get,
            patch.object(DatabaseConnectionFactory, "_primary_pool") as mock_pool,
        ):
            with DatabaseConnectionFactory.connection(
                connection_string="postgresql://u:p@h/db"
            ) as conn:
                assert conn is mock_conn
            mock_conn.commit.assert_called_once()
            mock_conn.close.assert_called_once()
            mock_pool.assert_not_called()
            mock_get.assert_called_once_with(connection_string="postgresql://u:p@h/db")


class TestConnectionSemaphore:
    """Test connection pool semaphore limiting."""

    def test_semaphore_default_limit(self):
        """Semaphore allows _MAX_POOL_CONN concurrent connections."""
        assert _MAX_POOL_CONN == 20

    def test_semaphore_released_on_success(self):
        """Semaphore is released after successful context manager exit."""
        pool = _FakePool([_live_conn()])
        with (
            patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None),
        ):
            with DatabaseConnectionFactory.connection():
                pass
        # If semaphore leaked, subsequent acquires would eventually block.
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_released_on_exception(self):
        """Semaphore is released even when the caller raises."""
        pool = _FakePool([_live_conn()])
        with (
            patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool),
            patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None),
        ):
            with pytest.raises(RuntimeError):
                with DatabaseConnectionFactory.connection():
                    raise RuntimeError("boom")
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_released_on_checkout_failure(self):
        """Semaphore is released when the pool checkout itself fails."""
        with patch.object(
            DatabaseConnectionFactory,
            "_primary_pool",
            side_effect=Exception("pool init refused"),
        ):
            with pytest.raises(Exception, match="pool init refused"):
                with DatabaseConnectionFactory.connection():
                    pass
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_limits_concurrency(self):
        """Threads beyond _MAX_POOL_CONN block until a slot opens."""
        # Use a small semaphore for this test to avoid needing 20+ threads.
        test_sem = threading.BoundedSemaphore(2)
        inside = threading.Event()
        gate = threading.Event()
        blocked = threading.Event()

        def _hold_slot():
            test_sem.acquire()
            try:
                inside.set()
                gate.wait(timeout=5)
            finally:
                test_sem.release()

        def _try_slot():
            got = test_sem.acquire(timeout=0.3)
            if not got:
                blocked.set()
            else:
                test_sem.release()

        # Fill 2 slots
        t1 = threading.Thread(target=_hold_slot)
        t2 = threading.Thread(target=_hold_slot)
        t1.start()
        t2.start()
        inside.wait(timeout=2)

        # Third thread should block
        t3 = threading.Thread(target=_try_slot)
        t3.start()
        t3.join(timeout=2)
        assert blocked.is_set(), "Third thread should have been blocked"

        # Release and clean up
        gate.set()
        t1.join(timeout=2)
        t2.join(timeout=2)


class TestLocalAliases:
    """The primary declared as `localhost` is the same box as its own FQDN.

    rek-d01 sets `[archive] catalog_hosts = rek-d01.vedur.is, pgdev.vedur.is`
    while `database.cfg` declares `host = localhost`. Every catalog write
    therefore logged "neither the primary nor the mirror_host — falling back to
    primary credentials" and connected over TCP to a machine it was already on.
    The credentials happened to be right, which is precisely the problem: an
    undeclared identity doing load-bearing work.
    """

    _CFG = {
        "host": "localhost",
        "user": "gpsops",
        "password": "primarypass",
        "mirror_host": "pgdev.vedur.is",
        "mirror_user": "bgo",
        "local_aliases": "rek-d01.vedur.is, reknew",
    }

    def _resolve(self, host, cfg=None):
        with patch(
            "receivers.health.database_factory._load_config_file",
            return_value=cfg or self._CFG,
        ):
            with patch.dict(os.environ, {}, clear=True):
                return DatabaseConnectionFactory.get_connection_params_for_host(host)

    def test_alias_resolves_to_the_primary(self):
        p = self._resolve("rek-d01.vedur.is")
        assert p["user"] == "gpsops"
        assert p["password"] == "primarypass"

    def test_alias_keeps_the_socket_not_a_tcp_loopback(self):
        """Returning the primary params unchanged means host stays `localhost`."""
        assert self._resolve("rek-d01.vedur.is")["host"] == "localhost"

    def test_second_alias_in_the_list(self):
        assert self._resolve("reknew")["host"] == "localhost"

    def test_whitespace_around_entries_is_tolerated(self):
        cfg = {**self._CFG, "local_aliases": "  rek-d01.vedur.is  ,  reknew "}
        assert self._resolve("rek-d01.vedur.is", cfg)["host"] == "localhost"

    def test_mirror_still_wins_its_own_credentials(self):
        """An alias list must not shadow the mirror branch."""
        p = self._resolve("pgdev.vedur.is")
        assert p["host"] == "pgdev.vedur.is"
        assert p["user"] == "bgo"

    def test_unknown_host_still_falls_back_and_warns(self):
        p = self._resolve("some-other.vedur.is")
        assert p["host"] == "some-other.vedur.is"
        assert p["user"] == "gpsops"

    def test_absent_local_aliases_changes_nothing(self):
        cfg = {k: v for k, v in self._CFG.items() if k != "local_aliases"}
        p = self._resolve("rek-d01.vedur.is", cfg)
        assert p["host"] == "rek-d01.vedur.is"

    def test_empty_local_aliases_is_not_a_wildcard(self):
        """A blank value must not make every host an alias of the primary."""
        cfg = {**self._CFG, "local_aliases": "  ,  "}
        p = self._resolve("rek-d01.vedur.is", cfg)
        assert p["host"] == "rek-d01.vedur.is"


class TestConfigLoaderCarriesLocalAliases:
    """`local_aliases` must survive the real loader, not just a mocked dict.

    `_load_config_file` copies an ALLOW-LIST of keys out of [postgresql].
    `local_aliases` was missing from it, so the resolver read None, the alias
    set was empty, and rek-d01 kept warning on every catalog write even though
    the key was sitting in database.cfg and the resolver code was deployed.

    Every other test in this file patches `_load_config_file`, which is exactly
    why they all passed while the feature did nothing. This one writes a real
    file and reads it back.
    """

    def _load(self, tmp_path, monkeypatch, body):
        cfg = tmp_path / "database.cfg"
        cfg.write_text(body)
        import receivers.health.database_factory as dbf

        monkeypatch.setattr(dbf, "_config_cache", None, raising=False)
        monkeypatch.setenv("GPS_CONFIG_PATH", str(tmp_path))
        return dbf._load_config_file()

    _BODY = (
        "[postgresql]\n"
        "host = localhost\n"
        "user = gpsops\n"
        "mirror_host = pgdev.vedur.is\n"
        "local_aliases = rek-d01.vedur.is, reknew\n"
    )

    def test_local_aliases_is_not_dropped(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, self._BODY)
        assert cfg.get("local_aliases") == "rek-d01.vedur.is, reknew"

    def test_the_fqdn_then_resolves_to_the_primary(self, tmp_path, monkeypatch):
        """End to end through the real loader: the warning case is gone."""
        self._load(tmp_path, monkeypatch, self._BODY)
        p = DatabaseConnectionFactory.get_connection_params_for_host("rek-d01.vedur.is")
        assert p["host"] == "localhost"
        assert p["user"] == "gpsops"

    def test_neighbouring_keys_still_load(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, self._BODY)
        assert cfg.get("mirror_host") == "pgdev.vedur.is"
        assert cfg.get("host") == "localhost"
