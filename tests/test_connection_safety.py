"""Connection-safety guards for the dual-write (mirror) connection layer.

Regression tests for the Fable-review Group 1 findings. The shared root cause:
``DatabaseConnectionFactory`` hands a ``_DualConnection`` — which fans every
write out to the pgdev mirror — to callers that assume a single, id-stable,
read-only or transactional connection. Concretely, before these guards:

* **S1** ``receivers db setup`` ran ``DROP SCHEMA public CASCADE`` on the mirror
  too, with no prompt, because the confirmation only fired when ``--host`` was
  passed *explicitly* — and the dangerous case is ``--host`` omitted.
* **S2** ``UPDATE … WHERE id = %s`` fanned to the mirror, where that serial id
  belongs to an unrelated row (the mirror holds a fraction of the primary's).
* **S3** ``health-query`` accepted multi-statement SQL (the EXPLAIN gate only
  plans the FIRST statement) and its ``conn.autocommit = True`` was silently
  swallowed by the wrapper, so both session timeouts sat uncommitted.
* **S7** no ``statement_timeout``/``lock_timeout`` bounded an app connection.
"""

import os
from unittest.mock import MagicMock, patch

from receivers.cli.db import LOCAL_HOSTS, resolve_db_host
from receivers.cli.health_query import is_multi_statement, strip_sql_noise
from receivers.health.database_factory import (
    DatabaseConnectionFactory,
    MirrorMetrics,
    _DualConnection,
    _DualCursor,
    _is_id_keyed_dml,
)

MIRROR_CFG = {"mirror_host": "pgdev.example.com"}


def _live_conn():
    conn = MagicMock()
    conn.closed = 0
    return conn


class _FakePool:
    def __init__(self, conns=None):
        self._conns = list(conns) if conns else []
        self.returned = []

    def getconn(self):
        return self._conns.pop(0) if self._conns else _live_conn()

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))

    def closeall(self):
        pass


# ── S1: single-host connections ───────────────────────────────────────────────


class TestSingleHost:
    """``single_host=True`` must never produce a mirrored connection."""

    def test_get_connection_single_host_skips_mirror(self):
        primary = _live_conn()
        with (
            patch("psycopg2.connect", return_value=primary),
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value=MIRROR_CFG,
            ),
            patch.object(
                DatabaseConnectionFactory, "_get_mirror_connection"
            ) as mock_mirror,
        ):
            conn = DatabaseConnectionFactory.get_connection(single_host=True)

        assert conn is primary
        assert not isinstance(conn, _DualConnection)
        # The mirror is never even opened — not opened-then-discarded.
        mock_mirror.assert_not_called()

    def test_get_connection_default_still_mirrors(self):
        """The default must keep fanning out — production health writes rely on it."""
        primary, mirror = _live_conn(), _live_conn()
        with (
            patch("psycopg2.connect", return_value=primary),
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value=MIRROR_CFG,
            ),
            patch.object(
                DatabaseConnectionFactory,
                "_get_mirror_connection",
                return_value=mirror,
            ),
        ):
            conn = DatabaseConnectionFactory.get_connection()

        assert isinstance(conn, _DualConnection)

    def test_context_manager_single_host_skips_mirror_pool(self):
        primary = _live_conn()
        ppool = _FakePool([primary])
        with (
            patch.object(
                DatabaseConnectionFactory, "_primary_pool", return_value=ppool
            ),
            patch.object(DatabaseConnectionFactory, "_mirror_pool") as mock_mpool,
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value=MIRROR_CFG,
            ),
        ):
            with DatabaseConnectionFactory.connection(single_host=True) as conn:
                assert conn is primary
                assert not isinstance(conn, _DualConnection)
            mock_mpool.assert_not_called()

    def test_db_verbs_request_single_host(self):
        """Every ``receivers db`` connection must be single-host."""
        from receivers.cli import db as db_cli

        with patch("receivers.db.connection.get_connection") as mock_get:
            db_cli.db_connection(None)
        assert mock_get.call_args.kwargs["single_host"] is True


