"""Unit tests for fire-and-forget RINEX conversion.

Tests the async_converter module: submission, worker logic, pool management,
and integration with the download pipeline hooks.
"""

import logging
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from receivers.rinex.async_converter import (
    _create_converter,
    _find_raw_files,
    _on_conversion_done,
    _rinex_worker,
    shutdown_rinex_pool,
    submit_file_rinex,
    submit_rinex_conversion,
)


@pytest.fixture(autouse=True)
def _reset_executor():
    """Ensure the global executor is cleaned up between tests."""
    yield
    shutdown_rinex_pool(wait=False)


@pytest.mark.unit
class TestSubmitRinexConversion:
    """Test the submit_rinex_conversion entry point."""

    @patch("receivers.rinex.async_converter._get_executor")
    def test_submit_returns_future(self, mock_get_executor):
        """Submission returns a Future object."""
        mock_executor = MagicMock()
        mock_future = MagicMock()
        mock_executor.submit.return_value = mock_future
        mock_get_executor.return_value = mock_executor

        future = submit_rinex_conversion(
            "ELDC",
            "15s_24hr",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
        )

        assert future is mock_future
        mock_executor.submit.assert_called_once()
        # Verify done callback was attached
        mock_future.add_done_callback.assert_called_once()

    @patch("receivers.rinex.async_converter._get_executor")
    def test_submit_passes_correct_args(self, mock_get_executor):
        """Worker is called with correct station/session/dates."""
        mock_executor = MagicMock()
        mock_get_executor.return_value = mock_executor

        start = datetime(2026, 3, 1)
        end = datetime(2026, 3, 2)
        submit_rinex_conversion("THOB", "1Hz_1hr", start, end)

        args, kwargs = mock_executor.submit.call_args
        assert args[0] is _rinex_worker
        assert args[1] == "THOB"
        assert args[2] == "1Hz_1hr"
        assert args[3] == start
        assert args[4] == end

    @patch("receivers.rinex.async_converter._get_executor")
    def test_submit_failure_returns_none(self, mock_get_executor):
        """If executor.submit raises, return None instead of crashing."""
        mock_executor = MagicMock()
        mock_executor.submit.side_effect = RuntimeError("pool broken")
        mock_get_executor.return_value = mock_executor

        result = submit_rinex_conversion(
            "ELDC",
            "15s_24hr",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
        )

        assert result is None


@pytest.mark.unit
class TestBrokenPoolRecovery:
    """A killed worker breaks the ProcessPoolExecutor singleton for good.

    On rek-d01 (2026-08-09) an OOM-killed RINEX worker left the pool broken; every
    later ``submit_rinex_conversion`` silently failed until the scheduler was
    restarted at 21:22. These tests lock in the reset-and-retry self-heal.
    """

    @patch("receivers.rinex.async_converter._get_executor")
    def test_broken_pool_is_reset_and_submit_retried(self, mock_get_executor):
        """First submit raises BrokenProcessPool → reset + retry succeeds."""
        from concurrent.futures.process import BrokenProcessPool

        broken = MagicMock()
        broken.submit.side_effect = BrokenProcessPool("worker died")
        fresh = MagicMock()
        fresh_future = MagicMock()
        fresh.submit.return_value = fresh_future
        mock_get_executor.side_effect = [broken, fresh]

        with patch("receivers.rinex.async_converter._reset_executor") as mock_reset:
            future = submit_rinex_conversion(
                "ELDC", "15s_24hr",
                datetime(2026, 3, 1), datetime(2026, 3, 2),
            )

        assert future is fresh_future
        mock_reset.assert_called_once()  # broken pool torn down before retry
        assert broken.submit.call_count == 1
        assert fresh.submit.call_count == 1  # exactly one retry

    @patch("receivers.rinex.async_converter._get_executor")
    def test_broken_pool_gives_up_after_one_retry(self, mock_get_executor):
        """If the fresh pool is also broken, do not loop forever."""
        from concurrent.futures.process import BrokenProcessPool

        dead = MagicMock()
        dead.submit.side_effect = BrokenProcessPool("still dead")
        mock_get_executor.side_effect = [dead, dead]

        with patch("receivers.rinex.async_converter._reset_executor"):
            result = submit_rinex_conversion(
                "ELDC", "15s_24hr",
                datetime(2026, 3, 1), datetime(2026, 3, 2),
            )

        assert result is None
        assert dead.submit.call_count == 2  # one try + one retry, then stop

    @patch("receivers.rinex.async_converter._get_executor")
    def test_file_submit_also_recovers_from_broken_pool(self, mock_get_executor):
        """submit_file_rinex (the per-file inline callback) self-heals too."""
        from concurrent.futures.process import BrokenProcessPool

        broken = MagicMock()
        broken.submit.side_effect = BrokenProcessPool("worker died")
        fresh = MagicMock()
        fresh_future = MagicMock()
        fresh.submit.return_value = fresh_future
        mock_get_executor.side_effect = [broken, fresh]

        with patch("receivers.rinex.async_converter._reset_executor"):
            future = submit_file_rinex(
                "THOB", "1Hz_1hr", "/data/THOB202603011400b.sbf.gz"
            )

        assert future is fresh_future
        assert fresh.submit.call_count == 1

    def test_reset_executor_is_idempotent_and_locks(self):
        """Resetting a None executor is a no-op; resetting twice is safe."""
        import receivers.rinex.async_converter as mod

        mod._executor = None
        mod._reset_executor()  # no-op, must not raise
        mod._reset_executor()  # still safe

    def test_done_callback_resets_on_broken_pool(self, caplog):
        """A future that failed with BrokenProcessPool triggers a pool reset."""
        from concurrent.futures.process import BrokenProcessPool

        mock_future = MagicMock()
        mock_future.exception.return_value = BrokenProcessPool("worker died")

        with patch("receivers.rinex.async_converter._reset_executor") as mock_reset:
            with caplog.at_level(logging.ERROR, logger="receivers.rinex.async"):
                _on_conversion_done(mock_future, "ELDC", "15s_24hr")

        assert "RINEX failed: ELDC" in caplog.text
        mock_reset.assert_called_once()

    def test_done_callback_does_not_reset_on_other_errors(self):
        """A normal conversion error must NOT tear down the whole pool."""
        mock_future = MagicMock()
        mock_future.exception.return_value = RuntimeError("sbf2rin crashed")

        with patch("receivers.rinex.async_converter._reset_executor") as mock_reset:
            _on_conversion_done(mock_future, "ELDC", "15s_24hr")

        mock_reset.assert_not_called()


