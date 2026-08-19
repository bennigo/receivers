"""
Unit tests for the RINEX conversion module.

Tests cover:
- RinexNamer: Filename generation for short/long conventions
- MetadataProvider: Equipment metadata lookup
- EquipmentMetadata: TOS session parsing and correction generation
- SBFConverter: Basic structure (without external tools)
- TrimbleConverter: Basic structure (without external tools)
"""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# Import module components
from receivers.rinex import (
    RINEX_FIELD_SPECS,
    ConversionError,
    ConversionResult,
    EquipmentMetadata,
    MetadataProvider,
    NamingConvention,
    OutputFormat,
    RinexNamer,
    RinexVersion,
    SBFConverter,
    TrimbleConverter,
    format_antenna_type_with_radome,
    format_rinex_field,
)


class TestRinexNamer:
    """Tests for RINEX filename generation."""

    def test_short_name_daily(self):
        """Test short (RINEX 2) naming for daily file."""
        namer = RinexNamer("ELDC", RinexVersion.RINEX_2)
        dt = datetime(2026, 1, 15)  # DOY 015

        name = namer.generate_filename(
            dt,
            convention=NamingConvention.SHORT,
            file_type="MO",
        )

        # Expected: ELDC0150.26o
        assert name.startswith("ELDC")
        assert "015" in name  # Day of year
        assert ".26" in name  # Year
        assert name.endswith("o")  # Observation file

    def test_short_name_hourly(self):
        """Test short naming for hourly file."""
        namer = RinexNamer("THOB", RinexVersion.RINEX_2)
        dt = datetime(2026, 3, 20, 14, 0)  # DOY 079, hour 14

        name = namer.generate_filename(
            dt,
            convention=NamingConvention.SHORT,
            file_type="MO",
        )

        # Expected: THOB0790.26o
        assert name.startswith("THOB")
        assert "079" in name  # Day of year
        assert ".26o" in name

    def test_long_name_daily(self):
        """Test long (IGS) naming for daily file."""
        namer = RinexNamer("ELDC", RinexVersion.RINEX_3, country_code="ISL")
        dt = datetime(2026, 1, 15)  # DOY 015

        name = namer.generate_filename(
            dt,
            convention=NamingConvention.LONG,
            file_type="MO",
            data_source="R",
            file_period="01D",
            data_frequency="15S",
        )

        # Expected: ELDC00ISL_R_20260150000_01D_15S_MO.rnx (uppercase by default)
        assert name.startswith("ELDC")  # Uppercase station ID (default)
        assert "00ISL" in name  # Monument + country
        assert "_R_" in name  # Receiver source
        assert "2026015" in name  # Year + DOY
        assert "_01D_" in name  # Daily period
        assert "_15S_" in name  # 15-second data
        assert "_MO" in name  # Mixed observation
        assert name.endswith(".rnx")

    def test_long_name_hourly(self):
        """Test long naming for hourly file."""
        namer = RinexNamer("MANA", RinexVersion.RINEX_3)
        dt = datetime(2026, 6, 15, 10, 0)  # DOY 166, hour 10

        name = namer.generate_filename(
            dt,
            convention=NamingConvention.LONG,
            file_type="MO",
            file_period="01H",
            data_frequency="01S",
        )

        # Expected: MANA00ISL_R_20261661000_01H_01S_MO.rnx (uppercase by default)
        assert "2026166" in name  # Year + DOY
        assert "1000" in name  # Hour and minute
        assert "_01H_" in name  # Hourly period
        assert "_01S_" in name  # 1-second data

    def test_station_id_formatting(self):
        """Test that station IDs are properly formatted."""
        # Short station ID should be padded
        namer = RinexNamer("ABC", RinexVersion.RINEX_3)
        assert namer.station_id == "ABC "  # Padded to 4 chars

        # Long station ID should be truncated
        namer = RinexNamer("ABCDEFGH", RinexVersion.RINEX_3)
        assert namer.station_id == "ABCD"  # Truncated to 4 chars

    def test_parse_short_filename(self):
        """Test parsing short RINEX filenames."""
        namer = RinexNamer("TEST", RinexVersion.RINEX_2)

        components = namer.parse_filename("ELDC0150.26o")

        assert components is not None
        assert components.station == "ELDC"
        assert components.day_of_year == 15
        assert components.year == 2026  # Converted from 26
        assert components.file_type == "MO"

    def test_parse_long_filename(self):
        """Test parsing long RINEX filenames."""
        namer = RinexNamer("TEST", RinexVersion.RINEX_3)

        components = namer.parse_filename("eldc00ISL_R_20260150000_01D_15S_MO.rnx")

        assert components is not None
        assert components.station == "ELDC"
        assert components.year == 2026
        assert components.day_of_year == 15
        assert components.file_period == "01D"
        assert components.data_frequency == "15S"
        assert components.file_type == "MO"

    def test_session_file_period(self):
        """Test getting file period from session type."""
        assert RinexNamer.get_session_file_period("15s_24hr") == "01D"
        assert RinexNamer.get_session_file_period("1Hz_1hr") == "01H"
        assert RinexNamer.get_session_file_period("unknown") == "01D"  # Default

    def test_session_data_frequency(self):
        """Test getting data frequency from session type."""
        assert RinexNamer.get_session_data_frequency("15s_24hr") == "15S"
        assert RinexNamer.get_session_data_frequency("1Hz_1hr") == "01S"
        assert RinexNamer.get_session_data_frequency("30s_daily") == "30S"

    def test_parse_date_hour_strips_compression_suffix(self):
        """parse_date_hour must strip .Z/.gz/.bz2 before parsing.

        Production files are stored compressed (RHOL1180.26o.Z). Without
        suffix stripping the parser returns (None, None) and file_tracking
        silently skips every RINEX file, leaving the dashboard blank.
        """
        from datetime import date

        # .Z suffix (Unix compress — the common production case)
        assert RinexNamer.parse_date_hour("RHOL1180.26o.Z", station_id="RHOL") == (
            date(2026, 4, 28),
            None,
        )
        # .gz suffix
        assert RinexNamer.parse_date_hour("AUSV1130.26o.gz", station_id="AUSV") == (
            date(2026, 4, 23),
            None,
        )
        # .bz2 suffix
        assert RinexNamer.parse_date_hour("KVSK1170.26o.bz2", station_id="KVSK") == (
            date(2026, 4, 27),
            None,
        )
        # No suffix still works (DOY 115 = April 25)
        assert RinexNamer.parse_date_hour("VOGS1150.26o", station_id="VOGS") == (
            date(2026, 4, 25),
            None,
        )
        # Non-RINEX name still returns (None, None)
        assert RinexNamer.parse_date_hour(
            "EYVI202604260000a.26o", station_id="EYVI"
        ) == (None, None)


