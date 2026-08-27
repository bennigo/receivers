"""Unit tests for the pure-logic pieces of septentrio.firmware_upgrade.

The flash itself needs a real receiver; these cover the bits that don't:
version parsing, content hashing, and the upgrade-mode readiness handshake.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.septentrio import firmware_upgrade as fw


def test_sha256_of(tmp_path):
    p = tmp_path / "x.suf"
    p.write_bytes(b"hello")
    import hashlib

    assert fw.sha256_of(p) == hashlib.sha256(b"hello").hexdigest()


def test_read_firmware_version_labeled():
    sock = MagicMock()
    sock.sendall = MagicMock()
    sock.recv = MagicMock(side_effect=[b"... Firmware: 5.7.0 ...\nIP10>", b""])
    assert fw.read_firmware_version(sock) == "5.7.0"


def test_read_firmware_version_bare_triplet():
    sock = MagicMock()
    sock.recv = MagicMock(side_effect=[b"blah 5.6.0 blah IP10>", b""])
    assert fw.read_firmware_version(sock) == "5.6.0"


def test_read_firmware_version_none():
    sock = MagicMock()
    sock.recv = MagicMock(side_effect=[b"no version here IP10>", b""])
    assert fw.read_firmware_version(sock) is None


def test_stream_suf_aborts_without_ready_signal(tmp_path):
    """If the receiver never says 'Ready for SUF download', stream_suf must raise
    BEFORE sending any firmware bytes."""
    suf = tmp_path / "PolaRx5-5.7.0.suf"
    suf.write_bytes(b"\x00" * 4096)
    sent = []
    sock = MagicMock()
    sock.sendall = MagicMock(side_effect=lambda b: sent.append(b))
    sock.recv = MagicMock(return_value=b"garbage prompt IP10>")  # never "Ready"

    with pytest.raises(fw.FirmwareUpgradeError):
        fw.stream_suf(sock, suf, ready_timeout_s=0.3)

    # Only the exeResetReceiver command was sent — no firmware payload.
    assert sent == [b"exeResetReceiver, Upgrade, none\n"]


def test_wait_for_reboot_and_verify_stops_on_login_rejection(monkeypatch):
    """A rejected login must abort the poll loop immediately.

    Retrying turns one rejected login into ~24 over the 240s reboot window, and
    on 5.7.0 that trips the GLOBAL brute-force lockout that blocks rec-provision
    itself. So a single False from login() must stop the loop.
    """
    calls = {"login": 0}
    fake_sock = MagicMock()

    def fake_connect_control(ip, port, **kw):
        return fake_sock, False

    def fake_login(sock, username, password):
        calls["login"] += 1
        return False  # definitive rejection (wrong creds or lockout)

    read_version = MagicMock()
    monkeypatch.setattr(fw, "connect_control", fake_connect_control)
    monkeypatch.setattr(fw, "login", fake_login)
    monkeypatch.setattr(fw, "read_firmware_version", read_version)

    with pytest.raises(fw.FirmwareUpgradeError, match="login was rejected"):
        fw.wait_for_reboot_and_verify(
            "10.0.0.1",
            28784,
            username="u",
            password="p",
            expect_version="5.7.0",
            reboot_wait_s=30,
            poll_every_s=1,
        )

    # login attempted exactly ONCE, and the version was never read.
    assert calls["login"] == 1
    read_version.assert_not_called()


def test_wait_for_reboot_and_verify_success(monkeypatch):
    """A successful login proceeds to read and confirm the firmware version."""
    fake_sock = MagicMock()
    monkeypatch.setattr(
        fw, "connect_control", lambda ip, port, **kw: (fake_sock, False)
    )
    monkeypatch.setattr(fw, "login", lambda sock, username, password: True)
    monkeypatch.setattr(fw, "read_firmware_version", lambda sock: "5.7.0")

    ver = fw.wait_for_reboot_and_verify(
        "10.0.0.1",
        28784,
        username="u",
        password="p",
        expect_version="5.7.0",
        reboot_wait_s=30,
        poll_every_s=1,
    )
    assert ver == "5.7.0"
