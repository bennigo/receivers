"""The hard rule: never overwrite an archived product that cannot be regenerated.

`--fix-headers` has enforced this since 1e0d81d (2026-07-03) via
``check_regenerable()`` + ``preserve_original_file()`` into a permanent
``rinex_org/``. The re-rinex path built two days later (615941c, 706dd28)
shipped its own ``--backup-old`` -> ``rinex_bak/`` instead — which *looks* like
the same protection but is unconditional and explicitly deletable. So a
re-conversion of a raw-less date overwrote an irreplaceable file with no
preservation and no refusal, for thirteen months.

The rule had been written as a step inside one verb rather than as an invariant
over every operation that overwrites an archived product. These tests state it
as the invariant, against the push path.
"""

import datetime as dt
from pathlib import Path

import pytest

from receivers.rinex.raw_presence import check_regenerable

SID = "ELDC"
SESSION = "15s_24hr"
OBS = dt.datetime(2026, 4, 1)


def _archive(tmp_path: Path, *, raw_name: str | None) -> Path:
    """Build YYYY/mon/SID/session/{rinex,raw}/ and return the rinex file."""
    base = tmp_path / "2026" / "apr" / SID / SESSION
    (base / "rinex").mkdir(parents=True)
    (base / "raw").mkdir(parents=True)
    prod = base / "rinex" / "ELDC0920.26D.Z"
    prod.write_bytes(b"archived product")
    if raw_name:
        (base / "raw" / raw_name).write_bytes(b"raw")
    return prod


def test_raw_present_and_recognised_is_regenerable(tmp_path):
    """Raw exists in a known format -> safe to overwrite, no preservation needed."""
    prod = _archive(tmp_path, raw_name="ELDC202604010000a.sbf.gz")

    res = check_regenerable(prod, OBS, station_id=SID, session_type=SESSION)

    assert res.regenerable is True


def test_no_raw_at_all_is_NOT_regenerable(tmp_path):
    """The data-loss case: nothing to rebuild from. Must refuse the overwrite."""
    prod = _archive(tmp_path, raw_name=None)

    res = check_regenerable(prod, OBS, station_id=SID, session_type=SESSION)

    assert res.regenerable is False


def test_raw_for_a_DIFFERENT_date_is_not_regenerable(tmp_path):
    """A raw for another day must not be mistaken for this date's raw."""
    prod = _archive(tmp_path, raw_name="ELDC202603150000a.sbf.gz")

    res = check_regenerable(prod, OBS, station_id=SID, session_type=SESSION)

    assert res.regenerable is False


def test_unrecognised_raw_format_is_not_regenerable(tmp_path):
    """bgo's caveat: raw present but in an unknown format counts as
    un-regenerable — we cannot actually rebuild from it."""
    prod = _archive(tmp_path, raw_name="ELDC202604010000a.xyzzy")

    res = check_regenerable(prod, OBS, station_id=SID, session_type=SESSION)

    assert res.regenerable is False


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("ELDC202604010000a.sbf.gz", True),
        ("ELDC202604010000a.T02", True),
        ("ELDC202604010000a.T00", True),
        ("ELDC202604010000a.m00.gz", True),
    ],
)
def test_every_converter_registry_format_counts_as_regenerable(
    tmp_path, raw_name, expected
):
    prod = _archive(tmp_path, raw_name=raw_name)

    res = check_regenerable(prod, OBS, station_id=SID, session_type=SESSION)

    assert res.regenerable is expected


def test_gate_fails_closed_on_error():
    """If regenerability cannot be determined, the push path treats it as
    un-regenerable and refuses — never fail open toward overwriting."""
    # A path with no archive layout at all: check_regenerable must not report
    # True, so the caller's `if regen.regenerable` branch cannot overwrite.
    res = check_regenerable(
        Path("/nonexistent/rinex/ELDC0920.26D.Z"),
        OBS,
        station_id=SID,
        session_type=SESSION,
    )

    assert res.regenerable is False
