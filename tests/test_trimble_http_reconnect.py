"""Tests for the mid-download HTTP reconnect in `NetR9HTTPDownloader.download_file`.

When a download hits a timeout/connection error, the retry loop is supposed to
rebuild the HTTP client to get a fresh session before retrying. That path was
dead: it called a six-argument `TrimbleHTTPClient(station_id, ip, http_port,
username, password, logger)` form that has not existed for a long time — the
real signature is `(station_id, station_config)`. None of `self.ip`,
`self.http_port`, `self.username` or `self.password` were ever assigned on the
downloader, so the very first one raised

    AttributeError: 'NetR9HTTPDownloader' object has no attribute 'ip'

which the surrounding `except Exception` swallowed as "❌ Reconnection failed".
Every NetR9/NetRS reconnect therefore failed, silently, and the retry reused
the same stale session.

Driving observation: 103 occurrences in rek-d01's `receivers.log` in a single
day (2026-08-29), across SJUK, GAKE, GJOG and others.
"""

import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from receivers.trimble.http_download_client import NetR9HTTPDownloader

# This repo is public, so nothing here may carry real network detail or
# credentials. 192.0.2.0/24 is TEST-NET-1 (RFC 5737), reserved for
# documentation and guaranteed never to route; the username/password are
# obvious dummies. The client is mocked, so no value here is ever dialled.
STATION_CONFIG = {
    "ip_address": "192.0.2.10",
    "http_port": 80,
    "username": "dummy-user",
    "password": "dummy-pass",
}


@pytest.fixture
def downloader():
    """A downloader with every outbound dependency mocked out."""
    cfg = Mock()
    cfg.get_receiver_config.return_value = {}

    with (
        patch(
            "receivers.trimble.http_download_client.TrimbleHTTPClient"
        ) as mock_client_cls,
        patch(
            "receivers.config.receivers_config.get_receivers_config", return_value=cfg
        ),
        patch("receivers.utils.stall_timeout.get_stall_timeout", return_value=5),
    ):
        dl = NetR9HTTPDownloader("SJUK", STATION_CONFIG)
        dl._mock_client_cls = mock_client_cls
        yield dl


def test_station_config_is_retained_for_reconnect(downloader):
    """The reconnect needs the config; __init__ must keep a reference to it."""
    assert downloader.station_config == STATION_CONFIG


def test_reconnect_rebuilds_client_with_real_signature(downloader, caplog):
    """A timeout mid-download must rebuild the client, not die in the handler."""
    downloader._discover_base_path = Mock(return_value="")
    downloader.file_validator.should_resume_download = Mock(return_value=(False, 0))

    # Every attempt fails with a reconnect-triggering error.
    downloader.http_client.session.get.side_effect = requests.exceptions.ConnectTimeout(
        "connection refused"
    )
    downloader._mock_client_cls.reset_mock()

    with (
        patch("receivers.trimble.http_download_client.time.sleep"),
        patch("receivers.utils.stall_timeout.record_download"),
        caplog.at_level(logging.INFO),
    ):
        result = downloader.download_file(
            remote_path="/Internal",
            filename="SJUK2420.26d.Z",
            local_path=Path("/nonexistent/SJUK2420.26d.Z"),
            max_retries=1,
        )

    assert result is False  # the download still fails — that is not what we fixed

    # The regression: reconnection must actually happen.
    assert "Reconnection failed" not in caplog.text
    assert "HTTP client reconnected" in caplog.text

    # And it must use the real 2-arg signature.
    assert downloader._mock_client_cls.call_count >= 1
    args, kwargs = downloader._mock_client_cls.call_args
    assert args == ("SJUK", STATION_CONFIG)
    assert not kwargs


def test_reconnect_does_not_reference_undefined_attributes(downloader):
    """Guard the specific shape of the bug: no bogus attrs on the downloader.

    These four were referenced by the dead six-arg call. They never existed on
    the downloader — `ip`/`http_port` live on the *client* (`self.http_client.ip`
    is used to build the URL), which is how the confusion arose.
    """
    for attr in ("ip", "http_port", "username", "password"):
        assert not hasattr(downloader, attr), (
            f"{attr!r} is set on the downloader — if this is now real, the "
            "reconnect test above must be revisited"
        )
