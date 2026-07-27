"""Enable the RutOS conntrack FTP helper over SSH so passive-mode FTP works through NAT.

RutOS 7+ (OpenWrt) ships with ``net.netfilter.nf_conntrack_helper=0``, which
disables *automatic* conntrack-helper assignment. With it off, the
``nf_conntrack_ftp`` helper never attaches to FTP control connections, so
passive-mode data ports are unreachable through the router's NAT and receiver
downloads stall on the data channel (see the ``ftp-pasv-villa-a-teltonika``
vault note and the FTP-helper memory).

Unlike :mod:`receivers.cfg.telemetry_probe` (REST) and ``cfg
ensure-port-forwards``, this is **not** exposed by the RutOS REST API (403 on
this firmware), so it must be applied over SSH (dropbear, ``root`` login —
the REST ``admin`` user shares the password).

:func:`_connect` is the package's only router-SSH entry point (the
``telemetry_probe`` SMS/USSD paths borrow it), so the host-key trust model lives
here: keys are pinned in a per-fleet known_hosts file, recorded on first contact
and refused if they ever change — see :func:`resolve_known_hosts_path` and
:class:`TofuHostKeyPolicy`.

The fix, verified live on a RUT241 (fw ``RUT2M_R_00.07.22.3`` / OpenWrt 21.02):

  - ``net.netfilter.nf_conntrack_helper=1`` — live via ``sysctl -w`` and
    persisted in ``/etc/sysctl.conf`` (the ``/etc/sysctl.d/*.conf`` files are
    upgrade-managed and explicitly say "Do not edit").
  - *optionally* extend ``nf_conntrack_ftp`` to also track the WAN-exposed port:
    the helper defaults to port 21, and the receiver's DNAT maps WAN 2160 → LAN
    21, so port-21 tracking already covers the receiver — ``ports=21,2160`` is
    only belt-and-suspenders for a direct-2160 reach. Off by default because it
    requires a module reload.

**Touches only connection tracking** — never firewall rules or routing — so it
cannot sever the SSH/management path. Idempotent, dry-run by default, with a
read-back verify after a live apply.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse the telemetry-probe credential resolver + exception hierarchy so the
# operator gets the identical "[teltonika] cleartext or pass-path" convention.
from .telemetry_probe import (
    ProbeAuthError,
    ProbeError,
    ProbeUnreachableError,
    _find_receivers_cfg,
    resolve_credentials,
)

logger = logging.getLogger(__name__)

DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "root"  # RutOS dropbear logs in as root, not the REST 'admin'
DEFAULT_TIMEOUT = 15
# Per-fleet known_hosts. Deliberately NOT under ~/.cache — a cache wipe would
# silently reset trust-on-first-use and turn the host-key check into a no-op.
_KNOWN_HOSTS_ENV = "RECEIVERS_ROUTER_KNOWN_HOSTS"
_KNOWN_HOSTS_DEFAULT = "gps_receivers/router_known_hosts"  # under $XDG_DATA_HOME
_HELPER_SYSCTL = "net.netfilter.nf_conntrack_helper"
_SYSCTL_PERSIST_FILE = "/etc/sysctl.conf"
_FTP_PORTS_PARAM = "/sys/module/nf_conntrack_ftp/parameters/ports"
_MODPROBE_FILE = "/etc/modprobe.d/nf_conntrack_ftp.conf"
_FTP_PORTS_RE = re.compile(r"^\d{1,5}(,\d{1,5})*$")


def _validate_ftp_ports(ftp_ports: str) -> str:
    """Return a normalised ``ports`` string (digits/commas), or raise.

    Guards the value before it is interpolated into the SSH ``modprobe`` command
    so there is no shell-injection surface from the ``--ftp-ports`` operator flag.
    """
    norm = ftp_ports.replace(" ", "")
    if not _FTP_PORTS_RE.match(norm):
        raise ProbeError(
            f"invalid --ftp-ports {ftp_ports!r}: expected comma-separated port "
            f"numbers (e.g. 21,2160)"
        )
    return norm


class HostKeyMismatchError(ProbeError):
    """The router presented a host key that differs from the recorded one.

    Distinct from :class:`ProbeUnreachableError` on purpose: "unreachable" reads
    as a flaky link and invites a blind retry, while a changed key must stop the
    operator (the router password is sent right after the handshake).
    """


@dataclass
class ConntrackState:
    """Current router conntrack-helper state, as read over SSH."""

    helper_value: Optional[str] = None  # "0" / "1" / None if unreadable
    helper_persisted: bool = False  # is it set in /etc/sysctl.conf?
    ftp_ports: Optional[str] = None  # e.g. "21" or "21,2160"
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SSH host-key store (per-fleet known_hosts, trust-on-first-use)
# ---------------------------------------------------------------------------


def resolve_known_hosts_path(
    known_hosts: Optional[str] = None, cfg_path: Optional[str] = None
) -> Path:
    """Return the per-fleet router ``known_hosts`` path (not created here).

    Precedence: explicit argument → ``receivers.cfg [teltonika] ssh_known_hosts``
    → ``$RECEIVERS_ROUTER_KNOWN_HOSTS`` → ``$XDG_DATA_HOME/gps_receivers/
    router_known_hosts`` (``~/.local/share/...`` when XDG_DATA_HOME is unset).

    Kept separate from the user's ``~/.ssh/known_hosts``: these are fleet
    devices reached by IP, and mixing them into the personal store would let an
    unrelated ``ssh`` session seed (or clear) trust for a router.
    """
    if known_hosts:
        return Path(known_hosts).expanduser()

    path = _find_receivers_cfg(cfg_path)
    if path:
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(path)
            configured = cp.get("teltonika", "ssh_known_hosts", fallback=None)
            if configured and configured.strip():
                return Path(configured.strip()).expanduser()
        except Exception as exc:  # noqa: BLE001 — cfg is optional; fall through
            logger.debug("teltonika ssh_known_hosts load from %s failed: %s", path, exc)

    env = os.environ.get(_KNOWN_HOSTS_ENV)
    if env:
        return Path(env).expanduser()

    xdg = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(xdg).expanduser() / _KNOWN_HOSTS_DEFAULT


def _prepare_known_hosts(path: Path) -> Path:
    """Create the known_hosts file (0600) and its parent dir (0700) if absent.

    paramiko's ``load_host_keys()`` raises on a missing file, and skipping the
    call would leave ``_host_keys_filename`` unset — first-contact keys would
    then be held in memory only and forgotten on exit, silently degrading to
    AutoAddPolicy behaviour. So the file must exist before we load it.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            # Explicit mode at creation time so the store is never briefly
            # group/world-readable; an existing file's mode is left alone.
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
    except OSError as exc:
        raise ProbeError(
            f"cannot create router known_hosts file {path}: {exc} "
            f"(set [teltonika] ssh_known_hosts in receivers.cfg or "
            f"${_KNOWN_HOSTS_ENV} to a writable path)"
        ) from exc
    return path