class TestDestructiveConfirmation:
    """The S1 prompt must key off the RESOLVED host, not the CLI arg."""

    def test_resolve_uses_config_host_when_arg_omitted(self):
        with patch.object(
            DatabaseConnectionFactory,
            "get_connection_params",
            return_value={"host": "rek-d01.vedur.is"},
        ):
            # host=None used to mean "no prompt"; it must resolve to the real target.
            assert resolve_db_host(None) == "rek-d01.vedur.is"

    def test_resolve_prefers_explicit_arg(self):
        assert resolve_db_host("pgdev.vedur.is") == "pgdev.vedur.is"

    def test_remote_default_host_now_prompts(self):
        """With no --host but a remote configured primary, confirmation is required."""
        from receivers.cli.db import confirm_destructive

        with (
            patch.object(
                DatabaseConnectionFactory,
                "get_connection_params",
                return_value={"host": "rek-d01.vedur.is"},
            ),
            patch("builtins.input", return_value="wrong") as mock_input,
        ):
            assert confirm_destructive("DROP + SETUP", None) is False
            mock_input.assert_called_once()

    def test_localhost_does_not_prompt(self):
        from receivers.cli.db import confirm_destructive

        for host in LOCAL_HOSTS:
            with patch("builtins.input", side_effect=AssertionError("prompted")):
                assert confirm_destructive("DROP + SETUP", host) is True


# ── S2: id-keyed writes must not fan out ──────────────────────────────────────


class TestIdKeyedWriteDetection:
    def test_detects_id_keyed_updates(self):
        assert _is_id_keyed_dml("UPDATE file_tracking SET status='x' WHERE id = %s")
        assert _is_id_keyed_dml("DELETE FROM archive_catalog WHERE id = %s")
        assert _is_id_keyed_dml(
            "UPDATE file_tracking SET format_id = %s "
            "WHERE id = %s AND format_id IS DISTINCT FROM %s"
        )
        assert _is_id_keyed_dml("update t set a=1 where id=%(row_id)s")
        assert _is_id_keyed_dml("UPDATE t SET a = 1 WHERE t.id = %s")

    def test_ignores_natural_key_writes(self):
        """Columns merely ENDING in ``id`` are natural keys — must still mirror."""
        assert not _is_id_keyed_dml(
            "UPDATE file_tracking SET status='removed' "
            "WHERE sid = %s AND session_type = %s AND file_date = %s"
        )
        assert not _is_id_keyed_dml(
            "UPDATE cfg_discrepancy SET resolved_at = NOW() "
            "WHERE station_id = %s AND cfg_key = %s AND resolved_at IS NULL"
        )
        assert not _is_id_keyed_dml(
            "UPDATE t SET format_id = %s WHERE format_id IS DISTINCT FROM %s"
        )

    def test_ignores_reads_and_inserts(self):
        assert not _is_id_keyed_dml("SELECT id FROM file_tracking WHERE id = %s")
        assert not _is_id_keyed_dml("INSERT INTO t (id) VALUES (%s)")


