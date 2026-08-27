"""PolaRX5 firmware upgrade — self-contained stream-download flash.

Implements Septentrio's "manual download" upgrade (Reference Guide § 1.32, method 5)
over the TCP command port, so it works for deployed stations behind a router:

    login → exeResetReceiver, Upgrade, none → wait for "Ready for SUF download ..."
    → stream the .suf in BINARY → receiver verifies + reboots → reconnect on TLS
    → lif, Identification to confirm the new version.

Why TLS for the reconnect: the upgrade resets ``SISAuthData`` to ``sis = secure`` —
plaintext 28784 closes on reboot, 28783 (TLS) is the only entry point. For
router-forwarded stations the TLS port is reached at ``control_port - 1`` (the same
convention rec-provision uses), so the caller must ensure a ``control_port-1 →
receiver:28783`` DNAT forward exists before flashing (see the CLI's --ensure-port-forward).

The flash itself can only be validated against a real receiver; everything up to the
``exeResetReceiver`` is exercised by --dry-run so the plan can be checked safely.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

#: receiver TLS control port (native). Router maps control_port-1 → this.
RECEIVER_TLS_PORT = 28783
#: the receiver waits this long for the download to *start* after exeResetReceiver.
SUF_DOWNLOAD_START_WINDOW_S = 200
#: how long to wait for the receiver to come back after streaming the .suf.
DEFAULT_REBOOT_WAIT_S = 240
_PROMPT_RE = re.compile(r"IP\d+>")
_READY_RE = re.compile(r"Ready for SUF download", re.IGNORECASE)


class FirmwareUpgradeError(RuntimeError):
    """Any unrecoverable problem during the upgrade flow."""


@dataclass
class UpgradeStep:
    """One source→target hop (usually just one: direct to the target)."""

    suf_path: Path
    target_version: str


@dataclass
class UpgradeResult:
    station_id: str
    ok: bool
    from_version: Optional[str] = None
    to_version: Optional[str] = None
    message: str = ""
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Low-level TCP command primitives (mirrors cmd_rec_provision's helpers).
# --------------------------------------------------------------------------- #
def _recv_until_prompt(sock: socket.socket, timeout: float = 5.0) -> str:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        try:
            sock.settimeout(1.0)
            chunk = sock.recv(4096)
            if chunk:
                buf += chunk
                if _PROMPT_RE.search(buf.decode("utf-8", errors="ignore")[-30:]):
                    break
        except (TimeoutError, OSError):
            if buf:
                break
    return buf.decode("utf-8", errors="ignore")


def _send(sock: socket.socket, cmd: str, *, timeout: float = 5.0) -> str:
    time.sleep(0.15)
    sock.sendall((cmd + "\n").encode("utf-8"))
    return _recv_until_prompt(sock, timeout=timeout)


def connect_control(
    ip: str,
    port: int,
    *,
    timeout: float = 15.0,
    force_tls: bool = False,
) -> tuple[socket.socket, bool]:
    """Open the command port; fall back to TLS on ``port-1`` (sis=secure).

    Returns ``(sock, using_tls)``. Raises ``FirmwareUpgradeError`` if neither the
    plaintext nor the TLS port answers.
    """
    if not force_tls:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(timeout)
        try:
            raw.connect((ip, port))
            raw.recv(1024)  # drain initial prompt
            return raw, False
        except ConnectionRefusedError:
            raw.close()
        except OSError as exc:
            raw.close()
            raise FirmwareUpgradeError(f"{ip}:{port} unreachable: {exc}") from exc

    tls_port = port - 1
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    try:
        raw.connect((ip, tls_port))
    except OSError as exc:
        raw.close()
        raise FirmwareUpgradeError(
            f"TLS port {ip}:{tls_port} unreachable — is the control_port-1 → "
            f"receiver:{RECEIVER_TLS_PORT} forward in place? ({exc})"
        ) from exc
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(raw)
    sock.recv(1024)
    return sock, True


def tls_lifeline_ok(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """True ONLY if ``host:port`` completes a TLS handshake.

    The post-upgrade reconnect needs the receiver's TLS control interface
    (:28783) reachable. A bare TCP check is unsafe: on a shared router the
    ``control_port-1`` port is often a *plaintext* mapping that answers TCP but
    dies after the upgrade closes plaintext. A successful TLS handshake is proof
    the path actually reaches a TLS endpoint. (Caveat: a pre-5.7 receiver may not
    open :28783 until after the upgrade — then this returns False and the caller
    must confirm the router forward another way, e.g. --i-confirm-tls-lifeline.)
    """
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.wrap_socket(raw).close()
        return True
    except (ssl.SSLError, OSError):
        try:
            raw.close()
        except OSError:
            pass
        return False


def login(sock: socket.socket, username: str, password: str) -> bool:
    """Send ``login`` and report whether the session is usable.

    True on success (``$R! LogIn``) or when the firmware does not know the
    command (pre-5.7 → ``$E: Invalid command`` → proceed unauthenticated).
    False is a DEFINITIVE rejection — ``$R? LogIn: Wrong username or password``
    or ``$R? LogIn: Too many failed login attempts``. Callers must NOT retry on
    False: every rejected login feeds the receiver's brute-force counter, which
    on 5.7.0 produces a global lockout (see ``septentrio.fw_policy``).
    """
    resp = _send(sock, f"login, {username}, {password}")
    return "$R! LogIn" in resp or "IP" in resp and "$R? LogIn" not in resp


def read_firmware_version(sock: socket.socket) -> Optional[str]:
    """Parse the firmware version from ``lif, Identification``."""
    resp = _send(sock, "lif, Identification")
    # e.g. "... Firmware: 5.7.0 ..." — accept N.N.N anywhere in the block.
    m = re.search(r"[Ff]irmware[:\s]+v?(\d+\.\d+\.\d+)", resp)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", resp)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# The flash itself.
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _excerpt(buf: bytes, limit: int = 400) -> str:
    """Render what the receiver actually sent, for error messages.

    The previous implementation discarded this buffer, so a failed flash gave
    no clue whether the receiver had objected, gone quiet, or hung up.
    """
    if not buf:
        return "<nothing>"
    text = buf.decode("utf-8", errors="replace").strip()
    if len(text) > limit:
        text = text[:limit] + f"… (+{len(text) - limit} chars)"
    return repr(text)


def await_suf_ready(sock: socket.socket, ready_timeout_s: float = 60.0) -> str:
    """Block until the receiver signals ``Ready for SUF download``.

    Returns the text received. Raises ``FirmwareUpgradeError`` otherwise —
    and crucially distinguishes *why*, because the three failures need
    different operator responses and the previous code reported all of them
    as "never signalled Ready":

    * **EOF** — ``recv()`` returns ``b""``, meaning the receiver closed the
      connection. This is NOT the documented protocol (Reference Guide § 1.32
      method 5 streams the ``.suf`` over the *same* socket), so it means the
      receiver rejected the command or rebooted instead of entering upgrade
      mode. The old code could not see it: a timeout also yielded ``b""``, so
      EOF was swallowed as "no data yet" and the loop spun out the full budget
      before blaming the wrong thing.
    * **socket error** — the link died mid-wait.
    * **timeout** — nothing arrived in time.

    In every failure case NO firmware has been streamed, and a receiver that
    did enter upgrade mode restarts in normal mode once its 200 s
    download-start window lapses. Aborting here is always safe.
    """
    end = time.time() + ready_timeout_s
    buf = b""
    sock.settimeout(2.0)
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
        # TimeoutError is a subclass of OSError — must be caught first.
        except TimeoutError:
            continue
        except OSError as exc:
            raise FirmwareUpgradeError(
                f"socket error while waiting for 'Ready for SUF download': {exc} "
                f"— no firmware was streamed. Receiver said: {_excerpt(buf)}"
            ) from exc
        if not chunk:
            raise FirmwareUpgradeError(
                "receiver closed the connection after 'exeResetReceiver, Upgrade' "
                "instead of signalling 'Ready for SUF download' — no firmware was "
                "streamed, and the receiver restarts in normal mode. "
                f"Receiver said before hanging up: {_excerpt(buf)}"
            )
        buf += chunk
        if _READY_RE.search(buf.decode("utf-8", errors="ignore")):
            return buf.decode("utf-8", errors="ignore")
    raise FirmwareUpgradeError(
        f"receiver never signalled 'Ready for SUF download' within "
        f"{ready_timeout_s:.0f}s of exeResetReceiver — no firmware was streamed. "
        f"Receiver said: {_excerpt(buf)}"
    )


def stream_suf(
    sock: socket.socket,
    suf_path: Path,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    chunk_size: int = 1 << 16,
    ready_timeout_s: float = 60.0,
) -> None:
    """Put the receiver into upgrade mode and stream the .suf in binary.

    Raises ``FirmwareUpgradeError`` if the receiver never signals readiness —
    see :func:`await_suf_ready` for how the failure modes are told apart. The
    receiver reboots on its own once the stream completes; the socket is
    expected to drop — callers reconnect over TLS afterward.

    ``ready_timeout_s`` bounds only the wait for readiness; it does not eat
    into the receiver's 200 s download-start window, which starts when the
    receiver signals ready. Hence the unhurried 60 s default — a slow APN
    link is not a reason to abort a flash.
    """
    total = suf_path.stat().st_size
    # Enter upgrade mode. The receiver replies, then begins waiting for the file.
    sock.sendall(b"exeResetReceiver, Upgrade, none\n")

    ready = await_suf_ready(sock, ready_timeout_s=ready_timeout_s)
    logger.debug("receiver signalled ready: %s", ready.strip()[-200:])

    # Stream the file. Must start within SUF_DOWNLOAD_START_WINDOW_S of readiness.
    sent = 0
    sock.settimeout(60.0)
    with suf_path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            sock.sendall(block)
            sent += len(block)
            if progress:
                progress(sent, total)
    logger.info("streamed %d/%d bytes of %s", sent, total, suf_path.name)


def wait_for_reboot_and_verify(
    ip: str,
    port: int,
    *,
    username: str,
    password: str,
    expect_version: str,
    reboot_wait_s: int = DEFAULT_REBOOT_WAIT_S,
    poll_every_s: int = 10,
    tls_port: Optional[int] = None,
) -> str:
    """Poll for the receiver after the reboot, then confirm the version.

    Tries PLAINTEXT (the control port) first — a deployed station reboots back
    into its saved boot config, which normally keeps FTP + plaintext TCP open
    (this is why JONC reconnected with plain reconcile after a web-UI flash; the
    web UI uses the same ``erst, upgrade`` command). Falls back to TLS only if
    plaintext stays shut (the sis=secure case, for receivers without a persisted
    services config).
    """
    tls_port = tls_port or (port - 1)
    deadline = time.time() + reboot_wait_s
    last_exc: Optional[Exception] = None
    while time.time() < deadline:
        time.sleep(poll_every_s)
        sock = None
        for endpoint, force_tls in ((port, False), (tls_port, True)):
            try:
                sock, _ = connect_control(ip, endpoint, force_tls=force_tls, timeout=10)
                break
            except FirmwareUpgradeError as exc:
                last_exc = exc
        if sock is None:
            continue
        try:
            if not login(sock, username, password):
                # Definitive rejection (wrong creds or the post-flash lockout).
                # STOP — do not retry. Retrying turns one rejected login into
                # ~24 over the reboot window, and on 5.7.0 that trips the GLOBAL
                # brute-force lockout which blocks rec-provision itself.
                raise FirmwareUpgradeError(
                    f"{ip}:{port} came back but login was rejected (wrong "
                    f"credentials or lockout). Not retrying, to avoid feeding "
                    f"the lockout counter — the flash itself may have succeeded. "
                    f"Run rec-provision to restore accounts/FTP, then reconcile "
                    f"the firmware version."
                )
            ver = read_firmware_version(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if ver:
            if ver != expect_version:
                raise FirmwareUpgradeError(
                    f"receiver came back on firmware {ver}, expected {expect_version}"
                )
            return ver
    raise FirmwareUpgradeError(
        f"receiver did not return on {ip}:{port} (plaintext) or :{tls_port} (TLS) "
        f"within {reboot_wait_s}s ({last_exc or 'no version read'})"
    )