class TestEquipmentMetadata:
    """Tests for equipment metadata handling."""

    def test_from_tos_session(self):
        """Test creating metadata from TOS session."""
        session = {
            "time_from": "2024-01-01T00:00:00Z",
            "time_to": "2024-12-31T23:59:59Z",
            "gnss_receiver": {
                "model": "SEPT POLARX5",
                "serial_number": "1234567",
                "firmware_version": "5.4.0",
            },
            "antenna": {
                "model": "ASH701945C_M",
                "serial_number": "CR620012345",
                "antenna_height": 0.0,
            },
            "radome": {
                "model": "SCIS",
            },
            "monument": {
                "monument_height": 0.0,
            },
        }

        metadata = EquipmentMetadata.from_tos_session(session)

        assert metadata.receiver_model == "SEPT POLARX5"
        assert metadata.receiver_serial == "1234567"
        assert metadata.receiver_firmware == "5.4.0"
        assert metadata.antenna_model == "ASH701945C_M"
        assert metadata.radome_model == "SCIS"

    def test_to_rinex_corrections(self):
        """Test generating RINEX header corrections with fixed-width formatting.

        RINEX header fields use fixed-width Fortran format:
        - MARKER NAME: 60 chars
        - MARKER NUMBER: 20 chars
        - OBSERVER / AGENCY: A20 + A40
        - ANT # / TYPE: A20 (serial) + A20 (type with radome)
        - ANTENNA: DELTA H/E/N: F14.4 + F14.4 + F14.4

        NOTE: REC # / TYPE / VERS is NOT included because converter tools (sbf2rin)
        correctly extract receiver info from the raw data file.
        """
        metadata = EquipmentMetadata(
            marker_name="ELDC",
            marker_number="12345M001",
            receiver_model="SEPT POLARX5",  # Not used in corrections
            receiver_serial="1234567",  # Not used in corrections
            receiver_firmware="5.4.0",  # Not used in corrections
            antenna_model="ASH701945C_M",
            antenna_serial="CR620012345",
            radome_model="SCIS",
            antenna_height=0.0,
            monument_height=0.0,
            observer="BGO",
            agency="IMO",
        )

        corrections = metadata.to_rinex_corrections()

        # MARKER NAME: 60 chars, uppercase
        assert "MARKER NAME" in corrections
        assert corrections["MARKER NAME"] == "ELDC" + " " * 56  # 60 chars total
        assert len(corrections["MARKER NAME"]) == 60

        # MARKER NUMBER: 20 chars
        assert "MARKER NUMBER" in corrections
        assert corrections["MARKER NUMBER"].startswith("12345M001")
        assert len(corrections["MARKER NUMBER"]) == 20

        # REC # / TYPE / VERS should NOT be in corrections
        # (sbf2rin gets this correct from the SBF file)
        assert "REC # / TYPE / VERS" not in corrections

        # ANT # / TYPE: A20 (serial) + A20 (type with radome) = 40 chars
        assert "ANT # / TYPE" in corrections
        ant_field = corrections["ANT # / TYPE"]
        assert len(ant_field) == 40
        assert ant_field[:20].startswith("CR620012345")  # Serial in first 20 chars
        assert "ASH701945C_M" in ant_field  # Antenna model
        assert "SCIS" in ant_field  # Radome

        # OBSERVER / AGENCY: A20 + A40 = 60 chars
        assert "OBSERVER / AGENCY" in corrections
        obs_field = corrections["OBSERVER / AGENCY"]
        assert len(obs_field) == 60
        assert obs_field[:20].startswith("BGO")  # Observer in first 20 chars
        assert obs_field[20:].startswith("IMO")  # Agency in next 40 chars

        # ANTENNA: DELTA H/E/N: F14.4 + F14.4 + F14.4 = 42 chars
        assert "ANTENNA: DELTA H/E/N" in corrections
        delta_field = corrections["ANTENNA: DELTA H/E/N"]
        assert len(delta_field) == 42
        assert "0.0000" in delta_field

    def test_to_rinex_corrections_without_antenna_serial(self):
        """A missing serial must not take the antenna TYPE and RADOME with it.

        This used to assert the field was omitted entirely when the serial was
        empty. That gate was wrong in the common case: TOS very often has no
        manufacturer serial (it assigns a placeholder instead), and suppressing
        the whole field then dropped the antenna model and radome — the two parts
        that are known and that downstream processing actually needs. The field
        is now emitted whenever EITHER part is known, with the serial left blank.
        """
        metadata = EquipmentMetadata(
            marker_name="TEST",
            antenna_model="ASH701945C_M",
            antenna_serial="",  # No serial
            radome_model="SCIS",
        )

        corrections = metadata.to_rinex_corrections()

        assert "ANT # / TYPE" in corrections
        field = corrections["ANT # / TYPE"]
        assert field[:20].strip() == "", "unknown serial is blank, not invented"
        assert "ASH701945C_M" in field
        assert "SCIS" in field

    def test_to_rinex_corrections_without_antenna_model_or_serial(self):
        """Neither part known ⇒ nothing to say, so the field stays out."""
        metadata = EquipmentMetadata(
            marker_name="TEST", antenna_model="", antenna_serial=""
        )
        assert "ANT # / TYPE" not in metadata.to_rinex_corrections()

    def test_to_rinex_corrections_with_receiver(self):
        """Test including receiver info with include_receiver=True."""
        metadata = EquipmentMetadata(
            marker_name="ELDC",
            receiver_model="SEPT POLARX5",
            receiver_serial="1234567",
            receiver_firmware="5.4.0",
        )

        # Default: no receiver
        corrections = metadata.to_rinex_corrections()
        assert "REC # / TYPE / VERS" not in corrections

        # With include_receiver=True
        corrections = metadata.to_rinex_corrections(include_receiver=True)
        assert "REC # / TYPE / VERS" in corrections
        rec_field = corrections["REC # / TYPE / VERS"]
        assert len(rec_field) == 60  # A20 + A20 + A20
        assert rec_field[:20].startswith("1234567")  # Serial
        assert "SEPT POLARX5" in rec_field  # Model
        assert "5.4.0" in rec_field  # Firmware

    def test_to_rinex_corrections_with_overrides(self):
        """Test override functionality in to_rinex_corrections."""
        metadata = EquipmentMetadata(
            marker_name="ELDC",
            marker_number="DOMES001",
            observer="BGO",
            agency="IMO",
        )

        # Override marker name
        corrections = metadata.to_rinex_corrections(overrides={"MARKER NAME": "CUSTOM"})
        assert corrections["MARKER NAME"].startswith("CUSTOM")

        # Add receiver via override (not from metadata)
        corrections = metadata.to_rinex_corrections(
            overrides={
                "REC # / TYPE / VERS": ("SN123", "MODEL", "1.0"),
            }
        )
        assert "REC # / TYPE / VERS" in corrections
        assert corrections["REC # / TYPE / VERS"].startswith("SN123")

        # Skip a field with None
        corrections = metadata.to_rinex_corrections(overrides={"MARKER NUMBER": None})
        assert "MARKER NUMBER" not in corrections
        assert "MARKER NAME" in corrections  # Others still present

    def test_from_station_config(self):
        """Test creating metadata from station configuration.

        Uses the new config structure with 'rinex' and 'antenna' sections
        populated from teqc configs.
        """
        config = {
            "station_id": "THOB",
            "rinex": {
                "marker_name": "THOB",
                "marker_number": "12345M001",
                "observer": "BGO/HMF",
                "agency": "IMO",
                "config_valid_from": "2020-01-29",
            },
            "receiver": {
                "type": "SEPT POLARX5TR",
                "serial": "9876543",
                "firmware": "5.5.0",
            },
            "antenna": {
                "type": "LEIAR25.R4",
                "serial": "ANT12345",
                "height": 0.0089,
                "radome": "LEIT",
            },
        }

        metadata = EquipmentMetadata.from_station_config(config)

        assert metadata.marker_name == "THOB"
        assert metadata.marker_number == "12345M001"
        assert metadata.observer == "BGO/HMF"
        assert metadata.agency == "IMO"
        assert metadata.receiver_model == "SEPT POLARX5TR"
        assert metadata.antenna_model == "LEIAR25.R4"
        assert metadata.radome_model == "LEIT"
        assert metadata.antenna_height == 0.0089