class TestDualCursorIdKeyedGuard:
    def test_id_keyed_write_hits_primary_only(self):
        MirrorMetrics.reset()
        primary, mirror = MagicMock(), MagicMock()
        cur = _DualCursor(primary, mirror, "pgdev.example.com")

        sql = "UPDATE file_tracking SET status = 'removed' WHERE id = %s"
        cur.execute(sql, (42,))

        primary.execute.assert_called_once_with(sql, (42,))
        # The mirror's row 42 is a DIFFERENT file — never touch it.
        mirror.execute.assert_not_called()
        assert MirrorMetrics.snapshot()["id_keyed_writes_not_mirrored"] == 1

    def test_natural_key_write_still_fans_out(self):
        primary, mirror = MagicMock(), MagicMock()
        cur = _DualCursor(primary, mirror, "pgdev.example.com")

        sql = "UPDATE file_tracking SET status = 'removed' WHERE sid = %s"
        cur.execute(sql, ("ELDC",))

        primary.execute.assert_called_once_with(sql, ("ELDC",))
        mirror.execute.assert_called_once_with(sql, ("ELDC",))

    def test_executemany_guard(self):
        MirrorMetrics.reset()
        primary, mirror = MagicMock(), MagicMock()
        cur = _DualCursor(primary, mirror, "pgdev.example.com")

        cur.executemany("DELETE FROM t WHERE id = %s", [(1,), (2,)])

        primary.executemany.assert_called_once()
        mirror.executemany.assert_not_called()
        assert MirrorMetrics.snapshot()["id_keyed_writes_not_mirrored"] == 1

    def test_mirror_failure_is_counted_not_just_logged(self):
        MirrorMetrics.reset()
        primary, mirror = MagicMock(), MagicMock()
        mirror.execute.side_effect = RuntimeError("mirror down")
        cur = _DualCursor(primary, mirror, "pgdev.example.com")

        cur.execute("UPDATE t SET a = 1 WHERE sid = %s", ("ELDC",))

        # Primary is authoritative — the failure must not propagate.
        primary.execute.assert_called_once()
        assert MirrorMetrics.snapshot()["execute_failures"] == 1


# ── S3: attribute delegation (the swallowed autocommit) ───────────────────────


class TestDualConnectionAttributeDelegation:
    def test_autocommit_reaches_both_legs(self):
        primary, mirror = MagicMock(), MagicMock()
        primary.autocommit = False
        mirror.autocommit = False
        conn = _DualConnection(primary, mirror, "pgdev.example.com")

        conn.autocommit = True

        # Before the __setattr__ fix this landed in the wrapper's __dict__ and
        # the real connections stayed in transactional mode — so every session
        # SET the caller "applied" was never committed.
        assert primary.autocommit is True
        assert mirror.autocommit is True

    def test_own_attributes_stay_on_the_wrapper(self):
        primary, mirror = MagicMock(), MagicMock()
        conn = _DualConnection(primary, mirror, "pgdev.example.com")
        assert conn._primary is primary
        assert conn._mirror is mirror
        assert conn._mirror_host == "pgdev.example.com"

    def test_mirror_setattr_failure_does_not_propagate(self):
        MirrorMetrics.reset()

        class _Strict:
            def __setattr__(self, name, value):
                raise RuntimeError("mirror closed")

        primary = MagicMock()
        conn = _DualConnection(primary, _Strict(), "pgdev.example.com")
        conn.autocommit = True

        assert primary.autocommit is True
        assert MirrorMetrics.snapshot()["setattr_failures"] == 1


# ── S3: health-query gate ─────────────────────────────────────────────────────


class TestHealthQueryMultiStatement:
    def test_rejects_second_statement(self):
        """The EXPLAIN gate only plans the first statement."""
        assert is_multi_statement("SELECT 1; DELETE FROM stations")
        assert is_multi_statement("SELECT 1;\nDROP TABLE stations")

    def test_allows_single_statement_with_trailing_semicolon(self):
        assert not is_multi_statement("SELECT count(*) FROM stations;")
        assert not is_multi_statement("SELECT count(*) FROM stations")

    def test_semicolon_inside_a_literal_is_not_a_separator(self):
        assert not is_multi_statement("SELECT * FROM t WHERE name = 'a;b'")
        assert not is_multi_statement("SELECT 1 -- trailing ; comment")

    def test_strip_sql_noise_blanks_literals(self):
        assert ";" not in strip_sql_noise("SELECT 'a;b'")
        assert ";" not in strip_sql_noise("SELECT 1 /* x ; y */")


# ── S7: statement/lock timeouts on every connection ───────────────────────────


