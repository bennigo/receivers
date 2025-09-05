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
        assert not self.receiver.pasv  # 10.4.1.x should use non-passive

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

    @patch("receivers.septentrio.polarx5.FTP")
    def test_connection_status_success(self, mock_ftp):
        """Test successful connection status check."""
        mock_ftp_instance = Mock()
        mock_ftp.return_value = mock_ftp_instance

        status = self.receiver.get_connection_status()

        assert status["router"] is True
        assert status["receiver"] is True
        assert status["ip"] == "10.4.1.100"
        assert status["port"] == 21
        assert status["error"] is None

    @patch("receivers.septentrio.polarx5.FTP")
    def test_connection_status_failure(self, mock_ftp):
        """Test failed connection status check."""
        mock_ftp.side_effect = Exception("Connection failed")

        status = self.receiver.get_connection_status()

        assert status["router"] is False
        assert status["receiver"] is False
        assert "Connection failed" in status["error"]

    def test_get_health_status(self):
        """Test health status reporting."""
        with patch.object(self.receiver, "get_connection_status") as mock_conn:
            mock_conn.return_value = {"receiver": True, "router": True}

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
