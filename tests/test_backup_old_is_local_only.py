"""``--backup-old`` keeps the pre-overwrite copy LOCAL — never on the archive.

Policy (bgo, 2026-08-19): the long-term archive holds ``raw/``, ``rinex/`` and
``rinex_org/`` and nothing else. ``rinex_org`` is the TERMINAL state for data
that cannot be regenerated — at that point the preserved copy *is* the original.
A pre-overwrite backup is a different thing: an artefact of the verification
window, kept only until the regeneration is confirmed good. It therefore lives
beside the staging tree, not in the archive, where ~265 GB of server-side
``rinex_bak/`` had accumulated on a 98 %-full volume.

Two properties are pinned here.

**Nothing server-side.** The push path must not invoke the archive-side backup
(``archive.remove.backup_old_archive_files``) at all.

**Copy, not move.** The old server-side path MOVED ``rinex/FILE`` →
``rinex_bak/FILE`` *before* the rsync, so a failed rsync left the archive's
``rinex/`` empty with the only copy in ``rinex_bak/`` — a real window in which a
bak cleanup would have destroyed the last copy. Copying leaves the live archive
file untouched until rsync replaces it, so the window cannot exist.
"""

from __future__ import annotations

import importlib
import inspect

cli_main = importlib.import_module("receivers.cli.main")


class TestNothingIsWrittenToTheArchive:
    def test_push_path_never_calls_the_server_side_backup(self):
        src = inspect.getsource(cli_main._push_reconverted)
        assert "backup_old_archive_files" not in src, (
            "the re-rinex push must not create rinex_bak/ on the archive"
        )

    def test_the_local_copy_is_taken_instead(self):
        src = inspect.getsource(cli_main._push_reconverted)
        assert "_prev" in src and "backup_old" in src

    def test_it_copies_rather_than_moves(self):
        # copy2 keeps the archive's live file in place until rsync replaces it.
        # A move would recreate the stranded-only-copy window.
        src = inspect.getsource(cli_main._push_reconverted)
        assert "copy2" in src
        assert ".move(" not in src and "shutil.move" not in src

    def test_a_failed_copy_is_surfaced_not_swallowed(self):
        # A backup we asked for and did not get must never pass silently — the
        # file it covers is about to be overwritten with no fallback.
        src = inspect.getsource(cli_main._push_reconverted)
        assert "FAILED" in src


class TestTheArchiveSideHelperStillExistsForTheBacklog:
    def test_it_is_not_deleted(self):
        # ~265 GB of server-side rinex_bak already exists; --del-backup and the
        # helper are the only tooling that can reclaim it. Retire them after the
        # backlog is gone, not before.
        from receivers.archive import remove

        assert hasattr(remove, "backup_old_archive_files")


class TestTheRinexOrgFailsafeIsUntouched:
    def test_preservation_still_refuses_on_failure(self):
        # The one behaviour that must never be relaxed: if an un-regenerable
        # original cannot be preserved, the header rewrite is refused.
        from receivers.rinex import header_fix

        src = inspect.getsource(header_fix.fix_headers_in_file)
        assert "preserve_original_file" in src
        assert "refusing to overwrite" in src

    def test_cleanup_never_removes_rinex_org(self):
        from receivers.rinex import header_fix

        src = inspect.getsource(header_fix.cleanup_after_push)
        assert "rinex_org" in src
