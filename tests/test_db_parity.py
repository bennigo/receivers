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
        tables=None,
        all=False,
        tolerance_pct=0.5,
        json=False,
        mirror=None,
        host=None,
        by=None,  # no grouping unless a test asks for it
        no_group=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def counts():
    """Patch the per-host counters; returns the dicts a test can populate.

    ``counts[host][table]`` -> total; ``groups[host][table]`` -> {group: n}.
    """

    class _Counts(dict):
        """dict of totals, with the per-group counts hung off `.groups`."""

    per_host = _Counts()
    per_group = {}

    def _fake(host, tables):
        return {t: per_host.get(host, {}).get(t) for t in tables}

    def _fake_grouped(host, tables, by):
        return {t: per_group.get(host, {}).get(t) for t in tables}

    with (
        patch("receivers.cli.db._count_rows", side_effect=_fake),
        patch("receivers.cli.db._count_rows_grouped", side_effect=_fake_grouped),
        patch("receivers.cli.db.resolve_db_host", return_value="rek-d01"),
        patch(
            "receivers.health.database_factory._load_config_file",
            return_value={"mirror_host": "pgdev"},
        ),
    ):
        per_host.groups = per_group  # attach so tests reach it off one fixture
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
    assert ft["net"] == -100 and ft["divergence"] == 100 and ft["status"] == "OVER"


def test_explicit_tables_override_the_default_set(counts):
    counts["rek-d01"] = {"stations": 173}
    counts["pgdev"] = {"stations": 173}

    assert cmd_db_parity(_args(tables="stations")) == 0


# ── Grouping: the whole point — opposite drift must not cancel ────────────────
#
# Real numbers, measured on rek-d01 vs pgdev 2026-08-10. Per session_type the
# divergence is 32,340 rows; the whole-table delta reads 24,598. Counting the
# table understates the truth by 24% because pgdev is AHEAD on the daily
# sessions and BEHIND on the hourly ones.
REAL_PRIMARY = {
    "status_1hr": 367_822,
    "1Hz_1hr": 279_310,
    "1Hz_1hr_rinex": 270_210,
    "15s_24hr": 18_830,
    "15s_24hr_rinex": 24_848,
}
REAL_MIRROR = {
    "status_1hr": 342_476,
    "1Hz_1hr": 276_334,
    "1Hz_1hr_rinex": 270_063,
    "15s_24hr": 21_659,
    "15s_24hr_rinex": 25_890,
}


def test_grouping_reports_true_divergence_not_the_net(counts, capsys):
    """The measured case: 32,340 real vs 24,598 net."""
    counts["rek-d01"] = {"file_tracking": sum(REAL_PRIMARY.values())}
    counts["pgdev"] = {"file_tracking": sum(REAL_MIRROR.values())}
    counts.groups["rek-d01"] = {"file_tracking": REAL_PRIMARY}
    counts.groups["pgdev"] = {"file_tracking": REAL_MIRROR}

    rc = cmd_db_parity(_args(tables="file_tracking", by="session_type", json=True))
    import json

    ft = json.loads(capsys.readouterr().out)["tables"][0]

    assert ft["divergence"] == 32_340, "Σ|delta| per group is the honest number"
    assert ft["net"] == -24_598, "the net is what a whole-table count would show"
    assert ft["divergence"] > abs(ft["net"])
    assert rc == 1


def test_fully_cancelling_drift_is_caught_by_grouping(counts):
    """The failure mode that makes whole-table counting unsafe.

    Mirror short 500 rows in one session and long 500 in another: totals match
    exactly, so an ungrouped check reports 'ok' on a database that is wrong on
    both sides. Grouping must still flag it.
    """
    pri = {"a": 1000, "b": 1000}
    mir = {"a": 500, "b": 1500}

    counts["rek-d01"] = {"file_tracking": 2000}
    counts["pgdev"] = {"file_tracking": 2000}
    counts.groups["rek-d01"] = {"file_tracking": pri}
    counts.groups["pgdev"] = {"file_tracking": mir}

    # Ungrouped: totals are identical, so it says everything is fine.
    assert cmd_db_parity(_args(tables="file_tracking")) == 0
    # Grouped: 1000 rows are in the wrong place and it says so.
    assert cmd_db_parity(_args(tables="file_tracking", by="session_type")) == 1


def test_no_group_flag_forces_whole_table_counts(counts):
    pri = {"a": 1000, "b": 1000}
    mir = {"a": 500, "b": 1500}
    counts["rek-d01"] = {"file_tracking": 2000}
    counts["pgdev"] = {"file_tracking": 2000}
    counts.groups["rek-d01"] = {"file_tracking": pri}
    counts.groups["pgdev"] = {"file_tracking": mir}

    assert (
        cmd_db_parity(_args(tables="file_tracking", by="session_type", no_group=True))
        == 0
    )


def test_table_without_the_group_column_falls_back_not_dropped(counts, capsys):
    """A table lacking session_type must still be compared, just ungrouped."""
    counts["rek-d01"] = {"archive_catalog": 1000}
    counts["pgdev"] = {"archive_catalog": 900}
    counts.groups["rek-d01"] = {"archive_catalog": None}  # column absent
    counts.groups["pgdev"] = {"archive_catalog": None}

    rc = cmd_db_parity(_args(tables="archive_catalog", by="session_type", json=True))
    import json

    row = json.loads(capsys.readouterr().out)["tables"][0]

    assert rc == 1
    assert row["divergence"] == 100
    assert row["grouped_by"] is None, "must report that it could not group"


