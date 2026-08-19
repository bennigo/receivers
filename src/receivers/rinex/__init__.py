"""
RINEX conversion module for GPS receiver data.

This module provides converters for converting raw GPS receiver data (SBF, T02, T00, m00)
to RINEX format with proper header metadata from TOS database.

Main Components:
    - RawToRinexConverter: Abstract base class for all converters
    - SBFConverter: Septentrio SBF -> RINEX (using sbf2rin)
    - TrimbleConverter: Trimble T02/T00 -> RINEX (using runpkr00 + teqc + gfzrnx)
    - TrimbleNativeConverter: Trimble T02/T00 -> native RINEX 3 (using Docker)
    - LeicaConverter: Leica m00 -> RINEX (using mdb2rinex or teqc + gfzrnx)
    - MetadataProvider: Historical metadata lookup from TOS database
    - RinexNamer: Short/long RINEX filename conventions
"""

from .ashtech_converter import AshtechConverter
from .converter_base import (
    BatchConversionResult,
    ConversionError,
    ConversionResult,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)
from .leica_converter import G10Converter, LeicaConverter
from .metadata_provider import (
    RINEX_FIELD_SPECS,
    EquipmentMetadata,
    MetadataProvider,
    format_antenna_type_with_radome,
    format_rinex_field,
)
from .rinex_namer import NamingConvention, RinexNamer
from .sbf_converter import SBFConverter
from .trimble_converter import (
    NetR9Converter,
    NetRSConverter,
    R00Converter,
    TrimbleConverter,
)
from .trimble_native_converter import TrimbleNativeConverter

__all__ = [
    # Base classes
    "RawToRinexConverter",
    "ConversionResult",
    "ConversionError",
    "BatchConversionResult",
    "RinexVersion",
    "OutputFormat",
    # Metadata
    "MetadataProvider",
    "EquipmentMetadata",
    # RINEX formatting utilities
    "format_rinex_field",
    "format_antenna_type_with_radome",
    "RINEX_FIELD_SPECS",
    # Naming
    "RinexNamer",
    "NamingConvention",
    # Converters
    "SBFConverter",
    "TrimbleConverter",
    "TrimbleNativeConverter",
    "AshtechConverter",
    "NetR9Converter",
    "NetRSConverter",
    "R00Converter",
    "LeicaConverter",
    "G10Converter",
]
