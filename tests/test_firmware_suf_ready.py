"""Tests for the ``Ready for SUF download`` handshake.

The bug these pin down: ``sock.recv()`` returns ``b""`` on EOF, and the original
loop also produced ``b""`` for a timeout. EOF was therefore swallowed as "no data
yet", the loop spun out its whole budget, and the flash aborted with
"receiver never signalled 'Ready for SUF download'" — which is true but hides
that the receiver had actually hung up. Hit for real flashing ISAK 5.6.0→5.7.0
on 2026-07-31.

Every failure path here must leave the firmware unstreamed.
"""

from __future__ import annotations

import pytest

from receivers.septentrio.firmware_upgrade import (
    FirmwareUpgradeError,
    await_suf_ready,
)


class FakeSocket:
    """Minimal socket stand-in driven by a scripted list of recv outcomes.

    Each entry is either ``bytes`` (returned) or an exception (raised).
    ``b""`` means EOF, exactly as the real socket reports a closed peer.
    """

    def __init__(self, script):
        self.script = list(script)
        self.timeout = None
        self.sent = b""

    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data):
        self.sent += data

    def recv(self, _n):
        if not self.script:
            raise TimeoutError("no more scripted data")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_ready_returns_text():
    sock = FakeSocket([b"$R: exeResetReceiver\r\nReady for SUF download\r\n"])
    assert "Ready for SUF download" in await_suf_ready(sock, ready_timeout_s=5)


def test_ready_split_across_chunks():
    """The marker must be matched across recv boundaries, not per chunk."""
    sock = FakeSocket([b"$R: exeResetRec", b"eiver\r\nReady for S", b"UF download\r\n"])
    assert "Ready for SUF download" in await_suf_ready(sock, ready_timeout_s=5)


def test_ready_survives_interleaved_timeouts():
    sock = FakeSocket(
        [
            TimeoutError(),
            b"$R: exeResetReceiver\r\n",
            TimeoutError(),
            b"Ready for SUF download\r\n",
        ]
    )
    assert "Ready for SUF download" in await_suf_ready(sock, ready_timeout_s=5)


def test_eof_is_reported_as_a_closed_connection_not_a_timeout():
    """The regression: EOF must not masquerade as 'never signalled'."""
    sock = FakeSocket([b"$R: exeResetReceiver\r\n", b""])
    with pytest.raises(FirmwareUpgradeError) as exc:
        await_suf_ready(sock, ready_timeout_s=30)
    msg = str(exc.value)
    assert "closed the connection" in msg
    # The operator must be told nothing was flashed...
    assert "no firmware was streamed" in msg
    # ...and see what the receiver actually said before hanging up.
    assert "exeResetReceiver" in msg


def test_eof_fails_fast_rather_than_burning_the_whole_budget():
    """EOF is terminal — waiting out a 300s budget helps nobody."""
    sock = FakeSocket([b""])
    with pytest.raises(FirmwareUpgradeError):
        await_suf_ready(sock, ready_timeout_s=300)
    # Nothing left unconsumed: it returned on the first recv, not after polling.
    assert sock.script == []


def test_socket_error_is_distinguished_from_silence():
    sock = FakeSocket([ConnectionResetError("connection reset by peer")])
    with pytest.raises(FirmwareUpgradeError) as exc:
        await_suf_ready(sock, ready_timeout_s=5)
    assert "socket error" in str(exc.value)
    assert "no firmware was streamed" in str(exc.value)


def test_genuine_timeout_still_reports_a_timeout():
    sock = FakeSocket([TimeoutError()] * 50)
    with pytest.raises(FirmwareUpgradeError) as exc:
        await_suf_ready(sock, ready_timeout_s=0.3)
    msg = str(exc.value)
    assert "never signalled" in msg
    assert "no firmware was streamed" in msg


def test_timeout_message_quotes_whatever_did_arrive():
    """A receiver that answers but never readies must not look silent."""
    sock = FakeSocket(
        [b"$R? exeResetReceiver: Invalid argument\r\n"] + [TimeoutError()] * 50
    )
    with pytest.raises(FirmwareUpgradeError) as exc:
        await_suf_ready(sock, ready_timeout_s=0.3)
    assert "Invalid argument" in str(exc.value)


def test_timeouterror_is_caught_before_oserror():
    """TimeoutError subclasses OSError — catch order must not flip them.

    If OSError were caught first every poll would abort as a socket error.
    """
    sock = FakeSocket([TimeoutError(), b"Ready for SUF download\r\n"])
    assert "Ready for SUF download" in await_suf_ready(sock, ready_timeout_s=5)
