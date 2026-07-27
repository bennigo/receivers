"""
Tool manager for RINEX conversion tools.

Handles downloading, installing, and configuring external tools required
for converting GPS receiver data to RINEX format.
"""

import hashlib
import hmac
import logging
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Callable, Dict, List, Optional
from urllib.request import urlretrieve

logger = logging.getLogger(__name__)

# Read size for hashing downloads — archives are tens of MB, so stream them.
_HASH_CHUNK_BYTES = 1024 * 1024

# Schemes we are willing to fetch a binary over. Plain http would let anyone on
# the path swap the payload, and the digest pin (when present) is only as good
# as the definition it is compared against.
_ALLOWED_DOWNLOAD_SCHEMES = ("https://",)


class ToolIntegrityError(RuntimeError):
    """A downloaded tool archive failed an integrity or safety check.

    Raised on checksum mismatch, on a disallowed download scheme, and on
    archive members that would be written outside the extraction directory
    (zip-slip / tar-slip). Always fatal for the install in progress — the
    installers translate it into a failed InstallResult.
    """


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(
    url: str, dest: Path, tool_name: str, expected_sha256: Optional[str]
) -> str:
    """Download ``url`` to ``dest`` and check it against ``expected_sha256``.

    Returns the computed hex digest. Raises ToolIntegrityError on a mismatch or
    a non-https URL, deleting ``dest`` before it does so a rejected payload can
    never be installed or picked up by a later run.
    """
    if not url.lower().startswith(_ALLOWED_DOWNLOAD_SCHEMES):
        raise ToolIntegrityError(
            f"Refusing to download {tool_name} over an insecure URL: {url}"
        )

    urlretrieve(url, dest)  # noqa: S310 — scheme checked above
    digest = _sha256_file(dest)

    if expected_sha256:
        # compare_digest, not ==, so a mismatch cannot be probed by timing.
        if not hmac.compare_digest(digest, expected_sha256.strip().lower()):
            dest.unlink(missing_ok=True)
            raise ToolIntegrityError(
                f"Checksum mismatch for {tool_name} from {url}: "
                f"expected sha256 {expected_sha256.strip().lower()}, got {digest}"
            )
        logger.debug(f"Verified {tool_name} download (sha256 {digest})")
    else:
        logger.warning(
            f"{tool_name} download from {url} is NOT checksum-pinned "
            f"(sha256 {digest}) — add this digest to ToolInfo.sha256 to pin it"
        )

    return digest


