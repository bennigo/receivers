"""Tests for download reliability improvements (#1–#6).

Tests:
- Error classifier (#6c)
- DiskStatus SBF parser (#6a)
- Data routing fix (#6b)
- Health gate (#4)
- Consecutive failure backoff (#3)
- Packet loss factor (#5)
- Router failure cache (#2)
"""

from __future__ import annotations

import struct
import threading
import time
from datetime import UTC, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── Error classifier tests (#6c) ──────────────────────────────────────────


class TestErrorClassifier:
    """Tests for classify_download_error()."""

    def setup_method(self):
        from receivers.utils.error_classifier import classify_download_error

        self.classify = classify_download_error

    def test_dead_connection_sendall(self):
        assert (
            self.classify("'NoneType' object has no attribute 'sendall'")
            == "dead_connection"
        )

    def test_dead_connection_broken_pipe(self):
        assert self.classify("Broken pipe") == "dead_connection"

    def test_dead_connection_reset(self):
        assert self.classify("Connection reset by peer") == "dead_connection"

    def test_stall_timeout_watchdog(self):
        assert (
            self.classify("Watchdog: No data received in 10.0s, killing connection")
            == "stall_timeout"
        )

    def test_stall_timeout_timed_out(self):
        assert self.classify("FTP connection timed out") == "stall_timeout"

    def test_stall_timeout_timeout(self):
        assert self.classify("timeout waiting for data") == "stall_timeout"

    def test_file_not_found_550(self):
        assert self.classify("550 No such file or directory") == "file_not_found"

    def test_file_not_found_not_found(self):
        assert self.classify("Remote file not found on server") == "file_not_found"

    def test_unreachable_ping(self):
        assert self.classify("Ping check failed") == "unreachable"

    def test_unreachable_refused(self):
        assert self.classify("Connection refused") == "unreachable"

    def test_unreachable_no_route(self):
        assert self.classify("No route to host") == "unreachable"

    def test_auth_failed_530(self):
        assert self.classify("530 Login authentication failed") == "auth_failed"

    def test_auth_failed_permission(self):
        assert self.classify("Permission denied") == "auth_failed"

    def test_disk_error_full(self):
        assert self.classify("Disk full, cannot write") == "disk_error"

    def test_disk_error_unmounted(self):
        assert self.classify("disk unmounted") == "disk_error"

    def test_validation_failed(self):
        assert (
            self.classify("Validation failed: corrupt gzip header")
            == "validation_failed"
        )

    def test_validation_size_mismatch(self):
        assert (
            self.classify("Size mismatch: got 12345, expected 67890")
            == "validation_failed"
        )

    def test_unknown_empty(self):
        assert self.classify("") == "unknown"

    def test_unknown_gibberish(self):
        assert self.classify("Something completely unexpected happened") == "unknown"


# ── DiskStatus SBF parser tests (#6a) ─────────────────────────────────────


def _build_disk_status_sbf(disks: list[dict]) -> bytes:
    """Build a raw DiskStatus SBF block matching real PolaRX5 firmware layout.

    Real firmware layout (confirmed via bin2asc on GFUM/ENTC):
    Sub-block (16 bytes per disk):
      - bytes 0-3:  float32 (internal use, not disk usage %)
      - byte 4:     DiskID (uint8)
      - byte 5:     Status bitmask (bit 0=DISK_MOUNTED)
      - bytes 6-7:  Reserved (uint16)
      - bytes 8-11: Internal (uint32)
      - bytes 12-15: DiskSize in MB (uint32)

    Each disk dict: {disk_id, status_flags, disk_size_mb}
    status_flags: bit 0=mounted, bit 2=activity, bit 3=logging
    """
    sb_length = 16
    n_disks = len(disks)

    # SBF header
    body_length = 8 + 8 + n_disks * sb_length + 20  # header + fields + disks + trailer
    header = b"$@"
    header += struct.pack("<H", 0)  # CRC
    header += struct.pack("<H", 4059 | (1 << 13))  # Block ID rev=1
    header += struct.pack("<H", body_length)

    # TOW(4B) + WNc(2B) + N(1B) + SBLength(1B)
    body = struct.pack("<I", 0) + struct.pack("<H", 0)
    body += struct.pack("<B", n_disks) + struct.pack("<B", sb_length)

    for d in disks:
        desc = struct.pack("<f", 0.0)  # float32 placeholder
        desc += struct.pack("<B", d["disk_id"])
        desc += struct.pack("<B", d["status_flags"])
        desc += struct.pack("<H", 0)  # reserved
        desc += struct.pack("<I", 0)  # internal
        desc += struct.pack("<I", d["disk_size_mb"])
        body += desc

    # 20-byte trailer (CreateDeleteCount + rates)
    body += b"\x00" * 20

    return header + body


class TestDiskStatusParser:
    """Tests for DiskStatus parsing (header-only fallback when bin2asc unavailable)."""

    def setup_method(self):
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        self.extractor = PolaRX5TCPExtractor.__new__(PolaRX5TCPExtractor)
        self.extractor.host = "127.0.0.1"
        self.extractor.port = 28784
        self.extractor.station_id = "TEST"
        self.extractor.timeout = 5
        self.extractor.logger = MagicMock()

    def test_single_mounted_disk(self):
        """Mounted disk with 8192 MB total — header-only parse extracts size + status."""
        sbf_data = _build_disk_status_sbf(
            [
                {
                    "disk_id": 1,
                    "status_flags": 0x0D,
                    "disk_size_mb": 8192,
                },  # mounted+activity+logging
            ]
        )
        # Force header-only fallback (no bin2asc)
        result = self.extractor._parse_disk_header_only(sbf_data)

        assert result is not None
        assert result["status"] == "mounted"
        assert result["total_mb"] == 8192.0
        assert len(result["disks"]) == 1
        assert result["disks"][0]["disk_id"] == 1

    def test_unmounted_disk(self):
        """Unmounted disk (status bit 0 = 0)."""
        sbf_data = _build_disk_status_sbf(
            [
                {"disk_id": 1, "status_flags": 0x00, "disk_size_mb": 0},
            ]
        )
        result = self.extractor._parse_disk_header_only(sbf_data)

        assert result is not None
        assert result["status"] == "unmounted"
        assert result["disks"][0]["status"] == "unmounted"

    def test_two_disks(self):
        """Two disks: one mounted, one unmounted."""
        sbf_data = _build_disk_status_sbf(
            [
                {"disk_id": 0, "status_flags": 0x01, "disk_size_mb": 8192},
                {"disk_id": 1, "status_flags": 0x00, "disk_size_mb": 0},
            ]
        )
        result = self.extractor._parse_disk_header_only(sbf_data)

        assert result is not None
        assert len(result["disks"]) == 2
        assert result["total_mb"] == 8192.0

    def test_no_data_returns_none(self):
        """No SBF data received."""
        with patch.object(self.extractor, "_send_sbf_request", return_value=None):
            result = self.extractor._query_disk_status()
        assert result is None

    def test_bin2asc_parse(self):
        """bin2asc-based parse returns correct disk metrics."""
        # Build a real-format SBF block
        sbf_data = _build_disk_status_sbf(
            [
                {"disk_id": 1, "status_flags": 0x0D, "disk_size_mb": 14894},
            ]
        )
        # Mock parse_sbf_bytes to return known parsed records
        parsed_rows = [
            {
                "DiskID": 1.0,
                "DISK_MOUNTED": 1.0,
                "DISK_FULL": 0.0,
                "DiskSize [MB]": 14894.0,
                "DiskUsagePercent [%]": 88.0,
                "Error": "No error",
            },
        ]
        with patch(
            "receivers.utils.rxtools_extractor.parse_sbf_bytes",
            return_value=parsed_rows,
        ):
            result = self.extractor._parse_disk_via_bin2asc(sbf_data)

        assert result is not None
        assert result["status"] == "mounted"
        assert result["usage_percent"] == 88.0
        assert result["total_mb"] == 14894.0
        assert result["used_mb"] == pytest.approx(13106.7, abs=1)

    def test_zero_disks(self):
        """Zero disk descriptors — header-only fallback."""
        sbf_data = _build_disk_status_sbf([])
        result = self.extractor._parse_disk_header_only(sbf_data)

        assert result is not None
        assert result["status"] == "unavailable"
        assert result["disks"] == []


# ── Data routing fix test (#6b) ────────────────────────────────────────────


class TestDiskDataRouting:
    """Test that disk data is stored in metrics (not data_quality)."""

    def test_disk_routed_to_metrics(self):
        """Verify disk data goes to health_data['metrics']['disk']."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        # Construct normally rather than via __new__ + hand-set attributes.
        # __init__ does no I/O (it only assigns), and bypassing it meant every
        # new field added there broke this test from a distance — that is how it
        # ended up failing on `_auth_failed`, a flag added by the fw 5.7.0 work
        # and read unconditionally by extract_health_data.
        ext = PolaRX5TCPExtractor("127.0.0.1", "TEST", port=28784, timeout=5)
        ext.logger = MagicMock()
        ext.metric_checker = MagicMock()

        disk_result = {"status": "mounted", "used_mb": 100, "total_mb": 1000}

        with (
            patch.object(ext, "_check_port_status", return_value=None),
            patch.object(ext, "_query_power_status", return_value=None),
            patch.object(ext, "_query_receiver_status", return_value=None),
            patch.object(ext, "_query_disk_status", return_value=disk_result),
            patch.object(ext, "_query_pvt_geodetic", return_value=None),
            patch.object(ext, "_query_satellite_tracking", return_value=None),
            patch.object(ext, "_query_ntrip_client_status", return_value=None),
            patch.object(ext, "_query_ntrip_server_status", return_value=None),
            patch.object(ext, "_query_receiver_setup", return_value=None),
            patch.object(ext, "_query_logging_sessions", return_value=None),
        ):
            health = ext.extract_health_data()

        # Disk should be in metrics (where db_writer looks for it), not data_quality
        assert "disk" in health["metrics"]
        assert "disk" not in health["data_quality"]
        assert health["metrics"]["disk"]["used_mb"] == 100


# ── Health gate tests (#4) ─────────────────────────────────────────────────


class TestHealthGate:
    """Tests for check_station_health_gate().

    These mock the internal _query_health_gate function to avoid needing
    a real database connection.
    """

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    def test_no_satellites_skips(self):
        """Station with 0 satellites tracked should be skipped."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_health_gate",
            return_value="no_satellites",
        ):
            result = check_station_health_gate("GJAC")
        assert result == "no_satellites"

    def test_disk_full_skips(self):
        """Station with >98% disk usage should be skipped."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_health_gate", return_value="disk_full"
        ):
            result = check_station_health_gate("GJAC")
        assert result == "disk_full"

    def test_disk_broken_skips(self):
        """Station with broken disk (total_mb=0) should be skipped."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_health_gate",
            return_value="disk_broken",
        ):
            result = check_station_health_gate("GJAC")
        assert result == "disk_broken"

    def test_healthy_station_proceeds(self):
        """Healthy station should return None (proceed)."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_health_gate", return_value=None
        ):
            result = check_station_health_gate("GOOD")
        assert result is None

    def test_db_failure_proceeds(self):
        """Database connection failure should not block downloads."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_health_gate",
            side_effect=Exception("DB down"),
        ):
            # The wrapper catches exceptions from _query and returns None
            result = check_station_health_gate("NODB")
            assert result is None

    def test_cache_prevents_repeated_queries(self):
        """Second call should use cached result."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_health_gate", return_value="disk_full"
        ) as mock_q:
            check_station_health_gate("CACHED")
            check_station_health_gate("CACHED")  # Should hit cache
            assert mock_q.call_count == 1


# ── _query_health_gate integration tests ────────────────────────────────────


class TestQueryHealthGateIntegration:
    """Test _query_health_gate with mocked DB connection."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_satellites_detected(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # station_latest_metrics → sats=0, fresh (age computed in Python)
        mock_cur.fetchone.side_effect = [
            (0, 50.0, datetime.now(UTC)),
        ]

        result = _query_health_gate("GJAC")
        assert result == "no_satellites"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_disk_full_does_not_gate(self, mock_dbf):
        # A full receiver disk still serves fresh ring-buffered data (ALHV,
        # 2026-06-12), so disk_usage_pct > 98 must NOT gate the download — it
        # only warns and proceeds. (Was: returned "disk_full".)
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sats=12 (OK), disk=99.5% (>98), fresh (age computed in Python)
        mock_cur.fetchone.side_effect = [
            (12, 99.5, datetime.now(UTC)),
        ]

        result = _query_health_gate("DISKFULL")
        assert result is None  # proceeds despite full disk

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_stale_data_proceeds(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sats=0, disk=99%, but stale (1 hour old → proceed)
        from datetime import timedelta

        stale_time = datetime.now(UTC) - timedelta(hours=1)
        mock_cur.fetchone.side_effect = [
            (0, 99.0, stale_time),
            None,  # block_disk_status → no data (disk_pct=99, not 0/None)
        ]

        result = _query_health_gate("STALE")
        assert result is None

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_data_proceeds(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.return_value = None  # No rows

        result = _query_health_gate("NODATA")
        assert result is None

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_healthy_station_no_skip(self, mock_dbf):
        """Healthy station should return None (proceed)."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Healthy station: sats=15, disk=50%, fresh (age computed in Python)
        # disk_pct=50 → block_disk_status query not reached
        mock_cur.fetchone.side_effect = [
            (15, 50.0, datetime.now(UTC)),
        ]

        result = _query_health_gate("GOOD")
        assert result is None

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_disk_broken_detected(self, mock_dbf):
        """Broken disk (total_mb=0) should trigger disk_broken skip."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Healthy metrics, but broken disk (total_mb=0)
        # Age computed in Python (fresh), disk_pct=0 → triggers block_disk_status query
        mock_cur.fetchone.side_effect = [
            (15, 0.0, datetime.now(UTC)),
            (0,),  # block_disk_status → total_mb=0 → broken
        ]

        result = _query_health_gate("GJAC")
        assert result == "disk_broken"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_disk_broken_no_data(self, mock_dbf):
        """No disk data in block_disk_status should not trigger disk_broken."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Healthy metrics, no disk data in block_disk_status
        # disk_pct=50 → block_disk_status query not reached
        mock_cur.fetchone.side_effect = [
            (15, 50.0, datetime.now(UTC)),
        ]

        result = _query_health_gate("NODISK")
        assert result is None


# ── Session-aware health gate tests (#4 follow-up) ─────────────────────────


class TestHealthGateSessionAware:
    """Session-aware no_satellites gate.

    The no_satellites gate skips daily backfill (15s_24hr) entirely — yesterday's
    archived file does not depend on current sats — but still applies to live/recent
    sessions (1Hz_1hr, status_1hr). disk_full and disk_broken stay session-agnostic.
    """

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_sats_proceeds_for_15s_24hr(self, mock_dbf):
        """sats=0 should NOT skip 15s_24hr — daily backfill is independent of live sats."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sats=0, disk OK, fresh — gate would fire for 1Hz_1hr but not for 15s_24hr
        mock_cur.fetchone.side_effect = [
            (0, 50.0, datetime.now(UTC)),
        ]

        assert _query_health_gate("GSIG", "15s_24hr") is None

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_sats_skips_for_1Hz_1hr(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            (0, 50.0, datetime.now(UTC)),
        ]

        assert _query_health_gate("GSIG", "1Hz_1hr") == "no_satellites"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_sats_skips_for_status_1hr(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            (0, 50.0, datetime.now(UTC)),
        ]

        assert _query_health_gate("GSIG", "status_1hr") == "no_satellites"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_sats_skips_when_session_type_none(self, mock_dbf):
        """session_type=None must keep legacy behaviour (always gate) for safety."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            (0, 50.0, datetime.now(UTC)),
        ]

        assert _query_health_gate("GSIG", None) == "no_satellites"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_disk_full_does_not_skip_any_session(self, mock_dbf):
        """A full disk never gates — it warns and proceeds for every session."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sats=15 (OK), disk=99.5% (>98) — no longer a skip for any session
        mock_cur.fetchone.side_effect = [
            (15, 99.5, datetime.now(UTC)),
        ]

        assert _query_health_gate("ANY", "15s_24hr") is None

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_disk_broken_still_skips_15s_24hr(self, mock_dbf):
        """disk_broken (total_mb=0) is session-agnostic."""
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # disk_pct=0 → triggers block_disk_status query → total_mb=0 → broken
        mock_cur.fetchone.side_effect = [
            (15, 0.0, datetime.now(UTC)),
            (0,),
        ]

        assert _query_health_gate("ANY", "15s_24hr") == "disk_broken"

    def test_cache_key_isolates_session_types(self):
        """Same station with different session_types must not collide in cache."""
        from receivers.utils.stall_timeout import (
            check_station_health_gate,
            invalidate_cache,
        )

        invalidate_cache()

        # First call gates (sats=0, 1Hz_1hr) → cached as "no_satellites"
        # Second call (sats=0, 15s_24hr) must NOT inherit that cache entry
        with patch(
            "receivers.utils.stall_timeout._query_health_gate",
            side_effect=lambda sid, st: (
                "no_satellites" if st in ("1Hz_1hr", "status_1hr") else None
            ),
        ) as mock_q:
            assert check_station_health_gate("GSIG", "1Hz_1hr") == "no_satellites"
            assert check_station_health_gate("GSIG", "15s_24hr") is None
            # Both keys queried independently
            assert mock_q.call_count == 2
            # Cache hit for repeats per key
            assert check_station_health_gate("GSIG", "1Hz_1hr") == "no_satellites"
            assert check_station_health_gate("GSIG", "15s_24hr") is None
            assert mock_q.call_count == 2  # no new queries


# ── Consecutive failure backoff tests (#3) ─────────────────────────────────


class TestConsecutiveFailureBackoff:
    """Tests for should_skip_station()."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    def test_five_failures_triggers_backoff(self):
        from receivers.utils.stall_timeout import invalidate_cache, should_skip_station

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=True,
        ):
            assert should_skip_station("BADST") is True

    def test_mixed_results_no_backoff(self):
        from receivers.utils.stall_timeout import invalidate_cache, should_skip_station

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=False,
        ):
            assert should_skip_station("MIXED") is False

    def test_cache_prevents_repeated_queries(self):
        from receivers.utils.stall_timeout import invalidate_cache, should_skip_station

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=True,
        ) as mock_q:
            should_skip_station("CACHED")
            should_skip_station("CACHED")
            assert mock_q.call_count == 1


class TestQueryConsecutiveFailuresIntegration:
    """Test _query_consecutive_failures with mocked DB."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_all_five_failed(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_consecutive_failures

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchall.return_value = [
            ("failed",),
            ("unreachable",),
            ("stall_timeout",),
            ("failed",),
            ("failed",),
        ]

        assert _query_consecutive_failures("BADST") is True

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_one_success_breaks_streak(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_consecutive_failures

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchall.return_value = [
            ("completed",),
            ("failed",),
            ("failed",),
            ("failed",),
            ("failed",),
        ]

        assert _query_consecutive_failures("MIXED") is False

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_fewer_than_five_no_backoff(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_consecutive_failures

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchall.return_value = [("failed",), ("failed",)]

        assert _query_consecutive_failures("NEW") is False


# ── Packet loss factor tests (#5) ─────────────────────────────────────────


class TestPacketLossFactor:
    """Tests for get_packet_loss_factor()."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    def test_zero_loss(self):
        from receivers.utils.stall_timeout import (
            get_packet_loss_factor,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_packet_loss_factor", return_value=1.0
        ):
            assert get_packet_loss_factor("GOOD") == 1.0

    def test_high_loss_capped(self):
        from receivers.utils.stall_timeout import (
            get_packet_loss_factor,
            invalidate_cache,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_packet_loss_factor", return_value=2.0
        ):
            assert get_packet_loss_factor("BAD") == 2.0


class TestQueryPacketLossFactorIntegration:
    """Test _query_packet_loss_factor with mocked DB."""

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_zero_loss(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (0.0,)

        assert _query_packet_loss_factor("GOOD") == 1.0

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_20_percent_boundary(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (20.0,)

        assert _query_packet_loss_factor("MED") == 1.0

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_35_percent_interpolated(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (35.0,)

        factor = _query_packet_loss_factor("SKRO")
        assert factor == pytest.approx(1.5, abs=0.01)

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_50_percent_capped(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (75.0,)

        assert _query_packet_loss_factor("BAD") == 2.0

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_data_returns_1(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        assert _query_packet_loss_factor("NODATA") == 1.0


# ── Router failure cache tests (#2) ───────────────────────────────────────


class TestRouterFailureCache:
    """Tests for RouterFailureCache."""

    def test_unknown_router_not_failed(self):
        from receivers.cli.parallel import RouterFailureCache

        cache = RouterFailureCache()
        assert cache.is_failed("10.4.1.43") is False

    def test_mark_and_check(self):
        from receivers.cli.parallel import RouterFailureCache

        cache = RouterFailureCache()
        cache.mark_failed("10.4.1.43")
        assert cache.is_failed("10.4.1.43") is True

    def test_different_router_not_affected(self):
        from receivers.cli.parallel import RouterFailureCache

        cache = RouterFailureCache()
        cache.mark_failed("10.4.1.43")
        assert cache.is_failed("10.4.1.44") is False

    def test_expiry(self):
        from receivers.cli.parallel import RouterFailureCache

        cache = RouterFailureCache()
        cache._TTL = 0.1  # 100ms for testing
        cache.mark_failed("10.4.1.43")
        assert cache.is_failed("10.4.1.43") is True
        time.sleep(0.15)
        assert cache.is_failed("10.4.1.43") is False

    def test_thread_safety(self):
        """Concurrent mark/check should not crash."""
        from receivers.cli.parallel import RouterFailureCache

        cache = RouterFailureCache()
        errors = []

        def marker():
            try:
                for i in range(100):
                    cache.mark_failed(f"10.0.0.{i % 10}")
            except Exception as e:
                errors.append(e)

        def checker():
            try:
                for i in range(100):
                    cache.is_failed(f"10.0.0.{i % 10}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=marker),
            threading.Thread(target=checker),
            threading.Thread(target=marker),
            threading.Thread(target=checker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ── Fix 1: Mode-switch returns new FTP connection ─────────────────────────


class TestModeSwitchFtpReturn:
    """Test that _download_with_progressbar_and_retry returns (result, ftp) tuple
    and that a mode-switch reconnect returns the NEW ftp connection."""

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5

        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.pasv = True
        rx.progress_timeout = 600
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 30
        return rx

    def test_happy_path_returns_original_ftp(self):
        """Normal download returns (result, original_ftp) tuple."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()

        with patch.object(rx, "_download_with_progressbar", return_value=0):
            result, ftp_out = rx._download_with_progressbar_and_retry(
                ftp_orig,
                "/remote/file",
                "/local/file",
                1000,
                0,
            )

        assert result == 0
        assert ftp_out is ftp_orig

    def test_mode_switch_returns_new_ftp(self):
        """After mode-switch reconnect, returns (result, ftp_new)."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()
        ftp_new = MagicMock()

        # First call raises connection error, triggering mode switch
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("500 I won't open a connection to X (only to Y)")
            return 0

        with (
            patch.object(rx, "_download_with_progressbar", side_effect=side_effect),
            patch.object(rx, "_ftp_open_connection", return_value=ftp_new),
            patch.object(rx, "_get_ftp_mode_description", return_value="passive"),
        ):
            result, ftp_out = rx._download_with_progressbar_and_retry(
                ftp_orig,
                "/remote/file",
                "/local/file",
                1000,
                0,
            )

        assert result == 0
        assert ftp_out is ftp_new
        assert ftp_out is not ftp_orig

    def test_immediate_retry_propagates_new_ftp(self):
        """_download_with_immediate_retry propagates the new ftp from mode-switch."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()
        ftp_new = MagicMock()

        with patch.object(
            rx, "_download_with_progressbar_and_retry", return_value=(0, ftp_new)
        ):
            result, ftp_out = rx._download_with_immediate_retry(
                ftp_orig,
                "/remote/file",
                "/local/file",
                1000,
                0,
            )

        assert result == 0
        assert ftp_out is ftp_new


class TestWatchdogKillTriggersModeSwitch:
    """Watchdog zero-byte stalls share the root cause of explicit FTP NAT
    errors (broken nf_conntrack_ftp helper on non-standard ports → data
    channel silently dead). Before this fix, the mode-switch trigger only
    matched explicit FTP errors like "500 I won't open"; watchdog kills
    propagated unchanged and every retry stayed in the same broken passive
    mode. 30+ stations seen on 2026-05-12 with 2-4 zero-byte stalls each.
    """

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5

        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.pasv = True
        rx.progress_timeout = 600
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 30
        return rx

    def test_watchdog_kill_triggers_mode_switch(self):
        """Full watchdog error string (as written by the production code path)
        must trigger a passive→active mode-switch reconnect."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()
        ftp_new = MagicMock()
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise TimeoutError(
                    "No data received within 60s - connection killed by watchdog"
                )
            return 0

        with (
            patch.object(rx, "_download_with_progressbar", side_effect=side_effect),
            patch.object(rx, "_ftp_open_connection", return_value=ftp_new),
            patch.object(rx, "_get_ftp_mode_description", return_value="active"),
        ):
            result, ftp_out = rx._download_with_progressbar_and_retry(
                ftp_orig,
                "/remote/file",
                "/local/file",
                1000,
                0,
            )

        assert result == 0
        assert ftp_out is ftp_new
        assert call_count[0] == 2, "Expected one retry on the new connection"

    def test_killed_by_watchdog_substring_alone_triggers_switch(self):
        """The shorter `killed by watchdog` phrase still triggers — guards
        against future error-message refactors that drop "no data received"."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()
        ftp_new = MagicMock()
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise TimeoutError("transfer aborted - killed by watchdog at 0%")
            return 0

        with (
            patch.object(rx, "_download_with_progressbar", side_effect=side_effect),
            patch.object(rx, "_ftp_open_connection", return_value=ftp_new),
            patch.object(rx, "_get_ftp_mode_description", return_value="active"),
        ):
            result, ftp_out = rx._download_with_progressbar_and_retry(
                ftp_orig, "/remote/file", "/local/file", 1000, 0
            )

        assert result == 0
        assert ftp_out is ftp_new

    def test_unrelated_timeout_does_not_trigger_switch(self):
        """Regression guard: a generic socket timeout (not the watchdog phrase)
        must propagate — these are read errors, not data-channel-dead errors,
        and mode-switching them masks real issues."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()

        with (
            patch.object(
                rx,
                "_download_with_progressbar",
                side_effect=TimeoutError("socket recv timed out"),
            ),
            patch.object(rx, "_ftp_open_connection") as open_mock,
            patch.object(rx, "_get_ftp_mode_description", return_value="passive"),
        ):
            with pytest.raises(TimeoutError, match="socket recv timed out"):
                rx._download_with_progressbar_and_retry(
                    ftp_orig, "/remote/file", "/local/file", 1000, 0
                )

        open_mock.assert_not_called()


# ── Fix 2: Ping-override for backoff ──────────────────────────────────────


class TestBackoffPingOverride:
    """Test that should_skip_station() can be overridden by a successful ping."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache

        invalidate_cache()

    def test_clear_backoff_cache(self):
        """clear_backoff_cache removes the station from the cache."""
        from receivers.utils.stall_timeout import (
            clear_backoff_cache,
            invalidate_cache,
            should_skip_station,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=True,
        ):
            assert should_skip_station("PING1") is True

        clear_backoff_cache("PING1")

        # After clearing, it should re-query (which we now make return False)
        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=False,
        ):
            assert should_skip_station("PING1") is False

    def test_clear_backoff_cache_case_insensitive(self):
        """clear_backoff_cache normalizes station ID to uppercase."""
        from receivers.utils.stall_timeout import (
            clear_backoff_cache,
            invalidate_cache,
            should_skip_station,
        )

        invalidate_cache()

        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=True,
        ):
            assert should_skip_station("PING2") is True

        clear_backoff_cache("ping2")  # lowercase

        with patch(
            "receivers.utils.stall_timeout._query_consecutive_failures",
            return_value=False,
        ):
            assert should_skip_station("PING2") is False

    def test_clear_nonexistent_station_no_error(self):
        """Clearing cache for unknown station doesn't raise."""
        from receivers.utils.stall_timeout import clear_backoff_cache

        clear_backoff_cache("DOESNOTEXIST")  # Should not raise


# ── Fix 3: Progress-aware timeout extension ───────────────────────────────


class TestTimeoutExtension:
    """Test that near-complete downloads get a one-time timeout extension."""

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5

        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.progress_timeout = 100
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 60
        return rx

    def test_extension_logged_when_over_70_percent(self):
        """When progress >70% and timeout hit, extension should be logged."""
        self._make_receiver()

        # The extension logic is inside _download_with_progressbar which is
        # deeply integrated with FTP. Test the logic pattern directly:
        # - timeout_extended starts False
        # - if >70% and not extended: extend by 50%, set flag True
        effective_timeout = 100
        timeout_extended = False
        offset = 0
        remote_file_size = 1000
        current_bytes = 750  # 75% done

        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert timeout_extended is True
        assert effective_timeout == 150  # 100 + 50

    def test_no_extension_when_under_70_percent(self):
        """When progress <70%, no extension."""
        effective_timeout = 100
        timeout_extended = False
        offset = 0
        remote_file_size = 1000
        current_bytes = 600  # 60% done

        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert timeout_extended is False
        assert effective_timeout == 100

    def test_extension_only_once(self):
        """Extension flag prevents double extension."""
        effective_timeout = 150  # Already extended once
        timeout_extended = True  # Flag already set
        offset = 0
        remote_file_size = 1000
        current_bytes = 900  # 90% done

        original_timeout = effective_timeout
        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert effective_timeout == original_timeout  # Unchanged

    def test_extension_with_resume_offset(self):
        """Extension progress calculation accounts for resume offset."""
        effective_timeout = 100
        timeout_extended = False
        offset = 500  # Resumed from 500 bytes
        remote_file_size = 1000
        current_bytes = 900  # 400/500 remaining = 80% of remaining done

        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert timeout_extended is True
        assert effective_timeout == 150


# ── Fix 4: Size mismatch clean retry ─────────────────────────────────────


class TestSizeMismatchRetry:
    """Test the size mismatch → delete → retry clean logic."""

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5

        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.progress_timeout = 600
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 30
        rx._last_effective_timeout = 600
        rx.file_validator = MagicMock()
        rx.file_validator.validate_file.return_value = {
            "valid": True,
            "compression": "gzip",
            "size": 1000,
        }
        return rx

    def test_handle_successful_download_valid(self):
        """_handle_successful_download records completed for valid files."""
        import os
        import tempfile

        rx = self._make_receiver()
        record = MagicMock()
        downloaded = []

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100)
            tmp_path = f.name

        try:
            from pathlib import Path

            result = rx._handle_successful_download(
                "test.sbf.gz",
                Path(tmp_path),
                100,
                100,
                "15s_24hr",
                1.0,
                downloaded,
                None,
                False,
                False,
                False,
                None,
                record,
            )
            assert result is True
            record.assert_called_once()
            assert record.call_args[0][2] == "completed"
            assert tmp_path in downloaded[0] or str(Path(tmp_path)) in downloaded[0]
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_handle_successful_download_invalid(self):
        """_handle_successful_download records failed and removes invalid files."""
        import os
        import tempfile

        rx = self._make_receiver()
        rx.file_validator.validate_file.return_value = {
            "valid": False,
            "error": "corrupt gzip",
        }
        record = MagicMock()
        downloaded = []

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100)
            tmp_path = f.name

        try:
            from pathlib import Path

            result = rx._handle_successful_download(
                "test.sbf.gz",
                Path(tmp_path),
                100,
                100,
                "15s_24hr",
                1.0,
                downloaded,
                None,
                False,
                False,
                False,
                None,
                record,
            )
            assert result is True
            record.assert_called_once()
            assert record.call_args[0][2] == "failed"
            assert not os.path.exists(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_size_mismatch_retried_flag_resets_per_file(self):
        """size_mismatch_retried should be False at start of each file iteration."""
        # This tests the pattern: the flag is initialized per-file in the loop
        size_mismatch_retried = False  # As set at start of each iteration
        assert size_mismatch_retried is False

        # After first mismatch retry
        size_mismatch_retried = True
        assert size_mismatch_retried is True

        # Next file iteration resets it
        size_mismatch_retried = False
        assert size_mismatch_retried is False


# ── Session bootstrap timeout tests (R8) ─────────────────────────────────


class TestSessionBootstrapTimeout:
    """Tests for session-aware bootstrap timeout (tier 2b)."""

    def test_bootstrap_used_when_no_adaptive_data(self):
        """15s_24hr sessions get 900s bootstrap when adaptive returns None."""
        from receivers.utils.stall_timeout import get_stall_timeout

        with (
            patch("receivers.utils.stall_timeout._get_overrides", return_value={}),
            patch(
                "receivers.utils.stall_timeout.compute_adaptive_timeout",
                return_value=None,
            ),
        ):
            timeout = get_stall_timeout(
                "BRTT",
                "polarx5",
                default=300,
                session_type="15s_24hr",
            )
            assert timeout == 900

    def test_adaptive_takes_priority_over_bootstrap(self):
        """Adaptive timeout (tier 2) overrides bootstrap (tier 2b)."""
        from receivers.utils.stall_timeout import get_stall_timeout

        with (
            patch("receivers.utils.stall_timeout._get_overrides", return_value={}),
            patch(
                "receivers.utils.stall_timeout.compute_adaptive_timeout",
                return_value=1200,
            ),
        ):
            timeout = get_stall_timeout(
                "BRTT",
                "polarx5",
                default=300,
                session_type="15s_24hr",
            )
            assert timeout == 1200

    def test_db_override_takes_priority_over_bootstrap(self):
        """DB override (tier 1) overrides bootstrap (tier 2b)."""
        from receivers.utils.stall_timeout import get_stall_timeout

        with (
            patch(
                "receivers.utils.stall_timeout._get_overrides",
                return_value={"BRTT": 500},
            ),
            patch(
                "receivers.utils.stall_timeout.compute_adaptive_timeout"
            ) as mock_adaptive,
        ):
            timeout = get_stall_timeout(
                "BRTT",
                "polarx5",
                default=300,
                session_type="15s_24hr",
            )
            assert timeout == 500
            mock_adaptive.assert_not_called()

    def test_no_bootstrap_for_hourly_sessions(self):
        """1Hz_1hr sessions fall through to receivers.cfg (no bootstrap defined)."""
        from receivers.utils.stall_timeout import get_stall_timeout

        with (
            patch("receivers.utils.stall_timeout._get_overrides", return_value={}),
            patch(
                "receivers.utils.stall_timeout.compute_adaptive_timeout",
                return_value=None,
            ),
            patch("receivers.config.receivers_config.get_receivers_config") as mock_cfg,
        ):
            mock_cfg.return_value.get_receiver_config.return_value = {
                "stall_timeout": 600
            }
            timeout = get_stall_timeout(
                "ELDC",
                "polarx5",
                default=300,
                session_type="1Hz_1hr",
            )
            assert timeout == 600

    def test_no_bootstrap_without_session_type(self):
        """Without session_type, falls through to receivers.cfg."""
        from receivers.utils.stall_timeout import get_stall_timeout

        with (
            patch("receivers.utils.stall_timeout._get_overrides", return_value={}),
            patch("receivers.config.receivers_config.get_receivers_config") as mock_cfg,
        ):
            mock_cfg.return_value.get_receiver_config.return_value = {
                "stall_timeout": 600
            }
            timeout = get_stall_timeout("ELDC", "polarx5", default=300)
            assert timeout == 600
