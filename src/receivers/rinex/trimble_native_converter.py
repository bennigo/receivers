"""
Trimble native RINEX 3 converter using Docker + Wine.

This module provides native RINEX 3 conversion for Trimble T00/T02 files
using the official Trimble Convert to RINEX utility running in a Docker
container with Wine.

Requirements:
    - Docker installed and running
    - trm2rinex:cli-light Docker image built from:
      https://github.com/Matioupi/trm2rinex-docker

Advantages over teqc+gfzrnx workflow:
    - Native RINEX 3.x output (not reformatted)
    - Proper RINEX 3 observation codes
    - Official Trimble conversion

Disadvantages:
    - Requires Docker
    - ~3x slower than native Windows
    - Docker image must be built manually (IP restrictions)
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .converter_base import (
    ConversionError,
    NamingConvention,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)


class TrimbleNativeConverter(RawToRinexConverter):
    """Native RINEX 3 converter for Trimble files using Docker.

    Uses the Trimble Convert to RINEX utility via Docker+Wine wrapper.

    Supports:
    - NetR9 .T02 files
    - NetRS .T00 files
    - Native RINEX 3.02, 3.03, 3.04, 3.05 output

    Example:
        >>> converter = TrimbleNativeConverter("MANA", rinex_version=RinexVersion.RINEX_3)
        >>> result = converter.convert_file("MANA202601010000a.T02")
        >>> print(result.rinex_file)
        MANA0010.26o.gz
    """

    # Content gate (receivers.archive.raw_format): only this format may
    # reach the decoder — the archive's extensions lie (.atc covers Ashtech
    # U/R AND Septentrio SBF) and the wrong tool segfaults or emits nothing.
    # 'unknown' always passes (formats without printable magic).
    accepted_raw_formats = frozenset({"trimble"})

    # Docker image name
    DOCKER_IMAGE = "trm2rinex:cli-light"

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: Optional[OutputFormat] = None,
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        apply_hatanaka: Optional[bool] = None,
        compression_format=None,
        docker_image: Optional[str] = None,
        loglevel: int = logging.INFO,
        session_type: Optional[str] = None,
    ):
        """Initialize Trimble native converter.

        Args:
            station_id: Station identifier (e.g., 'MANA')
            rinex_version: Target RINEX version (3.02-3.05)
            output_format: Legacy parameter (use apply_hatanaka/compression_format instead)
            naming_convention: Filename convention (SHORT or LONG)
            apply_header_corrections: Whether to apply TOS metadata corrections
            apply_hatanaka: Apply Hatanaka compression (None = read from config)
            compression_format: File compression format (None = read from config)
            docker_image: Override Docker image name (default: trm2rinex:cli-light)
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
        self.docker_image = docker_image or self.DOCKER_IMAGE
        self._temp_dirs: List[Path] = []

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
        return "trimble-docker"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools."""
        return ["docker"]

    @classmethod
    def is_available(cls) -> bool:
        """Check if Docker and the trm2rinex image are available.

        Returns:
            True if Docker is running and image exists
        """
        try:
            # Check Docker is running
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False

            # Check image exists
            result = subprocess.run(
                ["docker", "image", "inspect", cls.DOCKER_IMAGE],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    #: Grace between SIGTERM and SIGKILL when a conversion overruns. The wedged
    #: processes observed on 2026-08-11 ignored SIGTERM entirely and only died to
    #: SIGKILL, so the KILL is not a formality — it is the one that works.
    _KILL_GRACE_S = 5

    def _run_group_killable(
        self, cmd: List[str], timeout: int
    ) -> subprocess.CompletedProcess:
        """Run ``cmd`` in its own process group and kill the WHOLE GROUP on timeout.

        ``subprocess.run(timeout=...)`` kills only its direct child. Everywhere
        else in this package that is the converter itself, so the timeout works.
        Not here: this path launches wine, and the real ``convertToRinex.exe``
        runs as a grandchild **in a different process group**. Killing the direct
        child therefore reaps the launcher and leaves the worker running.

        Measured on rek-d01 2026-08-11 — one live conversion, two processes::

            pid 864860  ppid=860296  pgid=860296  cpu=0.1%   <- wine launcher
            pid 864904  ppid=864881  pgid=864904  cpu=77.4%  <- the real worker

        With the old code a 600 s timeout killed 864860 and orphaned 864904 at
        100% CPU, forever. The raised ConversionError then triggered a retry,
        which leaked another orphan: four had accumulated on one truncated input
        (``KISA202607170000a.T02``, 167 KB where a normal day is ~3.35 MB), two of
        them 2h24m old, holding ~400% CPU and dragging loadavg past 12. That in
        turn made ``auto_workers`` throttle unrelated jobs to a single worker.

        ``start_new_session=True`` puts the launcher in a NEW process group that
        wine's children inherit, so one ``killpg`` reaches the whole tree.

        Raises:
            subprocess.TimeoutExpired: after the group has been killed, so the
                caller's existing handler still sees a timeout — but nothing is
                left running behind it.
        """
        import signal

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.logger.error(
                "conversion exceeded %ss — killing process group of pid %s",
                timeout,
                proc.pid,
            )
            self._kill_group(proc, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=self._KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                self._kill_group(proc, signal.SIGKILL)
                try:
                    out, err = proc.communicate(timeout=self._KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    # A survivor still holds the inherited stdout/stderr pipes,
                    # so communicate() would block on THEM, not on the process.
                    # Found the hard way: with the group-kill removed, this
                    # method hung for the orphan's full lifetime instead of
                    # returning. Never block a worker thread on a process we
                    # have already given up on — surface it and move on.
                    self.logger.error(
                        "pid %s did not release its pipes after SIGKILL — "
                        "something in its tree survived; abandoning the read",
                        proc.pid,
                    )
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def _kill_group(self, proc: subprocess.Popen, sig: int) -> None:
        """Signal the child's whole process group, falling back to the child."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            # Already gone, or we lost the right to signal it — still make sure
            # the direct child does not survive us.
            self.logger.debug("killpg(%s) failed (%s); killing child only", sig, exc)
            try:
                proc.kill()
            except OSError:
                pass

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run native Trimble to RINEX conversion via Docker.

        Args:
            raw_file: Path to T02/T00 file
            output_dir: Output directory for RINEX file
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file

        Raises:
            ConversionError: If conversion fails
        """
        import gzip

        try:
            # Create temp directory for Docker volume mount.
            # Must NOT be under /tmp when running under systemd with PrivateTmp=true —
            # the Docker daemon runs in the host mount namespace and can't see into the
            # service's private /tmp.  Using XDG cache dir is always in the real fs.
            docker_tmp_base = Path.home() / ".cache" / "gps_receivers" / "tmp"
            docker_tmp_base.mkdir(parents=True, exist_ok=True)
            temp_dir = Path(
                tempfile.mkdtemp(prefix="trimble_native_", dir=docker_tmp_base)
            )
            temp_dir.chmod(0o755)
            self._temp_dirs.append(temp_dir)

            # Decompress if needed and copy to temp dir
            if raw_file.suffix.lower() == ".gz":
                working_file = temp_dir / raw_file.stem
                with gzip.open(raw_file, "rb") as f_in:
                    with open(working_file, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
            else:
                working_file = temp_dir / raw_file.name
                shutil.copy(raw_file, working_file)

            # Output subdirectory. The container runs as OUR uid:gid (see the
            # --user flag below), so 0755 is enough — it no longer has to be
            # world-writable for a foreign container user to write into it.
            docker_out = temp_dir / "out"
            docker_out.mkdir()
            docker_out.chmod(0o755)

            # Determine RINEX version string
            version_map = {
                RinexVersion.RINEX_2: "2.11",
                RinexVersion.RINEX_3: "3.04",
                RinexVersion.RINEX_4: "3.05",  # Trimble doesn't support RINEX 4 yet
            }
            rinex_ver = version_map.get(self.rinex_version, "3.04")

            # Build Docker command
            # The trm2rinex image uses Wine to run convertToRinex.exe
            # We need to:
            # 1. Mount the temp directory to /data in the container
            # 2. Run wine with the full Windows path to convertToRinex.exe
            # 3. Use Z: drive mapping for Linux paths (Z:\data maps to /data)

            # Path to convertToRinex inside the container
            convert_exe = (
                "C:\\Program Files\\Trimble\\convertToRINEX\\convertToRinex.exe"
            )
            wine_path = "/opt/wine/bin/wine"

            # Run the container as the INVOKING user (gpsops in production), not
            # the image's baked-in `user` (uid 1000 / gid 100).
            #
            # The image default made every output file land in the archive owned
            # by uid 1000 — which on rek-d01 is an unrelated account (`firstuser`),
            # not gpsops. A leftover intermediate then became UNOVERWRITABLE by the
            # scheduler, so the next run died with `[Errno 13] Permission denied`
            # and that station's daily RINEX stopped for good while raw downloads
            # kept succeeding. Owning our own output makes such a leftover merely
            # stale instead of fatal — the failure becomes self-healing.
            #
            # Wine refuses a WINEPREFIX it does not own ("is not owned by you"),
            # and the prefix baked into the image (/home/user/.wine, 120 MB) holds
            # the installed convertToRinex. So copy it to a HOME we own inside the
            # container's own overlay first. Measured at ~0.16 s — it is a
            # copy-on-write overlay copy, not 120 MB of real I/O — and it keeps
            # each conversion isolated, so parallel conversions cannot race on a
            # shared prefix.
            prefix_setup = (
                'set -e; cp -a /home/user/.wine "$HOME/.wine"; '
                'export WINEPREFIX="$HOME/.wine"; exec "$@"'
            )

            cmd = [
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-e",
                "HOME=/tmp",  # container overlay: writable by any uid, discarded on --rm
                "-v",
                f"{temp_dir}:/data",
                "--entrypoint",
                "",
                self.docker_image,
                "bash",
                "-c",
                prefix_setup,
                "_",  # becomes $0; the real argv follows as "$@"
                wine_path,
                convert_exe,
                f"Z:\\data\\{working_file.name}",
                "-p",
                "Z:\\data\\out",
                "-v",
                rinex_ver,
                "-d",  # Include Doppler
                "-co",  # Include clock offsets
                "-s",  # Include SNR
            ]

            self.logger.info(f"Running Trimble native conversion for {raw_file.name}")
            self.logger.debug(f"Docker command: {' '.join(cmd)}")

            # NOT subprocess.run: its timeout kills only the DIRECT child, and
            # on this path that child is the wine launcher, not the converter.
            # See _run_group_killable.
            result = self._run_group_killable(cmd, timeout=600)

            if result.returncode != 0:
                raise ConversionError(
                    f"Docker conversion failed: {result.stderr}",
                    raw_file,
                )

            # Find output file
            rinex_file = self._find_output_file(docker_out, observation_date)

            if not rinex_file:
                raise ConversionError(
                    "Trimble converter produced no output file",
                    raw_file,
                )

            # Move to final output directory
            final_file = output_dir / rinex_file.name
            shutil.move(rinex_file, final_file)

            # Normalize epoch lines so rnx2crx (Hatanaka) succeeds
            self._normalize_epoch_lines(final_file)

            return final_file

        except subprocess.TimeoutExpired:
            raise ConversionError(
                "Docker conversion timed out after 10 minutes",
                raw_file,
            )
        except Exception as e:
            if isinstance(e, ConversionError):
                raise
            raise ConversionError(str(e), raw_file)
        finally:
            self._cleanup_temp_dirs()

    def _normalize_epoch_lines(self, rinex_file: Path) -> None:
        """Normalize RINEX 3 epoch line clock offsets to spec-compliant columns.

        The Trimble native converter (trm2rinex) outputs epoch lines with the
        receiver clock offset field misaligned — it uses 13 spaces + 14 chars
        instead of the RINEX 3.04 spec format: 6X,F15.12 (columns 41-55).
        This causes rnx2crx (Hatanaka compression) to fail with
        "invalid format for clock offset".

        This method rewrites epoch lines in-place to conform to the spec.
        Only data records (lines starting with '> ') are affected; header
        and observation lines are untouched.
        """
        # RINEX 3 epoch line pattern:
        # > YYYY MM DD HH MM SS.SSSSSSS  F NNN      clock_offset
        # Columns: 1-35 = time fields, 36-40 = flag+nsats, 41-55 = 6X+F15.12
        _EPOCH_RE = re.compile(
            r"^(> \d{4} \d{2} \d{2} \d{2} \d{2} [ \d]\d\.\d{7}  \d[ \d]{3})"
            r"\s+"
            r"([-\d][\d.]+)\s*$",
            re.MULTILINE,
        )

        try:
            content = rinex_file.read_text(encoding="ascii", errors="replace")
        except Exception as e:
            self.logger.warning(
                f"Could not read {rinex_file.name} for epoch normalization: {e}"
            )
            return

        fixed_count = 0

        def _fix_epoch(match: re.Match) -> str:
            nonlocal fixed_count
            prefix = match.group(1)  # first 35 chars (time + flag + nsats)
            offset_val = float(match.group(2))
            fixed_count += 1
            return prefix + f"{offset_val:21.12f}"  # 6 spaces + 15-char number

        normalized = _EPOCH_RE.sub(_fix_epoch, content)

        if fixed_count > 0:
            rinex_file.write_text(normalized, encoding="ascii")
            self.logger.debug(
                f"Normalized {fixed_count} epoch lines in {rinex_file.name}"
            )

    def _find_output_file(
        self,
        output_dir: Path,
        observation_date: datetime,
    ) -> Optional[Path]:
        """Find the RINEX output file created by Trimble converter.

        Args:
            output_dir: Directory where converter wrote output
            observation_date: Date of observation

        Returns:
            Path to RINEX file if found
        """
        # Trimble converter creates files with various naming patterns
        patterns = [
            "*.??o",  # RINEX 2/3 obs
            "*.??O",
            "*.rnx",  # RINEX 3
            "*.RNX",
        ]

        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            # Filter to observation files only (not nav)
            obs_files = [
                f
                for f in matches
                if f.suffix.lower() in (".o", ".rnx")
                or (len(f.suffix) == 4 and f.suffix[3].lower() == "o")
            ]
            if obs_files:
                return max(obs_files, key=lambda p: p.stat().st_mtime)

        return None

    def _cleanup_temp_dirs(self) -> None:
        """Clean up temporary directories."""
        for temp_dir in self._temp_dirs:
            try:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                    self.logger.debug(f"Cleaned up {temp_dir}")
            except Exception as e:
                self.logger.warning(f"Could not clean up {temp_dir}: {e}")
        self._temp_dirs.clear()
