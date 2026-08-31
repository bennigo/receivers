"""
Metadata provider for RINEX header corrections.

This module provides:
1. RINEX header formatting utilities (format_rinex_field, format_antenna_type_with_radome)
2. EquipmentMetadata dataclass for representing station equipment
3. MetadataProvider for config-based metadata lookup

Note: TOS database queries for RINEX header correction are now handled by
tostools.rinex.correct_rinex_from_tos(). This module focuses on formatting
utilities and config-based metadata.

RINEX Header Field Formatting:
    RINEX uses fixed-width Fortran format. Field specifications are based on
    RINEX 3.x standard and tostools/rinex/reader.py:

    Field Name              Format          Width   Description
    -----------------------------------------------------------------
    MARKER NAME             A60             60      Station identifier
    MARKER NUMBER           A20             20      IERS DOMES number
    OBSERVER / AGENCY       A20,A40         60      Observer + Agency
    REC # / TYPE / VERS     A20,A20,A20     60      Serial + Type + Version
    ANT # / TYPE            A20,A20         40      Serial + Type (with radome)
    APPROX POSITION XYZ     3F14.4          42      ECEF X, Y, Z
    ANTENNA: DELTA H/E/N    3F14.4          42      Height, East, North offsets
    INTERVAL                F10.3           10      Sampling interval
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# RINEX header field formatting is generic (not receiver-specific) and now
# lives in tostools.rinex.formatter. These re-exports keep the receivers
# import surface stable for existing callers.
from tostools.device import PUBLISHED_UNKNOWN_ANTENNA_SERIAL, is_synthetic_serial
from tostools.rinex.formatter import (  # noqa: F401
    RINEX_FIELD_SPECS,
    format_antenna_type_with_radome,
    format_rinex_field,
)


@dataclass
class EquipmentMetadata:
    """Equipment metadata for a specific time period.

    Contains all information needed to populate RINEX header fields
    for equipment at a station during a specific time period.
    """

    # Time period
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None

    # Receiver information
    receiver_model: str = ""
    receiver_serial: str = ""
    receiver_firmware: str = ""

    # Antenna information
    antenna_model: str = ""
    antenna_serial: str = ""
    antenna_height: float = 0.0

    # Radome information
    radome_model: str = ""

    # Monument information
    monument_height: float = 0.0
    #: East/north eccentricity, summed across the TOS monument and antenna
    #: entities the same way the height is. See to_rinex_corrections.
    antenna_offset_east: float = 0.0
    antenna_offset_north: float = 0.0

    # Station information
    marker_name: str = ""
    marker_number: str = ""
    iers_domes: str = ""

    # Observer/Agency
    observer: str = "GNSS OPERATOR"
    agency: str = "IMO"

    # Additional fields
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_rinex_corrections(
        self,
        overrides: Optional[Dict[str, Any]] = None,
        include_receiver: bool = False,
    ) -> Dict[str, str]:
        """Convert metadata to RINEX header correction dictionary.

        Generates properly formatted RINEX header corrections using fixed-width
        Fortran format per RINEX 3.x specification.

        Args:
            overrides: Optional dictionary of additional/override corrections.
                       Keys are RINEX field names, values can be:
                       - str: Single value
                       - tuple/list: Multi-part value (e.g., (observer, agency))
                       - None: Explicitly skip this field (remove from output)
                       Any field in overrides replaces the auto-generated value.

            include_receiver: If True, include REC # / TYPE / VERS from metadata.
                              Default False because converter tools (sbf2rin)
                              usually extract correct values from raw data files.

        Returns:
            Dictionary mapping RINEX field names to formatted values.
            Only fields with non-empty values are included.

        Examples:
            # Basic usage - auto-generates from metadata
            >>> corrections = metadata.to_rinex_corrections()

            # Include receiver info (if needed for tools that don't embed it)
            >>> corrections = metadata.to_rinex_corrections(include_receiver=True)

            # Override specific fields
            >>> corrections = metadata.to_rinex_corrections(
            ...     overrides={
            ...         "REC # / TYPE / VERS": ("1234567", "SEPT POLARX5", "5.6.0"),
            ...         "MARKER NAME": None,  # Skip this field
            ...     }
            ... )
        """
        # Build base values from metadata (unformatted)
        base: Dict[str, Any] = {}

        if self.marker_name:
            base["MARKER NAME"] = self.marker_name

        if self.marker_number:
            base["MARKER NUMBER"] = self.marker_number

        if self.observer or self.agency:
            base["OBSERVER / AGENCY"] = (self.observer or "", self.agency or "")

        # Receiver: only if explicitly requested
        if include_receiver and (self.receiver_serial or self.receiver_model):
            base["REC # / TYPE / VERS"] = (
                self.receiver_serial or "",
                self.receiver_model or "",
                self.receiver_firmware or "",
            )

        # Antenna. A TOS synthetic serial (<subtype>-<STID>-<YYYYMMDD>) must
        # never reach the header: it is an internal lookup key, and at 21 chars
        # it does not even fit the A20 field. VMEY 2026-08-01 onwards shows what
        # happens — `antenna-VMEY-20230111` was truncated to
        # `antenna-VMEY-2023011`, filling the column exactly so the serial abuts
        # the antenna type with no separator, and 65 archived headers now carry
        # a malformed ANT # / TYPE. It also defeats the reconstruct dup-guard,
        # which reads the truncated string back, fails to match TOS's 21-char
        # serial, and proposes creating a duplicate antenna.
        #
        # A suppressed serial is published as "0000", NOT blank.
        #
        # This used to write an empty field, citing the IGS instruction (tostools
        # cb02a03). That instruction is real, but M3G overrides it: an empty
        # antenna Serial Number is rejected outright (422, bisected against the
        # live API 2026-08-20 — M3G's own stored log with the serial emptied
        # returns 422; the same log with "0000" returns 200). site_log.py and
        # corrector.py moved to PUBLISHED_UNKNOWN_ANTENNA_SERIAL then; this path
        # was missed — and at the time it did write the converter's headers, so
        # 463 archived 2026 daily files still carry a blank serial field.
        #
        # THAT IS NO LONGER TRUE, and the stale claim cost a later session real
        # time: the converter now goes through tostools.correct_rinex_from_tos
        # (converter_base.py), and `to_rinex_corrections` has NO production
        # caller — only tests/test_no_synthetic_serial_in_header.py. Verified
        # 2026-08-31 by grepping both src trees. It stays correct and tested
        # rather than being left to rot into a trap for whoever adopts it, but
        # do not reason about live header output from this function.
        #
        # Must stay equal to tostools.rinex.validator's suppressed value, or a
        # freshly converted file reads back as discrepant and --fix-headers
        # re-injects what the converter just withheld.
        published_serial = self.antenna_serial or ""
        if is_synthetic_serial(published_serial):
            published_serial = PUBLISHED_UNKNOWN_ANTENNA_SERIAL
        # Emit on model OR serial — gating on the serial alone (the old
        # behaviour) would drop the antenna TYPE and RADOME along with a
        # suppressed serial, which is a worse header than a blank serial field.
        if published_serial or self.antenna_model:
            if self.antenna_model:
                ant_type = format_antenna_type_with_radome(
                    self.antenna_model, self.radome_model
                )
            else:
                ant_type = ""
            base["ANT # / TYPE"] = (published_serial, ant_type)

        # ANTENNA: DELTA H/E/N — a composite on all three axes. The east/north
        # terms were hardcoded to 0.0 here, the same defect the tostools
        # corrector's config branch carried: TOS splits the mark->ARP vector
        # across a monument and an antenna entity, and reading only the height
        # publishes a zeroed eccentricity against a site log that carries the
        # real one.
        total_height = self.antenna_height + self.monument_height
        base["ANTENNA: DELTA H/E/N"] = (
            total_height,
            self.antenna_offset_east,
            self.antenna_offset_north,
        )

        # Apply overrides
        if overrides:
            for key, value in overrides.items():
                if value is None:
                    # None means explicitly skip this field
                    base.pop(key, None)
                else:
                    base[key] = value

        # Format all fields using the general formatter
        corrections = {}
        for field_name, value in base.items():
            formatted = format_rinex_field(field_name, value)
            if formatted is not None:
                corrections[field_name] = formatted

        return corrections

    @classmethod
    def from_tos_session(cls, session: Dict[str, Any]) -> "EquipmentMetadata":
        """Create EquipmentMetadata from TOS device_history session.

        The TOS API returns device_history as a list of sessions, each with:
        - time_from, time_to: Session time period
        - gnss_receiver: Receiver details
        - antenna: Antenna details
        - radome: Radome details
        - monument: Monument details

        Args:
            session: TOS device_history session dictionary

        Returns:
            EquipmentMetadata instance
        """
        metadata = cls()

        # Parse time period
        if "time_from" in session:
            if isinstance(session["time_from"], datetime):
                metadata.time_from = session["time_from"]
            elif isinstance(session["time_from"], str):
                try:
                    metadata.time_from = datetime.fromisoformat(
                        session["time_from"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        if "time_to" in session:
            if isinstance(session["time_to"], datetime):
                metadata.time_to = session["time_to"]
            elif isinstance(session["time_to"], str) and session["time_to"]:
                try:
                    metadata.time_to = datetime.fromisoformat(
                        session["time_to"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        # Parse receiver
        receiver = session.get("gnss_receiver", {})
        if receiver:
            metadata.receiver_model = receiver.get("model", "")
            metadata.receiver_serial = receiver.get("serial_number", "")
            metadata.receiver_firmware = receiver.get("firmware_version", "")

        # Parse antenna
        antenna = session.get("antenna", {})
        if antenna:
            metadata.antenna_model = antenna.get("model", "")
            metadata.antenna_serial = antenna.get("serial_number", "")
            metadata.antenna_height = float(antenna.get("antenna_height", 0) or 0)
            metadata.antenna_offset_east = float(
                antenna.get("antenna_offset_east", 0) or 0
            )
            metadata.antenna_offset_north = float(
                antenna.get("antenna_offset_north", 0) or 0
            )

        # Parse radome
        radome = session.get("radome", {})
        if radome:
            metadata.radome_model = radome.get("model", "NONE")

        # Parse monument
        monument = session.get("monument", {})
        if monument:
            metadata.monument_height = float(monument.get("monument_height", 0) or 0)
            metadata.antenna_offset_east += float(
                monument.get("monument_offset_east", 0) or 0
            )
            metadata.antenna_offset_north += float(
                monument.get("monument_offset_north", 0) or 0
            )

        return metadata

    @classmethod
    def from_station_config(cls, station_config: Dict[str, Any]) -> "EquipmentMetadata":
        """Create EquipmentMetadata from gps_parser station configuration.

        The station configuration now includes RINEX metadata from teqc configs:
        - station_config['rinex'] - RINEX header info (observer, agency, marker)
        - station_config['antenna'] - Antenna info (type, serial, radome, height)
        - station_config['receiver'] - Receiver info (type)

        Args:
            station_config: Station configuration from gps_parser/config_utils

        Returns:
            EquipmentMetadata instance
        """
        metadata = cls()

        # Parse validity period from config
        rinex_config = station_config.get("rinex", {})
        valid_from = rinex_config.get("config_valid_from", "")
        if valid_from:
            try:
                metadata.time_from = datetime.strptime(valid_from, "%Y-%m-%d")
            except ValueError:
                metadata.time_from = datetime.now()
        else:
            metadata.time_from = datetime.now()
        metadata.time_to = None  # Current config, no end date

        # RINEX header metadata (from teqc configs)
        metadata.marker_name = rinex_config.get(
            "marker_name", station_config.get("station_id", "")
        )
        metadata.marker_number = rinex_config.get(
            "marker_number", station_config.get("station_id", "")
        )
        metadata.observer = rinex_config.get("observer", "GNSS OPERATOR")
        metadata.agency = rinex_config.get("agency", "IMO")

        # Receiver info
        receiver = station_config.get("receiver", {})
        if receiver:
            metadata.receiver_model = receiver.get("type", "")
            metadata.receiver_serial = receiver.get("serial", "")
            metadata.receiver_firmware = receiver.get("firmware", "")

        # Antenna info (from teqc configs)
        antenna = station_config.get("antenna", {})
        if antenna:
            metadata.antenna_model = antenna.get("type", "")
            metadata.antenna_serial = antenna.get("serial", "")
            metadata.antenna_height = float(antenna.get("height", 0) or 0)
            metadata.radome_model = antenna.get("radome", "NONE")

        return metadata


class MetadataProvider:
    """Provider for RINEX header metadata from station configuration.

    Note: TOS database queries are now handled by tostools.rinex.correct_rinex_from_tos().
    This class only provides config-based metadata lookup for use with EquipmentMetadata.

    For RINEX header correction with TOS support, use:
        from tostools.rinex import correct_rinex_from_tos
    """

    def __init__(
        self,
        loglevel: int = logging.INFO,
    ):
        """Initialize metadata provider.

        Args:
            loglevel: Logging level
        """
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.logger.setLevel(loglevel)
        self._config_cache: Dict[str, EquipmentMetadata] = {}

    def get_equipment_from_config(
        self,
        station_id: str,
    ) -> Optional[EquipmentMetadata]:
        """Get equipment metadata from station configuration.

        Args:
            station_id: Station identifier (e.g., 'ELDC')

        Returns:
            EquipmentMetadata from station config, or None if not found
        """
        station_id = station_id.upper()

        if station_id in self._config_cache:
            return self._config_cache[station_id]

        try:
            from ..config_utils import get_station_config

            config = get_station_config(station_id)
            if config is None:
                self.logger.warning(f"Station {station_id} not found in configuration")
                return None

            metadata = EquipmentMetadata.from_station_config(config)
            metadata.marker_name = station_id
            self._config_cache[station_id] = metadata
            return metadata

        except Exception as e:
            self.logger.error(f"Failed to get config for {station_id}: {e}")
            return None

    def clear_cache(self) -> None:
        """Clear cached metadata."""
        self._config_cache.clear()
