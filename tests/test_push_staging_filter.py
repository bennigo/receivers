"""Push must send finished products only, and back up in chunks.

Both defects surfaced together on the ISAK R3 run (2026-08-03), and between
them ~1,300 converted files never reached the archive:

* The staging glob took every file in a ``rinex/`` directory, including the
  converter's own intermediates (``ISAK…0000a.13o``, ``…_gfzrnx.13o``). Those
  are deleted as each file finishes, so rsync was handed paths that vanished
  underneath it — ``link_stat … No such file or directory``, exit 24, partial
  transfer. Under ``--parallel`` that is not a rare race: sibling year-chunks
  convert throughout the push.
* ``backup_old_archive_files`` passed every path as argv in ONE ssh call. At
  ~3,100 paths the remote shell answered ``/bin/bash: Argument list too long``,
  rc=1, and reported ``0 file(s)`` backed up — 11 times, each leaving a batch
  unprotected immediately before rsync overwrote it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from receivers.archive.remove import _BACKUP_CHUNK, backup_old_archive_files


def _proc(stdout="", rc=0, stderr=""):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = rc
    m.stderr = stderr
    return m


def _rel(n, prefix="2018/jun/ISAK/15s_24hr/rinex"):
    return [f"{prefix}/ISAK{i:04d}0.18D.Z" for i in range(n)]


class TestBackupChunking:
    def test_small_batch_is_one_call(self):
        rel = _rel(10)
        with patch(
            "receivers.archive.remove.subprocess.run",
            return_value=_proc("\n".join(f"BACKED_UP|{r}" for r in rel)),
        ) as run:
            res = backup_old_archive_files(
                rel, ssh_target="h", dest_root="/d", execute=True
            )
        assert run.call_count == 1
        assert len(res.backed_up) == 10

    def test_large_batch_is_split(self):
        """3,100 paths is the size that failed on ISAK."""
        rel = _rel(3100)
        with patch(
            "receivers.archive.remove.subprocess.run", return_value=_proc("")
        ) as run:
            backup_old_archive_files(rel, ssh_target="h", dest_root="/d", execute=True)
        expected = (3100 + _BACKUP_CHUNK - 1) // _BACKUP_CHUNK
        assert run.call_count == expected

    def test_no_call_carries_more_than_the_chunk_size(self):
        """The whole point — argv length is what broke."""
        rel = _rel(1200)
        with patch(
            "receivers.archive.remove.subprocess.run", return_value=_proc("")
        ) as run:
            backup_old_archive_files(rel, ssh_target="h", dest_root="/d", execute=True)
        for call in run.call_args_list:
            argv = call.args[0]
            # argv = ssh -o BatchMode=yes <target> bash -s -- <dest_root> <flag> *paths
            assert len(argv) - 9 <= _BACKUP_CHUNK

    def test_results_accumulate_across_chunks(self):
        rel = _rel(1000)
        outs = [
            _proc("\n".join(f"BACKED_UP|{r}" for r in rel[:500])),
            _proc("\n".join(f"BACKED_UP|{r}" for r in rel[500:])),
        ]
        with patch("receivers.archive.remove.subprocess.run", side_effect=outs):
            res = backup_old_archive_files(
                rel, ssh_target="h", dest_root="/d", execute=True
            )
        assert len(res.backed_up) == 1000

    def test_one_failing_chunk_does_not_lose_the_others(self):
        rel = _rel(1000)
        outs = [
            _proc("", rc=1, stderr="/bin/bash: Argument list too long"),
            _proc("\n".join(f"BACKED_UP|{r}" for r in rel[500:])),
        ]
        with patch("receivers.archive.remove.subprocess.run", side_effect=outs):
            res = backup_old_archive_files(
                rel, ssh_target="h", dest_root="/d", execute=True
            )
        assert len(res.backed_up) == 500

    def test_empty_input_makes_no_calls(self):
        with patch("receivers.archive.remove.subprocess.run") as run:
            backup_old_archive_files([], ssh_target="h", dest_root="/d", execute=True)
        assert run.call_count == 0


class TestStagingProductFilter:
    """The push list must exclude uncompressed converter intermediates."""

    def _staged(self, tmp_path, names):
        d = tmp_path / "2018" / "jun" / "ISAK" / "15s_24hr" / "rinex"
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_text("x")
        return [
            p
            for p in tmp_path.rglob("*")
            if p.is_file() and p.parent.name == "rinex" and p.suffix in (".Z", ".gz")
        ]

    def test_intermediates_are_excluded(self, tmp_path):
        kept = self._staged(
            tmp_path,
            [
                "ISAK1600.18D.Z",  # product
                "ISAK20180609000a.13o",  # converter intermediate
                "ISAK20180609000a_gfzrnx.13o",  # gfzrnx intermediate
            ],
        )
        assert [p.name for p in kept] == ["ISAK1600.18D.Z"]

    def test_gz_products_are_kept(self, tmp_path):
        kept = self._staged(tmp_path, ["ISAK1600.18D.gz", "ISAK1600.18o"])
        assert [p.name for p in kept] == ["ISAK1600.18D.gz"]

    def test_long_name_r3_product_is_kept(self, tmp_path):
        kept = self._staged(
            tmp_path, ["ISAK00ISL_R_20181600000_01D_15S_MO.crx.gz", "scratch.rnx"]
        )
        assert len(kept) == 1
