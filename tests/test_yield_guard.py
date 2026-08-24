"""Guard that keeps observation-less raw files out of the archive.

Worked example throughout: RFEL tracks 0 satellites yet still emits hourly 1Hz
files of ~8 KB where a healthy Trimble writes ~460 KB. The guard must reject
that, and must NEVER reject anything when its own inputs are missing.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from receivers.utils.file_archiver import ArchiveMode, FileArchiver
from receivers.utils.yield_guard import (
    YieldGuardConfig,
    check_yield,
    median_size,
    parse_archive_path,
    satellite_alert,
)

RFEL_SIZE = 8_029  # measured on rek-d01
HEALTHY_MEDIAN = 461_902  # SAUR, same session type


def _conn(median, samples=500):
    """A connection whose baseline query returns (median, sample_count)."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (median, samples)
    return conn


# --- path parsing -----------------------------------------------------------


def test_parse_archive_path_extracts_station_and_session():
    p = Path("/mnt/data/gpsdata/2026/aug/RFEL/1Hz_1hr/raw/RFEL202608142100a.T00")
    assert parse_archive_path(p) == ("RFEL", "1Hz_1hr")


def test_parse_archive_path_rejects_unrecognised_layout():
    assert parse_archive_path(Path("/tmp/foo.sbf")) == (None, None)
    # 5-char "station" is not a station id
    assert parse_archive_path(Path("/a/b/LONGX/1Hz_1hr/raw/f.sbf")) == (None, None)


# --- the actual decision ----------------------------------------------------


def test_rejects_rfel_sized_file():
    v = check_yield(RFEL_SIZE, "RFEL", "1Hz_1hr", _conn(HEALTHY_MEDIAN))
    assert v.allowed is False
    assert v.station == "RFEL"
    assert "below the" in v.reason
    assert v.fraction < 0.02


def test_allows_a_normal_file():
    v = check_yield(HEALTHY_MEDIAN, "SAUR", "1Hz_1hr", _conn(HEALTHY_MEDIAN))
    assert v.allowed is True


def test_allows_a_merely_short_hour():
    """A half-size hour is plausible (power blip); only the floor is rejected."""
    v = check_yield(HEALTHY_MEDIAN // 2, "SAUR", "1Hz_1hr", _conn(HEALTHY_MEDIAN))
    assert v.allowed is True


def test_boundary_is_inclusive_at_the_floor():
    """>= floor passes, just under it does not."""
    import math

    floor = math.ceil(HEALTHY_MEDIAN * 0.10)
    assert check_yield(floor, "SAUR", "1Hz_1hr", _conn(HEALTHY_MEDIAN)).allowed is True
    assert (
        check_yield(floor - 2, "SAUR", "1Hz_1hr", _conn(HEALTHY_MEDIAN)).allowed
        is False
    )


# --- fail-open guarantees (the important half) ------------------------------


def test_fails_open_without_a_connection():
    v = check_yield(0, "RFEL", "1Hz_1hr", None)
    assert v.allowed is True


def test_fails_open_without_enough_samples():
    """A thin baseline is noise, not a baseline."""
    v = check_yield(RFEL_SIZE, "RFEL", "1Hz_1hr", _conn(HEALTHY_MEDIAN, samples=3))
    assert v.allowed is True
    assert "no baseline" in v.reason


def test_fails_open_when_the_query_raises():
    conn = MagicMock()
    conn.cursor.side_effect = Exception("db down")
    v = check_yield(RFEL_SIZE, "RFEL", "1Hz_1hr", conn)
    assert v.allowed is True


def test_fails_open_on_null_median():
    v = check_yield(RFEL_SIZE, "NEWS", "1Hz_1hr", _conn(None))
    assert v.allowed is True


def test_baseline_query_excludes_zero_byte_rows():
    """Catalog rows with size 0 (missing files) would drag the median to zero
    and disarm the guard exactly where it is needed."""
    conn = _conn(HEALTHY_MEDIAN)
    median_size(conn, "RFEL", "1Hz_1hr")
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]
    assert "file_size > 0" in sql


# --- archiver integration ---------------------------------------------------


def test_guard_is_off_by_default(tmp_path):
    """No config = no behaviour change for every existing caller."""
    archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE)
    assert archiver.yield_guard is None
    assert archiver._check_yield(tmp_path / "f", tmp_path / "f", 1) is None


def test_rejected_file_is_not_archived(tmp_path):
    src = tmp_path / "tmp" / "RFEL202608142100a.T00"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * RFEL_SIZE)
    dest = tmp_path / "arch" / "2026" / "aug" / "RFEL" / "1Hz_1hr" / "raw" / src.name
    qroot = tmp_path / "quarantine"

    archiver = FileArchiver(
        mode=ArchiveMode.IMMEDIATE,
        yield_guard=YieldGuardConfig(
            connection=_conn(HEALTHY_MEDIAN), quarantine_root=qroot
        ),
    )
    ok = archiver.archive_file(src, dest, compress=False)

    assert ok is False
    assert not dest.exists(), "rejected file must not enter the archive tree"
    assert not dest.parent.exists(), "must not even create the archive directory"
    assert (qroot / "RFEL" / src.name).exists(), "should be quarantined for inspection"


def test_allowed_file_still_archives_with_guard_on(tmp_path):
    src = tmp_path / "tmp" / "SAUR202608142100a.T00"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * HEALTHY_MEDIAN)
    dest = tmp_path / "arch" / "2026" / "aug" / "SAUR" / "1Hz_1hr" / "raw" / src.name

    archiver = FileArchiver(
        mode=ArchiveMode.IMMEDIATE,
        yield_guard=YieldGuardConfig(connection=_conn(HEALTHY_MEDIAN)),
    )
    assert archiver.archive_file(src, dest, compress=False) is True
    assert dest.exists()