def _fingerprint(key: Any) -> str:
    """OpenSSH-style ``SHA256:…`` fingerprint for an operator to compare."""
    try:
        digest = hashlib.sha256(key.asbytes()).digest()
        return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
    except Exception:  # noqa: BLE001 — a fingerprint must never break a connect
        return "<unavailable>"


class TofuHostKeyPolicy:
    """Trust-on-first-use policy: record an unknown router key, warn, continue.

    Duck-types paramiko's ``MissingHostKeyPolicy`` (paramiko only ever calls
    ``missing_host_key``), which keeps paramiko a lazy import for this module.

    This is invoked **only for hosts with no recorded key**. A host that *is*
    recorded and answers with a different key never reaches here — paramiko
    raises ``BadHostKeyException`` first, which :func:`_connect` turns into
    :class:`HostKeyMismatchError`. Failing closed on first contact instead would
    mean hand-seeding a key for every router before the tool could be used at
    all, so TOFU is the deliberate trade: one unauthenticated first contact per
    router, every later one authenticated.
    """

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        client.get_host_keys().add(hostname, key.get_name(), key)
        filename = getattr(client, "_host_keys_filename", None)
        if filename:
            client.save_host_keys(filename)
            try:
                os.chmod(filename, 0o600)  # paramiko writes with the umask
            except OSError as exc:  # noqa: BLE001 — perms are best-effort
                logger.debug("chmod 0600 on %s failed: %s", filename, exc)
        logger.warning(
            "first contact with router %s — trusting and recording host key %s "
            "(%s) in %s; a later change will be refused",
            hostname,
            _fingerprint(key),
            key.get_name(),
            filename or "<memory only>",
        )


# ---------------------------------------------------------------------------
# SSH plumbing
# ---------------------------------------------------------------------------


