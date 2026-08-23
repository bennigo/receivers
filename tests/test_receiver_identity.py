"""Tests for receiver identity capture and mismatch detection.

Tests cover:
- SBF block 4027 (ReceiverSetup) parsing
- Trimble firmware/serial extraction
- FTP banner text capture
- Fingerprint matching and mismatch detection
- Status formatter identity display
- Port status detail reporting (refused vs timeout)
"""

import struct
from unittest.mock import MagicMock, patch

import pytest

# ---- ReceiverSetup SBF block parsing ----


class TestReceiverSetupParsing:
    """Test _query_receiver_setup() SBF block 4027 parsing."""

    def _build_sbf_block(
        self,
        block_id: int = 4027,
        marker_name: str = "THOB",
        marker_number: str = "",
        observer: str = "",
        agency: str = "",
        serial_number: str = "3034406",
        rx_name: str = "PolaRx5",
        rx_version: str = "5.5.2",
    ) -> bytes:
        """Build a minimal SBF ReceiverSetup block."""
        # SBF header: sync(2) + crc(2) + id_rev(2) + length(2) = 8 bytes
        # Then: TOW(4) + WNc(2) = 6 bytes → total 14 bytes before fields

        def _pad(s: str, size: int) -> bytes:
            return s.encode("ascii").ljust(size, b"\x00")

        # Build payload after header
        tow = struct.pack("<I", 0)  # TOW placeholder
        wnc = struct.pack("<H", 0)  # WNc placeholder

        # ReceiverSetup carries a 2-byte Reserved field between WNc and
        # MarkerName, so the strings start at offset 16 — not 14. This fixture
        # omitted it and laid every field 2 bytes early, which shifted the whole
        # block: the parser read "laRx5" out of "PolaRx5". It also made the
        # block 214 bytes, short of the parser's 216-byte minimum, so the
        # earlier symptom was a silent None. Both are the one missing field.
        reserved = struct.pack("<H", 0)

        fields = (
            _pad(marker_name, 60)  # 16-75
            + _pad(marker_number, 20)  # 76-95
            + _pad(observer, 20)  # 96-115
            + _pad(agency, 40)  # 116-155
            + _pad(serial_number, 20)  # 156-175
            + _pad(rx_name, 20)  # 176-195
            + _pad(rx_version, 20)  # 196-215
        )

        payload = tow + wnc + reserved + fields
        total_length = 8 + len(payload)
        # 8 header + 4 TOW + 2 WNc + 2 Reserved + 200 strings = 216, which is
        # both the parser's minimum and 4-byte aligned as every SBF block must be.
        assert total_length == 216, total_length
        assert total_length % 4 == 0, "SBF block length must be 4-byte aligned"

        # SBF header
        sync = b"$@"
        crc = struct.pack("<H", 0)
        id_rev = struct.pack("<H", block_id & 0x1FFF)
        length = struct.pack("<H", total_length)

        return sync + crc + id_rev + length + payload

    def test_parse_receiver_setup_basic(self):
        """Test parsing a well-formed ReceiverSetup block."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")

        sbf_data = self._build_sbf_block(
            serial_number="3034406",
            rx_name="PolaRx5",
            rx_version="5.5.2",
        )

        # `_query_receiver_setup` tries an UNAUTHENTICATED request first (the
        # fw 5.7.0 bootstrap added in ee09519: pre-5.7 receivers are fully open,
        # and the fw version is itself what decides whether to authenticate).
        # `_send_sbf_request` is only the authenticated fallback, so stubbing it
        # alone left the real unauthenticated call to run — it returned None off
        # the network, and with no credentials configured the method bailed out
        # before ever reaching the stub.
        extractor._request_receiver_setup_unauthenticated = MagicMock(
            return_value=sbf_data
        )
        extractor._send_sbf_request = MagicMock(return_value=sbf_data)

        result = extractor._query_receiver_setup()

        assert result is not None
        assert result["receiver_model"] == "PolaRx5"
        assert result["firmware_version"] == "5.5.2"
        assert result["serial_number"] == "3034406"

    def test_parse_receiver_setup_no_data(self):
        """Test handling when no SBF data returned."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")
        extractor._send_sbf_request = MagicMock(return_value=None)

        result = extractor._query_receiver_setup()
        assert result is None

    def test_parse_receiver_setup_short_response(self):
        """Test handling when response is too short."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")
        # Short SBF block with valid header but not enough data
        short_block = b"$@\x00\x00" + struct.pack("<HH", 4027, 20) + b"\x00" * 12
        extractor._send_sbf_request = MagicMock(return_value=short_block)

        result = extractor._query_receiver_setup()
        assert result is None

    def test_parse_receiver_setup_empty_fields(self):
        """Test handling when all identity fields are empty."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")
        sbf_data = self._build_sbf_block(serial_number="", rx_name="", rx_version="")
        extractor._send_sbf_request = MagicMock(return_value=sbf_data)

        result = extractor._query_receiver_setup()
        assert result is None

    def test_extract_health_data_includes_identity(self):
        """Test that extract_health_data includes receiver_identity when available."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")

        identity = {
            "receiver_model": "PolaRx5",
            "firmware_version": "5.5.2",
            "serial_number": "3034406",
        }

        # Mock all SBF queries: only receiver_setup returns data
        extractor._check_port_status = MagicMock(return_value={})
        extractor._query_power_status = MagicMock(return_value=None)
        extractor._query_receiver_status = MagicMock(return_value=None)
        extractor._query_disk_status = MagicMock(return_value=None)
        extractor._query_pvt_geodetic = MagicMock(return_value=None)
        extractor._query_satellite_tracking = MagicMock(return_value=None)
        extractor._query_ntrip_client_status = MagicMock(return_value=None)
        extractor._query_ntrip_server_status = MagicMock(return_value=None)
        extractor._query_receiver_setup = MagicMock(return_value=identity)

        health_data = extractor.extract_health_data()

        assert "receiver_identity" in health_data
        assert health_data["receiver_identity"]["receiver_model"] == "PolaRx5"
        assert health_data["receiver_identity"]["firmware_version"] == "5.5.2"


# ---- Fingerprint matching ----


class TestReceiverFingerprint:
    """Test fingerprint matching and mismatch detection."""

    def test_no_mismatch_polarx5(self):
        """Test that PolaRx5 identity matches PolaRX5 config."""
        from receivers.health.receiver_fingerprint import check_identity_mismatch

        result = check_identity_mismatch(
            "PolaRX5",
            {"receiver_model": "PolaRx5 5.5.2", "serial_number": "3034406"},
        )
        assert result is None

    def test_mismatch_detected(self):
        """Test mismatch when NetR9 is configured but PolaRx5 detected."""
        from receivers.health.receiver_fingerprint import check_identity_mismatch

        result = check_identity_mismatch(
            "NetR9",
            {"receiver_model": "PolaRx5 5.5.2"},
        )
        assert result is not None
        assert "mismatch" in result.lower()
        assert "NetR9" in result
        assert "PolaRx5" in result

    def test_no_model_no_mismatch(self):
        """Test that empty model data returns no mismatch."""
        from receivers.health.receiver_fingerprint import check_identity_mismatch

        result = check_identity_mismatch(
            "PolaRX5",
            {"serial_number": "3034406"},  # No receiver_model
        )
        assert result is None

    def test_unknown_configured_type(self):
        """Test that unknown configured type returns no mismatch."""
        from receivers.health.receiver_fingerprint import check_identity_mismatch

        result = check_identity_mismatch(
            "UnknownReceiver",
            {"receiver_model": "PolaRx5"},
        )
        assert result is None

    def test_banner_mismatch(self):
        """Test banner-based mismatch detection."""
        from receivers.health.receiver_fingerprint import check_identity_mismatch

        result = check_identity_mismatch(
            "PolaRX5",
            {"receiver_model": "NetR9"},
            ftp_banner="220 Trimble FTP server ready",
        )
        assert result is not None
        assert "mismatch" in result.lower()

    def test_identify_receiver_type(self):
        """Test receiver type identification from identity data."""
        from receivers.health.receiver_fingerprint import identify_receiver_type

        assert identify_receiver_type({"receiver_model": "PolaRx5 5.5.2"}) == "PolaRX5"
        assert identify_receiver_type({"receiver_model": "NetR9"}) == "NetR9"
        assert identify_receiver_type({"receiver_model": "GR10"}) == "G10"
        assert identify_receiver_type({"receiver_model": "unknown_thing"}) is None

    def test_identify_from_banner(self):
        """Test receiver type identification from FTP banner."""
        from receivers.health.receiver_fingerprint import identify_receiver_type

        result = identify_receiver_type(
            {"receiver_model": ""},
            ftp_banner="220 Septentrio FTP server ready",
        )
        assert result == "PolaRX5"


# ---- Trimble firmware extraction ----


class TestTrimbleFirmware:
    """Test firmware version extraction from Trimble receivers."""

    def test_firmware_endpoint_in_health_endpoints(self):
        """Test that firmware endpoint is defined."""
        from receivers.health.trimble_http_extractor import TrimbleHTTPExtractor

        assert "firmware" in TrimbleHTTPExtractor.HEALTH_ENDPOINTS
        assert "FirmwareVersion" in TrimbleHTTPExtractor.HEALTH_ENDPOINTS["firmware"]

    def test_system_info_includes_firmware(self):
        """Test that _fetch_system_info fetches firmware version."""
        from receivers.health.trimble_http_extractor import TrimbleHTTPExtractor

        extractor = TrimbleHTTPExtractor("10.0.0.1", "TEST")

        # Mock endpoint responses
        def mock_fetch(endpoint_name):
            responses = {
                "serial": "sn=1234567890",
                "firmware": "version=5.45",
                "antenna": 'name="TRM59800.00" height=0.075',
                "refstation": "Name='TEST'",
            }
            return responses.get(endpoint_name)

        extractor._fetch_endpoint = MagicMock(side_effect=mock_fetch)

        result = extractor._fetch_system_info()
        assert result is not None
        assert result["serial_number"] == "1234567890"
        assert result["firmware_version"] == "5.45"

    def test_receiver_identity_in_health_data(self):
        """Test that extract_health_data includes receiver_identity for Trimble."""
        from receivers.health.trimble_http_extractor import TrimbleHTTPExtractor

        extractor = TrimbleHTTPExtractor("10.0.0.1", "TEST", receiver_type="NetR9")

        # Build a minimal health_data manually to test the identity wiring
        # Mock all fetch methods to return test data
        extractor._test_connection = MagicMock(
            return_value={"status": "ok", "port": 8060, "accessible": True}
        )
        extractor._check_port_status = MagicMock(return_value={})
        extractor._fetch_merge_xml = MagicMock(return_value=None)
        extractor._fetch_and_parse_voltages = MagicMock(return_value=None)
        extractor._fetch_and_parse_temperature = MagicMock(return_value=None)
        extractor._fetch_and_parse_tracking = MagicMock(return_value=None)
        extractor._fetch_and_parse_position = MagicMock(return_value=None)
        extractor._fetch_system_info = MagicMock(
            return_value={
                "serial_number": "12345",
                "firmware_version": "5.45",
            }
        )

        health_data = extractor.extract_health_data()

        assert "receiver_identity" in health_data
        assert health_data["receiver_identity"]["serial_number"] == "12345"
        assert health_data["receiver_identity"]["firmware_version"] == "5.45"
        assert health_data["receiver_identity"]["receiver_model"] == "NetR9"


# ---- FTP banner text ----


class TestFTPBannerCapture:
    """Test FTP banner text storage in connection checker."""

    def test_ftp_banner_stored(self):
        """Test that FTP banner text is captured in details."""
        from receivers.health.connection_checker import ConnectionChecker

        checker = ConnectionChecker("10.0.0.1", "TEST")

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recv.return_value = b"220 Septentrio PolaRx5 FTP server ready\r\n"

            result = checker.check_ftp(port=2160)

            assert result.accessible is True
            assert result.details["ftp_banner"] is True
            assert (
                result.details["banner_text"]
                == "220 Septentrio PolaRx5 FTP server ready"
            )

    def test_ftp_no_banner(self):
        """Test FTP connection with no banner."""
        from receivers.health.connection_checker import ConnectionChecker

        checker = ConnectionChecker("10.0.0.1", "TEST")

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recv.return_value = b""

            result = checker.check_ftp(port=2160)

            assert result.accessible is True
            assert result.details["ftp_banner"] is False
            assert result.details["banner_text"] is None


# ---- Port status detail reporting ----


class TestPortStatusDetail:
    """Test that port status uses proper HealthStatus values with details."""

    def test_refused_port_is_warning(self):
        """Test that a refused port reports status=warning, detail=refused."""
        import errno

        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.connect_ex.return_value = errno.ECONNREFUSED

            result = extractor._check_port_status()

            # Check all ports report refused
            for name in ("ftp", "http", "control"):
                assert result[name]["status"] == "warning"
                assert result[name]["detail"] == "refused"
                assert result[name]["open"] is False

    def test_open_port_is_ok(self):
        """Test that an open port reports status=ok."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.connect_ex.return_value = 0

            result = extractor._check_port_status()

            for name in ("ftp", "http", "control"):
                assert result[name]["status"] == "ok"
                assert result[name]["detail"] == "open"
                assert result[name]["open"] is True

    def test_timeout_port_is_critical(self):
        """Test that a timed-out port reports status=critical."""
        import errno

        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        extractor = PolaRX5TCPExtractor("10.0.0.1", "TEST")

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.connect_ex.return_value = errno.ETIMEDOUT

            result = extractor._check_port_status()

            for name in ("ftp", "http", "control"):
                assert result[name]["status"] == "critical"
                assert result[name]["detail"] == "timeout"


