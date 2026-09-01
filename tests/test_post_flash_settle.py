"""The post-flash login must not be fired the instant the port reopens.

`wait_for_reboot_and_verify` polls until the control port accepts a TCP
connection and then logs in. But an open port is not a ready receiver: the
listener returns well before the auth subsystem does, so that first login is
rejected for "not ready" — indistinguishable from bad credentials. The function
correctly refuses to retry a rejection, so that single premature attempt is the
whole outcome, and on 5.7.0 it also counts toward the GLOBAL brute-force
lockout.

Measured 2026-09-01: ELEY and GFUM were each locked out ~24 h exactly this way
(85,484 s and 86,296 s remaining), while FIM2 — same firmware, same
credentials, same SSH key already pushed — logged in on the first try when the
identical rec-provision was run by hand ~4 minutes after its flash.

So the fix is a settle FLOOR, not a retry loop: still exactly one login
attempt, just spent late enough to mean something.
"""

from __future__ import annotations

import pytest

from receivers.septentrio import firmware_upgrade as fw


class _Clock:
    """Deterministic time: sleep() advances it, so no test actually waits."""

    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def clock():
    return _Clock()


def _wire(monkeypatch, *, port_open_after=0, login_ok=True, version="5.7.0"):
    """Fake a receiver whose port opens after N connect attempts."""

    class _Sock:
        def close(self):
            pass

    state = {"connects": 0, "logins": [], "sock": _Sock()}

    def connect_control(ip, endpoint, force_tls=False, timeout=10):
        state["connects"] += 1
        if state["connects"] <= port_open_after:
            raise fw.FirmwareUpgradeError("refused")
        return state["sock"], None

    def login(sock, user, pwd):
        state["logins"].append(True)
        return login_ok

    monkeypatch.setattr(fw, "connect_control", connect_control)
    monkeypatch.setattr(fw, "login", login)
    monkeypatch.setattr(fw, "read_firmware_version", lambda sock: version)
    return state


class TestItWaitsBeforeSpendingTheOneLoginAttempt:
    def test_the_settle_window_elapses_before_any_login(self, monkeypatch, clock):
        state = _wire(monkeypatch)
        fw.wait_for_reboot_and_verify(
            "10.0.0.1",
            28784,
            username="u",
            password="p",
            expect_version="5.7.0",
            settle_s=180,
            poll_every_s=10,
            sleep=clock.sleep,
            now=clock.time,
        )
        assert 180 in clock.slept, (
            f"no 180s settle before login; slept {clock.slept} — the login was "
            "fired as soon as the port answered, which is the bug"
        )
        assert len(state["logins"]) == 1, "must be exactly ONE login attempt"

    def test_settle_starts_when_the_port_returns_not_when_we_began(
        self, monkeypatch, clock
    ):
        """A slow reboot must still get its full settle, not a truncated one."""
        _wire(monkeypatch, port_open_after=3)
        fw.wait_for_reboot_and_verify(
            "10.0.0.1",
            28784,
            username="u",
            password="p",
            expect_version="5.7.0",
            settle_s=180,
            poll_every_s=10,
            sleep=clock.sleep,
            now=clock.time,
        )
        assert clock.slept.count(180) == 1
        # The settle must come AFTER the polling that waited for the port, and
        # must be the FULL 180 — not a remainder trimmed by time already spent
        # waiting for a slow reboot. Each poll tries both the plaintext and the
        # TLS endpoint, so polls != connect attempts; assert the shape.
        idx = clock.slept.index(180)
        assert idx >= 1, f"settle came before any polling: {clock.slept}"
        assert set(clock.slept[:idx]) == {
            10
        }, f"expected only 10s polls before the settle, got {clock.slept[:idx]}"

    def test_settle_is_paid_once_not_per_poll(self, monkeypatch, clock):
        _wire(monkeypatch)
        fw.wait_for_reboot_and_verify(
            "10.0.0.1",
            28784,
            username="u",
            password="p",
            expect_version="5.7.0",
            settle_s=180,
            poll_every_s=10,
            sleep=clock.sleep,
            now=clock.time,
        )
        assert clock.slept.count(180) == 1

    def test_zero_settle_restores_the_immediate_attempt(self, monkeypatch, clock):
        """The escape hatch must genuinely skip the wait."""
        _wire(monkeypatch)
        fw.wait_for_reboot_and_verify(
            "10.0.0.1",
            28784,
            username="u",
            password="p",
            expect_version="5.7.0",
            settle_s=0,
            poll_every_s=10,
            sleep=clock.sleep,
            now=clock.time,
        )
        assert 0 not in clock.slept and 180 not in clock.slept


class TestARejectedLoginIsStillNeverRetried:
    """The settle must not weaken the no-retry rule that limits the damage."""

    def test_one_rejection_aborts_immediately(self, monkeypatch, clock):
        state = _wire(monkeypatch, login_ok=False)
        with pytest.raises(fw.FirmwareUpgradeError, match="login was rejected"):
            fw.wait_for_reboot_and_verify(
                "10.0.0.1",
                28784,
                username="u",
                password="p",
                expect_version="5.7.0",
                settle_s=180,
                poll_every_s=10,
                sleep=clock.sleep,
                now=clock.time,
            )
        assert len(state["logins"]) == 1, (
            f"{len(state['logins'])} login attempts after a rejection — each one "
            "feeds the ~24h lockout counter"
        )


class TestTheDefaultsLeaveRoomForTheSettle:
    def test_reboot_wait_exceeds_the_settle(self):
        assert fw.DEFAULT_REBOOT_WAIT_S > fw.DEFAULT_POST_FLASH_SETTLE_S, (
            "the reboot window must be able to contain the settle, or the "
            "function times out before it ever attempts its login"
        )

    def test_settle_covers_the_observed_recovery_delay(self):
        """FIM2 logged in cleanly ~4 min after its flash; ELEY/GFUM at ~0 did not."""
        assert fw.DEFAULT_POST_FLASH_SETTLE_S >= 120