class TestMetadataProvider:
    """Tests for metadata provider functionality.

    Note: TOS database queries are now handled by tostools.rinex.correct_rinex_from_tos().
    This class only tests config-based metadata lookup.
    """

    def test_init_defaults(self):
        """Test default initialization."""
        provider = MetadataProvider()
        assert hasattr(provider, "_config_cache")
        assert len(provider._config_cache) == 0

    def test_get_equipment_from_config_caches_result(self):
        """Test that config lookup results are cached."""
        provider = MetadataProvider()

        # Mock the config loader
        config_metadata = EquipmentMetadata(
            marker_name="TEST",
            time_from=datetime(2025, 1, 1),
        )

        with patch.object(provider, "_config_cache", {"TEST": config_metadata}):
            # Should return cached value
            result = provider.get_equipment_from_config("TEST")
            assert result is not None
            assert result.marker_name == "TEST"

    def test_cache_clearing(self):
        """Test cache clearing functionality."""
        provider = MetadataProvider()
        provider._config_cache = {"TEST": EquipmentMetadata()}

        provider.clear_cache()

        assert len(provider._config_cache) == 0


class TestConverterBase:
    """Tests for converter base functionality."""

    def test_sbf_converter_properties(self):
        """Test SBF converter properties."""
        converter = SBFConverter("ELDC")

        assert ".sbf" in converter.supported_extensions
        assert ".sbf.gz" in converter.supported_extensions
        assert converter.converter_name == "sbf2rin"
        assert "sbf2rin" in converter._get_required_tools()

    def test_trimble_converter_properties(self):
        """Test Trimble converter properties."""
        converter = TrimbleConverter("MANA")

        assert (
            ".t02" in converter.supported_extensions
            or ".T02" in converter.supported_extensions
        )
        assert (
            ".t00" in converter.supported_extensions
            or ".T00" in converter.supported_extensions
        )
        assert converter.converter_name == "runpkr00"

    def test_hatanaka_output_renamed_to_uppercase_D(self, tmp_path):
        """rnx2crx writes lowercase .YYd; the converter must rename it to the
        IMO archive convention .YYD (uppercase). Regression for the old-rek ->
        rek_new cutover where lowercase .d.Z broke the okada getimorinex.py fetch.
        """
        converter = SBFConverter("ELDC")
        obs = tmp_path / "ELDC1720.26o"
        obs.write_text("dummy rinex obs\n")

        def fake_rnx2crx(cmd, timeout=60):
            # Simulate rnx2crx: read .26o, emit lowercase .26d alongside it.
            src = Path(cmd[-1])
            src.with_suffix(".26d").write_text("dummy crinex\n")
            return Mock(returncode=0, stdout="", stderr="")

        with patch.object(converter, "get_tool_path", return_value=Path("rnx2crx")):
            with patch.object(converter, "_run_subprocess", side_effect=fake_rnx2crx):
                out = converter._apply_hatanaka_compression(obs)

        assert out.name == "ELDC1720.26D", f"expected uppercase .26D, got {out.name}"
        assert out.exists()
        assert not (tmp_path / "ELDC1720.26d").exists(), "lowercase .26d not renamed"
        assert not obs.exists(), "original .26o not removed"

    def test_conversion_result_structure(self):
        """Test ConversionResult data structure."""
        result = ConversionResult(
            raw_file=Path("/test/file.sbf"),
            rinex_file=Path("/test/file.rnx"),
            success=True,
            message="Test conversion",
            duration_seconds=1.5,
            header_corrections_applied=5,
            warnings=["warning1"],
        )

        result_dict = result.to_dict()

        assert result_dict["success"] is True
        assert result_dict["duration_seconds"] == 1.5
        assert result_dict["header_corrections_applied"] == 5
        assert len(result_dict["warnings"]) == 1

    def test_conversion_error(self):
        """Test ConversionError exception."""
        error = ConversionError(
            "Test error",
            raw_file=Path("/test/file.sbf"),
            details="Additional details",
        )

        assert "Test error" in str(error)
        assert "file.sbf" in str(error)
        assert "Additional details" in str(error)

    def test_date_extraction_from_filename(self):
        """Test extracting observation date from filename."""
        converter = SBFConverter("ELDC")

        # Test standard format: STATIONYYYYMMDDHHMM
        path = Path("/test/ELDC202601150000a.sbf")
        dt = converter._extract_date_from_filename(path)

        assert dt.year == 2026
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 0
        assert dt.minute == 0

    def test_date_extraction_simple_format(self):
        """Test extracting date from simple YYYYMMDD format."""
        converter = SBFConverter("ELDC")

        path = Path("/test/ELDC20260315.sbf")
        dt = converter._extract_date_from_filename(path)

        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 15

    def test_version_configuration(self):
        """Test RINEX version configuration."""
        converter = SBFConverter("ELDC", rinex_version=RinexVersion.RINEX_2)
        assert converter.rinex_version == RinexVersion.RINEX_2

        converter = SBFConverter("ELDC", rinex_version=RinexVersion.RINEX_3)
        assert converter.rinex_version == RinexVersion.RINEX_3

    def test_output_format_configuration(self):
        """Test output format configuration."""
        converter = SBFConverter("ELDC", output_format=OutputFormat.MODERN)
        assert converter.output_format == OutputFormat.MODERN

        converter = SBFConverter("ELDC", output_format=OutputFormat.LEGACY)
        assert converter.output_format == OutputFormat.LEGACY


