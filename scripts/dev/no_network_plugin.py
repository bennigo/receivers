"""pytest plugin: make any real outbound socket connection impossible.

This suite is known to reach live GNSS receivers (10.4.1.x) and the live TOS
API when a mock detaches. Fail loudly instead of touching production.

Usage::

    PYTHONPATH=scripts/dev pytest tests/ -p no_network_plugin

Keep it. It converts "this test might call production" from a silent hazard
into an immediate, named failure — and it is what surfaced the two
`TestSitelogDatedSeries` tests that pass ONLY because they phone the live TOS
API at 10.254.0.12. Those two are left failing rather than papered over:
`generate_site_log` needs a client seam.
"""

import socket


class NetworkBlockedError(RuntimeError):
    """Raised in place of any outbound connection attempt."""


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create = socket.create_connection


def _blocked(*a, **k):
    raise NetworkBlockedError(
        f"outbound network blocked by test guard: {a[1] if len(a) > 1 else a}"
    )


def pytest_configure(config):
    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.create_connection = _blocked


def pytest_unconfigure(config):
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.create_connection = _real_create
