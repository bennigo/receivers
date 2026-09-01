"""`run-after.sh` must never wait on itself.

The hand-rolled idiom this replaces deadlocks permanently:

    bash -c 'while pgrep -f "receivers rinex OFEL --from-archive" >/dev/null; \\
             do sleep 300; done; receivers rinex OFEL --fix-headers … --push'

`pgrep -f` matches full command lines, so the watcher matches its own `bash -c`
text, the loop never exits, and the queued job never runs. Two of these were
found spinning for 4 days 16 hours on 2026-09-01 (OFEL on the laptop, DYNC on
rek-d01), their fix-headers pushes never having fired.

Every test here is TIMEBOXED: a regression is a hang, and a hang that fails the
suite is worth far more than one that quietly blocks a production job.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-after.sh"
TIMEOUT = 25


def _run(args, timeout=TIMEOUT):
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, timeout=timeout
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="run-after.sh not present")
class TestItNeverWaitsOnItself:
    def test_a_pattern_matching_only_this_script_runs_immediately(self):
        """The exact shape of the bug: the pattern appears in our own argv."""
        started = time.monotonic()
        r = _run(
            ["--pattern", "run-after.sh --pattern", "--poll", "1", "--", "echo", "ok"]
        )
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout
        assert (
            time.monotonic() - started < 10
        ), "run-after.sh waited on itself — this is the 4-day deadlock"

    def test_it_reports_that_there_was_nothing_to_wait_for(self):
        r = _run(["--pattern", "run-after.sh --pattern", "--poll", "1", "--", "true"])
        assert "nothing to wait for" in r.stderr, r.stderr

    def test_a_pattern_matching_nothing_at_all_runs_immediately(self):
        r = _run(["--pattern", "definitely-no-such-process-xyzzy", "--", "echo", "ok"])
        assert r.returncode == 0 and "ok" in r.stdout


@pytest.mark.skipif(not SCRIPT.exists(), reason="run-after.sh not present")
class TestItStillActuallyWaits:
    """A self-exclusion bug that made it never wait would be just as wrong."""

    def test_it_waits_for_a_real_process_then_runs(self):
        proc = subprocess.Popen(["sleep", "4"])
        try:
            started = time.monotonic()
            r = _run(["--pid", str(proc.pid), "--poll", "1", "--", "echo", "done"])
            waited = time.monotonic() - started
            assert r.returncode == 0 and "done" in r.stdout, r.stderr
            assert waited >= 3, f"returned after {waited:.1f}s — it did not wait"
        finally:
            proc.wait(timeout=10)

    def test_it_propagates_the_commands_exit_status(self):
        r = _run(["--pid", "999999999", "--", "sh", "-c", "exit 7"])
        assert r.returncode == 7, "the queued command's status must survive"


@pytest.mark.skipif(not SCRIPT.exists(), reason="run-after.sh not present")
class TestUsageErrors:
    def test_a_missing_command_is_an_error_not_a_silent_success(self):
        r = _run(["--pattern", "whatever"])
        assert r.returncode == 2 and "no command" in r.stderr

    def test_an_unknown_option_is_rejected(self):
        r = _run(["--nope", "x", "--", "true"])
        assert r.returncode == 2


@pytest.mark.skipif(not SCRIPT.exists(), reason="run-after.sh not present")
class TestAZombieCountsAsExited:
    """`kill -0` is TRUE for a zombie, so a naive liveness test hangs forever.

    A process that has exited but not been reaped still owns its PID. Waiting on
    it never finishes — and this is the normal case when the target's parent is
    itself busy (a script that launches a job and then blocks). Caught exactly
    that way: this suite's own `sleep` was left unreaped while run-after.sh
    waited on it, and the wait never returned.
    """

    def test_it_does_not_wait_forever_on_an_unreaped_child(self):
        # Deliberately NOT reaped until after the assertion — the child is a
        # zombie for the whole of the run-after.sh call.
        proc = subprocess.Popen(["sleep", "0.2"])
        time.sleep(1.5)  # exited, but still unreaped → zombie
        try:
            started = time.monotonic()
            r = _run(["--pid", str(proc.pid), "--poll", "1", "--", "echo", "ok"])
            waited = time.monotonic() - started
            assert r.returncode == 0 and "ok" in r.stdout, r.stderr
            assert waited < 8, (
                f"waited {waited:.1f}s on a zombie — kill -0 succeeds for one, so "
                "liveness must check the process state, not just PID existence"
            )
        finally:
            proc.wait(timeout=10)
