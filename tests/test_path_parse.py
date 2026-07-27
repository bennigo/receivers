"""Tests for archive path/filename date parsing (receivers.archive.path_parse
and receivers.utils.download_tracker.parse_date_from_filename).

Regression guard for the historical re-rinex mis-dating bug: a short-name
RINEX-2 file (``NYLA060a.21D.Z``) reindexed from a ``2021/`` archive path must be
catalogued under its *observation* year (2021), not the year the reindex ran,
and hour 0 of an hourly session must not be read as a daily file.
"""

from datetime import date, datetime, timedelta

from receivers.archive.path_parse import parse_archive_path
from receivers.utils.download_tracker import parse_date_from_filename

# --- parse_archive_path: the reindex path (year + hour anchored to the path) ---


def test_short_name_rinex_dated_by_path_year_not_now():
    """NYLA060a.21D.Z under 2021/ is 2021-03-01 hour 0 — the exact bug fixed."""
    root = "/mnt/rawgpsdata"
    path = f"{root}/2021/mar/NYLA/1Hz_1hr/rinex/NYLA060a.21D.Z"

    parsed = parse_archive_path(path, root)

    assert parsed is not None
    assert parsed.file_date == date(2021, 3, 1)  # day-of-year 060 of 2021
    assert parsed.file_hour == 0  # letter 'a' = hour 0 for an hourly session
    assert parsed.station == "NYLA"
    assert parsed.session_type == "1Hz_1hr"
    assert parsed.file_category == "rinex"


def test_short_name_hourly_letter_maps_to_hour():
    """b..x -> 1..23 for an hourly session, still anchored to the path year."""
    root = "/mnt/rawgpsdata"
    path = f"{root}/2021/mar/NYLA/1Hz_1hr/rinex/NYLA060i.21D.Z"

    parsed = parse_archive_path(path, root)

    assert parsed is not None
    assert parsed.file_date == date(2021, 3, 1)
    assert parsed.file_hour == 8  # 'i' = 8


def test_long_name_septentrio_unaffected_by_year_anchor():
    """Long names carry a full date; the path-year anchor must not override it."""
    root = "/mnt/rawgpsdata"
    path = f"{root}/2021/mar/NYLA/1Hz_1hr/raw/NYLA202103010500b.T00"

    parsed = parse_archive_path(path, root)

    assert parsed is not None
    assert parsed.file_date == date(2021, 3, 1)
    assert parsed.file_hour == 5


def test_daily_zero_short_name_is_dated_no_hour():
    """RINEX-2 daily file ``NYLAddd0.YYd`` (seq char '0') must date, hour=None.

    Regression for the daily-'0' parser gap: the short-name regex only matched
    ``[a-x]``, so daily files parsed to None -> file_date NULL in the catalog.
    """
    root = "/mnt/rawgpsdata"
    path = f"{root}/2021/mar/NYLA/15s_24hr/rinex/NYLA0600.21D.Z"

    parsed = parse_archive_path(path, root)

    assert parsed is not None
    assert parsed.file_date == date(2021, 3, 1)  # DOY 060 of 2021
    assert parsed.file_hour is None  # '0' = daily file, no hour
    assert parsed.session_type == "15s_24hr"


def test_daily_zero_leap_year():
    """'0' daily on a leap DOY resolves to the leap date (2024 DOY060 = Feb 29)."""
    root = "/mnt/rawgpsdata"
    path = f"{root}/2024/feb/NYLA/15s_24hr/rinex/NYLA0600.24D.Z"
    parsed = parse_archive_path(path, root)
    assert parsed is not None
    assert parsed.file_date == date(2024, 2, 29)
    assert parsed.file_hour is None


# --- parse_date_from_filename: unit-level, default_year / session_type ---


def test_default_year_anchors_short_name():
    d, h = parse_date_from_filename(
        "NYLA060a.21D.Z", "NYLA", default_year=2021, session_type="1Hz_1hr"
    )
    assert d == date(2021, 3, 1)
    assert h == 0


def test_no_context_preserves_legacy_current_year_and_daily_a():
    """Without default_year/session_type the old behaviour is preserved:
    current-year fallback and 'a' = daily (no hour) — so FTP-listing and
    download-tracker callers are unchanged."""
    now_year = datetime.now().year
    d, h = parse_date_from_filename("SKFC266a.m00", "SKFC")
    assert d == date(now_year, 1, 1) + timedelta(days=265)  # DOY 266, current year
    assert h is None  # 'a' with no hourly session context = daily


def test_daily_session_letter_a_is_not_an_hour():
    """A daily session's 'a' stays hourless even with a default_year."""
    d, h = parse_date_from_filename(
        "SKFC266a.m00", "SKFC", default_year=2019, session_type="15s_24hr"
    )
    assert d == date(2019, 1, 1) + timedelta(days=265)  # DOY 266 of 2019
    assert h is None
