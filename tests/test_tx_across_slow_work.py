"""No transaction may stay open across slow non-DB work.

The class `dev/audits/tx_audit.py` structurally cannot see: these functions
DO commit, so the audit classifies them as writes and skips them —
correctly by its own model. But interleaving DB statements with filesystem
walks and SHA-256 hashing parks the connection `idle in transaction` for
the duration, and the server effect is identical to a true leak: it pins
the vacuum xmin horizon and blocks CREATE/DROP INDEX CONCURRENTLY, which
is what killed migration 065 six times.

Measured in production before the fix: `sync_archive_to_db` 373 s,
`verify_archive_catalog` 371 s.

So these are runtime tests, not static ones — they record when the
connection is in a transaction AT THE MOMENT the slow work runs.
"""

from __future__ import annotations

from datetime import date

import pytest


class RecordingConn:
    """psycopg2-shaped connection that tracks transaction state over time.

    `MagicMock` is useless here: it absorbs commit/rollback whether or not
    they are called, so a leak looks exactly like a clean read.
    """

    def __init__(self, rows=None, one=None):
        self._rows = rows if rows is not None else []
        self._one = one
        self.in_transaction = False
        self.commits = 0
        self.rollbacks = 0
        #: in_transaction sampled each time the slow work ran
        self.tx_during_slow_work: list[bool] = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *a, **k):
                conn.in_transaction = True  # psycopg2 opens one implicitly

            def fetchone(self):
                return conn._one

            def fetchall(self):
                return list(conn._rows)

        return _Cur()

    def commit(self):
        self.commits += 1
        self.in_transaction = False

    def rollback(self):
        self.rollbacks += 1
        self.in_transaction = False

    def sample(self):
        """Call from the slow work to record whether a tx is open."""
        self.tx_during_slow_work.append(self.in_transaction)


class TestSyncArchiveToDb:
    """The archive probe must run with NO transaction open."""

    def _sync(self, conn, monkeypatch, probe_hook):
        from receivers.health import file_tracker as ft

        obj = ft.GapDetector.__new__(ft.GapDetector)
        tracker = type("T", (), {"_conn": conn, "connect": lambda self: True})()
        obj.file_tracker = tracker
        monkeypatch.setattr(
            obj,
            "_generate_expected_files",
            lambda *a, **k: [(date(2026, 7, 1), None), (date(2026, 7, 2), None)],
            raising=False,
        )
        monkeypatch.setattr(obj, "_check_archive_for_file", probe_hook, raising=False)
        return obj.sync_archive_to_db(
            "RFEL", "15s_24hr", date(2026, 7, 1), date(2026, 7, 2)
        )

    def test_no_transaction_is_open_while_the_archive_is_probed(self, monkeypatch):
        conn = RecordingConn(rows=None)

        def probe(*a, **k):
            conn.sample()  # this is the NFS filesystem work
            return (False, "/archive/x", None)

        self._sync(conn, monkeypatch, probe)

        assert conn.tx_during_slow_work, "the probe never ran — test is vacuous"
        assert not any(conn.tx_during_slow_work), (
            "a transaction was open while the archive was probed: "
            f"{conn.tx_during_slow_work}"
        )

    def test_every_expected_file_is_still_probed(self, monkeypatch):
        conn = RecordingConn(rows=None)
        calls = []

        def probe(*a, **k):
            calls.append(a)
            return (False, "/archive/x", None)

        self._sync(conn, monkeypatch, probe)
        assert len(calls) == 2

    def test_a_probe_failure_is_counted_not_raised(self, monkeypatch):
        conn = RecordingConn(rows=None)

        def probe(*a, **k):
            raise OSError("NFS stale handle")

        res = self._sync(conn, monkeypatch, probe)
        assert res.errors == 2


class TestVerifyArchiveCatalog:
    """The re-hash must run with NO transaction open."""

    def test_no_transaction_is_open_while_hashing(self, monkeypatch, tmp_path):
        from receivers.archive import verify as v

        f = tmp_path / "AKUR_a.T02.gz"
        f.write_bytes(b"payload")
        row = (
            1,
            "AKUR",
            "15s_24hr",
            "raw",
            date(2026, 7, 1),
            "~/gpsdata/2026/jul/AKUR/15s_24hr/raw/AKUR_a.T02.gz",
            "deadbeef",
            "akur_a",
        )
        conn = RecordingConn(rows=[row])

        monkeypatch.setattr(v, "_local_archive_path", lambda *a, **k: str(f))

        def slow_hash(path):
            conn.sample()  # decompress + sha256 over NFS
            return "deadbeef"

        monkeypatch.setattr(v, "content_sha256", slow_hash)

        v.verify_archive_catalog(
            conn=conn,
            storage_location="imo_archive",
            read_root=str(tmp_path),
            dest_prefix="~/gpsdata",
            workers=1,  # the SERIAL path — hashes inline in the row loop
        )

        assert conn.tx_during_slow_work, "the hash never ran — test is vacuous"
        assert not any(conn.tx_during_slow_work), (
            "a transaction was open while hashing the archive file: "
            f"{conn.tx_during_slow_work}"
        )

    def test_the_read_ends_its_own_transaction(self, monkeypatch, tmp_path):
        """rollback, not commit — the row's UPDATE has not run yet."""
        from receivers.archive import verify as v

        f = tmp_path / "x.gz"
        f.write_bytes(b"p")
        row = (
            1,
            "AKUR",
            "15s_24hr",
            "raw",
            date(2026, 7, 1),
            "~/gpsdata/x.gz",
            "deadbeef",
            "x",
        )
        conn = RecordingConn(rows=[row])
        monkeypatch.setattr(v, "_local_archive_path", lambda *a, **k: str(f))
        monkeypatch.setattr(v, "content_sha256", lambda p: "deadbeef")

        v.verify_archive_catalog(
            conn=conn,
            storage_location="imo_archive",
            read_root=str(tmp_path),
            dest_prefix="~/gpsdata",
            workers=1,
        )
        assert conn.rollbacks >= 1
