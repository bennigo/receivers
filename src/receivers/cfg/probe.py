"""Per-station probes — pure I/O, no prompts, no side effects.

Extracted from ``cli/cfg.py``. These four functions are what a caller needs to
ask "what does the receiver say, and what does TOS say" about a station, and
none of them decides anything: they return data or ``None``.

That matters because the CLI is not meant to be the only caller. The planned
rek_new web UI needs exactly this — the ability to probe a station without a
terminal — and previously had to import a 9,000-line command module to get it.
``_probe_station`` is documented thread-safe and is already driven from a
thread pool by ``cfg reconcile --all``.

``cli.cfg`` re-exports all four, so existing imports keep working.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _silent(message: str, *, json_mode: bool = False, **kwargs) -> None:
    """Default progress sink: say nothing.

    This module is pure I/O and must not print — a web backend or a thread pool
    has no business writing to stdout. The CLI injects its own ``_progress``
    (which routes to stderr in JSON mode so it cannot corrupt the document),
    so terminal output is unchanged where it matters.
    """


def _query_receiver_identity(
    station_id: str, station_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Run a receiver health probe and return the identity dict, or None.

    The probe skips file/NTRIP checks since we only need identity. Returns
    ``None`` on any failure (unreachable, auth error, parse failure).
    """
    try:
        from ..base.receiver_factory import create_receiver

        receiver = create_receiver(station_id, station_config)
        # Identity-only path: bypass NTRIP/file checks. Most extractors
        # populate receiver_identity inside get_health_status() itself.
        health = receiver.get_health_status()
        if not isinstance(health, dict):
            return None

        identity = dict(health.get("receiver_identity") or {})

        # Enrich identity with position from PVT solution — receiver coordinates
        # are reconcilable QC values, but the extractor stores them under
        # metrics.position rather than receiver_identity. Promote them here so
        # the cfg reconcile field manifest can read everything from one dict.
        position = (health.get("metrics") or {}).get("position") or {}
        for key in ("latitude", "longitude", "height"):
            val = position.get(key)
            if val is not None:
                identity[key] = val

        # Antenna metadata (type/serial/radome/height delta) is only useful
        # for cfg reconcile, so the extractor doesn't probe it during routine
        # 5-min health checks. Run the dedicated ASCII probe here. Best-effort:
        # failure leaves the antenna fields blank in the diff, which the
        # reconciler renders as NO_DATA.
        antenna_info = _query_antenna_info(station_id, station_config)
        if antenna_info:
            identity.update(antenna_info)

        if not identity:
            logger.debug("[%s] receiver returned no identity dict", station_id)
            return None
        return identity
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] receiver probe failed: %s", station_id, exc)
        return None


def _query_antenna_info(
    station_id: str, station_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Probe antenna metadata via the PolaRX5 ASCII control channel.

    Currently only PolaRX5 exposes antenna config over the control port. For
    other receiver types this is a no-op — the cfg reconcile flow falls back
    to TOS as the only authoritative source, which is correct.
    """
    try:
        receiver_type = (station_config.get("receiver_type") or "").lower()
        if "polarx" not in receiver_type:
            return None
        from ..health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        host = (
            station_config.get("router_ip")
            or station_config.get("ip_number")
            or (station_config.get("router") or {}).get("ip")
        )
        if not host:
            return None
        control_port = int(
            station_config.get("receiver_controlport")
            or station_config.get("control_port")
            or (station_config.get("receiver") or {}).get("controlport")
            or 28784
        )
        extractor = PolaRX5TCPExtractor(host, station_id, port=control_port)
        return extractor.query_antenna_info()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] antenna probe failed: %s", station_id, exc)
        return None


def _query_tos(station_id: str) -> Optional[Dict[str, Any]]:
    try:
        from tostools.api.tos_client import TOSClient
    except ImportError:
        return None
    try:
        client = TOSClient()
        data = client.get_complete_station_metadata(station_id)
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] TOS query failed: %s", station_id, exc)
        return None


# ---------------------------------------------------------------------------
# Per-station probe (parallelisable I/O — no side effects, no prompts)
# ---------------------------------------------------------------------------


def _probe_station(
    station_id: str,
    station_config: Dict[str, Any],
    sources: List[str],
    json_mode: bool,
    verbose: bool = True,
    progress: Optional[Callable[..., None]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Query receiver and TOS for one station.

    Pure I/O — creates its own network connections, returns data only.
    Safe to call from a thread.  When ``verbose=False`` (parallel mode)
    per-station progress lines are suppressed so interleaved output doesn't
    corrupt the terminal.

    Returns ``(receiver_identity, tos_data)``.
    """
    progress = progress if progress is not None else _silent
    receiver_identity: Optional[Dict[str, Any]] = None
    tos_data: Optional[Dict[str, Any]] = None

    if "receiver" in sources:
        if station_config.get("_adhoc"):
            if verbose:
                progress(
                    f"   ↳ {station_id}: ad-hoc config, skipping receiver probe",
                    json_mode=json_mode,
                )
        else:
            if verbose:
                progress(
                    f"   ↳ {station_id}: probing receiver…",
                    json_mode=json_mode,
                    flush=True,
                )
            receiver_identity = _query_receiver_identity(station_id, station_config)
            if receiver_identity is None and verbose:
                progress(
                    f"   ↳ {station_id}: receiver unreachable or no identity",
                    json_mode=json_mode,
                )

    if "tos" in sources:
        if verbose:
            progress(
                f"   ↳ {station_id}: querying TOS…",
                json_mode=json_mode,
                flush=True,
            )
        tos_data = _query_tos(station_id)
        if tos_data is None and verbose:
            progress(
                f"   ↳ {station_id}: not in TOS or TOS unavailable",
                json_mode=json_mode,
            )

    return receiver_identity, tos_data
