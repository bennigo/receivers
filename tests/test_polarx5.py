"""Tests for PolaRX5 receiver implementation."""

from unittest.mock import Mock, patch

import pytest

from receivers.base.exceptions import ConfigurationError
from receivers.septentrio.polarx5 import PolaRX5


class TestPolaRX5:
    """Test cases for PolaRX5 receiver class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.station_info = {
            "router": {"ip": "10.4.1.100"},
            "receiver": {"ftpport": "21"},
        }
        self.receiver = PolaRX5("REYK", self.station_info)

    def test_init(self):
        """Test receiver initialization."""
        assert self.receiver.station_id == "REYK"
        assert self.receiver.ip_number == "10.4.1.100"
        assert self.receiver.ip_port == 21
        # FTP mode is NOT inferred from the subnet any more. This line used to
        # assert `not pasv` with the comment "10.4.1.x should use non-passive";
        # b9c8eb2 removed that heuristic and defaulted auto/unset to passive,
        # because almost all stations sit behind NAT routers where active-mode
        # PORT commands fail on the IP mismatch. The full contract (which
        # section ftp_mode is read from, and the `receiver.ftp_mode` decoy) is
        # already covered by TestPolaRX5FtpModeFromStationConfig below — not
        # repeated here.
        assert self.receiver.pasv

    def test_init_missing_config(self):
        """Test initialization with missing configuration."""
        bad_config = {"router": {"ip": "10.4.1.100"}}  # Missing receiver info

        with pytest.raises(ConfigurationError):
            PolaRX5("REYK", bad_config)

    def test_get_station_info(self):
        """Test getting station information."""
        info = self.receiver.get_station_info()

        assert info["station_id"] == "REYK"
        assert info["receiver_type"] == "PolaRX5"
        assert info["ip"] == "10.4.1.100"
        assert info["port"] == 21

    @patch("socket.socket")
    @patch("subprocess.run")
    def test_connection_status_success(self, mock_run, mock_socket):
        """Test successful connection status check."""
        # Mock successful ping
        mock_run.return_value = Mock(returncode=0)
        # Mock successful HTTP port check
        mock_sock_instance = Mock()
        mock_sock_instance.connect_ex.return_value = 0
        mock_socket.return_value = mock_sock_instance

        status = self.receiver.get_connection_status()

        assert status["router"] is True
        assert status["receiver"] is True
        assert status["ip"] == "10.4.1.100"
        assert status["http_port"] == 8060
        assert status["error"] is None

    @patch("socket.socket")
    @patch("subprocess.run")
    def test_connection_status_failure(self, mock_run, mock_socket):
        """Test failed connection status check."""
        # Mock failed ping
        mock_run.return_value = Mock(returncode=1)
        # Mock failed HTTP port check
        mock_sock_instance = Mock()
        mock_sock_instance.connect_ex.return_value = 1
        mock_socket.return_value = mock_sock_instance

        status = self.receiver.get_connection_status()

        assert status["router"] is False
        assert status["receiver"] is False
        assert status["error"] is not None

    def test_get_health_status(self):
        """Test health status reporting.

        This used to stub only `get_connection_status`, which was enough when
        health was derived from it alone. It no longer is: `get_health_status`
        builds its own PolaRX5TCPExtractor and probes ports itself. With only
        the old stub the test **opened a real TCP connection to 10.4.1.100:28784**
        — a production receiver — and then asserted on whatever the sandbox's
        failure looked like ("warning"). A unit test must not touch the network,
        and on a host that can route to 10.4.1.x this one reached live hardware.

        Both collaborators are stubbed at their source modules, since
        `get_health_status` imports them lazily inside the call.
        """
        healthy_ports = {
            name: {"status": "ok", "open": True} for name in ("ftp", "http", "control")
        }
        extractor = Mock()
        extractor._check_port_status.return_value = healthy_ports
        extractor.extract_health_data.return_value = {
            "metrics": {"ports": healthy_ports},
            "data_quality": {},
        }

        # The ping result is formatted with `:.1f`, so a bare Mock raises
        # TypeError and the whole TCP branch is skipped — the stub needs real
        # numbers, not just truthiness.
        ping_result = Mock(
            accessible=True, response_time_ms=12.5, details={"packet_loss": 0}
        )
        checker = Mock()
        checker.check_ping.return_value = ping_result

        with (
            patch.object(
                self.receiver,
                "get_connection_status",
                return_value={"receiver": True, "router": True},
            ),
            patch(
                "receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor",
                return_value=extractor,
            ),
            patch(
                "receivers.health.connection_checker.ConnectionChecker",
                return_value=checker,
            ),
        ):
            health = self.receiver.get_health_status()

        assert health["station_id"] == "REYK"
        assert health["receiver_type"] == "PolaRX5"
        assert health["overall_status"] == "healthy"

    def test_is_gz_file(self):
        """Test gzip file detection."""
        # Test with non-existent file
        assert not PolaRX5.is_gz_file("/nonexistent/file")

    def test_str_repr(self):
        """Test string representations."""
        assert str(self.receiver) == "PolaRX5(REYK)"
        assert repr(self.receiver) == "PolaRX5(station_id='REYK')"


class TestPolaRX5FtpModeFromStationConfig:
    """Verify _setup_connection_info reads ftp_mode from the same dict where
    config_utils.get_station_config writes it (router.ftp_mode), so the
    cfg_discrepancy override actually reaches self.pasv.
    """

    def _make(self, ftp_mode_value=None):
        info = {
            "router": {"ip": "10.4.1.100"},
            "receiver": {"ftpport": "21"},
        }
        if ftp_mode_value is not None:
            info["router"]["ftp_mode"] = ftp_mode_value
        return PolaRX5("TEST", info)

    def test_router_ftp_mode_active_sets_pasv_false(self):
        """router.ftp_mode='active' (the override target) → self.pasv = False."""
        r = self._make("active")
        assert r.pasv is False

    def test_router_ftp_mode_passive_sets_pasv_true(self):
        """router.ftp_mode='passive' → self.pasv = True."""
        r = self._make("passive")
        assert r.pasv is True

    def test_router_ftp_mode_auto_defaults_to_passive(self):
        """router.ftp_mode='auto' → defaults to passive (NAT-friendly)."""
        r = self._make("auto")
        assert r.pasv is True

    def test_router_ftp_mode_missing_defaults_to_passive(self):
        """No ftp_mode anywhere → defaults to passive (NAT-friendly)."""
        r = self._make(None)
        assert r.pasv is True

    def test_receiver_ftp_mode_is_ignored(self):
        """Regression guard: ftp_mode under 'receiver' must NOT be read.

        config_utils.get_station_config writes the cfg_discrepancy override
        to router.ftp_mode (line 159), not receiver.ftp_mode. Reading from
        receiver.ftp_mode silently ignored the override on every run.
        """
        info = {
            "router": {"ip": "10.4.1.100"},  # no ftp_mode → defaults
            "receiver": {"ftpport": "21", "ftp_mode": "active"},  # decoy
        }
        r = PolaRX5("TEST", info)
        # ftp_mode under receiver is ignored, falls through to default passive
        assert r.pasv is True


class TestSafeResumeOffset:
    """Verify _safe_resume_offset enforces partial_size <= remote_file_size."""

    def test_returns_zero_when_no_partial(self, tmp_path):
        import logging

        from receivers.septentrio.polarx5 import _safe_resume_offset

        logger = logging.getLogger("test")
        local = tmp_path / "no_such_file.gz"
        assert _safe_resume_offset(str(local), 1000, logger) == 0

    def test_returns_zero_when_empty_partial(self, tmp_path):
        import logging

        from receivers.septentrio.polarx5 import _safe_resume_offset

        logger = logging.getLogger("test")
        local = tmp_path / "empty.gz"
        local.write_bytes(b"")
        assert _safe_resume_offset(str(local), 1000, logger) == 0

    def test_returns_partial_size_when_within_remote(self, tmp_path):
        """Normal case: partial < remote → resume from partial size."""
        import logging

        from receivers.septentrio.polarx5 import _safe_resume_offset

        logger = logging.getLogger("test")
        local = tmp_path / "partial.gz"
        local.write_bytes(b"x" * 500)
        # remote is 1000 bytes, we have 500 → resume from 500
        assert _safe_resume_offset(str(local), 1000, logger) == 500

    def test_returns_zero_and_deletes_oversized_partial(self, tmp_path):
        """Critical: partial > remote → delete partial, return 0.

        Prevents the 554 deadlock observed 2026-05-10 with FAGC where the
        local partial was 24,904,440 bytes but the server's current file
        was 22,292,412 bytes.
        """
        import logging

        from receivers.septentrio.polarx5 import _safe_resume_offset

        logger = logging.getLogger("test")
        local = tmp_path / "oversized.gz"
        local.write_bytes(b"x" * 24_904_440)  # mimic FAGC scenario
        offset = _safe_resume_offset(str(local), 22_292_412, logger)
        assert offset == 0
        assert not local.exists(), "oversized partial should be deleted"

    def test_returns_partial_size_when_equal_to_remote(self, tmp_path):
        """Edge case: partial == remote → resume from end (download is done)."""
        import logging

        from receivers.septentrio.polarx5 import _safe_resume_offset

        logger = logging.getLogger("test")
        local = tmp_path / "complete.gz"
        local.write_bytes(b"x" * 1000)
        assert _safe_resume_offset(str(local), 1000, logger) == 1000


@pytest.mark.integration
class TestPolaRX5Integration:
    """Integration tests for PolaRX5 (require actual configuration)."""

    def test_download_dry_run(self):
        """Test download in dry-run mode."""
        # This would require actual station configuration
        pass

    @pytest.mark.ftp
    def test_real_connection(self):
        """Test connection to real receiver."""
        # This would require access to actual receiver
        pass
