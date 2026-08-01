"""TCP-based live health data extractor for Septentrio PolaRX5 receivers.

This module extracts real-time health data from PolaRX5 receivers via the TCP
command interface (default port 28784). It queries SBF blocks on demand using
the `esoc` (exeSBFOnce) command.

Uses the centralized MetricChecker from receivers.health.metrics for consistent
threshold evaluation across all health monitoring components.

SBF Blocks Used:
- 4101 PowerStatus: Power supply voltage
- 4014 ReceiverStatus: CPU load, temperature, uptime
- 4059 DiskStatus: Internal storage status
- 4007 PVTGeodetic2: Position (lat, lon, alt) and accuracy
- 4013 ChannelStatus: Satellite tracking per channel
- 4082 QualityInd: Quality indicators including satellite counts
- 5902 ReceiverSetup: Receiver model, firmware version, serial number (all fw versions)

Usage:
    extractor = PolaRX5TCPExtractor('10.6.1.201', 'ISFS')
    health_data = extractor.extract_health_data()
"""

import logging
import re
import socket
import ssl
import struct
import time
from datetime import UTC, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..septentrio.fw_policy import firmware_requires_auth
from .metrics import MetricChecker, load_thresholds

#: Backwards-compatible alias. The policy now lives in
#: ``receivers.septentrio.fw_policy`` so that every receiver-comms path (health
#: extractor, config-push TCP client, download manager, polarx5) shares one
#: definition instead of importing this module's private helper across packages.
_firmware_requires_auth = firmware_requires_auth


