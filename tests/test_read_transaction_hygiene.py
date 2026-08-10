"""A read on a long-lived connection must never leave a transaction open.

The bug this pins (found on rek-d01 2026-08-10): every WRITE method in
`file_tracker` commits or rolls back; no READ method did either. psycopg2
opens a transaction on the first statement -- a bare SELECT counts -- so a
long-lived connection that only ever reads parks in `idle in transaction`
forever. Two FormatResolver connections were measured holding one open for
68 and 59 minutes, with a constant trickle from is_file_missing.

Consequences, in order of severity:

  1. an open transaction pins the vacuum xmin horizon, so VACUUM cannot
     reclaim dead tuples on a table taking tens of millions of updates;
  2. CREATE/DROP INDEX CONCURRENTLY can never finish -- it waits for every
     transaction that could see the index, and there was always one open.
     Migration 065 died on lock_timeout six times because of this.

These tests assert the transaction is ENDED, not merely that the query ran.
"""

from unittest.mock import MagicMock

import pytest

from receivers.health.file_tracker import FileTracker, FormatResolver, read_only_cursor


class _Conn:
    """Minimal connection double that records transaction lifecycle."""

    def __init__(self, fetch=None):
        self.rollbacks = 0
        self.commits = 0
        self.executed: list = []
        self._fetch = fetch if fetch is not None else []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.executed.append((sql, params))

            def fetchone(self):
                return conn._fetch[0] if conn._fetch else None

            def fetchall(self):
                return conn._fetch

        return _Cur()

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_read_only_cursor_ends_the_transaction():
    conn = _Conn(fetch=[(1,)])
    with read_only_cursor(conn) as cur:
        cur.execute("SELECT 1")
    assert conn.rollbacks == 1, "a read must end its transaction"
    assert conn.commits == 0, "rollback, not commit -- never publish a stray write"


def test_read_only_cursor_ends_the_transaction_on_exception():
    """The cleanup is in a finally: a failed read must not leak either."""
    conn = _Conn()
    with pytest.raises(RuntimeError):
        with read_only_cursor(conn) as cur:
            cur.execute("SELECT 1")
            raise RuntimeError("boom")
    assert conn.rollbacks == 1


def test_cleanup_failure_does_not_mask_the_read():
    """A connection that cannot roll back must not turn a good read into an error."""
    conn = _Conn(fetch=[(True,)])
    conn.rollback = MagicMock(side_effect=Exception("connection already gone"))
    with read_only_cursor(conn) as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (True,)


def test_is_file_missing_ends_its_transaction():
    """The exact query measured parked in 'idle in transaction' on rek-d01."""
    from datetime import date

    tracker = FileTracker()
    tracker._conn = _Conn(fetch=[(True,)])

    assert tracker.is_file_missing("SEY2", "1Hz_1hr", date(2026, 8, 10), 3) is True
    assert tracker._conn.rollbacks == 1
    assert "is_file_missing" in tracker._conn.executed[0][0]


def test_format_resolver_load_ends_its_transaction():
    """The 68-minute offender: FormatResolver caching storage_location."""
    resolver = FormatResolver()
    resolver._conn = _Conn(fetch=[])

    resolver._load_locations()

    assert resolver._conn.rollbacks == 1
    assert "storage_location" in resolver._conn.executed[0][0]


def test_write_methods_still_commit_and_are_untouched():
    """The fix must not weaken the write paths -- they own their transactions."""
    from datetime import date

    tracker = FileTracker()
    tracker._conn = _Conn(fetch=[(42,)])

    tracker.mark_file_missing("SEY2", "1Hz_1hr", date(2026, 8, 10), 3)

    # >= 1 because mark_file_missing also drives _record_receiver_absence,
    # which owns its own transaction. Two commits is the pre-existing shape.
    assert tracker._conn.commits >= 1, "writes must still commit"
    assert tracker._conn.rollbacks == 0, "a successful write must not roll back"


def test_no_read_path_still_uses_a_bare_cursor():
    """Structural guard: catch the next read method that forgets the helper.

    Greps the source rather than the behaviour, deliberately -- a new read
    method added with `self._conn.cursor()` would pass every behavioural test
    above while re-introducing the leak.
    """
    import ast
    from pathlib import Path

    src_file = (
        Path(__file__).resolve().parents[1] / "src/receivers/health/file_tracker.py"
    )
    src = src_file.read_text()
    lines = src.splitlines()
    offenders = []

    for cls in [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            seg = "\n".join(lines[fn.lineno - 1 : fn.end_lineno])
            if "_conn.cursor()" not in seg:
                continue
            ends = "_conn.commit()" in seg or "_conn.rollback()" in seg
            if not ends:
                offenders.append(f"{cls.name}.{fn.name} (line {fn.lineno})")

    assert not offenders, (
        "these use a raw cursor on the long-lived connection without ending "
        "the transaction -- use read_cursor()/read_only_cursor() for reads, or "
        f"commit/rollback for writes: {offenders}"
    )
