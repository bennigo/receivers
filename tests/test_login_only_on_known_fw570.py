"""A login is sent ONLY to a receiver known to be >= 5.7.0.

Login is the only thing that feeds the receiver's brute-force counter. On 5.7.0
a tripped counter is a GLOBAL lockout — every user including the factory
account, which blocks `rec-provision`, the command needed to recover.

Measured on rek-d01 2026-08-17: the health extractor sent ~8 rejected logins per
cycle across 79 stations (~159,000/day). Harmless on pre-5.7, so it went
unnoticed for months — then ELDC was flashed to 5.7.0 and the counter tripped
within minutes, costing 3.5 h of downtime with FTP closed.

Root cause was divergence, not oversight: `septentrio/tcp_client.py:157` already
consulted `should_attempt_login()`, while `health/polarx5_tcp_extractor.py`
deliberately did not. Two implementations of one decision — the same failure
class as the Trimble converter split. These tests pin BOTH paths.
"""

from unittest.mock import MagicMock, patch

import pytest

from receivers.septentrio.fw_policy import (
    firmware_requires_auth,
    should_attempt_login,
)

# --- the policy itself ------------------------------------------------------


@pytest.mark.parametrize("fw", ["5.7.0", "5.7.1", "5.8.0", "6.0.0"])
def test_known_570_or_newer_logs_in(fw):
    assert should_attempt_login(fw) is True


@pytest.mark.parametrize("fw", ["5.6.0", "5.5.0", "5.4.0", "5.2.0", "4.10", "4.8.0"])
def test_known_pre_570_does_not_log_in(fw):
    """91 of 113 Septentrio stations were in this bracket."""
    assert should_attempt_login(fw) is False


@pytest.mark.parametrize("fw", [None, "", "   ", "garbage", "not.a.version"])
def test_unknown_firmware_does_NOT_log_in(fw):
    """Changed 2026-08-17: unknown used to mean "try anyway".

    A login that cannot be known to succeed is pure lockout risk. A 5.7.0
    receiver still reveals itself via "Not authorized" on the command path,
    which warns and names the fix — loud, and cleared by one rec-provision.
    """
    assert should_attempt_login(fw) is False


def test_requires_auth_still_fails_SAFE_on_unknown():
    """The two policies deliberately disagree on unknown, and must keep doing so.

    should_attempt_login(None) is False  -> do not risk the counter.
    firmware_requires_auth(None) is True -> assume commands need auth, so the
    caller reports "unavailable" instead of silently believing refused output.
    Both choices fail toward the safe side of their own question.
    """
    assert should_attempt_login(None) is False
    assert firmware_requires_auth(None) is True


# --- the extractor path (the one that had drifted) --------------------------


def _extractor(fw):
    from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

    ex = PolaRX5TCPExtractor.__new__(PolaRX5TCPExtractor)
    ex.station_id = "TEST"
    ex.firmware_version = fw
    ex.tcp_username = "u"
    ex.tcp_password = "p"
    ex._auth_failed = False
    ex.logger = MagicMock()
    return ex


@pytest.mark.parametrize("fw", ["5.6.0", "5.5.0", None, "garbage"])
def test_extractor_sends_NOTHING_on_the_wire_below_570(fw):
    """The regression that caused the ELDC lockout: bytes must not be sent."""
    ex = _extractor(fw)
    sock = MagicMock()
    assert ex._login(sock) is True
    sock.sendall.assert_not_called(), "a login was transmitted despite the guard"


def test_extractor_still_logs_in_on_570():
    ex = _extractor("5.7.0")
    sock = MagicMock()
    sock.recv.return_value = b"$R! LogIn\r\nIP10>"
    ex._login(sock)
    sock.sendall.assert_called_once()
    sent = sock.sendall.call_args[0][0].decode()
    assert sent.startswith("login, u, p")


def test_extractor_credentials_absent_is_unchanged():
    """No credentials configured must not start sending logins."""
    ex = _extractor("5.7.0")
    ex.tcp_username = None
    sock = MagicMock()
    ex._login(sock)
    sock.sendall.assert_not_called()


# --- both comms paths must consult the one policy ---------------------------


def test_both_paths_consult_the_shared_policy():
    """tcp_client and the extractor must not re-derive the rule separately.

    tcp_client.py:157 always had the guard; the extractor did not. That
    divergence is the defect this test exists to prevent recurring.
    """
    import inspect

    from receivers.health import polarx5_tcp_extractor
    from receivers.septentrio import tcp_client

    for mod, name in (
        (tcp_client, "tcp_client"),
        (polarx5_tcp_extractor, "polarx5_tcp_extractor"),
    ):
        src = inspect.getsource(mod)
        assert "should_attempt_login" in src, (
            f"{name} does not consult should_attempt_login — the two receiver-"
            "comms paths have diverged again"
        )