class PolaRX5TCPExtractor:
    """Extract live health data from PolaRX5 via TCP command interface."""

    # Default TCP command port for Septentrio receivers
    CONTROL_PORT = 28784

    # SBF block IDs
    BLOCK_POWER_STATUS = 4101
    BLOCK_RECEIVER_STATUS = 4014
    BLOCK_DISK_STATUS = 4059
    BLOCK_PVT_GEODETIC2 = 4007
    BLOCK_PVT_SAT_CARTESIAN = 4008  # Satellites used in PVT solution
    BLOCK_CHANNEL_STATUS = 4013
    BLOCK_QUALITY_IND = 4082
    BLOCK_NTRIP_SERVER_STATUS = 4122  # NTRIP server connections
    BLOCK_NTRIP_CLIENT_STATUS = 4053  # NTRIP client connection
    BLOCK_RECEIVER_SETUP = (
        5902  # ReceiverSetup - model, firmware, serial (all fw versions)
    )

    SECURE_CONTROL_PORT = (
        28783  # TLS port — used when sis=secure (fw upgrade resets to this)
    )

    def __init__(
        self,
        host: str,
        station_id: str,
        port: int = CONTROL_PORT,
        timeout: float = 10.0,
        port_config: Optional[Dict[str, int]] = None,
    ):
        """Initialize TCP health extractor.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
            port: TCP command port (default: 28784)
            timeout: Socket timeout in seconds
            port_config: Optional dict with port names and numbers to check
                        e.g., {"ftp": 2160, "http": 8060, "control": 28784}
        """
        self.host = host
        self.station_id = station_id
        self.port = port
        self.timeout = timeout
        self.logger = logging.getLogger(f"receivers.health.{station_id}")
        self._connection_id = None  # Will be detected from prompt (e.g., "IP11")
        self._auth_failed = (
            False  # Set on first bad-creds response; skips login for rest of session
        )
        self.use_tls = (
            False  # Set on first TLS fallback; subsequent connections reuse TLS
        )

        # TCP credentials for fw 5.7.0 authentication
        # Loaded from receivers.cfg [polarx5] section, with per-station override
        self.tcp_username: Optional[str] = None
        self.tcp_password: Optional[str] = None
        self.firmware_version: Optional[str] = None  # from stations.cfg; gates _login()
        try:
            from ..config.receivers_config import get_receivers_config

            rec_cfg = get_receivers_config().get_receiver_config("polarx5")
            self.tcp_username = rec_cfg.get("tcp_username") or None
            self.tcp_password = rec_cfg.get("tcp_password") or None
        except Exception as e:
            self.logger.debug(f"Could not load TCP credentials from receivers.cfg: {e}")

        # Initialize centralized metric checker for consistent threshold evaluation
        # Load thresholds with receiver-type and power-type overrides
        power_type = None
        try:
            from ..config_utils import get_station_config

            cfg = get_station_config(station_id, silent=True)
            if cfg:
                power_type = cfg.get("power_type") or None
                if cfg.get("tcp_username"):
                    self.tcp_username = cfg["tcp_username"]
                if cfg.get("tcp_password"):
                    self.tcp_password = cfg["tcp_password"]
        except Exception as e:
            self.logger.debug(f"Could not load station config: {e}")

        # Load firmware_version directly from stations.cfg (get_station_config parses only
        # known structured fields; receiver_firmware_version is a pass-through raw field)
        try:
            import gps_parser as _gps

            raw = _gps.ConfigParser().getStationInfo(station_id)
            station_raw = raw.get("station", {}) if isinstance(raw, dict) else {}
            self.firmware_version = station_raw.get("receiver_firmware_version") or None
        except Exception as e:
            self.logger.debug(f"Could not read firmware_version from stations.cfg: {e}")
        config = load_thresholds(receiver_type="PolaRX5", power_type=power_type)
        self.metric_checker = MetricChecker(config)

        # Port configuration for status checks
        self.port_config = port_config or {"ftp": 2160, "http": 8060, "control": 28784}

    def _open_socket(self) -> socket.socket:
        """Open a connected socket to the receiver TCP command port.

        Tries plaintext on self.port first. On ConnectionRefused, falls back to TLS on
        SECURE_CONTROL_PORT (28783) — this happens when sis=secure (e.g. right after a
        firmware upgrade before re-provisioning). Once TLS is confirmed, self.use_tls and
        self.port are updated so subsequent calls reuse TLS without reattempting plaintext.

        Returns the connected socket (plain or SSL-wrapped).
        Raises on all other errors so callers can handle them.
        """
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        try:
            raw.connect((self.host, self.port))
            if self.use_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return ctx.wrap_socket(raw)  # type: ignore[return-value]
            return raw
        except ConnectionRefusedError:
            if self.use_tls or self.port == self.SECURE_CONTROL_PORT:
                raise  # already on TLS port — nothing to fall back to
            raw.close()
            self.logger.debug(
                f"Port {self.port} refused — trying TLS on {self.SECURE_CONTROL_PORT}"
            )
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(self.timeout)
            raw.connect((self.host, self.SECURE_CONTROL_PORT))
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw)  # type: ignore[assignment]
            self.use_tls = True
            self.port = self.SECURE_CONTROL_PORT
            self.logger.info(
                f"[{self.station_id}] TLS fallback active — receiver is in sis=secure mode"
            )
            return sock  # type: ignore[return-value]

    def extract_health_data(self) -> Dict[str, Any]:
        """Extract health data from all available SBF blocks.

        Returns:
            Dictionary with extracted health data in standardized format
        """
        health_data = {
            "extraction_time": datetime.now(UTC).isoformat(),
            "extraction_method": "tcp_command",
            "metrics": {},
            "data_quality": {},
        }

        try:
            # Check port status (FTP, HTTP, control)
            port_status = self._check_port_status()
            if port_status:
                health_data["metrics"]["ports"] = port_status

            # Query PowerStatus for voltage
            power_data = self._query_power_status()
            if power_data:
                health_data["metrics"]["power"] = power_data

            # Query ReceiverStatus for CPU, temperature, uptime
            receiver_data = self._query_receiver_status()
            if receiver_data:
                if "cpu_load" in receiver_data:
                    health_data["metrics"]["cpu_load"] = receiver_data["cpu_load"]
                if "temperature" in receiver_data:
                    health_data["metrics"]["temperature"] = receiver_data["temperature"]
                if "uptime_seconds" in receiver_data:
                    health_data["metrics"]["uptime_seconds"] = receiver_data[
                        "uptime_seconds"
                    ]
                if "rx_status" in receiver_data:
                    health_data["metrics"]["rx_status"] = receiver_data["rx_status"]

            # Query DiskStatus for disk usage
            disk_data = self._query_disk_status()
            if disk_data:
                health_data["metrics"]["disk"] = disk_data

            # Query PVTGeodetic2 for position and accuracy
            position_data = self._query_pvt_geodetic()
            if position_data:
                health_data["metrics"]["position"] = position_data

            # Query ChannelStatus for satellite tracking
            satellite_data = self._query_satellite_tracking()
            if satellite_data:
                health_data["metrics"]["satellites"] = satellite_data

            # Query NTRIPClientStatus for RTK corrections
            ntrip_client = self._query_ntrip_client_status()
            if ntrip_client:
                health_data["metrics"]["ntrip_client"] = ntrip_client

            # Query NTRIPServerStatus for NTRIP caster
            ntrip_server = self._query_ntrip_server_status()
            if ntrip_server:
                health_data["metrics"]["ntrip_server"] = ntrip_server

            # Query ReceiverSetup for identity (model, firmware, serial, marker)
            setup_data = self._query_receiver_setup()
            if setup_data:
                health_data["receiver_identity"] = setup_data

            # Query logging sessions via ASCII command (lst, LogSession)
            logging_data = self._query_logging_sessions()
            if logging_data:
                health_data["metrics"]["logging_sessions"] = logging_data

        except Exception as e:
            self.logger.error(f"Error extracting health data: {e}")
            health_data["error"] = str(e)

        if self._auth_failed and not health_data.get("metrics"):
            self.logger.warning(
                f"All health data unavailable for {self.station_id} — TCP authentication failed"
            )

        return health_data

    def _query_power_status(self) -> Optional[Dict[str, Any]]:
        """Query PowerStatus SBF block for voltage info.

        Returns:
            Dictionary with power metrics or None on failure
        """
        sbf_data = self._send_sbf_request("PowerStatus", self.BLOCK_POWER_STATUS)
        if not sbf_data:
            return None

        try:
            # Parse SBF header (block ID already verified in _send_sbf_request)
            _, length = self._parse_sbf_header(sbf_data)

            # PowerStatus structure (16 bytes total):
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Bytes 14-15: PowerInfo (uint16, little-endian)
            #   - Lower 4 bits: PowerSource (1=Vin, 2=Vbat, etc.)
            #   - Upper 12 bits: VinVoltage (value / 40 = volts)
            # Formula verified against Septentrio bin2asc official output
            if length >= 16 and len(sbf_data) >= 16:
                # Read bytes 14-15 as 16-bit little-endian
                power_info = struct.unpack("<H", sbf_data[14:16])[0]
                # Voltage = upper 12 bits / 40
                voltage = (power_info >> 4) / 40.0

                return {
                    "voltage": round(voltage, 2),
                    "unit": "V",
                    "status": self._check_voltage_status(voltage),
                }

        except Exception as e:
            self.logger.error(f"Error parsing PowerStatus: {e}")

        return None

    def _query_receiver_status(self) -> Optional[Dict[str, Any]]:
        """Query ReceiverStatus SBF block (4014) for CPU, temperature, uptime.

        Returns:
            Dictionary with receiver metrics or None on failure
        """
        sbf_data = self._send_sbf_request("ReceiverStatus", self.BLOCK_RECEIVER_STATUS)
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            result = {}

            # ReceiverStatus structure (56 bytes typical for PolaRX5):
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: CPULoad (uint8, %)
            # Byte 15: ExtError (uint8)
            # Bytes 16-19: UpTime (uint32, seconds)
            # Bytes 20-23: RxStatus (uint32)
            # Bytes 24-27: RxError (uint32)
            # Byte 28: N (number of AGCState entries)
            # Byte 29: SBLength (size of AGCState)
            # Byte 30: CmdCount (uint8)
            # Byte 31: Temperature (int8, offset by 100°C)
            if length >= 32 and len(sbf_data) >= 32:
                # CPU Load at offset 14
                cpu_load = sbf_data[14]
                result["cpu_load"] = {
                    "percent": cpu_load,
                    "status": self._check_cpu_status(cpu_load),
                }

                # UpTime at offset 16-19
                uptime = struct.unpack("<I", sbf_data[16:20])[0]
                result["uptime_seconds"] = uptime

                # RxStatus at offset 20-23 (uint32 bitfield)
                rx_status = struct.unpack("<I", sbf_data[20:24])[0]
                result["rx_status"] = rx_status

                # Temperature at offset 31 (int8, formula: temp = raw - 100)
                # Verified against RxControl display
                temp_raw = sbf_data[31]
                temperature = temp_raw - 100
                result["temperature"] = {
                    "value": temperature,
                    "unit": "C",
                    "status": self._check_temperature_status(temperature),
                }

            return result

        except Exception as e:
            self.logger.error(f"Error parsing ReceiverStatus: {e}")

        return None

    def _query_ntrip_client_status(self) -> Optional[Dict[str, Any]]:
        """Query NTRIPClientStatus SBF block for NTRIP client connection info.

        NTRIPClientStatus (SBF 4053) reports RTK correction stream status.

        Returns:
            Dictionary with NTRIP client status or None on failure
        """
        sbf_data = self._send_sbf_request(
            "NTRIPClientStatus", self.BLOCK_NTRIP_CLIENT_STATUS
        )
        if not sbf_data:
            return None

        try:
            _, length = self._parse_sbf_header(sbf_data)

            # NTRIPClientStatus structure:
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: CDIndex (uint8) - connection descriptor index
            # Byte 15: Status (uint8) - 0=Idle, 1=Connected, 2=Error
            # Byte 16: ErrorCode (uint8)
            if length >= 17 and len(sbf_data) >= 17:
                cd_index = sbf_data[14]
                status_byte = sbf_data[15]
                error_code = sbf_data[16]
                # Septentrio NTRIPClientStatus status values:
                # 0=Idle, 1=Connecting, 2=Connected, 3=Error, 4=Sending
                status_map = {
                    0: "idle",
                    1: "connecting",
                    2: "connected",
                    3: "error",
                    4: "connected",  # Sending/receiving data = active connection
                }
                return {
                    "cd_index": f"NTR{cd_index + 1}",
                    "status": status_map.get(status_byte, f"unknown_{status_byte}"),
                    "error_code": error_code if status_byte == 3 else None,
                }

        except Exception as e:
            self.logger.error(f"Error parsing NTRIPClientStatus: {e}")

        return None

    def _query_ntrip_server_status(self) -> Optional[Dict[str, Any]]:
        """Query NTRIPServerStatus SBF block for NTRIP server connection info.

        NTRIPServerStatus (SBF 4122) reports NTRIP caster connection status.

        Returns:
            Dictionary with NTRIP server status or None on failure
        """
        sbf_data = self._send_sbf_request(
            "NTRIPServerStatus", self.BLOCK_NTRIP_SERVER_STATUS
        )
        if not sbf_data:
            return None

        try:
            _, length = self._parse_sbf_header(sbf_data)

            # NTRIPServerStatus structure:
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: CDIndex (uint8) - connection descriptor index
            # Byte 15: Status (uint8) - 0=Idle, 1=Connected, 2=Error
            # Byte 16: ErrorCode (uint8)
            if length >= 17 and len(sbf_data) >= 17:
                cd_index = sbf_data[14]
                status_byte = sbf_data[15]
                error_code = sbf_data[16]
                # Septentrio NTRIPServerStatus status values:
                # 0=Idle, 1=Connecting, 2=Connected, 3=Error, 4=Sending
                status_map = {
                    0: "idle",
                    1: "connecting",
                    2: "connected",
                    3: "error",
                    4: "connected",  # Sending/receiving data = active connection
                }
                return {
                    "cd_index": f"NTR{cd_index + 1}",
                    "status": status_map.get(status_byte, f"unknown_{status_byte}"),
                    "error_code": error_code if status_byte == 3 else None,
                }

        except Exception as e:
            self.logger.error(f"Error parsing NTRIPServerStatus: {e}")

        return None

    def _request_receiver_setup_unauthenticated(self) -> Optional[bytes]:
        """Request ReceiverSetup block without authenticating.

        Pre-5.7 receivers are fully open — no credentials configured. Skipping
        login here avoids spurious auth warnings on those stations and resolves
        the bootstrap problem (we need fw version to know whether to auth).

        Returns:
            Raw SBF bytes for block 5902, or None if auth is required or failed.
        """
        sock = None
        try:
            sock = self._open_socket()

            prompt = sock.recv(1024)
            conn_id = self._parse_connection_id(prompt)

            cmd = f"esoc, {conn_id}, ReceiverSetup\n"
            sock.sendall(cmd.encode())

            response = b""
            end_time = time.time() + 2.0
            while time.time() < end_time:
                try:
                    sock.settimeout(0.5)
                    chunk = sock.recv(8192)
                    if chunk:
                        response += chunk
                        decoded = response.decode("utf-8", errors="ignore")
                        if "Not authorized" in decoded:
                            self.logger.debug(
                                "ReceiverSetup unauthenticated esoc blocked — fw requires auth"
                            )
                            return None  # Needs auth — caller will retry with login
                        result = self._find_sbf_block(
                            response, self.BLOCK_RECEIVER_SETUP
                        )
                        if result is not None:
                            return result
                except TimeoutError:
                    if response:
                        break

            return self._find_sbf_block(response, self.BLOCK_RECEIVER_SETUP)

        except Exception as e:
            self.logger.debug(f"Unauthenticated ReceiverSetup query failed: {e}")
            return None
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _query_receiver_setup(self) -> Optional[Dict[str, Any]]:
        """Query ReceiverSetup SBF block (5902) for receiver identity.

        Extracts model name, firmware version, and serial number from the
        ReceiverSetup block. This identifies the actual hardware connected,
        enabling mismatch detection against configured receiver type.

        Bootstrap approach: tries WITHOUT auth first (pre-5.7 receivers have no
        auth configured and are fully open). Falls back to authenticated request
        if the receiver returns "Not authorized" (fw 5.7.0+).

        ReceiverSetup byte layout (offsets from start of $@ sync):
          0-7:   SBF header (Sync1, Sync2, CRC, ID, Length)
          8-11:  TOW (u4)
         12-13:  WNc (u2)
         14-15:  Reserved (u1[2])   ← 2 bytes, must not skip these
         16-75:  MarkerName (c1[60])
         76-95:  MarkerNumber (c1[20])
         96-115: Observer (c1[20])
        116-155: Agency (c1[40])
        156-175: RxSerialNumber (c1[20])
        176-195: RxName (c1[20]) — IGS/RINEX receiver type code (e.g. "SSRC7")
        196-215: RxVersion (c1[20]) — firmware version
        288-327: GNSSFWVersion (c1[40]) — Rev 2+
        328-367: ProductName (c1[40]) — Rev 2+, human-readable (e.g. "PolaRx5")

        Returns:
            Dictionary with receiver identity or None on failure
        """
        # Try without auth first — pre-5.7 is fully open, no credentials needed.
        # This avoids spurious "wrong username or password" warnings on open receivers
        # and solves the bootstrap problem (need fw version to decide whether to auth).
        sbf_data = self._request_receiver_setup_unauthenticated()
        if sbf_data is None:
            # Receiver requires auth — retry with credentials if configured.
            # Without credentials there's nothing we can do; skip silently so
            # the health check doesn't spam auth warnings for open-but-failing receivers.
            if not self.tcp_username or not self.tcp_password:
                self.logger.debug(
                    "ReceiverSetup unavailable unauthenticated and no credentials configured"
                )
                return None
            sbf_data = self._send_sbf_request(
                "ReceiverSetup", self.BLOCK_RECEIVER_SETUP
            )
        if not sbf_data:
            return None

        try:
            _, length = self._parse_sbf_header(sbf_data)

            # Need at least 216 bytes to extract through RxVersion (196 + 20)
            if length < 216 or len(sbf_data) < 216:
                self.logger.debug(f"ReceiverSetup response too short: {length} bytes")
                return None

            def _extract_string(data: bytes, start: int, size: int) -> str:
                """Extract null-terminated string from fixed-size field."""
                raw = data[start : start + size]
                return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()

            marker_name = _extract_string(sbf_data, 16, 60)
            # MarkerNumber (76-95) carries the IERS DOMES. Read so `cfg
            # reconcile` can show a receiver column for it and flag drift —
            # otherwise `rec-config --set-domes` writes blind, with no way to
            # verify afterwards.
            marker_number = _extract_string(sbf_data, 76, 20)
            serial_number = _extract_string(sbf_data, 156, 20)
            rx_name = _extract_string(sbf_data, 176, 20)
            firmware_version = _extract_string(sbf_data, 196, 20)

            # ProductName (Rev 2+, offset 328) is the human-readable product name
            # e.g. "PolaRx5" — firmware-set, not operator-configurable.
            # Prefer it over RxName ("SSRC7" = IGS code) for model identification.
            receiver_model = rx_name
            if len(sbf_data) >= 368:
                product_name = _extract_string(sbf_data, 328, 40)
                if product_name:
                    receiver_model = product_name

            if not any([serial_number, receiver_model, firmware_version]):
                return None

            identity = {}
            if receiver_model:
                identity["receiver_model"] = receiver_model
            if firmware_version:
                identity["firmware_version"] = firmware_version
            if serial_number:
                identity["serial_number"] = serial_number
            if marker_name:
                identity["marker_name"] = marker_name
            if marker_number:
                identity["marker_number"] = marker_number

            self.logger.info(
                f"Receiver identity: model={receiver_model}, "
                f"firmware={firmware_version}, serial={serial_number}, "
                f"marker={marker_name}"
            )
            return identity

        except Exception as e:
            self.logger.error(f"Error parsing ReceiverSetup: {e}")

        return None

    def _query_logging_sessions(self) -> Optional[Dict[str, Any]]:
        """Query active logging sessions via ASCII command interface.

        Sends 'getLogSession' to list configured sessions and their state.
        Parses the response to identify which sessions are actively logging.

        The response format contains lines like:
            LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
            LogSession, LOG4, Disabled, DSK1, "geod_15m", After1Year, Medium, Continuous
            LogSession, LOG6, Unused, DSK1, "", Never, Medium, Continuous

        Returns:
            Logging sessions dict compatible with db_writer._write_logging_status(),
            or None if query fails or no sessions found.
        """
        response = self._send_ascii_command("getLogSession")
        if not response:
            return None

        return self._parse_log_session_response(response)

    def _parse_log_session_response(self, response: str) -> Optional[Dict[str, Any]]:
        """Parse getLogSession response to extract active sessions.

        Response format (one line per LOG slot):
            LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
            LogSession, LOG2, Enabled, DSK1, "1Hz_1hr", After30Days, High, Continuous
            LogSession, LOG5, Enabled, DSK1, "status_1hr", After1Year, High, Continuous
            LogSession, LOG4, Disabled, DSK1, "geod_15m", ...
            LogSession, LOG6, Unused, DSK1, "", Never, ...

        State field: Enabled/Disabled/Unused

        Known session names are mapped to canonical names used by the
        dashboard (15s_24hr, 1Hz_1hr, status_1hr).

        Args:
            response: Raw text response from receiver

        Returns:
            Logging sessions dict or None
        """
        # Map receiver session names to our canonical names
        session_map = {
            "15s_24hr": "15s_24hr",
            "1hz_1hr": "1Hz_1hr",
            "1Hz_1hr": "1Hz_1hr",
            "status_1hr": "status_1hr",
        }

        active_sessions: List[Dict[str, str]] = []

        for line in response.split("\n"):
            line = line.strip()

            # Look for LogSession lines
            if "LogSession" not in line:
                continue

            # Skip lines that are just the command echo
            if line.startswith("$R: getLogSession"):
                continue

            # Check if session is Enabled (vs Disabled/Unused)
            if "Enabled" not in line:
                continue

            # Extract session name from quoted string
            name_match = re.search(r'"([^"]+)"', line)
            if not name_match:
                continue

            session_name = name_match.group(1)
            if not session_name:
                continue

            # Map to canonical name
            canonical = session_map.get(session_name)
            if not canonical:
                canonical = session_map.get(session_name.lower())
            if not canonical:
                continue

            # Avoid duplicates
            if not any(s["session"] == canonical for s in active_sessions):
                active_sessions.append({"session": canonical})

        if not active_sessions:
            return None

        return {
            "active_sessions": len(active_sessions),
            "sessions": active_sessions,
            "status": "ok",
        }

    def query_antenna_info(self) -> Optional[Dict[str, Any]]:
        """Public alias for :meth:`_query_antenna_info`.

        The antenna probe is excluded from :meth:`extract_health_data` so that
        routine 5-minute health checks across 173 stations don't generate
        unnecessary ASCII-command load. Callers that need antenna metadata —
        currently only ``receivers cfg reconcile`` — invoke this directly.
        """
        return self._query_antenna_info()

    def _query_antenna_info(self) -> Optional[Dict[str, Any]]:
        """Query antenna configuration via getAntennaOffset ASCII command.

        Returns a dict with antenna_type / antenna_radome / antenna_serial /
        antenna_height_delta extracted from the receiver's configured antenna.
        Values are operator-typed on the receiver and not authoritative —
        cfg reconciliation flags mismatches but treats TOS as canonical.

        Response format (one line, fields comma-separated, strings double-quoted):
            setAntennaOffset, Main, 0.0000, 0.0000, 0.0000, "SEPCHOKE_B3E6   SPKE", "262509", "ELEY"

        AntType field is the IGS-standard 20-char code: 16-char antenna name
        (right-padded with spaces) + 4-char radome (right-padded). We split it.
        """
        response = self._send_ascii_command("getAntennaOffset")
        if not response:
            return None

        # Find the AntennaOffset row (the receiver echoes the setX form on read).
        target_line: Optional[str] = None
        for raw in response.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if "AntennaOffset" not in line:
                continue
            if "Main" not in line:
                # Skip auxiliary antenna entries (Aux1, Aux2, etc.)
                continue
            target_line = line
            break

        if not target_line:
            return None

        # Tokenise: comma-separated, strings may be 'single' or "double" quoted.
        # Use a regex that captures quoted or bare tokens.
        tokens = re.findall(r'"([^"]*)"|\'([^\']*)\'|([^,\s][^,]*)', target_line)
        # Each match is a tuple of (double, single, bare); collapse to a flat list.
        flat = [next((t for t in tup if t), "").strip() for tup in tokens]
        # Drop the leading "setAntennaOffset" / "AntennaOffset" / "$R:" prefix tokens
        # so we land on the data fields.
        while flat and flat[0] in ("setAntennaOffset", "AntennaOffset", "$R:"):
            flat = flat[1:]

        # Expected layout after prefix removal (Septentrio setAntennaOffset order):
        #   [0] Source ("Main")
        #   [1] DeltaE
        #   [2] DeltaN
        #   [3] DeltaH  ← vertical / Up component = antenna_height in stations.cfg
        #   [4] AntType (20-char IGS code)
        #   [5] Serial (optional)
        #   [6] Description (optional)
        if len(flat) < 5 or flat[0] != "Main":
            self.logger.debug(f"Unexpected getAntennaOffset format: {target_line!r}")
            return None

        def _f(s: str) -> Optional[float]:
            try:
                return float(s) if s else None
            except ValueError:
                return None

        delta_e = _f(flat[1])
        delta_n = _f(flat[2])
        delta_h = _f(flat[3])
        ant_code = flat[4] if len(flat) > 4 else ""
        serial = flat[5] if len(flat) > 5 else ""

        # Split IGS 20-char antenna code: 16 chars type + 4 chars radome.
        # Some receivers store a shorter string — handle gracefully.
        ant_type: Optional[str] = None
        radome: Optional[str] = None
        if ant_code:
            # Pad to 20 to make slicing safe, then strip each piece.
            padded = ant_code.ljust(20)
            ant_type = padded[:16].strip() or None
            radome = padded[16:20].strip() or None
            # IGS convention: NONE means no radome.
            if radome is None:
                radome = "NONE"

        info: Dict[str, Any] = {}
        if ant_type:
            info["antenna_type"] = ant_type
        if radome is not None:
            info["antenna_radome"] = radome
        if serial:
            info["antenna_serial"] = serial
        if delta_e is not None:
            info["antenna_east_delta"] = round(delta_e, 4)
        if delta_n is not None:
            info["antenna_north_delta"] = round(delta_n, 4)
        if delta_h is not None:
            info["antenna_height_delta"] = round(delta_h, 4)

        if not info:
            return None

        self.logger.debug(f"Antenna info: {info}")
        return info

    def _send_ascii_command(self, command: str) -> Optional[str]:
        """Send an ASCII command to the receiver and return text response.

        Opens a new TCP connection, sends the command, collects the text
        response until the prompt reappears or timeout, then closes.

        Args:
            command: ASCII command string (e.g., 'lst, LogSession')

        Returns:
            Response text or None on failure
        """
        sock = None
        try:
            sock = self._open_socket()

            # Read initial prompt (e.g., "IP11>")
            prompt = sock.recv(1024)
            conn_id = self._parse_connection_id(prompt)
            self.logger.debug(f"ASCII command connection as {conn_id}")

            # fw 5.7.0: authenticate before issuing any command
            if not self._login(sock):
                return None

            # Send command
            sock.sendall((command + "\n").encode("utf-8"))

            # Collect text response until prompt reappears or timeout
            response = b""
            end_time = time.time() + 3.0

            while time.time() < end_time:
                try:
                    sock.settimeout(1.0)
                    chunk = sock.recv(8192)
                    if chunk:
                        response += chunk

                        # Check if response ends with prompt (IPxx>)
                        decoded = response.decode("utf-8", errors="ignore")
                        if re.search(r"IP\d+>", decoded[-30:]):
                            break
                except TimeoutError:
                    if response:
                        break

            return response.decode("utf-8", errors="ignore")

        except TimeoutError:
            self.logger.debug(f"Timeout sending ASCII command: {command}")
            return None
        except ConnectionRefusedError:
            self.logger.debug(
                f"Connection refused for ASCII command to {self.host}:{self.port}"
            )
            return None
        except Exception as e:
            self.logger.debug(f"ASCII command '{command}' failed: {e}")
            return None
        finally:
            if sock:
                sock.close()

    def _query_disk_status(self) -> Optional[Dict[str, Any]]:
        """Query DiskStatus SBF block (4059) for disk usage.

        The DiskStatus sub-block layout varies by firmware revision and cannot
        be reliably parsed with hardcoded offsets (the sub-block contains a
        float32, status bitmask, 64-bit usage-in-bytes, and size-in-MB whose
        positions differ from the SBF v1 documentation).

        We delegate to bin2asc, the reference Septentrio parser, which handles
        all revisions correctly.  The SBF block is only ~52 bytes so the
        subprocess overhead is negligible (<10 ms).

        Returns:
            Dictionary with disk metrics or None on failure
        """
        sbf_data = self._send_sbf_request("DiskStatus", self.BLOCK_DISK_STATUS)
        if not sbf_data:
            return None

        return self._parse_disk_via_bin2asc(sbf_data)

    def _parse_disk_via_bin2asc(self, sbf_data: bytes) -> Optional[Dict[str, Any]]:
        """Parse DiskStatus SBF block using bin2asc.

        Delegates SBF→CSV conversion to the shared parse_sbf_bytes() utility,
        then applies DiskStatus-specific domain logic (aggregation, worst-status).

        Returns:
            Dictionary with disk metrics or None on failure
        """
        try:
            from ..utils.rxtools_extractor import parse_sbf_bytes
        except ImportError:
            self.logger.debug(
                "rxtools_extractor not available, falling back to header-only"
            )
            return self._parse_disk_header_only(sbf_data)

        try:
            rows = parse_sbf_bytes(sbf_data, "DiskStatus")
        except FileNotFoundError:
            self.logger.debug(
                "bin2asc not available, falling back to header-only parse"
            )
            return self._parse_disk_header_only(sbf_data)

        if not rows:
            self.logger.debug("bin2asc produced no DiskStatus output")
            return None

        disks: List[Dict[str, Any]] = []
        total_mb_sum = 0.0
        used_mb_sum = 0.0

        for row in rows:
            disk_id = int(row.get("DiskID") or 0)
            mounted = (row.get("DISK_MOUNTED") or 0) == 1
            disk_full = (row.get("DISK_FULL") or 0) == 1
            disk_size_mb = float(row.get("DiskSize [MB]") or 0)
            usage_pct = float(row.get("DiskUsagePercent [%]") or 0)
            error_str = row.get("Error", "")

            if mounted:
                status = "full" if disk_full else "mounted"
            else:
                status = "unmounted"
            if error_str and error_str != "No error":
                status = "error"

            disk_info: Dict[str, Any] = {
                "disk_id": disk_id,
                "status": status,
                "usage_percent": round(usage_pct, 2),
                "total_mb": round(disk_size_mb, 1),
            }
            if mounted and disk_size_mb > 0:
                used = round(disk_size_mb * usage_pct / 100, 1)
                disk_info["used_mb"] = used
                total_mb_sum += disk_size_mb
                used_mb_sum += used

            disks.append(disk_info)

        if not disks:
            return {"status": "unavailable", "disks": []}

        worst = "mounted"
        priority = {"mounted": 0, "full": 1, "unmounted": 2, "error": 3}
        for d in disks:
            if priority.get(d["status"], 3) > priority.get(worst, 0):
                worst = d["status"]

        result_dict: Dict[str, Any] = {"status": worst, "disks": disks}
        if total_mb_sum > 0:
            result_dict["total_mb"] = round(total_mb_sum, 1)
            result_dict["used_mb"] = round(used_mb_sum, 1)
            result_dict["usage_percent"] = round(used_mb_sum / total_mb_sum * 100, 2)
        else:
            result_dict["total_mb"] = 0
            result_dict["used_mb"] = 0
            result_dict["usage_percent"] = 0.0

        return result_dict

    def _parse_disk_header_only(self, sbf_data: bytes) -> Optional[Dict[str, Any]]:
        """Minimal DiskStatus parse when bin2asc is unavailable.

        Extracts only the fields at known stable offsets:
        - N and SBLength at bytes 14-15
        - DiskID at sub-block byte 4
        - Status bitmask at sub-block byte 5 (bit 0 = DISK_MOUNTED)
        - DiskSize [MB] at sub-block bytes 12-15 (uint32)

        DiskUsagePercent requires bin2asc (it is computed from a 64-bit
        usage-in-bytes field whose offset varies by firmware).

        **Limitation — full/error states are not detectable from header
        bytes alone.** bin2asc derives ``DISK_FULL`` from the usage
        computation and ``Error`` from internal fields whose layout
        varies. Callers consuming this fallback should treat ``mounted``
        as best-effort and surface ``limited_check=True`` so downstream
        (Icinga, dashboards) can flag the limitation if appropriate.
        """
        try:
            _, length = self._parse_sbf_header(sbf_data)
            if length < 16 or len(sbf_data) < 16:
                return None

            n_disks = sbf_data[14]
            sb_length = sbf_data[15]
            if n_disks == 0 or sb_length < 16:
                return {"status": "unavailable", "disks": [], "limited_check": True}

            disks: List[Dict[str, Any]] = []
            total_mb_sum = 0.0
            for i in range(n_disks):
                offset = 16 + i * sb_length
                if offset + 16 > len(sbf_data):
                    break
                disk_id = sbf_data[offset + 4]
                status_flags = sbf_data[offset + 5]
                mounted = bool(status_flags & 0x01)
                disk_size_mb = struct.unpack_from("<I", sbf_data, offset + 12)[0]

                disk_info: Dict[str, Any] = {
                    "disk_id": disk_id,
                    "status": "mounted" if mounted else "unmounted",
                    "total_mb": round(float(disk_size_mb), 1),
                }
                if mounted and disk_size_mb > 0:
                    total_mb_sum += disk_size_mb
                disks.append(disk_info)

            if not disks:
                return {"status": "unavailable", "disks": [], "limited_check": True}

            self.logger.warning(
                "DiskStatus parsed via header-only fallback (no bin2asc) — "
                "full/error states cannot be detected; status is best-effort."
            )

            result: Dict[str, Any] = {
                "status": (
                    "mounted"
                    if any(d["status"] == "mounted" for d in disks)
                    else "unmounted"
                ),
                "disks": disks,
                "total_mb": round(total_mb_sum, 1),
                # Flag for downstream consumers: this status didn't go
                # through bin2asc, so full/error states may be missed.
                "limited_check": True,
            }
            return result

        except Exception as e:
            self.logger.error(f"Error in header-only DiskStatus parse: {e}")
            return None

    def _send_sbf_request(
        self, block_name: str, expected_block_id: Optional[int] = None
    ) -> Optional[bytes]:
        """Send SBF request and receive response.

        Args:
            block_name: Name of SBF block to request (e.g., "PowerStatus")
            expected_block_id: Optional block ID to search for. If provided,
                              scans the response for this specific block ID.
                              This handles receivers with continuous SBF output.

        Returns:
            Raw SBF data bytes or None on failure
        """
        if self._auth_failed:
            return None  # Auth known to be failing — suppress per-block warnings
        sock = None
        try:
            sock = self._open_socket()

            # Read initial prompt to detect connection ID (e.g., "IP11>")
            prompt = sock.recv(1024)
            conn_id = self._parse_connection_id(prompt)

            # fw 5.7.0: authenticate before issuing any command
            if not self._login(sock):
                return None

            # Send SBF once request using detected connection ID
            # The esoc command streams SBF data to the specified connection
            cmd = f"esoc, {conn_id}, {block_name}\n"
            sock.sendall(cmd.encode())

            # Collect data and scan for expected block
            # Use overall timeout to avoid hanging on receivers with continuous output
            response = b""
            end_time = time.time() + 2.0  # 2 second total timeout

            while time.time() < end_time:
                try:
                    sock.settimeout(0.5)
                    chunk = sock.recv(8192)
                    if chunk:
                        response += chunk

                        # If we have a specific block to find, scan after each receive
                        if expected_block_id is not None:
                            result = self._find_sbf_block(response, expected_block_id)
                            if result is not None:
                                return result
                        else:
                            # No specific block, return first SBF found
                            sync_pos = response.find(b"$@")
                            if sync_pos >= 0:
                                return response[sync_pos:]
                except TimeoutError:
                    # On receivers without continuous output, timeout means no more data
                    if len(response) == 0:
                        continue  # Keep waiting if we haven't received anything yet
                    # If we've received data but no more coming, check what we have
                    if expected_block_id is None:
                        sync_pos = response.find(b"$@")
                        if sync_pos >= 0:
                            return response[sync_pos:]
                    break

            # Final check of accumulated data
            if expected_block_id is not None:
                result = self._find_sbf_block(response, expected_block_id)
                if result is not None:
                    return result

            decoded_resp = response.decode("utf-8", errors="ignore")
            if "Not authorized" in decoded_resp:
                if not self._auth_failed:
                    self.logger.warning(
                        f"TCP command denied for {self.station_id} ({block_name}): "
                        f"receiver requires authentication — verify tcp_username/tcp_password "
                        f"in receivers.cfg and receiver_firmware_version in stations.cfg"
                    )
                    self._auth_failed = True
                return None

            if expected_block_id is not None:
                self.logger.warning(
                    f"Block ID {expected_block_id} not found in response for {block_name}"
                )
            else:
                self.logger.warning(f"No SBF sync found in {block_name} response")

            return None

        except TimeoutError:
            self.logger.error(f"Timeout querying {block_name}")
            return None
        except ConnectionRefusedError:
            self.logger.error(f"Connection refused to {self.host}:{self.port}")
            return None
        except Exception as e:
            self.logger.error(f"Error querying {block_name}: {e}")
            return None
        finally:
            if sock:
                sock.close()

    def _find_sbf_block(self, data: bytes, expected_block_id: int) -> Optional[bytes]:
        """Scan data for a specific SBF block ID.

        Args:
            data: Raw bytes to scan
            expected_block_id: The SBF block ID to find

        Returns:
            SBF data starting at the found block, or None if not found
        """
        pos = 0
        while pos < len(data) - 8:
            sync_pos = data.find(b"$@", pos)
            if sync_pos < 0:
                break

            # Need at least 8 bytes for SBF header
            if sync_pos + 8 > len(data):
                break

            # Parse block ID (lower 13 bits of bytes 4-5)
            id_rev = struct.unpack("<H", data[sync_pos + 4 : sync_pos + 6])[0]
            block_id = id_rev & 0x1FFF
            length = struct.unpack("<H", data[sync_pos + 6 : sync_pos + 8])[0]

            if block_id == expected_block_id:
                return data[sync_pos:]

            # Move to next potential SBF block
            pos = sync_pos + max(length, 8)

        return None

    def _login(self, sock: socket.socket) -> bool:
        """Authenticate TCP session for fw 5.7.0+.

        Sends `login, <user>, <pw>` and drains the response plus the new
        prompt that follows before returning control to the caller.

        Behaviour by firmware:
        - fw 5.7.0: returns `$R! LogIn` + body + new prompt → True
        - fw 5.7.0, wrong creds: returns `$R? LogIn: Wrong username or password!` → True
          (commands will fail with $E: Not authorized! — health blocks return None)
        - fw ≤5.5.0: returns `$E: Invalid command!` (login unknown) → True (proceed unauthenticated)
        - No credentials configured: no-op → True

        Returns:
            True if session is ready to accept commands, False on definitive auth error
            that means proceeding is pointless.
        """
        if not self.tcp_username or not self.tcp_password:
            if self.firmware_version and _firmware_requires_auth(self.firmware_version):
                if not self._auth_failed:
                    self.logger.warning(
                        f"TCP credentials not configured for {self.station_id} "
                        f"(fw {self.firmware_version} requires authentication) — "
                        f"health data unavailable; set tcp_username/tcp_password in receivers.cfg"
                    )
                    self._auth_failed = True
            return True
        if self._auth_failed:
            # Previous attempt in this health check cycle failed — skip to avoid lockout.
            return True

        # Always attempt login when credentials are configured — pre-5.7 receivers
        # respond with "$E: Invalid command!" which is handled below, and 5.7.0+
        # accept it normally. Skipping based on the stations.cfg firmware version
        # silently breaks auth whenever the recorded version is stale (e.g. after
        # a firmware upgrade that wasn't reflected in stations.cfg).

        cmd = f"login, {self.tcp_username}, {self.tcp_password}\n"
        sock.sendall(cmd.encode("utf-8"))

        # Drain login response + subsequent prompt (IPxx>)
        response = b""
        end_time = time.time() + 3.0
        while time.time() < end_time:
            try:
                sock.settimeout(1.0)
                chunk = sock.recv(4096)
                if chunk:
                    response += chunk
                    decoded = response.decode("utf-8", errors="ignore")
                    if re.search(r"IP\d+>", decoded[-30:]):
                        break
            except TimeoutError:
                if response:
                    break

        decoded = response.decode("utf-8", errors="ignore")

        if "$R! LogIn" in decoded or "$R: login" in decoded:
            self.logger.debug(f"TCP login successful for {self.station_id}")
            return True
        elif "$E: Invalid command" in decoded:
            # fw ≤5.5.0 — login command did not exist; unauthenticated access is normal
            self.logger.debug(
                f"Login not recognised by {self.station_id} — assuming fw≤5.5.0, proceeding"
            )
            return True
        elif (
            "Wrong username or password" in decoded
            or "Too many failed login" in decoded
        ):
            fw = self.firmware_version
            if fw and _firmware_requires_auth(fw):
                # Known fw ≥5.7 station with wrong credentials — real auth failure.
                # Set _auth_failed so subsequent block queries don't waste time.
                self._auth_failed = True
                self.logger.warning(
                    f"TCP auth failed for {self.station_id}: wrong username or password"
                )
            else:
                # Pre-5.7 firmware: login command exists but auth is not enforced.
                # A "wrong password" response does NOT lock out unauthenticated SBF
                # queries — do NOT set _auth_failed or subsequent blocks will silently
                # return None.
                self.logger.debug(
                    f"TCP login not accepted by {self.station_id} "
                    f"(fw {fw or 'unknown'}), proceeding unauthenticated"
                )
            return True
        else:
            if decoded.strip():
                self.logger.debug(
                    f"Unexpected login response for {self.station_id}: {decoded[:100]!r}"
                )
            return True

    def _parse_connection_id(self, prompt: bytes) -> str:
        """Parse connection ID from receiver prompt.

        Args:
            prompt: Initial prompt bytes from receiver (e.g., b'IP11>')

        Returns:
            Connection identifier string (e.g., "IP11")
        """
        # Prompt format is typically "IPxx>" where xx is connection number
        try:
            prompt_str = prompt.decode("ascii", errors="ignore").strip()
            if prompt_str.startswith("IP") and prompt_str.endswith(">"):
                return prompt_str[:-1]  # Remove trailing '>'
        except Exception:
            pass
        # Default fallback
        return "IP11"

    def _parse_sbf_header(self, sbf_data: bytes) -> Tuple[int, int]:
        """Parse SBF message header.

        Args:
            sbf_data: Raw SBF data starting with sync bytes

        Returns:
            Tuple of (message_id, length)
        """
        # SBF header structure:
        # Bytes 0-1: Sync ($@)
        # Bytes 2-3: CRC
        # Bytes 4-5: ID + Revision (lower 13 bits = ID)
        # Bytes 6-7: Length
        id_rev = struct.unpack("<H", sbf_data[4:6])[0]
        message_id = id_rev & 0x1FFF
        length = struct.unpack("<H", sbf_data[6:8])[0]
        return message_id, length

    def _check_voltage_status(self, voltage: float) -> str:
        """Check voltage status against centralized thresholds."""
        result = self.metric_checker.check_voltage(voltage)
        return result.status.value

    def _check_cpu_status(self, cpu_load: int) -> str:
        """Check CPU load status against centralized thresholds."""
        result = self.metric_checker.check_cpu_load(cpu_load)
        return result.status.value

    def _check_temperature_status(self, temperature: float) -> str:
        """Check temperature status against centralized thresholds."""
        result = self.metric_checker.check_temperature(temperature)
        return result.status.value

    def test_connection(self) -> bool:
        """Test if TCP connection to receiver works.

        Returns:
            True if connection successful, False otherwise
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            return True
        except Exception as e:
            self.logger.debug(f"Connection test failed: {e}")
            return False
        finally:
            if sock is not None:
                sock.close()

    def _check_port_status(self) -> Dict[str, Any]:
        """Check status of configured ports (FTP, HTTP, control).

        Uses 5s timeout and retries once on timeout to reduce false negatives
        on slow 3G/4G links.  'refused' is definitive and never retried.

        Returns:
            Dictionary with port status for each configured port.
            Status values: 'open', 'timeout', 'refused', 'error'
        """
        port_status = {}
        all_ok = True

        for name, port_num in self.port_config.items():
            result_entry = self._check_single_port(name, port_num)
            # Retry once on timeout or refused — on lossy 3G/4G links,
            # both can be spurious (NAT routers may RST during packet loss)
            if result_entry["detail"] in ("timeout", "refused"):
                self.logger.debug(
                    f"Port {name}:{port_num} {result_entry['detail']}, retrying once..."
                )
                result_entry = self._check_single_port(name, port_num)
            port_status[name] = result_entry
            if not result_entry["open"]:
                all_ok = False

        port_status["overall_status"] = "ok" if all_ok else "warning"
        return port_status

    def _check_single_port(self, _name: str, port_num: int) -> Dict[str, Any]:
        """Check a single TCP port with 5s timeout.

        Returns:
            Dictionary with port, open, status, detail keys.
        """
        import errno

        def _port_result(is_open: bool, status: str, detail: str) -> Dict[str, Any]:
            return {
                "port": port_num,
                "open": is_open,
                "status": status,
                "detail": detail,
            }

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)  # 5s for slow 3G/4G links
            result = sock.connect_ex((self.host, port_num))

            if result == 0:
                return _port_result(True, "ok", "open")
            elif result == errno.ECONNREFUSED:
                return _port_result(False, "warning", "refused")
            else:
                return _port_result(False, "critical", "timeout")
        except TimeoutError:
            return _port_result(False, "critical", "timeout")
        except ConnectionRefusedError:
            return _port_result(False, "warning", "refused")
        except Exception as e:
            return _port_result(False, "error", str(e))
        finally:
            if sock is not None:
                sock.close()

    def _query_pvt_geodetic(self) -> Optional[Dict[str, Any]]:
        """Query PVTGeodetic2 SBF block for position and accuracy.

        Returns:
            Dictionary with position data or None on failure
        """
        # Note: Use "PVTGeodetic" command name (receiver responds to this, not "PVTGeodetic2")
        sbf_data = self._send_sbf_request("PVTGeodetic", self.BLOCK_PVT_GEODETIC2)
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            # PVTGeodetic2 structure:
            # Bytes 0-7: SBF header
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: Mode (uint8)
            # Byte 15: Error (uint8)
            # Bytes 16-23: Latitude (float64, radians)
            # Bytes 24-31: Longitude (float64, radians)
            # Bytes 32-39: Height (float64, meters above ellipsoid)
            # Bytes 40-43: Undulation (float32)
            # Bytes 44-47: Vn (float32, north velocity)
            # Bytes 48-51: Ve (float32, east velocity)
            # Bytes 52-55: Vu (float32, up velocity)
            # Bytes 56-59: COG (float32, course over ground)
            # Bytes 60-67: RxClkBias (float64)
            # Bytes 68-71: RxClkDrift (float32)
            # Byte 72: TimeSystem (uint8)
            # Byte 73: Datum (uint8)
            # Byte 74: NrSV (uint8, number of satellites used)
            # Byte 75: WACorrInfo (uint8)
            # Bytes 76-77: ReferenceID (uint16)
            # Bytes 78-79: MeanCorrAge (uint16)
            # Bytes 80-83: SignalInfo (uint32)
            # Byte 84: AlertFlag (uint8)
            # Byte 85: NrBases (uint8)
            # Bytes 86-87: PPPInfo (uint16)
            # Bytes 88-89: Latency (uint16)
            # Bytes 90-91: HAccuracy (uint16, mm)
            # Bytes 92-93: VAccuracy (uint16, mm)

            import math

            if length >= 94 and len(sbf_data) >= 94:
                # Extract position
                lat_rad = struct.unpack("<d", sbf_data[16:24])[0]
                lon_rad = struct.unpack("<d", sbf_data[24:32])[0]
                height = struct.unpack("<d", sbf_data[32:40])[0]

                # Convert radians to degrees
                lat_deg = math.degrees(lat_rad)
                lon_deg = math.degrees(lon_rad)

                # Check for Do-Not-Use values (NaN or very large)
                if math.isnan(lat_deg) or abs(lat_deg) > 90:
                    return None
                if math.isnan(lon_deg) or abs(lon_deg) > 180:
                    return None

                # Extract accuracy (in mm, convert to m)
                h_accuracy_mm = struct.unpack("<H", sbf_data[90:92])[0]
                v_accuracy_mm = struct.unpack("<H", sbf_data[92:94])[0]

                # Number of satellites used
                nr_sv = sbf_data[74]

                # Fix mode
                mode = sbf_data[14]
                mode_names = {
                    0: "no_fix",
                    1: "standalone",
                    2: "dgps",
                    3: "fixed",  # RTK fixed
                    4: "float",  # RTK float
                    5: "sbas",
                    6: "ppp",
                }

                return {
                    "latitude": round(lat_deg, 8),
                    "longitude": round(lon_deg, 8),
                    "height": round(height, 3),
                    "h_accuracy_m": (
                        round(h_accuracy_mm / 1000.0, 3)
                        if h_accuracy_mm < 65535
                        else None
                    ),
                    "v_accuracy_m": (
                        round(v_accuracy_mm / 1000.0, 3)
                        if v_accuracy_mm < 65535
                        else None
                    ),
                    "satellites_used": nr_sv,
                    "fix_mode": mode_names.get(mode, f"unknown_{mode}"),
                    "status": "ok" if mode >= 1 else "warning",
                }

        except Exception as e:
            self.logger.error(f"Error parsing PVTGeodetic2: {e}")

        return None

    def _query_satellite_tracking(self) -> Optional[Dict[str, Any]]:
        """Query PVTSatCartesian SBF block for satellites used in position solution.

        Uses PVTSatCartesian (4008) instead of ChannelStatus (4013) because
        ChannelStatus reports ALL allocated channels including disabled constellations,
        while PVTSatCartesian reports only satellites actually used in the PVT solution.
        This matches what the receiver's web interface displays.

        Returns:
            Dictionary with satellite counts per constellation or None on failure
        """
        sbf_data = self._send_sbf_request(
            "PVTSatCartesian", self.BLOCK_PVT_SAT_CARTESIAN
        )
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            # PVTSatCartesian structure:
            # Bytes 0-7: SBF header
            # Bytes 8-11: TOW
            # Bytes 12-13: WNc
            # Byte 14: N (number of satellites used in PVT solution)
            # Byte 15: SBLength (size of each SatInfo sub-block)
            # Followed by N SatInfo sub-blocks

            if length < 16 or len(sbf_data) < 16:
                return None

            n_satellites = sbf_data[14]
            sb_length = sbf_data[15]

            if n_satellites == 0:
                return {"total": 0, "by_constellation": {}, "status": "warning"}

            # Count satellites by constellation
            tracking_counts: Dict[str, int] = {}

            offset = 16  # Start of first SatInfo

            for _ in range(n_satellites):
                if offset + sb_length > len(sbf_data):
                    break

                # SatInfo structure:
                # Byte 0: SVID (satellite vehicle ID)
                svid = sbf_data[offset]

                # Determine constellation from SVID ranges
                const_name = self._svid_to_constellation(svid)

                if const_name is not None:
                    if const_name not in tracking_counts:
                        tracking_counts[const_name] = 0
                    tracking_counts[const_name] += 1

                offset += sb_length

            return {
                "total": n_satellites,
                "by_constellation": tracking_counts,
                "status": "ok" if n_satellites >= 4 else "warning",
            }

        except Exception as e:
            self.logger.error(f"Error parsing PVTSatCartesian: {e}")

        return None

    @staticmethod
    def _svid_to_constellation(svid: int) -> Optional[str]:
        """Convert Septentrio SVID to constellation name.

        SVID ranges per Septentrio SBF Reference Guide v4.x — chosen to be
        non-overlapping (IRNSS 191-197 takes priority over the legacy
        wider QZSS range, since modern firmware uses these allocations):

        - GPS: 1-37 (PRN 1-32 + reserved)
        - GLONASS: 38-62 (slot 1-24 + reserved)
        - Galileo: 63-106 (E01-E36, some firmware uses 63-68 for E33-E36)
        - SBAS: 120-140
        - BeiDou: 141-180 (legacy) and 201-263 (newer firmware C01-C63)
        - QZSS: 181-187 (J01-J07; the only operational QZSS satellites)
        - IRNSS/NavIC: 191-197 (I01-I07)
        - SVIDs 188-190 and 198-200 are reserved/unused — return Unknown
        - Invalid: 0, 255

        Args:
            svid: Satellite Vehicle ID from ChannelStatus

        Returns:
            Constellation name string, or None for invalid SVIDs
        """
        # Invalid SVIDs
        if svid == 0 or svid == 255:
            return None
        elif 1 <= svid <= 37:
            return "GPS"
        elif 38 <= svid <= 62:
            return "GLONASS"
        elif 63 <= svid <= 106:
            return "Galileo"
        elif 120 <= svid <= 140:
            return "SBAS"
        elif 141 <= svid <= 180:
            return "BeiDou"
        elif 181 <= svid <= 187:
            return "QZSS"
        elif 191 <= svid <= 197:
            return "IRNSS"
        elif 201 <= svid <= 263:
            return "BeiDou"
        else:
            return f"Unknown_{svid}"
