"""Three-way comparison of stations.cfg, live receiver, and TOS.

Produces a list of :class:`FieldDiff` records — one per declared field —
that capture the cfg value, the value reported by the live receiver (if
queried), and the value present in TOS (if queried), along with a
:class:`Verdict` describing how the sources agree or disagree.

This is the data layer; the CLI in :mod:`receivers.cli.cfg` owns the
human interaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from .field_manifest import FIELDS, FieldSpec

logger = logging.getLogger(__name__)


class Verdict(StrEnum):
    OK = "ok"  # cfg matches all queried sources
    MISSING = "missing"  # cfg empty, ≥1 source has a value
    CONFLICT = "conflict"  # cfg present, disagrees with at least one source
    SOURCES_DISAGREE = "sources_disagree"  # receiver and TOS disagree
    NO_DATA = "no_data"  # no source could supply a value
    NOT_QUERYABLE = "not_queryable"  # field can't be derived from requested sources


class SourceUnavailableError(Exception):
    """Raised when a requested source could not be queried."""


@dataclass
class FieldDiff:
    spec: FieldSpec
    cfg_value: Optional[str]  # normalized — used for comparison and DB storage
    receiver_value: Optional[str]  # None: not queried OR field not receiver-derivable
    tos_value: Optional[str]  # None: not queried OR field absent in TOS
    sources_queried: FrozenSet[str] = field(default_factory=frozenset)
    verdict: Verdict = Verdict.OK
    suggestion: Optional[str] = None
    suggestion_source: Optional[str] = None  # "receiver", "tos", "agree"
    note: Optional[str] = None
    cfg_raw: Optional[str] = None  # raw value from stations.cfg, for display only
    tos_raw: Optional[str] = None  # raw value from TOS, for display only

    @property
    def cfg_key(self) -> str:
        return self.spec.cfg_key

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def needs_attention(self) -> bool:
        return self.verdict not in (Verdict.OK, Verdict.NO_DATA, Verdict.NOT_QUERYABLE)

    @property
    def cfg_placeholder(self) -> bool:
        """cfg contains a raw value that normalizes to None (recognized placeholder).

        True when the raw cfg line holds a value (e.g. ``antenna-AFST-20210527``)
        that ``spec.normalize`` strips to ``None``.  The key should be removed
        from stations.cfg rather than kept or written to.
        """
        return self.cfg_raw is not None and self.cfg_value is None

    @property
    def tos_placeholder(self) -> bool:
        """TOS holds a raw value that normalizes to None (a known placeholder).

        True when TOS carries e.g. a synthetic device serial
        (``antenna-ODDF-20230706`` — TOS's encoding of "serial unknown") that
        ``spec.normalize`` strips to ``None``. Semantically very different
        from "TOS has no value": the device EXISTS and its serial is
        recorded-as-unknown, so blindly pushing the cfg value into TOS is
        usually wrong (the cfg value may belong to a previous device).
        """
        return self.tos_raw is not None and self.tos_value is None

    @property
    def format_mismatch(self) -> bool:
        """cfg is logically correct but stored in a different notation than the receiver uses.

        True when verdict is OK (normalized values agree) but the raw cfg value
        differs from the receiver's raw value. Indicates a notation-only
        discrepancy (e.g. "NP 4.81 / SP 4.81" vs "4.81", or "4.8.1" vs "4.81")
        that can be fixed by writing the receiver value to cfg.
        """
        return (
            self.verdict == Verdict.OK
            and self.cfg_raw is not None
            and self.receiver_value is not None
            and self.cfg_raw != self.receiver_value
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "field": self.cfg_key,
            "label": self.label,
            "cfg": self.cfg_value,
            "receiver": self.receiver_value,
            "tos": self.tos_value,
            "sources_queried": sorted(self.sources_queried),
            "verdict": self.verdict.value,
            "suggestion": self.suggestion,
            "suggestion_source": self.suggestion_source,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _read_cfg_value(station_config: Dict[str, Any], spec: FieldSpec) -> Optional[str]:
    """Look up a field in a station config dict.

    Station configs come in two shapes in this codebase:

    * Flat: ``{"receiver_type": "PolaRX5", "router_ip": "1.2.3.4", ...}``
    * Nested: ``{"receiver": {"type": "..."}, "router": {"ip": "..."}}``

    We look for the flat key first (which matches what's actually written
    in stations.cfg), then fall back to common nested locations.
    """
    val = station_config.get(spec.cfg_key)
    if val is None:
        # nested fallbacks
        if spec.cfg_key.startswith("receiver_"):
            sub = spec.cfg_key[len("receiver_") :]
            val = (station_config.get("receiver") or {}).get(sub)
        elif spec.cfg_key.startswith("antenna_"):
            sub = spec.cfg_key[len("antenna_") :]
            val = (station_config.get("antenna") or {}).get(sub)
        elif spec.cfg_key.startswith("router_"):
            sub = spec.cfg_key[len("router_") :]
            val = (station_config.get("router") or {}).get(sub)
    if val is None or val == "":
        return None
    return str(val)


def _suggest_value(
    spec: FieldSpec,
    receiver_value: Optional[str],
    tos_value: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Choose a candidate value for auto-fill (used when cfg is missing).

    Priority follows the operational workflow: TOS is authoritative for most
    fields, but for receiver-only fields (firmware/serial/type as reported
    by the device) the receiver itself is canonical reality. If both sources
    agree, return the agreed value tagged ``"agree"``.

    When ``spec.receiver_authoritative`` is False, the receiver alone is not
    a valid auto-fill source — the receiver value still drives QC/flagging
    against TOS, but cfg is only ever auto-filled from TOS or from agreement
    between TOS and receiver.
    """
    if receiver_value is not None and tos_value is not None:
        if spec.values_equal(receiver_value, tos_value):
            return tos_value, "agree"
        return None, None  # disagreement; caller decides
    if tos_value is not None:
        return tos_value, "tos"
    if receiver_value is not None and spec.receiver_authoritative:
        return receiver_value, "receiver"
    return None, None


def _compute_verdict(
    spec: FieldSpec,
    cfg: Optional[str],
    rx: Optional[str],
    tos: Optional[str],
    sources: FrozenSet[str],
) -> Verdict:
    has_rx = rx is not None and "receiver" in sources
    has_tos = tos is not None and "tos" in sources

    # Determine if the requested sources can in principle supply this field.
    can_query_rx = "receiver" in sources and spec.receiver_extract is not None
    can_query_tos = "tos" in sources and spec.tos_extract is not None
    queryable = can_query_rx or can_query_tos

    if not queryable:
        return Verdict.NOT_QUERYABLE

    if not has_rx and not has_tos:
        # The field is queryable but no source returned a value — caller
        # needs to know the result is "no data", not a real conflict.
        return Verdict.NO_DATA

    if cfg is None:
        return Verdict.MISSING

    cfg_matches_rx = (not has_rx) or spec.values_equal(cfg, rx)
    cfg_matches_tos = (not has_tos) or spec.values_equal(cfg, tos)

    if cfg_matches_rx and cfg_matches_tos:
        # cfg agrees with everything queried; sources may still disagree
        # internally, which the caller may want to know about.
        if has_rx and has_tos and not spec.values_equal(rx, tos):
            return Verdict.SOURCES_DISAGREE
        return Verdict.OK

    return Verdict.CONFLICT


def _sync_discrepancy_log(
    station_id: str,
    outcomes: List[
        Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]
    ],
) -> None:
    """Write a station's collected log outcomes in one batch.

    Import is function-local and the whole thing is best-effort: the audit log
    is a side record, and a database being unreachable must never fail a
    comparison the caller asked for.
    """
    try:
        from . import discrepancy_log as _dlog  # type: ignore[attr-defined]

        _dlog.sync_station(
            station_id,
            [
                _dlog.LogSyncEntry(
                    cfg_key=key,
                    verdict=verdict,
                    cfg_value=cfg_v,
                    receiver_value=rx_v,
                    tos_value=tos_v,
                )
                for key, verdict, cfg_v, rx_v, tos_v in outcomes
            ],
            detected_by=_dlog.DETECTED_BY_RECONCILE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] discrepancy_log sync failed: %s", station_id, exc)