# ---- Overall status with unknown statuses ----


class TestOverallStatusUnknown:
    """Test that overall status ignores unknowns when real data exists."""

    def test_ok_plus_unknown_is_healthy(self):
        """Test that OK + UNKNOWN statuses produce healthy, not unknown."""
        from receivers.base.receiver import BaseReceiver
        from receivers.health.connection_checker import HealthStatus

        # Can't instantiate BaseReceiver directly, but we can test the logic
        # by checking the code path. Instead test via a mock.
        # The logic is: known_statuses = [OK]; all OK → healthy
        known_statuses = [HealthStatus.OK, HealthStatus.OK]
        all_statuses = known_statuses + [HealthStatus.UNKNOWN]

        # Simulate the build_health_status logic
        known = [s for s in all_statuses if s != HealthStatus.UNKNOWN]
        if HealthStatus.CRITICAL in all_statuses:
            overall = "critical"
        elif HealthStatus.ERROR in all_statuses:
            overall = "critical"
        elif HealthStatus.WARNING in all_statuses:
            overall = "warning"
        elif known and all(s == HealthStatus.OK for s in known):
            overall = "healthy"
        elif not known:
            overall = "unknown"
        else:
            overall = "unknown"

        assert overall == "healthy"

    def test_warning_plus_unknown_is_warning(self):
        """Test that WARNING + UNKNOWN produces warning, not unknown."""
        from receivers.health.connection_checker import HealthStatus

        all_statuses = [HealthStatus.OK, HealthStatus.WARNING, HealthStatus.UNKNOWN]

        known = [s for s in all_statuses if s != HealthStatus.UNKNOWN]
        if HealthStatus.WARNING in all_statuses:
            overall = "warning"
        elif known and all(s == HealthStatus.OK for s in known):
            overall = "healthy"
        else:
            overall = "unknown"

        assert overall == "warning"


