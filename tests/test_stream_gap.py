"""Tests for stream gap-detection + download-fallback."""

from datetime import UTC, date, datetime

from receivers.streaming.gap import (
    GapFiller,
    GapPolicy,
    find_missing_hours,
    make_archive_slot_checker,
)

DAY = date(2026, 6, 11)  # doy 162


def _present(hours):
    """SlotChecker that reports a fixed set of hours as present."""
    hours = set(hours)
    return lambda _sid, dt: dt.hour in hours


class RecordingDownloader:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, station_id, start, end):
        self.calls.append((station_id, start, end))
        if self.fail:
            raise RuntimeError("receiver unreachable")


class TestFindMissingHours:
    def test_all_present_no_missing(self):
        assert find_missing_hours("GONH", DAY, _present(range(24))) == []

    def test_missing_listed(self):
        present = _present(set(range(24)) - {5, 6})
        missing = find_missing_hours("GONH", DAY, present)
        assert [d.hour for d in missing] == [5, 6]

    def test_up_to_excludes_future(self):
        present = _present({0, 1})
        up_to = datetime(2026, 6, 11, 3, tzinfo=UTC)
        missing = find_missing_hours("GONH", DAY, present, up_to=up_to)
        # only hours 0..3 considered; 0,1 present -> missing 2,3
        assert [d.hour for d in missing] == [2, 3]


class TestArchiveSlotChecker:
    def test_present_and_case_insensitive(self, tmp_path):
        rinex = tmp_path / "2026/jun/GONH/1Hz_1hr/rinex"
        rinex.mkdir(parents=True)
        (rinex / "GONH162a.26D.Z").write_bytes(b"x")  # hour 0
        (rinex / "GONH162B.26d.gz").write_bytes(b"x")  # hour 1, upper letter + .gz
        check = make_archive_slot_checker(tmp_path)
        assert check("GONH", datetime(2026, 6, 11, 0, tzinfo=UTC)) is True
        assert check("GONH", datetime(2026, 6, 11, 1, tzinfo=UTC)) is True
        assert check("GONH", datetime(2026, 6, 11, 2, tzinfo=UTC)) is False

    def test_missing_dir_is_absent(self, tmp_path):
        check = make_archive_slot_checker(tmp_path)
        assert check("GONH", datetime(2026, 6, 11, 0, tzinfo=UTC)) is False


class TestGapFiller:
    NOW = datetime(2026, 6, 11, 23, tzinfo=UTC)  # grace=2 -> consider up to hour 21

    def test_complete_no_download(self):
        filler = GapFiller(_present(range(22)))  # 0..21 present, 22/23 in grace
        dl = RecordingDownloader()
        res = filler.check_and_fill("GONH", DAY, downloader=dl, now=self.NOW)
        assert res.status == "complete"
        assert dl.calls == []

    def test_below_threshold_no_download(self):
        present = _present(set(range(22)) - {5})  # 1 missing, min_missing_to_fill=2
        res = GapFiller(present).check_and_fill(
            "GONH", DAY, downloader=RecordingDownloader(), now=self.NOW
        )
        assert res.status == "below_threshold"
        assert res.missing_hours and not res.attempted_download

    def test_fills_large_gap(self):
        present = _present(set(range(22)) - {5, 6, 7})
        dl = RecordingDownloader()
        res = GapFiller(present).check_and_fill(
            "GONH", DAY, downloader=dl, now=self.NOW
        )
        assert res.status == "filled"
        assert res.downloaded_span == (
            datetime(2026, 6, 11, 5, tzinfo=UTC),
            datetime(2026, 6, 11, 7, tzinfo=UTC),
        )
        assert len(dl.calls) == 1
        assert dl.calls[0][0] == "GONH"

    def test_download_failure_recorded(self):
        present = _present(set(range(22)) - {5, 6})
        res = GapFiller(present).check_and_fill(
            "GONH", DAY, downloader=RecordingDownloader(fail=True), now=self.NOW
        )
        assert res.status == "download_failed"
        assert "unreachable" in res.error

    def test_custom_policy_threshold(self):
        present = _present(set(range(22)) - {5})  # 1 missing
        dl = RecordingDownloader()
        res = GapFiller(
            present, policy=GapPolicy(min_missing_to_fill=1)
        ).check_and_fill("GONH", DAY, downloader=dl, now=self.NOW)
        assert res.status == "filled"
        assert len(dl.calls) == 1
