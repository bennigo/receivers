"""Backfill RINEX must yield to live load (2026-08-09 incident fix).

``_run_rinex_conversion`` is the backfill-only chokepoint for synchronous RINEX.
When the load monitor says the box is busy it must defer (return early) so a
historical-recovery backlog cannot saturate the box and cause the live load-gate
to skip ~285 downloads/hour — which is exactly the cascade that left 2026-08-09's
hourly data unconverted.

This function is never on the inline download→RINEX path (that uses the async
``submit_rinex_*`` pool), so deferring here cannot stall a live download.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


def _ok_result() -> MagicMock:
    r = MagicMock()
    r.success = True
    r.data = {"files_converted": 1}
    r.output_files = []
    return r


@pytest.mark.unit
class TestBackfillRinexLoadGate:
    @patch("receivers.scheduling.bulk_scheduler._get_load_monitor")
    @patch("receivers.scheduling.tasks.rinex_task.RINEXTask")
    def test_defers_when_load_high(self, mock_rinex_task, mock_get_monitor):
        """Under load, backfill RINEX returns early — no conversion attempted."""
        from receivers.scheduling.bulk_scheduler import _run_rinex_conversion

        monitor = MagicMock()
        monitor.can_start_job.return_value = False  # BACKFILL priority blocked
        monitor.get_load.return_value.cpu_load_1m = 17.4
        mock_get_monitor.return_value = monitor

        _run_rinex_conversion(
            "ELDC", "15s_24hr", [], {}, logging.getLogger("test")
        )

        mock_rinex_task.assert_not_called()  # deferred, never built

    @patch("receivers.scheduling.bulk_scheduler._get_load_monitor")
    @patch("receivers.scheduling.tasks.rinex_task.RINEXTask")
    def test_runs_when_load_low(self, mock_rinex_task, mock_get_monitor):
        """With headroom, backfill RINEX proceeds normally."""
        from receivers.scheduling.bulk_scheduler import _run_rinex_conversion

        monitor = MagicMock()
        monitor.can_start_job.return_value = True
        monitor.get_load.return_value.cpu_load_1m = 2.0
        mock_get_monitor.return_value = monitor

        instance = MagicMock()
        instance.execute.return_value = _ok_result()
        mock_rinex_task.return_value = instance

        _run_rinex_conversion(
            "ELDC", "15s_24hr", [], {}, logging.getLogger("test")
        )

        mock_rinex_task.assert_called_once()  # conversion proceeded
        instance.execute.assert_called_once()

    @patch("receivers.scheduling.bulk_scheduler._get_load_monitor")
    @patch("receivers.scheduling.tasks.rinex_task.RINEXTask")
    def test_no_monitor_means_no_gate(self, mock_rinex_task, mock_get_monitor):
        """When load monitoring is disabled (monitor is None), backfill RINEX is
        unchanged — the gate is opt-in via the monitor, never a hard block."""
        from receivers.scheduling.bulk_scheduler import _run_rinex_conversion

        mock_get_monitor.return_value = None

        instance = MagicMock()
        instance.execute.return_value = _ok_result()
        mock_rinex_task.return_value = instance

        _run_rinex_conversion(
            "ELDC", "15s_24hr", [], {}, logging.getLogger("test")
        )

        mock_rinex_task.assert_called_once()
