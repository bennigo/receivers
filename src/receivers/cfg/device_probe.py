"""Receiver-identity probe layer for ``receivers cfg add-receiver``.

Wraps the per-protocol extractor classes in :mod:`receivers.health` so the CLI
gets a single ``probe_receiver(...)`` entry point that returns a uniform
:class:`ReceiverIdentity`. Keeps the protocol details (TCP control port vs
HTTP /prog/show vs BarracudaServer) out of the CLI handler.

Priority is PolaRX5 because the IMO GNSS network is migrating onto it and SBF
block 5902 is the firmware-authoritative identity source. Trimble and Leica
remain supported via explicit ``--probe-type`` so this command can register
any deployed receiver, but ``auto`` only attempts PolaRX5 — the other
protocols need a model hint (NetR9 vs NetRS vs NetR5) or operator-supplied
overrides (G10 exposes no identity endpoint).

Extending to seismic / non-GPS instruments
------------------------------------------
Add a new strategy callable matching the ``ProbeStrategy`` protocol, register
it in :data:`PROBE_STRATEGIES`, and broaden the CLI ``--probe-type`` choices.
The dispatcher + override-merging logic in :func:`probe_receiver` and
:func:`to_subtype_attrs` is domain-agnostic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ReceiverIdentity:
    """Probed identity for a single device.

    ``subtype`` is the TOS entity subtype the device will be created as
    (always ``gnss_receiver`` today; the field exists so the same probe
    surface can be reused by future ``add-antenna`` / ``add-radome`` work).
    ``model_raw`` is the unnormalised vendor string — pass it through
    :func:`tostools.device.validate_model` before writing to TOS.
    ``partial`` is set when the probe could not fill every required field
    on its own (e.g. G10) and the caller must supply overrides via CLI.
    """

    subtype: str
    probe_type: str
    serial: Optional[str] = None
    model_raw: Optional[str] = None
    firmware_version: Optional[str] = None
    marker_name: Optional[str] = None
    partial: bool = False
    #: TOS constellation codes the receiver is demonstrably using (``{"GPS",
    #: "GLO", "GAL", "BDS"}``). Positive evidence only — see
    #: :func:`constellations_from_satellites` for why an empty entry is never
    #: proof a system is disabled. Empty when the probe could not read them.
    constellations: FrozenSet[str] = frozenset()


class ProbeError(Exception):
    """Base class for probe failures."""


class ProbeUnreachableError(ProbeError):
    """Network / socket failure — receiver did not respond at all."""


class ProbeNotIdentifiedError(ProbeError):
    """Receiver responded but its type could not be determined automatically."""


class ProbeIncompleteError(ProbeError):
    """Probe ran but did not return every required identity field.

    Raised by :func:`to_subtype_attrs` after overrides are applied; the
    caller (CLI) translates this into an actionable user-facing message
    listing the missing flags.
    """


ProbeStrategy = Callable[..., ReceiverIdentity]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_host_port(probe_arg: str) -> Tuple[str, Optional[int]]:
    """Split ``host[:port]`` from ``--probe``.

    Returns ``(host, port)``; ``port`` is ``None`` when the caller omitted it
    (each strategy then applies its own default — 28784 for PolaRX5, 8060 for
    Trimble, etc.). Empty input raises ``ValueError``.
    """
    if not probe_arg:
        raise ValueError("--probe must be a non-empty host[:port] string")
    if ":" not in probe_arg:
        return probe_arg, None
    host, _, port_str = probe_arg.rpartition(":")
    if not host or not port_str:
        raise ValueError(
            f"--probe must be host[:port], got {probe_arg!r} (strip any scheme/path)"
        )
    try:
        return host, int(port_str)
    except ValueError as e:
        raise ValueError(f"--probe port must be an integer, got {port_str!r}") from e


class StationProbeResolveError(ValueError):
    """Raised when a station ID can't be resolved to a probe target."""


