"""A session-less archive directory must be reported, not silently skipped.

A few station-months carry ``YYYY/mon/STA/{raw,rinex}/`` with no session
segment. Every walk in this codebase descends through a session, so such a
directory was invisible everywhere: absent from ``archive_catalog`` (0 rows
fleet-wide), skipped by both ``archive-sort`` station scans, and skipped by
this audit. ``archive-sort STA`` would therefore report a station clean no
matter what sat in there.

Measured 2026-08-26: 73 such directories (2019-2023; TKJS, TKJ2, TORK, GONH,
SEY9, JONC) holding 1,625 files / 1.6 GB. 56 are byte-identical duplicates of
their canonical twin — but 8 hold 82 files that exist NOWHERE else, and 8 more
hold 19 files that differ in content from the canonical file of the same name.
TKJS/TKJ2/TORK have no raw at all, so their RINEX cannot be regenerated.

Hence: report, never walk and never delete. Walking would enumerate the 56
duplicate dirs as findings; marking them junk would feed archive-rm the exact
files that must not be deleted blind.
"""

from pathlib import Path

from receivers.archive.audit import audit_station_session


def _mk(root: Path, rel: str, data: bytes = b"x" * 300) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


class TestSessionLessLayoutIsReported:
    def test_a_sessionless_rinex_dir_is_flagged(self, tmp_path):
        _mk(tmp_path, "2020/jun/TKJS/15s_24hr/rinex/TKJS1760.20D.Z")
        _mk(tmp_path, "2020/jun/TKJS/rinex/TKJS1760.20D.Z", b"y" * 300)

        rep = audit_station_session(tmp_path, "TKJS", "15s_24hr", check_identity=False)
        hits = [f for f in rep.findings if f.issue == "session-less-layout"]

        assert [h.rel_path for h in hits] == ["2020/jun/TKJS/rinex"]

    def test_a_sessionless_raw_dir_is_flagged_too(self, tmp_path):
        """SEY9 2022/jul is a raw one, and all 11 of its files are unique."""
        _mk(tmp_path, "2022/jul/SEY9/15s_24hr/rinex/SEY91960.22D.Z")
        _mk(tmp_path, "2022/jul/SEY9/raw/SEY9202207150000a.sbf.gz")

        rep = audit_station_session(tmp_path, "SEY9", "15s_24hr", check_identity=False)
        hits = [f for f in rep.findings if f.issue == "session-less-layout"]

        assert [h.rel_path for h in hits] == ["2022/jul/SEY9/raw"]

    def test_it_is_never_deletable_or_regenerable(self, tmp_path):
        """junk= feeds archive-rm directly. These are the files that must not
        be deleted blind — 82 of them exist nowhere else."""
        _mk(tmp_path, "2020/jun/TKJS/15s_24hr/rinex/TKJS1760.20D.Z")
        _mk(tmp_path, "2020/jun/TKJS/rinex/TKJS1760.20D.Z")

        rep = audit_station_session(tmp_path, "TKJS", "15s_24hr", check_identity=False)
        hits = [f for f in rep.findings if f.issue == "session-less-layout"]

        assert hits
        for h in hits:
            assert h.junk is False, "archive-rm must never be handed these"
            assert h.regen is False, "TKJS has no raw — nothing to regenerate from"

    def test_the_files_inside_are_not_walked(self, tmp_path):
        """Walking them would enumerate 56 dirs of byte-identical duplicates
        as findings. One finding per directory is the whole point."""
        _mk(tmp_path, "2020/jun/TKJS/15s_24hr/rinex/TKJS1760.20D.Z")
        for i in range(5):
            _mk(tmp_path, f"2020/jun/TKJS/rinex/TKJS17{i}0.20D.Z")

        rep = audit_station_session(tmp_path, "TKJS", "15s_24hr", check_identity=False)
        hits = [f for f in rep.findings if f.issue == "session-less-layout"]

        assert len(hits) == 1, "one finding for the directory, not one per file"
        assert "5 file(s)" in hits[0].detail

    def test_a_clean_station_reports_nothing(self, tmp_path):
        _mk(tmp_path, "2020/jun/TKJS/15s_24hr/rinex/TKJS1760.20D.Z")

        rep = audit_station_session(tmp_path, "TKJS", "15s_24hr", check_identity=False)

        assert [f for f in rep.findings if f.issue == "session-less-layout"] == []

    def test_the_canonical_tree_is_not_mistaken_for_one(self, tmp_path):
        """YYYY/mon/STA/<session>/rinex must never trip the check."""
        for sess in ("15s_24hr", "1Hz_1hr", "30s_1hr"):
            _mk(tmp_path, f"2023/may/GONH/{sess}/rinex/GONH1410.23D.Z")

        rep = audit_station_session(tmp_path, "GONH", "15s_24hr", check_identity=False)

        assert [f for f in rep.findings if f.issue == "session-less-layout"] == []
