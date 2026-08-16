"""Single source of truth for which Trimble converter to use.

This exists because two implementations drifted apart and cost real data.
``async_converter._select_converter`` (live downloads) preferred the native
Docker converter, while ``RINEXTask._get_converter`` (backfill) hardcoded
``TrimbleConverter``. Only the live path was kept current, so every Trimble
BACKFILL conversion ran runpkr00 and failed with exit 30 — these .T02 carry a
bzip2-compressed payload runpkr00 cannot decode (it writes a 26-byte stub .dat).
Measured on rek-d01 2026-08-16: 566 distinct files, 1,132 failed spawns per 3 h.

Because the backfill cursor advances unconditionally — a failed date is passed
over, counted in ``files_missing``, and the row still ends ``completed`` — those
dates were then only reachable through gap_detection's 7-day re-enqueue. Trimble
dates older than a week that the cursor had already crossed were effectively
abandoned. A silent divergence between two copies of one decision, not a bug in
either copy.

Both paths now call :func:`resolve_trimble_converter`. Adding a third caller
should mean calling this, never re-deriving the rule.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Receiver types the native (Docker/Wine) converter handles. It is the only path
# that produces RINEX 3 for these, and the only one that reads a compressed .T02.
NATIVE_TRIMBLE_TYPES = ("netr9", "netrs", "netr5")


def wants_native_trimble(receiver_type: str) -> bool:
    """Is this a receiver type the native converter should handle?

    Substring match, because callers pass anything from ``"NetR9"`` to
    ``"trimble netr9"``. Case is normalised here so callers cannot get it wrong
    — the live path matched on a lowercased string while the DB stores
    ``"NetR9"``, which is the kind of mismatch that hides for months.
    """
    rt = (receiver_type or "").lower()
    return any(t in rt for t in NATIVE_TRIMBLE_TYPES)


def resolve_trimble_converter(
    fallback: Any,
    *,
    log: Optional[logging.Logger] = None,
    receiver_type: Optional[str] = None,
    rinex_config: Optional[dict] = None,
) -> Any:
    """Return the native Trimble converter class, or ``fallback``.

    ``fallback`` is normally ``TrimbleConverter`` (runpkr00 + teqc). It is
    returned when the native converter is switched off in config, when its
    Docker image is unavailable, or on any error — a Docker outage must degrade
    to the old path, never fail the conversion outright.

    ``receiver_type`` is optional: when given, a non-Trimble type short-circuits
    to the fallback so callers can pass it unconditionally.

    ``rinex_config`` is the caller's ALREADY-RESOLVED rinex settings. Pass it
    whenever you have one — a CLI run can override ``use_native_trimble`` for a
    single invocation (``receivers rinex --native-trimble``), and re-reading the
    global config here would silently discard that override. Only when it is
    omitted do we fall back to reading config ourselves.
    """
    log = log or logger

    if receiver_type is not None and not wants_native_trimble(receiver_type):
        return fallback

    try:
        if rinex_config is None:
            from ..config.receivers_config import get_receivers_config

            rinex_config = get_receivers_config().get_rinex_config()

        if not rinex_config.get("use_native_trimble", False):
            return fallback

        from .trimble_native_converter import TrimbleNativeConverter

        if TrimbleNativeConverter.is_available():
            return TrimbleNativeConverter

        log.warning(
            "use_native_trimble is set but the Docker image is unavailable — "
            "falling back to runpkr00, which cannot read a compressed .T02 "
            "(exit 30). Check `docker image inspect trm2rinex:cli-light`."
        )
    except Exception as exc:  # noqa: BLE001 - selection must never raise
        log.debug(f"native Trimble converter unavailable: {exc}")

    return fallback
