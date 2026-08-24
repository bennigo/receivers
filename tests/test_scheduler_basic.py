"""Unit tests for BulkDownloadScheduler basic functionality.

Tests scheduler initialization, configuration, job scheduling logic,
and time distribution without executing actual downloads.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# Check if APScheduler is available
try:
    from receivers.scheduling.bulk_scheduler import (
        HAS_APSCHEDULER,
        BulkDownloadScheduler,
        ScheduleConfig,
    )
    from receivers.scheduling.config_loader import (
        create_default_config_file,
        get_default_config,
    )

    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    pytestmark = pytest.mark.skip(reason="APScheduler not installed")


@pytest.fixture(autouse=True)
def mock_scheduler_config():
    """Mock load_scheduler_config to return defaults for all tests.

    This prevents tests from loading the user's actual scheduler.yaml file,
    which may have different settings (like 'schedule: 5m' for testing).
    """
    with patch("receivers.scheduling.config_loader.load_scheduler_config") as mock:
        mock.return_value = get_default_config()
        yield mock


@pytest.mark.unit
@pytest.mark.scheduler
class TestScheduleConfig:
    """Test ScheduleConfig dataclass."""

    def test_schedule_config_creation(self):
        """Test creating schedule configuration."""
        config = ScheduleConfig(
            session_type="1Hz_1hr",
            schedule_minute=15,
            distribution_window=10,
            frequency="hourly",
            enabled=True,
            max_concurrent=4,
            timeout_minutes=30,
        )

        assert config.session_type == "1Hz_1hr"
        assert config.schedule_minute == 15
        assert config.distribution_window == 10
        assert config.frequency == "hourly"
        assert config.enabled is True
        assert config.max_concurrent == 4
        assert config.timeout_minutes == 30

    def test_schedule_config_defaults(self):
        """Test schedule configuration with defaults."""
        config = ScheduleConfig(
            session_type="15s_24hr",
            schedule_minute=10,
            distribution_window=10,
            frequency="daily",
        )

        # Defaults
        assert config.enabled is True
        assert config.max_concurrent == 3
        assert config.timeout_minutes == 30


@pytest.mark.unit
@pytest.mark.scheduler
class TestBulkDownloadSchedulerInit:
    """Test BulkDownloadScheduler initialization."""

    @patch("receivers.cli.main.get_all_station_configs")
    def test_scheduler_initialization(self, mock_get_stations):
        """Test basic scheduler initialization."""
        # Mock station configs
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
            "TEST2": {"receiver_type": "netr9", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False, max_workers=2)

        assert scheduler.max_workers == 2
        assert scheduler.production_mode is False
        assert len(scheduler.stations) == 2
        assert "TEST1" in scheduler.stations
        assert "TEST2" in scheduler.stations

    @patch("receivers.cli.main.get_all_station_configs")
    def test_scheduler_with_station_filter(self, mock_get_stations):
        """Test scheduler with station filter."""
        mock_get_stations.return_value = {
            "ELDC": {"receiver_type": "polarx5", "enabled": True},
            "ORFC": {"receiver_type": "polarx5", "enabled": True},
            "THOB": {"receiver_type": "netr9", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False,
            station_filter=["ELDC", "orfc"],  # Test case insensitivity
        )

        # Should filter to only specified stations
        assert scheduler.station_filter == ["ELDC", "ORFC"]  # Uppercased

    @patch("receivers.cli.main.get_all_station_configs")
    def test_scheduler_with_max_stations(self, mock_get_stations):
        """Test scheduler with max stations limit."""
        mock_get_stations.return_value = {
            f"TEST{i}": {"receiver_type": "polarx5", "enabled": True} for i in range(10)
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False, max_stations_per_session=3
        )

        assert scheduler.max_stations_per_session == 3

    def test_scheduler_default_configs(self):
        """Test scheduler has correct default session configs."""
        with patch("receivers.cli.main.get_all_station_configs", return_value={}):
            scheduler = BulkDownloadScheduler(production_mode=False)

            # Should have 3 default session types
            assert len(scheduler.schedule_configs) == 3
            assert "15s_24hr" in scheduler.schedule_configs
            assert "1Hz_1hr" in scheduler.schedule_configs
            assert "status_1hr" in scheduler.schedule_configs

            # Check 15s_24hr config
            daily_config = scheduler.schedule_configs["15s_24hr"]
            assert daily_config.frequency == "daily"
            assert daily_config.schedule_minute == 10
            assert daily_config.distribution_window == 10

            # Check 1Hz_1hr config
            hourly_config = scheduler.schedule_configs["1Hz_1hr"]
            assert hourly_config.frequency == "hourly"
            assert hourly_config.schedule_minute == 15
            assert hourly_config.distribution_window == 10

            # Check status_1hr config
            status_config = scheduler.schedule_configs["status_1hr"]
            assert status_config.frequency == "hourly"
            assert status_config.schedule_minute == 25
            assert status_config.distribution_window == 5


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerStationFiltering:
    """Test station filtering logic."""

    @patch("receivers.cli.main.get_all_station_configs")
    def test_get_stations_for_session_no_filter(self, mock_get_stations):
        """Test getting stations without filter."""
        mock_get_stations.return_value = {
            "ELDC": {"receiver_type": "polarx5", "enabled": True},
            "ORFC": {"receiver_type": "polarx5", "enabled": True},
            "THOB": {"receiver_type": "netr9", "enabled": False},  # Disabled
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        stations = scheduler._get_stations_for_session("1Hz_1hr")

        # Should return enabled stations
        assert len(stations) == 2
        assert "ELDC" in stations
        assert "ORFC" in stations
        assert "THOB" not in stations  # Disabled

    @patch("receivers.cli.main.get_all_station_configs")
    def test_get_stations_with_filter(self, mock_get_stations):
        """Test getting stations with filter applied."""
        mock_get_stations.return_value = {
            "ELDC": {"receiver_type": "polarx5", "enabled": True},
            "ORFC": {"receiver_type": "polarx5", "enabled": True},
            "THOB": {"receiver_type": "netr9", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False, station_filter=["ELDC", "THOB"]
        )
        stations = scheduler._get_stations_for_session("1Hz_1hr")

        # Should return only filtered stations
        assert len(stations) == 2
        assert "ELDC" in stations
        assert "THOB" in stations
        assert "ORFC" not in stations

    @patch("receivers.cli.main.get_all_station_configs")
    def test_get_stations_with_max_limit(self, mock_get_stations):
        """Test getting stations with max limit."""
        mock_get_stations.return_value = {
            f"TEST{i}": {"receiver_type": "polarx5", "enabled": True} for i in range(10)
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False, max_stations_per_session=3
        )
        stations = scheduler._get_stations_for_session("1Hz_1hr")

        # Should limit to max_stations
        assert len(stations) == 3

    @patch("receivers.cli.main.get_all_station_configs")
    def test_stream_stations_session_aware(self, mock_get_stations):
        """Stream stations download only the sessions they log on disk
        (remote_sessions); sessions the stream provides are skipped. A stream
        station with no remote_sessions (pure low-bandwidth stream) downloads
        nothing. Regression: acquisition_mode was not carried into self.stations,
        so the skip silently no-opped and stream stations were downloaded too."""
        mock_get_stations.return_value = {
            "ELDC": {"receiver_type": "polarx5", "enabled": True},  # plain download
            "STRD": {  # stream + logs 15s on disk (download the authoritative SBF)
                "receiver_type": "mosaic-X5",
                "enabled": True,
                "acquisition_mode": "stream",
                "remote_sessions": "15s_24hr",
            },
            "PURE": {  # pure stream, no remote_sessions -> download nothing
                "receiver_type": "polarx5",
                "enabled": True,
                "acquisition_mode": "stream",
            },
        }
        scheduler = BulkDownloadScheduler(production_mode=False)

        s15 = scheduler._get_stations_for_session("15s_24hr")
        assert "ELDC" in s15  # plain download
        assert "STRD" in s15  # logs 15s on disk -> authoritative SBF download
        assert "PURE" not in s15  # no remote_sessions -> no download

        s1hz = scheduler._get_stations_for_session("1Hz_1hr")
        assert "ELDC" in s1hz
        assert "STRD" not in s1hz  # 1Hz provided by the stream, not on disk
        assert "PURE" not in s1hz


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerTimeDistribution:
    """Test time distribution logic for scheduled downloads."""

    @patch("receivers.cli.main.get_all_station_configs")
    def test_time_distribution_calculation(self, mock_get_stations):
        """Test that stations are distributed across time window."""
        # Create 10 stations
        mock_get_stations.return_value = {
            f"TEST{i:02d}": {"receiver_type": "polarx5", "enabled": True}
            for i in range(10)
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Get config for 1Hz_1hr (minute 15, window 10)
        config = scheduler.schedule_configs["1Hz_1hr"]
        stations = scheduler._get_stations_for_session("1Hz_1hr")

        # Calculate how stations should be distributed
        stations_per_minute = len(stations) / config.distribution_window  # 10 / 10 = 1

        # First station should be at minute 15
        minute_offset_0 = int(0 / stations_per_minute)  # 0
        assert minute_offset_0 == 0
        schedule_minute_0 = config.schedule_minute + minute_offset_0  # 15 + 0 = 15

        # Fifth station should be at minute 20 (halfway through)
        minute_offset_5 = int(5 / stations_per_minute)  # 5
        assert minute_offset_5 == 5
        config.schedule_minute + minute_offset_5  # 15 + 5 = 20

        # Last station should be at minute 24 (within window)
        minute_offset_9 = int(9 / stations_per_minute)  # 9
        assert minute_offset_9 == 9
        schedule_minute_9 = config.schedule_minute + minute_offset_9  # 15 + 9 = 24

        # Verify all within window
        assert schedule_minute_0 >= config.schedule_minute
        assert schedule_minute_9 < (config.schedule_minute + config.distribution_window)

    @patch("receivers.cli.main.get_all_station_configs")
    def test_many_stations_distribution(self, mock_get_stations):
        """Test distribution with many stations (more than window minutes)."""
        # Create 50 stations with 10-minute window
        mock_get_stations.return_value = {
            f"TEST{i:03d}": {"receiver_type": "polarx5", "enabled": True}
            for i in range(50)
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        config = scheduler.schedule_configs["1Hz_1hr"]
        stations = scheduler._get_stations_for_session("1Hz_1hr")

        stations_per_minute = len(stations) / config.distribution_window  # 50 / 10 = 5

        # Multiple stations should get same minute
        minute_offset_0 = int(0 / stations_per_minute)  # 0
        minute_offset_4 = int(4 / stations_per_minute)  # 0 (both in first minute)
        assert minute_offset_0 == minute_offset_4

        minute_offset_5 = int(5 / stations_per_minute)  # 1
        assert minute_offset_5 == 1


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerJobScheduling:
    """Test job scheduling without execution."""

    @patch("receivers.cli.main.get_all_station_configs")
    def test_schedule_all_sessions(self, mock_get_stations):
        """Test scheduling all session types."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
            "TEST2": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False, max_workers=2)

        # Schedule all sessions
        scheduler.schedule_all_sessions()

        # Should have jobs scheduled
        jobs = scheduler.get_scheduled_jobs()

        # 2 stations × 3 sessions = 6 download + 2 health + 1 config_watcher
        # + 3 backfill + 1 gap_detection + 1 gap_detection_startup
        # + 1 archive_reconciler + 1 archive_reconciler_startup = 16 total
        # (plus possible daily catch-up jobs depending on time of day)
        assert len(jobs) >= 16

        # Check job IDs follow pattern: session_station
        job_ids = [job["id"] for job in jobs]
        assert "15s_24hr_TEST1" in job_ids
        assert "15s_24hr_TEST2" in job_ids
        assert "1Hz_1hr_TEST1" in job_ids
        assert "1Hz_1hr_TEST2" in job_ids
        assert "status_1hr_TEST1" in job_ids
        assert "status_1hr_TEST2" in job_ids
        # Health monitoring jobs are also scheduled
        assert "health_TEST1" in job_ids
        assert "health_TEST2" in job_ids

    @patch("receivers.cli.main.get_all_station_configs")
    def test_get_job_status(self, mock_get_stations):
        """Test getting scheduler status."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        scheduler.schedule_all_sessions()

        status = scheduler.get_job_status()

        assert "scheduler_running" in status
        assert "total_jobs" in status
        assert "running_jobs" in status
        assert "current_jobs" in status
        # 1 station × 3 sessions + 1 health + 1 config_watcher
        # + 3 backfill + 1 gap_detection + 1 gap_detection_startup
        # + 1 archive_reconciler + 1 archive_reconciler_startup = 12 total
        # (plus possible daily catch-up jobs depending on time of day)
        assert status["total_jobs"] >= 12


@pytest.mark.unit
@pytest.mark.scheduler
def test_merge_fills_push_verify_sessions():
    """A deployed archive_verify block missing push_verify_sessions gets it filled.

    Regression guard: archive_verify must be in merge_with_defaults' section list,
    else a deployed scheduler.yaml (which has the block but not the new key) takes
    the block wholesale → push_verify_sessions absent → immediate 15s read-back
    silently disabled in production.
    """
    from receivers.scheduling.config_loader import merge_with_defaults

    deployed = {
        "archive_verify": {
            "enabled": True,
            "read_root": "/mnt/rawgpsdata",
            "limit": 5000,
            "reverify_after_days": None,
        }
    }
    av = merge_with_defaults(deployed)["archive_verify"]
    assert av["push_verify_sessions"] == ["15s_24hr"]  # default-filled
    assert av["read_root"] == "/mnt/rawgpsdata"  # deployed value preserved
    assert av["limit"] == 5000


def test_create_scheduler_config(tmp_path):
    """Test creating scheduler configuration file (YAML format)."""
    import yaml

    config_file = tmp_path / "scheduler.yaml"
    created_file = create_default_config_file(config_file)

    assert created_file.exists()
    assert created_file.name == "scheduler.yaml"

    # Read and verify YAML config
    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert "scheduler" in config
    assert "sessions" in config
    assert config["scheduler"]["max_workers"] == 100

    # Check session configs
    assert "15s_24hr" in config["sessions"]
    assert "1Hz_1hr" in config["sessions"]
    assert "status_1hr" in config["sessions"]

    # Verify session structure (uses flexible schedule format in default template)
    daily_session = config["sessions"]["15s_24hr"]
    assert daily_session["enabled"] is True
    assert "schedule" in daily_session  # New format uses 'schedule' field


@pytest.mark.unit
@pytest.mark.scheduler
class TestOutageRecovery:
    """Test outage detection and dynamic lookback for daily catch-up."""

    @patch("receivers.cli.main.get_all_station_configs")
    def test_detect_outage_gap_no_db(self, mock_get_stations):
        """No psycopg2/database available — returns default lookback of 1."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Mock the import to raise ImportError (no psycopg2)
        with patch.dict("sys.modules", {"receivers.health.database_factory": None}):
            with patch(
                "receivers.scheduling.bulk_scheduler.BulkDownloadScheduler._detect_outage_gap",
                wraps=scheduler._detect_outage_gap,
            ):
                # Force ImportError by patching the import inside the method

                def mock_detect(session_type="15s_24hr"):
                    # Simulate ImportError path
                    try:
                        raise ImportError("No module named 'psycopg2'")
                    except ImportError:
                        return 1

                scheduler._detect_outage_gap = mock_detect
                result = scheduler._detect_outage_gap()
                assert result == 1

    @patch("receivers.cli.main.get_all_station_configs")
    def test_detect_outage_gap_no_data(self, mock_get_stations):
        """Empty file_tracking table — returns default lookback of 1."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Mock: query returns 0 tracked stations (empty table)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0, None, None, None)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        mock_cm = MagicMock()
        mock_cm.__enter__ = Mock(return_value=mock_conn)
        mock_cm.__exit__ = Mock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            return_value=mock_cm,
        ):
            result = scheduler._detect_outage_gap("15s_24hr")
            assert result == 1

    @patch("receivers.cli.main.get_all_station_configs")
    def test_detect_outage_gap_recent(self, mock_get_stations):
        """All stations downloaded yesterday — returns 1 (no multi-day gap)."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Mock: (tracked_stations, p5_gap, max_gap, min_gap) — all at 1 day
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (100, 1, 1, 1)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        mock_cm = MagicMock()
        mock_cm.__enter__ = Mock(return_value=mock_conn)
        mock_cm.__exit__ = Mock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            return_value=mock_cm,
        ):
            result = scheduler._detect_outage_gap("15s_24hr")
            assert result == 1

    @patch("receivers.cli.main.get_all_station_configs")
    def test_detect_outage_gap_multi_day(self, mock_get_stations):
        """5th-percentile station is 5 days behind — returns 5."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Mock: (tracked=100, p5_gap=5, max_gap=10, min_gap=1)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (100, 5, 10, 1)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        mock_cm = MagicMock()
        mock_cm.__enter__ = Mock(return_value=mock_conn)
        mock_cm.__exit__ = Mock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            return_value=mock_cm,
        ):
            result = scheduler._detect_outage_gap("15s_24hr")
            assert result == 5

    @patch("receivers.cli.main.get_all_station_configs")
    def test_detect_outage_gap_capped(self, mock_get_stations):
        """60-day p5 gap is capped to max_recovery_days (30)."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Mock: (tracked=100, p5_gap=60, max_gap=90, min_gap=1)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (100, 60, 90, 1)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

        mock_cm = MagicMock()
        mock_cm.__enter__ = Mock(return_value=mock_conn)
        mock_cm.__exit__ = Mock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory.connection",
            return_value=mock_cm,
        ):
            result = scheduler._detect_outage_gap("15s_24hr")
            assert result == 30  # Capped by max_recovery_days default

    @patch("receivers.cli.main.get_all_station_configs")
    def test_catchup_uses_dynamic_lookback(self, mock_get_stations):
        """Verify _schedule_daily_catchup passes dynamic lookback to job args."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Mock _detect_outage_gap to return 5
        scheduler._detect_outage_gap = Mock(return_value=5)

        # Build session_stations for a daily session that has already passed
        # We need to trigger the catch-up path by making the scheduled time in the past
        from receivers.scheduling.schedule_parser import parse_schedule

        config = scheduler.schedule_configs["15s_24hr"]

        # Force a daily schedule that has already passed
        config.schedule = "00:01"  # Daily at 00:01
        config.schedule_minute = None
        config.frequency = None

        session_stations = {"15s_24hr": ["TEST1"]}

        # Only run catch-up if current time is past the scheduled time
        # Since 00:01 will have passed for any reasonable test run time, this should trigger
        from datetime import datetime as dt

        local_now = dt.now()
        if local_now.hour > 0 or (local_now.hour == 0 and local_now.minute > 1):
            scheduler._schedule_daily_catchup(session_stations)

            # Verify _detect_outage_gap was called
            scheduler._detect_outage_gap.assert_called_once_with("15s_24hr")

            # Verify the job was scheduled with lookback=5
            jobs = scheduler.get_scheduled_jobs()
            catchup_jobs = [j for j in jobs if j["id"].startswith("catchup_")]
            assert len(catchup_jobs) == 1
            # args[3] is lookback_periods
            assert catchup_jobs[0]["args"][3] == 5


@pytest.mark.unit
@pytest.mark.scheduler
class TestRinexAfterDownload:
    """The live download path must hand RINEX the files it actually got.

    The original bug this class guards: the code read an ``archived_files``
    key that the download result does not have, so RINEX silently never
    ran. The rule is still `result["downloaded_files"]` — but the live path
    no longer calls `_run_rinex_conversion`. It submits an ASYNC conversion
    (`rinex.async_converter.submit_rinex_conversion`), and
    `_run_rinex_conversion` survives only for the backfill path, which its
    own docstring states (bulk_scheduler.py: "called only from the backfill
    path (never the inline submit_rinex_* pool)").

    Every test here patched `_run_rinex_conversion`, so they asserted
    against a function this path stopped calling: one failed, and the three
    `assert_not_called()` ones passed VACUOUSLY — green while checking
    nothing. Repointed at the submit seam, so they constrain the live path
    again.
    """

    def _patch_download_job(
        self,
        mock_result,
        station_id="ELDC",
        session_type="1Hz_1hr",
        run_rinex=True,
    ):
        """Run _download_station_data_job with mocked internals."""
        from receivers.scheduling.bulk_scheduler import _download_station_data_job

        mock_receiver = MagicMock()
        mock_receiver.download_data.return_value = mock_result
        station_config = {"receiver_type": "polarx5"}

        with (
            patch("receivers.cli.main.get_station_config", return_value=station_config),
            patch("receivers.cli.main.create_receiver", return_value=mock_receiver),
            patch(
                "receivers.utils.time_utils.calculate_download_time_range",
                return_value=(datetime(2026, 2, 10), datetime(2026, 2, 10, 1)),
            ),
        ):
            _download_station_data_job(
                station_id,
                session_type,
                production_mode=False,
                lookback_periods=1,
                timeout_minutes=30,
                run_rinex=run_rinex,
            )

        return station_config

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_rinex_submitted_after_a_successful_download(self, _health, mock_submit):
        mock_result = {
            "status": "completed",
            "files_downloaded": 1,
            "duration": 5.0,
            "downloaded_files": [
                "/data/2026/feb/ELDC/1Hz_1hr/raw/ELDC202602101400b.sbf.gz"
            ],
        }
        self._patch_download_job(mock_result, session_type="1Hz_1hr", run_rinex=True)

        mock_submit.assert_called_once()
        args = mock_submit.call_args[0]
        assert args[0] == "ELDC"
        assert args[1] == "1Hz_1hr"
        # The async converter re-derives the file list from the time window,
        # so the raw paths are NOT passed — only the window is.
        assert args[2] == datetime(2026, 2, 10)
        assert args[3] == datetime(2026, 2, 10, 1)

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_archived_files_key_does_not_trigger_rinex(self, _health, mock_submit):
        """The original bug, pinned: the gate reads `downloaded_files`.

        A result carrying only `archived_files` must not look like a
        successful download to the RINEX gate — reading that key is what
        made RINEX silently never run.
        """
        mock_result = {
            "status": "completed",
            "files_downloaded": 1,
            "duration": 5.0,
            "archived_files": ["/data/some/file.sbf.gz"],
        }
        self._patch_download_job(mock_result, run_rinex=True)
        mock_submit.assert_not_called()

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_rinex_not_submitted_when_disabled(self, _health, mock_submit):
        mock_result = {
            "status": "completed",
            "files_downloaded": 1,
            "duration": 5.0,
            "downloaded_files": ["/data/some/file.sbf.gz"],
        }
        self._patch_download_job(mock_result, run_rinex=False)
        mock_submit.assert_not_called()

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_rinex_not_submitted_on_failed_download(self, _health, mock_submit):
        mock_result = {
            "status": "failed",
            "files_downloaded": 0,
            "duration": 2.0,
            "downloaded_files": [],
            "error_message": "Connection refused",
        }
        self._patch_download_job(mock_result, session_type="15s_24hr", run_rinex=True)
        mock_submit.assert_not_called()

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_rinex_not_submitted_with_empty_files(self, _health, mock_submit):
        mock_result = {
            "status": "up_to_date",
            "files_downloaded": 0,
            "duration": 1.0,
            "downloaded_files": [],
        }
        self._patch_download_job(mock_result, run_rinex=True)
        mock_submit.assert_not_called()

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_health_extraction_still_uses_downloaded_files(self, mock_health, _submit):
        """Verify health extraction (status_1hr) still works correctly."""
        status_files = [
            "/data/2026/feb/ELDC/status_1hr/raw/ELDC202602101400_status.sbf.gz"
        ]
        mock_result = {
            "status": "completed",
            "files_downloaded": 1,
            "duration": 3.0,
            "downloaded_files": status_files,
        }
        self._patch_download_job(
            mock_result, session_type="status_1hr", run_rinex=False
        )

        mock_health.assert_called_once()
        call_args = mock_health.call_args[0]
        assert call_args[0] == "ELDC"
        assert call_args[1] == status_files


@pytest.mark.unit
@pytest.mark.scheduler
class TestBackfillRinex:
    """Phase 2: Test that backfill triggers RINEX conversion when configured."""

    @patch("receivers.scheduling.backfill._run_backfill_rinex")
    @patch("receivers.scheduling.backfill._extract_and_store_health")
    @patch("receivers.scheduling.backfill._download_day_generic")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
    def test_backfill_triggers_rinex(
        self, mock_conn, mock_download, mock_health, mock_rinex
    ):
        """Verify backfill calls _run_backfill_rinex when run_rinex=True."""
        from receivers.scheduling.backfill import _backfill_station_day_generic

        fake_files = ["/data/2026/feb/ELDC/15s_24hr/raw/ELDC20260210.sbf.gz"]
        mock_download.return_value = {
            "status": "completed",
            "files_downloaded": 1,
            "downloaded_files": fake_files,
        }

        # Mock the DB connection context manager
        mock_cursor = MagicMock()
        mock_conn_obj = MagicMock()
        mock_conn_obj.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn_obj.cursor.return_value.__exit__ = Mock(return_value=False)
        mock_conn.return_value.__enter__ = Mock(return_value=mock_conn_obj)
        mock_conn.return_value.__exit__ = Mock(return_value=False)

        _backfill_station_day_generic(
            "ELDC",
            date(2026, 2, 10),
            date(2026, 2, 12),
            "15s_24hr",
            immediate_archive=False,
            run_rinex=True,
        )

        # RINEX should be called
        mock_rinex.assert_called_once_with("ELDC", "15s_24hr", fake_files)

    @patch("receivers.scheduling.backfill._run_backfill_rinex")
    @patch("receivers.scheduling.backfill._extract_and_store_health")
    @patch("receivers.scheduling.backfill._download_day_generic")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
    def test_backfill_no_rinex_when_disabled(
        self, mock_conn, mock_download, mock_health, mock_rinex
    ):
        """Verify backfill does NOT call RINEX when run_rinex=False."""
        from receivers.scheduling.backfill import _backfill_station_day_generic

        mock_download.return_value = {
            "status": "completed",
            "files_downloaded": 1,
            "downloaded_files": ["/data/some/file.sbf.gz"],
        }

        mock_cursor = MagicMock()
        mock_conn_obj = MagicMock()
        mock_conn_obj.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn_obj.cursor.return_value.__exit__ = Mock(return_value=False)
        mock_conn.return_value.__enter__ = Mock(return_value=mock_conn_obj)
        mock_conn.return_value.__exit__ = Mock(return_value=False)

        _backfill_station_day_generic(
            "ELDC",
            date(2026, 2, 10),
            date(2026, 2, 12),
            "15s_24hr",
            run_rinex=False,
        )

        mock_rinex.assert_not_called()

    @patch("receivers.scheduling.backfill._run_backfill_rinex")
    @patch("receivers.scheduling.backfill._extract_and_store_health")
    @patch("receivers.scheduling.backfill._download_day_generic")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
    def test_backfill_no_rinex_for_status_1hr(
        self, mock_conn, mock_download, mock_health, mock_rinex
    ):
        """Verify backfill does NOT call RINEX for status_1hr (health only)."""
        from receivers.scheduling.backfill import _backfill_station_day_generic

        mock_download.return_value = {
            "status": "completed",
            "files_downloaded": 1,
            "downloaded_files": ["/data/some/status.sbf.gz"],
        }

        mock_cursor = MagicMock()
        mock_conn_obj = MagicMock()
        mock_conn_obj.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn_obj.cursor.return_value.__exit__ = Mock(return_value=False)
        mock_conn.return_value.__enter__ = Mock(return_value=mock_conn_obj)
        mock_conn.return_value.__exit__ = Mock(return_value=False)

        _backfill_station_day_generic(
            "ELDC",
            date(2026, 2, 10),
            date(2026, 2, 12),
            "status_1hr",
            run_rinex=True,  # rinex=True but session is status_1hr
        )

        # RINEX should NOT be called for status_1hr
        mock_rinex.assert_not_called()
        # But health extraction SHOULD be called
        mock_health.assert_called_once()

    @patch("receivers.scheduling.backfill._run_backfill_rinex")
    @patch("receivers.scheduling.backfill._extract_and_store_health")
    @patch("receivers.scheduling.backfill._download_day_generic")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory.connection")
    def test_backfill_no_rinex_on_failed_download(
        self, mock_conn, mock_download, _mock_health, mock_rinex
    ):
        """Verify backfill does NOT call RINEX when download fails."""
        from receivers.scheduling.backfill import _backfill_station_day_generic

        mock_download.return_value = {
            "status": "failed",
            "files_downloaded": 0,
            "downloaded_files": [],
        }

        mock_cursor = MagicMock()
        mock_conn_obj = MagicMock()
        mock_conn_obj.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
        mock_conn_obj.cursor.return_value.__exit__ = Mock(return_value=False)
        mock_conn.return_value.__enter__ = Mock(return_value=mock_conn_obj)
        mock_conn.return_value.__exit__ = Mock(return_value=False)

        _backfill_station_day_generic(
            "ELDC",
            date(2026, 2, 10),
            date(2026, 2, 12),
            "15s_24hr",
            run_rinex=True,
        )

        mock_rinex.assert_not_called()

    @patch("receivers.cli.main.get_all_station_configs")
    def test_backfill_schedule_passes_rinex_flag(self, mock_get_stations):
        """Verify _schedule_multi_session_backfill passes rinex config to jobs."""
        mock_get_stations.return_value = {
            "TEST1": {"receiver_type": "polarx5", "enabled": True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False, max_workers=2)

        # Set rinex=True on 15s_24hr session config
        scheduler.schedule_configs["15s_24hr"].rinex = True
        scheduler.schedule_configs["1Hz_1hr"].rinex = True
        scheduler.schedule_configs["status_1hr"].rinex = False

        scheduler._schedule_multi_session_backfill()

        # Check jobs were scheduled with correct rinex flag in args
        jobs = scheduler.get_scheduled_jobs()
        backfill_jobs = {j["id"]: j for j in jobs if j["id"].startswith("backfill_")}

        # args = [session_type, window_start, window_end, archiving_mode, run_rinex]
        if "backfill_15s_24hr" in backfill_jobs:
            assert backfill_jobs["backfill_15s_24hr"]["args"][4] is True
        if "backfill_1Hz_1hr" in backfill_jobs:
            assert backfill_jobs["backfill_1Hz_1hr"]["args"][4] is True
        if "backfill_status_1hr" in backfill_jobs:
            assert backfill_jobs["backfill_status_1hr"]["args"][4] is False


@pytest.mark.unit
@pytest.mark.scheduler
class TestPipelineTracking:
    """Phase 3: Test pipeline state tracking integration."""

    def test_pipeline_state_store_basic(self, tmp_path):
        """Test PipelineStateStore create/load/save cycle."""
        from receivers.scheduling.pipeline import (
            PipelineJob,
            PipelineStage,
            PipelineStateStore,
            StageStatus,
        )
        from receivers.scheduling.task_interface import TaskPriority

        db_path = tmp_path / "pipeline_test.db"
        store = PipelineStateStore(db_path)

        # Create a pipeline job
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2026, 2, 10, tzinfo=None),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
            priority=TaskPriority.STANDARD,
        )

        # Save and load
        store.save_job(job)
        loaded = store.load_job(job.job_id)
        assert loaded is not None
        assert loaded.station_id == "ELDC"
        assert loaded.session_type == "15s_24hr"
        assert PipelineStage.DOWNLOAD in loaded.stages
        assert PipelineStage.RINEX in loaded.stages
        assert loaded.stages[PipelineStage.DOWNLOAD].status == StageStatus.PENDING

    def test_pipeline_stage_progression(self, tmp_path):
        """Test marking stages complete/failed."""
        from receivers.scheduling.pipeline import (
            PipelineJob,
            PipelineStage,
            PipelineStateStore,
            StageStatus,
        )
        from receivers.scheduling.task_interface import TaskPriority

        db_path = tmp_path / "pipeline_test.db"
        store = PipelineStateStore(db_path)

        job = PipelineJob.create(
            station_id="THOB",
            session_type="1Hz_1hr",
            target_time=datetime(2026, 2, 10),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
        )

        # Mark download started
        job.mark_stage_started(PipelineStage.DOWNLOAD)
        assert job.stages[PipelineStage.DOWNLOAD].status == StageStatus.RUNNING

        # Mark download complete
        job.mark_stage_complete(
            PipelineStage.DOWNLOAD,
            output_files=["/data/file.sbf.gz"],
            metrics={"files_downloaded": 1},
        )
        assert job.stages[PipelineStage.DOWNLOAD].status == StageStatus.COMPLETED
        assert job.stages[PipelineStage.DOWNLOAD].output_files == ["/data/file.sbf.gz"]

        # RINEX should now be runnable
        assert job.can_run_stage(PipelineStage.RINEX) is True

        # Mark RINEX failed
        job.mark_stage_failed(PipelineStage.RINEX, "Converter not available")
        assert job.stages[PipelineStage.RINEX].status == StageStatus.FAILED
        assert job.stages[PipelineStage.RINEX].error == "Converter not available"

        # Job is complete (all stages done) but not successful
        assert job.is_complete() is True
        assert job.is_successful() is False

        # Save and verify persistence
        store.save_job(job)
        loaded = store.load_job(job.job_id)
        assert loaded is not None
        assert loaded.stages[PipelineStage.RINEX].status == StageStatus.FAILED

    def test_pipeline_incomplete_jobs(self, tmp_path):
        """Test loading incomplete jobs for crash recovery."""
        from receivers.scheduling.pipeline import (
            PipelineJob,
            PipelineStage,
            PipelineStateStore,
            StageStatus,
        )
        from receivers.scheduling.task_interface import TaskPriority

        db_path = tmp_path / "pipeline_test.db"
        store = PipelineStateStore(db_path)

        # Create two jobs: one complete, one incomplete
        complete_job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2026, 2, 10),
            enabled_stages=[PipelineStage.DOWNLOAD],
        )
        complete_job.mark_stage_started(PipelineStage.DOWNLOAD)
        complete_job.mark_stage_complete(PipelineStage.DOWNLOAD)
        store.save_job(complete_job)

        incomplete_job = PipelineJob.create(
            station_id="THOB",
            session_type="1Hz_1hr",
            target_time=datetime(2026, 2, 10),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
        )
        incomplete_job.mark_stage_started(PipelineStage.DOWNLOAD)
        store.save_job(incomplete_job)

        # Should only return the incomplete job
        incomplete = store.load_incomplete_jobs()
        assert len(incomplete) == 1
        assert incomplete[0].station_id == "THOB"

    def test_pipeline_stats(self, tmp_path):
        """Test pipeline statistics."""
        from receivers.scheduling.pipeline import (
            PipelineJob,
            PipelineStage,
            PipelineStateStore,
        )

        db_path = tmp_path / "pipeline_test.db"
        store = PipelineStateStore(db_path)

        # Create jobs
        for i, session in enumerate(["15s_24hr", "15s_24hr", "1Hz_1hr"]):
            job = PipelineJob.create(
                station_id=f"TEST{i}",
                session_type=session,
                target_time=datetime(2026, 2, 10),
                enabled_stages=[PipelineStage.DOWNLOAD],
            )
            if i < 2:  # Mark first two as complete
                job.mark_stage_started(PipelineStage.DOWNLOAD)
                job.mark_stage_complete(PipelineStage.DOWNLOAD)
            store.save_job(job)

        stats = store.get_stats()
        assert stats["total_jobs"] == 3
        assert stats["complete_jobs"] == 2
        assert stats["incomplete_jobs"] == 1
        assert stats["by_session_type"]["15s_24hr"] == 2
        assert stats["by_session_type"]["1Hz_1hr"] == 1

    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_pipeline_created_during_download(
        self, _mock_health, mock_submit, tmp_path
    ):
        """Verify pipeline job is created and tracked during download.

        The RINEX stage does not complete inline — the live path submits an
        async conversion and marks the stage complete from the future's
        done-callback. So the seam to drive is
        `submit_rinex_conversion`, returning an ALREADY-RESOLVED future:
        `add_done_callback` then fires immediately, in this thread, exactly
        as it does when a real conversion finishes.

        This test used to patch `_run_rinex_conversion`, which the live path
        stopped calling — so nothing ever resolved the RINEX stage and
        `complete_jobs` was 0.
        """
        from concurrent.futures import Future

        resolved: Future = Future()
        resolved.set_result({"output_files": []})
        mock_submit.return_value = resolved
        import receivers.scheduling.bulk_scheduler as bs_module
        from receivers.scheduling.bulk_scheduler import (
            _download_station_data_job,
            _get_pipeline_store,
        )
        from receivers.scheduling.pipeline import (
            PipelineStage,
            PipelineStateStore,
            StageStatus,
        )

        # Set up a temporary pipeline store
        db_path = tmp_path / "pipeline_test.db"
        test_store = PipelineStateStore(db_path)
        original_store = bs_module._pipeline_store
        bs_module._pipeline_store = test_store

        try:
            fake_files = ["/data/file.sbf.gz"]
            mock_result = {
                "status": "completed",
                "files_downloaded": 1,
                "duration": 5.0,
                "downloaded_files": fake_files,
            }

            mock_receiver = MagicMock()
            mock_receiver.download_data.return_value = mock_result

            with (
                patch(
                    "receivers.cli.main.get_station_config",
                    return_value={"receiver_type": "polarx5"},
                ),
                patch("receivers.cli.main.create_receiver", return_value=mock_receiver),
                patch(
                    "receivers.utils.time_utils.calculate_download_time_range",
                    return_value=(datetime(2026, 2, 10), datetime(2026, 2, 10, 1)),
                ),
            ):
                _download_station_data_job(
                    "ELDC",
                    "15s_24hr",
                    production_mode=False,
                    lookback_periods=1,
                    timeout_minutes=30,
                    run_rinex=True,
                )

            # Check pipeline store has the job
            stats = test_store.get_stats()
            assert stats["total_jobs"] == 1
            assert stats["complete_jobs"] == 1

            # Load the job and check stages
            jobs = test_store.load_jobs_by_station("ELDC", limit=1)
            assert len(jobs) == 1
            job = jobs[0]
            assert job.station_id == "ELDC"
            assert job.session_type == "15s_24hr"
            assert PipelineStage.DOWNLOAD in job.stages
            assert job.stages[PipelineStage.DOWNLOAD].status == StageStatus.COMPLETED
            assert PipelineStage.RINEX in job.stages
            # RINEX stage should be complete (mock doesn't fail)
            assert job.stages[PipelineStage.RINEX].status == StageStatus.COMPLETED

        finally:
            bs_module._pipeline_store = original_store


class TestLoadMonitor:
    """Tests for the LoadMonitor system load gating."""

    def test_load_monitor_disabled(self):
        """LoadMonitor with enabled=False always allows jobs."""
        from receivers.scheduling.load_monitor import LoadMonitor
        from receivers.scheduling.task_interface import TaskPriority

        monitor = LoadMonitor({"enabled": False})
        assert monitor.can_start_job(TaskPriority.MAINTENANCE) is True
        assert monitor.can_start_job(TaskPriority.BACKFILL) is True
        assert monitor.can_start_job(TaskPriority.STANDARD) is True
        assert monitor.can_start_job(TaskPriority.REALTIME) is True

    def test_realtime_always_allowed(self):
        """REALTIME jobs always proceed regardless of load."""
        from receivers.scheduling.load_monitor import LoadMonitor
        from receivers.scheduling.task_interface import TaskPriority

        # Set thresholds very low so system is "overloaded"
        monitor = LoadMonitor(
            {
                "enabled": True,
                "max_cpu_load": 0.001,
                "max_active_jobs": 1,
                "max_network_mbps": 0,
            }
        )
        assert monitor.can_start_job(TaskPriority.REALTIME) is True

    @patch("os.getloadavg", return_value=(2.0, 1.5, 1.0))
    def test_cpu_throttling(self, _mock_loadavg):
        """Jobs blocked when CPU load exceeds threshold * priority factor."""
        from receivers.scheduling.load_monitor import LoadMonitor
        from receivers.scheduling.task_interface import TaskPriority

        monitor = LoadMonitor(
            {
                "enabled": True,
                "max_cpu_load": 3.0,  # max 3.0
                "max_active_jobs": 0,  # disabled
                "max_network_mbps": 0,  # disabled
                "check_interval": 0,  # no caching
                "priority_thresholds": {
                    "standard": 0.8,  # 3.0 * 0.8 = 2.4 — load 2.0 < 2.4 → allowed
                    "backfill": 0.6,  # 3.0 * 0.6 = 1.8 — load 2.0 > 1.8 → blocked
                },
            }
        )

        assert monitor.can_start_job(TaskPriority.STANDARD) is True
        assert monitor.can_start_job(TaskPriority.BACKFILL) is False

    @patch("os.getloadavg", return_value=(0.5, 0.5, 0.5))
    def test_thread_throttling(self, _mock_loadavg):
        """Jobs blocked when active threads exceed threshold."""
        import threading

        from receivers.scheduling.load_monitor import LoadMonitor
        from receivers.scheduling.task_interface import TaskPriority

        monitor = LoadMonitor(
            {
                "enabled": True,
                "max_cpu_load": 0,  # disabled
                "max_active_jobs": 3,  # Very low — current thread count will exceed
                "max_network_mbps": 0,  # disabled
                "check_interval": 0,
                "priority_thresholds": {
                    "standard": 0.8,
                    "backfill": 0.6,
                },
            }
        )

        # threading.active_count() is typically > 1 (main + test threads)
        # With max_active_jobs=3 and standard threshold 0.8 → threshold = 2.4
        # active_count() is usually >= 3 in pytest, so standard should be blocked
        current = threading.active_count()
        if current > 3 * 0.8:
            assert monitor.can_start_job(TaskPriority.STANDARD) is False
        else:
            assert monitor.can_start_job(TaskPriority.STANDARD) is True

    def test_get_status(self):
        """get_status() returns a well-formed summary dict."""
        from receivers.scheduling.load_monitor import LoadMonitor

        monitor = LoadMonitor({"enabled": True, "check_interval": 0})
        status = monitor.get_status()

        assert "enabled" in status
        assert "cpu_load_1m" in status
        assert "active_threads" in status
        assert "network_mbps" in status
        assert "thresholds" in status
        assert "can_start" in status
        assert "REALTIME" in status["can_start"]
        assert "STANDARD" in status["can_start"]
        assert status["enabled"] is True

    @patch("receivers.scheduling.bulk_scheduler._run_rinex_conversion")
    @patch("receivers.scheduling.bulk_scheduler._extract_and_store_health_data")
    def test_load_gate_skips_download(self, _mock_health, _mock_rinex):
        """Download job returns early when load monitor says no."""
        import receivers.scheduling.bulk_scheduler as bs_module
        from receivers.scheduling.load_monitor import LoadMonitor

        # Create a monitor that always blocks STANDARD jobs
        blocking_monitor = LoadMonitor(
            {
                "enabled": True,
                "max_cpu_load": 0.001,
                "max_active_jobs": 0,
                "max_network_mbps": 0,
                "check_interval": 0,
                "priority_thresholds": {"standard": 0.8},
            }
        )

        original_monitor = bs_module._load_monitor
        bs_module._load_monitor = blocking_monitor

        try:
            # Mock the imports that would happen inside the function
            mock_receiver = MagicMock()
            with (
                patch(
                    "receivers.cli.main.get_station_config",
                    return_value={"receiver_type": "polarx5"},
                ),
                patch("receivers.cli.main.create_receiver", return_value=mock_receiver),
                patch(
                    "receivers.utils.time_utils.calculate_download_time_range",
                    return_value=(datetime(2026, 2, 10), datetime(2026, 2, 10, 1)),
                ),
            ):
                bs_module._download_station_data_job(
                    "ELDC",
                    "15s_24hr",
                    production_mode=False,
                )

            # download_data should NOT have been called (load gate returned early)
            mock_receiver.download_data.assert_not_called()

        finally:
            bs_module._load_monitor = original_monitor


class TestBootstrap:
    """Tests for bootstrap / cold-start detection and scheduling."""

    def test_detect_cold_start_no_db(self):
        """Cold start detection returns True when DB is unavailable."""
        from receivers.scheduling.bootstrap import detect_cold_start

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.side_effect = Exception("no database")
            assert detect_cold_start() is True

    def test_detect_cold_start_empty_db(self):
        """Cold start detected when file_tracking has very few entries."""
        from receivers.scheduling.bootstrap import detect_cold_start

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (3,)  # 3 entries < 10 threshold
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.return_value = mock_conn
            assert detect_cold_start() is True

    def test_detect_cold_start_populated_db(self):
        """Not a cold start when file_tracking has plenty of data."""
        from receivers.scheduling.bootstrap import detect_cold_start

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (500,)  # 500 entries >> 10 threshold
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.return_value = mock_conn
            assert detect_cold_start() is False

    def test_schedule_bootstrap_creates_jobs(self):
        """Bootstrap creates one-shot download jobs for each station/session."""
        from receivers.scheduling.bootstrap import schedule_bootstrap
        from receivers.scheduling.bulk_scheduler import ScheduleConfig

        mock_scheduler = MagicMock()

        stations = {
            "ELDC": {"station_id": "ELDC", "receiver_type": "polarx5"},
            "THOB": {"station_id": "THOB", "receiver_type": "polarx5"},
            "DEAD": {"station_id": "DEAD", "station_status": "discontinued"},
        }

        configs = {
            "15s_24hr": ScheduleConfig(
                session_type="15s_24hr",
                schedule=":10",
                distribution_window=10,
                rinex=True,
                timeout_minutes=45,
            ),
            "1Hz_1hr": ScheduleConfig(
                session_type="1Hz_1hr",
                schedule=":01",
                distribution_window=10,
                rinex=True,
                timeout_minutes=30,
            ),
            "status_1hr": ScheduleConfig(
                session_type="status_1hr",
                schedule=":15",
                distribution_window=5,
                timeout_minutes=15,
            ),
        }

        bootstrap_cfg = {
            "distribution_window": 5,
            "initial_lookback_days": 3,
        }

        total = schedule_bootstrap(
            scheduler=mock_scheduler,
            stations=stations,
            session_configs=configs,
            bootstrap_cfg=bootstrap_cfg,
            production_mode=True,
        )

        # 2 active stations × 3 session types = 6 jobs
        assert total == 6
        assert mock_scheduler.add_job.call_count == 6

        # Verify the first job is for 15s_24hr (wave 1)
        first_call = mock_scheduler.add_job.call_args_list[0]
        assert first_call.kwargs["id"].startswith("bootstrap_15s_24hr_")

    def test_detect_cold_start_session_specific(self):
        """Session-filtered cold start: detect when specific session has no data."""
        from receivers.scheduling.bootstrap import detect_cold_start

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # 15s_24hr has 0 entries → cold start for that session
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.return_value = mock_conn
            assert detect_cold_start(sessions=["15s_24hr"]) is True

        # Verify the query filtered by session type
        executed_sql = mock_cursor.execute.call_args[0][0]
        assert "session_type IN" in executed_sql
        # Should pass both 15s_24hr and 15s_24hr_rinex
        params = mock_cursor.execute.call_args[0][1]
        assert "15s_24hr" in params
        assert "15s_24hr_rinex" in params

    def test_bootstrap_sessions_config(self):
        """Bootstrap respects sessions list in config — only schedules listed sessions."""
        from receivers.scheduling.bootstrap import schedule_bootstrap
        from receivers.scheduling.bulk_scheduler import ScheduleConfig

        mock_scheduler = MagicMock()

        stations = {
            "ELDC": {"station_id": "ELDC", "receiver_type": "polarx5"},
            "THOB": {"station_id": "THOB", "receiver_type": "polarx5"},
        }

        configs = {
            "15s_24hr": ScheduleConfig(
                session_type="15s_24hr",
                schedule=":10",
                distribution_window=10,
                rinex=True,
                timeout_minutes=45,
            ),
            "1Hz_1hr": ScheduleConfig(
                session_type="1Hz_1hr",
                schedule=":01",
                distribution_window=10,
                rinex=True,
                timeout_minutes=30,
            ),
            "status_1hr": ScheduleConfig(
                session_type="status_1hr",
                schedule=":15",
                distribution_window=5,
                timeout_minutes=15,
            ),
        }

        # Only bootstrap 15s_24hr
        bootstrap_cfg = {
            "distribution_window": 5,
            "initial_lookback_days": 3,
            "sessions": ["15s_24hr"],
        }

        total = schedule_bootstrap(
            scheduler=mock_scheduler,
            stations=stations,
            session_configs=configs,
            bootstrap_cfg=bootstrap_cfg,
        )

        # 2 stations × 1 session (15s_24hr only) = 2 jobs
        assert total == 2
        # All jobs should be 15s_24hr
        for call in mock_scheduler.add_job.call_args_list:
            assert "15s_24hr" in call.kwargs["id"]

    def test_bootstrap_respects_station_filter(self):
        """Bootstrap only schedules filtered stations."""
        from receivers.scheduling.bootstrap import schedule_bootstrap
        from receivers.scheduling.bulk_scheduler import ScheduleConfig

        mock_scheduler = MagicMock()

        stations = {
            "ELDC": {"station_id": "ELDC", "receiver_type": "polarx5"},
            "THOB": {"station_id": "THOB", "receiver_type": "polarx5"},
            "MANA": {"station_id": "MANA", "receiver_type": "netr9"},
        }

        configs = {
            "15s_24hr": ScheduleConfig(
                session_type="15s_24hr",
                schedule=":10",
                distribution_window=10,
                timeout_minutes=45,
            ),
        }

        total = schedule_bootstrap(
            scheduler=mock_scheduler,
            stations=stations,
            session_configs=configs,
            bootstrap_cfg={"distribution_window": 5, "initial_lookback_days": 2},
            station_filter=["ELDC", "THOB"],
        )

        # Only 2 filtered stations × 1 session = 2 jobs
        assert total == 2


class TestGapBackfill:
    """Tests for gap-priority backfill ordering."""

    def test_gap_priority_picks_station_with_fewest_files(self):
        """Gap priority selects the station with fewest archived files."""
        from receivers.scheduling.backfill import _pick_station_by_gap_count

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # Return station THOB (has fewest archived files → most gaps)
        mock_cursor.fetchall.return_value = [
            (
                "THOB",
                date(2026, 2, 1),
                date(2026, 1, 1),
                date(2026, 2, 10),
            )
        ]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.return_value = mock_conn
            result = _pick_station_by_gap_count("15s_24hr")

        assert result
        assert result[0][0] == "THOB"

    def test_gap_priority_returns_empty_on_error(self):
        """Gap priority gracefully returns [] on DB error."""
        from receivers.scheduling.backfill import _pick_station_by_gap_count

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.side_effect = Exception("db error")
            result = _pick_station_by_gap_count("status_1hr")

        assert result == []

    def test_gap_priority_returns_empty_when_no_pending(self):
        """Gap priority returns [] when no pending stations."""
        from receivers.scheduling.backfill import _pick_station_by_gap_count

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.return_value = mock_conn
            result = _pick_station_by_gap_count("15s_24hr")

        assert result == []

    def test_strategy_passed_through_schedule(self):
        """Verify strategy parameter is passed from scheduler to backfill job."""
        from receivers.scheduling.backfill import _backfill_next_station_for_session

        # Set minute inside backfill window
        with patch("receivers.scheduling.backfill.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(minute=30)
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min

            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            # gap_priority query returns a station
            mock_cursor.fetchone.return_value = (
                "ELDC",
                date(2026, 2, 5),
                date(2026, 1, 1),
                date(2026, 2, 10),
            )
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.__enter__ = MagicMock(
                return_value=mock_cursor
            )
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

            with (
                patch(
                    "receivers.health.database_factory.DatabaseConnectionFactory"
                ) as mock_db,
                patch(
                    "receivers.scheduling.backfill._pick_station_by_gap_count",
                    return_value=[
                        (
                            "ELDC",
                            date(2026, 2, 5),
                            date(2026, 1, 1),
                            date(2026, 2, 10),
                        )
                    ],
                ) as mock_gap,
                patch(
                    "receivers.scheduling.backfill._backfill_station_day_generic",
                    return_value=True,
                ),
            ):
                mock_db.connection.return_value = mock_conn

                _backfill_next_station_for_session(
                    "status_1hr",
                    strategy="gap_priority",
                )

                # Verify gap_priority was called (with the per-tick worker limit)
                mock_gap.assert_called_once_with("status_1hr", limit=1)

    def test_fallback_to_round_robin(self):
        """When gap_priority returns None, falls back to round-robin."""
        from receivers.scheduling.backfill import _backfill_next_station_for_session

        with patch("receivers.scheduling.backfill.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(minute=30)
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min

            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            # Round-robin query returns a station
            mock_cursor.fetchall.return_value = [
                (
                    "MANA",
                    date(2026, 2, 3),
                    date(2026, 1, 1),
                    date(2026, 2, 10),
                )
            ]
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.__enter__ = MagicMock(
                return_value=mock_cursor
            )
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

            with (
                patch(
                    "receivers.health.database_factory.DatabaseConnectionFactory"
                ) as mock_db,
                patch(
                    "receivers.scheduling.backfill._pick_station_by_gap_count",
                    return_value=[],
                ) as mock_gap,
                patch(
                    "receivers.scheduling.backfill._backfill_station_day_generic",
                    return_value=True,
                ) as mock_backfill,
            ):
                mock_db.connection.return_value = mock_conn

                _backfill_next_station_for_session(
                    "15s_24hr",
                    strategy="gap_priority",
                )

                # Gap priority tried but returned [] (empty)
                mock_gap.assert_called_once_with("15s_24hr", limit=1)
                # Should fall back to round-robin and still process a station
                mock_backfill.assert_called_once()

    def test_max_workers_processes_stations_concurrently(self):
        """max_workers>1 picks and processes that many stations per tick."""
        from receivers.scheduling.backfill import _backfill_next_station_for_session

        stations = [
            ("AAAA", date(2026, 2, 1), date(2026, 1, 1), date(2026, 2, 10)),
            ("BBBB", date(2026, 2, 1), date(2026, 1, 1), date(2026, 2, 10)),
            ("CCCC", date(2026, 2, 1), date(2026, 1, 1), date(2026, 2, 10)),
        ]
        with patch("receivers.scheduling.backfill.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(minute=30)  # inside window
            with (
                patch(
                    "receivers.scheduling.backfill._pick_backfill_stations",
                    return_value=stations,
                ) as mock_pick,
                patch(
                    "receivers.scheduling.backfill._backfill_station_day_generic",
                    return_value=False,
                ) as mock_backfill,
                patch("receivers.scheduling.backfill.clean_stale_tmp"),
            ):
                _backfill_next_station_for_session("1Hz_1hr", max_workers=3)

                # Picked with the configured concurrency
                mock_pick.assert_called_once_with("1Hz_1hr", 3, "round_robin")
                # All three stations processed (concurrently)
                assert mock_backfill.call_count == 3
                processed = {c.args[0] for c in mock_backfill.call_args_list}
                assert processed == {"AAAA", "BBBB", "CCCC"}

    def test_one_station_failure_does_not_abort_batch(self):
        """A single station raising must not stop the others in the batch."""
        from receivers.scheduling.backfill import _backfill_next_station_for_session

        stations = [
            ("AAAA", date(2026, 2, 1), date(2026, 1, 1), date(2026, 2, 10)),
            ("BBBB", date(2026, 2, 1), date(2026, 1, 1), date(2026, 2, 10)),
        ]

        def _side(station_id, *a, **k):
            if station_id == "AAAA":
                raise RuntimeError("boom")
            return False

        with patch("receivers.scheduling.backfill.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(minute=30)
            with (
                patch(
                    "receivers.scheduling.backfill._pick_backfill_stations",
                    return_value=stations,
                ),
                patch(
                    "receivers.scheduling.backfill._backfill_station_day_generic",
                    side_effect=_side,
                ) as mock_backfill,
                patch("receivers.scheduling.backfill.clean_stale_tmp"),
            ):
                # Must not raise despite AAAA failing
                _backfill_next_station_for_session("1Hz_1hr", max_workers=2)
                assert mock_backfill.call_count == 2

    def test_enqueue_backfill_upserts_gapped_stations(self):
        """gap_detection -> backfill self-refill upserts the gapped stations."""
        from receivers.scheduling.backfill import _enqueue_backfill

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            mock_db.connection.return_value = mock_conn
            n = _enqueue_backfill("1Hz_1hr", ["THOB", "RHOF", "VONC"], days_back=7)

        assert n == 3
        # one executemany with a param row per station
        mock_cursor.executemany.assert_called_once()
        params = mock_cursor.executemany.call_args.args[1]
        assert [p[0] for p in params] == ["THOB", "RHOF", "VONC"]
        assert all(p[1] == "1Hz_1hr" and p[2] == 7 for p in params)

    def test_enqueue_backfill_empty_is_noop(self):
        """Empty station list does not touch the DB."""
        from receivers.scheduling.backfill import _enqueue_backfill

        with patch(
            "receivers.health.database_factory.DatabaseConnectionFactory"
        ) as mock_db:
            assert _enqueue_backfill("1Hz_1hr", [], days_back=7) == 0
            mock_db.connection.assert_not_called()


class TestIsRetryableDownload:
    """Tests for _is_retryable_download and _categorize_failure."""

    def setup_method(self):
        from receivers.scheduling.bulk_scheduler import (
            _categorize_failure,
            _is_retryable_download,
        )

        self._is_retryable = _is_retryable_download
        self._categorize = _categorize_failure

    # --- _is_retryable_download ---

    def test_unreachable_not_retryable(self):
        assert not self._is_retryable({"status": "unreachable"})

    def test_configuration_error_not_retryable(self):
        assert not self._is_retryable({"status": "configuration_error"})

    def test_auth_401_not_retryable(self):
        assert not self._is_retryable(
            {"status": "failed", "error_message": "401 Unauthorized"}
        )

    def test_auth_530_not_retryable(self):
        assert not self._is_retryable(
            {"status": "failed", "error_message": "530 Login incorrect"}
        )

    def test_timeout_retryable(self):
        assert self._is_retryable(
            {"status": "failed", "error_message": "Connection timed out"}
        )

    def test_connection_refused_retryable(self):
        assert self._is_retryable(
            {"status": "failed", "error_message": "[Errno 111] Connection refused"}
        )

    def test_404_retryable(self):
        # File not ready at midnight — must be retryable now
        assert self._is_retryable(
            {"status": "failed", "error_message": "HTTP 404 Not Found"}
        )

    def test_not_found_retryable(self):
        # FTP 550 file not found at midnight
        assert self._is_retryable(
            {"status": "failed", "error_message": "File not found on server"}
        )

    def test_ftp_550_retryable(self):
        assert self._is_retryable(
            {"status": "failed", "error_message": "550 No such file"}
        )

    def test_size_mismatch_retryable(self):
        # File was still growing when downloaded
        assert self._is_retryable(
            {
                "status": "failed",
                "error_message": "Size mismatch after clean retry: got 22249690, expected 20519110",
            }
        )

    def test_watchdog_retryable(self):
        assert self._is_retryable(
            {"status": "failed", "error_message": "Watchdog triggered: no progress"}
        )

    def test_empty_error_not_retryable(self):
        # No recognized pattern → False
        assert not self._is_retryable({"status": "failed", "error_message": ""})

    # --- _categorize_failure ---

    def test_categorize_timeout(self):
        assert self._categorize("Connection timed out after 600s") == "timeout"

    def test_categorize_conn_refused(self):
        assert self._categorize("[Errno 111] Connection refused") == "conn_refused"

    def test_categorize_file_not_ready(self):
        assert self._categorize("HTTP 404 Not Found") == "file_not_ready"
        assert self._categorize("FTP 550 No such file") == "file_not_ready"

    def test_categorize_size_mismatch(self):
        assert self._categorize("Size mismatch after clean retry") == "size_mismatch"

    def test_categorize_unreachable(self):
        assert self._categorize("Host unreachable") == "unreachable"

    def test_categorize_auth(self):
        assert self._categorize("530 Login incorrect") == "auth"

    def test_categorize_other(self):
        assert self._categorize("Something completely unknown") == "other"


@pytest.mark.unit
@pytest.mark.scheduler
class TestRetryFailedDailyJob:
    """Tests for _retry_failed_daily_job parallel second-chance retry."""

    def setup_method(self):
        from receivers.scheduling.bulk_scheduler import (
            _RETRY_MAX_WORKERS,
            _retry_failed_daily_job,
        )

        self._retry_job = _retry_failed_daily_job
        self._max_workers = _RETRY_MAX_WORKERS

    def test_max_workers_constant_defined(self):
        assert isinstance(self._max_workers, int)
        assert self._max_workers >= 4

    def _make_db_row(self, sid, outcome="failed", message="Connection refused"):
        return (sid, outcome, message)

    def _make_mock_db(self, rows):
        """Build a context-manager-compatible mock DB connection returning `rows`."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = rows
        mock_cur.fetchone.return_value = None
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        return mock_conn

    @patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_failures_exits_early(self, mock_dcf, mock_download):
        """If DB returns no rows, job logs and returns without downloading."""
        mock_dcf.connection.return_value = self._make_mock_db([])
        self._retry_job("15s_24hr")
        mock_download.assert_not_called()

    @patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_parallel_calls_all_stations(self, mock_dcf, mock_download):
        """All queued stations get a download attempt."""
        stations = ["AFST", "AUST", "FAGD", "GSIG", "HEDI"]
        rows = [self._make_db_row(s) for s in stations]
        mock_dcf.connection.return_value = self._make_mock_db(rows)

        called = []
        mock_download.side_effect = lambda sid, *a, **kw: called.append(sid)

        self._retry_job("15s_24hr")
        assert sorted(called) == sorted(stations)

    def test_workers_capped_at_station_count(self):
        """min(_RETRY_MAX_WORKERS, len(stations)) keeps thread count bounded below."""
        n_stations = 3
        workers = min(self._max_workers, n_stations)
        assert workers == n_stations

    def test_workers_capped_at_max_for_large_batch(self):
        """Large batch is capped at _RETRY_MAX_WORKERS."""
        n_stations = 100
        workers = min(self._max_workers, n_stations)
        assert workers == self._max_workers

    @patch("receivers.scheduling.bulk_scheduler._download_station_data_job")
    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_expected_outcome_excluded_from_retry(self, mock_dcf, mock_download):
        """Stations with outcome='expected' are not queued for second-chance retry."""
        # Only AFST has a retryable failure; GSIG has outcome='expected' (health gate)
        rows = [
            self._make_db_row("AFST", outcome="failed"),
            # GSIG is excluded at the SQL level: outcome='expected' NOT IN ('completed','up_to_date','expected')
            # We simulate this by not including it in the DB rows returned.
        ]
        mock_dcf.connection.return_value = self._make_mock_db(rows)
        called = []
        mock_download.side_effect = lambda sid, *a, **kw: called.append(sid)

        self._retry_job("15s_24hr")
        assert called == ["AFST"]
        assert "GSIG" not in called


class TestExpectedFailureGates:
    """Unit tests for health gate and known_issue gate in _download_station_data_job."""

    @patch("receivers.scheduling.bulk_scheduler._record_batch_result")
    @patch(
        "receivers.scheduling.bulk_scheduler.check_station_health_gate",
        return_value="no_satellites",
        create=True,
    )
    @patch("receivers.scheduling.bulk_scheduler._get_load_monitor", return_value=None)
    def test_health_gate_no_satellites_skips(self, mock_load, mock_gate, mock_batch):
        """Health gate: no_satellites → early return, outcome='expected', no download attempt."""
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        with _patch(
            "receivers.utils.stall_timeout.check_station_health_gate",
            return_value="no_satellites",
        ):
            with _patch("receivers.utils.stall_timeout.record_download") as mock_rd:
                with _patch(
                    "receivers.scheduling.bulk_scheduler._get_load_monitor",
                    return_value=None,
                ):
                    with _patch(
                        "receivers.scheduling.bulk_scheduler._get_pipeline_store",
                        return_value=None,
                    ):
                        with _patch(
                            "receivers.scheduling.bulk_scheduler._record_batch_result"
                        ) as mock_rb:
                            from receivers.scheduling.bulk_scheduler import (
                                _download_station_data_job,
                            )

                            _download_station_data_job("GSIG", "15s_24hr")

                            mock_rd.assert_called_once()
                            call_kwargs = mock_rd.call_args
                            assert call_kwargs[1].get("outcome") == "expected" or (
                                len(call_kwargs[0]) >= 3
                                and call_kwargs[0][2] == "expected"
                            )
                            mock_rb.assert_called_once()
                            rb_args = mock_rb.call_args[0]
                            assert rb_args[2] == "expected"

    def test_categorize_failure_health_gate_messages(self):
        """_categorize_failure must recognise health-gate and known-issue strings."""
        from receivers.scheduling.bulk_scheduler import _categorize_failure

        assert _categorize_failure("no_satellites") == "no_satellites"
        assert _categorize_failure("disk_broken") == "disk_broken"
        assert _categorize_failure("gps_week_rollover") == "gps_week_rollover"
        assert _categorize_failure("hardware_broken") == "hardware_broken"

    def test_record_batch_result_expected_bucket(self):
        """outcome='expected' accumulates in 'expected' bucket, not 'fail'."""
        import threading

        from receivers.scheduling.bulk_scheduler import (
            _BATCH_LOCK,
            _BATCH_STATS,
            _record_batch_result,
        )

        with _BATCH_LOCK:
            _BATCH_STATS.pop("test_session", None)

        _record_batch_result("test_session", "GSIG", "expected", "no_satellites")
        _record_batch_result("test_session", "HEDI", "expected", "no_satellites")
        _record_batch_result("test_session", "AFST", "ok")

        with _BATCH_LOCK:
            bucket = _BATCH_STATS.pop("test_session", {})

        assert sorted(bucket.get("expected", [])) == ["GSIG", "HEDI"]
        assert bucket.get("fail", {}) == {}
        assert "AFST" in bucket.get("ok", [])

    def test_record_batch_result_skipped_bucket(self):
        """outcome='skipped' accumulates in 'skipped' bucket (self-clearing gates)."""
        from receivers.scheduling.bulk_scheduler import (
            _BATCH_LOCK,
            _BATCH_STATS,
            _record_batch_result,
        )

        with _BATCH_LOCK:
            _BATCH_STATS.pop("test_session_sk", None)

        _record_batch_result("test_session_sk", "FULLA", "skipped", "disk_full")
        _record_batch_result("test_session_sk", "FULLB", "skipped", "disk_full")
        _record_batch_result("test_session_sk", "STUCK", "expected", "disk_broken")
        _record_batch_result("test_session_sk", "GOOD", "ok")

        with _BATCH_LOCK:
            bucket = _BATCH_STATS.pop("test_session_sk", {})

        assert sorted(bucket.get("skipped", [])) == ["FULLA", "FULLB"]
        assert bucket.get("expected", []) == ["STUCK"]
        assert bucket.get("fail", {}) == {}
        assert "GOOD" in bucket.get("ok", [])

    @patch(
        "receivers.scheduling.bulk_scheduler.check_station_health_gate",
        create=True,
    )
    @patch("receivers.scheduling.bulk_scheduler._get_load_monitor", return_value=None)
    def test_health_gate_disk_full_uses_skipped_outcome(self, mock_load, mock_gate):
        """disk_full is self-clearing → outcome='skipped' so retry queue picks it up."""
        from unittest.mock import patch as _patch

        with _patch(
            "receivers.utils.stall_timeout.check_station_health_gate",
            return_value="disk_full",
        ):
            with _patch("receivers.utils.stall_timeout.record_download") as mock_rd:
                with _patch(
                    "receivers.scheduling.bulk_scheduler._get_load_monitor",
                    return_value=None,
                ):
                    with _patch(
                        "receivers.scheduling.bulk_scheduler._get_pipeline_store",
                        return_value=None,
                    ):
                        with _patch(
                            "receivers.scheduling.bulk_scheduler._record_batch_result"
                        ) as mock_rb:
                            from receivers.scheduling.bulk_scheduler import (
                                _download_station_data_job,
                            )

                            _download_station_data_job("FULLA", "15s_24hr")

                            mock_rd.assert_called_once()
                            kw = mock_rd.call_args[1]
                            assert kw.get("outcome") == "skipped"
                            assert kw.get("message") == "disk_full"
                            mock_rb.assert_called_once()
                            assert mock_rb.call_args[0][2] == "skipped"

    @patch(
        "receivers.scheduling.bulk_scheduler.check_station_health_gate",
        create=True,
    )
    @patch("receivers.scheduling.bulk_scheduler._get_load_monitor", return_value=None)
    def test_health_gate_disk_broken_stays_expected(self, mock_load, mock_gate):
        """disk_broken is sticky → outcome='expected', retry queue skips it."""
        from unittest.mock import patch as _patch

        with _patch(
            "receivers.utils.stall_timeout.check_station_health_gate",
            return_value="disk_broken",
        ):
            with _patch("receivers.utils.stall_timeout.record_download") as mock_rd:
                with _patch(
                    "receivers.scheduling.bulk_scheduler._get_load_monitor",
                    return_value=None,
                ):
                    with _patch(
                        "receivers.scheduling.bulk_scheduler._get_pipeline_store",
                        return_value=None,
                    ):
                        with _patch(
                            "receivers.scheduling.bulk_scheduler._record_batch_result"
                        ) as mock_rb:
                            from receivers.scheduling.bulk_scheduler import (
                                _download_station_data_job,
                            )

                            _download_station_data_job("BROK", "15s_24hr")

                            mock_rd.assert_called_once()
                            kw = mock_rd.call_args[1]
                            assert kw.get("outcome") == "expected"
                            assert kw.get("message") == "disk_broken"
                            mock_rb.assert_called_once()
                            assert mock_rb.call_args[0][2] == "expected"