def resolve_station_probe(station_id: str, *, router_only: bool = False) -> str:
    """Resolve a deployed station's probe target (``host[:port]``) from stations.cfg.

    Lets the probe-by-IP cfg verbs (``update-device``, ``add-receiver``,
    ``replace-modem``, ``replace-sim``) address a station by its 4-char ID
    instead of a raw IP. Crucially this disambiguates **port-forwarded stations
    that share a router IP** — e.g. JONC/KASC/OLAC/TANC all on ``10.4.1.43``,
    each reachable on its own ``receiver_controlport``.

    - receiver probes (default): returns ``router_ip:receiver_controlport``.
    - router probes (``router_only=True``, telemetry/RutOS): returns just
      ``router_ip`` — the RutOS REST API uses its own port and credentials.

    Raises ``StationProbeResolveError`` if the station is unknown or has no
    ``router_ip`` (e.g. a passive station with no direct connection).
    """
    from ..config_utils import get_station_config

    cfg = get_station_config(station_id, silent=True)
    if not cfg:
        raise StationProbeResolveError(
            f"station {station_id!r} not found in stations.cfg (or has no direct "
            "connection) — pass --probe HOST[:PORT] instead"
        )
    router = cfg.get("router") or {}
    host = router.get("ip") or cfg.get("router_ip") or cfg.get("ip_address")
    if not host:
        raise StationProbeResolveError(
            f"no router_ip for {station_id!r} in stations.cfg — pass --probe instead"
        )
    if router_only:
        return str(host)
    receiver = cfg.get("receiver") or {}
    port = receiver.get("controlport") or cfg.get("receiver_controlport") or "28784"
    return f"{host}:{port}"


# ---------------------------------------------------------------------------
# Per-protocol strategies
# ---------------------------------------------------------------------------


#: Health-extractor constellation labels → TOS ``gnss_receiver`` attribute codes.
#: TOS side is :data:`tostools.constellation.TOS_CONSTELLATION_CODES`; the
#: extractor reports vendor-style names, so the two have to be bridged here.
#: Keys are matched case-insensitively.
CONSTELLATION_LABEL_TO_TOS: Dict[str, str] = {
    "gps": "GPS",
    "glonass": "GLO",
    "glo": "GLO",
    "galileo": "GAL",
    "gal": "GAL",
    "beidou": "BDS",
    "bds": "BDS",
    "compass": "BDS",  # pre-2012 name, still seen in some firmware strings
    "qzss": "QZSS",
    "sbas": "SBAS",
    "irnss": "IRN",
    "navic": "IRN",  # IRNSS was renamed NavIC in 2016
}


