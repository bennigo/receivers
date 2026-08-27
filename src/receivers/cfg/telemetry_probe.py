"""Teltonika RutOS telemetry-identity probe for ``cfg replace-modem/replace-sim``.

Auto-extracts a station's router (``modem_gsm``) and SIM (``sim_card``) identity
from a live Teltonika router over the RutOS 7.x **REST API**, so an operator
recording a field swap doesn't transcribe serials/MAC/ICCID by hand. Mirrors the
GNSS-receiver :mod:`receivers.cfg.device_probe` surface: one
:func:`probe_teltonika` entry point returning a uniform :class:`TelemetryIdentity`.

Field map verified live 2026-06-06 against a RUT241 (fw RUT2M_R_00.07.22.3); see
the ``reference_teltonika_rutos_rest_api`` memory for the full endpoint dump.

  Router identity   ← GET /api/system/device/status → data
      mnfinfo.serial            router serial_number
      static.device_name        model (e.g. "RUT241")
      ports[] WAN mac           mac_address (colon-formatted)
  SIM / mobile      ← GET /api/modems/status → data[0]
      iccid                     SIM serial_number
      operator                  provider
      conntype "4G (LTE)"       modem subtype "4G"
      imsi / imei               module identity (informational)
  WAN IP            ← GET /api/interfaces/status → default-route iface
      ipv4-address[0].address   SIM ip_address

Transport: REST over HTTPS with a self-signed cert (verify disabled). Auth is a
short-lived bearer token (``POST /api/login``, ~299 s TTL) — fine for a probe
that fires a handful of GETs and exits. Credentials come from ``receivers.cfg``
``[teltonika]`` (cleartext ``username``/``password`` OR ``*_pass_path`` via
pass(1)), with explicit overrides; same convention as the TOS ``[tos]`` section.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# RutOS bearer tokens expire after ~299 s; a probe finishes in well under that,
# so we authenticate once per probe and don't bother refreshing.
DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class TelemetryIdentity:
    """Probed identity for a station's telemetry hardware (router + SIM).

    Split into the two TOS device subtypes the values map onto. Every field is
    optional — a probe that can't read one (older firmware, no SIM inserted)
    leaves it ``None`` and the operator supplies it via an explicit flag.
    """

    host: str
    # modem_gsm (router). router_lan_ip is the router's own management/LAN
    # address (e.g. 192.168.100.1) — distinct from the mobile WAN IP, which is
    # a SIM attribute. Keeping them on separate entities avoids double-entry.
    router_serial: Optional[str] = None
    router_model: Optional[str] = None
    router_manufacturer: Optional[str] = None
    router_mac: Optional[str] = None
    router_firmware: Optional[str] = None
    router_lan_ip: Optional[str] = None  # LAN/management IP → modem.ip_address
    modem_subtype: Optional[str] = None  # e.g. "4G" — derived from conntype
    # sim_card
    sim_iccid: Optional[str] = None
    sim_ip_address: Optional[str] = None  # mobile WAN IP → sim.ip_address
    provider: Optional[str] = None
    imsi: Optional[str] = None  # informational (module identity)
    imei: Optional[str] = None  # informational (module identity)


class ProbeError(Exception):
    """Base class for telemetry-probe failures."""


class ProbeUnreachableError(ProbeError):
    """Network / socket / TLS failure — router did not respond."""


class ProbeAuthError(ProbeError):
    """Router responded but rejected the credentials (HTTP 401/403 on login)."""


class ProbeCredentialsError(ProbeError):
    """No usable credentials could be resolved (config + overrides both empty)."""


# ---------------------------------------------------------------------------
# Credential resolution — mirrors tostools [tos] (cleartext OR pass-path)
# ---------------------------------------------------------------------------


def _find_receivers_cfg(cfg_path: Optional[str] = None) -> Optional[str]:
    """Locate receivers.cfg, reusing the package's own resolver."""
    if cfg_path:
        return cfg_path
    try:
        from ..config.receivers_config import ReceiversConfig

        return ReceiversConfig().config_path
    except Exception:  # noqa: BLE001 — fall through to None; caller handles
        return None