def _connect(
    host: str,
    *,
    ssh_user: str,
    password: str,
    port: int,
    timeout: int,
    known_hosts: Optional[str] = None,
    cfg_path: Optional[str] = None,
) -> Any:
    """Open a paramiko SSH session (password auth, TOFU-pinned host key).

    The router password is sent immediately after the handshake, so an
    unverified peer harvests it. Host keys are therefore pinned in a per-fleet
    known_hosts file (see :func:`resolve_known_hosts_path`): unknown hosts are
    recorded on first contact (loudly), and a *changed* key is refused.

    paramiko is imported lazily so importing this module / the CLI doesn't hard
    require it until an SSH op actually runs.
    """
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - dep declared in pyproject
        raise ProbeError(
            "paramiko is required for conntrack-helper SSH ops (pip install paramiko)"
        ) from exc

    kh_path = _prepare_known_hosts(resolve_known_hosts_path(known_hosts, cfg_path))

    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(kh_path))
    except Exception as exc:  # noqa: BLE001 — unreadable/corrupt store
        raise ProbeError(
            f"cannot read router known_hosts file {kh_path}: {exc} "
            f"(fix or remove the file, then re-run)"
        ) from exc
    client.set_missing_host_key_policy(TofuHostKeyPolicy())
    try:
        client.connect(
            host,
            port=port,
            username=ssh_user,
            password=password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.AuthenticationException as exc:
        _close_quietly(client)
        raise ProbeAuthError(
            f"{host}: SSH auth failed for {ssh_user!r} — RutOS SSH uses the "
            f"root password (same as the web/admin password)"
        ) from exc
    except paramiko.BadHostKeyException as exc:
        _close_quietly(client)  # paramiko leaves the transport open on this path
        # Must precede the generic handler below: BadHostKeyException is an
        # SSHException, and reporting it as "unreachable" would invite a retry
        # that hands the password to whatever answered.
        # Non-default ports are recorded as "[host]:port" — quoted so the
        # remediation command can be pasted into a shell as-is.
        target = host if port == DEFAULT_SSH_PORT else f'"[{host}]:{port}"'
        raise HostKeyMismatchError(
            f"{host}: SSH host key MISMATCH — refusing to connect and send the "
            f"router password.\n"
            f"  offered:  {_fingerprint(getattr(exc, 'key', None))}\n"
            f"  expected: {_fingerprint(getattr(exc, 'expected_key', None))}\n"
            f"Either the unit was re-flashed/replaced (RutOS regenerates its "
            f"key on factory reset) or this IP now answers for a different "
            f"router — or the session is being intercepted. Verify out of band, "
            f"then drop the stale entry:\n"
            f"  ssh-keygen -R {target} -f {kh_path}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — socket/TLS/timeout → unreachable
        _close_quietly(client)
        raise ProbeUnreachableError(f"{host}: SSH connect failed: {exc}") from exc
    return client


def _close_quietly(client: Any) -> None:
    """Close a half-open client, ignoring errors (caller is already raising)."""
    try:
        client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.debug("SSH client close failed: %s", exc)


def _run(client: Any, cmd: str, *, timeout: int) -> Tuple[int, str, str]:
    """Run a command, return ``(exit_status, stdout, stderr)`` (stripped)."""
    try:
        # cmd is built from module constants + the validated ftp_ports string
        # (digits/commas only — see _validate_ftp_ports), never raw operator
        # input, so there is no shell-injection vector here.
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)  # nosec B601
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        raise ProbeUnreachableError(f"SSH command failed ({cmd!r}): {exc}") from exc
    return rc, out, err


# ---------------------------------------------------------------------------
# State read
# ---------------------------------------------------------------------------


def _read_state(client: Any, *, timeout: int) -> ConntrackState:
    """Read the live conntrack-helper state over an open SSH session."""
    st = ConntrackState()

    rc, out, _ = _run(client, f"sysctl -n {_HELPER_SYSCTL}", timeout=timeout)
    st.helper_value = out if rc == 0 and out in ("0", "1") else (out or None)

    rc, out, _ = _run(
        client,
        f"grep -E '^{_HELPER_SYSCTL}\\s*=\\s*1' {_SYSCTL_PERSIST_FILE}",
        timeout=timeout,
    )
    st.helper_persisted = rc == 0 and bool(out)

    rc, out, _ = _run(client, f"cat {_FTP_PORTS_PARAM} 2>/dev/null", timeout=timeout)
    st.ftp_ports = out or None

    return st


def check_conntrack_helper(
    host: str,
    *,
    ssh_user: str = DEFAULT_SSH_USER,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    ssh_port: int = DEFAULT_SSH_PORT,
    timeout: int = DEFAULT_TIMEOUT,
) -> ConntrackState:
    """Read-only: return the router's current conntrack-helper state."""
    _user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not pw:
        raise ProbeError(
            f"{host}: no [teltonika] password resolved for SSH (set "
            f"receivers.cfg [teltonika] password / password_pass_path, or pass "
            f"--password)"
        )
    client = _connect(
        host,
        ssh_user=ssh_user,
        password=pw,
        port=ssh_port,
        timeout=timeout,
        cfg_path=cfg_path,
    )
    try:
        return _read_state(client, timeout=timeout)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def ensure_conntrack_helper(
    host: str,
    *,
    ssh_user: str = DEFAULT_SSH_USER,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    ssh_port: int = DEFAULT_SSH_PORT,
    ftp_ports: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Idempotently enable ``nf_conntrack_helper`` on a Teltonika router via SSH.

    Always (when needed): set the helper live (``sysctl -w``) and persist it in
    ``/etc/sysctl.conf``. When ``ftp_ports`` is given (e.g. ``"21,2160"``) and
    differs from the live value, also write ``/etc/modprobe.d`` and reload
    ``nf_conntrack_ftp`` (best-effort — a busy module reload is reported, not
    fatal; the persisted file applies on next boot).

    **Mutates the router (connection tracking only — no firewall/routing).**
    Dry-run by default: returns the planned shell ops without sending. Returns a
    dict with ``before`` / ``after`` :class:`ConntrackState`-derived values, the
    list of ``planned`` ops, ``changed`` (bool), and ``applied`` (bool).
    """
    _user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not pw:
        raise ProbeError(
            f"{host}: no [teltonika] password resolved for SSH (set "
            f"receivers.cfg [teltonika] password / password_pass_path, or pass "
            f"--password)"
        )

    client = _connect(
        host,
        ssh_user=ssh_user,
        password=pw,
        port=ssh_port,
        timeout=timeout,
        cfg_path=cfg_path,
    )
    try:
        before = _read_state(client, timeout=timeout)

        planned: List[str] = []
        # 1. Helper toggle — the actual fix.
        need_live = before.helper_value != "1"
        need_persist = not before.helper_persisted
        if need_live:
            planned.append(f"sysctl -w {_HELPER_SYSCTL}=1")
        if need_persist:
            # Replace an existing line, else append — avoids duplicate keys.
            planned.append(
                f"if grep -qE '^{_HELPER_SYSCTL}' {_SYSCTL_PERSIST_FILE}; then "
                f"sed -i 's/^{_HELPER_SYSCTL}.*/{_HELPER_SYSCTL}=1/' "
                f"{_SYSCTL_PERSIST_FILE}; else echo '{_HELPER_SYSCTL}=1' >> "
                f"{_SYSCTL_PERSIST_FILE}; fi"
            )
        # 2. Optional FTP-ports extension (needs a module reload).
        want_ports = _validate_ftp_ports(ftp_ports) if ftp_ports else ""
        do_ports = bool(want_ports) and want_ports != (before.ftp_ports or "")
        if do_ports:
            planned.append(
                f"mkdir -p /etc/modprobe.d && printf 'options nf_conntrack_ftp "
                f"ports=%s\\n' '{want_ports}' > {_MODPROBE_FILE}"
            )
            planned.append(
                f"rmmod nf_conntrack_ftp 2>/dev/null && modprobe nf_conntrack_ftp "
                f"ports={want_ports}"
            )

        result: Dict[str, Any] = {
            "dry_run": dry_run,
            "host": host,
            "before": {
                "helper": before.helper_value,
                "persisted": before.helper_persisted,
                "ftp_ports": before.ftp_ports,
            },
            "planned": planned,
            "changed": bool(planned),
            "applied": False,
        }

        if dry_run or not planned:
            return result

        # Live apply.
        notes: List[str] = []
        for cmd in planned:
            rc, _out, err = _run(client, cmd, timeout=timeout)
            is_reload = cmd.startswith("rmmod ")
            if rc != 0 and not is_reload:
                raise ProbeError(f"{host}: command failed (rc={rc}): {cmd}\n{err}")
            if rc != 0 and is_reload:
                notes.append(
                    "nf_conntrack_ftp live reload skipped (module busy); the "
                    "persisted ports apply on next boot"
                )

        after = _read_state(client, timeout=timeout)
        result["after"] = {
            "helper": after.helper_value,
            "persisted": after.helper_persisted,
            "ftp_ports": after.ftp_ports,
        }
        result["applied"] = True
        result["notes"] = notes
        if after.helper_value != "1":
            raise ProbeError(
                f"{host}: helper still {after.helper_value!r} after apply — "
                f"expected '1'"
            )
        return result
    finally:
        client.close()