class TestSBFConverter:
    """Tests for SBF converter specifics."""

    def test_supported_extensions(self):
        """Test supported file extensions."""
        converter = SBFConverter("TEST")
        extensions = converter.supported_extensions

        assert ".sbf" in extensions
        assert ".sbf.gz" in extensions

    def test_has_supported_extension(self):
        """Test extension checking."""
        converter = SBFConverter("TEST")

        assert converter._has_supported_extension(Path("/test/file.sbf"))
        assert converter._has_supported_extension(Path("/test/file.sbf.gz"))
        assert converter._has_supported_extension(Path("/test/file.SBF.GZ"))
        assert not converter._has_supported_extension(Path("/test/file.T02"))

    def test_tool_validation(self):
        """Test tool validation returns expected structure."""
        converter = SBFConverter("TEST")

        # Mock get_tool_path to always fail
        converter.get_tool_path = Mock(side_effect=ConversionError("Not found"))

        tools = converter.validate_tools()

        assert "sbf2rin" in tools
        assert tools["sbf2rin"] is False


class TestTrimbleConverter:
    """Tests for Trimble converter specifics."""

    def test_supported_extensions(self):
        """Test supported file extensions."""
        converter = TrimbleConverter("TEST")
        extensions = converter.supported_extensions

        assert any(ext.lower() == ".t02" for ext in extensions)
        assert any(ext.lower() == ".t00" for ext in extensions)

    def test_has_supported_extension(self):
        """Test extension checking."""
        converter = TrimbleConverter("TEST")

        assert converter._has_supported_extension(Path("/test/file.T02"))
        assert converter._has_supported_extension(Path("/test/file.t02"))
        assert converter._has_supported_extension(Path("/test/file.T00"))
        assert not converter._has_supported_extension(Path("/test/file.sbf"))

    def test_keep_intermediate_option(self):
        """Test keep intermediate files option."""
        converter = TrimbleConverter("TEST", keep_intermediate=True)
        assert converter.keep_intermediate is True

        converter = TrimbleConverter("TEST", keep_intermediate=False)
        assert converter.keep_intermediate is False


