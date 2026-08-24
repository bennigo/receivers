"""Tests for receivers.scheduling.morning_recovery."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from receivers.scheduling.morning_recovery import (
    _confirm_recovered,
    _query_stations_missing_yesterday,
    _retry_station,
    _run_morning_recovery_job,
)


def _mock_db(rows):
    """Build a mock DatabaseConnectionFactory.connection() context manager.

    `rows` is the list of tuples to return from cur.fetchall() / fetchone().
    """
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    return conn, cur


# ─── _query_stations_missing_yesterday ─────────────────────────────────────
#
# The query was rewritten to return a single result set of
# (bucket, sids[], count) rows so the operator can see *why* a station was
# filtered. The function returns only the 'queued' bucket — other buckets
# are surfaced via log lines.


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_returns_queued_bucket_only(mock_conn):
    conn, _cur = _mock_db(
        [
            ("queued", ["AFST", "ENTC"], 2),
            ("passive", ["GRAN"], 1),
            ("already_ok", ["ELDC", "THOB"], 2),
        ]
    )
    mock_conn.return_value = conn

    result = _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )

    assert result == ["AFST", "ENTC"]


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_bypass_known_missing_promotes_marked_missing(mock_conn):
    """marked_missing stations are promoted into the queue when bypass is on."""
    conn, _cur = _mock_db(
        [
            ("queued", ["AFST"], 1),
            ("marked_missing", ["HVSK", "SEY9"], 2),
        ]
    )
    mock_conn.return_value = conn

    result = _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=True
    )

    # Promoted SIDs merge into queued, sorted alphabetically
    assert result == ["AFST", "HVSK", "SEY9"]


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_no_bypass_keeps_marked_missing_out(mock_conn):
    """Default behaviour: marked_missing stations are NOT included."""
    conn, _cur = _mock_db(
        [
            ("queued", ["AFST"], 1),
            ("marked_missing", ["HVSK"], 1),
        ]
    )
    mock_conn.return_value = conn

    result = _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )

    assert result == ["AFST"]


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_passes_session_and_date_params(mock_conn):
    conn, cur = _mock_db([])
    mock_conn.return_value = conn

    _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )

    params = cur.execute.call_args[0][1]
    assert params == {
        "sess": "15s_24hr",
        "date": date(2026, 5, 7),
        "stale_minutes": 120,
    }


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_passes_custom_stale_window(mock_conn):
    """stale_missing_window_minutes flows through to the SQL parameters."""
    conn, cur = _mock_db([])
    mock_conn.return_value = conn

    _query_stations_missing_yesterday(
        "15s_24hr",
        date(2026, 5, 7),
        bypass_known_missing=False,
        stale_missing_window_minutes=30,
    )

    params = cur.execute.call_args[0][1]
    assert params["stale_minutes"] == 30


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_log_summary_includes_stale_window(mock_conn, caplog):
    """The filter summary surfaces the active window so operators see the policy."""
    import logging

    conn, _ = _mock_db([("queued", ["AFST"], 1)])
    mock_conn.return_value = conn

    with caplog.at_level(logging.INFO, logger="receivers.scheduler.morning_recovery"):
        _query_stations_missing_yesterday(
            "15s_24hr",
            date(2026, 5, 7),
            bypass_known_missing=False,
            stale_missing_window_minutes=45,
        )

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "stale_missing_window=45m" in text


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_returns_empty_list_when_no_missing(mock_conn):
    conn, _ = _mock_db([])
    mock_conn.return_value = conn

    result = _query_stations_missing_yesterday(
        "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
    )
    assert result == []


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_query_logs_filter_summary(mock_conn, caplog):
    """Operator should see why stations were filtered out."""
    import logging

    conn, _ = _mock_db(
        [
            ("queued", ["AFST"], 1),
            ("passive", ["GRAN", "TANC"], 2),
            ("already_ok", ["ELDC", "THOB", "OLKE"], 3),
            ("marked_missing", ["HVSK"], 1),
        ]
    )
    mock_conn.return_value = conn

    with caplog.at_level(logging.INFO, logger="receivers.scheduler.morning_recovery"):
        _query_stations_missing_yesterday(
            "15s_24hr", date(2026, 5, 7), bypass_known_missing=False
        )

    # The summary line names each non-empty bucket
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "queued=1" in text
    assert "passive=2" in text
    assert "already_ok=3" in text
    assert "marked_missing=1" in text
    # marked_missing SIDs surfaced for audit
    assert "HVSK" in text


# ─── _confirm_recovered ────────────────────────────────────────────────────


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_confirm_recovered_true_when_archived(mock_conn):
    conn, _ = _mock_db([(1,)])
    mock_conn.return_value = conn

    assert _confirm_recovered("AFST", "15s_24hr", date(2026, 5, 7)) is True


@patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
def test_confirm_recovered_false_when_no_row(mock_conn):
    conn, _ = _mock_db([])
    mock_conn.return_value = conn

    assert _confirm_recovered("AFST", "15s_24hr", date(2026, 5, 7)) is False


# ─── _retry_station ────────────────────────────────────────────────────────


@patch("receivers.scheduling.morning_recovery._confirm_recovered")
@patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
def test_retry_station_returns_recovered_tuple_on_success(mock_dl, mock_confirm):
    mock_confirm.return_value = True

    result = _retry_station(
        "AFST",
        "15s_24hr",
        date(2026, 5, 7),
        timeout_minutes=8,
        run_rinex=True,
        production_mode=True,
    )

    assert result == ("AFST", True)
    mock_dl.assert_called_once()


@patch("receivers.scheduling.morning_recovery._confirm_recovered")
@patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
def test_retry_station_returns_failed_tuple_when_not_recovered(mock_dl, mock_confirm):
    mock_confirm.return_value = False

    result = _retry_station(
        "AFST",
        "15s_24hr",
        date(2026, 5, 7),
        timeout_minutes=8,
        run_rinex=True,
        production_mode=True,
    )

    assert result == ("AFST", False)


@patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
def test_retry_station_swallows_exceptions(mock_dl):
    mock_dl.side_effect = RuntimeError("boom")

    result = _retry_station(
        "AFST",
        "15s_24hr",
        date(2026, 5, 7),
        timeout_minutes=8,
        run_rinex=True,
        production_mode=True,
    )

    assert result == ("AFST", False)


# ─── _run_morning_recovery_job ─────────────────────────────────────────────


@patch("receivers.scheduling.morning_recovery._query_stations_missing_yesterday")
def test_run_morning_recovery_noop_when_no_missing(mock_query):
    mock_query.return_value = []

    # Should not raise
    _run_morning_recovery_job(["15s_24hr"], days_back=1, max_workers=2)


@patch("receivers.scheduling.morning_recovery._retry_station")
@patch("receivers.scheduling.morning_recovery._query_stations_missing_yesterday")
def test_run_morning_recovery_invokes_retry_per_station(mock_query, mock_retry):
    mock_query.return_value = ["AFST", "ENTC"]
    mock_retry.side_effect = [("AFST", True), ("ENTC", True)]

    _run_morning_recovery_job(["15s_24hr"], days_back=1, max_workers=2)

    assert mock_retry.call_count == 2


@patch("receivers.scheduling.morning_recovery._retry_station")
@patch("receivers.scheduling.morning_recovery._query_stations_missing_yesterday")
def test_run_morning_recovery_handles_mixed_outcomes(mock_query, mock_retry):
    mock_query.return_value = ["AFST", "ENTC", "GRAN"]
    mock_retry.side_effect = [("AFST", True), ("ENTC", False), ("GRAN", True)]

    _run_morning_recovery_job(["15s_24hr"], days_back=1, max_workers=2)


# ─── _schedule_morning_recovery — multi-fire schedule support ──────────────
#
# The bulk_scheduler-side handler now accepts schedule as either str (legacy)
# or list of strings (multi-fire). Each entry creates its own APScheduler job.


def _make_scheduler(schedule_value):
    """Build a stub BulkDownloadScheduler with a fake APScheduler.

    Returns the (scheduler_instance, mock_apscheduler). The instance's
    _schedule_morning_recovery() method is invoked by tests to verify
    add_job() is called the expected number of times.
    """
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    inst = BulkDownloadScheduler.__new__(BulkDownloadScheduler)
    inst.scheduler = MagicMock()
    inst.logger = MagicMock()
    # __new__ bypasses __init__, so every attribute the method under test
    # touches has to be stubbed here. _disabled_jobs IS initialised in
    # __init__ (bulk_scheduler.py) — this stub simply drifted behind
    # _schedule_morning_recovery growing a _mark_disabled() call.
    inst._disabled_jobs = []
    inst.yaml_config = {
        "morning_recovery": {
            "enabled": True,
            "schedule": schedule_value,
            "sessions": ["15s_24hr"],
            "days_back": 1,
            "max_workers": 4,
            "station_timeout_minutes": 8,
            "bypass_known_missing": False,
        }
    }
    return inst


def test_schedule_morning_recovery_single_string_creates_one_job():
    inst = _make_scheduler("01:30")
    inst._schedule_morning_recovery()

    assert inst.scheduler.add_job.call_count == 1
    # Single-fire keeps the legacy id for backward compatibility
    job_id = inst.scheduler.add_job.call_args_list[0].kwargs["id"]
    assert job_id == "morning_recovery"


def test_schedule_morning_recovery_list_creates_one_job_per_entry():
    inst = _make_scheduler(["01:30", "06:00"])
    inst._schedule_morning_recovery()

    assert inst.scheduler.add_job.call_count == 2
    ids = [call.kwargs["id"] for call in inst.scheduler.add_job.call_args_list]
    assert ids == ["morning_recovery_0", "morning_recovery_1"]


def test_schedule_morning_recovery_list_accepts_mixed_format_entries():
    """List entries can be daily times, intervals, or cron expressions."""
    inst = _make_scheduler(["01:30", "cron: 30 6 * * 1-5"])
    inst._schedule_morning_recovery()

    assert inst.scheduler.add_job.call_count == 2


def test_schedule_morning_recovery_disabled_skips_all_jobs():
    inst = _make_scheduler(["01:30", "06:00"])
    inst.yaml_config["morning_recovery"]["enabled"] = False
    inst._schedule_morning_recovery()

    inst.scheduler.add_job.assert_not_called()
