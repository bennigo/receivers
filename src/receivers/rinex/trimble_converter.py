"""
Trimble T02/T00 to RINEX converter.

This module implements RINEX conversion for Trimble raw files (.T02, .T00)
using runpkr00 for extraction, teqc for RINEX 2 conversion, and GFZRNX for
RINEX 3 format conversion.

Trimble formats:
- T02: NetR9 raw format (newer)
- T00: NetRS raw format (older)

Workflow:
    1. runpkr00 extracts T02/T00 -> .dat (binary intermediate)
    2. teqc converts .dat -> RINEX 2 (text format)
    3. GFZRNX converts RINEX 2 -> RINEX 3 (if needed)
    4. MetadataProvider supplies TOS equipment metadata
    5. Header corrections applied using tostools
    6. File renamed to short/long naming convention

Note: runpkr00 produces binary .dat files that need teqc for RINEX conversion.
Since teqc cannot produce RINEX 3, we use GFZRNX for the final format upgrade.
"""

import gzip
import logging
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .converter_base import (
    ConversionError,
    NamingConvention,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)


class TrimbleConverter(RawToRinexConverter):
    """Converter for Trimble T02/T00 files to RINEX format.

    Uses runpkr00 for initial extraction and GFZRNX for RINEX 3 conversion.

    Supports:
    - NetR9 .T02 files
    - NetRS .T00 files
    - RINEX versions 2.x and 3.x output

    Example:
        >>> converter = TrimbleConverter("MANA", rinex_version=RinexVersion.RINEX_3)
        >>> result = converter.convert_file("MANA202601010000a.T02")
        >>> print(result.rinex_file)
        MANA00ISL_R_20260010000_01D_15S_MO.rnx.gz
    """

    # Content gate (receivers.archive.raw_format): only this format may
    # reach the decoder — the archive's extensions lie (.atc covers Ashtech
    # U/R AND Septentrio SBF) and the wrong tool segfaults or emits nothing.
    # 'unknown' always passes (formats without printable magic).
    accepted_raw_formats = frozenset({"trimble"})

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: Optional[OutputFormat] = None,
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        apply_hatanaka: Optional[bool] = None,
        compression_format=None,
        keep_intermediate: bool = False,
        loglevel: int = logging.INFO,
        session_type: Optional[str] = None,
    ):
        """Initialize Trimble converter.

        Args:
            station_id: Station identifier (e.g., 'MANA')
            rinex_version: Target RINEX version (2 or 3)
            output_format: Legacy parameter (use apply_hatanaka/compression_format instead)
            naming_convention: Filename convention (SHORT or LONG).
                              If None, defaults based on rinex_version.
            apply_header_corrections: Whether to apply TOS metadata corrections
            apply_hatanaka: Apply Hatanaka compression (None = read from config)
            compression_format: File compression format (None = read from config)
            keep_intermediate: Keep intermediate .tgd files
            loglevel: Logging level
        """
        super().__init__(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            naming_convention=naming_convention,
            apply_header_corrections=apply_header_corrections,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            loglevel=loglevel,
            session_type=session_type,
        )
        self.keep_intermediate = keep_intermediate
        self._temp_files: List[Path] = []

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return [
            ".t02",
            ".T02",
            ".t00",
            ".T00",
            ".t02.gz",
            ".T02.gz",
            ".t00.gz",
            ".T00.gz",
        ]

    @property
    def converter_name(self) -> str:
        """Return converter tool name."""
        return "runpkr00"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools."""
        # runpkr00 extracts binary, teqc converts to RINEX 2
        tools = ["runpkr00", "teqc"]
        if self.rinex_version.value >= 3:
            tools.append("gfzrnx")
        return tools

    def _decompress_if_needed(self, raw_file: Path) -> Path:
        """Decompress .gz file if needed.

        Args:
            raw_file: Input file (possibly compressed)

        Returns:
            Path to uncompressed file
        """
        if raw_file.suffix.lower() == ".gz":
            # Create temp file for decompressed data
            temp_dir = Path(tempfile.mkdtemp(prefix="trimble_"))
            decompressed = temp_dir / raw_file.stem

            self.logger.debug(f"Decompressing {raw_file} to {decompressed}")

            with gzip.open(raw_file, "rb") as f_in:
                with open(decompressed, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            self._temp_files.append(decompressed)
            self._temp_files.append(temp_dir)
            return decompressed

        return raw_file

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run T02/T00 to RINEX conversion.

        Workflow:
        1. Decompress if .gz file
        2. runpkr00 extracts T02/T00 -> .dat (binary)
        3. teqc converts .dat -> RINEX 2 (text)
        4. If RINEX 3 requested, GFZRNX converts RINEX 2 -> RINEX 3

        Args:
            raw_file: Path to T02/T00 file
            output_dir: Output directory for RINEX file
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file

        Raises:
            ConversionError: If conversion fails
        """
        try:
            # Step 0: Decompress if needed
            working_file = self._decompress_if_needed(raw_file)

            # Steps 1-3 run in a PRIVATE STAGING DIRECTORY, never in output_dir.
            #
            # These steps emit intermediates (.tgd from runpkr00, RINEX-2 .NNo
            # from teqc) alongside their output. Writing those into output_dir
            # meant every intermediate landed in the *archive* rinex/ directory,
            # and any that survived a failure stayed there forever. Worse, the
            # leftovers made the failure permanent: a stale intermediate owned by
            # a different uid (the archive is shared over NFS, and uid 1000 on
            # any client maps to a non-gpsops owner) cannot be overwritten by the
            # scheduler, so every subsequent run for that station died with
            # EACCES while raw downloads kept succeeding — silent loss of daily
            # RINEX. See the 2026-07 Trimble-fleet incident.
            #
            # TemporaryDirectory also guarantees cleanup on the exception path,
            # which the old explicit-cleanup approach only did best-effort.
            with tempfile.TemporaryDirectory(prefix="trimble_stage_") as staging:
                stage_dir = Path(staging)

                # Step 1: Extract with runpkr00 (produces binary .dat)
                dat_file = self._run_runpkr00(working_file, stage_dir)

                # Step 2: Convert binary .dat to RINEX 2 with teqc
                rinex2_file = self._run_teqc(dat_file, stage_dir, observation_date)

                # Step 3: Convert to final RINEX version
                if self.rinex_version.value >= 3:
                    # Use GFZRNX for RINEX 3 conversion
                    staged_result = self._run_gfzrnx(
                        rinex2_file, stage_dir, observation_date
                    )
                else:
                    # RINEX 2: already have the file
                    staged_result = rinex2_file

                # Publish ONLY the final artifact into the archive.
                output_dir.mkdir(parents=True, exist_ok=True)
                rinex_file = output_dir / staged_result.name
                shutil.move(str(staged_result), str(rinex_file))

            # Clean up any other intermediates (e.g. the decompressed input)
            if not self.keep_intermediate:
                self._cleanup_temp_files()

            return rinex_file

        except Exception as e:
            # Clean up on error too
            self._cleanup_temp_files()
            if isinstance(e, ConversionError):
                raise
            raise ConversionError(str(e), raw_file)

    def _run_runpkr00(self, raw_file: Path, output_dir: Path) -> Path:
        """Run runpkr00 to extract T02/T00 to TGD format.

        Args:
            raw_file: Input T02/T00 file
            output_dir: Output directory

        Returns:
            Path to extracted .tgd file

        Raises:
            ConversionError: If extraction fails
        """
        runpkr00 = self.get_tool_path("runpkr00")

        # Determine output filename (runpkr00 generates .tgd)
        tgd_file = output_dir / (raw_file.stem + ".tgd")

        # Build command
        # runpkr00 -g -d -s <input> <output_dir>
        # -g: Generate GPS observation file
        # -d: Generate RINEX 2 format
        # -s: Silent mode
        cmd = [
            str(runpkr00),
            "-g",  # GPS obs file
            "-d",  # RINEX 2 format
            str(raw_file),
            "-o",
            str(output_dir),
        ]

        self.logger.info(f"Running runpkr00 for {raw_file.name}")
        try:
            self._run_subprocess(cmd, timeout=300, cwd=output_dir)
        except ConversionError as e:
            # runpkr00 sometimes segfaults on exit (code -11/139) but still
            # produces valid output. Check for output before raising.
            if "exit code -11" in str(e) or "exit code 139" in str(e):
                self.logger.debug(
                    "runpkr00 crashed on exit but may have produced output"
                )
            else:
                raise

        # Find output file (runpkr00 naming can vary)
        # runpkr00 produces:
        # - .tgd for RT27 format (with -g flag)
        # - .dat for older formats
        if tgd_file.exists():
            self._temp_files.append(tgd_file)
            return tgd_file

        # Check for .dat file (common for T00 files)
        dat_file = output_dir / (raw_file.stem + ".dat")
        if dat_file.exists():
            self._temp_files.append(dat_file)
            return dat_file

        # Try to find any .tgd or .dat file
        for pattern in ["*.tgd", "*.dat"]:
            matches = list(output_dir.glob(pattern))
            if matches:
                out_file = matches[0]
                self._temp_files.append(out_file)
                return out_file

        # Also check for .obs files (alternative output)
        obs_files = list(output_dir.glob(f"{raw_file.stem}*.obs"))
        if obs_files:
            return obs_files[0]

        raise ConversionError(
            "runpkr00 did not produce expected output (.tgd or .dat)",
            raw_file,
        )

    def _teqc_extra_args(self, observation_date: datetime) -> List[str]:
        """Extra teqc flags for this container. Base: none.

        Subclass hook so a decoder that needs disambiguation (R00 and its GPS
        week-number rollover) can add flags without changing the .T02/.T00
        command that is already correct.
        """
        return []

    def _run_teqc(
        self,
        dat_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run teqc to convert binary .dat to RINEX 2 format.

        Args:
            dat_file: Input .dat file from runpkr00
            output_dir: Output directory
            observation_date: Date of observation

        Returns:
            Path to RINEX 2 observation file

        Raises:
            ConversionError: If conversion fails
        """
        teqc = self.get_tool_path("teqc")

        # Build output filename (RINEX 2 naming: SSSS0DDF.YYo)
        day_of_year = observation_date.timetuple().tm_yday
        year_2digit = observation_date.year % 100

        rinex_name = f"{self.station_id}{day_of_year:03d}0.{year_2digit:02d}o"
        rinex_file = output_dir / rinex_name

        # Build command
        # teqc +obs <output> <input>
        # teqc reads the .dat file and produces RINEX observation file
        cmd = [
            str(teqc),
            *self._teqc_extra_args(observation_date),
            "+obs",
            str(rinex_file),
            str(dat_file),
        ]

        self.logger.info(f"Running teqc to convert {dat_file.name} to RINEX 2")
        self._run_subprocess(cmd, timeout=300, cwd=output_dir)

        if rinex_file.exists():
            self._temp_files.append(rinex_file)
            return rinex_file

        # Check for alternative output (teqc may use different naming)
        patterns = [
            f"{self.station_id}*.{year_2digit:02d}o",
            f"{self.station_id.lower()}*.{year_2digit:02d}o",
            f"*.{year_2digit:02d}o",
        ]

        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            if matches:
                out_file = max(matches, key=lambda p: p.stat().st_mtime)
                self._temp_files.append(out_file)
                return out_file

        raise ConversionError(
            "teqc did not produce expected RINEX 2 output",
            dat_file,
        )

    def _run_gfzrnx(
        self,
        tgd_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run GFZRNX to convert to RINEX 3 format.

        Args:
            tgd_file: Input TGD/RINEX 2 file
            output_dir: Output directory
            observation_date: Date of observation

        Returns:
            Path to RINEX 3 file

        Raises:
            ConversionError: If conversion fails
        """
        gfzrnx = self.get_tool_path("gfzrnx")

        # Build output filename
        day_of_year = observation_date.timetuple().tm_yday
        year = observation_date.year

        # GFZRNX output naming
        output_name = f"{self.station_id.lower()}00isl_R_{year:04d}{day_of_year:03d}0000_01D_15S_MO.rnx"
        rinex_file = output_dir / output_name

        # Build command
        # gfzrnx -finp <input> -fout <output> -vo 3
        cmd = [
            str(gfzrnx),
            "-finp",
            str(tgd_file),
            "-fout",
            str(rinex_file),
            "-vo",
            "3",  # Output RINEX version 3
        ]

        self.logger.info("Running GFZRNX for RINEX 3 conversion")
        self._run_subprocess(cmd, timeout=300)

        if rinex_file.exists():
            return rinex_file

        # Check for alternative output patterns
        patterns = [
            f"{self.station_id}*.rnx",
            f"{self.station_id.lower()}*.rnx",
            "*.rnx",
        ]

        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            if matches:
                return max(matches, key=lambda p: p.stat().st_mtime)

        raise ConversionError(
            "GFZRNX did not produce expected RINEX 3 output",
            tgd_file,
        )

    def _rename_tgd_to_rinex2(
        self,
        tgd_file: Path,
        observation_date: datetime,
    ) -> Path:
        """Rename TGD file to proper RINEX 2 naming.

        Args:
            tgd_file: Input TGD file
            observation_date: Date of observation

        Returns:
            Path to renamed file
        """
        day_of_year = observation_date.timetuple().tm_yday
        year_2digit = observation_date.year % 100

        # RINEX 2 naming: SSSS0DDF.YYo
        rinex_name = f"{self.station_id}{day_of_year:03d}0.{year_2digit:02d}o"
        rinex_file = tgd_file.parent / rinex_name

        if tgd_file != rinex_file:
            shutil.copy(tgd_file, rinex_file)

        return rinex_file

    def _cleanup_temp_files(self) -> None:
        """Clean up intermediate files and directories."""
        for temp_path in self._temp_files:
            try:
                if temp_path.exists():
                    if temp_path.is_dir():
                        shutil.rmtree(temp_path)
                        self.logger.debug(f"Cleaned up directory {temp_path}")
                    else:
                        temp_path.unlink()
                        self.logger.debug(f"Cleaned up {temp_path.name}")
            except Exception as e:
                self.logger.warning(f"Could not clean up {temp_path}: {e}")

        self._temp_files.clear()


class NetR9Converter(TrimbleConverter):
    """Specialized converter for NetR9 T02 files.

    Inherits from TrimbleConverter with NetR9-specific defaults.
    """

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions for NetR9."""
        return [".t02", ".T02", ".t02.gz", ".T02.gz"]


class NetRSConverter(TrimbleConverter):
    """Specialized converter for NetRS T00 files.

    Inherits from TrimbleConverter with NetRS-specific defaults.
    """

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions for NetRS."""
        return [".t00", ".T00", ".t00.gz", ".T00.gz"]


#: GPS time origin — week 0 day 0. Used to derive teqc's ``-week``.
_GPS_EPOCH = date(1980, 1, 6)


class R00Converter(TrimbleConverter):
    """Trimble R00 raw (4000SSi / 4000Si era) → RINEX, via runpkr00 + teqc.

    The pipeline is IDENTICAL to .T02/.T00 — runpkr00 unpacks the container to
    a binary ``.dat``, teqc decodes that to RINEX 2 — so this subclass only
    changes the accepted extension and adds one flag.

    **That flag matters.** R00 predates the GPS week-number rollover, and the
    raw stream carries a 10-bit week. Left to itself teqc guesses, and says so::

        ? Error ? translation ... may have started with GPS week 2432
                  rather than 1586  (try using '-week 1586' option)

    It guessed right on the file this was built against, but a wrong guess is
    silent and lands the data ~19.6 years away. The observation date is known
    from the archive path, so the week is derived, not guessed.

    Output is native RINEX 2.11 (teqc cannot create real RINEX 3 from this
    raw); the inherited gfzrnx step performs the R2→R3 upgrade when asked, the
    same as for .T02.

    Verified against VMEY201006012359a.r00 (2010-06-02): runpkr00 8,495
    records, teqc 5,760 epochs at 15 s = a complete 24 h day, header receiver
    ``26093 TRIMBLE 4000SSI`` matching TOS's join for that era.
    """

    accepted_raw_formats = frozenset({"trimble_r00"})

    @property
    def supported_extensions(self) -> List[str]:
        return [".r00", ".R00", ".r00.gz", ".R00.gz"]

    def _extract_date_from_filename(self, file_path: Path) -> datetime:
        """Observation date for an R00, honouring the SESSION-START naming.

        The archive stamps some R00 files with the moment the session opened,
        one minute before midnight, so ``…YYYYMMDD2359a.r00`` holds the data of
        ``YYYYMMDD + 1``. Measured over 2008/2010/2012 fleet-wide: 2,150 files
        named ``0000`` (same day) against 1,188 named ``2359`` (next day), plus
        ~270 at assorted times. VMEY is a ``2359`` station — 951 of its 977.

        Confirmed both ways by decoding:

        * ``HVER201004010000a.r00`` -> first obs 2010-04-01  (same day)
        * ``VMEY201006012359a.r00`` -> first obs 2010-06-02  (next day)

        Only the unambiguous late-evening case is shifted. A file stamped at
        some other hour is left alone: the base class's identity gate compares
        the decoded first-obs date against the claim and refuses a mismatch, so
        an odd one is caught rather than silently misfiled.
        """
        stamp = super()._extract_date_from_filename(file_path)
        if stamp.hour == 23 and stamp.minute >= 55:
            shifted = (stamp + timedelta(days=1)).replace(hour=0, minute=0)
            self.logger.debug(
                "R00 %s: session-start naming — observation date is %s, not %s",
                file_path.name,
                shifted.date().isoformat(),
                stamp.date().isoformat(),
            )
            return shifted
        return stamp

    def _teqc_extra_args(self, observation_date: datetime) -> List[str]:
        """``-week N`` for ``observation_date`` — never let teqc guess."""
        d = observation_date.date() if isinstance(observation_date, datetime) else observation_date
        week = (d - _GPS_EPOCH).days // 7
        self.logger.debug(
            "R00 %s: pinning teqc -week %d (rollover is silent if guessed wrong)",
            d.isoformat(),
            week,
        )
        return ["-week", str(week)]
