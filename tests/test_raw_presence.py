"""Tests for the fix-headers regenerability safety net (raw_presence + preserve).

A RINEX is safe to overwrite in place only if it is regenerable — a convertible
raw file exists. Raw absent, OR raw in an unrecognised format, must be treated
as irreplaceable and preserved to rinex_org/ before any rewrite.
"""

from datetime import datetime

from receivers.rinex.header_fix import preserve_original_file
from receivers.rinex.raw_presence import (
    check_regenerable,
    raw_format_recognised,
    strip_raw_compression,
)

DATE = datetime(2026, 6, 21)  # doy 172 → tag 20260621


def _make_rinex(root, station="RHOF"):
    rnx_dir = root / "2026" / "jun" / station / "15s_24hr" / "rinex"
    rnx_dir.mkdir(parents=True)
    f = rnx_dir / f"{station}1720.26D.Z"
    f.write_bytes(b"crinex payload")
    return f


def _put_raw(root, name, station="RHOF"):
    raw_dir = root / "2026" / "jun" / station / "15s_24hr" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw_dir / name
    p.write_bytes(b"raw payload")
    return p


class TestFormatRecognition:
    def test_known_extensions(self):
        for n in ["X.sbf.gz", "X.T02", "X.T00.gz", "X.m00.gz", "X.T02.Z"]:
            assert raw_format_recognised(n), n

    def test_unknown_extensions(self):
        for n in ["X.rnx", "X.unknownfmt", "X.txt", "X.26d.Z"]:
            assert not raw_format_recognised(n), n

    def test_strip_compression(self):
        assert strip_raw_compression("A.T02.gz") == "A.T02"
        assert strip_raw_compression("A.T02.Z") == "A.T02"
        assert strip_raw_compression("A.T02") == "A.T02"


class TestCheckRegenerable:
    def test_regenerable_when_convertible_raw_present(self, tmp_path):
        rnx = _make_rinex(tmp_path)
        _put_raw(tmp_path, "RHOF202606210000a.T02.gz")
        r = check_regenerable(rnx, DATE, station_id="RHOF")
        assert r.regenerable
        assert r.raw_file is not None

    def test_not_regenerable_when_raw_absent(self, tmp_path):
        rnx = _make_rinex(tmp_path)
        _put_raw(tmp_path, "RHOF202606200000a.T02.gz")  # wrong day (doy 171)
        r = check_regenerable(rnx, DATE, station_id="RHOF")
        assert not r.regenerable
        assert "absent" in r.reason

    def test_not_regenerable_when_format_unrecognised(self, tmp_path):
        # Raw file for the right day but a format we can't convert.
        rnx = _make_rinex(tmp_path)
        _put_raw(tmp_path, "RHOF202606210000a.weirdfmt")
        r = check_regenerable(rnx, DATE, station_id="RHOF")
        assert not r.regenerable
        assert "unrecognised" in r.reason

    def test_not_regenerable_when_no_raw_dir(self, tmp_path):
        rnx = _make_rinex(tmp_path)  # no raw/ sibling created
        r = check_regenerable(rnx, DATE, station_id="RHOF")
        assert not r.regenerable

    def test_hourly_matches_only_same_hour(self, tmp_path):
        # HOURLY session: a raw for a DIFFERENT hour must NOT count as
        # regenerable (day-only match would falsely accept it → data loss).
        rnx = _make_rinex(tmp_path)
        _put_raw(tmp_path, "RHOF202606210000a.sbf.gz")  # hour 00, not 14
        hour14 = datetime(2026, 6, 21, 14)
        r = check_regenerable(rnx, hour14, station_id="RHOF", session_type="1Hz_1hr")
        assert not r.regenerable  # hour 14's raw is absent

    def test_hourly_regenerable_when_same_hour_present(self, tmp_path):
        rnx = _make_rinex(tmp_path)
        _put_raw(tmp_path, "RHOF202606211400a.sbf.gz")  # hour 14
        hour14 = datetime(2026, 6, 21, 14)
        r = check_regenerable(rnx, hour14, station_id="RHOF", session_type="1Hz_1hr")
        assert r.regenerable


class TestPreserveOriginal:
    def test_copies_to_rinex_org_permanent(self, tmp_path):
        rnx = _make_rinex(tmp_path)
        org = preserve_original_file(rnx)
        assert org is not None
        assert org.parent.name == "rinex_org"
        assert org.read_bytes() == rnx.read_bytes()
        assert rnx.exists()  # COPY, not move — original stays

    def test_idempotent_keeps_first(self, tmp_path):
        rnx = _make_rinex(tmp_path)
        first = preserve_original_file(rnx)
        # Change the source, preserve again — must NOT clobber the original org.
        rnx.write_bytes(b"different later content")
        second = preserve_original_file(rnx)
        assert second == first
        assert first.read_bytes() == b"crinex payload"
