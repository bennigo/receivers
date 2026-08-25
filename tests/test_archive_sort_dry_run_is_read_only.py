"""A dry-run `archive-sort` must not publish to the corrections repo.

`_persist_remediation_records` used to `git add`/`commit`/`push` unconditionally
whenever a run produced plans or issues — no `--apply`, no `--yes`, no flag of
any kind. So a pure dry-run published to a SHARED repo. Observed 2026-08-25: a
dry-run against a synthetic staged archive committed and pushed test data to
`gps-tos-corrections` (reverted). It also made the verb impossible to rehearse.

The fix keeps writing the files — the `--apply-plan` command the run prints
points at the plan.tsv, so the review workflow depends on them existing — and
gates only the commit+push on `--yes`. Nothing is lost by waiting:
`_record_plan_applied` stages the whole batch dir, so applying commits plan,
report and marker together.
"""

import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.archive.sort import MovePlan, SkipInfo
from receivers.cli import archive_sync


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A stand-in corrections repo, with git calls recorded not executed."""
    r = tmp_path / "gps-tos-corrections"
    r.mkdir()

    class _Cfg:
        def get_tos_corrections_repo(self):
            return str(r)

    monkeypatch.setattr(
        "receivers.config.receivers_config.ReceiversConfig", lambda *a, **k: _Cfg()
    )
    return r


def _plan():
    return MovePlan(
        src_rel="2024/jan/VMOS/15s_24hr/raw/VMOS202401300000a.sbf",
        dst_rel="2024/jan/GRVV/15s_24hr/raw/GRVV202401300000a.sbf",
        fmt="sbf",
        decoded_start=datetime(2024, 1, 30),
        claimed=datetime(2024, 1, 30),
        reasons=("wrong-station",),
        true_station="GRVV",
        station_dist_m=0.0,
    )


def _git_calls(mock_run):
    """Just the git subcommands attempted, in order."""
    out = []
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else []
        if argv and argv[0] == "git":
            # ['git', '-C', <repo>, <subcmd>, ...]
            out.append(argv[3] if len(argv) > 3 else "")
    return out


class TestDryRunDoesNotPublish:
    def test_no_git_command_runs_without_commit(self, repo):
        with patch("subprocess.run") as mock_run:
            archive_sync._persist_remediation_records(
                [_plan()], [], gate_m=10.0, commit=False
            )
        assert _git_calls(mock_run) == [], "a dry-run must not touch git at all"

    def test_files_are_still_written(self, repo):
        """The review workflow needs them — the printed --apply-plan path."""
        with patch("subprocess.run"):
            archive_sync._persist_remediation_records(
                [_plan()], [], gate_m=10.0, commit=False
            )
        written = archive_sync._persist_remediation_records.last_written
        assert len(written) == 1
        assert (written[0] / "plan.tsv").exists()
        assert (written[0] / "report.tsv").exists()
        assert "VMOS202401300000a.sbf" in (written[0] / "plan.tsv").read_text()

    def test_commit_is_the_default_off(self, repo):
        """Omitting the argument must not publish — the original defect was
        that publishing needed no opt-in at all."""
        with patch("subprocess.run") as mock_run:
            archive_sync._persist_remediation_records([_plan()], [], gate_m=10.0)
        assert _git_calls(mock_run) == []

    def test_issues_only_run_also_stays_local(self, repo):
        """The repo already carries an issues-only commit from a dry run
        ('vmos (0 moves, 1 issues)'), so this path matters too."""
        skips = [SkipInfo("2024/jan/VMOS/15s_24hr/raw/x.sbf", "unknown-station", "")]
        with patch("subprocess.run") as mock_run:
            archive_sync._persist_remediation_records(
                [], skips, gate_m=10.0, commit=False
            )
        assert _git_calls(mock_run) == []
        assert archive_sync._persist_remediation_records.last_written

    def test_it_says_the_files_are_uncommitted(self, repo, capsys):
        with patch("subprocess.run"):
            archive_sync._persist_remediation_records(
                [_plan()], [], gate_m=10.0, commit=False
            )
        out = capsys.readouterr().out
        assert "NOT committed" in out
        assert "git -C" in out, "must show how to record it if wanted"


class TestApplyStillPublishes:
    def test_commit_true_adds_commits_and_pushes(self, repo):
        def fake(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("subprocess.run", side_effect=fake) as mock_run:
            archive_sync._persist_remediation_records(
                [_plan()], [], gate_m=10.0, commit=True
            )
        assert _git_calls(mock_run) == ["add", "commit", "push"]

    def test_a_failed_commit_does_not_push(self, repo):
        def fake(argv, **kw):
            rc = 1 if len(argv) > 3 and argv[3] == "commit" else 0
            return subprocess.CompletedProcess(argv, rc, "", "")

        with patch("subprocess.run", side_effect=fake) as mock_run:
            archive_sync._persist_remediation_records(
                [_plan()], [], gate_m=10.0, commit=True
            )
        assert "push" not in _git_calls(mock_run)


class TestNothingToRecord:
    def test_a_clean_run_writes_nothing(self, repo):
        skips = [SkipInfo("2024/jan/VMOS/15s_24hr/raw/x.sbf", "verified-correct", "")]
        with patch("subprocess.run") as mock_run:
            archive_sync._persist_remediation_records(
                [], skips, gate_m=10.0, commit=True
            )
        assert _git_calls(mock_run) == []
        assert archive_sync._persist_remediation_records.last_written == []