def compare_station(
    station_id: str,
    station_config: Dict[str, Any],
    receiver_identity: Optional[Dict[str, Any]],
    tos_data: Optional[Dict[str, Any]],
    fields: Optional[Iterable[str]] = None,
    queried_sources: Optional[Iterable[str]] = None,
    field_specs: Optional[List[FieldSpec]] = None,
    log_discrepancies: bool = True,
) -> List[FieldDiff]:
    """Build :class:`FieldDiff` records for one station.

    Args:
        station_id: 4-letter marker (used only in log messages).
        station_config: Dict from gps_parser for the station.
        receiver_identity: ``health["receiver_identity"]`` dict, or ``None``
            if the receiver was not queried (or could not be reached).
        tos_data: TOS station record, or ``None`` if TOS was not queried
            (or the station is not in TOS).
        fields: Optional whitelist of cfg keys to include.
        queried_sources: Iterable subset of ``{"cfg","receiver","tos"}``
            indicating which sources the caller *attempted* to query. This
            distinguishes "not asked" from "asked but no data": a field
            reported as ``MISSING`` only makes sense for sources we asked.
            Defaults are inferred from the non-None arguments above.

    Returns:
        One :class:`FieldDiff` per field in the manifest (filtered to
        ``fields`` if provided).
    """
    if queried_sources is None:
        sources = {"cfg"}
        if receiver_identity is not None:
            sources.add("receiver")
        if tos_data is not None:
            sources.add("tos")
    else:
        sources = set(queried_sources)
    sources_frozen = frozenset(sources)

    source_specs = list(field_specs) if field_specs is not None else list(FIELDS)
    by_key = {f.cfg_key: f for f in source_specs}
    if fields is not None:
        wanted = [by_key[k] for k in fields if k in by_key]
    else:
        wanted = source_specs

    diffs: List[FieldDiff] = []
    # (cfg_key, verdict_or_None, cfg, receiver, tos) — verdict None = "close the
    # open row". Accumulated here and written once after the loop; see §4.7.
    log_outcomes: List[
        Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]
    ] = []
    for spec in wanted:
        cfg_raw = _read_cfg_value(station_config, spec)
        cfg_val = spec.normalize(cfg_raw)

        rx_val: Optional[str] = None
        if (
            "receiver" in sources
            and spec.receiver_extract is not None
            and receiver_identity is not None
        ):
            try:
                rx_val = spec.normalize(spec.receiver_extract(receiver_identity))
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[%s] receiver extract for %s failed: %s",
                    station_id,
                    spec.cfg_key,
                    exc,
                )

        tos_val: Optional[str] = None
        tos_raw: Optional[str] = None
        if "tos" in sources and spec.tos_extract is not None and tos_data is not None:
            try:
                extracted = spec.tos_extract(tos_data)
                tos_raw = str(extracted).strip() if extracted is not None else None
                tos_val = spec.normalize(extracted)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[%s] tos extract for %s failed: %s", station_id, spec.cfg_key, exc
                )

        verdict = _compute_verdict(spec, cfg_val, rx_val, tos_val, sources_frozen)

        suggestion: Optional[str] = None
        suggestion_source: Optional[str] = None
        if verdict in (Verdict.MISSING, Verdict.CONFLICT):
            suggestion, suggestion_source = _suggest_value(spec, rx_val, tos_val)

        note: Optional[str] = None
        if (
            verdict == Verdict.SOURCES_DISAGREE
            and rx_val is not None
            and tos_val is not None
        ):
            note = f"receiver={rx_val!r} but TOS={tos_val!r}"

        if (
            verdict in (Verdict.CONFLICT, Verdict.MISSING, Verdict.SOURCES_DISAGREE)
            and spec.tos_breakdown is not None
            and tos_data is not None
        ):
            try:
                breakdown = spec.tos_breakdown(tos_data)
                if breakdown:
                    note = f"{note}; {breakdown}" if note else breakdown
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[%s] tos_breakdown for %s failed: %s",
                    station_id,
                    spec.cfg_key,
                    exc,
                )

        # Collect this field's log outcome; the whole station is written in ONE
        # pass after the loop. See architecture review §4.7 for why this is no
        # longer done here — it used to open a connection per field.
        #   * actionable verdicts (MISSING/CONFLICT/SOURCES_DISAGREE) → open row
        #   * OK → auto-close ONLY when *every* source that could supply this
        #     field was actually queried, otherwise we'd close a row we have no
        #     business closing (e.g. cfg=AAA, TOS=AAA, but a stale receiver=BBB
        #     drift is still real and we just didn't probe).
        #   * NO_DATA / NOT_QUERYABLE → leave existing rows untouched.
        if verdict in (Verdict.MISSING, Verdict.CONFLICT, Verdict.SOURCES_DISAGREE):
            log_outcomes.append((spec.cfg_key, verdict.value, cfg_val, rx_val, tos_val))
        elif verdict == Verdict.OK:
            fully_observed = (
                spec.receiver_extract is None or "receiver" in sources_frozen
            ) and (spec.tos_extract is None or "tos" in sources_frozen)
            if fully_observed:
                log_outcomes.append((spec.cfg_key, None, None, None, None))

        diffs.append(
            FieldDiff(
                spec=spec,
                cfg_value=cfg_val,
                receiver_value=rx_val,
                tos_value=tos_val,
                sources_queried=sources_frozen,
                verdict=verdict,
                suggestion=suggestion,
                suggestion_source=suggestion_source,
                note=note,
                cfg_raw=cfg_raw,
                tos_raw=tos_raw,
            )
        )
    if log_discrepancies and log_outcomes:
        _sync_discrepancy_log(station_id, log_outcomes)

    return diffs


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_diff(
    station_id: str,
    diff: FieldDiff,
    new_value: str,
    cfg_path: Optional[Path] = None,
    resolved_by: Optional[str] = None,
) -> bool:
    """Write ``new_value`` for ``diff.cfg_key`` to stations.cfg.

    Returns True if the file changed, False if the value already matched.
    Raises :class:`FileNotFoundError` if the cfg path can't be located.

    On a successful write the matching open row in ``cfg_discrepancy``
    is marked resolved (``resolved_action='cfg_updated'``). DB failures
    are swallowed — the cfg write is the operator-visible action; the
    audit log is best-effort.
    """
    from ..config.receivers_config import _update_cfg_field

    if cfg_path is None:
        try:
            import gps_parser as _gps  # type: ignore
        except ImportError as exc:
            raise SourceUnavailableError(
                "gps_parser not importable — cannot locate stations.cfg"
            ) from exc
        cfg_path = Path(_gps.ConfigParser().get_stations_config_path())

    changed = _update_cfg_field(cfg_path, station_id, diff.cfg_key, new_value)

    if changed:
        try:
            from . import discrepancy_log as _dlog  # type: ignore[attr-defined]

            note = (
                f"cfg updated from {diff.cfg_value!r} to {new_value!r} "
                f"(receiver={diff.receiver_value!r}, tos={diff.tos_value!r})"
            )
            _dlog.record_resolution(
                station_id,
                diff.cfg_key,
                action=_dlog.ACTION_CFG_UPDATED,
                resolved_value=new_value,
                resolved_by=resolved_by,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[%s] failed to log resolution for %s: %s",
                station_id,
                diff.cfg_key,
                exc,
            )

    return changed