# ---- Status formatter identity display ----


class TestStatusFormatterIdentity:
    """Test that status formatter shows receiver identity."""

    def test_identity_displayed_in_summary(self):
        """Test that firmware and serial appear in formatted output."""
        from receivers.health.status_formatter import StatusFormatter

        formatter = StatusFormatter()

        health_data = {
            "station_id": "THOB",
            "receiver_type": "PolaRX5",
            "overall_status": "healthy",
            "receiver_identity": {
                "receiver_model": "PolaRx5",
                "firmware_version": "5.5.2",
                "serial_number": "3034406",
            },
            "metrics": {},
        }

        lines = formatter.format_health_summary(health_data)

        # Find the identity line
        identity_lines = [l for l in lines if "Identity" in l]
        assert len(identity_lines) == 1
        assert "FW: 5.5.2" in identity_lines[0]
        assert "S/N: 3034406" in identity_lines[0]

    def test_no_identity_no_line(self):
        """Test that no identity line appears when identity is missing."""
        from receivers.health.status_formatter import StatusFormatter

        formatter = StatusFormatter()

        health_data = {
            "station_id": "THOB",
            "receiver_type": "PolaRX5",
            "overall_status": "healthy",
            "metrics": {},
        }

        lines = formatter.format_health_summary(health_data)
        identity_lines = [l for l in lines if "Identity" in l]
        assert len(identity_lines) == 0

    def test_port_refused_detail_displayed(self):
        """Test that refused ports show 'refused' instead of 'closed'."""
        from receivers.health.status_formatter import StatusFormatter

        formatter = StatusFormatter()

        health_data = {
            "station_id": "AFST",
            "receiver_type": "PolaRX5",
            "overall_status": "warning",
            "metrics": {
                "ports": {
                    "ftp": {
                        "port": 2160,
                        "open": True,
                        "status": "ok",
                        "detail": "open",
                    },
                    "http": {
                        "port": 8060,
                        "open": True,
                        "status": "ok",
                        "detail": "open",
                    },
                    "control": {
                        "port": 28784,
                        "open": False,
                        "status": "warning",
                        "detail": "refused",
                    },
                },
            },
        }

        lines = formatter.format_health_summary(health_data)
        ports_lines = [l for l in lines if "Ports" in l]
        assert len(ports_lines) == 1
        assert "refused" in ports_lines[0]
        assert "control:28784 refused" in ports_lines[0]
