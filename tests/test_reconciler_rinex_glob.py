"""Regression tests for RINEX existence detection in the archive reconciler.

Guards the defect that caused the 2026-08-08/09 incident: `_find_rinex_file()`
globbed the Hatanaka type letter in lowercase only (`*d.Z`), while producers
have written UPPERCASE `.D.Z` since f0fe505. `Path.glob` is case-sensitive on
Linux, so the reconciler never found the RINEX it had just written and
re-converted every raw file on every sweep -- forever, for every station that
does not go through the FormatResolver path (i.e. everything non-polarx5).

The symptom was not a crash but sustained CPU load, which in turn held the
newly-enabled load gate shut and stopped live downloads fleet-wide.
"""

from pathlib import Path

import pytest

from receivers.scheduling.archive_reconciler import _find_rinex_file

# ELDC202608091900b -> station ELDC, 2026-08-09 = DOY 221, hour 19 -> letter 't'
RAW_NAME = "ELDC202608091900b.sbf.gz"


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Build a station/session tree and return the raw file path."""
    raw_dir = tmp_path / "ELDC" / "1Hz_1hr" / "raw"
    rinex_dir = tmp_path / "ELDC" / "1Hz_1hr" / "rinex"
    raw_dir.mkdir(parents=True)
    rinex_dir.mkdir(parents=True)
    raw_path = raw_dir / RAW_NAME
    raw_path.write_bytes(b"raw")
    return raw_path


def _rinex_dir(raw_path: Path) -> Path:
    return raw_path.parent.parent / "rinex"


@pytest.mark.parametrize(
    "rinex_name",
    [
        "ELDC221t.26D.Z",  # the archive convention -- regressed before this fix
        "ELDC221t.26d.Z",  # legacy lowercase, must keep working
        "ELDC221t.26D.gz",
        "ELDC221t.26d.gz",
        "ELDC2210.26D.Z",  # daily-form '0' session char, uppercase
    ],
)
def test_finds_existing_rinex_regardless_of_case(archive: Path, rinex_name: str):
    """An existing RINEX must be found whatever the case of the type letter.

    Failing this test means the reconciler re-converts already-converted files
    on every sweep.
    """
    (_rinex_dir(archive) / rinex_name).write_bytes(b"rinex")

    found = _find_rinex_file(archive)

    assert found is not None, f"{rinex_name} was not found -- phantom re-conversion"
    assert found.name == rinex_name


def test_returns_none_when_no_rinex_exists(archive: Path):
    """Genuinely missing RINEX must still be reported missing."""
    assert _find_rinex_file(archive) is None


def test_does_not_match_a_different_day(archive: Path):
    """The case-tolerant glob must not become a same-station wildcard.

    DOY 220 is the previous day; matching it would make the reconciler skip
    conversions that really are missing.
    """
    (_rinex_dir(archive) / "ELDC220t.26D.Z").write_bytes(b"rinex")

    assert _find_rinex_file(archive) is None