def remove_diff(
    station_id: str,
    diff: FieldDiff,
    cfg_path: Optional[Path] = None,
    resolved_by: Optional[str] = None,
) -> bool:
    """Remove the key for ``diff.cfg_key`` from stations.cfg entirely.

    Used to clean up placeholder values (e.g. TOS synthetic antenna serials
    like ``antenna-AFST-20210527``) that should not be stored in cfg at all.
    The RINEX pipeline handles the absent field gracefully.

    Returns True if a line was removed. DB audit is best-effort.
    """
    from ..config.receivers_config import _remove_cfg_field

    if cfg_path is None:
        try:
            import gps_parser as _gps  # type: ignore
        except ImportError as exc:
            raise SourceUnavailableError(
                "gps_parser not importable — cannot locate stations.cfg"
            ) from exc
        cfg_path = Path(_gps.ConfigParser().get_stations_config_path())

    changed = _remove_cfg_field(cfg_path, station_id, diff.cfg_key)

    if changed:
        try:
            from . import discrepancy_log as _dlog  # type: ignore[attr-defined]

            _dlog.record_resolution(
                station_id,
                diff.cfg_key,
                action=_dlog.ACTION_CFG_UPDATED,
                resolved_value=None,
                resolved_by=resolved_by,
                note=f"removed placeholder {diff.cfg_raw!r} from cfg",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[%s] failed to log removal for %s: %s",
                station_id,
                diff.cfg_key,
                exc,
            )

    return changed