def test_guard_failure_does_not_break_archiving(tmp_path):
    """A broken guard must not stop real data being archived."""
    src = tmp_path / "tmp" / "SAUR202608142100a.T00"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * HEALTHY_MEDIAN)
    dest = tmp_path / "arch" / "2026" / "aug" / "SAUR" / "1Hz_1hr" / "raw" / src.name

    broken = MagicMock()
    broken.enabled = True
    type(broken).min_fraction = property(
        lambda self: (_ for _ in ()).throw(Exception())
    )

    archiver = FileArchiver(mode=ArchiveMode.IMMEDIATE, yield_guard=broken)
    assert archiver.archive_file(src, dest, compress=False) is True
    assert dest.exists()


# --- the alert half ---------------------------------------------------------


@pytest.mark.parametrize("sats,expected", [(0, True), (None, False), (12, False)])
def test_satellite_alert_fires_only_at_zero(sats, expected, caplog):
    assert satellite_alert("RFEL", sats) is expected


def test_satellite_alert_names_the_station_and_urges_investigation():
    """Assert on the emitted record, not on caplog.

    caplog only sees records that propagate, and setup_logging() turns
    propagation OFF for the receivers.* hierarchy — so a caplog assertion here
    passes in isolation and fails as soon as a test that configures logging has
    run first. Order-dependent tests are worse than no test.
    """
    from unittest.mock import patch

    with patch("logging.getLogger") as get_logger:
        satellite_alert("RFEL", 0)

    log = get_logger.return_value
    log.error.assert_called_once()
    fmt, station = log.error.call_args[0][0], log.error.call_args[0][1]
    assert station == "RFEL"
    # Must not read as "handled" — a filtered station still needs a site visit.
    assert "investigation" in fmt.lower()


def test_db_writer_raises_the_zero_satellite_alert(caplog):
    """The alert must fire from where the evidence actually lands."""
    import logging
    from unittest.mock import MagicMock, patch

    from receivers.health.db_writer import HealthDatabaseWriter

    w = HealthDatabaseWriter()
    w._conn = MagicMock()
    with (
        caplog.at_level(logging.ERROR),
        patch("receivers.utils.yield_guard.satellite_alert") as alert,
    ):
        w._write_satellite_tracking(
            "RFEL", __import__("datetime").datetime.now(), {"total": 0}
        )
    alert.assert_called_once_with("RFEL", 0)


# --- the self-poisoning trap ------------------------------------------------


def test_baseline_window_is_lagged_not_trailing():
    """A station that broke recently must not define its own fault as normal.

    RFEL's trailing 14-day median was 8,085 bytes, making a 10% floor of 808 —
    its ~8 KB shells passed and the guard missed the case it exists for. The
    window must END before today, not at it.
    """
    conn = _conn(HEALTHY_MEDIAN)
    median_size(conn, "RFEL", "1Hz_1hr", lookback_days=90, lag_days=14)
    call = conn.cursor.return_value.__enter__.return_value.execute.call_args
    sql, params = call[0][0], call[0][1]
    assert "BETWEEN current_date -" in sql, "must bound both ends of the window"
    # (station, session, start_offset, end_offset) with end strictly before today
    assert params[2] == 104 and params[3] == 14
    assert params[3] > 0, "window must not run up to today"


def test_lagged_baseline_catches_a_recently_broken_station():
    """With a healthy pre-fault baseline, RFEL's shells are rejected."""
    v = check_yield(RFEL_SIZE, "RFEL", "1Hz_1hr", _conn(440_436))
    assert v.allowed is False
    assert v.fraction < 0.02


# --- the connection must not be left in a transaction ------------------------


class _TxTrackingConn:
    """Minimal psycopg2-shaped connection that records transaction state.

    `MagicMock` cannot answer this: it happily absorbs `rollback()` whether or
    not it is called, so a leak looks identical to a clean read.
    """

    def __init__(self, row=(461_902, 500)):
        self._row = row
        self.in_transaction = False
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *a, **k):
                conn.in_transaction = True  # psycopg2 opens one implicitly

            def fetchone(self):
                return conn._row

        return _Cur()

    def rollback(self):
        self.rollbacks += 1
        self.in_transaction = False

    def commit(self):
        self.commits += 1
        self.in_transaction = False


def test_median_size_leaves_no_open_transaction():
    """The guard runs per archived file on a long-lived connection.

    Leaving the implicit transaction open parked that connection in
    `idle in transaction` after every file — a contributor to the chronic
    connection-slot exhaustion on rek-d01.
    """
    conn = _TxTrackingConn()
    assert median_size(conn, "RFEL", "1Hz_1hr") == 461_902
    assert conn.in_transaction is False
    assert conn.rollbacks == 1


def test_median_size_ends_the_transaction_even_when_the_query_raises():
    class _Boom(_TxTrackingConn):
        def cursor(self):
            outer = self

            class _Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, *a, **k):
                    outer.in_transaction = True
                    raise RuntimeError("query blew up")

            return _Cur()

    conn = _Boom()
    assert median_size(conn, "RFEL", "1Hz_1hr") is None  # fails open
    assert conn.in_transaction is False


def test_median_size_never_commits():
    """rollback, not commit — discarding a stray write is the safer failure."""
    conn = _TxTrackingConn()
    median_size(conn, "RFEL", "1Hz_1hr")
    assert conn.commits == 0
