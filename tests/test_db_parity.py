"""`receivers db parity` — make mirror divergence visible instead of silent.

The dual-write mirror is best-effort: `_DualCursor` logs a failed mirror leg
and drops the statement. No retry, no queue, no reconciliation. So every pgdev
blip is a permanent divergence, and it drifts in BOTH directions — maintenance
run with `single_host=True` deletes on the primary only, leaving orphans on the
mirror. Measured 2026-08-10: file_tracking 961,020 (rek-d01) vs 936,423
(pgdev), and file_absence off by 424.

This command does not fix that (vault todo #142 owns the choice between a
reconciliation sweep and real replication). It reports it, and exits non-zero
past the tolerance so cron or Icinga can alarm.
"""

import argparse
from unittest.mock import patch

import pytest

from receivers.cli.db import PARITY_TABLES, cmd_db_parity


def _args(**kw):
    ns = argparse.Namespace(
        tables=None, all=False, tolerance_pct=0.5, json=False, mirror=None, host=None
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def counts():
    """Patch the per-host counter; returns the dict the test can populate."""
    per_host = {}

    def _fake(host, tables):
        return {t: per_host.get(host, {}).get(t) for t in tables}

    with (
        patch("receivers.cli.db._count_rows", side_effect=_fake),
        patch("receivers.cli.db.resolve_db_host", return_value="rek-d01"),
        patch(
            "receivers.health.database_factory._load_config_file",
            return_value={"mirror_host": "pgdev"},
        ),
    ):
        yield per_host


def test_no_mirror_configured_is_not_a_failure():
    """A laptop with no mirror must exit 0, not alarm."""
    with patch("receivers.health.database_factory._load_config_file", return_value={}):
        assert cmd_db_parity(_args()) == 0


def test_mirror_equal_to_primary_is_not_a_failure():
    with (
        patch("receivers.cli.db.resolve_db_host", return_value="pgdev"),
        patch(
            "receivers.health.database_factory._load_config_file",
            return_value={"mirror_host": "pgdev"},
        ),
    ):
        assert cmd_db_parity(_args()) == 0


def test_in_sync_exits_zero(counts, capsys):
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {t: 1000 for t in PARITY_TABLES}

    assert cmd_db_parity(_args()) == 0
    assert "ok" in capsys.readouterr().out


def test_divergence_over_tolerance_exits_nonzero(counts, capsys):
    """The real 2026-08-10 numbers: 2.6% short on the mirror."""
    counts["rek-d01"] = {
        "file_tracking": 961_020,
        "file_absence": 75_988,
        "archive_catalog": 100,
    }
    counts["pgdev"] = {
        "file_tracking": 936_423,
        "file_absence": 75_564,
        "archive_catalog": 100,
    }

    assert cmd_db_parity(_args()) == 1
    out = capsys.readouterr().out
    assert "-24,597" in out
    assert "OVER" in out


def test_mirror_ahead_also_counts_as_divergence(counts):
    """It drifts BOTH ways — orphans on the mirror are divergence too."""
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 1100}

    assert cmd_db_parity(_args()) == 1


def test_tolerance_is_respected(counts):
    counts["rek-d01"] = {t: 10_000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 10_000 for t in PARITY_TABLES}, "file_tracking": 9_990}

    assert cmd_db_parity(_args(tolerance_pct=0.5)) == 0  # 0.1% drift
    assert cmd_db_parity(_args(tolerance_pct=0.05)) == 1


def test_table_absent_on_one_host_is_reported_not_counted(counts, capsys):
    """A table only one host has is worth showing, but is not a % divergence."""
    counts["rek-d01"] = {t: 100 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 100 for t in PARITY_TABLES}, "file_absence": None}

    assert cmd_db_parity(_args()) == 0
    assert "absent on a host" in capsys.readouterr().out


def test_json_output_carries_the_verdict(counts, capsys):
    import json

    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 900}

    rc = cmd_db_parity(_args(json=True))
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["breached"] is True
    assert payload["primary"] == "rek-d01" and payload["mirror"] == "pgdev"
    ft = next(t for t in payload["tables"] if t["table"] == "file_tracking")
    assert ft["delta"] == -100 and ft["status"] == "OVER"


def test_explicit_tables_override_the_default_set(counts):
    counts["rek-d01"] = {"stations": 173}
    counts["pgdev"] = {"stations": 173}

    assert cmd_db_parity(_args(tables="stations")) == 0


# ── _count_rows: the SQL the tests above mock away ────────────────────────────


class _CountConn:
    """Connection double answering to_regclass probes then count(*)."""

    def __init__(self, existing):
        self.existing = existing
        self.executed: list = []
        self.rollbacks = 0
        self.closed = False
        self._pending = None

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.executed.append((sql, params))
                if "to_regclass" in sql:
                    table = params[0].split(".", 1)[1]
                    conn._pending = (table in conn.existing,)
                else:
                    conn._pending = (len(conn.existing),)

            def fetchone(self):
                return conn._pending

        return _Cur()

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_count_rows_skips_absent_tables_and_quotes_identifiers():
    """A table missing on one host must not raise — and must not be counted."""
    from receivers.cli.db import _count_rows

    conn = _CountConn(existing={"file_tracking"})
    with patch("receivers.cli.db.db_connection", return_value=conn):
        got = _count_rows("somehost", ["file_tracking", "gone_table"])

    assert got["gone_table"] is None
    assert got["file_tracking"] == 1

    counts_sql = [s for s, _ in conn.executed if "count(*)" in s]
    assert counts_sql == [
        'SELECT count(*) FROM "file_tracking"'
    ], "the identifier must be quoted, and an absent table must never be counted"


def test_count_rows_ends_its_transaction_and_closes():
    """Parity is a read — it must not park a connection, per todo #141."""
    from receivers.cli.db import _count_rows

    conn = _CountConn(existing={"file_tracking"})
    with patch("receivers.cli.db.db_connection", return_value=conn):
        _count_rows("somehost", ["file_tracking"])

    assert conn.rollbacks == 1
    assert conn.closed is True
