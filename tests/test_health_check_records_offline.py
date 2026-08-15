"""A failed 5-minute health check must leave an explicit offline sample.

Absence of a ``block_ping_status`` row is ambiguous: it cannot distinguish
"the station did not answer" from "the scheduler was not running". That matters
because the receiver-retained metrics that arrive later via ``status_1hr``
(voltage, temperature, satellites) DO backfill the same window at their original
timestamps — so an outage recorded only as a gap silently disappears from the
graph once the late data lands.

``_status_check_job`` already covers the common case: when
``gather_comprehensive_health`` raises, an error-only dict still reaches
``_write_connectivity_status`` and writes ``is_online=false``. Failures raised
*before* that inner try — ``create_receiver``, the imports, anything unexpected —
used to write nothing at all. These tests pin the outer handler.
"""

from unittest.mock import MagicMock, patch

import pytest

from receivers.scheduling.bulk_scheduler import _status_check_job


@pytest.fixture
def patched_config():
    """Station config resolves, so failure comes from create_receiver."""
    with patch("receivers.cli.main.get_station_config", return_value={"x": 1}):
        yield


def _run_with_receiver_failure(send_to_db=True):
    """Drive _status_check_job so it raises before the inner try."""
    with patch(
        "receivers.cli.main.create_receiver", side_effect=RuntimeError("no route to host")
    ), patch(
        "receivers.health.connectivity_writer.ConnectivityWriter"
    ) as writer_cls:
        _status_check_job("TEST", send_to_db=send_to_db, send_to_icinga=False)
    return writer_cls


def test_failure_before_inner_try_records_offline(patched_config):
    """create_receiver blowing up must still leave a ping sample."""
    writer_cls = _run_with_receiver_failure()

    writer_cls.return_value.write_ping_only.assert_called_once()
    station_id, health_data = writer_cls.return_value.write_ping_only.call_args[0]
    assert station_id == "TEST"
    # The error is carried so the row is not silently blank.
    assert "RuntimeError" in health_data["connection"]["error"]
    assert "no route to host" in health_data["connection"]["error"]


def test_recorded_payload_resolves_to_offline(patched_config):
    """The payload must make the REAL writer compute is_online=False.

    Guards against a payload that happens to carry a truthy router_ping and
    would therefore record the station as ONLINE during an outage.
    """
    writer_cls = _run_with_receiver_failure()
    _, health_data = writer_cls.return_value.write_ping_only.call_args[0]

    # Mirror connectivity_writer._write_ping_status's own derivation.
    connection = health_data.get("connection", {})
    router_ping = connection.get("router_ping", {})
    assert router_ping.get("accessible", False) is False


def test_no_db_write_when_db_disabled(patched_config):
    """send_to_db=False must not write connectivity either."""
    writer_cls = _run_with_receiver_failure(send_to_db=False)
    writer_cls.return_value.write_ping_only.assert_not_called()


def test_recording_failure_never_breaks_the_job(patched_config):
    """A failure to record must not propagate out of the health job."""
    with patch(
        "receivers.cli.main.create_receiver", side_effect=RuntimeError("boom")
    ), patch(
        "receivers.health.connectivity_writer.ConnectivityWriter"
    ) as writer_cls:
        writer_cls.return_value.write_ping_only.side_effect = Exception("db down")
        # Must return normally, not raise.
        _status_check_job("TEST", send_to_db=True, send_to_icinga=False)


def test_missing_station_config_does_not_record_offline():
    """No config is a configuration error, not a station outage.

    Recording it as 'offline' would blame the station for our own misconfig,
    so this path deliberately stays a no-op.
    """
    with patch("receivers.cli.main.get_station_config", return_value=None), patch(
        "receivers.health.connectivity_writer.ConnectivityWriter"
    ) as writer_cls:
        _status_check_job("TEST", send_to_db=True, send_to_icinga=False)

    writer_cls.return_value.write_ping_only.assert_not_called()


def test_connectivity_writer_derives_offline_from_error_only_payload():
    """End-to-end on the real writer: error-only payload => is_online False."""
    from receivers.health.connectivity_writer import ConnectivityWriter

    writer = ConnectivityWriter(MagicMock())
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    from datetime import datetime, timezone

    writer._write_ping_status(
        conn,
        "TEST",
        {"connection": {"error": "RuntimeError: no route to host"}},
        datetime(2026, 8, 14, 14, 23, tzinfo=timezone.utc),
    )

    assert cursor.execute.called
    params = cursor.execute.call_args[0][1]
    # is_online must be False somewhere in the bound parameters.
    assert False in params, f"no False (is_online) in bound params: {params}"