def test_group_present_on_only_one_host_counts_as_divergence(counts):
    counts["rek-d01"] = {"file_tracking": 1000}
    counts["pgdev"] = {"file_tracking": 1200}
    counts.groups["rek-d01"] = {"file_tracking": {"a": 1000}}
    counts.groups["pgdev"] = {"file_tracking": {"a": 1000, "orphan_session": 200}}

    assert cmd_db_parity(_args(tables="file_tracking", by="session_type")) == 1


# ── Icinga push: without it the daily timer's finding dies in the journal ─────


def _args_icinga(**kw):
    return _args(icinga=True, icinga_host="rek-d01", ttl=172_800, **kw)


def test_icinga_push_warns_on_divergence(counts):
    """WARNING, not CRITICAL: the drift is chronic and known, not an outage."""
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 900}

    sent = []
    with patch(
        "receivers.monitoring.icinga_client.IcingaClient.send_check_result",
        side_effect=lambda c: sent.append(c) or {"success": True},
    ):
        assert cmd_db_parity(_args_icinga()) == 1

    assert len(sent) == 1
    assert sent[0].exit_status == 1
    assert sent[0].check_name == "Mirror parity"
    assert sent[0].ttl == 172_800, "a TTL is what makes a DEAD timer visible"
    assert "file_tracking" in sent[0].plugin_output
    assert "file_tracking_divergence=100" in sent[0].performance_data


def test_icinga_push_reports_ok_when_in_sync(counts):
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {t: 1000 for t in PARITY_TABLES}

    sent = []
    with patch(
        "receivers.monitoring.icinga_client.IcingaClient.send_check_result",
        side_effect=lambda c: sent.append(c) or {"success": True},
    ):
        assert cmd_db_parity(_args_icinga()) == 0

    assert sent[0].exit_status == 0


def test_icinga_failure_never_breaks_the_check(counts):
    """Alerting is best-effort — a dead Icinga must not hide the divergence."""
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 900}

    with patch(
        "receivers.monitoring.icinga_client.IcingaClient.send_check_result",
        side_effect=Exception("icinga unreachable"),
    ):
        assert cmd_db_parity(_args_icinga()) == 1, "exit code must still report"


def test_append_json_writes_one_timestamped_line(counts, tmp_path):
    """The trend history. journalctl is unreadable by gpsops AND bgo on rek-d01,
    so without this the daily timer would be write-only."""
    import json

    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 900}
    log = tmp_path / "logs" / "mirror_parity.jsonl"

    cmd_db_parity(_args(append_json=str(log)))
    cmd_db_parity(_args(append_json=str(log)))

    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2, "appends, never truncates — the history is the point"
    rec = json.loads(lines[0])
    assert rec["checked_at"] and rec["breached"] is True
    assert any(t["table"] == "file_tracking" for t in rec["tables"])


def test_append_json_failure_never_breaks_the_check(counts):
    """An unwritable path must not swallow the divergence verdict."""
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 900}

    assert cmd_db_parity(_args(append_json="/proc/nope/parity.jsonl")) == 1


def test_no_icinga_push_unless_asked(counts):
    counts["rek-d01"] = {t: 1000 for t in PARITY_TABLES}
    counts["pgdev"] = {**{t: 1000 for t in PARITY_TABLES}, "file_tracking": 900}

    with patch(
        "receivers.monitoring.icinga_client.IcingaClient.send_check_result"
    ) as send:
        cmd_db_parity(_args())

    send.assert_not_called()


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
    assert counts_sql == ['SELECT count(*) FROM "file_tracking"'], (
        "the identifier must be quoted, and an absent table must never be counted"
    )


class _GroupConn:
    """Connection double for _count_rows_grouped: column probe, then GROUP BY."""

    def __init__(self, has_column, rows=()):
        self.has_column = has_column
        self.rows = rows
        self.executed: list = []
        self.rollbacks = 0
        self.closed = False
        self._pending = None
        self._pending_all = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.executed.append((sql, params))
                if "information_schema.columns" in sql:
                    conn._pending = (conn.has_column,)
                else:
                    conn._pending_all = list(conn.rows)

            def fetchone(self):
                return conn._pending

            def fetchall(self):
                return conn._pending_all

        return _Cur()

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_count_rows_grouped_returns_none_when_the_column_is_absent():
    """Signals 'cannot group' so the caller falls back instead of dropping it."""
    from receivers.cli.db import _count_rows_grouped

    conn = _GroupConn(has_column=False)
    with patch("receivers.cli.db.db_connection", return_value=conn):
        got = _count_rows_grouped("somehost", ["archive_catalog"], "session_type")

    assert got["archive_catalog"] is None
    assert not [s for s, _ in conn.executed if "GROUP BY" in s]


def test_count_rows_grouped_labels_null_groups():
    """A NULL group must be a visible bucket, not silently merged or dropped."""
    from receivers.cli.db import _count_rows_grouped

    conn = _GroupConn(has_column=True, rows=[("1Hz_1hr", 10), (None, 3)])
    with patch("receivers.cli.db.db_connection", return_value=conn):
        got = _count_rows_grouped("somehost", ["file_tracking"], "session_type")

    assert got["file_tracking"] == {"1Hz_1hr": 10, "∅": 3}
    assert conn.rollbacks == 1 and conn.closed is True


def test_count_rows_ends_its_transaction_and_closes():
    """Parity is a read — it must not park a connection, per todo #141."""
    from receivers.cli.db import _count_rows

    conn = _CountConn(existing={"file_tracking"})
    with patch("receivers.cli.db.db_connection", return_value=conn):
        _count_rows("somehost", ["file_tracking"])

    assert conn.rollbacks == 1
    assert conn.closed is True
