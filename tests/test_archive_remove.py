"""Tests for guarded archive deletion (receivers.archive.remove).

The safety spine: strict path validation, argv-boundary path passing (never
interpolated), server-side empty guard, dry-run default. SSH is mocked so no
real deletion happens.
"""

import subprocess

import pytest

from receivers.archive.remove import (
    remove_archive_files,
    validate_archive_relpath,
)

GOOD = "2023/aug/RHOF/15s_24hr/rinex/RHOF2400.23D.Z"


class TestValidation:
    @pytest.mark.parametrize(
        "p",
        [
            "2023/aug/RHOF/15s_24hr/rinex/RHOF2400.23D.Z",
            "2026/feb/ELDC/1Hz_1hr/raw/ELDC202602101400b.sbf.gz",
            "2023/aug/RHOF/15s_24hr/rinex_org/RHOF2400.23D.Z",
        ],
    )
    def test_accepts_archive_paths(self, p):
        assert validate_archive_relpath(p)

    @pytest.mark.parametrize(
        "p",
        [
            "/etc/passwd",  # absolute
            "2023/aug/RHOF/15s_24hr/rinex/../../../etc/pw",  # traversal
            "2023/aug/RHOF/15s_24hr/rinex/*.Z",  # glob
            "2023/aug/RHOF/15s_24hr/rinex/f.Z; rm -rf /",  # injection
            "2023/aug/RHOF/15s_24hr/rinex/$(whoami)",  # command sub
            "2023/aug/RHOF/15s_24hr/foo/f.Z",  # bad category
            "2023/aug/RHOF/15s_24hr/rinex/",  # dir, no file
            "",  # empty
        ],
    )
    def test_rejects_unsafe(self, p):
        assert not validate_archive_relpath(p)


class TestRemove:
    def _mock(self, monkeypatch, stdout, rc=0):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["input"] = kw.get("input")
            return subprocess.CompletedProcess(cmd, rc, stdout, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return captured

    def test_paths_passed_as_argv_not_interpolated(self, monkeypatch):
        cap = self._mock(monkeypatch, f"WOULD_DELETE|{GOOD}|0\n")
        remove_archive_files(
            [GOOD], ssh_target="gpsops@rawdata", dest_root="~/gpsdata", execute=False
        )
        cmd = cap["cmd"]
        # The path is a standalone argv element AFTER '--', never embedded.
        assert "--" in cmd and GOOD in cmd
        assert cmd.index(GOOD) > cmd.index("--")
        # The remote script (stdin) must NOT contain the path.
        assert GOOD not in (cap["input"] or "")
        # Args after '--' are: root, max_size, execute, then paths.
        assert cmd[cmd.index("--") + 2] == "0"  # max_size default 0
        assert cmd[cmd.index("--") + 3] == "0"  # execute flag 0 on dry-run

    def test_invalid_paths_never_sent(self, monkeypatch):
        cap = self._mock(monkeypatch, "")
        res = remove_archive_files(
            ["/etc/passwd", "a; rm -rf /"], ssh_target="h", dest_root="~/gpsdata"
        )
        assert len(res.invalid) == 2
        assert "cmd" not in cap  # ssh never invoked when nothing is valid

    def test_parses_would_delete(self, monkeypatch):
        self._mock(monkeypatch, f"WOULD_DELETE|{GOOD}|0\n")
        res = remove_archive_files(
            [GOOD], ssh_target="h", dest_root="~/gpsdata", execute=False
        )
        assert res.would_delete == [(GOOD, 0)]
        assert res.deleted == []

    def test_parses_deleted_and_skip_toobig(self, monkeypatch):
        other = "2023/aug/RHOF/15s_24hr/rinex/RHOF2410.23D.Z"
        self._mock(monkeypatch, f"DELETED|{GOOD}|0\nSKIP_TOOBIG|{other}|12345\n")
        res = remove_archive_files(
            [GOOD, other], ssh_target="h", dest_root="~/gpsdata", execute=True
        )
        assert res.deleted == [(GOOD, 0)]
        assert res.skipped_toobig == [(other, 12345)]
        assert res.ok

    def test_fail_marks_not_ok(self, monkeypatch):
        self._mock(monkeypatch, f"FAIL|{GOOD}|0\n", rc=1)
        res = remove_archive_files(
            [GOOD], ssh_target="h", dest_root="~/gpsdata", execute=True
        )
        assert res.failed == [(GOOD, 0)]
        assert not res.ok

    def test_missing_is_not_failure(self, monkeypatch):
        self._mock(monkeypatch, f"MISSING|{GOOD}|0\n")
        res = remove_archive_files(
            [GOOD], ssh_target="h", dest_root="~/gpsdata", execute=True
        )
        assert res.missing == [GOOD]
        assert res.ok  # idempotent — already gone is fine
