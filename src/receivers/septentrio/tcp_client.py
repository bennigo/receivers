"""Unified TCP client for Septentrio PolaRX5 receiver communication.

This module provides a unified interface for TCP communication with Septentrio
receivers, supporting both text commands (configuration) and binary SBF data
(health extraction).

TCP Command Interface (Port 28784):
- Text commands return responses prefixed with $R: (success) or $E: (error)
- Binary SBF requests use 'esoc' command, return data with $@ sync pattern
- Connection prompt format: "IPxx>" where xx is connection number

Key Commands:
- lstConfigFile, {Current|Boot} - Extract full configuration
- eccf, Current, Boot - Save current config to boot
- gso, all - Get all SBF output settings
- sso, Stream, Target, Blocks, Interval - Set SBF output
- esoc, ConnID, BlockName - Request single SBF block

Usage:
    client = PolaRX5TCPClient('10.6.1.201', 'ISFS')

    # Text commands
    response = client.send_command('gso, all')

    # Extract config
    config = client.extract_config('Current')

    # Push config
    client.push_commands(['sso, Stream1, LOG1', 'eccf, Current, Boot'])

    # Binary SBF request
    sbf_data = client.request_sbf_block('PowerStatus', expected_id=4101)
"""

import logging
import re
import socket
import ssl
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .fw_policy import firmware_requires_auth, should_attempt_login

logger = logging.getLogger(__name__)

# Default TCP command ports for Septentrio receivers
DEFAULT_CONTROL_PORT = 28784  # Plaintext
DEFAULT_SECURE_CONTROL_PORT = 28783  # TLS (same protocol, wrapped)
DEFAULT_TIMEOUT = 10.0


