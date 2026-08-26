"""Unit tests for G10HTTPExtractor with mock API responses.

Tests parsing of real Leica GR10 AJAX endpoint response formats.
Sample data captured from SKFC station.
"""

from unittest.mock import MagicMock, patch

import pytest

from receivers.health.g10_http_extractor import G10HTTPExtractor

# Sample responses captured from real SKFC receiver (Leica GR10)
SAMPLE_STATUS_BLOCK_XML = """<block>
  <uptime>2908d 09h 54min</uptime>
  <power>
    <external>
      <voltage>14.9 V</voltage>
    </external>
  </power>
  <sdCard>
    <state>good</state>
    <availSpPrc>78.39</availSpPrc>
    <totAvailSp>3.02 GB</totAvailSp>
  </sdCard>
  <dataStreams>
    <condition>ok</condition>
    <state>good</state>
    <actDataStreams>1</actDataStreams>
  </dataStreams>
  <loggingSessionStatus>
    <condition>ok</condition>
    <state>good</state>
    <actLogSessions>2</actLogSessions>
  </loggingSessionStatus>
</block>"""

SAMPLE_TRACKING_JSON = {
    "POS": {"type": "Navigated", "checkDistanceToRef": "1"},
    "GPS": {
        "state": "enabled",
        "visible": "10",
        "trackedL1": "10",
        "trackedL2P": "10",
        "trackedL2C": "0",
        "trackedL5": "0",
    },
    "GLO": {
        "state": "enabled",
        "visible": "8",
        "trackedL1": "8",
        "trackedL2P": "8",
        "trackedL2C": "0",
    },
    "GAL": {
        "state": "disabled",
        "visible": "0",
        "trackedL1": "0",
        "trackedL2P": "0",
    },
    "COM": {
        "state": "nolicense",
        "visible": "0",
        "trackedL1": "0",
    },
    "SBAS": {
        "state": "disabled",
        "trackedL1": "0",
    },
    "OSC": "Internal",
}


