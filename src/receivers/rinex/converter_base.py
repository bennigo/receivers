"""
Abstract base class for raw-to-RINEX converters.

This module defines the interface that all raw format converters must implement,
along with common data structures for conversion results.
"""

import logging
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Union

from ..config.receivers_config import get_receivers_config


class RinexVersion(Enum):
    """RINEX format versions."""

    RINEX_2 = 2
    RINEX_3 = 3
    RINEX_4 = 4


class OutputFormat(Enum):
    """Output file format options (legacy compatibility)."""

    MODERN = "modern"  # .rnx.gz (no Hatanaka)
    LEGACY = "legacy"  # .D.Z (Hatanaka compressed)


class CompressionFormat(Enum):
    """File compression format options."""

    GZ = "gz"  # gzip (.gz)
    Z = "Z"  # Unix compress (.Z) - requires ncompress


class NamingConvention(Enum):
    """RINEX filename naming conventions."""

    SHORT = "short"  # RINEX 2 style: SSSS0DDF.YYt
    LONG = "long"  # IGS/RINEX 3+ style: SSSS00CCC_R_YYYYDDDHHMM_...


def _is_network_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like a transport/connection failure (DNS, refused,
    reset, timeout) rather than a data error. Checks the exception-class name so
    we don't need a hard dependency on ``requests`` here — covers
    requests.ConnectionError / ConnectTimeout / ReadTimeout / MaxRetryError /
    NameResolutionError and urllib3's equivalents."""
    chain = [e for e in (exc, exc.__cause__, exc.__context__) if e]
    # Any transport-layer OSError (socket.gaierror for DNS, ECONNREFUSED, …)
    if any(isinstance(e, OSError) for e in chain):
        return True
    names = {type(e).__name__ for e in chain}
    markers = (
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "MaxRetryError",
        "NameResolutionError",
        "NewConnectionError",
        "gaierror",
    )
    return any(any(m in n for m in markers) for n in names)


class NetworkUnavailableError(Exception):
    """TOS/network unreachable during header correction.

    Distinct from a per-file conversion failure: the metadata source is down,
    not the data. Re-rinex must NOT stage a header-less file, so this propagates
    out of ``convert_file`` to abort the run cleanly; fixing connectivity and
    re-running the same command resumes (already-staged files are skipped).
    """


# TOS-fetch retry policy: a seconds-long VPN/DNS resolver blip on the client
# (laptop Cisco tunnel rekey — 2026-07-05/06 incidents) must not abort a
# multi-hour archive run. Network failures are retried with linear backoff
# (30 s, 60 s) before the NetworkUnavailableError abort fires as the backstop.
_TOS_NETWORK_ATTEMPTS = 3
_TOS_NETWORK_RETRY_DELAY_S = 30


# Where each external tool lives in OUR ecosystem — a missing-tool error must
# tell the operator exactly what to install/symlink, not just a bare name.
# gps-tools binaries reach /usr/local/bin via install.sh Phase 8.
_TOOL_HINTS = {
    "sbf2rin": "Septentrio RxTools (/usr/local/rxtools/bin, install.sh Phase 8)",
    "bin2asc": "Septentrio RxTools (/usr/local/rxtools/bin)",
    "teqc": "gps-tools repo bin/ (symlinked to /usr/local/bin by install.sh)",
    "runpkr00": "gps-tools repo bin/ (Trimble .T02/.T00 extraction)",
    "gfzrnx": "gps-tools repo bin/ or system PATH",
    "CRX2RNX": "gps-tools repo bin/ (Hatanaka)",
    "RNX2CRX": "gps-tools repo bin/ (Hatanaka)",
    "trm2rinex": "Docker image (TrimbleNativeConverter; docker must be running)",
    "compress": "ncompress package (apt install ncompress) — legacy .Z output",
}


class ConversionError(Exception):
    """Exception raised during RINEX conversion."""

    def __init__(
        self,
        message: str,
        raw_file: Optional[Path] = None,
        details: Optional[str] = None,
    ):
        self.message = message
        self.raw_file = raw_file
        self.details = details
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        msg = self.message
        if self.raw_file:
            msg += f" (file: {self.raw_file})"
        if self.details:
            msg += f"\nDetails: {self.details}"
        return msg


def validation_epilog(results) -> Optional[str]:
    """End-of-batch summary of raw-validation refusals, with suggested fixes.

    Returns None when nothing was refused. Grouped by category and capped per
    group — the procedure the operator follows AFTER a sweep, kept out of the
    per-file mid-run stream on purpose.
    """
    refused = [r for r in results if getattr(r, "validation_category", None)]
    if not refused:
        return None
    from collections import defaultdict

    by_cat: dict = defaultdict(list)
    for r in refused:
        by_cat[r.validation_category].append(r)
    lines = [
        f"raw-content validation refused {len(refused)} file(s) this batch — "
        "review before re-running:"
    ]
    for cat in ("wrong-format", "wrong-date", "wrong-station"):
        rs = by_cat.pop(cat, [])
        if not rs:
            continue
        lines.append(f"  [{cat}] {len(rs)} file(s):")
        for r in rs[:15]:
            first = r.message.splitlines()[0] if r.message else ""
            lines.append(f"    {Path(r.raw_file).name}: {first[:120]}")
        if len(rs) > 15:
            lines.append(f"    ... and {len(rs) - 15} more")
        for sug in sorted(
            {r.validation_suggestion for r in rs if r.validation_suggestion}
        ):
            lines.append(f"    → {sug}")
    for cat, rs in by_cat.items():  # any future category
        lines.append(f"  [{cat}] {len(rs)} file(s)")
    return "\n".join(lines)


class RawValidationError(ConversionError):
    """A raw-content validation gate refused this file (not a tool failure).

    ``category``: 'wrong-format' | 'wrong-date' | 'wrong-station'.
    ``suggestion``: the operator action that actually fixes it. Mid-run these
    log as ONE compact line; detail + suggestions surface in the end-of-batch
    epilog (validation_epilog) so a sweep isn't cluttered.
    """

    def __init__(self, message, raw_file=None, *, category: str, suggestion: str = ""):
        super().__init__(message, raw_file)
        self.category = category
        self.suggestion = suggestion