class PolaRX5TCPClient:
    """Unified TCP client for PolaRX5 receiver communication.

    Handles both text command/response and binary SBF data communication.
    """

    def __init__(
        self,
        host: str,
        station_id: str = "UNKNOWN",
        port: int = DEFAULT_CONTROL_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = False,
        firmware_version: Optional[str] = None,
    ):
        """Initialize TCP client.

        Args:
            host: Receiver IP address or hostname
            station_id: Station identifier for logging
            port: TCP command port (default 28784; use 28783 with use_tls=True)
            timeout: Socket timeout in seconds
            username: TCP login username for fw 5.7.0+ (None = skip auth)
            password: TCP login password for fw 5.7.0+
            use_tls: Wrap connection in TLS (for port 28783 when receiver is in secure mode)
            firmware_version: The station's firmware from stations.cfg. Decides
                whether a rejected login is a real failure (fw ≥5.7.0, where
                auth is enforced) or benign (pre-5.7, where the login command
                exists but unauthenticated commands still work). Without it
                every pre-5.7 push logs a misleading "wrong credentials"
                warning and then succeeds anyway. ``None`` = assume auth is
                required, the safe default.
        """
        self.host = host
        self.station_id = station_id
        self.port = port
        self.timeout = timeout
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.firmware_version = firmware_version
        self.logger = logging.getLogger(f"receivers.septentrio.tcp.{station_id}")
        self._sock: Optional[socket.socket] = None
        self._conn_id: Optional[str] = None
        #: True only when auth was REQUIRED (fw ≥5.7.0) and was rejected. Callers
        #: must not report success in that case — the receiver will have refused
        #: every command that followed.
        self.auth_failed = False

    def connect(self) -> bool:
        """Establish TCP connection to receiver.

        Returns:
            True if connected successfully
        """
        try:
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(self.timeout)
            raw.connect((self.host, self.port))

            if self.use_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._sock = ctx.wrap_socket(raw)
                self.logger.debug(f"TLS connected ({self._sock.version()})")  # type: ignore[attr-defined]
            else:
                self._sock = raw

            # Read initial prompt to get connection ID
            prompt = self._sock.recv(1024).decode("utf-8", errors="ignore")
            self._conn_id = self._parse_connection_id(prompt)
            self.logger.debug(f"Connected as {self._conn_id}")

            # fw 5.7.0: authenticate if credentials are configured
            self._login()
            return True

        except TimeoutError:
            self.logger.error(f"Connection timeout to {self.host}:{self.port}")
            return False
        except ConnectionRefusedError:
            self.logger.error(f"Connection refused to {self.host}:{self.port}")
            return False
        except Exception as e:
            self.logger.error(f"Connection error: {e}")
            return False

    def _login(self) -> None:
        """Send login command for fw 5.7.0+ authentication.

        No-op if username/password are not set.
        Tolerates fw ≤5.5.0 ('Invalid command!' → old firmware, proceed).
        """
        if not self.username or not self.password or not self._sock:
            return

        # Known pre-5.7 receiver: auth is not enforced, so a login buys nothing
        # and a rejected one still feeds the brute-force counter that produces
        # the ~9h global lockout after an upgrade. See fw_policy.
        if not should_attempt_login(self.firmware_version):
            self.logger.debug(
                f"Skipping login for {self.station_id} "
                f"(fw {self.firmware_version} does not enforce auth)"
            )
            return

        cmd = f"login, {self.username}, {self.password}\n"
        self._sock.sendall(cmd.encode("utf-8"))

        response = b""
        end_time = time.time() + 3.0
        while time.time() < end_time:
            try:
                self._sock.settimeout(1.0)
                chunk = self._sock.recv(4096)
                if chunk:
                    response += chunk
                    decoded = response.decode("utf-8", errors="ignore")
                    if decoded.rstrip().endswith(">") and "IP" in decoded[-20:]:
                        break
            except TimeoutError:
                if response:
                    break

        decoded = response.decode("utf-8", errors="ignore")
        if "$R! LogIn" in decoded or "$R: login" in decoded:
            self.logger.debug(f"TCP login successful for {self.station_id}")
        elif "$E: Invalid command" in decoded:
            self.logger.debug(
                f"Login not recognised by {self.station_id} — fw≤5.5.0, proceeding"
            )
        elif (
            "Wrong username or password" in decoded
            or "Too many failed login" in decoded
        ):
            # Gate on the firmware in stations.cfg, mirroring
            # polarx5_tcp_extractor._firmware_requires_auth. Pre-5.7 receivers
            # accept the `login` command but do NOT enforce it, so a rejection
            # there is benign and the commands that follow still apply — warning
            # about "wrong credentials" and then reporting success was simply
            # misleading (seen on NYLA, still fw 5.6.0).
            if firmware_requires_auth(self.firmware_version):
                self.auth_failed = True
                self.logger.error(
                    f"TCP auth failed for {self.station_id}: wrong credentials "
                    f"(fw {self.firmware_version or 'unknown'} enforces auth — "
                    f"commands will be REJECTED; check tcp_username/tcp_password "
                    f"in receivers.cfg)"
                )
            else:
                self.logger.debug(
                    f"TCP login not accepted by {self.station_id} "
                    f"(fw {self.firmware_version}), proceeding unauthenticated"
                )

    def disconnect(self) -> None:
        """Close TCP connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            self._conn_id = None

    def _parse_connection_id(self, prompt: str) -> str:
        """Parse connection ID from receiver prompt.

        Args:
            prompt: Initial prompt string (e.g., "IP11>", possibly with binary SBF trailer)

        Returns:
            Connection ID (e.g., "IP11")
        """
        m = re.search(r"IP(\d+)", prompt)
        return f"IP{m.group(1)}" if m else "IP11"

    def send_command(self, command: str, wait_time: float = 0.1) -> str:
        """Send a text command and receive response.

        Args:
            command: Command string (without newline)
            wait_time: Time to wait for response

        Returns:
            Response string

        Raises:
            ConnectionError: If not connected
        """
        if not self._sock:
            raise ConnectionError("Not connected")

        command = command.strip()
        self._sock.send((command + "\n").encode("utf-8"))
        time.sleep(wait_time)

        try:
            response = self._sock.recv(8192).decode("utf-8", errors="ignore")
            return response
        except TimeoutError:
            return ""

    def send_commands(
        self, commands: List[str], wait_time: float = 0.1
    ) -> List[Tuple[str, str]]:
        """Send multiple commands and collect responses.

        Args:
            commands: List of command strings
            wait_time: Time to wait between commands

        Returns:
            List of (command, response) tuples
        """
        results = []
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd or cmd.startswith("#"):
                continue  # Skip empty lines and comments
            response = self.send_command(cmd, wait_time)
            results.append((cmd, response))

            # Check for errors
            if "$E:" in response:
                self.logger.warning(f"Error in response to '{cmd}': {response}")

        return results

    def extract_config(self, config_type: str = "Current") -> str:
        """Extract configuration from receiver.

        Args:
            config_type: "Current" or "Boot"

        Returns:
            Configuration as command list string (cleaned)
        """
        if not self._sock:
            if not self.connect():
                raise ConnectionError(f"Cannot connect to {self.host}:{self.port}")

        assert self._sock is not None  # For type checker

        # Request config with longer wait for multi-block response
        self._sock.send(f"lstConfigFile, {config_type}\n".encode())

        # Collect all response blocks - config can be large and comes in chunks
        response = b""
        end_time = time.time() + 10.0  # 10 second timeout for full config
        consecutive_timeouts = 0

        while time.time() < end_time:
            try:
                self._sock.settimeout(1.0)
                chunk = self._sock.recv(16384)
                if chunk:
                    response += chunk
                    consecutive_timeouts = 0

                    # Check if we've received the end - look for prompt at end of response
                    # The prompt format is "IPxx>" at the end after all data
                    decoded = response.decode("utf-8", errors="ignore")
                    # Look for final prompt pattern (not followed by more data)
                    if decoded.rstrip().endswith(">") and "IP" in decoded[-20:]:
                        # Double-check it's not a block separator
                        if not decoded.rstrip().endswith("---->"):
                            break
            except TimeoutError:
                consecutive_timeouts += 1
                # After 2 consecutive timeouts with data, assume we're done
                if response and consecutive_timeouts >= 2:
                    break

        config = self._parse_config_response(response.decode("utf-8", errors="ignore"))

        # Validate we got a complete config
        if not config or len(config.split("\n")) < 5:
            self.logger.warning(
                f"Config appears incomplete: only {len(config.split(chr(10)))} lines"
            )

        return config

    def _parse_config_response(self, response: str) -> str:
        """Parse lstConfigFile response into clean command list.

        Args:
            response: Raw response from lstConfigFile command

        Returns:
            Clean command list (one per line)
        """
        lines = []
        in_config = False

        for line in response.split("\n"):
            line = line.strip()

            # Skip block markers and headers
            if line.startswith("$R;") or line.startswith("$--") or line == "---->":
                in_config = True
                continue

            # Skip prompts and empty lines
            if line.startswith("IP") and line.endswith(">"):
                continue
            if not line:
                continue

            # Skip comment header lines from lstConfigFile
            if line.startswith("# Configuration File") or line.startswith(
                "# Different from"
            ):
                continue

            # Include command lines (may have leading spaces)
            if in_config and (
                line.startswith("set")
                or line.startswith("# set")
                or line.lstrip().startswith("set")
                or line.lstrip().startswith("# set")
            ):
                lines.append(line.lstrip())

        return "\n".join(lines)

    def push_config(
        self, commands: List[str], save_to_boot: bool = True
    ) -> Tuple[bool, List[str]]:
        """Push configuration commands to receiver.

        Args:
            commands: List of command strings
            save_to_boot: Whether to save to boot config after

        Returns:
            Tuple of (success, error_messages)
        """
        if not self._sock:
            if not self.connect():
                return False, [f"Cannot connect to {self.host}:{self.port}"]

        errors = []

        # Send commands
        for cmd, response in self.send_commands(commands):
            if "$E:" in response:
                errors.append(f"{cmd}: {response}")

        # Save to boot if requested
        if save_to_boot and not errors:
            response = self.send_command("eccf, Current, Boot", wait_time=0.5)
            if "$E:" in response:
                errors.append(f"eccf: {response}")
            else:
                self.logger.info("Configuration saved to boot")

        return len(errors) == 0, errors

    def request_sbf_block(
        self, block_name: str, expected_id: Optional[int] = None
    ) -> Optional[bytes]:
        """Request a single SBF block using esoc command.

        Args:
            block_name: SBF block name (e.g., "PowerStatus")
            expected_id: Expected block ID for verification

        Returns:
            Raw SBF data bytes or None on failure
        """
        if not self._sock:
            if not self.connect():
                return None

        assert self._sock is not None  # For type checker

        # Send esoc command
        cmd = f"esoc, {self._conn_id}, {block_name}\n"
        self._sock.send(cmd.encode())

        # Collect data and scan for expected block
        response = b""
        end_time = time.time() + 2.0

        while time.time() < end_time:
            try:
                self._sock.settimeout(0.5)
                chunk = self._sock.recv(8192)
                if chunk:
                    response += chunk

                    # If we have a specific block to find, scan after each receive
                    if expected_id is not None:
                        result = self._find_sbf_block(response, expected_id)
                        if result is not None:
                            return result
                    else:
                        # No specific block, return first SBF found
                        sync_pos = response.find(b"$@")
                        if sync_pos >= 0:
                            return response[sync_pos:]
            except TimeoutError:
                if len(response) == 0:
                    continue
                if expected_id is None:
                    sync_pos = response.find(b"$@")
                    if sync_pos >= 0:
                        return response[sync_pos:]
                break

        # Final check
        if expected_id is not None:
            result = self._find_sbf_block(response, expected_id)
            if result is not None:
                return result
            self.logger.warning(f"Block ID {expected_id} not found for {block_name}")
        else:
            self.logger.warning(f"No SBF sync found for {block_name}")

        return None

    def _find_sbf_block(self, data: bytes, expected_id: int) -> Optional[bytes]:
        """Scan data for a specific SBF block ID.

        Args:
            data: Raw bytes to scan
            expected_id: The SBF block ID to find

        Returns:
            SBF data starting at the found block, or None
        """
        pos = 0
        while pos < len(data) - 8:
            sync_pos = data.find(b"$@", pos)
            if sync_pos < 0:
                break

            if sync_pos + 8 > len(data):
                break

            # Parse block ID (lower 13 bits of bytes 4-5)
            id_rev = struct.unpack("<H", data[sync_pos + 4 : sync_pos + 6])[0]
            block_id = id_rev & 0x1FFF
            length = struct.unpack("<H", data[sync_pos + 6 : sync_pos + 8])[0]

            if block_id == expected_id:
                # Wait for the WHOLE block. The header declares `length`;
                # returning as soon as the ID matched handed callers a
                # truncated buffer whenever the block straddled a recv()
                # boundary, and the parsers degrade SILENTLY: PVTSatCartesian
                # takes its satellite count from the header (so `total` stayed
                # right) while the SatInfo walk broke early. SatInfo runs in
                # SVID order and BeiDou holds the highest SVIDs (201-263), so
                # BeiDou was always the system that vanished — measured on NPSK
                # as 8 short reads out of 8.
                #
                # Returning None is not "not found": request_sbf_block()
                # accumulates into `response` and re-scans after every recv(),
                # so a block split across chunks is picked up on a later pass.
                #
                # This guard already existed in
                # health/polarx5_tcp_extractor.py::_find_sbf_block; this copy
                # never got it. Keep the two in step.
                if sync_pos + length > len(data):
                    return None
                # Slice to the declared length rather than "everything after
                # the sync marker", so a parser cannot read on into the next
                # block.
                return data[sync_pos : sync_pos + length]

            pos = sync_pos + max(length, 8)

        return None

    def test_connection(self) -> bool:
        """Test if connection to receiver works.

        Returns:
            True if connection successful
        """
        try:
            if self.connect():
                # Try a simple command
                response = self.send_command("grc", wait_time=0.2)
                self.disconnect()
                return "$R:" in response or "ReceiverCapabilities" in response
        except Exception as e:
            self.logger.debug(f"Connection test failed: {e}")
        return False

    def __enter__(self) -> "PolaRX5TCPClient":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Context manager exit."""
        self.disconnect()


def save_config_to_file(
    config: str,
    station_id: str,
    config_type: str = "Current",
    receiver_type: str = "PolaRx5",
    output_dir: Optional[Path] = None,
) -> Path:
    """Save configuration to file with standard naming convention.

    Naming: {ReceiverType}_{StationID}_{ConfigType}_{YYYY-MM-DD-HHMMSS}.txt

    Args:
        config: Configuration string (command list)
        station_id: Station identifier
        config_type: "Current" or "Boot"
        receiver_type: Receiver type name
        output_dir: Output directory (default: current directory)

    Returns:
        Path to saved file
    """
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = f"{receiver_type}_{station_id}_{config_type}_{timestamp}.txt"

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filepath = output_dir / filename
    else:
        filepath = Path(filename)

    filepath.write_text(config)
    return filepath


def load_config_from_file(filepath: Path) -> List[str]:
    """Load configuration commands from file.

    Args:
        filepath: Path to configuration file

    Returns:
        List of command strings
    """
    content = Path(filepath).read_text()
    return [line.strip() for line in content.split("\n") if line.strip()]
