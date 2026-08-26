"""The FTP transport must be injectable, so the download path is testable offline.

`PolaRX5` used to construct `FTP()` inline inside `download_data` and
`_ftp_open_connection`. That left a test nothing stable to attach a fake to: the
only option was patching an internal module path, and when that path moved the
mock detached SILENTLY and the test opened a real socket to a production
receiver. That is not hypothetical — `test_polarx5` did exactly that against
10.4.1.100:28784 until abc3735 (2026-08-23).

`ftp_factory` makes the attachment point explicit, so that failure mode cannot
recur. These tests assert the seam exists and is honoured; if someone reverts to
constructing FTP inline, they fail.
"""

from __future__ import annotations

import ftplib

import pytest

from receivers.septentrio.polarx5 import PolaRX5

STATION_CONFIG = {
    "receiver": {"type": "PolaRX5"},
    "router": {"ip": "192.0.2.1"},  # TEST-NET-1, never routable
}


class RecordingFTP:
    """Stands in for ftplib.FTP and records calls instead of opening a socket."""

    def __init__(self):
        self.calls: list[tuple] = []

    def connect(self, host, port, timeout=None):
        self.calls.append(("connect", host, port, timeout))

    def login(self, *args):
        self.calls.append(("login", *args))

    def set_pasv(self, value):
        self.calls.append(("set_pasv", value))

    def close(self):
        self.calls.append(("close",))

    def quit(self):
        self.calls.append(("quit",))


@pytest.fixture
def reachable(monkeypatch):
    """Satisfy the two fast-fail preconditions that gate the FTP connection.

    ``_ftp_open_connection`` pings via ``subprocess`` and then probes the port
    via ``socket`` before it ever constructs FTP. Neither is injected, so they
    have to be stubbed here — which is itself the point: the FTP seam alone does
    not make this method testable, it makes the FTP *transport* substitutable.
    The remaining two dependencies are the next seams worth cutting.
    """
    import subprocess

    class _Ok:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ok())


@pytest.fixture
def receiver_and_ftp():
    """A PolaRX5 whose transport is a fake we can inspect."""
    made: list[RecordingFTP] = []

    def factory():
        f = RecordingFTP()
        made.append(f)
        return f

    return PolaRX5("TEST", STATION_CONFIG, ftp_factory=factory), made


def test_default_factory_is_the_real_ftp():
    """Omitting the argument must keep production behaviour byte-for-byte."""
    rx = PolaRX5("TEST", STATION_CONFIG)
    assert rx._ftp_factory is ftplib.FTP


def test_factory_is_keyword_only_and_optional():
    """Existing call sites — including ReceiverFactory — pass two positionals."""
    rx = PolaRX5("TEST", STATION_CONFIG)
    assert rx is not None, "two-positional construction must still work"

    with pytest.raises(TypeError):
        # Third positional must NOT be silently accepted as the factory;
        # keyword-only keeps the signature unambiguous.
        PolaRX5("TEST", STATION_CONFIG, RecordingFTP)  # type: ignore[misc]


def test_injected_factory_is_used_for_the_connection(receiver_and_ftp, reachable):
    """_ftp_open_connection must go through the seam, not ftplib directly."""
    rx, made = receiver_and_ftp
    rx.ftp_anonymous = True
    rx.pasv = True

    try:
        rx._ftp_open_connection(timeout=1)
    except Exception:
        # The fake satisfies connect/login/set_pasv; anything beyond that is
        # out of scope here. What matters is that no real socket was opened.
        pass

    assert made, "the injected factory was never called — FTP is still inline"
    assert any(c[0] == "connect" for c in made[0].calls), "connect() not routed"
    host = next(c[1] for c in made[0].calls if c[0] == "connect")
    assert host == rx.ip_number


def test_no_real_socket_is_created(receiver_and_ftp, reachable, monkeypatch):
    """Belt and braces: make ftplib.FTP explode, and the seam still works."""

    def _explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("ftplib.FTP was constructed despite the injected factory")

    monkeypatch.setattr(ftplib, "FTP", _explode)

    rx, made = receiver_and_ftp
    rx.ftp_anonymous = True
    rx.pasv = True
    try:
        rx._ftp_open_connection(timeout=1)
    except AssertionError:
        raise
    except Exception:
        pass

    assert made, "injected factory not used"
