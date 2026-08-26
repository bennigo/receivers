"""HTTP-based health data extractor for Leica GR10 receivers.

This module fetches health data from Leica GR10 receivers via the BarracudaServer
web interface (port 8060) using session-based AJAX endpoints.

Uses the centralized MetricChecker from receivers.health.metrics for consistent
threshold evaluation across all health monitoring components.

Leica GR10 AJAX Endpoints (require authenticated session):
- /ajax_statusblockgeneral/ : XML - voltage, uptime, SD card, data streams
- /ajax_tracking_summary/   : JSON - satellite tracking per constellation
"""

import logging
import re
import socket
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from .metrics import MetricChecker


class G10HTTPExtractor:
    """Extract health data from Leica GR10 receivers via AJAX HTTP endpoints.

    The GR10 web interface (BarracudaServer) requires session-based authentication:
    1. POST to /index.lsp with j_username/j_password to establish session
    2. GET AJAX endpoints with session cookie for data

    Uses the centralized MetricChecker for consistent threshold evaluation.
    """

    AJAX_ENDPOINTS = {
        "status_block": "/ajax_statusblockgeneral/",
        "tracking_summary": "/ajax_tracking_summary/",
    }

    def __init__(
        self,
        host: str,
        station_id: str = "UNKNOWN",
        port: int = 8060,
        timeout: int = 10,
        username: str = "unrestrictedguestlogin",
        password: str = "unrestrictedguestlogin",
        ftp_port: Optional[int] = None,
    ):
        """Initialize HTTP health extractor for Leica GR10.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
            port: HTTP port (default: 8060 for BarracudaServer)
            timeout: Request timeout in seconds
            username: Login username (default: unrestrictedguestlogin)
            password: Login password (default: unrestrictedguestlogin)
            ftp_port: FTP port for connection checking (optional)
        """
        self.host = host
        self.station_id = station_id.upper()
        self.port = port
        self.ftp_port = ftp_port
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.username = username
        self.password = password
        self.logger = logging.getLogger(f"receivers.health.{station_id}")

        # Initialize centralized metric checker
        from .metrics import load_thresholds

        power_type = None
        try:
            from ..config_utils import get_station_config

            # silent=True: power_type is supplementary, and station_id may be a
            # probe placeholder with no cfg section — matches the PolaRX5
            # extractor; absence here is not an error.
            cfg = get_station_config(station_id, silent=True)
            if cfg:
                power_type = cfg.get("power_type") or None
        except Exception:
            pass
        config = load_thresholds(receiver_type="G10", power_type=power_type)
        self.metric_checker = MetricChecker(config)

    def extract_health_data(self) -> Dict[str, Any]:
        """Extract health data from all available AJAX endpoints.

        Returns:
            Dictionary with extracted health data in standardized format.
        """
        start_time = datetime.now(UTC)

        health_data = {
            "station_id": self.station_id,
            "receiver_type": "G10",
            "timestamp": start_time.isoformat().replace("+00:00", "Z"),
            "schema_version": "1.0",
            "connection": {},
            "metrics": {},
            "data_quality": {},
            "network": {},
            "overall_status": "unknown",
            "status_summary": {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0},
            "extraction_metadata": {
                "extraction_time": start_time.isoformat().replace("+00:00", "Z"),
                "data_source": "g10_http_ajax",
                "tool_version": "1.0.0",
            },
        }

        statuses: List[str] = []

        # Test HTTP connection
        conn_status = self._test_connection()
        health_data["connection"]["http_port"] = conn_status
        statuses.append(conn_status.get("status", "unknown"))

        # Check port status
        port_status = self._check_port_status(
            http_accessible=conn_status.get("accessible", False)
        )
        if port_status:
            health_data["metrics"]["ports"] = port_status
            for port_data in port_status.values():
                statuses.append("ok" if port_data.get("open") else "warning")

        # Login to get session
        session = self._login()
        if not session:
            self.logger.warning("Login failed, cannot fetch AJAX data")
            health_data["metrics"]["temperature"] = {"available": False}
            health_data["metrics"]["cpu_load"] = {"available": False}
            health_data["metrics"]["memory"] = {"available": False}
            health_data["overall_status"] = self._calculate_overall_status(statuses)
            health_data["status_summary"] = self._count_statuses(statuses)

            end_time = datetime.now(UTC)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)
            health_data["extraction_metadata"]["extraction_duration_ms"] = duration_ms
            return health_data

        # Fetch and parse status block (XML) - voltage, uptime, disk, data streams
        status_xml = self._fetch_status_block(session)
        if status_xml:
            root = self._parse_xml(status_xml)
            if root is not None:
                voltage_data = self._parse_voltage(root)
                if voltage_data:
                    health_data["metrics"]["power"] = voltage_data
                    statuses.append(voltage_data.get("status", "unknown"))

                uptime_data = self._parse_uptime(root)
                if uptime_data:
                    health_data["metrics"]["uptime"] = uptime_data
                else:
                    health_data["metrics"]["uptime"] = {"available": False}

                disk_data = self._parse_disk(root)
                if disk_data:
                    health_data["metrics"]["disk"] = disk_data
                    statuses.append(disk_data.get("status", "unknown"))
                else:
                    health_data["metrics"]["disk"] = {"available": False}

                streams_data = self._parse_data_streams(root)
                if streams_data:
                    health_data["metrics"]["data_streams"] = streams_data
                    statuses.append(streams_data.get("status", "unknown"))

                logging_data = self._parse_logging_sessions(root)
                if logging_data:
                    health_data["metrics"]["logging_sessions"] = logging_data
                    statuses.append(logging_data.get("status", "unknown"))
        else:
            health_data["metrics"]["uptime"] = {"available": False}
            health_data["metrics"]["disk"] = {"available": False}

        # Fetch and parse tracking summary (JSON) - satellites
        tracking_json = self._fetch_tracking_summary(session)
        if tracking_json:
            tracking_data = self._parse_tracking(tracking_json)
            if tracking_data:
                health_data["metrics"]["satellites"] = tracking_data
                statuses.append(tracking_data.get("status", "unknown"))

        # Correct port status if AJAX endpoints succeeded after initial test
        # (BarracudaServer can be slow on initial unauthenticated GET but works
        # fine for authenticated session requests)
        http_data_fetched = status_xml is not None or tracking_json is not None
        if http_data_fetched and "ports" in health_data["metrics"]:
            http_port = health_data["metrics"]["ports"].get("http", {})
            if not http_port.get("open"):
                health_data["metrics"]["ports"]["http"]["open"] = True
                health_data["metrics"]["ports"]["http"]["status"] = "ok"
                health_data["connection"]["http_port"]["accessible"] = True
                health_data["connection"]["http_port"]["status"] = "ok"
                # Fix stale statuses from initial connection test timeout
                statuses = ["ok" if s == "critical" else s for s in statuses]

        # Mark unavailable metrics
        health_data["metrics"]["temperature"] = {"available": False}
        health_data["metrics"]["cpu_load"] = {"available": False}
        health_data["metrics"]["memory"] = {"available": False}
        if "position" not in health_data["metrics"]:
            health_data["metrics"]["position"] = {"available": False}

        # Network features not available on G10 via AJAX
        health_data["network"]["ntrip_client"] = {"available": False}
        health_data["network"]["ntrip_server"] = {"available": False}

        # Calculate overall status
        health_data["overall_status"] = self._calculate_overall_status(statuses)
        health_data["status_summary"] = self._count_statuses(statuses)

        # Calculate extraction duration
        end_time = datetime.now(UTC)
        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        health_data["extraction_metadata"]["extraction_duration_ms"] = duration_ms

        return health_data

    def _login(self) -> Optional[requests.Session]:
        """Establish authenticated session with BarracudaServer.

        POSTs credentials to /index.lsp to get session cookie.

        Returns:
            requests.Session with auth cookie, or None on failure.
        """
        try:
            session = requests.Session()
            response = session.post(
                f"{self.base_url}/index.lsp",
                data={
                    "j_username": self.username,
                    "j_password": self.password,
                },
                timeout=self.timeout,
            )

            if response.status_code == 200:
                self.logger.debug("Login successful")
                return session
            else:
                self.logger.warning(f"Login failed: HTTP {response.status_code}")
                return None

        except requests.Timeout:
            self.logger.error(f"Login timeout after {self.timeout}s")
            return None
        except requests.ConnectionError as e:
            self.logger.error(f"Login connection error: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return None

    def _fetch_status_block(self, session: requests.Session) -> Optional[str]:
        """Fetch /ajax_statusblockgeneral/ XML data.

        Args:
            session: Authenticated requests session

        Returns:
            Raw XML string or None
        """
        url = f"{self.base_url}{self.AJAX_ENDPOINTS['status_block']}"
        try:
            response = session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                self.logger.debug(f"Fetched status block ({len(response.text)} bytes)")
                return response.text
            else:
                self.logger.warning(
                    f"Status block fetch failed: HTTP {response.status_code}"
                )
                return None
        except Exception as e:
            self.logger.error(f"Error fetching status block: {e}")
            return None

    def _fetch_tracking_summary(self, session: requests.Session) -> Optional[Dict]:
        """Fetch /ajax_tracking_summary/ JSON data.

        Args:
            session: Authenticated requests session

        Returns:
            Parsed JSON dict or None
        """
        url = f"{self.base_url}{self.AJAX_ENDPOINTS['tracking_summary']}"
        try:
            response = session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                self.logger.debug(
                    f"Fetched tracking summary ({len(response.text)} bytes)"
                )
                return response.json()
            else:
                self.logger.warning(
                    f"Tracking summary fetch failed: HTTP {response.status_code}"
                )
                return None
        except Exception as e:
            self.logger.error(f"Error fetching tracking summary: {e}")
            return None

    def _parse_xml(self, xml_text: str) -> Optional[ET.Element]:
        """Parse XML text into ElementTree root.

        Args:
            xml_text: Raw XML string

        Returns:
            Root Element or None on parse failure
        """
        try:
            return ET.fromstring(xml_text)
        except ET.ParseError as e:
            self.logger.error(f"XML parse error: {e}")
            return None

    def _parse_voltage(self, root: ET.Element) -> Optional[Dict[str, Any]]:
        """Extract voltage from status block XML.

        Expected path: <power><external><voltage>14.9 V</voltage></external></power>

        Args:
            root: XML root element

        Returns:
            Voltage data dict or None
        """
        try:
            voltage_elem = root.find(".//power/external/voltage")
            if voltage_elem is None or voltage_elem.text is None:
                return None

            # Parse "14.9 V" format
            match = re.match(r"([\d.]+)\s*V", voltage_elem.text.strip())
            if not match:
                return None

            voltage = float(match.group(1))
            status = self._voltage_status(voltage)

            return {
                "voltage": voltage,
                "unit": "V",
                "status": status,
                "source": "external",
                "threshold_warning": self.metric_checker.config.voltage_warning_low,
                "threshold_critical": self.metric_checker.config.voltage_critical_low,
            }
        except Exception as e:
            self.logger.error(f"Error parsing voltage: {e}")
            return None

    def _parse_uptime(self, root: ET.Element) -> Optional[Dict[str, Any]]:
        """Parse uptime from status block XML.

        Expected: <uptime>2908d 09h 54min</uptime>

        Args:
            root: XML root element

        Returns:
            Uptime data dict or None
        """
        try:
            uptime_elem = root.find(".//uptime")
            if uptime_elem is None or uptime_elem.text is None:
                return None

            text = uptime_elem.text.strip()
            # Parse "2908d 09h 54min" format
            days = hours = minutes = 0

            day_match = re.search(r"(\d+)d", text)
            if day_match:
                days = int(day_match.group(1))

            hour_match = re.search(r"(\d+)h", text)
            if hour_match:
                hours = int(hour_match.group(1))

            min_match = re.search(r"(\d+)min", text)
            if min_match:
                minutes = int(min_match.group(1))

            total_seconds = (days * 86400) + (hours * 3600) + (minutes * 60)

            return {
                "seconds": total_seconds,
                "days": days,
                "hours": hours,
                "minutes": minutes,
                "formatted": f"{days}d {hours}h {minutes}m",
                "source": "status_block",
            }
        except Exception as e:
            self.logger.error(f"Error parsing uptime: {e}")
            return None

    def _parse_disk(self, root: ET.Element) -> Optional[Dict[str, Any]]:
        """Extract SD card info from status block XML.

        Expected:
            <sdCard>
                <state>good</state>
                <availSpPrc>78.39</availSpPrc>
                <totAvailSp>3.02 GB</totAvailSp>
            </sdCard>

        Args:
            root: XML root element

        Returns:
            Disk data dict or None
        """
        try:
            sd_card = root.find(".//sdCard")
            if sd_card is None:
                return None

            state_elem = sd_card.find("state")
            avail_prc_elem = sd_card.find("availSpPrc")
            total_elem = sd_card.find("totAvailSp")

            state = (
                state_elem.text.strip()
                if state_elem is not None and state_elem.text
                else "unknown"
            )
            free_percent = (
                float(avail_prc_elem.text.strip())
                if avail_prc_elem is not None and avail_prc_elem.text
                else None
            )

            # Parse total available space (e.g., "3.02 GB")
            total_available_str = (
                total_elem.text.strip()
                if total_elem is not None and total_elem.text
                else None
            )

            usage_percent = (
                round(100.0 - free_percent, 1) if free_percent is not None else None
            )

            # Determine status from usage percent
            status = "ok"
            if usage_percent is not None:
                if usage_percent >= 95:
                    status = "critical"
                elif usage_percent >= 85:
                    status = "warning"

            result: Dict[str, Any] = {
                "sd_card_state": state,
                "status": status,
                "source": "status_block",
            }

            if free_percent is not None:
                result["free_percent"] = free_percent
            if usage_percent is not None:
                result["usage_percent"] = usage_percent
            if total_available_str:
                result["total_available"] = total_available_str

            return result
        except Exception as e:
            self.logger.error(f"Error parsing disk info: {e}")
            return None

    def _parse_data_streams(self, root: ET.Element) -> Optional[Dict[str, Any]]:
        """Extract data streams info from status block XML.

        Expected:
            <dataStreams>
                <condition>ok</condition>
                <state>good</state>
                <actDataStreams>1</actDataStreams>
            </dataStreams>

        Args:
            root: XML root element

        Returns:
            Data streams dict or None
        """
        try:
            streams = root.find(".//dataStreams")
            if streams is None:
                return None

            condition_elem = streams.find("condition")
            state_elem = streams.find("state")
            count_elem = streams.find("actDataStreams")

            condition = (
                condition_elem.text.strip()
                if condition_elem is not None and condition_elem.text
                else "unknown"
            )
            state = (
                state_elem.text.strip()
                if state_elem is not None and state_elem.text
                else "unknown"
            )
            count = (
                int(count_elem.text.strip())
                if count_elem is not None and count_elem.text
                else 0
            )

            status = "ok" if condition == "ok" and state == "good" else "warning"

            return {
                "condition": condition,
                "state": state,
                "active_streams": count,
                "status": status,
            }
        except Exception as e:
            self.logger.error(f"Error parsing data streams: {e}")
            return None

    def _parse_logging_sessions(self, root: ET.Element) -> Optional[Dict[str, Any]]:
        """Extract logging session info from status block XML.

        Expected:
            <loggingSessionStatus>
                <condition>ok</condition>
                <state>good</state>
                <actLogSessions>2</actLogSessions>
            </loggingSessionStatus>

        Args:
            root: XML root element

        Returns:
            Logging sessions dict or None
        """
        try:
            logging_elem = root.find(".//loggingSessionStatus")
            if logging_elem is None:
                return None

            condition_elem = logging_elem.find("condition")
            state_elem = logging_elem.find("state")
            count_elem = logging_elem.find("actLogSessions")

            condition = (
                condition_elem.text.strip()
                if condition_elem is not None and condition_elem.text
                else "unknown"
            )
            state = (
                state_elem.text.strip()
                if state_elem is not None and state_elem.text
                else "unknown"
            )
            count = (
                int(count_elem.text.strip())
                if count_elem is not None and count_elem.text
                else 0
            )

            status = "ok" if condition == "ok" and state == "good" else "warning"

            return {
                "condition": condition,
                "state": state,
                "active_sessions": count,
                "status": status,
            }
        except Exception as e:
            self.logger.error(f"Error parsing logging sessions: {e}")
            return None

    def _parse_tracking(self, data: Dict) -> Optional[Dict[str, Any]]:
        """Parse satellite tracking data from JSON response.

        Expected format:
            {"GPS": {"state": "enabled", "visible": "10", "trackedL1": "10", ...},
             "GLO": {"state": "enabled", "visible": "8", "trackedL1": "8", ...},
             ...}

        Args:
            data: Parsed JSON dict from tracking summary endpoint

        Returns:
            Standardized satellite data dict or None
        """
        try:
            constellations = {
                "GPS": "GPS",
                "GLO": "GLONASS",
                "GAL": "Galileo",
                "BDS": "BeiDou",
                "SBAS": "SBAS",
            }

            by_constellation: Dict[str, int] = {}
            total_tracked = 0
            total_visible = 0

            for key, display_name in constellations.items():
                constellation_data = data.get(key, {})
                if not isinstance(constellation_data, dict):
                    by_constellation[display_name] = 0
                    continue

                state = constellation_data.get("state", "disabled")
                if state not in ("enabled",):
                    by_constellation[display_name] = 0
                    continue

                tracked = int(constellation_data.get("trackedL1", "0"))
                visible = int(constellation_data.get("visible", "0"))
                by_constellation[display_name] = tracked
                total_tracked += tracked
                total_visible += visible

            status = self._satellite_status(total_tracked)

            return {
                "total": total_tracked,
                "visible": total_visible,
                "status": status,
                "by_constellation": by_constellation,
                "threshold_warning": self.metric_checker.config.sat_warning,
                "threshold_critical": self.metric_checker.config.sat_critical,
            }
        except Exception as e:
            self.logger.error(f"Error parsing tracking data: {e}")
            return None

    def _test_connection(self) -> Dict[str, Any]:
        """Test HTTP connection to receiver.

        Returns:
            Connection status dictionary
        """
        start = datetime.now()
        try:
            response = requests.get(
                f"{self.base_url}/index.lsp",
                timeout=self.timeout,
            )
            duration_ms = int((datetime.now() - start).total_seconds() * 1000)

            if response.status_code == 200:
                return {
                    "status": "ok",
                    "port": self.port,
                    "response_time_ms": duration_ms,
                    "accessible": True,
                }
            else:
                return {
                    "status": "warning",
                    "port": self.port,
                    "response_time_ms": duration_ms,
                    "accessible": False,
                    "error": f"HTTP {response.status_code}",
                }
        except requests.Timeout:
            return {
                "status": "critical",
                "port": self.port,
                "accessible": False,
                "error": f"Timeout after {self.timeout}s",
            }
        except requests.ConnectionError as e:
            return {
                "status": "critical",
                "port": self.port,
                "accessible": False,
                "error": str(e),
            }
        except Exception as e:
            return {
                "status": "critical",
                "port": self.port,
                "accessible": False,
                "error": str(e),
            }

    def _check_port_status(
        self, http_accessible: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """Check HTTP and FTP port status.

        Args:
            http_accessible: Whether HTTP API is accessible

        Returns:
            Dictionary with port status for http and ftp (if configured)
        """
        ports: Dict[str, Dict[str, Any]] = {}

        ports["http"] = {
            "port": int(self.port),
            "open": http_accessible,
            "status": "ok" if http_accessible else "critical",
        }

        if self.ftp_port:
            ftp_open = self._check_tcp_port(self.host, self.ftp_port)
            ports["ftp"] = {
                "port": int(self.ftp_port),
                "open": ftp_open,
                "status": "ok" if ftp_open else "critical",
            }

        return ports

    def _check_tcp_port(self, host: str, port: int) -> bool:
        """Check if a TCP port is reachable."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception as e:
            self.logger.debug(f"Port check failed for {host}:{port}: {e}")
            return False

    def _voltage_status(self, voltage: float) -> str:
        """Determine voltage status using centralized thresholds."""
        result = self.metric_checker.check_voltage(voltage)
        return result.status.value

    def _satellite_status(self, count: int) -> str:
        """Determine satellite tracking status using centralized thresholds."""
        result = self.metric_checker.check_satellites(count)
        return result.status.value

    def _calculate_overall_status(self, statuses: List[str]) -> str:
        """Calculate overall health status from individual statuses."""
        if not statuses:
            return "unknown"

        if "critical" in statuses:
            return "critical"
        elif "warning" in statuses:
            return "warning"
        else:
            # Guard the empty case explicitly. `all(... for s in statuses if
            # s != "unknown")` filters every element out when the station
            # reported nothing but "unknown", and `all()` over an empty
            # generator is True — so a station we know NOTHING about used to
            # report "healthy", which is the one answer monitoring must never
            # give. The `if not statuses` check above only catches an empty
            # list, not an all-unknown one. Mirrors the Trimble extractor's
            # `_calculate_overall_status`.
            non_unknown = [s for s in statuses if s != "unknown"]
            if non_unknown and all(s == "ok" for s in non_unknown):
                return "healthy"
        return "unknown"

    def _count_statuses(self, statuses: List[str]) -> Dict[str, int]:
        """Count statuses by category."""
        counts = {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
        for status in statuses:
            if status == "ok":
                counts["healthy"] += 1
            elif status == "warning":
                counts["warning"] += 1
            elif status == "critical":
                counts["critical"] += 1
            else:
                counts["unknown"] += 1
        return counts
