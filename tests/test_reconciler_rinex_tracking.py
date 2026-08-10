"""Tests for the reconciler's file_tracking lookup path.

Guards the defect that took pgdev down on 2026-08-10: `_ensure_rinex_tracked()`
asked "is this RINEX tracked?" with

    SELECT 1 FROM file_tracking WHERE sid=%s AND session_type=%s AND filename=%s

`filename` carries no index, and the call sits inside the reconciler's
station x date x hour loop with its own connection. pg_stat_statements measured
22.6M calls at 24 ms each -- 151 hours of server CPU, and the bulk of
file_tracking's 46M sequential scans over 6.18e12 tuples.

The fix keys the lookup on the table's uniqueness slot (sid, session_type,
file_date, file_hour) and prefetches the whole window in one range query. These
tests pin the two properties that make that safe:

  * a slot whose recorded filename differs still re-upserts (so a .o.Z -> .d.Z
    or R2 -> R3 rename corrects the stored name, as the filename check did); and
  * a failed prefetch degrades to a point query, never to "nothing is tracked"
    -- which would re-upsert every file in the window on every sweep.
"""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.scheduling.archive_reconciler import _ensure_rinex_tracked

STATION = "ELDC"
SESSION = "1Hz_1hr"
# ELDC, 2026 DOY 221, session letter 't' -> hour 19
HOURLY_NAME = "ELDC221t.26D.Z"
HOURLY_SLOT = (date(2026, 8, 9), 19)
# Daily file: session '0' -> file_hour is None, the NULL-slot case
DAILY_NAME = "ELDC2210.26D.Z"
DAILY_SLOT = (date(2026, 8, 9), None)


@pytest.fixture
def track_calls():
    """Patch the upsert helper and collect the paths it was asked to track."""
    calls: list[str] = []

    def _fake_track(station_id, session_type, output_files, logger):
        calls.extend(output_files)

    with patch(
        "receivers.scheduling.bulk_scheduler._track_rinex_output_files",
        side_effect=_fake_track,
    ):
        yield calls


@pytest.mark.parametrize(
    "filename,slot",
    [(HOURLY_NAME, HOURLY_SLOT), (DAILY_NAME, DAILY_SLOT)],
    ids=["hourly", "daily-null-hour"],
)
def test_prefetched_hit_skips_upsert(track_calls, filename, slot):
    """A slot already tracked under the same name does no work at all."""
    _ensure_rinex_tracked(
        STATION, SESSION, Path("/archive") / filename, tracked={slot: filename}
    )
    assert track_calls == []


def test_prefetched_miss_upserts_and_updates_map(track_calls):
    """An untracked slot is registered -- and the map learns it."""
    tracked: dict = {}
    path = Path("/archive") / HOURLY_NAME

    _ensure_rinex_tracked(STATION, SESSION, path, tracked=tracked)

    assert track_calls == [str(path)]
    assert tracked[HOURLY_SLOT] == HOURLY_NAME

    # Second call in the same sweep is now a hit, not a repeat upsert.
    _ensure_rinex_tracked(STATION, SESSION, path, tracked=tracked)
    assert track_calls == [str(path)]


def test_renamed_file_in_same_slot_reupserts(track_calls):
    """A slot tracked under a stale name (.o.Z era) is corrected, not skipped.

    This is what the old filename-equality check bought us; keying on the slot
    alone would freeze the stale name in file_tracking forever.
    """
    path = Path("/archive") / HOURLY_NAME

    _ensure_rinex_tracked(
        STATION, SESSION, path, tracked={HOURLY_SLOT: "ELDC221t.26o.Z"}
    )

    assert track_calls == [str(path)]


def test_out_of_window_file_is_not_trusted_as_a_miss(track_calls):
    """A file parsing outside the prefetched range takes the point query.

    Its absence from the map says nothing about whether it is tracked, so
    trusting the miss would re-upsert it on every single sweep -- a fresh
    infinite loop of exactly the 2026-08-08/09 shape.
    """
    executed: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchone(self):
            return (HOURLY_NAME,)  # it IS tracked -- the map just didn't cover it

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    with patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.connection",
        return_value=_Conn(),
    ):
        _ensure_rinex_tracked(
            STATION,
            SESSION,
            Path("/archive") / HOURLY_NAME,
            tracked={},  # empty map...
            tracked_window=(date(2026, 9, 1), date(2026, 9, 30)),  # ...wrong window
        )

    assert len(executed) == 1, "empty map must not be trusted outside its window"
    assert track_calls == []


def test_unparseable_name_falls_through_to_tracker(track_calls):
    """A name the parser rejects is handed to the tracker, which logs it."""
    path = Path("/archive/not-a-rinex-file.dat")

    _ensure_rinex_tracked(STATION, SESSION, path, tracked={})

    assert track_calls == [str(path)]


def test_no_prefetch_uses_indexed_point_query(track_calls):
    """Without a prefetch map the fallback must filter on the slot columns.

    Explicitly asserts `filename` is NOT in the WHERE clause -- that predicate
    is the unindexed one that caused the incident.
    """
    executed: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchone(self):
            return (HOURLY_NAME,)  # already tracked under this name

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    with patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.connection",
        return_value=_Conn(),
    ):
        _ensure_rinex_tracked(STATION, SESSION, Path("/archive") / HOURLY_NAME)

    assert len(executed) == 1
    sql, params = executed[0]
    assert "filename = " not in sql
    assert "file_date = " in sql and "file_hour = " in sql
    assert params == (STATION, f"{SESSION}_rinex", HOURLY_SLOT[0], HOURLY_SLOT[1])
    assert track_calls == []  # filename matched -> no upsert


def test_no_prefetch_daily_uses_is_null_branch(track_calls):
    """Daily rows really do carry file_hour NULL, so the branch must match."""
    executed: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchone(self):
            return None  # not tracked

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    path = Path("/archive") / DAILY_NAME
    with patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.connection",
        return_value=_Conn(),
    ):
        _ensure_rinex_tracked(STATION, "15s_24hr", path)

    sql, params = executed[0]
    assert "file_hour IS NULL" in sql
    assert params == (STATION, "15s_24hr_rinex", DAILY_SLOT[0])
    assert track_calls == [str(path)]
