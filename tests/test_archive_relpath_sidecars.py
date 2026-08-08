"""archive-rm must reach the pipeline's own sidecar dirs — and nothing else.

The layout regex allowed only YYYY/mon/STA/session/category/FILE, so the dated
directories the pipeline itself creates beside the products were unreachable:
`superseded_rt_<date>` (RT stream files replaced by a raw-derived conversion,
206 dirs / 1,359 files fleet-wide), `rinex_bak/` (--backup-old) and
`fix-headers_<date>`. Cleaning up its own leftovers therefore meant bypassing
the gateway and hand-running rm on the server — exactly what this verb exists
to prevent.

Widening a delete path is the kind of change that must not overshoot, so the
refusals matter more than the acceptances here.
"""

from __future__ import annotations

from receivers.archive.remove import validate_archive_relpath as valid

BASE = "2024/mar/HRNC/15s_24hr"


class TestSidecarsAreReachable:
    def test_superseded_rt(self):
        assert valid(f"{BASE}/rinex/superseded_rt_20260705/HRNC0640.24D.Z")

    def test_rinex_bak(self):
        assert valid(f"{BASE}/rinex/rinex_bak/HRNC0640.24D.Z")

    def test_fix_headers_under_rinex_archive(self):
        assert valid(f"{BASE}/rinex_archive/fix-headers_20260803/HRNC0640.24D.Z")

    def test_the_plain_layout_still_works(self):
        assert valid(f"{BASE}/rinex/HRNC0640.24D.Z")
        assert valid(f"{BASE}/raw/HRNC202403040000a.sbf.gz")


class TestNothingElseIsReachable:
    def test_an_arbitrary_subdirectory_is_refused(self):
        assert not valid(f"{BASE}/rinex/anything/HRNC0640.24D.Z")

    def test_a_near_miss_name_is_refused(self):
        """Named exactly — not a prefix match."""
        assert not valid(f"{BASE}/rinex/superseded_rt_evil/HRNC0640.24D.Z")
        assert not valid(f"{BASE}/rinex/superseded_rt_2026/HRNC0640.24D.Z")

    def test_two_sidecar_levels_are_refused(self):
        assert not valid(f"{BASE}/rinex/rinex_bak/deeper/HRNC0640.24D.Z")

    def test_traversal_is_still_refused(self):
        assert not valid(f"{BASE}/rinex/../../../../etc/passwd")
        assert not valid(f"{BASE}/rinex/superseded_rt_20260705/../../../x")

    def test_absolute_paths_are_refused(self):
        assert not valid("/mnt/rawgpsdata/2024/mar/HRNC/15s_24hr/rinex/x.Z")

    def test_shell_metacharacters_are_refused(self):
        assert not valid(f"{BASE}/rinex/superseded_rt_20260705/*.Z")
        assert not valid(f"{BASE}/rinex/superseded_rt_20260705/a;rm -rf /")

    def test_a_bare_sidecar_directory_is_not_a_file(self):
        assert not valid(f"{BASE}/rinex/superseded_rt_20260705/")
