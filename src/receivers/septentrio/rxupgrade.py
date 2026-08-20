"""Firmware flash via Septentrio's own ``rxupgrade`` (the RxTools launcher).

This is the *preferred* backend. The sibling :mod:`firmware_upgrade` re-implements
Septentrio's manual-download protocol over the TCP command port; it is not
hardware-proven and left a deployed receiver (OLAC) in recovery mode, so it stays
gated behind ``--allow-deployed-flash``. RxTools ships the vendor tool already —
``gps-tools`` installs it as ``runRxupgrade`` — and it is what actually flashed
VMEY 5.6.0 → 5.7.0 on 2026-08-20.

Two vendor behaviours drive the design:

**It restores the receiver configuration after upgrading, by default.** That is
the likely reason web-UI/RxTools-flashed stations (AFST, ROTH, JONC) kept
plaintext 28784 open, where the hand-rolled path leaves ``sis = secure`` and only
TLS 28783 answers. Callers who want the vendor's ``-n`` behaviour must ask for it.

**Its exit status cannot be trusted as the flash verdict.** The tool flashes, the
receiver reboots, and then it reconnects to confirm — but the reboot can close the
port it came in on, so the confirmation times out and the run reports an error for
a flash that fully succeeded. VMEY's log, verbatim::

    Rebooting receiver in Upgrade mode
    Uploading and processing ".../PolaRx5-5.7.0.suf"
    Upgrade Finished
    Checking if upgrade succeeded
    Error: Connection timed out.

So the verdict is read from the **log transcript**, not from the return code:
``Upgrade Finished`` means the image was accepted. Confirming the running version
afterwards is the caller's job (``firmware_upgrade.wait_for_reboot_and_verify``),
because only the caller knows which port survived.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Native Septentrio control port. A router that forwards this one straight
#: through needs no ``:port`` suffix on ``-t``.
DEFAULT_CONTROL_PORT = 28784

#: Where gps-tools installs RxTools when it is not on PATH.
_FALLBACK_BINS = (
    Path("/usr/local/rxtools/bin/runRxupgrade"),
    Path("/usr/local/rxtools/bin/rxupgrade"),
)

#: Emitted once the receiver has accepted and processed the image. This — not the
#: exit status — is the flash verdict. See the module docstring.
_SUCCESS_RE = re.compile(r"Upgrade Finished", re.IGNORECASE)

#: The post-flash reconnect the vendor makes to confirm the new version. It fails
#: whenever the reboot closes the port it arrived on, which is expected rather
#: than a fault, so it must not be reported as a failed upgrade.
_VERIFY_STEP_RE = re.compile(r"Checking if upgrade succeeded", re.IGNORECASE)

#: Generous default: the image is ~35 MB and stations sit on 3G/4G links.
DEFAULT_TIMEOUT_S = 1800


class RxUpgradeError(RuntimeError):
    """The vendor tool could not be run, or reported no successful flash."""


@dataclass
class RxUpgradeResult:
    """Outcome of one vendor invocation."""

    station_id: str
    #: True when the transcript carries ``Upgrade Finished``.
    flashed: bool
    returncode: Optional[int]
    log_path: Optional[Path]
    #: True when the tool's own post-flash reconnect failed. Expected when the
    #: reboot closes the port; informational, never a failure on its own.
    verify_step_failed: bool = False
    transcript: str = ""

    @property
    def ok(self) -> bool:
        return self.flashed


def resolve_binary(explicit: Optional[str] = None) -> Path:
    """Find ``runRxupgrade``: explicit path → PATH → the gps-tools install."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise RxUpgradeError(f"rxupgrade binary not found: {p}")
        return p
    for name in ("runRxupgrade", "rxupgrade"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for cand in _FALLBACK_BINS:
        if cand.is_file():
            return cand
    raise RxUpgradeError(
        "runRxupgrade not found on PATH or in /usr/local/rxtools/bin — "
        "install the gps-tools RxTools toolchain, or pass --rxupgrade-bin PATH"
    )


def build_target(host: str, port: int = DEFAULT_CONTROL_PORT) -> str:
    """Render the ``-t`` argument.

    A bare hostname is used for the native port — that is the form proven against
    VMEY. A non-default forwarded port (shared-IP stations such as OLAC/KASC) is
    appended as ``host:port``; that form is *not* yet hardware-proven, so the
    caller logs the exact argument before flashing.
    """
    if port and port != DEFAULT_CONTROL_PORT:
        return f"{host}:{port}"
    return host


def build_command(
    binary: Path,
    *,
    host: str,
    suf: Path,
    port: int = DEFAULT_CONTROL_PORT,
    username: Optional[str] = None,
    password: Optional[str] = None,
    log_path: Optional[Path] = None,
    restore_config: bool = True,
    ignore_ssl_errors: bool = False,
    legacy: bool = False,
) -> List[str]:
    """Assemble the vendor argv.

    ``restore_config=True`` (the default) deliberately omits ``-n``: letting the
    vendor put the configuration back is the behaviour that keeps plaintext 28784
    open across the upgrade.
    """
    cmd: List[str] = [
        str(binary),
        "-t",
        build_target(host, port),
        "-f",
        str(suf),
    ]
    if username:
        cmd += ["-u", username]
    if password:
        cmd += ["-p", password]
    if log_path:
        cmd += ["-l", str(log_path)]
    if not restore_config:
        cmd.append("-n")
    if ignore_ssl_errors:
        cmd.append("-i")
    if legacy:
        cmd.append("-L")
    cmd.append("-q")  # batch: exit when the upgrade is done
    return cmd


def redact(cmd: Sequence[str]) -> str:
    """Render argv for display with the password masked."""
    out: List[str] = []
    mask_next = False
    for tok in cmd:
        if mask_next:
            out.append("<password>")
            mask_next = False
            continue
        if tok == "-p":
            mask_next = True
        out.append(tok)
    return " ".join(out)


def parse_transcript(text: str) -> tuple[bool, bool]:
    """``(flashed, verify_step_failed)`` from a vendor transcript."""
    flashed = bool(_SUCCESS_RE.search(text))
    verify_failed = bool(_VERIFY_STEP_RE.search(text)) and "Error" in text
    return flashed, verify_failed


def run_upgrade(
    *,
    station_id: str,
    host: str,
    suf: Path,
    port: int = DEFAULT_CONTROL_PORT,
    username: Optional[str] = None,
    password: Optional[str] = None,
    log_path: Optional[Path] = None,
    binary: Optional[str] = None,
    restore_config: bool = True,
    ignore_ssl_errors: bool = False,
    legacy: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> RxUpgradeResult:
    """Run the vendor flash and read the verdict from its transcript."""
    exe = resolve_binary(binary)
    if not suf.is_file():
        raise RxUpgradeError(f"firmware image not found: {suf}")
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_command(
        exe,
        host=host,
        suf=suf,
        port=port,
        username=username,
        password=password,
        log_path=log_path,
        restore_config=restore_config,
        ignore_ssl_errors=ignore_ssl_errors,
        legacy=legacy,
    )
    logger.info("rxupgrade: %s", redact(cmd))

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False
        )
        stdio = (proc.stdout or "") + (proc.stderr or "")
        rc: Optional[int] = proc.returncode
    except subprocess.TimeoutExpired as exc:
        # A timeout is NOT automatically a failed flash: the image may already be
        # in and the tool merely stuck on its post-flash reconnect. Fall through
        # to the transcript, which is the authority.
        stdio = (
            (exc.stdout or "") + (exc.stderr or "") if exc.stdout or exc.stderr else ""
        )
        rc = None
        logger.warning(
            "rxupgrade exceeded %ss for %s — reading the transcript for a verdict",
            timeout_s,
            station_id,
        )
    except OSError as exc:
        raise RxUpgradeError(f"could not execute {exe}: {exc}") from exc

    transcript = stdio
    if log_path and log_path.is_file():
        try:
            transcript += "\n" + log_path.read_text(errors="replace")
        except OSError:  # pragma: no cover - log unreadable is not fatal
            pass

    flashed, verify_failed = parse_transcript(transcript)
    return RxUpgradeResult(
        station_id=station_id,
        flashed=flashed,
        returncode=rc,
        log_path=log_path,
        verify_step_failed=verify_failed,
        transcript=transcript,
    )
