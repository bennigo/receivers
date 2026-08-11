"""A conversion timeout must leave NOTHING running behind it.

`subprocess.run(timeout=...)` kills only its direct child. On every other
converter that child is the tool itself, so the timeout works. The native
Trimble path launches wine, and the real `convertToRinex.exe` runs as a
grandchild in a different process group — measured live on rek-d01 2026-08-11:

    pid 864860  ppid=860296  pgid=860296  cpu=0.1%   <- wine launcher
    pid 864904  ppid=864881  pgid=864904  cpu=77.4%  <- the real worker

So the old code reaped the launcher and orphaned the worker at 100% CPU forever;
the resulting ConversionError triggered a retry, which leaked another. Four had
accumulated on one truncated input, two of them 2h24m old, holding ~400% CPU.

The first test below spawns a REAL process tree and asserts the grandchild is
dead afterwards. It fails against a plain `proc.kill()` implementation, which is
the whole point — the rest are unit-level guards.
"""

import os
import subprocess
import time

import pytest

from receivers.rinex.trimble_native_converter import TrimbleNativeConverter


def _converter():
    """A bare instance — the method under test needs only .logger and the grace."""
    import logging

    c = TrimbleNativeConverter.__new__(TrimbleNativeConverter)
    c.logger = logging.getLogger("test.trimble_native")
    return c


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_timeout_kills_the_grandchild_not_just_the_direct_child(tmp_path):
    """The regression: a grandchild must not survive the timeout.

    The shell is the direct child (the stand-in for wine); the backgrounded
    sleep is the grandchild (the stand-in for convertToRinex.exe).
    """
    pidfile = tmp_path / "grandchild.pid"
    cmd = ["sh", "-c", f"sleep 300 & echo $! > {pidfile}; wait"]

    with pytest.raises(subprocess.TimeoutExpired):
        _converter()._run_group_killable(cmd, timeout=1)

    grandchild = int(pidfile.read_text().strip())
    deadline = time.time() + 5
    while _alive(grandchild) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), (
        f"grandchild {grandchild} survived the timeout — the process group was "
        "not killed, so this is the orphaned-worker bug"
    )


def test_child_runs_in_its_own_process_group():
    """start_new_session is what makes one killpg reach the whole tree."""
    out = _converter()._run_group_killable(
        ["sh", "-c", "echo $$; ps -o pgid= -p $$"], timeout=10
    )
    pid_line, pgid_line = out.stdout.split()[:2]
    # A new session leader's pgid equals its own pid.
    assert pid_line == pgid_line


def test_normal_completion_returns_output_and_returncode():
    out = _converter()._run_group_killable(["sh", "-c", "echo hi; exit 3"], timeout=10)
    assert out.returncode == 3
    assert out.stdout.strip() == "hi"


def test_nonzero_exit_is_not_treated_as_timeout():
    """A failing conversion must surface as a returncode, not an exception."""
    out = _converter()._run_group_killable(["sh", "-c", "exit 1"], timeout=10)
    assert out.returncode == 1


def test_timeout_is_reraised_so_callers_still_see_it():
    """The caller's existing TimeoutExpired handler must keep working."""
    with pytest.raises(subprocess.TimeoutExpired):
        _converter()._run_group_killable(["sleep", "300"], timeout=1)


def test_kill_group_falls_back_to_the_child_when_killpg_fails(monkeypatch):
    """Losing the right to signal the group must not leave the child running."""
    killed = []

    class FakeProc:
        pid = 4242

        def kill(self):
            killed.append(True)

    def boom(*_a, **_k):
        raise PermissionError("not allowed")

    monkeypatch.setattr(os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(os, "killpg", boom)
    _converter()._kill_group(FakeProc(), 9)
    assert killed, "fallback proc.kill() was not called"


def test_kill_group_tolerates_an_already_dead_process(monkeypatch):
    """A race where the tree exits first must not raise."""

    class FakeProc:
        pid = 4243

        def kill(self):
            raise OSError("gone")

    monkeypatch.setattr(os, "getpgid", lambda _pid: 4243)
    monkeypatch.setattr(
        os, "killpg", lambda *_a: (_ for _ in ()).throw(ProcessLookupError())
    )
    _converter()._kill_group(FakeProc(), 9)  # must not raise


def test_sigkill_is_used_not_only_sigterm():
    """The observed processes ignored SIGTERM — the KILL must be reachable."""
    import inspect
    import signal

    src = inspect.getsource(TrimbleNativeConverter._run_group_killable)
    assert "SIGTERM" in src and "SIGKILL" in src
    assert signal.SIGKILL  # platform sanity


def test_never_blocks_forever_on_a_survivor_holding_the_pipes(monkeypatch):
    """A process we've given up on must not hold the worker thread hostage.

    Found empirically: with the group-kill removed, this method hung for the
    orphan's full lifetime rather than failing, because the survivor inherits
    stdout/stderr and communicate() waits on the PIPES, not the process. The
    bounded final read is what makes the timeout path terminate regardless.
    """
    conv = _converter()
    monkeypatch.setattr(conv, "_kill_group", lambda *_a, **_k: None)  # nothing dies

    started = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        # A grandchild holding the pipes open well past every grace period.
        conv._run_group_killable(["sh", "-c", "sleep 120 & wait"], timeout=1)
    elapsed = time.time() - started

    budget = 1 + (conv._KILL_GRACE_S * 2) + 5
    assert elapsed < budget, f"blocked {elapsed:.1f}s — should bail within {budget}s"
