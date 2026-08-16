"""Satellite gate: skip downloads for a receiver that is tracking nothing.

The property that matters most is that it re-opens BY ITSELF. Health checks run
on their own executor and are not gated, so a station keeps being probed every
5 minutes while gated; the moment it tracks a satellite the next window
proceeds. Nobody has to clear a flag — the person who fixes the antenna would
never know to.
"""

from unittest.mock import MagicMock, patch

import pytest

from receivers.utils.download_gate import (
    DEFAULT_MIN_SAMPLES,
    satellite_health,
    should_skip_download,
)


def _conn(samples, max_sats):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        samples,
        max_sats,
    )
    return conn


# --- the decision -----------------------------------------------------------


def test_skips_when_nothing_tracked_across_a_full_window():
    skip, why = should_skip_download("RFEL", _conn(72, 0))
    assert skip is True
    assert "0 satellites" in why
    # The reason must tell the reader it recovers on its own.
    assert "resume automatically" in why


def test_does_not_skip_a_healthy_station():
    skip, why = should_skip_download("SAUR", _conn(72, 12))
    assert skip is False
    assert "12 satellites" in why


def test_a_single_tracked_satellite_is_enough_to_proceed():
    """MAX, not average: one epoch with sky proves the receiver works.

    A struggling station (obstruction, icing) still yields real data.
    """
    skip, _ = should_skip_download("HVOL", _conn(72, 1))
    assert skip is False


# --- fail-open guarantees ---------------------------------------------------


def test_does_not_skip_without_a_connection():
    skip, why = should_skip_download("RFEL", None)
    assert skip is False
    assert "no satellite health evidence" in why


def test_does_not_skip_when_the_query_raises():
    conn = MagicMock()
    conn.cursor.side_effect = Exception("db down")
    assert should_skip_download("RFEL", conn)[0] is False


def test_does_not_skip_on_a_thin_sample_count():
    """A health outage or a fresh station must not look like a dead receiver."""
    skip, why = should_skip_download("RFEL", _conn(DEFAULT_MIN_SAMPLES - 1, 0))
    assert skip is False
    assert "health samples" in why


def test_does_not_skip_a_receiver_family_that_reports_no_satellites():
    """Absence of a satellite block is absence of evidence, not of satellites."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (0, 0)
    assert should_skip_download("SOMELEICA", conn)[0] is False


def test_satellite_health_returns_none_without_rows():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (0, 0)
    assert satellite_health("X", conn) is None


# --- the self-healing property ----------------------------------------------


def test_gate_reopens_as_soon_as_satellites_return():
    """Same station, same code path: gated, then not, with no intervention."""
    assert should_skip_download("RFEL", _conn(72, 0))[0] is True
    # ...antenna fixed, health (which was never gated) records a satellite...
    assert should_skip_download("RFEL", _conn(72, 4))[0] is False


# --- integration with the download job --------------------------------------


def _run_job():
    from receivers.scheduling.bulk_scheduler import _download_station_data_job

    return _download_station_data_job("RFEL", "1Hz_1hr")


def test_download_job_returns_early_when_gated():
    with (
        patch("receivers.db.connection.get_connection", return_value=MagicMock()),
        patch(
            "receivers.utils.download_gate.should_skip_download",
            return_value=(True, "0 satellites"),
        ),
        patch("receivers.scheduling.bulk_scheduler._get_load_monitor") as load,
    ):
        _run_job()
    # Gated before any work — the load monitor is further down the function.
    load.assert_not_called()


def test_download_job_proceeds_when_not_gated():
    with (
        patch("receivers.db.connection.get_connection", return_value=MagicMock()),
        patch(
            "receivers.utils.download_gate.should_skip_download",
            return_value=(False, "tracking 9 satellites"),
        ),
        patch(
            "receivers.scheduling.bulk_scheduler._get_load_monitor", return_value=None
        ) as load,
    ):
        # The job handles an unreachable station gracefully rather than raising;
        # reaching the load monitor is what proves it got past the gate.
        _run_job()
        load.assert_called()


def test_a_broken_gate_never_blocks_a_download():
    """A gate that cannot even open its connection must not stop the download."""
    with (
        patch("receivers.db.connection.get_connection", side_effect=Exception("boom")),
        patch(
            "receivers.scheduling.bulk_scheduler._get_load_monitor", return_value=None
        ) as load,
    ):
        _run_job()
        load.assert_called()