# Integration-style tests (mocked)
class TestConverterIntegration:
    """Integration tests with mocked external tools."""

    @patch("subprocess.run")
    def test_sbf_conversion_subprocess_call(self, mock_run):
        """Test that subprocess is called correctly for SBF conversion.

        Uses simple workflow from mall_septentrio.sh:
            sbf2rin -v -f input.sbf -o output.rnx
        """
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        converter = SBFConverter("ELDC")

        # Mock get_tool_path
        converter.get_tool_path = Mock(return_value=Path("/opt/rxtools/bin/sbf2rin"))

        # Build command with output file (not directory)
        cmd = converter._build_sbf2rin_command(
            Path("/opt/rxtools/bin/sbf2rin"),
            Path("/test/ELDC202601150000a.sbf"),
            Path("/test/output/ELDC0150.26oT"),  # Output file with T suffix
        )

        # Verify command structure (simple workflow)
        assert "/opt/rxtools/bin/sbf2rin" in cmd
        assert "-v" in cmd  # Verbose flag
        assert "-f" in cmd  # Input file flag
        assert "-o" in cmd  # Output file flag
        assert "/test/output/ELDC0150.26oT" in cmd  # Output file path
        # Note: RINEX 3 is default, no -R3 flag needed

    def test_sbf_rinex2_command(self):
        """Test RINEX 2 output adds version flag."""
        converter = SBFConverter("ELDC", rinex_version=RinexVersion.RINEX_2)

        cmd = converter._build_sbf2rin_command(
            Path("/opt/rxtools/bin/sbf2rin"),
            Path("/test/ELDC202601150000a.sbf"),
            Path("/test/output/ELDC0150.26oT"),
        )

        assert "-R211" in cmd  # RINEX 2.11 flag

    def test_sbf_rinex4_command(self):
        """Test RINEX 4 output adds version flag."""
        converter = SBFConverter("ELDC", rinex_version=RinexVersion.RINEX_4)

        cmd = converter._build_sbf2rin_command(
            Path("/opt/rxtools/bin/sbf2rin"),
            Path("/test/ELDC202601150000a.sbf"),
            Path("/test/output/ELDC0150.26oT"),
        )

        assert "-R4" in cmd  # RINEX 4 flag