def constellations_from_satellites(
    satellites: Optional[Dict[str, Any]],
) -> FrozenSet[str]:
    """Map the probe's per-constellation satellite counts to TOS codes.

    ``satellites`` is the extractor's ``metrics.satellites`` payload, whose
    ``by_constellation`` counts come from **PVTSatCartesian (SBF 4008)** —
    satellites actually used in the PVT solution. Deliberately *not*
    ChannelStatus (4013), which lists allocated channels for disabled
    constellations too and would over-report (see the note at
    ``polarx5_tcp_extractor._query_satellite_tracking``).

    Returns only systems with a **non-zero** count, i.e. positive evidence.

    An absent system is NOT evidence it is disabled, for two reasons: the
    receiver may simply have had no satellite of that system in view at the
    instant of the probe, and QZSS / IRN are regional systems never visible
    from Iceland even when fully enabled. Callers must therefore write ``true``
    for what is present and leave the rest unset — never ``false``.
    """
    if not isinstance(satellites, dict):
        return frozenset()
    by_system = satellites.get("by_constellation")
    if not isinstance(by_system, dict):
        return frozenset()

    # Truncation guard. The per-system counts come from walking the SatInfo
    # sub-blocks, but `total` is read straight from the block header — so if the
    # SBF block arrived short, `total` stays right while the walk stops early
    # and quietly loses the tail. `_find_sbf_block` returns on block-ID match
    # without waiting for its declared `length`
    # (polarx5_tcp_extractor.py:1210), so this happens routinely.
    #
    # It is not random which system is lost: SatInfo runs in SVID order and
    # BeiDou holds the highest SVIDs (201-263), so BeiDou is always first over
    # the cliff. Measured on NPSK 2026-08-08 — 5 consecutive probes, 4 summed to
    # 27 against `total 41` and reported no BeiDou; the one that summed to 40
    # found 11 BeiDou satellites.
    #
    # The systems we DID see are still real (positive evidence survives a short
    # read), so return them — but flag the set as possibly incomplete so the
    # caller can say so rather than presenting a partial answer as the answer.
    counted = 0
    for value in by_system.values():
        try:
            counted += max(int(value), 0)
        except (TypeError, ValueError):
            continue
    try:
        declared = int(satellites.get("total") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared and counted < declared:
        logger.warning(
            "constellation counts look truncated: %d satellites across %s but "
            "the block header declares %d — the set may be missing a system "
            "(BeiDou first). See polarx5_tcp_extractor._find_sbf_block.",
            counted,
            sorted(by_system),
            declared,
        )

    codes = set()
    for label, count in by_system.items():
        try:
            if int(count) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        code = CONSTELLATION_LABEL_TO_TOS.get(str(label).strip().lower())
        if code:
            codes.add(code)
        else:
            logger.warning(
                "unmapped constellation label %r from probe — add it to "
                "CONSTELLATION_LABEL_TO_TOS",
                label,
            )
    return frozenset(codes)


def _probe_polarx5(
    host: str,
    port: Optional[int],
    *,
    station_id_hint: Optional[str] = None,
    timeout: float = 10.0,
    tcp_username: Optional[str] = None,
    tcp_password: Optional[str] = None,
) -> ReceiverIdentity:
    """Probe SBF block 5902 over the PolaRX5 TCP control port.

    Delegates port selection (28784 plaintext → 28783 TLS fallback) to
    :class:`PolaRX5TCPExtractor`. ``station_id_hint`` only feeds the
    extractor's logger — pass ``"BENCH"`` for a fresh receiver that
    isn't in stations.cfg yet.

    ``tcp_username`` and ``tcp_password`` override the fleet defaults
    from receivers.cfg ``[polarx5]`` when given. Use for bench receivers
    that have non-default credentials (e.g. fresh-out-of-box on TEST
    creds, or just-upgraded firmware where the recorded fleet password
    doesn't yet match what the receiver expects).
    """
    from ..health.polarx5_tcp_extractor import PolaRX5TCPExtractor

    extractor = PolaRX5TCPExtractor(
        host=host,
        station_id=station_id_hint or "BENCH",
        port=port or PolaRX5TCPExtractor.CONTROL_PORT,
        timeout=timeout,
    )
    if tcp_username:
        extractor.tcp_username = tcp_username
    if tcp_password:
        extractor.tcp_password = tcp_password
    try:
        setup = extractor._query_receiver_setup()
    except OSError as e:
        raise ProbeUnreachableError(
            f"PolaRX5 control port unreachable at {host}:{port or 28784}: {e}"
        ) from e

    if not setup:
        # Reached the host but the SBF block didn't come back — either the
        # receiver isn't a PolaRX5 or auth is required and unconfigured.
        raise ProbeNotIdentifiedError(
            f"PolaRX5 probe reached {host} but returned no ReceiverSetup; "
            "host may not be a PolaRX5 or requires TCP credentials"
        )

    # Constellations ride the same unauthenticated SBF channel as the identity
    # block, so this is one extra request and no credentials. Best-effort: a
    # receiver that answers 5902 but not 4008 still yields a usable identity —
    # constellations are an optional TOS attribute, serial/model are not.
    constellations: FrozenSet[str] = frozenset()
    try:
        constellations = constellations_from_satellites(
            extractor._query_satellite_tracking()
        )
    except Exception as e:  # noqa: BLE001 — never fail intake over an optional attr
        logger.debug("PolaRX5 constellation probe failed at %s: %s", host, e)

    return ReceiverIdentity(
        subtype="gnss_receiver",
        probe_type="polarx5",
        serial=setup.get("serial_number"),
        model_raw=setup.get("receiver_model"),
        firmware_version=setup.get("firmware_version"),
        marker_name=setup.get("marker_name"),
        partial=False,
        constellations=constellations,
    )


def _probe_trimble(
    host: str,
    port: Optional[int],
    *,
    receiver_type: str,
    station_id_hint: Optional[str] = None,
    timeout: float = 10.0,
) -> ReceiverIdentity:
    """Probe a Trimble NetR9 / NetRS / NetR5 via HTTP ``/prog/show``.

    ``receiver_type`` selects which Trimble family the extractor announces
    to the receiver — NetR5 has fewer endpoints than NetR9/NetRS, so the
    extractor degrades gracefully. Model is taken from ``receiver_type``;
    serial/firmware come from the HTTP probe.
    """
    from ..health.trimble_http_extractor import TrimbleHTTPExtractor

    extractor = TrimbleHTTPExtractor(
        host=host,
        station_id=station_id_hint or "BENCH",
        port=port or 8060,
        receiver_type=receiver_type,
        timeout=int(timeout),
    )
    try:
        info = extractor._fetch_system_info()
    except OSError as e:
        raise ProbeUnreachableError(
            f"Trimble HTTP unreachable at {host}:{port or 8060}: {e}"
        ) from e

    info = info or {}
    serial = info.get("serial_number")
    firmware = info.get("firmware_version")  # only NetR9/NetRS expose this
    partial = not (serial and firmware)
    return ReceiverIdentity(
        subtype="gnss_receiver",
        probe_type=receiver_type.lower(),
        serial=serial,
        model_raw=receiver_type,
        firmware_version=firmware,
        marker_name=info.get("station_name"),
        partial=partial,
    )


def _probe_g10(
    host: str,
    port: Optional[int],
    *,
    station_id_hint: Optional[str] = None,
    timeout: float = 10.0,
) -> ReceiverIdentity:
    """Reachability check for a Leica G10 — identity must come from CLI overrides.

    The G10 firmware does not expose a serial / model / firmware endpoint that
    is safe to scrape, so this strategy verifies the receiver is reachable on
    its HTTP port and returns a ``partial`` identity. The caller is expected
    to supply ``--serial`` and ``--model`` (and optionally ``--firmware``).
    """
    from ..health.g10_http_extractor import G10HTTPExtractor

    try:
        G10HTTPExtractor(
            host=host,
            station_id=station_id_hint or "BENCH",
            port=port or 8060,
            timeout=int(timeout),
        )
    except OSError as e:
        raise ProbeUnreachableError(
            f"G10 HTTP unreachable at {host}:{port or 8060}: {e}"
        ) from e

    # No identity pre-fill: the IGS lookup table keys are aliases (e.g. "GR10"),
    # not canonical values (e.g. "LEICA GR10"). Pre-filling the canonical name
    # would fail validate_model. The operator must supply --model explicitly.
    return ReceiverIdentity(
        subtype="gnss_receiver",
        probe_type="g10",
        serial=None,
        model_raw=None,
        firmware_version=None,
        marker_name=None,
        partial=True,
    )


PROBE_STRATEGIES: Dict[str, ProbeStrategy] = {
    "polarx5": _probe_polarx5,
    "netr9": lambda h, p, **kw: _probe_trimble(h, p, receiver_type="NetR9", **kw),
    "netrs": lambda h, p, **kw: _probe_trimble(h, p, receiver_type="NetRS", **kw),
    "netr5": lambda h, p, **kw: _probe_trimble(h, p, receiver_type="NetR5", **kw),
    "g10": _probe_g10,
}

PROBE_TYPE_CHOICES: Tuple[str, ...] = ("auto",) + tuple(PROBE_STRATEGIES)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def probe_receiver(
    host: str,
    port: Optional[int],
    *,
    probe_type: str = "auto",
    station_id_hint: Optional[str] = None,
    timeout: float = 10.0,
    tcp_username: Optional[str] = None,
    tcp_password: Optional[str] = None,
) -> ReceiverIdentity:
    """Return a :class:`ReceiverIdentity` for the receiver at ``host:port``.

    ``probe_type="auto"`` tries PolaRX5 only — other protocols need extra
    information that ``auto`` can't infer (Trimble model family; G10
    operator-supplied serial/model). For those, pass ``--probe-type``
    explicitly.

    ``tcp_username``/``tcp_password``: PolaRX5-only — override receivers.cfg
    ``[polarx5]`` fleet defaults. Ignored by Trimble / G10 probe strategies.
    """
    if probe_type == "auto":
        try:
            return _probe_polarx5(
                host,
                port,
                station_id_hint=station_id_hint,
                timeout=timeout,
                tcp_username=tcp_username,
                tcp_password=tcp_password,
            )
        except ProbeUnreachableError as e:
            raise ProbeUnreachableError(
                f"{e}. Auto-probe only attempts PolaRX5; pass --probe-type "
                "{netr9,netrs,netr5,g10} for non-Septentrio receivers."
            ) from e
        except ProbeNotIdentifiedError as e:
            raise ProbeNotIdentifiedError(
                f"{e}. Pass --probe-type explicitly for non-PolaRX5 receivers."
            ) from e

    if probe_type not in PROBE_STRATEGIES:
        raise ValueError(
            f"Unknown --probe-type {probe_type!r}. "
            f"Valid choices: {', '.join(PROBE_TYPE_CHOICES)}"
        )
    if probe_type == "polarx5":
        return _probe_polarx5(
            host,
            port,
            station_id_hint=station_id_hint,
            timeout=timeout,
            tcp_username=tcp_username,
            tcp_password=tcp_password,
        )
    return PROBE_STRATEGIES[probe_type](
        host, port, station_id_hint=station_id_hint, timeout=timeout
    )


# ---------------------------------------------------------------------------
# Identity → tos.device input shape
# ---------------------------------------------------------------------------


def to_subtype_attrs(
    identity: ReceiverIdentity,
    *,
    serial_override: Optional[str] = None,
    model_override: Optional[str] = None,
    firmware_override: Optional[str] = None,
) -> Dict[str, str]:
    """Merge probe identity with CLI overrides and validate completeness.

    Override values always win — the operator may know something the probe
    can't see (e.g. a serial sticker on a G10) and warehouse intake is the
    right moment to record it. The returned dict has the exact keys the
    CLI feeds into :func:`tostools.device.build_required_attributes`.

    Raises :class:`ProbeIncompleteError` listing the missing fields so the CLI
    can echo "pass --serial / --model / ..." to the user.
    """
    serial = serial_override or identity.serial
    model = model_override or identity.model_raw
    firmware = firmware_override or identity.firmware_version

    missing = [
        name
        for name, value in (
            ("serial", serial),
            ("model", model),
        )
        if not value
    ]
    if missing:
        flags = ", ".join(f"--{m}" for m in missing)
        raise ProbeIncompleteError(
            f"Probe did not supply {', '.join(missing)} "
            f"for probe_type={identity.probe_type!r}; "
            f"re-run with {flags} explicitly."
        )

    out: Dict[str, str] = {
        "subtype": identity.subtype,
        "serial": serial,  # type: ignore[dict-item]
        "model_raw": model,  # type: ignore[dict-item]
    }
    if firmware:
        out["firmware_version"] = firmware
    return out