def resolve_credentials(
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve Teltonika router credentials.

    Precedence (each of username/password independently): explicit override →
    ``[teltonika] *_pass_path`` (via pass(1)) → ``[teltonika]`` cleartext. The
    pass-path support reuses :func:`tostools.api.tos_writer.load_password_from_pass`,
    the same helper backing the ``[tos]`` credentials, so the operator gets the
    identical "cleartext for convenience, pass for secrecy" choice.

    Returns ``(username, password)`` — either may be ``None`` if unresolved.
    """
    # Reuse the battle-tested pass resolver from tostools (already a dependency).
    try:
        from tostools.api.tos_writer import load_password_from_pass
    except Exception:  # noqa: BLE001 — pass support optional; cleartext still works

        def load_password_from_pass(pass_spec: str) -> Optional[str]:  # type: ignore[misc]
            return None

    path = _find_receivers_cfg(cfg_path)
    cfg_user = cfg_pass = None
    if path:
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(path)
            if cp.has_section("teltonika"):
                u_pp = cp.get("teltonika", "username_pass_path", fallback=None)
                if u_pp:
                    cfg_user = load_password_from_pass(u_pp.strip())
                if not cfg_user:
                    cfg_user = cp.get("teltonika", "username", fallback=None) or None
                p_pp = cp.get("teltonika", "password_pass_path", fallback=None)
                if p_pp:
                    cfg_pass = load_password_from_pass(p_pp.strip())
                if not cfg_pass:
                    cfg_pass = cp.get("teltonika", "password", fallback=None) or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("teltonika cred load from %s failed: %s", path, exc)

    return (username or cfg_user, password or cfg_pass)


def resolve_discover_phone_to(cfg_path: Optional[str] = None) -> Optional[str]:
    """Return the MSISDN-discovery catcher number from ``[teltonika]``.

    Reads ``discover_phone_to`` (the operator's mobile that receives the
    discovery SMS so its sender header reveals the field SIM's number). Returns
    ``None`` when unset — the CLI then requires ``--discover-phone-to`` on the
    command line.
    """
    path = _find_receivers_cfg(cfg_path)
    if not path:
        return None
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path)
        return cp.get("teltonika", "discover_phone_to", fallback=None) or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("teltonika discover_phone_to load failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


def _conntype_to_subtype(conntype: Optional[str]) -> Optional[str]:
    """Map a RutOS ``conntype`` (e.g. ``"4G (LTE)"``) to a TOS modem subtype.

    TOS records modem ``subtype`` as a bare generation string (``"3G"``/``"4G"``/
    ``"5G"`` — verified against existing router entities). RutOS reports richer
    strings like ``"4G (LTE)"``; take the leading ``<n>G`` token.
    """
    if not conntype:
        return None
    token = conntype.strip().split()[0]  # "4G (LTE)" → "4G"
    return token or None


def _iface_addr(iface: Any) -> Optional[str]:
    """Return an interface dict's first IPv4 address, or None."""
    if not isinstance(iface, dict):
        return None
    addrs = iface.get("ipv4-address") or []
    if addrs and isinstance(addrs[0], dict):
        return addrs[0].get("address") or None
    return None


def _wan_ip(interfaces_data: Any) -> Optional[str]:
    """Pull the mobile WAN IPv4 from /api/interfaces/status: the default-route iface.

    The WAN interface is the one carrying a default route (``route[].target ==
    "0.0.0.0"``); its address is the mobile/public IP that belongs on the SIM.
    Verified live: the mobile interface (``mob1s1a1_4``) holds the default
    route. Returns None when no default-route interface has an address — we do
    NOT fall back to a non-default address, since that would wrongly pick up the
    LAN IP (a separate, router-owned attribute).
    """
    if not isinstance(interfaces_data, list):
        return None
    for iface in interfaces_data:
        if not isinstance(iface, dict):
            continue
        routes = iface.get("route") or []
        # "0.0.0.0" here is the RutOS default-route *target* we match against
        # (the WAN iface carries it) — not a socket bind address.
        has_default = any(
            isinstance(r, dict) and r.get("target") == "0.0.0.0"  # nosec B104
            for r in routes
        )
        if has_default:
            addr = _iface_addr(iface)
            if addr:
                return addr
    return None


def _lan_ip(interfaces_data: Any) -> Optional[str]:
    """Pull the router LAN/management IPv4 (the ``lan`` interface, e.g. 192.168.100.1).

    This is the router's OWN address → belongs on the modem_gsm entity, distinct
    from the mobile WAN IP (:func:`_wan_ip`, a SIM attribute). Matched by
    interface name == ``"lan"`` (verified live: name/interface/id all == "lan").
    """
    if not isinstance(interfaces_data, list):
        return None
    for iface in interfaces_data:
        if not isinstance(iface, dict):
            continue
        name = (iface.get("name") or iface.get("interface") or "").lower()
        if name == "lan":
            return _iface_addr(iface)
    return None


def _login(
    host: str,
    *,
    username: Optional[str],
    password: Optional[str],
    cfg_path: Optional[str],
    timeout: int,
    verify_tls: bool,
):
    """Authenticate to a RutOS router; return ``(base_url, session, headers)``.

    Shared by :func:`probe_teltonika` (read) and :func:`send_sms` (write).
    Resolves credentials, opens a TLS-relaxed session, POSTs /api/login, and
    returns the bearer-auth header. Raises the same Probe* errors as the probe.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover — requests is a hard dep
        raise ProbeError("requests not installed — cannot reach router") from exc

    user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not user or not pw:
        raise ProbeCredentialsError(
            "No Teltonika credentials. Add a [teltonika] section to "
            "receivers.cfg (username/password or username_pass_path/"
            "password_pass_path) or pass --username/--password."
        )

    base = host if host.startswith("http") else f"https://{host}"
    base = base.rstrip("/")

    session = requests.Session()
    session.verify = verify_tls
    if not verify_tls:
        # Suppress the noisy per-request InsecureRequestWarning — self-signed
        # router certs are expected and the operator opted in via verify_tls.
        try:
            from urllib3.exceptions import InsecureRequestWarning

            requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
                InsecureRequestWarning
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        resp = session.post(
            f"{base}/api/login",
            json={"username": user, "password": pw},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise ProbeUnreachableError(f"{host}: login request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        raise ProbeAuthError(f"{host}: login rejected (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise ProbeUnreachableError(
            f"{host}: unexpected login status HTTP {resp.status_code}"
        )
    try:
        token = (resp.json().get("data") or {}).get("token")
    except (ValueError, AttributeError) as exc:
        raise ProbeUnreachableError(f"{host}: login response not JSON") from exc
    if not token:
        raise ProbeAuthError(f"{host}: login succeeded but no token returned")

    return base, session, {"Authorization": f"Bearer {token}"}


def probe_teltonika(
    host: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify_tls: bool = False,
) -> TelemetryIdentity:
    """Probe a Teltonika RutOS router and return its telemetry identity.

    Args:
        host: Router IP or hostname (scheme optional; HTTPS assumed).
        username / password: Explicit credential overrides; resolved from
            ``receivers.cfg [teltonika]`` when omitted.
        cfg_path: Override receivers.cfg location (testing / non-standard host).
        timeout: Per-request timeout in seconds.
        verify_tls: Verify the router's TLS cert. Default ``False`` — fleet
            routers use self-signed certs. Set ``True`` only with a real cert.

    Returns:
        :class:`TelemetryIdentity` with whatever fields the router exposed.

    Raises:
        ProbeCredentialsError: No usable credentials.
        ProbeAuthError: Login rejected (bad credentials).
        ProbeUnreachableError: Network/TLS failure or unexpected HTTP error.
    """
    base, session, headers = _login(
        host,
        username=username,
        password=password,
        cfg_path=cfg_path,
        timeout=timeout,
        verify_tls=verify_tls,
    )

    def _get(endpoint: str) -> Optional[Any]:
        """GET an endpoint; return its ``data`` payload, or None on any failure.

        Probes are best-effort per-field — a missing/forbidden endpoint degrades
        the identity (fields stay None) rather than failing the whole probe.
        Broad except: a single flaky endpoint must never fail the whole probe.
        """
        try:
            r = session.get(f"{base}{endpoint}", headers=headers, timeout=timeout)
            if r.status_code != 200:
                logger.debug("%s %s → HTTP %s", host, endpoint, r.status_code)
                return None
            return r.json().get("data")
        except Exception as exc:  # noqa: BLE001 — best-effort per-field
            logger.debug("%s %s failed: %s", host, endpoint, exc)
            return None

    identity = TelemetryIdentity(host=host)

    # --- Router identity: /api/system/device/status ----------------------
    dev = _get("/api/system/device/status")
    if isinstance(dev, dict):
        mnf = dev.get("mnfinfo") or {}
        identity.router_serial = mnf.get("serial") or None
        static = dev.get("static") or {}
        identity.router_model = static.get("device_name") or None
        identity.router_firmware = static.get("fw_version") or None
        # Prefer the colon-formatted WAN MAC from ports[] (matches existing TOS
        # mac_address values); fall back to mnfinfo.macEth (hex, no colons).
        identity.router_mac = _extract_wan_mac(dev) or _format_mac(mnf.get("macEth"))
        # Teltonika is the manufacturer for all RUT* devices.
        if identity.router_model or identity.router_serial:
            identity.router_manufacturer = "Teltonika"

    # --- SIM / mobile: /api/modems/status --------------------------------
    modems = _get("/api/modems/status")
    if isinstance(modems, list) and modems and isinstance(modems[0], dict):
        m = modems[0]
        identity.sim_iccid = m.get("iccid") or None
        identity.provider = m.get("operator") or m.get("oper") or None
        identity.modem_subtype = _conntype_to_subtype(m.get("conntype"))
        identity.imsi = m.get("imsi") or None
        identity.imei = m.get("imei") or None

    # --- IPs: /api/interfaces/status (fetched once) ----------------------
    # Mobile WAN IP → SIM; router LAN/management IP → modem. Kept distinct so
    # each lands on its correct entity (no double-entry).
    ifaces = _get("/api/interfaces/status")
    identity.sim_ip_address = _wan_ip(ifaces)
    identity.router_lan_ip = _lan_ip(ifaces)

    return identity


def send_sms(
    host: str,
    to_number: str,
    message: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify_tls: bool = False,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Send one SMS *from* the router's SIM via the RutOS REST API.

    Used by the MSISDN-discovery flow: the field router texts a known catcher
    number, whose received-message sender header then reveals this SIM's own
    number (a SIM cannot read its own MSISDN locally).

    **Outward-facing, costs a message.** Dry-run by default — returns the
    planned request without sending. The CLI only flips ``dry_run=False`` under
    an explicit ``--discover-phone`` + ``--no-dry-run`` from the operator.

    Uses ``POST /api/messages/actions/send`` with body
    ``{"data": {"number": <to>, "message": <text>, "modem": "1-1"}}`` (the
    RutOS 7.x messages endpoint; ``modem`` defaults to the primary modem id).

    Args:
        host: Router IP/hostname (the SENDING router — the field unit).
        to_number: Destination (the catcher / operator mobile).
        message: SMS body.
        username/password/cfg_path/timeout/verify_tls: As :func:`probe_teltonika`.
        dry_run: When True (default), do not send — return the planned payload.

    Returns:
        ``{"dry_run": bool, "to": ..., "endpoint": ..., "sent": bool,
           "response": <api-response-or-None>}``.

    Raises:
        ProbeCredentialsError / ProbeAuthError / ProbeUnreachableError as login.
        ProbeError: When the send is attempted but the API rejects it.
    """
    endpoint = "/api/messages/actions/send"
    payload = {"data": {"number": to_number, "message": message, "modem": "1-1"}}

    if dry_run:
        return {
            "dry_run": True,
            "to": to_number,
            "endpoint": endpoint,
            "sent": False,
            "response": None,
        }

    base, session, headers = _login(
        host,
        username=username,
        password=password,
        cfg_path=cfg_path,
        timeout=timeout,
        verify_tls=verify_tls,
    )
    try:
        r = session.post(
            f"{base}{endpoint}", headers=headers, json=payload, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        raise ProbeUnreachableError(f"{host}: SMS send request failed: {exc}") from exc
    if r.status_code not in (200, 201):
        raise ProbeError(
            f"{host}: SMS send rejected (HTTP {r.status_code}): {r.text[:200]}"
        )
    try:
        resp_json = r.json()
    except ValueError:
        resp_json = None
    return {
        "dry_run": False,
        "to": to_number,
        "endpoint": endpoint,
        "sent": True,
        "response": resp_json,
    }


def _ssh_run_bounded(client: Any, cmd: str, *, timeout: int) -> Tuple[int, str, str]:
    """Run ``cmd`` over an open paramiko SSH client with a **client-side** timeout.

    Some routers' busybox lacks the ``timeout`` applet (e.g. the RUT240), and
    paramiko's ``recv_exit_status()`` blocks regardless of the channel timeout —
    so a stalled remote command (``gsmctl`` against a flaky modem) would hang
    forever. This polls ``exit_status_ready()`` against a wall-clock deadline and
    closes the channel if the command overruns. Returns ``(rc, out, err)``
    (stripped).

    Raises:
        ProbeError: when the command does not return within ``timeout`` seconds.
    """
    import time

    _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)  # nosec B601
    chan = stdout.channel
    deadline = time.monotonic() + timeout
    while not chan.exit_status_ready():
        if time.monotonic() >= deadline:
            try:
                chan.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass
            raise ProbeError(
                f"SSH command did not return within {timeout}s "
                f"(remote stalled): {cmd[:80]}"
            )
        time.sleep(0.2)
    rc = chan.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    return rc, out, err


def send_sms_ssh(
    host: str,
    to_number: str,
    message: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    ssh_user: str = "root",
    port: int = 22,
    timeout: int = DEFAULT_TIMEOUT,
    send_timeout: int = 30,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Send one SMS from the router's SIM via ``gsmctl`` over SSH.

    The universal counterpart to :func:`send_sms` (REST): ``gsmctl`` is present
    on every Teltonika router — legacy RUT240/uhttpd **and** RutOS 7.x — so this
    works where the REST API is absent or disabled (e.g. VOTT's RUT240, which
    refuses :443). Same MSISDN-discovery purpose: the field router texts a catcher
    number, whose received-message sender header reveals this SIM's own number.

    RutOS syntax: ``gsmctl -S -s "<NUMBER> <TEXT>"`` (number + text in one quoted
    arg). The payload is ``shlex``-quoted to neutralise shell metacharacters, and
    ``to_number`` is validated as a phone number.

    **Outward-facing, costs a message.** Dry-run by default — returns the planned
    command without connecting.

    Args:
        host: Router IP/hostname (the SENDING router).
        to_number: Destination (the catcher / operator mobile, ``+`` and digits).
        message: SMS body.
        username/password/cfg_path: SSH creds — resolved via
            :func:`resolve_credentials` when not given (RutOS SSH logs in as
            ``root`` with the web/admin password).
        ssh_user/port/timeout: SSH connection params.
        dry_run: When True (default), do not send — return the planned command.

    Returns:
        ``{"dry_run": bool, "to": ..., "transport": "ssh", "cmd": ...,
           "sent": bool, "output": ...}``.

    Raises:
        ProbeError / ProbeAuthError / ProbeUnreachableError on bad input,
        missing creds, or SSH/auth/send failure.
    """
    import re
    import shlex

    if not re.fullmatch(r"\+?\d{4,15}", (to_number or "").strip()):
        raise ProbeError(
            f"invalid --to number {to_number!r} — expected digits, optional leading +"
        )

    payload = shlex.quote(f"{to_number.strip()} {message}")
    cmd = f"gsmctl -S -s {payload}"
    if dry_run:
        return {
            "dry_run": True,
            "to": to_number,
            "transport": "ssh",
            "cmd": cmd,
            "sent": False,
            "output": None,
        }

    _user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not pw:
        raise ProbeError(
            f"{host}: no router password — set [teltonika] password / "
            f"password_pass_path in receivers.cfg, or pass --password"
        )

    # Lazy import to avoid a telemetry_probe ↔ conntrack_helper import cycle.
    from .conntrack_helper import _connect

    client = _connect(host, ssh_user=ssh_user, password=pw, port=port, timeout=timeout)
    try:
        rc, out, err = _ssh_run_bounded(client, cmd, timeout=send_timeout)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass
    if rc != 0:
        raise ProbeError(
            f"{host}: gsmctl SMS send failed (rc={rc}): {(err or out)[:200]}"
        )
    return {
        "dry_run": False,
        "to": to_number,
        "transport": "ssh",
        "cmd": cmd,
        "sent": True,
        "output": out,
    }


def query_ussd_ssh(
    host: str,
    code: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    ssh_user: str = "root",
    port: int = 22,
    timeout: int = DEFAULT_TIMEOUT,
    ussd_wait: int = 8,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Run a USSD query via ``gsmctl -U`` over SSH and return the network reply.

    A more reliable own-number probe than the SMS catcher when the modem's SMS
    subsystem is flaky: the SIM asks the network **directly** — no catcher phone,
    no delivery wait, the answer comes back in the USSD reply. The ``code`` is
    operator-specific (e.g. an own-number / MSISDN code) and the reply format
    varies by operator, so this returns the raw network text.

    ``gsmctl -U "<code>"`` sends the USSD and saves the response to
    ``/tmp/ussd_<modem>``; we send, wait ``ussd_wait`` s for the network round
    trip, then read that file — one bounded SSH command (busybox ``sleep`` +
    ``cat``). Outward-facing (a network query); dry-run by default.

    Args:
        host: Router IP/hostname.
        code: USSD string (digits, ``*``, ``#``), e.g. an own-number code.
        username/password/cfg_path/ssh_user/port/timeout: As :func:`send_sms_ssh`.
        ussd_wait: Seconds to wait for the network reply before reading the file.
        dry_run: When True (default), return the planned command without running.

    Returns:
        ``{"dry_run": bool, "code": ..., "transport": "ssh", "cmd": ...,
           "response": <raw text or None>}``.
    """
    import re
    import shlex

    if not re.fullmatch(r"[\d*#+]{2,20}", (code or "").strip()):
        raise ProbeError(f"invalid --ussd code {code!r} — expected digits, '*' and '#'")

    qcode = shlex.quote(code.strip())
    # send the USSD, give the network a moment, then read the saved reply
    cmd = f"gsmctl -U {qcode}; sleep {ussd_wait}; cat /tmp/ussd_* 2>/dev/null"
    if dry_run:
        return {
            "dry_run": True,
            "code": code,
            "transport": "ssh",
            "cmd": cmd,
            "response": None,
        }

    _user, pw = resolve_credentials(
        username=username, password=password, cfg_path=cfg_path
    )
    if not pw:
        raise ProbeError(
            f"{host}: no router password — set [teltonika] password / "
            f"password_pass_path in receivers.cfg, or pass --password"
        )

    from .conntrack_helper import _connect

    client = _connect(host, ssh_user=ssh_user, password=pw, port=port, timeout=timeout)
    try:
        # rc is not load-bearing — gsmctl -U may return non-zero yet still file a
        # reply; the saved /tmp/ussd_* text is what matters.
        _rc, out, err = _ssh_run_bounded(client, cmd, timeout=ussd_wait + 15)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass
    return {
        "dry_run": False,
        "code": code,
        "transport": "ssh",
        "cmd": cmd,
        "response": (out or err or None),
    }


# ---------------------------------------------------------------------------
# RutOS port-forwards (DNAT) — so the WAN-side scheduler can reach the
# receiver's control/FTP/HTTP ports on the router's LAN.
# ---------------------------------------------------------------------------
#
# Endpoint shapes verified live against a RUT241 (fw 00.07.22, 2026-06-07):
#   GET  /api/firewall/port_forwards/config   → {"data": [ {rule}, ... ]}
#   POST /api/firewall/port_forwards/config   → create a rule (body {"data": {...}})
#   POST /api/firewall/port_forwards/changes  → apply staged config (uci commit
#        + reload); RutOS 7.x needs this or the rule is staged but not live.
# A rule mirrors the existing fleet shape, e.g. GPS_http:
#   {"enabled":"1","proto":["tcp"],"src":"wan","src_dport":"28784",
#    "dest":"lan","dest_ip":"192.168.100.60","dest_port":"28784",
#    "name":"GPS_control",".type":"redirect"}


def list_port_forwards(
    host: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify_tls: bool = False,
) -> List[Dict[str, Any]]:
    """Return the router's current port-forward (DNAT redirect) rules.

    Read-only. Raises the same Probe* errors as :func:`probe_teltonika` on
    auth/connectivity failure.
    """
    base, session, headers = _login(
        host,
        username=username,
        password=password,
        cfg_path=cfg_path,
        timeout=timeout,
        verify_tls=verify_tls,
    )
    try:
        r = session.get(
            f"{base}/api/firewall/port_forwards/config",
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise ProbeUnreachableError(f"{host}: port_forwards GET failed: {exc}") from exc
    if r.status_code != 200:
        raise ProbeUnreachableError(f"{host}: port_forwards GET → HTTP {r.status_code}")
    data = r.json().get("data")
    return data if isinstance(data, list) else []


def ensure_port_forwards(
    host: str,
    dest_ip: str,
    wanted: List[Dict[str, Any]],
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    cfg_path: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    verify_tls: bool = False,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Idempotently ensure each wanted DNAT forward exists; apply if changed.

    ``wanted`` is a list of ``{"name","src_dport","dest_port","proto"}`` dicts
    (``proto`` defaults to ``["tcp"]``). A forward is considered already-present
    when an existing rule has the same ``src_dport`` AND ``dest_ip`` — so this
    is safe to re-run. Missing ones are POSTed, then a single apply commits.

    **Outward-facing, mutates the router firewall.** Dry-run by default: returns
    the planned creates without sending. Additive only (never deletes/edits
    existing rules), and never touches conntrack/raw-iptables — so it cannot
    sever the management path.

    Returns ``{"dry_run", "existing":[names], "created":[names],
    "skipped":[names], "applied":bool}``.
    """
    existing = list_port_forwards(
        host,
        username=username,
        password=password,
        cfg_path=cfg_path,
        timeout=timeout,
        verify_tls=verify_tls,
    )

    def _present(src_dport: str) -> bool:
        return any(
            str(r.get("src_dport")) == str(src_dport)
            and str(r.get("dest_ip")) == str(dest_ip)
            for r in existing
        )

    to_create = [w for w in wanted if not _present(w["src_dport"])]
    skipped = [w["name"] for w in wanted if _present(w["src_dport"])]

    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "dest_ip": dest_ip,
        "existing": [r.get("name") for r in existing],
        "created": [],
        "skipped": skipped,
        "applied": False,
    }

    if dry_run or not to_create:
        result["would_create"] = [
            {
                "name": w["name"],
                "src_dport": w["src_dport"],
                "dest_ip": dest_ip,
                "dest_port": w.get("dest_port", w["src_dport"]),
                "proto": w.get("proto", ["tcp"]),
            }
            for w in to_create
        ]
        return result

    # Live: re-login (the list call's session/token is fine to reuse, but a
    # fresh write session avoids any TTL edge during a multi-POST batch).
    base, session, headers = _login(
        host,
        username=username,
        password=password,
        cfg_path=cfg_path,
        timeout=timeout,
        verify_tls=verify_tls,
    )
    for w in to_create:
        body = {
            "data": {
                "name": w["name"],
                "enabled": "1",
                ".type": "redirect",
                "src": "wan",
                "dest": "lan",
                "src_dport": str(w["src_dport"]),
                "dest_ip": dest_ip,
                "dest_port": str(w.get("dest_port", w["src_dport"])),
                "proto": w.get("proto", ["tcp"]),
            }
        }
        try:
            r = session.post(
                f"{base}/api/firewall/port_forwards/config",
                headers=headers,
                json=body,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProbeUnreachableError(
                f"{host}: create forward {w['name']!r} failed: {exc}"
            ) from exc
        if r.status_code not in (200, 201):
            raise ProbeError(
                f"{host}: create forward {w['name']!r} → HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        result["created"].append(w["name"])

    # Apply (uci commit + reload) so the staged rules go live. The config POSTs
    # above already succeeded — the rules are written. RutOS auto-applies new
    # redirect rules, and the explicit apply endpoint is not always permitted for
    # an admin API token (observed: 501 on /port_forwards/changes, 403 on
    # /firewall/changes). Treat a non-2xx apply as a soft condition: record it,
    # don't raise — the rules were committed regardless. Only a transport-level
    # failure (router unreachable mid-batch) is a hard error.
    try:
        ra = session.post(
            f"{base}/api/firewall/port_forwards/changes",
            headers=headers,
            json={"data": {}},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise ProbeUnreachableError(f"{host}: apply (changes) failed: {exc}") from exc
    if ra.status_code in (200, 201):
        result["applied"] = True
    else:
        result["applied"] = False
        result["apply_status"] = ra.status_code
        result["apply_note"] = (
            f"apply endpoint returned HTTP {ra.status_code}; rules were written "
            f"to config and RutOS auto-applies redirect rules — expected live. "
            f"Verify with a reachability check on the forwarded port."
        )
    return result


def _format_mac(hex_mac: Optional[str]) -> Optional[str]:
    """Format a colon-less hex MAC (``2097270A7727``) as ``20:97:27:0A:77:27``."""
    if not hex_mac:
        return None
    h = hex_mac.strip().replace(":", "")
    if len(h) != 12:
        return hex_mac  # unexpected shape — return as-is rather than mangle
    return ":".join(h[i : i + 2] for i in range(0, 12, 2)).upper()


def _extract_wan_mac(device_status: Dict[str, Any]) -> Optional[str]:
    """Return the WAN port's colon-formatted MAC from device/status ports[]."""
    for port in device_status.get("ports") or []:
        if isinstance(port, dict) and (port.get("name") or "").upper() == "WAN":
            mac = port.get("mac")
            if mac:
                return mac.upper()
    return None