class MockResponse:
    """Mock HTTP response."""

    def __init__(self, text="", status_code=200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("No JSON data")


@pytest.fixture
def extractor():
    """Create G10HTTPExtractor instance for testing."""
    return G10HTTPExtractor(
        host="10.4.1.100",
        station_id="SKFC",
        port=8060,
        timeout=10,
    )


class TestG10Login:
    """Test session login to BarracudaServer."""

    def test_successful_login(self, extractor):
        """Successful login returns a session object."""
        mock_session = MagicMock()
        mock_session.post.return_value = MockResponse(status_code=200)

        with patch("requests.Session", return_value=mock_session):
            session = extractor._login()

        assert session is not None
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "/index.lsp" in call_args[0][0]
        assert call_args[1]["data"]["j_username"] == "unrestrictedguestlogin"
        assert call_args[1]["data"]["j_password"] == "unrestrictedguestlogin"

    def test_login_failure_returns_none(self, extractor):
        """Failed login (non-200) returns None."""
        mock_session = MagicMock()
        mock_session.post.return_value = MockResponse(status_code=403)

        with patch("requests.Session", return_value=mock_session):
            session = extractor._login()

        assert session is None

    def test_login_timeout_returns_none(self, extractor):
        """Login timeout returns None."""
        import requests as req

        mock_session = MagicMock()
        mock_session.post.side_effect = req.Timeout("Connection timed out")

        with patch("requests.Session", return_value=mock_session):
            session = extractor._login()

        assert session is None

    def test_login_connection_error_returns_none(self, extractor):
        """Login connection error returns None."""
        import requests as req

        mock_session = MagicMock()
        mock_session.post.side_effect = req.ConnectionError("Refused")

        with patch("requests.Session", return_value=mock_session):
            session = extractor._login()

        assert session is None


class TestG10StatusBlockParsing:
    """Test XML parsing of /ajax_statusblockgeneral/ responses."""

    def test_parse_voltage(self, extractor):
        """Parse voltage from status block XML."""
        root = extractor._parse_xml(SAMPLE_STATUS_BLOCK_XML)
        result = extractor._parse_voltage(root)

        assert result is not None
        assert result["voltage"] == 14.9
        assert result["unit"] == "V"
        assert result["status"] == "ok"
        assert result["source"] == "external"

    def test_parse_uptime(self, extractor):
        """Parse uptime from status block XML."""
        root = extractor._parse_xml(SAMPLE_STATUS_BLOCK_XML)
        result = extractor._parse_uptime(root)

        assert result is not None
        assert result["days"] == 2908
        assert result["hours"] == 9
        assert result["minutes"] == 54
        assert result["formatted"] == "2908d 9h 54m"
        expected_seconds = (2908 * 86400) + (9 * 3600) + (54 * 60)
        assert result["seconds"] == expected_seconds

    def test_parse_disk(self, extractor):
        """Parse SD card info from status block XML."""
        root = extractor._parse_xml(SAMPLE_STATUS_BLOCK_XML)
        result = extractor._parse_disk(root)

        assert result is not None
        assert result["sd_card_state"] == "good"
        assert result["free_percent"] == 78.39
        assert result["usage_percent"] == pytest.approx(21.61, abs=0.1)
        assert result["total_available"] == "3.02 GB"
        assert result["status"] == "ok"

    def test_parse_disk_high_usage_warning(self, extractor):
        """Disk with >85% usage gets warning status."""
        xml = """<block><sdCard>
            <state>good</state>
            <availSpPrc>10.0</availSpPrc>
            <totAvailSp>0.5 GB</totAvailSp>
        </sdCard></block>"""
        root = extractor._parse_xml(xml)
        result = extractor._parse_disk(root)

        assert result["usage_percent"] == 90.0
        assert result["status"] == "warning"

    def test_parse_disk_critical_usage(self, extractor):
        """Disk with >95% usage gets critical status."""
        xml = """<block><sdCard>
            <state>good</state>
            <availSpPrc>3.0</availSpPrc>
            <totAvailSp>0.1 GB</totAvailSp>
        </sdCard></block>"""
        root = extractor._parse_xml(xml)
        result = extractor._parse_disk(root)

        assert result["usage_percent"] == 97.0
        assert result["status"] == "critical"

    def test_parse_data_streams(self, extractor):
        """Parse data streams from status block XML."""
        root = extractor._parse_xml(SAMPLE_STATUS_BLOCK_XML)
        result = extractor._parse_data_streams(root)

        assert result is not None
        assert result["condition"] == "ok"
        assert result["state"] == "good"
        assert result["active_streams"] == 1
        assert result["status"] == "ok"

    def test_parse_logging_sessions(self, extractor):
        """Parse logging sessions from status block XML."""
        root = extractor._parse_xml(SAMPLE_STATUS_BLOCK_XML)
        result = extractor._parse_logging_sessions(root)

        assert result is not None
        assert result["condition"] == "ok"
        assert result["state"] == "good"
        assert result["active_sessions"] == 2
        assert result["status"] == "ok"

    def test_parse_voltage_missing_element(self, extractor):
        """Missing voltage element returns None."""
        xml = "<block><power><external></external></power></block>"
        root = extractor._parse_xml(xml)
        result = extractor._parse_voltage(root)

        assert result is None

    def test_parse_uptime_missing_element(self, extractor):
        """Missing uptime element returns None."""
        xml = "<block></block>"
        root = extractor._parse_xml(xml)
        result = extractor._parse_uptime(root)

        assert result is None

    def test_parse_disk_missing_sd_card(self, extractor):
        """Missing sdCard element returns None."""
        xml = "<block></block>"
        root = extractor._parse_xml(xml)
        result = extractor._parse_disk(root)

        assert result is None


class TestG10TrackingSummary:
    """Test JSON parsing of /ajax_tracking_summary/ responses."""

    def test_parse_tracking_gps_and_glonass(self, extractor):
        """Parse GPS and GLONASS from tracking JSON."""
        result = extractor._parse_tracking(SAMPLE_TRACKING_JSON)

        assert result is not None
        assert result["total"] == 18  # GPS:10 + GLO:8
        assert result["visible"] == 18  # GPS:10 + GLO:8
        assert result["by_constellation"]["GPS"] == 10
        assert result["by_constellation"]["GLONASS"] == 8
        assert result["status"] == "ok"

    def test_disabled_constellations_excluded(self, extractor):
        """Disabled constellations report 0 tracked satellites."""
        result = extractor._parse_tracking(SAMPLE_TRACKING_JSON)

        assert result["by_constellation"]["Galileo"] == 0
        assert result["by_constellation"]["SBAS"] == 0

    def test_nolicense_constellation_excluded(self, extractor):
        """Constellations with 'nolicense' state are excluded."""
        result = extractor._parse_tracking(SAMPLE_TRACKING_JSON)

        # BeiDou maps from "COM" which has state "nolicense"
        # COM is not in our constellation mapping, so BeiDou stays at 0
        assert result["by_constellation"]["BeiDou"] == 0

    def test_tracking_total_count(self, extractor):
        """Total count sums all enabled constellations."""
        result = extractor._parse_tracking(SAMPLE_TRACKING_JSON)

        expected_total = 10 + 8  # GPS + GLO (others disabled)
        assert result["total"] == expected_total

    def test_tracking_gps_only(self, extractor):
        """Station with only GPS enabled."""
        data = {
            "GPS": {"state": "enabled", "visible": "7", "trackedL1": "6"},
            "GLO": {"state": "disabled", "visible": "0", "trackedL1": "0"},
        }
        result = extractor._parse_tracking(data)

        assert result["total"] == 6
        assert result["visible"] == 7
        assert result["by_constellation"]["GPS"] == 6
        assert result["by_constellation"]["GLONASS"] == 0

    def test_tracking_low_satellite_warning(self, extractor):
        """Low satellite count triggers warning."""
        data = {
            "GPS": {"state": "enabled", "visible": "5", "trackedL1": "5"},
        }
        result = extractor._parse_tracking(data)

        assert result["total"] == 5
        assert result["status"] == "warning"  # 5 < sat_warning (8)

    def test_tracking_critical_satellite_count(self, extractor):
        """Very low satellite count triggers critical."""
        data = {
            "GPS": {"state": "enabled", "visible": "3", "trackedL1": "3"},
        }
        result = extractor._parse_tracking(data)

        assert result["total"] == 3
        assert result["status"] == "critical"  # 3 < sat_critical (4)

    def test_tracking_non_dict_constellation_ignored(self, extractor):
        """Non-dict values (like 'OSC': 'Internal') are handled."""
        result = extractor._parse_tracking(SAMPLE_TRACKING_JSON)

        # Should not crash on "OSC": "Internal"
        assert result is not None


class TestG10FullExtraction:
    """Test full extract_health_data() with mocked session."""

    def test_full_extraction_all_data(self, extractor):
        """Full extraction returns all metrics when both endpoints succeed."""
        mock_session = MagicMock()
        # Login response
        mock_session.post.return_value = MockResponse(status_code=200)
        # Status block response
        status_response = MockResponse(text=SAMPLE_STATUS_BLOCK_XML)
        tracking_response = MockResponse(json_data=SAMPLE_TRACKING_JSON)
        mock_session.get.side_effect = [status_response, tracking_response]

        def mock_test_conn():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch("requests.Session", return_value=mock_session):
            with patch.object(
                extractor, "_test_connection", return_value=mock_test_conn()
            ):
                result = extractor.extract_health_data()

        # Check structure
        assert result["station_id"] == "SKFC"
        assert result["receiver_type"] == "G10"
        assert result["schema_version"] == "1.0"
        assert result["extraction_metadata"]["data_source"] == "g10_http_ajax"

        # Check metrics present
        assert "power" in result["metrics"]
        assert result["metrics"]["power"]["voltage"] == 14.9
        assert "uptime" in result["metrics"]
        assert result["metrics"]["uptime"]["days"] == 2908
        assert "disk" in result["metrics"]
        assert result["metrics"]["disk"]["free_percent"] == 78.39
        assert "satellites" in result["metrics"]
        assert result["metrics"]["satellites"]["total"] == 18
        assert "data_streams" in result["metrics"]
        assert "logging_sessions" in result["metrics"]

        # Check unavailable metrics
        assert result["metrics"]["temperature"]["available"] is False
        assert result["metrics"]["cpu_load"]["available"] is False
        assert result["metrics"]["memory"]["available"] is False
        assert result["metrics"]["position"]["available"] is False

        # Check network unavailable
        assert result["network"]["ntrip_client"]["available"] is False

        # Overall status should be healthy (all metrics ok)
        assert result["overall_status"] == "healthy"

    def test_full_extraction_schema_complete(self, extractor):
        """Verify all required schema fields are present."""
        mock_session = MagicMock()
        mock_session.post.return_value = MockResponse(status_code=200)
        status_response = MockResponse(text=SAMPLE_STATUS_BLOCK_XML)
        tracking_response = MockResponse(json_data=SAMPLE_TRACKING_JSON)
        mock_session.get.side_effect = [status_response, tracking_response]

        def mock_test_conn():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch("requests.Session", return_value=mock_session):
            with patch.object(
                extractor, "_test_connection", return_value=mock_test_conn()
            ):
                result = extractor.extract_health_data()

        required_keys = [
            "station_id",
            "receiver_type",
            "timestamp",
            "schema_version",
            "connection",
            "metrics",
            "overall_status",
            "status_summary",
            "extraction_metadata",
        ]
        for key in required_keys:
            assert key in result, f"Missing required key: {key}"

        assert "extraction_duration_ms" in result["extraction_metadata"]


class TestG10Unavailable:
    """Test graceful handling of failures."""

    def test_login_failure_returns_minimal_health(self, extractor):
        """Login failure still returns a valid health dict."""
        mock_session = MagicMock()
        mock_session.post.return_value = MockResponse(status_code=403)

        def mock_test_conn():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch("requests.Session", return_value=mock_session):
            with patch.object(
                extractor, "_test_connection", return_value=mock_test_conn()
            ):
                result = extractor.extract_health_data()

        assert result["station_id"] == "SKFC"
        assert result["receiver_type"] == "G10"
        # Should still have basic structure
        assert "metrics" in result
        assert result["metrics"]["temperature"]["available"] is False
        assert result["metrics"]["cpu_load"]["available"] is False

    def test_connection_timeout_handled(self, extractor):
        """Connection timeout returns critical status."""
        import requests as req

        with patch("requests.get", side_effect=req.Timeout("Timeout")):
            conn = extractor._test_connection()

        assert conn["status"] == "critical"
        assert conn["accessible"] is False
        assert "Timeout" in conn["error"]

    def test_connection_refused_handled(self, extractor):
        """Connection refused returns critical status."""
        import requests as req

        with patch("requests.get", side_effect=req.ConnectionError("Refused")):
            conn = extractor._test_connection()

        assert conn["status"] == "critical"
        assert conn["accessible"] is False


class TestG10StatusCalculation:
    """Test overall status calculation."""

    def test_all_ok_is_healthy(self, extractor):
        """All ok statuses result in healthy."""
        assert extractor._calculate_overall_status(["ok", "ok", "ok"]) == "healthy"

    def test_any_critical_is_critical(self, extractor):
        """Any critical status makes overall critical."""
        assert (
            extractor._calculate_overall_status(["ok", "critical", "ok"]) == "critical"
        )

    def test_warning_without_critical(self, extractor):
        """Warning without critical results in warning."""
        assert extractor._calculate_overall_status(["ok", "warning", "ok"]) == "warning"

    def test_empty_statuses(self, extractor):
        """Empty status list returns unknown."""
        assert extractor._calculate_overall_status([]) == "unknown"

    def test_all_unknown_is_not_healthy(self, extractor):
        """A station we know NOTHING about must never report healthy.

        ``all(s == "ok" for s in statuses if s != "unknown")`` filters every
        element out here, and ``all()`` over an empty generator is True — so
        this returned "healthy" for a station that reported no usable metric at
        all, which is the one answer monitoring must never give. The
        ``if not statuses`` guard above only catches an EMPTY list, not an
        all-unknown one, so it did not save us.
        """
        assert extractor._calculate_overall_status(["unknown"]) == "unknown"
        assert extractor._calculate_overall_status(["unknown", "unknown"]) == "unknown"

    def test_ok_alongside_unknown_is_still_healthy(self, extractor):
        """Guarding the empty case must not break partial reporting."""
        assert extractor._calculate_overall_status(["ok", "unknown"]) == "healthy"

    def test_status_counting(self, extractor):
        """Test status counting."""
        counts = extractor._count_statuses(
            ["ok", "ok", "warning", "critical", "unknown"]
        )
        assert counts["healthy"] == 2
        assert counts["warning"] == 1
        assert counts["critical"] == 1
        assert counts["unknown"] == 1
