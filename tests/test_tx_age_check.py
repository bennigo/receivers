"""The runtime detector for transactions left open (#143).

`dev/audits/tx_audit.py` is blind to this class by construction — the
offending functions DO commit, so it classifies them as writes and skips
them. Two were found by hand (`sync_archive_to_db` 373 s,
`verify_archive_catalog` 371 s). This check is what finds the third.
"""

from __future__ import annotations

import pytest

from receivers.monitoring.tx_age_check import (
    NAGIOS_CRITICAL,
    NAGIOS_OK,
    NAGIOS_UNKNOWN,
    NAGIOS_WARNING,
    evaluate_tx_age,
)


class FakeConn:
    """Answers the two pg_stat_activity queries the check makes."""

    def __init__(self, worst=None, oldest=None, fail=False):
        self._worst = worst  # (age_s, pid, app, query) or None
        self._oldest = oldest  # seconds or None
        self._fail = fail
        self.rollbacks = 0
        self.queries: list[str] = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                if conn._fail:
                    raise RuntimeError("pg_stat_activity unavailable")
                self._sql = sql
                conn.queries.append(sql)

            def fetchone(self):
                if "min(xact_start)" in self._sql:
                    return (conn._oldest,)
                return conn._worst

        return _Cur()

    def rollback(self):
        self.rollbacks += 1

    def commit(self):  # pragma: no cover
        raise AssertionError("a monitoring probe must never write")


def _idle(age_s, pid=4242, app="receivers-scheduler", query="SELECT 1"):
    return (age_s, pid, app, query)


class TestTheRealOffenders:
    """The two leaks found by hand would both have been caught."""

    @pytest.mark.parametrize("age", [373, 371])
    def test_the_measured_leaks_would_have_alerted(self, age):
        """373 s and 371 s sit between WARN (120) and CRIT (600)."""
        r = evaluate_tx_age(FakeConn(worst=_idle(age), oldest=age))
        assert r.exit_status == NAGIOS_WARNING
        assert "pins the vacuum xmin horizon" in r.summary
        assert f"{age}s" in r.summary

    def test_the_offender_is_named(self):
        r = evaluate_tx_age(
            FakeConn(
                worst=_idle(700, pid=99, app="archive-sync", query="SELECT sha FROM x"),
                oldest=700,
            )
        )
        assert "pid 99" in r.summary
        assert "archive-sync" in r.summary
        assert "SELECT sha FROM x" in r.summary


class TestIdleThresholds:
    @pytest.mark.parametrize(
        "age,expected",
        [
            (0, NAGIOS_OK),
            (119, NAGIOS_OK),
            (120, NAGIOS_WARNING),
            (599, NAGIOS_WARNING),
            (600, NAGIOS_CRITICAL),
        ],
    )
    def test_boundaries(self, age, expected):
        # oldest kept under its own warn threshold so only the idle signal moves
        r = evaluate_tx_age(FakeConn(worst=_idle(age), oldest=age))
        assert r.exit_status == expected

    def test_no_idle_session_is_ok(self):
        r = evaluate_tx_age(FakeConn(worst=None, oldest=None))
        assert r.exit_status == NAGIOS_OK
        assert "no long transactions" in r.summary


class TestOldestTransaction:
    """A long-running ACTIVE query pins the xmin horizon just as hard."""

    def test_a_long_active_transaction_warns_even_with_no_idle_session(self):
        r = evaluate_tx_age(FakeConn(worst=None, oldest=1000))
        assert r.exit_status == NAGIOS_WARNING
        assert "oldest open transaction" in r.summary

    def test_a_very_long_active_transaction_is_critical(self):
        r = evaluate_tx_age(FakeConn(worst=None, oldest=4000))
        assert r.exit_status == NAGIOS_CRITICAL

    def test_the_worst_of_both_signals_wins(self):
        """Idle CRIT beats oldest WARN."""
        r = evaluate_tx_age(FakeConn(worst=_idle(700), oldest=1000))
        assert r.exit_status == NAGIOS_CRITICAL


class TestProbeHygiene:
    def test_the_check_excludes_its_own_backend(self):
        """Otherwise the probe's own read is a candidate — wrong and absurd."""
        conn = FakeConn(worst=None, oldest=None)
        evaluate_tx_age(conn)
        assert conn.queries, "no query ran — test is vacuous"
        assert all("pg_backend_pid()" in q for q in conn.queries)

    def test_reads_end_their_own_transaction(self):
        """A leak detector that leaks is worse than none."""
        conn = FakeConn(worst=None, oldest=None)
        evaluate_tx_age(conn)
        assert conn.rollbacks >= 2

    def test_the_check_never_writes(self):
        evaluate_tx_age(FakeConn(worst=None, oldest=None))

    def test_it_only_looks_at_the_current_database(self):
        conn = FakeConn(worst=None, oldest=None)
        evaluate_tx_age(conn)
        assert all("current_database()" in q for q in conn.queries)

    def test_a_db_error_is_unknown_not_a_crash(self):
        r = evaluate_tx_age(FakeConn(fail=True))
        assert r.exit_status == NAGIOS_UNKNOWN
        assert "cannot read pg_stat_activity" in r.summary


class TestPluginContract:
    def test_perfdata_carries_both_signals(self):
        r = evaluate_tx_age(FakeConn(worst=_idle(30), oldest=30))
        assert "idle_in_transaction_seconds=30" in r.perfdata
        assert "oldest_transaction_seconds=30" in r.perfdata

    def test_output_is_nagios_shaped(self):
        r = evaluate_tx_age(FakeConn(worst=None, oldest=None))
        assert r.plugin_output.startswith("OK - ")