class TestTimeoutOptions:
    def test_defaults_bound_every_connection(self):
        with (
            patch(
                "receivers.health.database_factory._load_config_file", return_value={}
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            params = DatabaseConnectionFactory.get_connection_params()

        assert "statement_timeout=" in params["options"]
        assert "lock_timeout=" in params["options"]

    def test_config_overrides(self):
        with (
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value={"statement_timeout": "90s", "lock_timeout": "3s"},
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            params = DatabaseConnectionFactory.get_connection_params()

        assert params["options"] == "-c statement_timeout=90s -c lock_timeout=3s"

    def test_env_overrides_config(self):
        with (
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value={"statement_timeout": "90s"},
            ),
            patch.dict(os.environ, {"POSTGRES_STATEMENT_TIMEOUT": "5min"}, clear=True),
        ):
            params = DatabaseConnectionFactory.get_connection_params()

        assert "statement_timeout=5min" in params["options"]

    def test_migrations_are_exempt(self):
        """DDL on a live 8.5M-row table must not abort at the app-wide ceiling."""
        from receivers.db.migrator import Migrator

        conn = MagicMock()
        with patch("receivers.db.migrator.get_connection", return_value=conn):
            assert Migrator()._get_conn() is conn

        executed = [c.args[0] for c in conn.cursor().__enter__().execute.call_args_list]
        assert "SET statement_timeout = 0" in executed
        assert "SET lock_timeout = 0" in executed

    def test_zero_disables(self):
        with (
            patch(
                "receivers.health.database_factory._load_config_file",
                return_value={"statement_timeout": "0", "lock_timeout": "0"},
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            params = DatabaseConnectionFactory.get_connection_params()

        assert "options" not in params


# ── Natural-key predicates must stay index-able ───────────────────────────────


class TestNaturalKeyPredicates:
    """The S2 rewrites must not trade a PK lookup for a scan.

    ``IS NOT DISTINCT FROM`` is not a btree-indexable operator, and the unique
    indexes on the file_tracking grain are PARTIAL (``WHERE file_hour IS NULL``
    / ``IS NOT NULL``). Both cases need the predicate spelled out as ``IS NULL``
    or ``= %s``, chosen in Python — otherwise the planner demotes the column
    from Index Cond to Filter and the rewrite costs a near-full index scan per
    row on an 8.5M-row table.
    """

    def test_verify_last_verified_update_is_indexable(self):
        import inspect

        from receivers.archive.verify import verify_archive_catalog

        src = inspect.getsource(verify_archive_catalog)
        assert "session_type IS NOT DISTINCT FROM" not in src
        assert "session_type IS NULL" in src
        assert "session_type = %s" in src

    def test_scan_rinex_format_update_is_indexable(self):
        import inspect

        from receivers.health.file_tracker import GapDetector

        src = inspect.getsource(GapDetector.scan_rinex_files)
        assert "file_hour IS NOT DISTINCT FROM" not in src
        # Conditional predicate: "IS NULL" or "= %s", chosen in Python.
        assert 'hour_pred="IS NULL"' in src
        assert 'hour_pred="= %s"' in src


class TestMirrorMetricsReporting:
    """The counters need a reader, or they are as invisible as the warnings."""

    def test_reports_only_deltas(self, caplog):
        import logging as _logging

        from receivers.scheduling import bulk_scheduler

        bulk_scheduler._MIRROR_METRICS_LAST.clear()
        MirrorMetrics.reset()

        with caplog.at_level(_logging.WARNING, logger="receivers.scheduler"):
            bulk_scheduler._mirror_metrics_job()
            assert "mirror dual-write" not in caplog.text  # healthy → silent

            MirrorMetrics.incr("execute_failures", 3)
            caplog.clear()
            bulk_scheduler._mirror_metrics_job()
            assert "execute_failures=3" in caplog.text

            caplog.clear()
            bulk_scheduler._mirror_metrics_job()
            assert "mirror dual-write" not in caplog.text  # no new failures
