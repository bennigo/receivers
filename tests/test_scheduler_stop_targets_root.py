"""`scheduler stop` must signal the scheduler, not its forked workers as well.

Measured on rek-d01, 2026-08-19: 17 processes match the finder — the main
scheduler and its 16 RINEX pool workers, which are forked and therefore inherit
the parent's cmdline verbatim (`receivers scheduler start`). The old code
terminated all 17 SERIALLY with `wait(timeout=30)` each, so ExecStop needed up
to 17 x 30 = 510 s against the unit's `TimeoutStopSec=120`. It could never
finish: systemd SIGKILLed the whole cgroup on every deploy (`Result=signal`,
unit `failed`, 4m0.4s = 8 timeouts x 30 s).

It got worse silently when the RINEX pool went 4 -> 16 workers. At 4 workers the
serial path was 5 x 30 = 150 s — already over budget, just less obviously.

Two properties are pinned here:
  1. only ROOT processes are signalled (a matched process whose parent is also
     matched is a worker and must be left to its parent);
  2. the waits OVERLAP — signal everything, then wait once — so total time is
     bounded by the timeout, not by timeout x process-count.
"""

from __future__ import annotations

import argparse
import sys
import types
from unittest.mock import MagicMock

import pytest

from receivers.cli import scheduler as sch

_CMD = ["/venv/bin/python3", "/venv/bin/receivers", "scheduler", "start"]

MAIN_PID = 2865850
WORKER_PIDS = list(range(2867437, 2867453))  # 16 workers, as measured


class _FakeProc:
    """Stand-in for psutil.Process with the attributes the stop path touches."""

    def __init__(self, pid, ppid, registry):
        self.pid = pid
        self.info = {"pid": pid, "ppid": ppid, "name": "python3", "cmdline": _CMD}
        self._registry = registry

    def terminate(self):
        self._registry["terminated"].append(self.pid)

    def kill(self):
        self._registry["killed"].append(self.pid)


@pytest.fixture
def psutil_stub(monkeypatch):
    """A fake psutil exposing exactly the surface cmd_scheduler_stop uses."""
    registry = {"terminated": [], "killed": [], "wait_calls": []}

    procs = [_FakeProc(MAIN_PID, 1, registry)]
    procs += [_FakeProc(p, MAIN_PID, registry) for p in WORKER_PIDS]
    by_pid = {p.pid: p for p in procs}

    fake = types.ModuleType("psutil")
    fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake.AccessDenied = type("AccessDenied", (Exception,), {})
    fake.TimeoutExpired = type("TimeoutExpired", (Exception,), {})
    fake.process_iter = lambda attrs=None: list(procs)
    fake.Process = lambda pid: by_pid[pid]

    def wait_procs(plist, timeout=None):
        registry["wait_calls"].append((sorted(p.pid for p in plist), timeout))
        return list(plist), []  # everything exits gracefully

    fake.wait_procs = wait_procs
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(sch, "HAS_APSCHEDULER", True, raising=False)
    return registry


def _run(force=False, timeout=60.0):
    return sch.cmd_scheduler_stop(argparse.Namespace(force=force, timeout=timeout))


class TestOnlyTheRootIsSignalled:
    def test_workers_are_not_terminated_individually(self, psutil_stub):
        assert _run() == 0
        assert psutil_stub["terminated"] == [MAIN_PID]

    def test_seventeen_matches_become_one_signal(self, psutil_stub):
        # The regression in one number: 17 matched, 1 signalled.
        _run()
        assert len(psutil_stub["terminated"]) == 1

    def test_force_also_targets_only_the_root(self, psutil_stub):
        assert _run(force=True) == 0
        assert psutil_stub["killed"] == [MAIN_PID]


class TestWaitsOverlap:
    def test_a_single_wait_covers_all_signalled_processes(self, psutil_stub):
        _run(timeout=45.0)
        # Exactly one graceful wait, carrying the full budget — not one wait per
        # process, which is what multiplied 30 s into 510 s.
        assert len(psutil_stub["wait_calls"]) == 1
        pids, timeout = psutil_stub["wait_calls"][0]
        assert pids == [MAIN_PID]
        assert timeout == 45.0

    def test_default_timeout_stays_under_the_units_timeoutstopsec(self, psutil_stub):
        # TimeoutStopSec=120 on gps-receivers-scheduler.service. If ExecStop can
        # outlive that, systemd SIGKILLs the cgroup and the jobstore is never
        # closed cleanly — the exact failure this test exists for.
        _run()
        _, timeout = psutil_stub["wait_calls"][0]
        assert timeout < 120, "graceful budget must fit inside TimeoutStopSec"


class TestNoSchedulerRunning:
    def test_reports_and_returns_zero(self, monkeypatch):
        fake = types.ModuleType("psutil")
        fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        fake.TimeoutExpired = type("TimeoutExpired", (Exception,), {})
        fake.process_iter = lambda attrs=None: []
        fake.wait_procs = lambda p, timeout=None: ([], [])
        monkeypatch.setitem(sys.modules, "psutil", fake)
        monkeypatch.setattr(sch, "HAS_APSCHEDULER", True, raising=False)
        assert _run() == 0


class TestSignalHelper:
    def test_already_exited_process_is_not_an_error(self, monkeypatch):
        fake = types.ModuleType("psutil")
        fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        monkeypatch.setitem(sys.modules, "psutil", fake)
        p = MagicMock(pid=1)
        assert sch._signal(p, MagicMock(side_effect=fake.NoSuchProcess)) is False

    def test_access_denied_is_reported_not_raised(self, monkeypatch, capsys):
        # A scheduler owned by another user must not abort the whole stop.
        fake = types.ModuleType("psutil")
        fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        monkeypatch.setitem(sys.modules, "psutil", fake)
        p = MagicMock(pid=99)
        assert sch._signal(p, MagicMock(side_effect=fake.AccessDenied)) is False
        assert "99" in capsys.readouterr().out

    def test_success_returns_true(self, monkeypatch):
        fake = types.ModuleType("psutil")
        fake.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake.AccessDenied = type("AccessDenied", (Exception,), {})
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert sch._signal(MagicMock(pid=1), MagicMock()) is True
