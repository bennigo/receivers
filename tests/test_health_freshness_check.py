"""The check that should have caught the 2026-08-10 monitoring blackout.

Fleet health went blind for 5.5 h and nothing alerted: the 5-minute health
job last ran at 16:21, everything else stayed green, and it was found only
because bgo looked at a dashboard. The headline test here replays those
numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from receivers.monitoring.health_freshness_check import (
    NAGIOS_CRITICAL,
    NAGIOS_OK,
    NAGIOS_UNKNOWN,
    NAGIOS_WARNING,
    evaluate_health_freshness,
)

NOW = datetime(2026, 8, 10, 21, 51, tzinfo=UTC)


class FakeConn:
    """Answers the two queries the check makes, in order."""

    def __init__(self, newest, stations=176, fail_on=None):
        self._newest = newest
        self._stations = stations
        self._fail_on = fail_on  # "newest" | "coverage" | None
        self.rollbacks = 0

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                self._sql = sql
                if conn._fail_on == "newest" and "max(ts)" in sql:
                    raise RuntimeError("db exploded")
                if conn._fail_on == "coverage" and "count(DISTINCT" in sql:
                    raise RuntimeError("db exploded")

            def fetchone(self):
                if "max(ts)" in self._sql:
                    return (conn._newest,)
                return (conn._stations,)

        return _Cur()

    def rollback(self):
        self.rollbacks += 1

    def commit(self):  # pragma: no cover - the check never writes
        raise AssertionError("the liveness check must never write")


class TestTheIncident:
    def test_the_2026_08_10_blackout_is_critical(self):
        """5.5 h stale — the case that alerted nothing at the time."""
        last_run = datetime(2026, 8, 10, 16, 21, tzinfo=UTC)
        r = evaluate_health_freshness(FakeConn(last_run), now=NOW)
        assert r.exit_status == NAGIOS_CRITICAL
        assert "health job has stopped" in r.summary
        assert "330 min" in r.summary

    def test_a_healthy_five_minute_cadence_is_ok(self):
        r = evaluate_health_freshness(
            FakeConn(NOW - timedelta(minutes=3), stations=176), now=NOW
        )
        assert r.exit_status == NAGIOS_OK
        assert "176 station(s) reporting" in r.summary


class TestFreshnessThresholds:
    @pytest.mark.parametrize(
        "age_min,expected",
        [
            (5, NAGIOS_OK),
            (29, NAGIOS_OK),
            (30, NAGIOS_WARNING),
            (59, NAGIOS_WARNING),
            (60, NAGIOS_CRITICAL),
            (600, NAGIOS_CRITICAL),
        ],
    )
    def test_boundaries(self, age_min, expected):
        r = evaluate_health_freshness(
            FakeConn(NOW - timedelta(minutes=age_min)), now=NOW
        )
        assert r.exit_status == expected

    def test_empty_table_is_critical(self):
        r = evaluate_health_freshness(FakeConn(None), now=NOW)
        assert r.exit_status == NAGIOS_CRITICAL
        assert "never written" in r.summary

    def test_a_naive_timestamp_is_not_compared_across_domains(self):
        """Mixing naive and aware datetimes would raise, not just misjudge."""
        naive = NOW.replace(tzinfo=None) - timedelta(minutes=3)
        r = evaluate_health_freshness(FakeConn(naive), now=NOW)
        assert r.exit_status == NAGIOS_OK


class TestCoverage:
    """A job that runs but covers three stations is still a starving job."""

    def test_partial_coverage_warns_even_when_data_is_fresh(self):
        r = evaluate_health_freshness(
            FakeConn(NOW - timedelta(minutes=2), stations=60), now=NOW
        )
        assert r.exit_status == NAGIOS_WARNING
        assert "60 station(s) reported" in r.summary

    def test_near_zero_coverage_is_critical(self):
        r = evaluate_health_freshness(
            FakeConn(NOW - timedelta(minutes=2), stations=3), now=NOW
        )
        assert r.exit_status == NAGIOS_CRITICAL

    def test_coverage_is_not_evaluated_when_data_is_already_stale(self):
        """One finding per fault — a stopped job is not also 'low coverage'."""
        r = evaluate_health_freshness(
            FakeConn(NOW - timedelta(minutes=600), stations=0), now=NOW
        )
        assert r.exit_status == NAGIOS_CRITICAL
        assert "station(s) reported" not in r.summary


class TestPluginContract:
    def test_output_is_nagios_shaped(self):
        r = evaluate_health_freshness(FakeConn(NOW - timedelta(minutes=2)), now=NOW)
        assert r.plugin_output.startswith("OK - ")
        assert "age_minutes=" in r.perfdata
        assert "stations_reporting=" in r.perfdata

    def test_a_db_error_is_unknown_not_a_crash(self):
        """A monitoring plugin that raises tells the operator nothing."""
        r = evaluate_health_freshness(FakeConn(NOW, fail_on="newest"), now=NOW)
        assert r.exit_status == NAGIOS_UNKNOWN
        assert "cannot read block_ping_status" in r.summary

    def test_a_coverage_query_error_is_unknown_not_a_crash(self):
        r = evaluate_health_freshness(
            FakeConn(NOW - timedelta(minutes=2), fail_on="coverage"), now=NOW
        )
        assert r.exit_status == NAGIOS_UNKNOWN

    def test_the_check_never_writes(self):
        """FakeConn.commit raises — the check must only read."""
        evaluate_health_freshness(FakeConn(NOW - timedelta(minutes=2)), now=NOW)

    def test_reads_end_their_own_transaction(self):
        """Same rule as #143: this runs on a timer, forever."""
        conn = FakeConn(NOW - timedelta(minutes=2))
        evaluate_health_freshness(conn, now=NOW)
        assert conn.rollbacks >= 2  # freshness + coverage
