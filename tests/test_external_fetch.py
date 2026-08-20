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
