"""convert_cache_dir accepts a candidate list, so one shared sync.yaml fits every host.

This replaces a rek-d01-only symlink from ~/.cache/gps_receivers/epos_convert to
/mnt/data/gpsops_scratch/. That symlink produced SILENT wrong answers rather than
errors — `du -sh` reported 0 for a 7.8 G tree and `find -maxdepth 1` matched
nothing, each looking like a valid result. An explicit candidate list has no
indirection left to fall through.
"""

from pathlib import Path


def _cache_path(value):
    from receivers.dissemination.config import DisseminationTarget

    holder = type("T", (), {"convert_cache_dir": value})()
    return DisseminationTarget.cache_path.fget(holder)


class TestCandidateResolution:
    def test_plain_string_still_works(self, tmp_path):
        assert _cache_path(str(tmp_path / "cache")) == tmp_path / "cache"

    def test_first_candidate_with_an_existing_parent_wins(self, tmp_path):
        good = tmp_path / "vol" / "epos_convert"
        good.parent.mkdir(parents=True)
        assert _cache_path([str(good), str(tmp_path / "fallback")]) == good

    def test_falls_through_when_the_first_parent_is_absent(self, tmp_path):
        # The laptop case: /mnt/data does not exist there, so ~/.cache wins.
        fallback = tmp_path / "home" / "cache"
        fallback.parent.mkdir(parents=True)
        got = _cache_path(["/nonexistent-volume/epos_convert", str(fallback)])
        assert got == fallback

    def test_parent_not_dir_itself(self, tmp_path):
        # The cache dir is created on first use. Testing the DIR would always
        # pick the last candidate on a clean host, silently moving the cache.
        target = tmp_path / "vol" / "epos_convert"
        target.parent.mkdir(parents=True)
        assert not target.exists()
        assert _cache_path([str(target), str(tmp_path / "other")]) == target

    def test_existing_dir_also_wins(self, tmp_path):
        target = tmp_path / "vol" / "epos_convert"
        target.mkdir(parents=True)
        assert _cache_path([str(target)]) == target

    def test_no_candidate_resolves_falls_back_to_the_first(self, tmp_path):
        # Never return None — the caller would crash far from the cause.
        got = _cache_path(["/nope/a", "/nope/b"])
        assert got == Path("/nope/a")

    def test_user_expansion(self):
        got = _cache_path(["~/some-cache-dir-xyz"])
        assert str(got).startswith(str(Path.home()))