@pytest.mark.unit
class TestOnConversionDone:
    """Test the done callback that logs conversion results."""

    def test_success_logging(self, caplog):
        """Successful conversion is logged at INFO."""
        mock_future = MagicMock()
        mock_future.exception.return_value = None
        mock_future.result.return_value = {
            "converted": 3,
            "failed": 0,
            "duration": 12.5,
        }

        with caplog.at_level(logging.INFO, logger="receivers.rinex.async"):
            _on_conversion_done(mock_future, "ELDC", "15s_24hr")

        assert "RINEX done: ELDC" in caplog.text
        assert "3 file(s)" in caplog.text

    def test_failure_logging(self, caplog):
        """Failed conversion is logged at ERROR."""
        mock_future = MagicMock()
        mock_future.exception.return_value = RuntimeError("sbf2rin crashed")

        with caplog.at_level(logging.ERROR, logger="receivers.rinex.async"):
            _on_conversion_done(mock_future, "THOB", "15s_24hr")

        assert "RINEX failed: THOB" in caplog.text
        assert "sbf2rin crashed" in caplog.text

    def test_partial_failure_logging(self, caplog):
        """Partial conversion (some failed) is logged at WARNING."""
        mock_future = MagicMock()
        mock_future.exception.return_value = None
        mock_future.result.return_value = {
            "converted": 2,
            "failed": 1,
            "duration": 8.0,
        }

        with caplog.at_level(logging.WARNING, logger="receivers.rinex.async"):
            _on_conversion_done(mock_future, "ISFS", "15s_24hr")

        assert "RINEX partial: ISFS" in caplog.text

    def test_no_files_logging(self, caplog):
        """No raw files found is logged at DEBUG (not warning)."""
        mock_future = MagicMock()
        mock_future.exception.return_value = None
        mock_future.result.return_value = {
            "converted": 0,
            "failed": 0,
            "duration": 0.1,
        }

        with caplog.at_level(logging.DEBUG, logger="receivers.rinex.async"):
            _on_conversion_done(mock_future, "ELDC", "15s_24hr")

        assert "no raw files" in caplog.text


