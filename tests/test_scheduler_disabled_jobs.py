"""Regression tests for the 2026-08 'archive_sync disabled but still pushing' bug.

Two bypasses are covered:

1. Persisted jobstore jobs survived a config disable — every _schedule_* hook
   early-returned without removing the already-persisted job, so APScheduler
   reloaded it from SQLite on every restart and it kept firing.
   _mark_disabled()/_remove_disabled_jobs() now drop them post-start.

2. The push-on-download write-through (_push_to_storage) ignored
   ``archive_sync.enabled`` — it was gated only on a sync.yaml target being
   active. ``_get_push_target`` now honours ``archive_sync.enabled`` (with
   ``push_on_download`` as an independent override).
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

try:
    from receivers.scheduling import bulk_scheduler as bs

    SCHEDULER_AVAILABLE = bs.HAS_APSCHEDULER
except ImportError:
    SCHEDULER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not SCHEDULER_AVAILABLE, reason="APScheduler not installed"
)


def _scheduler_stub(disabled_specs):
    """BulkDownloadScheduler shell with a mocked APScheduler (no __init__)."""
    sched = bs.BulkDownloadScheduler.__new__(bs.BulkDownloadScheduler)
    sched.logger = logging.getLogger("receivers.test")
    sched._disabled_jobs = list(disabled_specs)
    sched.scheduler = MagicMock()
    return sched


def _fake_job(job_id):
    return MagicMock(id=job_id)


@pytest.mark.unit
@pytest.mark.scheduler
class TestRemoveDisabledJobs:
    """_remove_disabled_jobs drops persisted jobs for config-disabled features."""

    def test_exact_id_removed(self):
        sched = _scheduler_stub(["archive_sync"])
        sched.scheduler.get_jobs.return_value = [_fake_job("archive_sync")]

        sched._remove_disabled_jobs()

        sched.scheduler.remove_job.assert_called_once_with("archive_sync")

    def test_prefix_removes_matching_only(self):
        sched = _scheduler_stub(["backfill_*"])
        sched.scheduler.get_jobs.return_value = [
            _fake_job("backfill_15s_24hr"),
            _fake_job("backfill_1Hz_1hr"),
            _fake_job("reconnection_backfill"),  # must NOT match the prefix
            _fake_job("archive_sync"),
        ]

        sched._remove_disabled_jobs()

        removed = {c.args[0] for c in sched.scheduler.remove_job.call_args_list}
        assert removed == {"backfill_15s_24hr", "backfill_1Hz_1hr"}

    def test_morning_recovery_prefix_covers_multi_fire_ids(self):
        sched = _scheduler_stub(["morning_recovery*"])
        sched.scheduler.get_jobs.return_value = [
            _fake_job("morning_recovery"),
            _fake_job("morning_recovery_0"),
            _fake_job("morning_recovery_1"),
        ]

        sched._remove_disabled_jobs()

        removed = {c.args[0] for c in sched.scheduler.remove_job.call_args_list}
        assert removed == {
            "morning_recovery",
            "morning_recovery_0",
            "morning_recovery_1",
        }

    def test_missing_job_is_noop(self):
        """Disabled feature whose job was never persisted: no remove call."""
        sched = _scheduler_stub(["archive_sync"])
        sched.scheduler.get_jobs.return_value = [_fake_job("gap_detection")]

        sched._remove_disabled_jobs()

        sched.scheduler.remove_job.assert_not_called()

    def test_empty_queue_touches_nothing(self):
        sched = _scheduler_stub([])

        sched._remove_disabled_jobs()

        sched.scheduler.get_jobs.assert_not_called()
        sched.scheduler.remove_job.assert_not_called()

    def test_duplicate_specs_deduped(self):
        sched = _scheduler_stub(["archive_sync", "archive_sync"])
        sched.scheduler.get_jobs.return_value = [_fake_job("archive_sync")]

        sched._remove_disabled_jobs()

        sched.scheduler.remove_job.assert_called_once_with("archive_sync")


@pytest.mark.unit
@pytest.mark.scheduler
class TestMarkDisabled:
    """_mark_disabled logs at the requested level and queues the ids."""

    def test_queues_ids_and_logs(self, caplog):
        sched = _scheduler_stub([])

        with caplog.at_level(logging.DEBUG, logger="receivers.test"):
            sched._mark_disabled("Archive sync", "archive_sync")
            sched._mark_disabled(
                "Stream capture", "stream_supervise", level=logging.DEBUG
            )

        assert sched._disabled_jobs == ["archive_sync", "stream_supervise"]
        messages = [r.message for r in caplog.records]
        assert "Archive sync disabled in config" in messages
        assert "Stream capture disabled in config" in messages


@pytest.mark.unit
@pytest.mark.scheduler
class TestPushTargetGating:
    """_get_push_target honours archive_sync.enabled / push_on_download."""

    @pytest.fixture(autouse=True)
    def reset_push_target_cache(self):
        bs._push_target = None
        bs._push_target_loaded = False
        yield
        bs._push_target = None
        bs._push_target_loaded = False

    def _run(self, archive_sync_cfg):
        active_target = MagicMock(active=True)
        with (
            patch(
                "receivers.scheduling.config_loader.load_scheduler_config"
            ) as m_cfg,
            patch("receivers.archive.load_sync_config") as m_sync,
        ):
            m_cfg.return_value = {"archive_sync": archive_sync_cfg}
            m_sync.return_value = [active_target]
            result = bs._get_push_target()
        return result, m_sync

    def test_disabled_stops_write_through(self):
        result, m_sync = self._run({"enabled": False})

        assert result is None
        m_sync.assert_not_called()  # never even looks at sync.yaml

    def test_enabled_with_active_target_pushes(self):
        result, _ = self._run({"enabled": True})

        assert result is not None
        assert result.active is True

    def test_push_on_download_overrides_enabled_off(self):
        """push_on_download: true keeps write-through while the sweep is off."""
        result, _ = self._run({"enabled": False, "push_on_download": True})

        assert result is not None

    def test_push_on_download_off_overrides_enabled(self):
        result, m_sync = self._run({"enabled": True, "push_on_download": False})

        assert result is None
        m_sync.assert_not_called()

    def test_push_on_download_null_follows_enabled(self):
        """YAML `push_on_download:` (null) behaves as 'follow enabled'."""
        result, _ = self._run({"enabled": True, "push_on_download": None})

        assert result is not None

    def test_missing_archive_sync_section_disables_push(self):
        result, m_sync = self._run({})

        # enabled defaults to False → push off
        assert result is None
        m_sync.assert_not_called()
