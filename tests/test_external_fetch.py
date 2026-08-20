"""Unit tests for :mod:`receivers.external_fetch` — URL templating + config."""

from __future__ import annotations

from datetime import datetime

import pytest

from receivers.external_fetch import (
    build_external_urls,
    external_station_config,
    external_stations,
)


class TestExternalStationConfig:
    def test_none_without_template(self):
        assert external_station_config({"station_id": "X"}) is None
        assert external_station_config({"external_url_template": "  "}) is None

    def test_defaults(self):
        cfg = external_station_config({"external_url_template": "ftp://h/x/%j"})
        assert cfg["frequency"] == "1D"
        assert cfg["data_type"] == "rinex"
        assert cfg["username"] is None

    def test_full(self):
        cfg = external_station_config(
            {
                "external_url_template": "ftp://h/x/%j",
                "external_frequency": "1H",
                "external_username": "anon",
                "external_password": "p",
                "external_data_type": "raw",
            }
        )
        assert cfg["frequency"] == "1H"
        assert cfg["username"] == "anon"
        assert cfg["password"] == "p"
        assert cfg["data_type"] == "raw"


class TestBuildExternalUrls:
    def test_station_substitution_case(self):
        urls = build_external_urls(
            "myva",
            "ftp://h/{station}/{station_lower}/%Y",
            "1D",
            datetime(2026, 8, 19),
        )
        assert urls == ["ftp://h/MYVA/myva/2026"]

    def test_doy_and_year_expansion(self):
        # LMI-style template: %Y/%j/{station}%j0.%yO
        urls = build_external_urls(
            "MYVA",
            "ftp://ftp.lmi.is/.gnsmart_data/15s_data/%Y/%j/{station}%j0.%yO",
            "1D",
            datetime(2026, 8, 19),
        )
        # 2026-08-19 = DOY 231, 2-digit year 26
        assert urls == ["ftp://ftp.lmi.is/.gnsmart_data/15s_data/2026/231/MYVA2310.26O"]

    def test_range_multiple_days(self):
        urls = build_external_urls(
            "MYVA",
            "%Y%m%d/{station}.dat",
            "1D",
            datetime(2026, 8, 19),
            datetime(2026, 8, 21),
        )
        assert urls == [
            "20260819/MYVA.dat",
            "20260820/MYVA.dat",
            "20260821/MYVA.dat",
        ]


class TestExternalStations:
    def test_filters_and_sorts(self):
        configs = {
            "ZZZ": {"external_url_template": "ftp://h/x"},
            "AAA": {"external_url_template": "ftp://h/y"},
            "BBB": {},  # not external
        }
        assert external_stations(configs) == ["AAA", "ZZZ"]


class TestFetchCleanup:
    """Failed downloads must not leave 0-byte / partial junk files."""

    def test_failed_ftp_leaves_no_file(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from receivers.external_fetch import _fetch_one

        ftp = MagicMock()

        def boom(*a, **k):
            raise OSError("550 Can't open")

        ftp.retrbinary.side_effect = boom
        with patch("receivers.external_fetch.ftplib.FTP", return_value=ftp):
            with pytest.raises(OSError):
                _fetch_one(
                    "ftp://h/15s_data/2026/2432/232/MYVA2320.26e",
                    tmp_path,
                    username=None,
                    password=None,
                )
        assert list(tmp_path.iterdir()) == []  # no junk, no .part

    def test_successful_ftp_renames_atomically(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from receivers.external_fetch import _fetch_one

        ftp = MagicMock()

        def write(cmd, callback):
            callback(b"RINEXDATA")  # simulate streamed bytes
            return "226 done"

        ftp.retrbinary.side_effect = write
        with patch("receivers.external_fetch.ftplib.FTP", return_value=ftp):
            path = _fetch_one(
                "ftp://h/15s_data/2026/2432/232/MYVA2320.26e",
                tmp_path,
                username=None,
                password=None,
            )
        assert path.name == "MYVA2320.26e"
        assert path.read_bytes() == b"RINEXDATA"
        # no leftover .part file
        assert [p.name for p in tmp_path.iterdir()] == ["MYVA2320.26e"]


CRINEX_HEADER = (
    "     3.05           O                   M                   RINEX VERSION / TYPE\n"
    "RNX2CRX ver.4.1.0                       19-Aug-26 00:00     CRINEX PROG / DATE\n"
    "3.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE\n"
)
PLAIN_R3_HEADER = (
    "     3.05           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE\n"
)


def _lzw(data: bytes) -> bytes:
    import subprocess

    return subprocess.run(["compress", "-c"], input=data, capture_output=True).stdout


class TestDetectFileFormat:
    def test_lzw_crinex_r3(self, tmp_path):
        from receivers.external_fetch import detect_file_format

        p = tmp_path / "MYVA2300.26e"
        p.write_bytes(_lzw(CRINEX_HEADER.encode()))
        assert detect_file_format(p) == {
            "compression": "lzw",
            "compact": True,
            "version": "3",
        }

    def test_plain_r3(self, tmp_path):
        from receivers.external_fetch import detect_file_format

        p = tmp_path / "x.26o"
        p.write_text(PLAIN_R3_HEADER)
        fmt = detect_file_format(p)
        assert fmt == {"compression": "none", "compact": False, "version": "3"}

    def test_gzip_crinex(self, tmp_path):
        import gzip

        from receivers.external_fetch import detect_file_format

        p = tmp_path / "x.26e"
        p.write_bytes(gzip.compress(CRINEX_HEADER.encode()))
        fmt = detect_file_format(p)
        assert fmt["compression"] == "gzip"
        assert fmt["compact"] is True
        assert fmt["version"] == "3"

    def test_plain_r2(self, tmp_path):
        from receivers.external_fetch import detect_file_format

        p = tmp_path / "x.26o"
        p.write_text(
            "     2.11           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE\n"
        )
        fmt = detect_file_format(p)
        assert fmt["version"] == "2"
        assert fmt["compact"] is False


class TestStandardArchiveName:
    def test_daily(self):
        from receivers.external_fetch import standard_archive_name

        assert (
            standard_archive_name("MYVA", datetime(2026, 8, 18), "1D")
            == "MYVA2300.26D.Z"
        )


class TestNormalizeExternalFile:
    def test_rename_and_validate_lzw_crinex(self, tmp_path):
        from receivers.external_fetch import normalize_external_file

        p = tmp_path / "MYVA2300.26e"
        p.write_bytes(_lzw(CRINEX_HEADER.encode()))
        out = normalize_external_file(p, "MYVA", datetime(2026, 8, 18), tmp_path, "1D")
        assert out.name == "MYVA2300.26D.Z"
        assert out.exists()

    def test_refuses_non_lzw(self, tmp_path):
        from receivers.external_fetch import normalize_external_file

        p = tmp_path / "MYVA2300.26e"
        p.write_text(CRINEX_HEADER)  # plain — wrong compression
        with pytest.raises(ValueError, match="unsupported external format"):
            normalize_external_file(p, "MYVA", datetime(2026, 8, 18), tmp_path, "1D")
        assert p.exists()  # original left in place (not archived)

    def test_refuses_plain_r3(self, tmp_path):
        from receivers.external_fetch import normalize_external_file

        p = tmp_path / "MYVA2300.26o"
        p.write_text(PLAIN_R3_HEADER)  # not Hatanaka
        with pytest.raises(ValueError, match="unsupported external format"):
            normalize_external_file(p, "MYVA", datetime(2026, 8, 18), tmp_path, "1D")