@pytest.mark.unit
class TestCreateConverter:
    """Test converter creation for different receiver types."""

    @patch("receivers.rinex.SBFConverter")
    def test_polarx5_creates_sbf_converter(self, mock_sbf):
        """PolaRX5 receivers get SBFConverter with .sbf.gz extension."""
        mock_sbf.return_value = MagicMock()
        converter, ext = _create_converter(
            "ELDC",
            "polarx5",
            {
                "default_version": 3,
                "default_naming": "short",
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        assert converter is not None
        assert ext == ".sbf.gz"
        mock_sbf.assert_called_once()

    @patch("receivers.rinex.TrimbleConverter")
    def test_netr9_creates_trimble_converter(self, mock_trimble):
        """NetR9 receivers get TrimbleConverter with .T02* extension."""
        mock_trimble.return_value = MagicMock()
        converter, ext = _create_converter(
            "MANA",
            "netr9",
            {
                "default_version": 3,
                "default_naming": "short",
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        assert converter is not None
        assert ext == ".T02*"

    @patch("receivers.rinex.TrimbleConverter")
    def test_netrs_creates_trimble_converter(self, mock_trimble):
        """NetRS receivers get TrimbleConverter with .T00* extension."""
        mock_trimble.return_value = MagicMock()
        converter, ext = _create_converter(
            "BLEI",
            "netrs",
            {
                "default_version": 3,
                "default_naming": "short",
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        assert converter is not None
        assert ext == ".T00*"

    @patch("receivers.rinex.TrimbleConverter")
    def test_netrs_pinned_to_rinex2_short(self, mock_trimble):
        """NetRS is pinned to RINEX 2.11 + SHORT naming even when the global
        default_version is 3 — its codeless L2 (C2D) has no P2 in RINEX 3, which
        GAMIT deletes. Bound to receiver type, not station."""
        from receivers.rinex import NamingConvention, RinexVersion

        mock_trimble.return_value = MagicMock()
        _create_converter(
            "BLEI",
            "netrs",
            {
                "default_version": 3,
                "default_naming": "short",
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        _, kwargs = mock_trimble.call_args
        assert kwargs["rinex_version"] == RinexVersion.RINEX_2
        assert kwargs["naming_convention"] == NamingConvention.SHORT

    @patch("receivers.rinex.trimble_native_converter.TrimbleNativeConverter")
    def test_netrs_uses_native_at_rinex2(self, mock_native):
        """When Docker is available, NetRS uses the NATIVE converter but at
        RINEX 2 (not 3). The teqc TrimbleConverter path produces no output for
        NetRS .T00, so native is required; RINEX 2 keeps the L2 range as P2."""
        from receivers.rinex import RinexVersion

        mock_native.is_available.return_value = True
        mock_native.return_value = MagicMock()
        _create_converter(
            "BLEI",
            "netrs",
            {
                "default_version": 3,
                "default_naming": "short",
                "use_native_trimble": True,
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        mock_native.assert_called_once()
        _, kwargs = mock_native.call_args
        assert kwargs["rinex_version"] == RinexVersion.RINEX_2

    @patch("receivers.rinex.TrimbleConverter")
    def test_netrs_version_override(self, mock_trimble):
        """An explicit netrs_rinex_version override is honoured (escape hatch)."""
        from receivers.rinex import RinexVersion

        mock_trimble.return_value = MagicMock()
        _create_converter(
            "BLEI",
            "netrs",
            {
                "default_version": 3,
                "default_naming": "short",
                "netrs_rinex_version": 3,
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        _, kwargs = mock_trimble.call_args
        assert kwargs["rinex_version"] == RinexVersion.RINEX_3

    @patch("receivers.rinex.LeicaConverter")
    def test_g10_creates_leica_converter(self, mock_leica):
        """G10 receivers get LeicaConverter with .m00.gz extension."""
        mock_leica.return_value = MagicMock()
        converter, ext = _create_converter(
            "SKFC",
            "g10",
            {
                "default_version": 3,
                "default_naming": "short",
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        assert converter is not None
        assert ext == ".m00.gz"

    def test_unknown_receiver_returns_none(self):
        """Unknown receiver type returns (None, None)."""
        converter, ext = _create_converter(
            "TEST",
            "unknown_rx",
            {
                "default_version": 3,
                "default_naming": "short",
                "apply_header_corrections": True,
            },
            MagicMock(),
        )
        assert converter is None
        assert ext is None


@pytest.mark.unit
class TestFindRawFiles:
    """Test raw file discovery by globbing archive directories."""

    def test_daily_session_globs_by_date(self, tmp_path):
        """15s_24hr session globs STATION+YYYYMMDD pattern."""
        # Create archive structure
        raw_dir = tmp_path / "2026" / "mar" / "ELDC" / "15s_24hr" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "ELDC20260301a.sbf.gz").touch()
        (raw_dir / "ELDC20260302a.sbf.gz").touch()

        files = _find_raw_files(
            "ELDC",
            "15s_24hr",
            ".sbf.gz",
            datetime(2026, 3, 1),
            datetime(2026, 3, 3),
            str(tmp_path),
        )

        assert len(files) == 2

    def test_hourly_session_globs_by_hour(self, tmp_path):
        """1Hz_1hr session globs STATION+YYYYMMDDHHMM pattern."""
        raw_dir = tmp_path / "2026" / "mar" / "THOB" / "1Hz_1hr" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "THOB202603011400b.sbf.gz").touch()
        (raw_dir / "THOB202603011500b.sbf.gz").touch()

        files = _find_raw_files(
            "THOB",
            "1Hz_1hr",
            ".sbf.gz",
            datetime(2026, 3, 1, 14, 0),
            datetime(2026, 3, 1, 16, 0),
            str(tmp_path),
        )

        assert len(files) == 2

    def test_no_files_returns_empty(self, tmp_path):
        """Missing directory returns empty list."""
        files = _find_raw_files(
            "FAKE",
            "15s_24hr",
            ".sbf.gz",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
            str(tmp_path),
        )
        assert files == []

    def test_files_are_sorted(self, tmp_path):
        """Returned files are sorted by name."""
        raw_dir = tmp_path / "2026" / "mar" / "ELDC" / "15s_24hr" / "raw"
        raw_dir.mkdir(parents=True)
        # Create in reverse order
        (raw_dir / "ELDC20260303a.sbf.gz").touch()
        (raw_dir / "ELDC20260301a.sbf.gz").touch()
        (raw_dir / "ELDC20260302a.sbf.gz").touch()

        files = _find_raw_files(
            "ELDC",
            "15s_24hr",
            ".sbf.gz",
            datetime(2026, 3, 1),
            datetime(2026, 3, 4),
            str(tmp_path),
        )

        assert len(files) == 3
        assert files[0].name < files[1].name < files[2].name


@pytest.mark.unit
class TestRinexWorker:
    """Test the worker function that runs in a separate process."""

    @patch("receivers.scheduling.bulk_scheduler._track_rinex_output_files")
    @patch("receivers.rinex.async_converter._find_raw_files")
    @patch("receivers.rinex.async_converter._create_converter")
    @patch("receivers.config_utils.get_station_config")
    @patch("receivers.config.receivers_config.get_receivers_config")
    def test_worker_converts_files(
        self,
        mock_config,
        mock_station_config,
        mock_create,
        mock_find,
        mock_track,
        tmp_path,
    ):
        """Worker finds raw files, converts them, returns counts."""
        # Config mocks
        mock_cfg = MagicMock()
        mock_cfg.get_data_prepath.return_value = str(tmp_path)
        mock_cfg.get_rinex_config.return_value = {"default_version": 3}
        mock_config.return_value = mock_cfg

        mock_station_config.return_value = {"receiver": {"type": "polarx5"}}

        # Converter mock
        mock_converter = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.rinex_file = tmp_path / "output.d.Z"
        mock_converter.convert_file.return_value = mock_result
        mock_create.return_value = (mock_converter, ".sbf.gz")

        # Raw files
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        raw_file = raw_dir / "ELDC20260301a.sbf.gz"
        raw_file.touch()
        mock_find.return_value = [raw_file]

        result = _rinex_worker(
            "ELDC",
            "15s_24hr",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
        )

        assert result["converted"] == 1
        assert result["failed"] == 0
        assert len(result["output_files"]) == 1

    @patch("receivers.rinex.async_converter._find_raw_files")
    @patch("receivers.rinex.async_converter._create_converter")
    @patch("receivers.config_utils.get_station_config")
    @patch("receivers.config.receivers_config.get_receivers_config")
    def test_worker_no_raw_files(
        self,
        mock_config,
        mock_station_config,
        mock_create,
        mock_find,
    ):
        """Worker with no raw files returns zero counts."""
        mock_cfg = MagicMock()
        mock_cfg.get_data_prepath.return_value = "/tmp/fake"
        mock_cfg.get_rinex_config.return_value = {"default_version": 3}
        mock_config.return_value = mock_cfg

        mock_station_config.return_value = {"receiver": {"type": "polarx5"}}

        mock_create.return_value = (MagicMock(), ".sbf.gz")
        mock_find.return_value = []

        result = _rinex_worker(
            "ELDC",
            "15s_24hr",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
        )

        assert result["converted"] == 0
        assert result["failed"] == 0

    @patch("receivers.config_utils.get_station_config")
    @patch("receivers.config.receivers_config.get_receivers_config")
    def test_worker_unknown_station(
        self,
        mock_config,
        mock_station_config,
    ):
        """Worker with unknown station returns skipped."""
        mock_cfg = MagicMock()
        mock_cfg.get_data_prepath.return_value = "/tmp/fake"
        mock_cfg.get_rinex_config.return_value = {}
        mock_config.return_value = mock_cfg

        mock_station_config.return_value = None

        result = _rinex_worker(
            "FAKE",
            "15s_24hr",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
        )

        assert result["skipped"] == 1
        assert result["converted"] == 0


@pytest.mark.unit
class TestParallelDownloadRinexHook:
    """Test that the RINEX hook fires in _download_one_station."""

    @patch("receivers.cli.parallel._check_health_ping_online", return_value=True)
    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_rinex_submitted_on_success(
        self,
        mock_validate,
        mock_download,
        mock_submit,
        mock_ping,
    ):
        """RINEX is submitted when download succeeds and --rinex is set."""
        from receivers.cli.parallel import _download_one_station

        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (5, 0, 24)

        args = Mock()
        args.session = "15s_24hr"
        args.rinex = True

        start = datetime(2026, 3, 1)
        end = datetime(2026, 3, 2)

        result = _download_one_station(
            "ELDC",
            args,
            start,
            end,
            "1D",
            "15s",
            False,
        )

        assert result.status == "completed"
        # Per-file callback is set, so per-station fallback only fires if no per-file
        # callbacks ran. With mock download, _rinex_file_count stays empty → fallback.
        mock_submit.assert_called_once_with("ELDC", "15s_24hr", start, end)

    @patch("receivers.cli.parallel._check_health_ping_online", return_value=True)
    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_rinex_not_submitted_without_flag(
        self,
        mock_validate,
        mock_download,
        mock_submit,
        mock_ping,
    ):
        """RINEX is NOT submitted when --rinex is not set."""
        from receivers.cli.parallel import _download_one_station

        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (5, 0, 24)

        args = Mock()
        args.session = "15s_24hr"
        args.rinex = False

        result = _download_one_station(
            "ELDC",
            args,
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
            "1D",
            "15s",
            False,
        )

        assert result.status == "completed"
        mock_submit.assert_not_called()

    @patch("receivers.cli.parallel._check_health_ping_online", return_value=True)
    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_rinex_not_submitted_on_failure(
        self,
        mock_validate,
        mock_download,
        mock_submit,
        mock_ping,
    ):
        """RINEX is NOT submitted when download fails."""
        from receivers.cli.parallel import _download_one_station

        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (0, 1, 5)  # errors > 0

        args = Mock()
        args.session = "15s_24hr"
        args.rinex = True

        result = _download_one_station(
            "ELDC",
            args,
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
            "1D",
            "15s",
            False,
        )

        assert result.status == "failed"
        mock_submit.assert_not_called()

    @patch("receivers.cli.parallel._check_health_ping_online", return_value=True)
    @patch("receivers.rinex.async_converter.submit_rinex_conversion")
    @patch("receivers.cli.main._download_station_period")
    @patch("receivers.cli.main._validate_station_for_download")
    def test_rinex_not_submitted_when_up_to_date(
        self,
        mock_validate,
        mock_download,
        mock_submit,
        mock_ping,
    ):
        """RINEX is NOT submitted for up_to_date (no new files)."""
        from receivers.cli.parallel import _download_one_station

        mock_receiver = MagicMock()
        mock_receiver._quick_ping.return_value = True
        mock_validate.return_value = mock_receiver
        mock_download.return_value = (0, 0, 24)  # up_to_date

        args = Mock()
        args.session = "15s_24hr"
        args.rinex = True

        result = _download_one_station(
            "ELDC",
            args,
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
            "1D",
            "15s",
            False,
        )

        assert result.status == "up_to_date"
        mock_submit.assert_not_called()


@pytest.mark.unit
class TestShutdownPool:
    """Test pool shutdown behavior."""

    @patch("receivers.rinex.async_converter._get_executor")
    def test_shutdown_waits_for_pending(self, mock_get_executor):
        """shutdown_rinex_pool(wait=True) blocks until conversions finish."""
        # Submit a real job to verify shutdown waits
        mock_executor = MagicMock()
        mock_get_executor.return_value = mock_executor

        submit_rinex_conversion(
            "ELDC",
            "15s_24hr",
            datetime(2026, 3, 1),
            datetime(2026, 3, 2),
        )

        import receivers.rinex.async_converter as mod

        mod._executor = mock_executor

        shutdown_rinex_pool(wait=True)
        mock_executor.shutdown.assert_called_once_with(wait=True)

    def test_shutdown_noop_when_no_executor(self):
        """Shutdown is safe when no executor was ever created."""
        import receivers.rinex.async_converter as mod

        mod._executor = None
        shutdown_rinex_pool(wait=True)  # Should not raise
