"""Tests for receivers.cfg.global_sync — gps-config-data repo write + git commit.

Uses a real temp git repo (no network for push tests — push targets a second
local bare repo). The helper sets its own git identity, so a fresh `git init`
works without host config.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from receivers.cfg.global_sync import (
    CfgOperationError,
    git_commit_cfg,
    resolve_global_repo,
)


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q", "-b", "main")
    (path / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.5.0\n")
    _run(path, "-c", "user.name=t", "-c", "user.email=t@t", "add", "stations.cfg")
    _run(
        path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"
    )
    return path


# --- resolve_global_repo ----------------------------------------------------


def test_resolve_explicit_repo(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    assert resolve_global_repo(str(repo)) == repo


def test_resolve_env_var(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "cfgdata")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(repo))
    assert resolve_global_repo() == repo


def test_resolve_from_receivers_cfg(tmp_path, monkeypatch):
    """receivers.cfg [paths] gps_config_data_repo is used when env var is unset."""
    repo = _init_repo(tmp_path / "cfgdata")
    monkeypatch.delenv("GPS_CONFIG_DATA_REPO", raising=False)
    monkeypatch.setattr("receivers.cfg.global_sync._repo_from_cfg", lambda: str(repo))
    assert resolve_global_repo() == repo


def test_env_var_beats_receivers_cfg(tmp_path, monkeypatch):
    """Precedence: env var wins over the receivers.cfg value."""
    env_repo = _init_repo(tmp_path / "env")
    cfg_repo = _init_repo(tmp_path / "cfg")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(env_repo))
    monkeypatch.setattr(
        "receivers.cfg.global_sync._repo_from_cfg", lambda: str(cfg_repo)
    )
    assert resolve_global_repo() == env_repo


def test_resolve_missing_repo_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(tmp_path / "nope"))
    with pytest.raises(CfgOperationError, match="not found"):
        resolve_global_repo()


def test_resolve_not_a_git_worktree_errors(tmp_path, monkeypatch):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "stations.cfg").write_text("[X]\n")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(d))
    with pytest.raises(CfgOperationError, match="not a git"):
        resolve_global_repo()


def test_resolve_no_stations_cfg_errors(tmp_path, monkeypatch):
    d = tmp_path / "norepo"
    d.mkdir()
    _run(d, "init", "-q")
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(d))
    with pytest.raises(CfgOperationError, match="no stations.cfg"):
        resolve_global_repo()


# --- git_commit_cfg ---------------------------------------------------------


def test_commit_changes(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    res = git_commit_cfg(repo, ["stations.cfg"], "stations(ROTH): fw 5.7.0")
    assert res["committed"] is True
    assert res["commit"]
    # the change is the new HEAD
    msg = _run(repo, "log", "-1", "--format=%s")
    assert "fw 5.7.0" in msg


def test_no_op_when_clean(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    res = git_commit_cfg(repo, ["stations.cfg"], "no change")
    assert res["committed"] is False
    assert res.get("reason") == "no changes"


def test_dry_run_shows_diff_no_commit(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    head_before = _run(repo, "rev-parse", "HEAD")
    res = git_commit_cfg(repo, ["stations.cfg"], "msg", dry_run=True)
    assert res["committed"] is False
    assert res["dry_run"] is True
    assert "5.7.0" in res["diff"]
    assert _run(repo, "rev-parse", "HEAD") == head_before  # nothing committed


def test_detached_head_refused(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")
    sha = _run(repo, "rev-parse", "HEAD").strip()
    _run(repo, "checkout", "-q", sha)  # detach
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    with pytest.raises(CfgOperationError, match="detached"):
        git_commit_cfg(repo, ["stations.cfg"], "msg")


def test_push_to_local_bare(tmp_path):
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(bare))
    repo = _init_repo(tmp_path / "cfgdata")
    _run(repo, "remote", "add", "origin", str(bare))
    _run(repo, "push", "-q", "-u", "origin", "main")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    res = git_commit_cfg(repo, ["stations.cfg"], "stations(ROTH): fw", push=True)
    assert res["committed"] is True
    assert res["pushed"] is True
    # the bare remote now has the commit (ref the pushed branch explicitly —
    # the bare repo's default HEAD may point at an unborn 'master')
    remote_head = _run(bare, "log", "-1", "--format=%s", "main")
    assert "fw" in remote_head


def test_push_failure_keeps_commit(tmp_path):
    repo = _init_repo(tmp_path / "cfgdata")  # no remote configured
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    res = git_commit_cfg(repo, ["stations.cfg"], "msg", push=True)
    assert res["committed"] is True  # commit stands
    assert res["pushed"] is False
    assert res.get("push_error")  # failure surfaced, not raised


# --- divergence guardrail (assert_committable) ------------------------------


def _clone(bare: Path, dest: Path) -> Path:
    _run(dest.parent, "clone", "-q", str(bare), str(dest))
    _run(dest, "config", "user.name", "t")
    _run(dest, "config", "user.email", "t@t")
    return dest


def test_assert_committable_local_only_is_noop(tmp_path):
    """A clone with no upstream cannot diverge — guardrail allows (no raise)."""
    from receivers.cfg.global_sync import assert_committable

    repo = _init_repo(tmp_path / "cfgdata")  # no remote
    assert_committable(repo, push=False)  # must not raise


def test_assert_committable_requires_push_when_remote_tracked(tmp_path):
    """A remote-tracked clone must --push (else it diverges from origin)."""
    from receivers.cfg.global_sync import assert_committable

    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(bare))
    repo = _init_repo(tmp_path / "cfgdata")
    _run(repo, "remote", "add", "origin", str(bare))
    _run(repo, "push", "-q", "-u", "origin", "main")  # now remote-tracked + even
    with pytest.raises(CfgOperationError, match="requires --push"):
        assert_committable(repo, push=False)
    # with --push and even with origin → allowed
    assert_committable(repo, push=True)


def test_assert_committable_refuses_when_behind(tmp_path):
    """Behind origin → committing would diverge → refuse even with --push."""
    from receivers.cfg.global_sync import assert_committable

    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(bare))
    _run(bare, "symbolic-ref", "HEAD", "refs/heads/main")  # so clones get main
    a = _init_repo(tmp_path / "A")
    _run(a, "remote", "add", "origin", str(bare))
    _run(a, "push", "-q", "-u", "origin", "main")
    # A second clone advances origin → A is now behind.
    b = _clone(bare, tmp_path / "B")
    (b / "stations.cfg").write_text("[X]\nv = 2\n")
    _run(b, "commit", "-aqm", "advance origin")
    _run(b, "push", "-q", "origin", "main")
    with pytest.raises(CfgOperationError, match="not even with origin"):
        assert_committable(a, push=True)  # fetch sees A behind by 1


def test_git_commit_cfg_blocks_remote_without_push(tmp_path):
    """git_commit_cfg enforces the guardrail too (no dirty commit slips through)."""
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(bare))
    repo = _init_repo(tmp_path / "cfgdata")
    _run(repo, "remote", "add", "origin", str(bare))
    _run(repo, "push", "-q", "-u", "origin", "main")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")
    with pytest.raises(CfgOperationError, match="requires --push"):
        git_commit_cfg(repo, ["stations.cfg"], "msg", push=False)
    # nothing committed (HEAD unchanged) — clone stays clean for the sync
    assert _run(repo, "rev-list", "--count", "HEAD").strip() == "1"


# --- committer identity (GitLab pre-receive: email must be the pusher's) -----


def test_commit_uses_repo_configured_identity(tmp_path):
    """A repo with its own identity commits with THAT email, not a placeholder.

    GitLab's pre-receive hook rejects a push whose committer email isn't one of
    the pusher's verified emails. Overriding a real clone's identity with a
    placeholder (gps@vedur.is) breaks the --global --push flow — so when the repo
    has a configured identity, it must win.
    """
    repo = _init_repo(tmp_path / "cfgdata")
    _run(repo, "config", "user.name", "Benedikt")
    _run(repo, "config", "user.email", "bgo@vedur.is")
    (repo / "stations.cfg").write_text("[ROTH]\nreceiver_firmware_version = 5.7.0\n")

    res = git_commit_cfg(repo, ["stations.cfg"], "stations(ROTH): fw 5.7.0")
    assert res["committed"] is True
    committer_email = _run(repo, "log", "-1", "--format=%ce").strip()
    assert committer_email == "bgo@vedur.is"


def test_commit_falls_back_to_placeholder_identity(tmp_path, monkeypatch):
    """With NO identity anywhere (isolated git config), the placeholder is used.

    Isolates global/system git config so no machine identity leaks in — only
    then does git_commit_cfg fall back to the gps@vedur.is placeholder so a
    commit doesn't fail outright (the CI / fresh-container case).
    """
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))

    path = tmp_path / "noident"
    path.mkdir()
    _run(path, "init", "-q", "-b", "main")
    (path / "stations.cfg").write_text("[ROTH]\nx = 1\n")
    _run(path, "-c", "user.name=t", "-c", "user.email=t@t", "add", "stations.cfg")
    _run(
        path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"
    )

    (path / "stations.cfg").write_text("[ROTH]\nx = 2\n")
    res = git_commit_cfg(path, ["stations.cfg"], "msg")
    assert res["committed"] is True
    assert _run(path, "log", "-1", "--format=%ce").strip() == "gps@vedur.is"