@dataclass
class ConversionResult:
    """Result of a single file conversion."""

    raw_file: Path
    rinex_file: Optional[Path] = None
    success: bool = False
    message: str = ""
    duration_seconds: float = 0.0
    header_corrections_applied: int = 0
    warnings: List[str] = field(default_factory=list)
    # Set when a raw-content validation gate refused the file (see
    # RawValidationError) — batch runs aggregate these into an end epilog.
    validation_category: Optional[str] = None
    validation_suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "raw_file": str(self.raw_file),
            "rinex_file": str(self.rinex_file) if self.rinex_file else None,
            "success": self.success,
            "message": self.message,
            "duration_seconds": self.duration_seconds,
            "header_corrections_applied": self.header_corrections_applied,
            "warnings": self.warnings,
        }


@dataclass
class BatchConversionResult:
    """Result of batch conversion."""

    station_id: str
    total_files: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    results: List[ConversionResult] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    @property
    def total_duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return sum(r.duration_seconds for r in self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "station_id": self.station_id,
            "total_files": self.total_files,
            "successful": self.successful,
            "failed": self.failed,
            "skipped": self.skipped,
            "total_duration_seconds": self.total_duration_seconds,
            "results": [r.to_dict() for r in self.results],
        }


class RawToRinexConverter(ABC):
    """Abstract base class for raw GPS data to RINEX converters.

    Subclasses implement conversion for specific raw formats:
    - SBFConverter: Septentrio Binary Format (.sbf)
    - TrimbleConverter: Trimble formats (.T02, .T00)
    - LeicaConverter: Leica MDB format (.m00)

    Each converter handles:
    1. Raw format conversion using appropriate external tools
    2. Header correction using TOS database metadata
    3. Output naming according to short/long conventions
    """

    # Probed once per class in _apply_header_corrections: does the installed
    # tostools correct_rinex_from_tos accept the tos_metadata_cache kwarg?
    # None = not probed yet.
    _tos_cache_kw_supported: ClassVar[Optional[bool]] = None

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: Optional[OutputFormat] = None,
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        apply_hatanaka: Optional[bool] = None,
        compression_format: Optional[CompressionFormat] = None,
        loglevel: int = logging.INFO,
        session_type: Optional[str] = None,
    ):
        """Initialize converter.

        Args:
            station_id: Station identifier (e.g., 'ELDC')
            rinex_version: Target RINEX version (2, 3, or 4)
            output_format: Legacy parameter for backwards compatibility.
                          Use apply_hatanaka and compression_format instead.
            naming_convention: Filename convention (SHORT or LONG).
                              If None, defaults based on rinex_version:
                              RINEX_2 -> SHORT, RINEX_3/4 -> LONG
            apply_header_corrections: Whether to apply TOS metadata corrections
            apply_hatanaka: Apply Hatanaka compression (.YYd vs .YYo).
                           If None, reads from config default_hatanaka.
            compression_format: File compression (GZ or Z).
                               If None, reads from config default_compression.
            loglevel: Logging level
        """
        self.station_id = station_id.upper()
        self.rinex_version = rinex_version
        self.apply_header_corrections = apply_header_corrections
        # session_type drives hourly vs daily filename session letter; None = daily
        self.session_type = session_type
        # strict_hatanaka: a Hatanaka failure FAILS the file instead of
        # falling back to an uncompacted .o product. Re-rinex sets this —
        # the archive convention is .D.Z, and a silent .o.Z fallback both
        # pollutes the archive and never matches the resume-skip name, so
        # the date is re-converted (and re-pushed) on every run. Default
        # False keeps the daily pipeline's degraded-but-present behavior.
        self.strict_hatanaka = False
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.logger.setLevel(loglevel)

        # Load configuration
        self.config = get_receivers_config()
        self._tool_paths: Dict[str, Path] = {}

        # Handle legacy output_format parameter
        if output_format is not None:
            if output_format == OutputFormat.LEGACY:
                self.apply_hatanaka = True
                self.compression_format = CompressionFormat.Z
            else:
                self.apply_hatanaka = False
                self.compression_format = CompressionFormat.GZ
        else:
            # Read from config or use defaults
            rinex_config = self.config.get_rinex_config()

            if apply_hatanaka is not None:
                self.apply_hatanaka = apply_hatanaka
            else:
                config_hatanaka = rinex_config.get("default_hatanaka", True)
                if isinstance(config_hatanaka, str):
                    self.apply_hatanaka = config_hatanaka.lower() in (
                        "true",
                        "yes",
                        "1",
                    )
                else:
                    self.apply_hatanaka = bool(config_hatanaka)

            if compression_format is not None:
                self.compression_format = compression_format
            else:
                config_compression = str(rinex_config.get("default_compression", "gz"))
                if config_compression.upper() == "Z":
                    self.compression_format = CompressionFormat.Z
                else:
                    self.compression_format = CompressionFormat.GZ

        # Keep output_format for backwards compatibility
        if self.apply_hatanaka:
            self.output_format = OutputFormat.LEGACY
        else:
            self.output_format = OutputFormat.MODERN

        # Naming convention: explicit > config > version-based default
        if naming_convention is not None:
            self.naming_convention = naming_convention
        else:
            rinex_cfg = self.config.get_rinex_config()
            config_naming = str(rinex_cfg.get("default_naming", "")).lower()
            if config_naming == "short":
                self.naming_convention = NamingConvention.SHORT
            elif config_naming == "long":
                self.naming_convention = NamingConvention.LONG
            elif rinex_version == RinexVersion.RINEX_2:
                self.naming_convention = NamingConvention.SHORT
            else:
                self.naming_convention = NamingConvention.LONG

    # Where each external tool lives in OUR ecosystem — so a missing-tool
    # error tells the operator exactly what to install/symlink instead of a
    # bare name. gps-tools binaries reach /usr/local/bin via install.sh
    # Phase 8. Module-level so subclasses share it (see get_tool_path).

    # Raw formats (receivers.archive.raw_format identifiers) this converter can
    # actually decode — the CONTENT gate. The archive's extensions lie (.atc
    # covers Ashtech U/R AND Septentrio SBF), and the wrong decoder either
    # segfaults or silently emits nothing. None = no content gate (converters
    # for formats the classifier doesn't know). UNKNOWN always passes — the
    # gate refuses only a POSITIVE wrong identification.
    accepted_raw_formats: Optional[frozenset] = None

    @property
    @abstractmethod
    def supported_extensions(self) -> List[str]:
        """Return list of supported raw file extensions (e.g., ['.sbf', '.sbf.gz'])."""
        pass

    @property
    @abstractmethod
    def converter_name(self) -> str:
        """Return name of the external converter tool (e.g., 'sbf2rin')."""
        pass

    @abstractmethod
    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run the actual conversion process.

        Args:
            raw_file: Path to raw input file
            output_dir: Directory for output RINEX file
            observation_date: Date of observation for naming

        Returns:
            Path to converted RINEX file (before header correction/renaming)

        Raises:
            ConversionError: If conversion fails
        """
        pass

    # What decodes each raw format in our toolchain — used by the format gate
    # so a refusal tells the operator the RIGHT chain, and by missing-tool
    # errors so they say where the tool lives.
    _FORMAT_CHAINS = {
        "sbf": "sbf2rin (Septentrio RxTools)",
        "trimble": "runpkr00+teqc or trm2rinex (Docker)",
        "ashtech_u": "teqc -ash u (gps-tools; no receivers converter yet — todo #56)",
        "ashtech_r": "teqc -ash r (gps-tools; no receivers converter yet — todo #56)",
    }

    def _raw_validation_enabled(self) -> bool:
        v = self.config.get_rinex_config().get("raw_validation", True)
        return str(v).lower() not in ("false", "no", "0")

    def _validate_raw_content(
        self, raw_path: Path, observation_date: Optional[datetime]
    ) -> None:
        """Content gate: magic-byte format check (cheap, 64 bytes).

        The archive's raw extensions lie (.atc covers Ashtech U-file, R-file
        AND Septentrio SBF), and the wrong decoder segfaults or silently emits
        nothing — refuse a POSITIVE wrong identification before decoding.
        Date/station validation deliberately lives in the POST-conversion
        identity gate instead: it is free there (the header is already
        decoded), while a pre-decode date check (teqc +meta) would read the
        whole raw file a second time on the hot reconciler path. Gate errors
        fail OPEN (log + continue) — validation must never block a legitimate
        conversion because of its own hiccup.
        """
        if not self._raw_validation_enabled():
            return
        try:
            from ..archive.raw_format import UNKNOWN, classify_raw

            fmt = classify_raw(raw_path)
        except Exception as exc:  # noqa: BLE001 - gate is fail-open
            self.logger.warning(f"raw content classification failed: {exc}")
            return
        if (
            self.accepted_raw_formats is not None
            and fmt != UNKNOWN
            and fmt not in self.accepted_raw_formats
        ):
            chain = self._FORMAT_CHAINS.get(fmt, "no known chain")
            raise RawValidationError(
                f"raw content is '{fmt}', not a {self.converter_name} input — "
                f"the extension lies. That format needs: {chain}",
                raw_path,
                category="wrong-format",
                suggestion=(
                    f"decode with {chain}; if also misfiled, run "
                    f"'receivers archive-sort --file <rel-path>' first"
                ),
            )

    # APPROX POSITION farther than this from the station's surveyed coordinates
    # means the raw is NOT this station. Shared with archive-sort — ONE metric
    # for position identity. Configurable via [rinex] position_gate_m.
    _POSITION_GATE_M = 10.0

    def _verify_conversion_identity(
        self, rinex_file: Path, observation_date: Optional[datetime]
    ) -> None:
        """Identity gate on the RAW conversion output, before header corrections.

        Two checks, per the station-identity model (TOS is canonical for
        marker/antenna; the raw-derived coordinates CONFIRM the identity):

        * first-obs date must match the claimed observation date (catches
          misfiled Trimble raw, which has no cheap pre-decode date check);
        * APPROX POSITION must be within the position gate of the station's
          surveyed coordinates — a wrong-station file passes every filename
          check but cannot fake where the receiver stood.

        On failure the output is DELETED (a wrong product must not survive)
        and ConversionError raised. Missing header fields fail open.
        """
        if not self._raw_validation_enabled():
            return
        try:
            first_obs, xyz, marker = self._read_identity_header(rinex_file)
        except Exception as exc:  # noqa: BLE001 - gate is fail-open
            self.logger.warning(f"identity header read failed: {exc}")
            return

        if (
            first_obs is not None
            and observation_date is not None
            and first_obs != observation_date.date()
        ):
            rinex_file.unlink(missing_ok=True)
            raise RawValidationError(
                f"converted output starts {first_obs} but this file claims "
                f"{observation_date.date()} — misfiled raw",
                rinex_file,
                category="wrong-date",
                suggestion=(
                    "the receiver's embedded time is authoritative — relocate "
                    "with 'receivers archive-sort --file <rel-path>' (dry-run "
                    "first), then convert at the TRUE date"
                ),
            )

        if marker and self.station_id not in marker.upper():
            self.logger.warning(
                f"raw marker '{marker}' does not mention {self.station_id} — "
                "position check decides (TOS is canonical for the marker)"
            )

        if xyz is None or all(abs(c) < 1.0 for c in xyz):
            return  # no usable position in the raw header — nothing to confirm
        gate_m = float(
            self.config.get_rinex_config().get("position_gate_m", self._POSITION_GATE_M)
        )
        expected = self._expected_station_xyz()
        if expected is None:
            return
        dist = sum((a - b) ** 2 for a, b in zip(xyz, expected)) ** 0.5
        if dist > gate_m:
            # Outside the tight gate: mirror archive-sort's nuance before
            # refusing. A degraded single-point solution tens of metres off
            # its OWN mark is a quality note, not an identity problem — the
            # legacy RHOF 2009-2011 days sit 10-100 m out and are RHOF beyond
            # doubt (no other station within kilometres).
            nearest, near_dist = self._nearest_fleet_station(xyz)
            if (
                nearest == self.station_id.upper() or nearest is None
            ) and dist <= 1000.0:
                self.logger.warning(
                    f"position noisy: {dist:.0f} m from {self.station_id}'s mark "
                    f"(gate {gate_m:.0f} m) but no other station is nearer — "
                    "identity accepted, solution quality flagged"
                )
            else:
                rinex_file.unlink(missing_ok=True)
                hint = (
                    f"nearest station is {nearest} at {near_dist:.0f} m"
                    if nearest
                    else "matches no station"
                )
                raise RawValidationError(
                    f"raw-derived position is {dist / 1000.0:.1f} km from "
                    f"{self.station_id}'s surveyed coordinates ({hint}) — "
                    "this raw is NOT this station",
                    rinex_file,
                    category="wrong-station",
                    suggestion=(
                        "identify the true station (decode + compare APPROX "
                        "POSITION against stations.cfg) and move the file to "
                        "that station's tree — needs eyes"
                    ),
                )
        else:
            self.logger.debug(
                f"identity confirmed: position within {dist:.1f} m of {self.station_id}"
            )

    def _nearest_fleet_station(self, xyz):
        """(station, distance_m) of the fleet station nearest to ECEF xyz —
        the same identity semantics as archive-sort. Best-effort (None, inf)."""
        try:
            import pyproj

            from ..archive.sort import fleet_coordinates, nearest_station

            tr = pyproj.Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
            lon, lat, _h = tr.transform(*xyz)
            return nearest_station(lat, lon, fleet_coordinates())
        except Exception:  # noqa: BLE001 - fail-open
            return None, float("inf")

    @staticmethod
    def _read_identity_header(
        rinex_file: Path,
    ) -> "tuple[Optional[Any], Optional[tuple], Optional[str]]":
        """(first_obs_date, approx_xyz, marker_name) from a RINEX obs header."""
        from datetime import date as _date

        first_obs = xyz = marker = None
        with open(rinex_file, encoding="latin-1", errors="replace") as fh:
            for i, line in enumerate(fh):
                label = line[60:].strip()
                if label == "TIME OF FIRST OBS":
                    parts = line[:60].split()
                    try:
                        first_obs = _date(int(parts[0]), int(parts[1]), int(parts[2]))
                    except (ValueError, IndexError):
                        pass
                elif label == "APPROX POSITION XYZ":
                    try:
                        x, y, z = (float(v) for v in line[:60].split()[:3])
                        xyz = (x, y, z)
                    except (ValueError, IndexError):
                        pass
                elif label == "MARKER NAME":
                    marker = line[:60].strip()
                elif label == "END OF HEADER" or i > 300:
                    break
        return first_obs, xyz, marker

    def _expected_station_xyz(self) -> Optional[tuple]:
        """Station's surveyed coordinates as ECEF, from stations.cfg."""
        try:
            from ..config_utils import get_station_config

            cfg = get_station_config(self.station_id, silent=True) or {}
            lat, lon, hgt = (
                cfg.get("latitude"),
                cfg.get("longitude"),
                cfg.get("height"),
            )
            if lat is None or lon is None:
                return None
            import pyproj

            tr = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
            x, y, z = tr.transform(float(lon), float(lat), float(hgt or 0.0))
            return (x, y, z)
        except Exception as exc:  # noqa: BLE001 - gate is fail-open
            self.logger.debug(f"no expected coordinates for {self.station_id}: {exc}")
            return None

    def convert_file(
        self,
        raw_file: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        observation_date: Optional[datetime] = None,
        force: bool = False,
    ) -> ConversionResult:
        """Convert a single raw file to RINEX format.

        Args:
            raw_file: Path to raw input file
            output_dir: Output directory (default: same as input)
            observation_date: Date of observation (extracted from filename if not provided)
            force: Overwrite existing output files

        Returns:
            ConversionResult with conversion details
        """
        import time

        start_time = time.time()
        raw_path = Path(raw_file)
        result = ConversionResult(raw_file=raw_path)
        work_file: Optional[Path] = None

        try:
            # Validate input file
            if not raw_path.exists():
                raise ConversionError("Raw file not found", raw_path)

            if not self._has_supported_extension(raw_path):
                raise ConversionError(
                    f"Unsupported file extension. Expected one of: {self.supported_extensions}",
                    raw_path,
                )

            # Set output directory
            if output_dir is None:
                output_dir = raw_path.parent
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            # Extract observation date from filename if not provided
            if observation_date is None:
                observation_date = self._extract_date_from_filename(raw_path)

            self.logger.info(
                f"Converting {raw_path.name} for {observation_date.date()}"
            )

            # Content gate BEFORE decoding: magic-byte format check (variable
            # raw types — extensions lie) + decoded-date vs claimed-date where
            # a cheap decoder exists (teqc +meta). Misfiled/mislabeled raw must
            # never become a wrongly-dated RINEX product.
            self._validate_raw_content(raw_path, observation_date)

            # Run conversion
            rinex_file = self._run_conversion(raw_path, output_path, observation_date)
            # Track the in-archive working file so an interrupted chain cannot
            # strand it (see the cleanup in the finally block below).
            work_file = rinex_file

            # Identity gate BEFORE header corrections: the raw-derived header
            # (first obs epoch + APPROX POSITION) must match the station/date
            # this file CLAIMS to be — corrections would overwrite exactly the
            # evidence (TOS marker + surveyed coords), so check first.
            self._verify_conversion_identity(rinex_file, observation_date)

            # Apply header corrections if enabled
            corrections_applied = 0
            if self.apply_header_corrections:
                corrections_applied = self._apply_header_corrections(
                    rinex_file, observation_date
                )

            # Canonicalize header order via gfzrnx (piece 3: matches the EPOS
            # path's header order so operational and EPOS files are directly
            # comparable). Keeps the current filename; gfzrnx rewrites in place.
            rinex_file = self._canonicalize_rinex(rinex_file, observation_date)

            # Rename to final naming convention
            final_file = self._rename_to_convention(rinex_file, observation_date)

            # Apply naming-gated header records (piece 4): long names get a
            # 9-char MARKER NAME (RHOF00ISL); short names keep the 4-char marker
            # the converter emitted — safe for GAMIT processing until confirmed.
            final_file = self._apply_naming_headers(final_file, observation_date)

            # Apply compression if needed
            final_file = self._apply_compression(final_file)

            result.rinex_file = final_file
            result.success = True
            result.message = "Conversion successful"
            result.header_corrections_applied = corrections_applied

        except NetworkUnavailableError:
            # Metadata source down, not a per-file failure — propagate so the run
            # aborts cleanly and a re-run resumes (the final compressed file is
            # never written: corrections precede compression, so no header-less
            # product is staged).
            raise

        except RawValidationError as e:
            # Gate refusal, not a tool failure: ONE compact line mid-run —
            # the detail + suggested fix surfaces in the end-of-batch epilog
            # (validation_epilog) so sweeps stay readable.
            result.success = False
            result.message = str(e)
            result.validation_category = e.category
            result.validation_suggestion = e.suggestion
            self.logger.warning(
                f"raw-validation refused {raw_path.name} ({e.category})"
            )

        except ConversionError as e:
            result.success = False
            result.message = str(e)
            self.logger.error(f"Conversion failed: {e}")

        except Exception as e:
            result.success = False
            result.message = f"Unexpected error: {e}"
            self.logger.exception(f"Unexpected error converting {raw_path}")

        finally:
            result.duration_seconds = time.time() - start_time
            self._discard_stranded_work_file(work_file, result)

        return result

    def _discard_stranded_work_file(
        self, work_file: Optional[Path], result: "ConversionResult"
    ) -> None:
        """Remove the in-archive working file if the chain never finished with it.

        ``_run_conversion`` lands its output in the ARCHIVE directory under the
        converter's own name (e.g. ``STAN202607260000a.26o``), and the rest of
        the chain — identity gate, header corrections, gfzrnx canonicalisation,
        rename-to-convention, Hatanaka — rewrites it in place until
        ``_apply_hatanaka_compression`` unlinks it and leaves only the final
        ``.NND.Z``. That is correct on the happy path.

        It is NOT correct when the chain stops early. Any refusal or crash
        between those two points leaves a raw-named, uncompressed file sitting
        in the archive's ``rinex/`` directory — ~15-20 MB beside a ~3 MB
        product. Under the 2026-07 Trimble outage *every* conversion stopped
        early (EACCES on the previous run's leftover), so ~6,000 of these
        accumulated across 54 stations and blocked the daily RINEX for weeks.

        Cleaning up here bounds the damage to zero files rather than one per
        failed attempt. A hard kill still bypasses this — the complete answer is
        to stage the whole chain outside the archive and publish only the final
        artifact — but that is a change to every converter, not just this path.
        """
        if work_file is None:
            return
        # Success: the chain consumed/renamed the working file into the product.
        if result.success:
            return
        try:
            if work_file.exists() and work_file != result.rinex_file:
                size = work_file.stat().st_size
                work_file.unlink()
                self.logger.warning(
                    "Discarded stranded intermediate %s (%.1f MB) after a failed "
                    "conversion — it would otherwise sit in the archive and, being "
                    "unwritable by a later run under a different uid, block it",
                    work_file.name,
                    size / 1048576,
                )
        except OSError as exc:
            # Never let cleanup mask the real conversion failure.
            self.logger.debug("could not remove stranded %s: %s", work_file, exc)

    def convert_batch(
        self,
        raw_files: List[Union[str, Path]],
        output_dir: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> BatchConversionResult:
        """Convert multiple raw files to RINEX format.

        Args:
            raw_files: List of raw file paths
            output_dir: Output directory (default: same as each input)
            force: Overwrite existing output files

        Returns:
            BatchConversionResult with all conversion details
        """
        batch_result = BatchConversionResult(
            station_id=self.station_id,
            total_files=len(raw_files),
            start_time=datetime.now(),
        )

        for raw_file in raw_files:
            result = self.convert_file(raw_file, output_dir, force=force)
            batch_result.results.append(result)

            if result.success:
                batch_result.successful += 1
            else:
                batch_result.failed += 1

        batch_result.end_time = datetime.now()
        epilog = validation_epilog(batch_result.results)
        if epilog:
            self.logger.warning(epilog)
        return batch_result

    def _has_supported_extension(self, file_path: Path) -> bool:
        """Check if file has a supported extension."""
        name_lower = file_path.name.lower()
        return any(
            name_lower.endswith(ext.lower()) for ext in self.supported_extensions
        )

    def _extract_date_from_filename(self, file_path: Path) -> datetime:
        """Extract observation date from raw filename.

        Expected patterns:
        - Septentrio: STATIONYYYYMMDDHHMM{session}.sbf[.gz]
        - Trimble: STATIONYYYYMMDDHHMM{session}.T02/.T00

        Args:
            file_path: Path to raw file

        Returns:
            Extracted datetime

        Raises:
            ConversionError: If date cannot be extracted
        """
        import re

        name = file_path.stem
        # Remove .sbf, .T02, etc. extensions that might be left after .stem
        for ext in [".sbf", ".t02", ".t00"]:
            if name.lower().endswith(ext):
                name = name[: -len(ext)]

        # Try pattern: STATION + YYYYMMDD + HHMM + session_letter
        # Station is 4 alphanumeric chars, date is 8 chars, time is 4 chars
        pattern = r"([A-Za-z0-9]{4})(\d{8})(\d{4})"
        match = re.match(pattern, name, re.IGNORECASE)

        if match:
            date_str = match.group(2)
            time_str = match.group(3)
            try:
                return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M")
            except ValueError:
                pass

        # Try simpler pattern: just YYYYMMDD somewhere in filename
        pattern = r"(\d{8})"
        match = re.search(pattern, name)
        if match:
            date_str = match.group(1)
            try:
                return datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                pass

        raise ConversionError(
            f"Could not extract date from filename: {file_path.name}",
            file_path,
        )

    def _apply_header_corrections(
        self,
        rinex_file: Path,
        observation_date: datetime,
    ) -> int:
        """Apply TOS metadata corrections to RINEX header.

        Uses tostools.rinex.correct_rinex_from_tos() which:
        - For recent dates (>= config_valid_from): Uses station.cfg (no TOS query)
        - For historical dates (< config_valid_from): Queries TOS database

        Args:
            rinex_file: Path to RINEX file
            observation_date: Date of observation (for historical metadata lookup)

        Returns:
            Number of corrections applied (estimated from operation success)
        """
        try:
            from tostools.rinex import correct_rinex_from_tos
        except ImportError:
            self.logger.warning("tostools not available, skipping header corrections")
            return 0

        try:
            # Get station configuration (used for recent dates)
            station_config = None
            try:
                from ..config_utils import get_station_config

                station_config = get_station_config(self.station_id)
            except Exception as e:
                self.logger.debug(f"Could not load station config: {e}")

            # Always inject the resolved OBSERVER / AGENCY. config_utils resolves
            # it from station_operator → agencies.yaml onto station_config['rinex'];
            # but correct_rinex_from_tos uses the TOS path for historical dates,
            # which does NOT emit observer/agency — so without this the converter's
            # own default ("GNSS Observer / Trimble") would survive on re-rinexed
            # historical files. Overrides that with GNSSatIMO / the agency name.
            extra = None
            if station_config:
                _rc = station_config.get("rinex", {})
                _obs = str(_rc.get("observer") or "").strip()
                _agc = str(_rc.get("agency") or "").strip()
                if _obs or _agc:
                    extra = {"OBSERVER / AGENCY": [_obs, _agc]}

            # Apply corrections using tostools
            # This function handles the config vs TOS decision internally.
            # tostools.search_station does sys.exit(1) on requests.ConnectionError
            # (→ SystemExit, a BaseException that skips `except Exception`); a
            # timeout/other transport error surfaces as a requests/OSError. Convert
            # all of these to NetworkUnavailableError so the run aborts cleanly
            # instead of grinding out header-less files (or dying on a raw
            # SystemExit). Genuine data no-ops still fall through to return 0.
            # Run-scoped cache of the date-independent TOS station payload. The
            # historical (re-rinex / batch) path fetches gps_metadata(station) — the
            # same payload for every date — so without this a 365-day re-rinex makes
            # 365 identical TOS calls and can hammer TOS over. The converter is one
            # instance per station, so an instance cache = 1 TOS call per station per
            # run (mirrors fix-headers' TOSSesionCache). Only truthy fetches are
            # cached (in _get_corrections_from_tos) so a soft miss re-fetches.
            if not hasattr(self, "_tos_metadata_cache"):
                self._tos_metadata_cache: dict = {}
            # tos_metadata_cache is a newer-tostools optimization (re-rinex reuses
            # one TOS fetch per station). Older tostools (e.g. 0.6.1) lacks the
            # kwarg and raises TypeError — which this method catches and turns into
            # a *silently default* header on every file. Pass it only when the
            # installed tostools actually accepts it (detected once per class).
            cls = type(self)
            if cls._tos_cache_kw_supported is None:
                import inspect as _inspect

                try:
                    cls._tos_cache_kw_supported = (
                        "tos_metadata_cache"
                        in _inspect.signature(correct_rinex_from_tos).parameters
                    )
                except (ValueError, TypeError):
                    cls._tos_cache_kw_supported = False

            corr_kwargs: Dict[str, Any] = dict(
                rinex_file=rinex_file,
                station_id=self.station_id,
                observation_date=observation_date,
                output_file=rinex_file,  # Overwrite in place
                station_config=station_config,
                loglevel=self.logger.level,
                extra_corrections=extra,
            )
            if cls._tos_cache_kw_supported:
                corr_kwargs["tos_metadata_cache"] = self._tos_metadata_cache
            # Network failures get _TOS_NETWORK_ATTEMPTS tries with backoff so
            # a transient resolver blip doesn't abort the whole run; anything
            # non-network raises immediately (handled by the outer except).
            last_err: Optional[BaseException] = None
            for attempt in range(1, _TOS_NETWORK_ATTEMPTS + 1):
                try:
                    result = correct_rinex_from_tos(**corr_kwargs)
                    break
                except SystemExit as e:
                    # tostools does sys.exit(1) on requests.ConnectionError
                    last_err = e
                except OSError as e:
                    # socket.gaierror (DNS) and other transport errors subclass OSError
                    last_err = e
                except Exception as e:  # noqa: BLE001 — inspect for transport errors
                    if not _is_network_error(e):
                        raise
                    last_err = e
                if attempt < _TOS_NETWORK_ATTEMPTS:
                    delay = _TOS_NETWORK_RETRY_DELAY_S * attempt
                    self.logger.warning(
                        f"TOS unreachable for {self.station_id} "
                        f"{observation_date.date()} ({last_err!r}); retrying in "
                        f"{delay}s (attempt {attempt}/{_TOS_NETWORK_ATTEMPTS})"
                    )
                    time.sleep(delay)
            else:
                raise NetworkUnavailableError(
                    f"TOS unreachable while correcting {self.station_id} "
                    f"{observation_date.date()} after {_TOS_NETWORK_ATTEMPTS} "
                    f"attempts (network/DNS down): {last_err!r}"
                ) from last_err

            if result is None:
                self.logger.warning(
                    f"Header correction failed for {self.station_id} at {observation_date.date()}"
                )
                return 0

            # Success - return approximate count (tostools doesn't return exact count)
            self.logger.info(f"Applied header corrections for {self.station_id}")
            return 1  # Indicates success

        except NetworkUnavailableError:
            raise
        except Exception as e:
            self.logger.warning(f"Header correction failed: {e}")
            return 0

    def _data_frequency(self) -> str:
        """gtimes lfrequency for this conversion ('1H' hourly, '1D' daily).

        Daily is the safe default — legacy callers without a session_type
        still produce the 24h naming convention they used before.
        """
        if self.session_type and "1hr" in self.session_type.lower():
            return "1H"
        return "1D"

    def _build_short_filename(self, observation_date: datetime, file_type: str) -> str:
        """Build a RINEX 2 short filename via gtimes' frequency-aware template.

        The ``#Rin2`` token in gtimes.datepathlist resolves to ``DDD<letter>.YY``
        where ``<letter>`` is ``0`` for daily (``lfrequency='1D'``) and the
        hour letter ``a``–``x`` for hourly (``lfrequency='1H'``). Going through
        the template (rather than calling ``rinex2_filename`` with an explicit
        session letter) keeps the converter aligned with how ``FormatResolver``
        and the rest of the receivers code build paths.
        """
        import gtimes.timefunc as gt

        template = f"{self.station_id}#Rin2{file_type}"
        names = gt.datepathlist(
            template, self._data_frequency(), datelist=[observation_date]
        )
        return names[0]

    def _rename_to_convention(
        self,
        rinex_file: Path,
        observation_date: datetime,
    ) -> Path:
        """Rename RINEX file according to naming convention.

        Args:
            rinex_file: Path to current RINEX file
            observation_date: Date of observation

        Returns:
            Path to renamed file
        """
        from gtimes import timefunc

        # Generate filename based on naming convention
        if self.naming_convention == NamingConvention.SHORT:
            # RINEX 2 short format: SSSS<DOY><session>.YY<type>
            # session letter comes from gtimes #Rin2 template via _data_frequency().
            new_name = self._build_short_filename(observation_date, "o")
        else:
            # RINEX 3 long format: SSSS00CCC_R_YYYYDDD...
            new_name = timefunc.rinex3_filename(
                self.station_id,
                observation_date,
                country_code="ISL",
                data_source="R",  # Receiver
                file_period="01D",  # Will be updated based on session
                data_frequency="15S",  # Will be updated based on session
                file_type="MO",  # Mixed Observation
                uppercase=True,  # Use uppercase station ID
            )

        # Rename file
        new_path = rinex_file.parent / new_name

        if new_path != rinex_file:
            rinex_file.rename(new_path)
            self.logger.debug(f"Renamed {rinex_file.name} -> {new_name}")

        return new_path

    def _apply_compression(self, rinex_file: Path) -> Path:
        """Apply compression to RINEX file.

        Uses apply_hatanaka and compression_format attributes:
        - apply_hatanaka: True -> run rnx2crx (.YYo -> .YYd)
        - compression_format: GZ -> .gz, Z -> .Z

        Args:
            rinex_file: Path to RINEX file

        Returns:
            Path to compressed file

        Raises:
            ConversionError: ``.Z`` requested but compress(1) is not installed.
        """
        import gzip
        import shutil
        import subprocess

        # Step 1: Apply Hatanaka compression if enabled
        if self.apply_hatanaka:
            rinex_file = self._apply_hatanaka_compression(rinex_file)

        # Step 2: Apply file compression
        if rinex_file.suffix == ".gz":
            # Already compressed
            return rinex_file

        if self.compression_format == CompressionFormat.Z:
            # ".Z" is the IGS/GAMIT LZW convention and the historical IMO
            # archive format — it MUST be real compress(1) output. Writing
            # gzip bytes under a .Z name (as this method did until 2026-07)
            # silently drifted the post-cutover archive from the legacy LZW
            # files; consumers that use genuine ncompress uncompress choke on
            # it. No silent gzip fallback here — fail loudly so a host
            # without ncompress can't reintroduce the drift.
            compressed_path = rinex_file.parent / (rinex_file.name + ".Z")
            try:
                # compress -f writes <name>.Z alongside and removes <name>.
                subprocess.run(
                    ["compress", "-f", str(rinex_file)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            except FileNotFoundError as e:
                raise ConversionError(
                    "compress(1) not installed — required for .Z output "
                    "(install ncompress, or set default_compression=gz)",
                    rinex_file,
                ) from e
            except subprocess.CalledProcessError as e:
                raise ConversionError(
                    f"compress failed for {rinex_file.name}: "
                    f"{e.stderr.decode(errors='replace').strip()[:200]}",
                    rinex_file,
                ) from e
            if not compressed_path.exists():
                raise ConversionError(
                    f"compress produced no output for {rinex_file.name}",
                    rinex_file,
                )
            return compressed_path

        compressed_path = rinex_file.parent / (rinex_file.name + ".gz")
        with open(rinex_file, "rb") as f_in:
            with gzip.open(compressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        rinex_file.unlink()
        return compressed_path

    def _apply_hatanaka_compression(self, rinex_file: Path) -> Path:
        """Apply Hatanaka compression to RINEX observation file.

        Uses rnx2crx to convert .YYo -> .YYd (compact RINEX format).
        Only applies to observation files (.o extension).

        Args:
            rinex_file: Path to RINEX observation file

        Returns:
            Path to Hatanaka compressed file, or original if not applicable
        """
        # Check if this is an observation file (.YYo format)
        suffix = rinex_file.suffix.lower()
        if not (len(suffix) == 4 and suffix[1:3].isdigit() and suffix[3] == "o"):
            self.logger.debug(f"Skipping Hatanaka: not an observation file ({suffix})")
            return rinex_file

        try:
            rnx2crx = self.get_tool_path("rnx2crx")
        except ConversionError:
            if self.strict_hatanaka:
                raise ConversionError(
                    f"rnx2crx not found — Hatanaka required for {rinex_file.name} "
                    "(strict mode: refusing to stage uncompacted RINEX)",
                    rinex_file,
                )
            self.logger.warning("rnx2crx not found, skipping Hatanaka compression")
            return rinex_file

        # rnx2crx writes lowercase .YYd (its own convention). IMO's long-term
        # archive on rawdata — and the okada getimorinex.py / GAMIT pipeline that
        # reads it — expects UPPERCASE .YYD, so rename rnx2crx's output to match.
        # Lowercase .d.Z broke the okada fetch at the old-rek -> rek_new cutover
        # (DOY 172, 2026): getimorinex.py requests .D.Z, found no file, and left a
        # 0-byte download. Keep the whole archive on the historical .D.Z convention.
        rnx2crx_out = rinex_file.with_suffix(suffix[:-1] + "d")  # what rnx2crx writes
        # uppercase .YYD = IMO archive convention
        hatanaka_file = rinex_file.with_suffix(suffix[:-1] + "D")

        try:
            # Run rnx2crx: reads input file, creates lowercase .YYd alongside it.
            cmd = [str(rnx2crx), str(rinex_file)]
            self._run_subprocess(cmd, timeout=60)

            # rnx2crx creates output file alongside input with changed extension
            if rnx2crx_out.exists() and rnx2crx_out.stat().st_size > 0:
                rnx2crx_out.rename(hatanaka_file)  # .YYd -> .YYD (uppercase)
                rinex_file.unlink()  # Remove original .YYo file
                self.logger.debug(
                    f"Hatanaka compressed: {rinex_file.name} -> {hatanaka_file.name}"
                )
                return hatanaka_file
            else:
                if self.strict_hatanaka:
                    raise ConversionError(
                        f"rnx2crx produced no output for {rinex_file.name} "
                        "(strict mode: refusing to stage uncompacted RINEX)",
                        rinex_file,
                    )
                self.logger.warning(
                    f"rnx2crx did not create expected output: {rnx2crx_out}"
                )
                return rinex_file

        except ConversionError:
            # strict-mode refusal from above — clean the intermediates so
            # nothing non-conforming lingers in the staging tree, then fail
            # the file (convert_file turns this into a per-file ❌; the date
            # stays unstaged and the existing archive .D.Z remains
            # authoritative).
            for leftover in (rnx2crx_out, rinex_file):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        except Exception as e:
            if self.strict_hatanaka:
                for leftover in (rnx2crx_out, rinex_file):
                    try:
                        leftover.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise ConversionError(
                    f"Hatanaka compression failed for {rinex_file.name}: {e} "
                    "— corrupt raw data? (strict mode: refusing to stage "
                    "uncompacted RINEX; existing archive file stays authoritative)",
                    rinex_file,
                ) from e
            self.logger.warning(f"Hatanaka compression failed: {e}")
            return rinex_file

    def _canonicalize_rinex(self, rinex_file: Path, observation_date: datetime) -> Path:
        """Run gfzrnx to canonicalize RINEX 3 header order (piece 3).

        Mirrors the EPOS dissemination path's ``gfzrnx -vo {version}`` pass so
        operational-archive files and EPOS files share identical header ordering.
        Runs in place: the canonicalized output replaces the input file.

        On failure or if gfzrnx is unavailable, the original file is returned
        unchanged — canonicalization is best-effort.
        """
        rinex_file = Path(rinex_file)

        # Only RINEX 3 benefits from canonicalization (R2 has a different
        # standard and gfzrnx up-conversion would change the marker convention).
        if self.rinex_version not in (RinexVersion.RINEX_3, RinexVersion.RINEX_4):
            return rinex_file

        try:
            gfzrnx = self.get_tool_path("gfzrnx")
        except Exception:  # noqa: BLE001
            self.logger.debug("gfzrnx not available — skipping canonicalization")
            return rinex_file

        out = rinex_file.with_name(rinex_file.stem + "_gfzrnx" + rinex_file.suffix)
        version = self.rinex_version.value
        try:
            cmd = [
                str(gfzrnx),
                "-finp",
                str(rinex_file),
                "-fout",
                str(out),
                "-vo",
                str(version),
            ]
            self._run_subprocess(cmd, timeout=120)
        except Exception:  # noqa: BLE001
            self.logger.debug("gfzrnx run failed — keeping original header order")
            if out.exists():
                out.unlink()
            return rinex_file

        if not out.exists() or out.stat().st_size == 0:
            self.logger.debug("gfzrnx produced no output — keeping original")
            return rinex_file

        out.rename(rinex_file)
        self.logger.debug("gfzrnx canonicalized header for %s", rinex_file.name)
        return rinex_file

    def _apply_naming_headers(
        self, rinex_file: Path, observation_date: datetime
    ) -> Path:
        """Set naming-gated header records (piece 4).

        Long names → 9-char MARKER NAME (RHOF00ISL) + generic OBSERVER/AGENCY.
        Short names → keep the 4-char marker the converter emitted (safe for
        GAMIT until confirmed).

        Uses :func:`_set_header_records` (same logic the EPOS dissemination path
        uses), imported lazily to avoid a startup-time circular dependency on
        the dissemination subpackage.
        """
        if self.naming_convention != NamingConvention.LONG:
            return rinex_file

        try:
            from ..dissemination.convert import _set_header_records, epos_marker_name

            nine_char = epos_marker_name(
                self.station_id, self.rinex_version.value, "ISL"
            )
            _set_header_records(rinex_file, {"MARKER NAME": nine_char})
            self.logger.debug(
                "set 9-char MARKER NAME for %s: %s",
                rinex_file.name,
                nine_char,
            )
        except Exception:  # noqa: BLE001
            self.logger.debug(
                "could not apply 9-char marker for %s (dissemination not available)",
                rinex_file.name,
            )
        return rinex_file

    def _run_subprocess(
        self,
        cmd: List[str],
        timeout: int = 300,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        """Run external command with error handling.

        Args:
            cmd: Command and arguments
            timeout: Timeout in seconds
            cwd: Working directory

        Returns:
            Completed process result

        Raises:
            ConversionError: If command fails
        """
        self.logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            if result.returncode != 0:
                raise ConversionError(
                    f"{cmd[0]} failed with exit code {result.returncode}",
                    details=result.stderr or result.stdout,
                )

            return result

        except subprocess.TimeoutExpired:
            raise ConversionError(f"{cmd[0]} timed out after {timeout} seconds")

        except FileNotFoundError:
            raise ConversionError(
                f"{cmd[0]} not found. Please install or configure path.",
                details=f"Command: {' '.join(cmd)}",
            )

    def get_tool_path(self, tool_name: str) -> Path:
        """Get path to external conversion tool.

        Args:
            tool_name: Name of the tool (e.g., 'sbf2rin')

        Returns:
            Path to tool executable

        Raises:
            ConversionError: If tool not found
        """
        if tool_name in self._tool_paths:
            return self._tool_paths[tool_name]

        # Try configuration first
        try:
            config_key = f"{tool_name}_path"
            # Try [rinex_tools] section, then [rinex] section
            for section in ["rinex_tools", "rinex"]:
                try:
                    tool_path = self.config.config.get(section, config_key)
                    if tool_path and Path(tool_path).exists():
                        self._tool_paths[tool_name] = Path(tool_path)
                        return self._tool_paths[tool_name]
                except Exception:
                    pass
        except Exception:
            pass

        # Try system PATH (also uppercase variant for tools like RNX2CRX)
        import shutil

        system_path = shutil.which(tool_name) or shutil.which(tool_name.upper())
        if system_path:
            self._tool_paths[tool_name] = Path(system_path)
            return self._tool_paths[tool_name]

        hint = _TOOL_HINTS.get(
            tool_name,
            "install it or set its path in receivers.cfg [rinex_tools]",
        )
        raise ConversionError(
            f"Conversion tool '{tool_name}' not found — {self.converter_name} "
            f"cannot run without it. Where it lives: {hint}. "
            f"(Override: receivers.cfg [rinex_tools] {tool_name}_path = /path)",
        )

    def validate_tools(self) -> Dict[str, bool]:
        """Validate that required conversion tools are available.

        Returns:
            Dictionary mapping tool names to availability status
        """
        tools = self._get_required_tools()
        results = {}

        for tool in tools:
            try:
                self.get_tool_path(tool)
                results[tool] = True
            except ConversionError:
                results[tool] = False

        return results

    @abstractmethod
    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools for this converter."""
        pass