def _reject_unsafe_member(name: str, dest_root: Path) -> None:
    """Raise ToolIntegrityError if archive member ``name`` escapes ``dest_root``."""
    # Absolute members ('/etc/cron.d/x', 'C:\\x') would be written outside the
    # destination outright; PureWindowsPath catches the drive-qualified form,
    # which a plain startswith('/') check misses.
    if name.startswith(("/", "\\")) or PureWindowsPath(name).is_absolute():
        raise ToolIntegrityError(f"Archive member has an absolute path: {name!r}")

    # Then the traversal form ('../../etc/x'): normalise and confirm the result
    # is still under the destination.
    target = (dest_root / name.replace("\\", "/")).resolve()
    if target != dest_root and dest_root not in target.parents:
        raise ToolIntegrityError(
            f"Archive member escapes the extraction directory: {name!r}"
        )


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract a zip archive, rejecting members that escape ``dest_dir``.

    zipfile.extractall() on its own will happily honour a member named
    '../../etc/...' (zip-slip), so every member name is validated first and the
    whole archive is refused if any one of them is unsafe.
    """
    dest_root = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            _reject_unsafe_member(member.filename, dest_root)
        zf.extractall(dest_dir)


def _safe_extract_tar(archive_path: Path, dest_dir: Path, mode: str = "r:gz") -> None:
    """Extract a tar archive, rejecting members that escape ``dest_dir``.

    Two layers. The explicit name check refuses absolute and '..' members up
    front — tarfile's own filter merely *strips* a leading '/', which is safe
    but silent, and a tool archive shipping absolute paths is suspect enough to
    abort on. The 'data' filter then covers what names alone cannot: links
    pointing outside the destination, device nodes, and setuid bits. Both raise
    rather than skip, so an unsafe archive fails the install.
    """
    dest_root = dest_dir.resolve()
    with tarfile.open(archive_path, mode) as tf:
        for member in tf.getmembers():
            _reject_unsafe_member(member.name, dest_root)
        tf.extractall(dest_dir, filter="data")


class InstallStatus(Enum):
    """Tool installation status."""

    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    OUTDATED = "outdated"
    MANUAL_REQUIRED = "manual_required"
    UNAVAILABLE = "unavailable"


@dataclass
class InstallResult:
    """Result of a tool installation attempt."""

    success: bool
    tool_name: str
    message: str
    path: Optional[Path] = None
    version: Optional[str] = None


@dataclass
class ToolInfo:
    """Information about a RINEX conversion tool."""

    name: str
    description: str
    required_for: List[str]  # Receiver types that need this tool
    auto_install: bool  # Can be automatically installed
    download_url: Optional[str] = None
    manual_instructions: Optional[str] = None
    version_cmd: Optional[List[str]] = None  # Command to check version
    version_pattern: Optional[str] = None  # Regex to extract version
    install_func: Optional[Callable] = None  # Custom install function
    # Hex SHA-256 of the archive/binary at download_url. Pin this whenever a
    # real digest is available: without it a compromised mirror or a MITM'd
    # download installs an arbitrary binary that the daily pipeline then runs.
    # None means "unpinned" — the install proceeds but logs the digest it saw
    # at WARNING so an operator can paste it back in here. Never invent a value.
    sha256: Optional[str] = None


# Default installation directory
DEFAULT_TOOLS_DIR = Path.home() / ".local" / "share" / "gps-rinex-tools"


class ToolManager:
    """Manages installation and configuration of RINEX conversion tools."""

    # Tool download URLs and metadata
    TOOLS: Dict[str, ToolInfo] = {}

    def __init__(self, tools_dir: Optional[Path] = None):
        """Initialize tool manager.

        Args:
            tools_dir: Directory for tool installations.
                      Defaults to ~/.local/share/gps-rinex-tools/
        """
        self.tools_dir = tools_dir or DEFAULT_TOOLS_DIR
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.bin_dir = self.tools_dir / "bin"
        self.bin_dir.mkdir(exist_ok=True)

        # Initialize tool definitions
        self._init_tool_definitions()

        logger.debug(f"ToolManager initialized with tools_dir={self.tools_dir}")

    def _init_tool_definitions(self):
        """Initialize tool definitions with download URLs and metadata."""

        # Detect platform for download URLs
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "linux" and machine in ("x86_64", "amd64"):
            teqc_url = "https://www.unavco.org/software/data-processing/teqc/development/teqc_CentOSLx86_64d.zip"
            # gfzrnx now requires registration - set to None for manual install
            gfzrnx_url = None
            rnx2crx_url = "https://terras.gsi.go.jp/ja/crx2rnx/RNXCMP_4.1.0_Linux_x86_64bit.tar.gz"
        elif system == "darwin":
            teqc_url = "https://www.unavco.org/software/data-processing/teqc/development/teqc_OSX_x86_64_Intel.zip"
            gfzrnx_url = None
            rnx2crx_url = (
                "https://terras.gsi.go.jp/ja/crx2rnx/RNXCMP_4.1.0_MacOS_Intel.tar.gz"
            )
        else:
            teqc_url = None
            gfzrnx_url = None
            rnx2crx_url = None

        self.TOOLS = {
            "teqc": ToolInfo(
                name="teqc",
                description="UNAVCO TEQC - converts Leica MDB/m00 and other formats to RINEX 2",
                required_for=["G10", "Leica"],
                auto_install=teqc_url is not None,
                download_url=teqc_url,
                # UNAVCO publishes no digest for the teqc archives (and the
                # project is end-of-life), so this stays unpinned and warns.
                sha256=None,
                version_cmd=["teqc", "-version"],
                version_pattern=r"teqc\s+(\d{4}[A-Za-z]+\d+)",
                manual_instructions=(
                    "Download from https://www.unavco.org/software/data-processing/teqc/teqc.html\n"
                    "Note: TEQC is end-of-life (final release 2019-02-25) but still works."
                ),
            ),
            "gfzrnx": ToolInfo(
                name="gfzrnx",
                description="GFZ RINEX toolkit - format conversion, QC, splicing",
                required_for=["all"],
                auto_install=False,  # Now requires registration
                download_url=gfzrnx_url,
                # Download is behind a registration wall (url is None on every
                # platform) — nothing to pin until that changes.
                sha256=None,
                version_cmd=["gfzrnx", "-help"],  # Version shown at end of help
                version_pattern=r"VERSION:\s*gfzrnx-([\d.]+-\d+)",
                manual_instructions=(
                    "gfzrnx now requires registration (free for non-commercial use).\n"
                    "1. Visit https://gnss.gfz-potsdam.de/services/gfzrnx\n"
                    "2. Register for a free scientific license\n"
                    "3. Download the Linux 64-bit binary\n"
                    "4. Copy to ~/.local/share/gps-rinex-tools/bin/gfzrnx\n"
                    "5. Run: chmod +x ~/.local/share/gps-rinex-tools/bin/gfzrnx"
                ),
            ),
            "rnx2crx": ToolInfo(
                name="rnx2crx",
                description="Hatanaka compression - RINEX to compact RINEX",
                required_for=["all"],
                auto_install=rnx2crx_url is not None,
                download_url=rnx2crx_url,
                # GSI ships RNXCMP without a published checksum; pin here if a
                # digest is ever obtained out of band (fail-closed on mismatch).
                sha256=None,
                version_cmd=["RNX2CRX", "-h"],
                version_pattern=r"ver\.?\s*([\d.]+)",
                manual_instructions=(
                    "Download from https://terras.gsi.go.jp/ja/crx2rnx.html\n"
                    "Includes both RNX2CRX and CRX2RNX."
                ),
            ),
            "mdb2rinex": ToolInfo(
                name="mdb2rinex",
                description="Leica MDB to RINEX 3 converter (official Leica tool)",
                required_for=["G10", "Leica", "GR10", "GR25", "GR30", "GR50"],
                auto_install=False,
                download_url=None,
                version_cmd=["mdb2rinex", "-h"],
                manual_instructions=(
                    "Download from Leica myWorld portal:\n"
                    "1. Visit https://myworld.leica-geosystems.com/\n"
                    "2. Navigate to your GNSS receiver product (GR10, GR25, etc.)\n"
                    "3. Find 'Tools' section and download mdb2rinex for Linux\n"
                    "4. Extract and copy to this tools directory\n"
                    "\n"
                    "Requires a Leica myWorld account (free with Leica hardware)."
                ),
            ),
            "runpkr00": ToolInfo(
                name="runpkr00",
                description="Trimble T00/T02 raw data extractor",
                required_for=["NetR9", "NetRS", "NetR5", "Trimble"],
                auto_install=False,
                download_url=None,
                version_cmd=["runpkr00"],  # Outputs version on stderr with no args
                version_pattern=r"Version (\d+\.\d+)",
                manual_instructions=(
                    "Trimble runpkr00 is available from UNAVCO:\n"
                    "https://kb.unavco.org/article/trimble-runpkr00-latest-versions-744.html\n"
                    "\n"
                    "Download the Linux RPM and extract with:\n"
                    "  pip install rpmfile\n"
                    '  python -c "import rpmfile; ..."\n'
                    "\n"
                    "Or install via Trimble Business Center."
                ),
            ),
            "sbf2rin": ToolInfo(
                name="sbf2rin",
                description="Septentrio SBF to RINEX converter",
                required_for=["PolaRX5", "PolaRx5", "Septentrio"],
                auto_install=False,
                download_url=None,
                version_cmd=["sbf2rin", "-V"],  # Outputs version string
                version_pattern=r"sbf2rin-([\d.]+)",
                manual_instructions=(
                    "Part of Septentrio RxTools package.\n"
                    "1. Download RxTools from https://www.septentrio.com/\n"
                    "2. Requires Septentrio account (free)\n"
                    "3. Install and add to PATH or copy sbf2rin binary"
                ),
            ),
        }

    def list_tools(self) -> Dict[str, Dict]:
        """List all tools with their installation status.

        Returns:
            Dictionary of tool name -> status info
        """
        result = {}
        for name, info in self.TOOLS.items():
            installed_path = self._find_tool(name)
            status = (
                InstallStatus.INSTALLED
                if installed_path
                else InstallStatus.NOT_INSTALLED
            )

            if not info.auto_install and not installed_path:
                status = InstallStatus.MANUAL_REQUIRED

            version = None
            if installed_path and info.version_cmd:
                version = self._get_tool_version(name, installed_path)

            result[name] = {
                "name": name,
                "description": info.description,
                "status": status.value,
                "installed_path": str(installed_path) if installed_path else None,
                "version": version,
                "auto_install": info.auto_install,
                "required_for": info.required_for,
            }

        return result

    def _find_tool(self, name: str) -> Optional[Path]:
        """Find a tool in the tools directory or system PATH.

        Args:
            name: Tool name

        Returns:
            Path to tool if found, None otherwise
        """
        # Check our tools bin directory first
        tool_path = self.bin_dir / name
        if tool_path.exists() and os.access(tool_path, os.X_OK):
            return tool_path

        # Check for uppercase variants (RNX2CRX, CRX2RNX)
        for variant in [name, name.upper(), name.lower()]:
            tool_path = self.bin_dir / variant
            if tool_path.exists() and os.access(tool_path, os.X_OK):
                return tool_path

        # Check system PATH
        system_path = shutil.which(name)
        if system_path:
            return Path(system_path)

        # Check uppercase in PATH
        system_path = shutil.which(name.upper())
        if system_path:
            return Path(system_path)

        return None

    def _get_tool_version(self, name: str, path: Path) -> Optional[str]:
        """Get version string for a tool.

        Args:
            name: Tool name
            path: Path to tool executable

        Returns:
            Version string or None
        """
        info = self.TOOLS.get(name)
        if not info or not info.version_cmd:
            return None

        try:
            cmd = [str(path)] + info.version_cmd[1:]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout + result.stderr

            if info.version_pattern:
                import re

                match = re.search(info.version_pattern, output)
                if match:
                    return match.group(1)

            # Return first line as fallback
            lines = output.strip().split("\n")
            if lines:
                return lines[0][:50]

        except Exception as e:
            logger.debug(f"Could not get version for {name}: {e}")

        return None

    def install(self, tool_name: str, force: bool = False) -> InstallResult:
        """Install a specific tool.

        Args:
            tool_name: Name of tool to install
            force: Force reinstall even if already installed

        Returns:
            InstallResult with success status and details
        """
        if tool_name not in self.TOOLS:
            return InstallResult(
                success=False,
                tool_name=tool_name,
                message=f"Unknown tool: {tool_name}. Available: {', '.join(self.TOOLS.keys())}",
            )

        info = self.TOOLS[tool_name]

        # Check if already installed
        existing = self._find_tool(tool_name)
        if existing and not force:
            return InstallResult(
                success=True,
                tool_name=tool_name,
                message=f"Already installed at {existing}",
                path=existing,
            )

        # Check if auto-install is available
        if not info.auto_install:
            return InstallResult(
                success=False,
                tool_name=tool_name,
                message=f"Manual installation required:\n{info.manual_instructions}",
            )

        # Dispatch to specific installer
        if tool_name == "teqc":
            return self._install_teqc()
        elif tool_name == "gfzrnx":
            return self._install_gfzrnx()
        elif tool_name == "rnx2crx":
            return self._install_hatanaka()
        else:
            return InstallResult(
                success=False,
                tool_name=tool_name,
                message=f"No installer implemented for {tool_name}",
            )

    def install_all(self, force: bool = False) -> List[InstallResult]:
        """Install all auto-installable tools.

        Args:
            force: Force reinstall

        Returns:
            List of InstallResults
        """
        results = []
        for name, info in self.TOOLS.items():
            if info.auto_install:
                result = self.install(name, force=force)
                results.append(result)
        return results

    def _install_teqc(self) -> InstallResult:
        """Install TEQC from UNAVCO."""
        info = self.TOOLS["teqc"]

        if not info.download_url:
            return InstallResult(
                success=False,
                tool_name="teqc",
                message="No download URL for this platform",
            )

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                zip_path = tmpdir / "teqc.zip"

                logger.info(f"Downloading teqc from {info.download_url}")
                print("Downloading teqc...")
                _download_verified(info.download_url, zip_path, "teqc", info.sha256)

                _safe_extract_zip(zip_path, tmpdir)

                # Find the teqc binary. Skip the archive itself — it is named
                # teqc.zip and would otherwise match this prefix test.
                teqc_bin = None
                for f in tmpdir.iterdir():
                    if f == zip_path:
                        continue
                    if f.name.startswith("teqc") and f.is_file():
                        teqc_bin = f
                        break

                if not teqc_bin:
                    return InstallResult(
                        success=False,
                        tool_name="teqc",
                        message="Could not find teqc binary in downloaded archive",
                    )

                # Copy to bin directory
                dest = self.bin_dir / "teqc"
                shutil.copy2(teqc_bin, dest)
                dest.chmod(
                    dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )

                logger.info(f"Installed teqc to {dest}")
                print(f"✅ Installed teqc to {dest}")

                return InstallResult(
                    success=True,
                    tool_name="teqc",
                    message=f"Installed to {dest}",
                    path=dest,
                )

        except Exception as e:
            logger.error(f"Failed to install teqc: {e}")
            return InstallResult(
                success=False,
                tool_name="teqc",
                message=f"Installation failed: {e}",
            )

    def _install_gfzrnx(self) -> InstallResult:
        """Install GFZRNX from GFZ Potsdam."""
        info = self.TOOLS["gfzrnx"]

        if not info.download_url:
            return InstallResult(
                success=False,
                tool_name="gfzrnx",
                message="No download URL for this platform",
            )

        try:
            dest = self.bin_dir / "gfzrnx"

            logger.info(f"Downloading gfzrnx from {info.download_url}")
            print("Downloading gfzrnx...")
            # Stage in a temp dir and only move into bin/ once the digest checks
            # out — downloading straight onto `dest` would leave an unverified
            # binary on the tool path when verification fails.
            with tempfile.TemporaryDirectory() as tmpdir:
                staged = Path(tmpdir) / "gfzrnx"
                _download_verified(info.download_url, staged, "gfzrnx", info.sha256)
                shutil.copy2(staged, dest)

            # Make executable
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            logger.info(f"Installed gfzrnx to {dest}")
            print(f"✅ Installed gfzrnx to {dest}")

            return InstallResult(
                success=True,
                tool_name="gfzrnx",
                message=f"Installed to {dest}",
                path=dest,
            )

        except Exception as e:
            logger.error(f"Failed to install gfzrnx: {e}")
            return InstallResult(
                success=False,
                tool_name="gfzrnx",
                message=f"Installation failed: {e}",
            )

    def _install_hatanaka(self) -> InstallResult:
        """Install Hatanaka compression tools (RNX2CRX, CRX2RNX)."""
        info = self.TOOLS["rnx2crx"]

        if not info.download_url:
            return InstallResult(
                success=False,
                tool_name="rnx2crx",
                message="No download URL for this platform",
            )

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                archive_path = tmpdir / "rnxcmp.tar.gz"

                logger.info(f"Downloading Hatanaka tools from {info.download_url}")
                print("Downloading Hatanaka compression tools...")
                _download_verified(
                    info.download_url, archive_path, "rnx2crx", info.sha256
                )

                _safe_extract_tar(archive_path, tmpdir)

                # Find the binaries (they're in a subdirectory)
                installed = []
                for root, _dirs, files in os.walk(tmpdir):
                    for fname in files:
                        if fname in ("RNX2CRX", "CRX2RNX", "rnx2crx", "crx2rnx"):
                            src = Path(root) / fname
                            dest = self.bin_dir / fname
                            shutil.copy2(src, dest)
                            dest.chmod(
                                dest.stat().st_mode
                                | stat.S_IXUSR
                                | stat.S_IXGRP
                                | stat.S_IXOTH
                            )
                            installed.append(fname)
                            print(f"✅ Installed {fname} to {dest}")

                if not installed:
                    return InstallResult(
                        success=False,
                        tool_name="rnx2crx",
                        message="Could not find RNX2CRX/CRX2RNX in archive",
                    )

                return InstallResult(
                    success=True,
                    tool_name="rnx2crx",
                    message=f"Installed: {', '.join(installed)}",
                    path=self.bin_dir / installed[0],
                )

        except Exception as e:
            logger.error(f"Failed to install Hatanaka tools: {e}")
            return InstallResult(
                success=False,
                tool_name="rnx2crx",
                message=f"Installation failed: {e}",
            )

    def check_tools(self, receiver_type: Optional[str] = None) -> Dict[str, bool]:
        """Check which tools are available.

        Args:
            receiver_type: Optional filter by receiver type

        Returns:
            Dictionary of tool name -> available status
        """
        result = {}

        if receiver_type:
            # Use the accurate mapping of receiver type to tools
            tools_to_check = self.get_tools_for_receiver(receiver_type)
            for name in tools_to_check:
                path = self._find_tool(name)
                result[name] = path is not None
        else:
            # Check all tools
            for name in self.TOOLS:
                path = self._find_tool(name)
                result[name] = path is not None

        return result

    def get_tool_path(self, name: str) -> Optional[Path]:
        """Get path to a tool if installed.

        Args:
            name: Tool name

        Returns:
            Path to tool or None
        """
        return self._find_tool(name)

    def configure_receivers_cfg(self, config_path: Optional[Path] = None) -> bool:
        """Update receivers.cfg with tool paths.

        Args:
            config_path: Path to receivers.cfg (default: ~/.config/gpsconfig/receivers.cfg)

        Returns:
            True if config was updated
        """
        if config_path is None:
            config_path = Path.home() / ".config" / "gpsconfig" / "receivers.cfg"

        if not config_path.exists():
            logger.warning(f"Config file not found: {config_path}")
            return False

        import configparser

        config = configparser.ConfigParser()
        config.read(config_path)

        if "rinex_tools" not in config:
            config["rinex_tools"] = {}

        updated = False
        for name in self.TOOLS:
            path = self._find_tool(name)
            if path:
                key = f"{name}_path"
                current = config.get("rinex_tools", key, fallback=None)
                if current != str(path):
                    config["rinex_tools"][key] = str(path)
                    updated = True
                    logger.info(f"Updated {key} = {path}")

        if updated:
            with open(config_path, "w") as f:
                config.write(f)
            print(f"Updated {config_path}")

        return updated

    def get_installation_guide(self, tool_names: List[str]) -> str:
        """Get detailed installation instructions for missing tools.

        Args:
            tool_names: List of tool names that need to be installed

        Returns:
            Formatted string with installation instructions
        """
        lines = []
        lines.append("\n" + "=" * 60)
        lines.append("MISSING TOOLS - Installation Guide")
        lines.append("=" * 60)

        for name in tool_names:
            info = self.TOOLS.get(name)
            if not info:
                lines.append(f"\n{name}: Unknown tool")
                continue

            lines.append(f"\n{name}")
            lines.append("-" * len(name))
            lines.append(f"  {info.description}")

            if info.auto_install:
                lines.append("\n  Quick install:")
                lines.append(f"    receivers tools install {name}")
            elif info.manual_instructions:
                lines.append("\n  Manual installation required:")
                for line in info.manual_instructions.split("\n"):
                    lines.append(f"    {line}")

            lines.append("\n  Or configure custom path in receivers.cfg:")
            lines.append("    [rinex_tools]")
            lines.append(f"    {name}_path = /path/to/{name}")

        lines.append("\n" + "=" * 60)
        lines.append("Quick commands:")
        lines.append("  receivers tools list        # Show all tools")
        lines.append("  receivers tools install-all # Install auto-installable tools")
        lines.append("  receivers tools check       # Verify installation")
        lines.append("=" * 60 + "\n")

        return "\n".join(lines)

    def get_tools_for_receiver(self, receiver_type: str) -> List[str]:
        """Get list of tools required for a specific receiver type.

        Args:
            receiver_type: Receiver type (e.g., 'netr9', 'polarx5', 'g10')

        Returns:
            List of required tool names
        """
        receiver_lower = receiver_type.lower()
        required = []

        # Map receiver types to tool requirements
        if "polarx" in receiver_lower or "septentrio" in receiver_lower:
            required = ["sbf2rin", "gfzrnx"]
        elif (
            "netr9" in receiver_lower
            or "netrs" in receiver_lower
            or "trimble" in receiver_lower
        ):
            required = ["runpkr00", "teqc", "gfzrnx"]
        elif (
            "g10" in receiver_lower
            or "leica" in receiver_lower
            or "gr" in receiver_lower
        ):
            required = ["mdb2rinex", "gfzrnx"]  # mdb2rinex preferred, teqc fallback

        # Always need rnx2crx for Hatanaka compression
        if "rnx2crx" not in required:
            required.append("rnx2crx")

        return required

    def check_tools_with_details(
        self, receiver_type: Optional[str] = None
    ) -> Dict[str, Dict]:
        """Check tool availability with detailed status.

        Args:
            receiver_type: If specified, only check tools for this receiver

        Returns:
            Dictionary with tool status and installation info
        """
        result = {}

        if receiver_type:
            tools_to_check = self.get_tools_for_receiver(receiver_type)
        else:
            tools_to_check = list(self.TOOLS.keys())

        for name in tools_to_check:
            info = self.TOOLS.get(name)
            path = self._find_tool(name)

            result[name] = {
                "available": path is not None,
                "path": str(path) if path else None,
                "auto_install": info.auto_install if info else False,
                "manual_instructions": info.manual_instructions if info else None,
            }

        return result
