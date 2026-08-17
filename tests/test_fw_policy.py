"""Tests for firmware-aware receiver communication.

fw 5.7.0 enforces TCP auth; earlier versions accept the ``login`` command but
ignore it. Guessing wrong is harmful in BOTH directions:

* assume no-auth on a 5.7.0 box → every command is silently rejected while the
  caller reports success;
* send a doomed login to a pre-5.7 box → the rejection still increments the
  receiver's brute-force counter, which becomes a ~9h GLOBAL lockout (factory
  account included) the moment that station is upgraded. That is exactly how
  ISAK became unreachable on 2026-08-01.
"""

from __future__ import annotations

import pytest

from receivers.septentrio.fw_policy import (
    firmware_requires_auth,
    parse_firmware,
    should_attempt_login,
)


class TestParse:
    @pytest.mark.parametrize(
        "text,expected",
        [("5.7.0", (5, 7, 0)), ("5.6.0", (5, 6, 0)), (" 5.7.0 ", (5, 7, 0))],
    )
    def test_parses(self, text, expected):
        assert parse_firmware(text) == expected

    @pytest.mark.parametrize("bad", [None, "", "unknown", "5.x.0", "abc"])
    def test_unparseable_is_none(self, bad):
        assert parse_firmware(bad) is None


class TestRequiresAuth:
    @pytest.mark.parametrize("fw", ["5.7.0", "5.7.1", "5.8.0", "6.0.0"])
    def test_57_and_later_enforce_auth(self, fw):
        assert firmware_requires_auth(fw) is True

    @pytest.mark.parametrize("fw", ["5.6.0", "5.5.0", "5.4.0", "5.2.0"])
    def test_pre_57_does_not(self, fw):
        assert firmware_requires_auth(fw) is False

    @pytest.mark.parametrize("fw", [None, "", "unknown"])
    def test_unknown_assumes_auth_required(self, fw):
        """Safer direction: a confusing failure beats silently-rejected commands."""
        assert firmware_requires_auth(fw) is True


class TestShouldAttemptLogin:
    @pytest.mark.parametrize("fw", ["5.6.0", "5.5.0", "5.2.0"])
    def test_known_pre_57_does_not_send_login(self, fw):
        """The lockout guard — nothing to gain, a lockout to lose."""
        assert should_attempt_login(fw) is False

    @pytest.mark.parametrize("fw", ["5.7.0", "6.0.0"])
    def test_57_plus_sends_login(self, fw):
        assert should_attempt_login(fw) is True

    @pytest.mark.parametrize("fw", [None, "unknown"])
    def test_unknown_does_NOT_try(self, fw):
        """INVERTED 2026-08-17. Was: "can't know — must attempt".

        The old reasoning was that a 5.7.0 box would otherwise reject
        everything. It still reveals itself — via "Not authorized" on the
        COMMAND path, which warns and names the fix
        (polarx5_tcp_extractor.py:1153-1161). That is exactly how ELDC's
        post-flash state was diagnosed.

        Meanwhile the cost of guessing wrong the other way is a GLOBAL 3.5-9 h
        lockout that also blocks rec-provision, the command needed to recover.
        A login that cannot be known to succeed is pure risk, so unknown now
        means "do not send".
        """
        assert should_attempt_login(fw) is False


def test_extractor_alias_still_resolves():
    """download_manager and polarx5 import the old private name; keep it working."""
    from receivers.health.polarx5_tcp_extractor import _firmware_requires_auth

    assert _firmware_requires_auth("5.7.0") is True
    assert _firmware_requires_auth("5.6.0") is False


class TestClientWiring:
    def _client(self, fw):
        from receivers.septentrio.tcp_client import PolaRX5TCPClient

        return PolaRX5TCPClient(
            "192.0.2.1", "TEST", username="u", password="p", firmware_version=fw
        )

    def test_defaults_to_unknown_not_a_crash(self):
        from receivers.septentrio.tcp_client import PolaRX5TCPClient

        c = PolaRX5TCPClient("192.0.2.1", "TEST")
        assert c.firmware_version is None
        assert c.auth_failed is False

    def test_carries_firmware(self):
        assert self._client("5.6.0").firmware_version == "5.6.0"

    def test_pre_57_login_is_skipped_entirely(self):
        """No socket traffic at all — that is what avoids feeding the counter."""
        c = self._client("5.6.0")
        sent = []

        class FakeSock:
            def sendall(self, data):
                sent.append(data)

            def settimeout(self, _):
                pass

            def recv(self, _):
                return b""

        c._sock = FakeSock()
        c._login()
        assert sent == []
        assert c.auth_failed is False
