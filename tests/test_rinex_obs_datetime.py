"""Tests for the generic RINEX obs-datetime parser used by fix-headers.

_rinex_obs_datetime handles daily AND hourly (and long) RINEX names by
delegating to RinexNamer.parse_date_hour — this is what lets fix-headers run on
1Hz_1hr (hourly) files, not just daily 15s_24hr.
"""

from datetime import datetime

import pytest

from receivers.rinex.header_fix import _rinex_obs_datetime


@pytest.mark.parametrize(
    "name,expected",
    [
        # RINEX 2 short — daily (session '0') → 00:00
        ("RHOF1720.26D.Z", datetime(2026, 6, 21, 0)),
        ("RHOF1720.26O", datetime(2026, 6, 21, 0)),
        # RINEX 2 short — hourly (session a-x) → the file's hour
        ("RHOF172a.26D.Z", datetime(2026, 6, 21, 0)),
        ("RHOF172b.26D.Z", datetime(2026, 6, 21, 1)),
        ("RHOF172n.26D.Z", datetime(2026, 6, 21, 13)),
        ("RHOF172x.26D.Z", datetime(2026, 6, 21, 23)),
    ],
)
def test_parses_daily_and_hourly(name, expected):
    assert _rinex_obs_datetime(name, "RHOF") == expected


def test_year_pivot():
    # 2-digit year pivot: 99 → 1999, 05 → 2005.
    assert _rinex_obs_datetime("RHOF0010.99D.Z", "RHOF") == datetime(1999, 1, 1, 0)
    assert _rinex_obs_datetime("RHOF0010.05D.Z", "RHOF") == datetime(2005, 1, 1, 0)


def test_unparseable_returns_none():
    assert _rinex_obs_datetime("not-a-rinex.txt", "RHOF") is None


def test_station_mismatch_returns_none():
    # A different station's file must not parse as ours.
    assert _rinex_obs_datetime("ELDC1720.26D.Z", "RHOF") is None
