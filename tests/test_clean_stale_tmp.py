"""`clean_stale_tmp` — the staging-dir sweep that runs before every backfill.

It had no direct coverage. What surfaced that was two `TestGapBackfill`
tests failing with `FileNotFoundError` on a developer machine whose
`tmp_dir` did not exist — the same thing that happens on rek-d01 after a
reboot, because production's `tmp_dir` is `/tmp/gps_receivers` and `/tmp`
does not survive one.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from receivers.scheduling.backfill import clean_stale_tmp


def _with_tmp_root(root: Path):
    """Point `clean_stale_tmp` at `root` instead of the real config."""
    return patch(
        "receivers.config.receivers_config.ReceiversConfig.get_tmp_dir",
        return_value=str(root),
    )


def _stage(root: Path, station: str, session: str, name: str, age_hours: float):
    d = root / station / session
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"partial")
    when = time.time() - age_hours * 3600
    import os

    os.utime(f, (when, when))
    return f


class TestMissingTmpRoot:
    """Nothing staged means nothing stale — (0, []), never an exception."""

    def test_absent_directory_is_not_an_error(self, tmp_path):
        missing = tmp_path / "never-created"
        with _with_tmp_root(missing):
            assert clean_stale_tmp("15s_24hr") == (0, [])

    def test_absent_directory_does_not_raise(self, tmp_path):
        """The regression: this raised FileNotFoundError out of a cleanup
        helper and took the whole backfill tick down with it."""
        missing = tmp_path / "gone"
        with _with_tmp_root(missing):
            clean_stale_tmp("15s_24hr")  # must not raise

    def test_a_file_where_the_dir_should_be_is_also_survivable(self, tmp_path):
        not_a_dir = tmp_path / "tmp_root"
        not_a_dir.write_text("not a directory")
        with _with_tmp_root(not_a_dir):
            assert clean_stale_tmp("15s_24hr") == (0, [])


class TestSweep:
    """The behaviour the guard must not have changed."""

    def test_empty_root_deletes_nothing(self, tmp_path):
        (tmp_path / "root").mkdir()
        with _with_tmp_root(tmp_path / "root"):
            assert clean_stale_tmp("15s_24hr") == (0, [])

    def test_stale_file_is_deleted_and_station_reported(self, tmp_path):
        root = tmp_path / "root"
        f = _stage(root, "RFEL", "15s_24hr", "part.sbf", age_hours=99)
        with _with_tmp_root(root):
            deleted, affected = clean_stale_tmp("15s_24hr")
        assert deleted == 1
        assert affected == ["RFEL"]
        assert not f.exists()

    def test_fresh_file_is_kept(self, tmp_path):
        """A resumable download must not be swept out from under itself."""
        root = tmp_path / "root"
        f = _stage(root, "RFEL", "15s_24hr", "part.sbf", age_hours=0.1)
        with _with_tmp_root(root):
            deleted, affected = clean_stale_tmp("15s_24hr")
        assert deleted == 0
        assert affected == []
        assert f.exists()

    def test_other_sessions_are_untouched(self, tmp_path):
        root = tmp_path / "root"
        other = _stage(root, "RFEL", "1Hz_1hr", "part.sbf", age_hours=99)
        with _with_tmp_root(root):
            deleted, _ = clean_stale_tmp("15s_24hr")
        assert deleted == 0
        assert other.exists()