class TestRinexFieldFormatting:
    """Tests for RINEX field formatting utilities."""

    def test_format_marker_name(self):
        """Test MARKER NAME formatting: 60 chars, uppercase."""
        result = format_rinex_field("MARKER NAME", "eldc")
        assert result == "ELDC" + " " * 56
        assert len(result) == 60

        # Empty value returns None
        assert format_rinex_field("MARKER NAME", "") is None
        assert format_rinex_field("MARKER NAME", None) is None

    def test_format_marker_number(self):
        """Test MARKER NUMBER formatting: 20 chars."""
        result = format_rinex_field("MARKER NUMBER", "12345M001")
        assert result.startswith("12345M001")
        assert len(result) == 20

    def test_format_observer_agency_tuple(self):
        """Test OBSERVER / AGENCY with tuple input."""
        result = format_rinex_field("OBSERVER / AGENCY", ("BGO", "IMO"))
        assert len(result) == 60
        assert result[:20].startswith("BGO")
        assert result[20:].startswith("IMO")

    def test_format_observer_agency_string(self):
        """Test OBSERVER / AGENCY with string input (splits on space)."""
        result = format_rinex_field("OBSERVER / AGENCY", "BGO IMO")
        assert len(result) == 60
        assert result[:20].startswith("BGO")
        assert result[20:].startswith("IMO")

    def test_format_receiver(self):
        """Test REC # / TYPE / VERS formatting: 3 x 20 chars."""
        result = format_rinex_field(
            "REC # / TYPE / VERS", ("1234567", "SEPT POLARX5", "5.4.0")
        )
        assert len(result) == 60
        assert result[:20].startswith("1234567")
        assert "SEPT POLARX5" in result
        assert "5.4.0" in result

    def test_format_receiver_string(self):
        """Test REC # / TYPE / VERS with string input."""
        result = format_rinex_field("REC # / TYPE / VERS", "1234567 SEPT 5.4.0")
        assert len(result) == 60
        assert result[:20].startswith("1234567")

    def test_format_antenna(self):
        """Test ANT # / TYPE formatting: 2 x 20 chars."""
        result = format_rinex_field(
            "ANT # / TYPE", ("CR620012345", "ASH701945C_M    SCIS")
        )
        assert len(result) == 40
        assert result[:20].startswith("CR620012345")
        assert "ASH701945C_M" in result

        # A known type with an unknown serial still writes the type — dropping
        # the field would discard the part we do know.
        no_serial = format_rinex_field("ANT # / TYPE", ("", "TYPE"))
        assert no_serial is not None
        assert no_serial[:20].strip() == ""
        assert no_serial[20:].strip() == "TYPE"

        # Neither part known = nothing to write.
        assert format_rinex_field("ANT # / TYPE", ("", "")) is None

    def test_format_antenna_delta(self):
        """Test ANTENNA: DELTA H/E/N formatting: 3 x F14.4."""
        result = format_rinex_field("ANTENNA: DELTA H/E/N", (0.0089, 0.0, 0.0))
        assert len(result) == 42
        assert "0.0089" in result

        # Single value (height only)
        result = format_rinex_field("ANTENNA: DELTA H/E/N", 1.5)
        assert len(result) == 42
        assert "1.5000" in result

    def test_format_position_xyz(self):
        """Test APPROX POSITION XYZ formatting: 3 x F14.4."""
        result = format_rinex_field(
            "APPROX POSITION XYZ", (2679689.5678, -727951.1234, 5722788.9012)
        )
        assert len(result) == 42
        assert "2679689.5678" in result

    def test_format_interval(self):
        """Test INTERVAL formatting: F10.3."""
        result = format_rinex_field("INTERVAL", 15.0)
        assert len(result) == 10
        assert "15.000" in result

    def test_format_unknown_field(self):
        """Test unknown field returns string as-is."""
        result = format_rinex_field("CUSTOM FIELD", "value")
        assert result == "value"

        # Empty unknown field returns None
        assert format_rinex_field("CUSTOM FIELD", "") is None

    def test_format_antenna_type_with_radome(self):
        """Test antenna type + radome formatting: 15 + space + 4 = 20 chars."""
        result = format_antenna_type_with_radome("ASH701945C_M", "SCIS")
        assert len(result) == 20
        assert result == "ASH701945C_M    SCIS"

        # Default radome is NONE
        result = format_antenna_type_with_radome("SEPPOLANT_X_MF", "")
        assert result.endswith("NONE")

    def test_rinex_field_specs_defined(self):
        """Test that RINEX_FIELD_SPECS contains expected fields."""
        assert "MARKER NAME" in RINEX_FIELD_SPECS
        assert "REC # / TYPE / VERS" in RINEX_FIELD_SPECS
        assert "ANT # / TYPE" in RINEX_FIELD_SPECS
        assert "ANTENNA: DELTA H/E/N" in RINEX_FIELD_SPECS

        # Check format tuples
        marker_format, marker_width = RINEX_FIELD_SPECS["MARKER NAME"]
        assert marker_width == 60
        assert "A60" in marker_format
