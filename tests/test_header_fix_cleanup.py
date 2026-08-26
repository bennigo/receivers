"""Tests for the post-push staging cleanup (header_fix.cleanup_after_push).

Cleanup deletes staged rinex/ obs (on the archive after push) and TOS-confirmed
rinex_archive/ backups, but NEVER rinex_org/ preservations. The TOS re-read is
injected (confirm_fn) so these run offline.
"""

from datetime import datetime

from receivers.rinex.header_fix import cleanup_after_push


def _layout(root, station="RHOF"):
    """Build a staging mirror with rinex/, rinex_archive/, rinex_org/ + files."""
    base = root / "2026" / "jun" / station / "15s_24hr"
    rnx = base / "rinex" / f"{station}1720.26D.Z"
    bak = base / "rinex_archive" / "fix-headers_20260703" / f"{station}1720.26D.Z"
    org = base / "rinex_org" / f"{station}1720.26D.Z"
    for f in (rnx, bak, org):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return rnx, bak, org


def _detail(rnx, bak=None, org=None, *, fixed=True, station="RHOF"):
    return {
        "file": str(rnx),
        "source": f"/mnt_data/rawgpsdata/2026/jun/{station}/15s_24hr/rinex/{rnx.name}",
        "station": station,
        "observation_date": datetime(2026, 6, 21),
        "fixed": fixed,
        "archived": str(bak) if bak else None,
        "preserved_org": str(org) if org else None,
    }


def test_staged_removed_backup_kept_when_unconfirmed(tmp_path):
    rnx, bak, _ = _layout(tmp_path)
    stats = cleanup_after_push(
        [_detail(rnx, bak)], work_dir=tmp_path, confirm_fn=lambda *a: False
    )
    assert not rnx.exists()  # staged obs removed
    assert bak.exists()  # backup kept (not TOS-confirmed)
    assert stats["staged_removed"] == 1
    assert stats["backups_kept"] == 1
    assert stats["backups_removed"] == 0


def test_backup_removed_when_tos_confirmed(tmp_path):
    rnx, bak, _ = _layout(tmp_path)
    stats = cleanup_after_push(
        [_detail(rnx, bak)], work_dir=tmp_path, confirm_fn=lambda *a: True
    )
    assert not rnx.exists()
    assert not bak.exists()  # TOS-confirmed → backup deleted
    assert stats["backups_removed"] == 1


def test_rinex_org_never_deleted(tmp_path):
    rnx, bak, org = _layout(tmp_path)
    stats = cleanup_after_push(
        [_detail(rnx, bak, org)], work_dir=tmp_path, confirm_fn=lambda *a: True
    )
    assert org.exists()  # preservation survives even when confirmed
    assert stats["org_kept"] == 1


def test_unfixed_detail_ignored(tmp_path):
    rnx, bak, _ = _layout(tmp_path)
    stats = cleanup_after_push(
        [_detail(rnx, bak, fixed=False)], work_dir=tmp_path, confirm_fn=lambda *a: True
    )
    assert rnx.exists()  # not fixed → left alone
    assert bak.exists()
    assert stats["staged_removed"] == 0


def test_staged_outside_workdir_not_touched(tmp_path):
    # A staged path outside work_dir (defensive) must never be unlinked.
    rnx, bak, _ = _layout(tmp_path)
    other = tmp_path.parent
    stats = cleanup_after_push(
        [_detail(rnx, bak)], work_dir=other / "elsewhere", confirm_fn=lambda *a: True
    )
    assert rnx.exists()  # not under the given work_dir → untouched
    assert stats["staged_removed"] == 0
