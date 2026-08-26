"""--backup-old / --del-backup for re-rinexing (move old RINEX to rinex_bak/).

Re-rinexing to RINEX3-short reuses the old RINEX2-short filename, so it would
overwrite. --backup-old moves the existing file to a deletable rinex_bak/ sibling
first; --del-backup removes those backups after verification.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime
from types import SimpleNamespace

m = importlib.import_module("receivers.cli.main")

_LOG = logging.getLogger("test")


def _mk_rinex(tmp_path, *files):
    rinex = tmp_path / "2015/apr/RHOF/15s_24hr/rinex"
    rinex.mkdir(parents=True)
    for f in files:
        (rinex / f).write_bytes(b"x")
    return rinex


def test_backup_moves_only_matching_date(tmp_path):
    rinex = _mk_rinex(tmp_path, "RHOF0910.15D.Z", "RHOF0920.15D.Z")  # DOY 091, 092
    n = m._backup_existing_rinex_for_date(
        rinex,
        datetime(2015, 4, 1),
        "RHOF",
        "15s_24hr",
        _LOG,  # DOY 091
    )
    assert n == 1
    assert sorted(p.name for p in rinex.iterdir()) == ["RHOF0920.15D.Z"]
    bak = rinex.parent / "rinex_bak"
    assert [p.name for p in bak.iterdir()] == ["RHOF0910.15D.Z"]


def test_backup_dry_run_moves_nothing(tmp_path):
    rinex = _mk_rinex(tmp_path, "RHOF0910.15D.Z")
    n = m._backup_existing_rinex_for_date(
        rinex, datetime(2015, 4, 1), "RHOF", "15s_24hr", _LOG, dry_run=True
    )
    assert n == 1  # counted
    assert (rinex / "RHOF0910.15D.Z").exists()  # but not moved
    assert not (rinex.parent / "rinex_bak").exists()


def test_backup_no_clobber_existing_backup(tmp_path):
    rinex = _mk_rinex(tmp_path, "RHOF0910.15D.Z")
    (rinex.parent / "rinex_bak").mkdir()
    (rinex.parent / "rinex_bak" / "RHOF0910.15D.Z").write_bytes(b"prev")
    m._backup_existing_rinex_for_date(
        rinex, datetime(2015, 4, 1), "RHOF", "15s_24hr", _LOG
    )
    names = sorted(p.name for p in (rinex.parent / "rinex_bak").iterdir())
    assert names == ["RHOF0910.15D.Z", "RHOF0910.15D_1.Z"]  # both kept, uniquified


def test_del_backup_removes_in_range(tmp_path, monkeypatch):
    rinex = _mk_rinex(tmp_path, "RHOF0910.15D.Z")
    m._backup_existing_rinex_for_date(
        rinex, datetime(2015, 4, 1), "RHOF", "15s_24hr", _LOG
    )
    bak = rinex.parent / "rinex_bak"
    assert bak.is_dir()
    # _cmd_del_backup does `from ..config.receivers_config import get_receivers_config`
    import receivers.config.receivers_config as rc

    monkeypatch.setattr(
        rc,
        "get_receivers_config",
        lambda: SimpleNamespace(get_data_prepath=lambda: str(tmp_path)),
    )
    args = SimpleNamespace(session="15s_24hr", all=False, dry_run=False)
    rc_code = m._cmd_del_backup(
        ["RHOF"], args, datetime(2015, 4, 1), datetime(2015, 4, 2), _LOG
    )
    assert rc_code == 0
    assert not bak.exists()  # emptied + removed


# archive-side backup (remote move) — SSH gateway mocked
def test_backup_old_archive_files_argv_and_parse(monkeypatch):
    import receivers.archive.remove as rm

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace

        rel = "2015/apr/RHOF/15s_24hr/rinex/RHOF0910.15D.Z"
        return SimpleNamespace(returncode=0, stdout=f"BACKED_UP|{rel}\n", stderr="")

    monkeypatch.setattr(rm.subprocess, "run", fake_run)
    rel = "2015/apr/RHOF/15s_24hr/rinex/RHOF0910.15D.Z"
    res = rm.backup_old_archive_files(
        [rel, "../evil", "abs/../x"],
        ssh_target="gpsops@rawdata",
        dest_root="~/gpsdata",
        execute=True,
    )
    assert res.backed_up == [rel]
    assert "../evil" not in res.backed_up and len(res.invalid) == 2  # rejected
    # paths passed as ARGV (after the '--'), never interpolated into a string
    assert rel in captured["cmd"]
    assert captured["cmd"][:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "gpsops@rawdata",
        "bash",
        "-s",
    ]
