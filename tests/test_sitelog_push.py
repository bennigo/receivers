"""A committed site log must be pushed, and a failed push must not be fatal.

Committing without pushing leaves gps-sitelogs behind while the log itself is
already live on M3G — the publish reads the LOCAL file, so nothing downstream
ever notices the repo is stale. 67 unpushed commits had accumulated by
2026-09-01, some weeks old, purely from this gap.

The push is therefore automatic, but strictly best-effort: the commit is
already durable locally, and the realistic failures (offline, or origin moved
ahead so the push is non-fast-forward) need an operator rather than a retry.
"""

from __future__ import annotations

import subprocess

import pytest

from receivers.dissemination.sitelogs import push_site_logs


class _Res:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class TestItPushes:
    def test_a_successful_push_reports_ok(self, monkeypatch, tmp_path):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"] = cmd
            return _Res(0, err="   abc123..def456  master -> master")

        monkeypatch.setattr(subprocess, "run", run)
        ok, detail = push_site_logs(tmp_path)
        assert ok
        assert "master -> master" in detail
        assert seen["cmd"][:3] == ["git", "-C", str(tmp_path)]
        assert seen["cmd"][3:] == ["push", "origin", "HEAD"]

    def test_it_pushes_the_current_branch_not_a_hardcoded_name(
        self, monkeypatch, tmp_path
    ):
        """HEAD, so a repo on a non-master branch still pushes the right ref."""
        seen = {}
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: (seen.update(cmd=cmd), _Res(0))[1]
        )
        push_site_logs(tmp_path)
        assert "HEAD" in seen["cmd"] and "master" not in seen["cmd"]


class TestAFailedPushIsReportedNotRaised:
    """The commit is already safe; losing the run over a push would be worse."""

    def test_non_fast_forward_is_returned_as_a_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _Res(
                1, err="! [rejected]  master -> master (fetch first)"
            ),
        )
        ok, detail = push_site_logs(tmp_path)
        assert ok is False
        assert "rejected" in detail

    def test_it_does_not_pull_or_rebase_on_rejection(self, monkeypatch, tmp_path):
        """Rebasing to force our push through would reorder other people's commits."""
        cmds = []

        def run(cmd, **kw):
            cmds.append(cmd)
            return _Res(1, err="! [rejected]")

        monkeypatch.setattr(subprocess, "run", run)
        push_site_logs(tmp_path)
        # Inspect the git SUBCOMMAND + flags only. The repo path is a pytest
        # tmp_path named after this test, so it contains the word "pull" and
        # would false-positive a naive substring scan of the whole command.
        verbs = [c[3:] for c in cmds]
        flat = " ".join(" ".join(v) for v in verbs)
        for forbidden in ("pull", "rebase", "reset", "--force", "-f"):
            assert forbidden not in flat, f"push path ran {forbidden!r}: {verbs}"
        assert len(cmds) == 1, f"expected exactly one git call, got {verbs}"

    @pytest.mark.parametrize(
        "exc", [OSError("no git"), subprocess.TimeoutExpired("git", 180)]
    )
    def test_a_subprocess_failure_never_escapes(self, monkeypatch, tmp_path, exc):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(subprocess, "run", boom)
        ok, detail = push_site_logs(tmp_path)
        assert ok is False and detail

    def test_a_silent_failure_still_yields_a_message(self, monkeypatch, tmp_path):
        """An operator must never see an empty reason."""
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Res(128))
        ok, detail = push_site_logs(tmp_path)
        assert ok is False and detail.strip()
