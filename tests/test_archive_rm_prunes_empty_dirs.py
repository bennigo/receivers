"""archive-rm removes the directory it just emptied.

Deleting files left the shells behind: clearing ELDC's rinex_bak on 2026-08-12
removed 4,425 files and left 76 empty `rinex_bak/` dirs, which then had to be
swept with a hand-run rmdir loop on the gateway — exactly the manual rm this
verb exists to prevent.

The safety argument is `rmdir` itself: it removes ONLY an empty directory and
refuses everything else, so even a wrong candidate list cannot destroy data.
Candidates are additionally limited to the parents of files this call actually
deleted, and parents are never climbed — an empty `15s_24hr/` or station dir is
part of the layout other tooling globs over.
"""

from unittest.mock import patch

from receivers.archive.remove import _REMOTE_SCRIPT, remove_archive_files

REL = "2020/apr/ELDC/15s_24hr/rinex_bak/ELDC0920.20D.Z"
DIR = "2020/apr/ELDC/15s_24hr/rinex_bak"


def _proc(stdout="", rc=0, stderr=""):
    class P:
        pass

    p = P()
    p.stdout, p.returncode, p.stderr = stdout, rc, stderr
    return p


def _argv(run):
    return run.call_args[0][0]


# argv: ssh -o BatchMode=yes host bash -s -- root maxsize execute prune <paths...>
#         0  1        2       3    4   5  6   7      8       9      10
_I_EXECUTE, _I_PRUNE = 9, 10


def test_pruning_is_on_by_default():
    """The whole point: no flag needed to avoid leaving empty shells."""
    with patch("subprocess.run", return_value=_proc(f"DELETED|{REL}|100")) as run:
        remove_archive_files(
            [REL], ssh_target="u@h", dest_root="~/gpsdata", execute=True
        )
    argv = _argv(run)
    # Assert the PRUNE slot specifically — index 9 is `execute`, which is also
    # "1" here, so checking that slot would pass for the wrong reason.
    assert argv[_I_EXECUTE] == "1"
    assert argv[_I_PRUNE] == "1", "prune flag should default to enabled"


def test_prune_can_be_disabled():
    with patch("subprocess.run", return_value=_proc(f"DELETED|{REL}|100")) as run:
        remove_archive_files(
            [REL],
            ssh_target="u@h",
            dest_root="~/gpsdata",
            execute=True,
            prune_empty_dirs=False,
        )
    argv = _argv(run)
    assert argv[_I_PRUNE] == "0"
    assert argv[_I_EXECUTE] == "1", "disabling prune must not disable the delete"


def test_removed_dir_is_reported():
    out = f"DELETED|{REL}|100\nDIR_REMOVED|{DIR}|0"
    with patch("subprocess.run", return_value=_proc(out)):
        res = remove_archive_files(
            [REL], ssh_target="u@h", dest_root="~/gpsdata", execute=True
        )
    assert res.deleted == [(REL, 100)]
    assert res.dirs_removed == [DIR]
    assert res.ok


def test_dry_run_reports_would_remove_without_deleting():
    out = f"WOULD_DELETE|{REL}|100\nWOULD_REMOVE_DIR|{DIR}|0"
    with patch("subprocess.run", return_value=_proc(out)):
        res = remove_archive_files([REL], ssh_target="u@h", dest_root="~/gpsdata")
    assert res.dirs_would_remove == [DIR]
    assert res.dirs_removed == []
    assert res.deleted == []


def test_a_dir_that_keeps_files_is_not_removed():
    """If other files remain, the remote emits no DIR_REMOVED — nothing to record."""
    with patch("subprocess.run", return_value=_proc(f"DELETED|{REL}|100")):
        res = remove_archive_files(
            [REL], ssh_target="u@h", dest_root="~/gpsdata", execute=True
        )
    assert res.dirs_removed == []


def test_remote_script_uses_rmdir_never_rm_r():
    """rmdir's refusal to touch a non-empty dir IS the safety guarantee."""
    assert "rmdir " in _REMOTE_SCRIPT
    assert "rm -r" not in _REMOTE_SCRIPT
    assert "rm -rf" not in _REMOTE_SCRIPT


def test_remote_script_does_not_climb_to_parents():
    """Only the emptied directory — never its parents.

    Climbing would mean reassigning the candidate to its own parent and looping;
    the script must never do that, or clearing one category dir could unravel
    the session/station/month layout above it.
    """
    assert 'd=$(dirname "$d")' not in _REMOTE_SCRIPT
    assert 'd="$(dirname "$d")"' not in _REMOTE_SCRIPT
    assert 'while [ "$d" != "$root" ]' not in _REMOTE_SCRIPT
    # dirname is used only to derive a candidate from a deleted FILE path.
    for line in _REMOTE_SCRIPT.splitlines():
        if "dirname" in line:
            assert "$rel" in line, f"dirname applied to a non-file path: {line!r}"


def test_prune_only_considers_dirs_this_call_touched():
    """Candidates come from deleted files, not a directory walk."""
    assert "find" not in _REMOTE_SCRIPT
    assert 'touched="$touched' in _REMOTE_SCRIPT


def test_invalid_paths_still_never_reach_the_remote():
    """The pre-existing guard must survive the change."""
    for bad in ("/etc/passwd", "../../etc/passwd", "2020/apr/E;rm -rf ~/x.Z"):
        with patch("subprocess.run") as run:
            res = remove_archive_files(
                [bad], ssh_target="u@h", dest_root="~/gpsdata", execute=True
            )
        run.assert_not_called()
        assert res.invalid == [bad]
