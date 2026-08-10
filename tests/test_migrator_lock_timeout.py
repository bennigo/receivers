"""The migration connection must never wait unboundedly for a lock.

`Migrator._get_conn()` used to run `SET lock_timeout = 0`, on the reasoning
that DDL legitimately waits for ACCESS EXCLUSIVE while the scheduler writes.
It does -- but an unbounded AEL *request* parks at the head of the lock queue,
and from that moment every later query on the table queues behind it, readers
included. On pgdev, which is shared with other teams, that turns a routine
index swap into a server-wide freeze.

Each migration file runs in its own transaction, so a lock timeout rolls that
file back cleanly and the operator retries. "Retry later" beats "wedge the
shared box".
"""

from unittest.mock import MagicMock, patch

from receivers.db.migrator import Migrator


def _conn_with_recorder():
    """A mock connection that records every statement its cursor executes."""
    executed: list[tuple] = []
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = lambda s, *exc: False
    cur.execute = lambda sql, params=None: executed.append((sql, params))
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, executed


def _settings(executed):
    return {sql.split("=")[0].strip(): params for sql, params in executed}


def test_lock_timeout_is_bounded_by_default():
    conn, executed = _conn_with_recorder()
    with patch("receivers.db.migrator.get_connection", return_value=conn):
        Migrator()._get_conn()

    stmts = [sql for sql, _ in executed]
    assert "SET statement_timeout = 0" in stmts, "long DDL must not be capped"

    lock = [(sql, params) for sql, params in executed if "lock_timeout" in sql]
    assert lock, "lock_timeout must be set explicitly"
    sql, params = lock[0]
    assert params == ("5s",), "an unbounded lock wait head-of-line blocks the table"
    assert "= 0" not in sql


def test_lock_timeout_is_overridable():
    """A dedicated host may legitimately want the old unbounded behaviour."""
    conn, executed = _conn_with_recorder()
    with (
        patch("receivers.db.migrator.get_connection", return_value=conn),
        patch.dict("os.environ", {"MIGRATION_LOCK_TIMEOUT": "30s"}),
    ):
        Migrator()._get_conn()

    lock = [params for sql, params in executed if "lock_timeout" in sql]
    assert lock == [("30s",)]


def test_migration_connection_never_fans_out_to_the_mirror():
    """DDL must stay single-host -- a mirrored migration is a silent divergence."""
    conn, _ = _conn_with_recorder()
    with patch("receivers.db.migrator.get_connection", return_value=conn) as get_conn:
        Migrator(host_override="pgdev.vedur.is")._get_conn()

    assert get_conn.call_args.kwargs["single_host"] is True
