"""Field-workflow orchestration: install / move / visit operations.

This module is the single source of truth for receiver field operations.
The ``receivers cfg`` CLI subcommands and the standalone ``field_visit.py``
script both call into these Python functions — no logic duplication.

Each operation combines:

* a TOS state change via :class:`tostools.api.tos_writer.TOSWriter`
  (join close+open for moves, vitjun create for visits), and
* an optional ``stations.cfg`` update for installs (the destination
  station gets new ``receiver_serial`` / ``receiver_type`` /
  ``receiver_firmware_version`` / ``rinex_config_valid_from``).

All three operations accept a ``date`` parameter that defaults to *now*
but accepts an arbitrary past date — field work happens first, computer
entry follows, sometimes days later.

The operations default to ``dry_run=True`` (same convention as
:class:`TOSWriter`). The CLI flips the default with ``--no-dry-run`` /
``--live`` after the operator has reviewed the dry-run output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from tostools.api.tos_writer import TOSWriter

logger = logging.getLogger(__name__)

# Fallback default warehouse for retired devices — matches the TOS
# station name + the memory note `reference_tos_warehouse_locations`.
# Operators / sites can override via the ``[tos] default_warehouse``
# key in receivers.cfg (read by :func:`_resolve_default_warehouse`)
# without editing source, so a TOS rename of B9 does not silently
# break ``move-device`` / ``replace-receiver``.
_FALLBACK_DEFAULT_WAREHOUSE = "B9 - Kjallari - Jörð"


def _resolve_default_warehouse() -> str:
    """Read ``[tos] default_warehouse`` from receivers.cfg if set.

    Falls back to :data:`_FALLBACK_DEFAULT_WAREHOUSE` when the section
    or key is absent. Read once at module import time via
    :data:`DEFAULT_WAREHOUSE` so CLI ``--to`` defaults can reference it
    statically; runtime callers that want the live value should call
    this function directly.
    """
    try:
        from ..config.receivers_config import ReceiversConfig

        cfg = ReceiversConfig()
        if cfg.config.has_section("tos"):
            value = cfg.config.get("tos", "default_warehouse", fallback=None)
            if value:
                return value
    except Exception:
        # Config absent / corrupt / gps_parser missing — fall through
        # to the hardcoded fallback rather than crashing.
        pass
    return _FALLBACK_DEFAULT_WAREHOUSE


DEFAULT_WAREHOUSE = _resolve_default_warehouse()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class OperationResult:
    """Summary returned by :func:`install_device` / :func:`move_device` /
    :func:`add_visit`.

    Attributes:
        operation: ``"install"``, ``"move"``, or ``"visit"``.
        station_id: 4-char marker the operation targets (or for move,
            the device's source station if any).
        serial: Device serial number, when applicable.
        date: ISO date string used for the operation.
        tos_changes: Per-step TOS responses keyed by step name. Values
            are :class:`tostools.api.tos_writer.DryRunResult` in dry-run
            mode.
        cfg_changes: Map of ``stations.cfg`` keys that were updated to
            their new values. Empty for move/visit and dry-run.
        vitjun_id: ``id_maintenance`` of any vitjun created, or
            ``"<dry-run>"`` when dry-run skipped the real POST.
        dry_run: Whether the operation was a dry run.
    """

    operation: str
    station_id: Optional[str] = None
    serial: Optional[str] = None
    date: Optional[str] = None
    tos_changes: Dict[str, Any] = field(default_factory=dict)
    cfg_changes: Dict[str, str] = field(default_factory=dict)
    vitjun_id: Optional[Union[int, str]] = None
    dry_run: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CfgOperationError(RuntimeError):
    """Raised by operations when a precondition cannot be satisfied."""


def _visit_default_time(date_arg: Optional[str]) -> str:
    """Resolve a ``date`` arg to an ISO datetime.

    Two distinct operator intents map to two different defaults:

      * ``None`` (operator typed no ``--date`` flag) → **right now**
        (current timestamp, seconds precision). Means "I'm entering
        this at the moment the field event is happening."
      * Bare ``"YYYY-MM-DD"`` (operator typed a date but no time) →
        ``"YYYY-MM-DDT12:00:00"`` (noon). Means "this happened on
        that day during the workday" — backdated entry.
      * Any string containing ``'T'`` (full ISO datetime) → preserved.
        Means "this happened at this specific moment" (e.g. the
        ``T23:00:00`` HRAC swap).

    Used for both the join transition_date and the vitjun start_time
    inside :func:`move_device`, so a single resolved value flows
    through every TOS write per invocation.
    """
    if date_arg is None:
        return datetime.now().replace(microsecond=0).isoformat()
    if "T" not in date_arg:
        return f"{date_arg}T12:00:00"
    return date_arg


def _visit_default_end_time(date_arg: Optional[str]) -> Optional[str]:
    """Resolve an *end-time* arg to an ISO datetime.

    Mirrors :func:`_visit_default_time` but promotes a bare
    ``YYYY-MM-DD`` to **end-of-day** (``T23:59:59``) rather than noon.
    The operator intent for ``--end-time YYYY-MM-DD`` is "ended some
    time that day", not "ended at noon" — noon-promotion can produce
    an end-time *before* the start-time when ``--date`` carries an
    explicit afternoon time, which TOS will store as a
    negative-duration vitjun.

    ``None`` and full ISO datetimes pass through unchanged.
    """
    if date_arg is None:
        return None
    if "T" not in date_arg:
        return f"{date_arg}T23:59:59"
    return date_arg


def _resolve_writer(writer: Optional[TOSWriter], dry_run: bool) -> TOSWriter:
    """Return the caller's writer or build a default one in the requested
    dry-run mode. The caller still owns the writer's lifecycle when they
    pass one in."""
    if writer is not None:
        return writer
    return TOSWriter(dry_run=dry_run)


def _resolve_cfg_path(cfg_path: Optional[Path]) -> Path:
    """Locate ``stations.cfg`` for write operations.

    Order:
        1. Caller-supplied ``cfg_path`` (CLI ``--cfg-path``).
        2. ``GPS_CONFIG_DATA_REPO`` env var → ``$REPO/stations.cfg``
           when the file exists (the gps-config-data source-of-truth
           clone).
        3. :func:`gps_parser.ConfigParser.get_stations_config_path` —
           the runtime-deployed copy (usually ``~/.config/gpsconfig/``).
    """
    if cfg_path is not None:
        return cfg_path

    import os

    repo = os.environ.get("GPS_CONFIG_DATA_REPO")
    if repo:
        candidate = Path(repo).expanduser() / "stations.cfg"
        if candidate.exists():
            return candidate

    try:
        import gps_parser as _gps  # type: ignore
    except ImportError as exc:
        raise CfgOperationError(
            "gps_parser not importable and no cfg_path / "
            "GPS_CONFIG_DATA_REPO given — cannot locate stations.cfg"
        ) from exc
    return Path(_gps.ConfigParser().get_stations_config_path())


def _resolve_station(writer: TOSWriter, station_id: str) -> int:
    """Resolve a 4-char marker to a TOS station ``id_entity``."""
    eid = writer.find_station_by_marker(station_id)
    if eid is None:
        raise CfgOperationError(
            f"No TOS station matches marker {station_id!r}. "
            f"Check spelling or the station's marker attribute in TOS."
        )
    return eid


def _find_open_children(writer: TOSWriter, station_eid: int, subtype: str) -> List[int]:
    """Return every open child of ``subtype`` joined to a station.

    The TOS invariant is at most one open join per device subtype, but the
    invariant is only *enforced* by the verbs that respect it — a historical
    ``add-antenna --force`` (or a hand-edit in the web UI) can leave two open
    antennas on one station. Callers that must not silently pick one of them
    (:func:`replace_antenna`) use this and refuse on >1; callers that only
    need "is there one" use :func:`_find_open_child`.
    """
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return []
    children = history.get("children_connections") or []
    open_children = [c for c in children if c.get("time_to") is None]
    found: List[int] = []
    for child in open_children:
        cid = child.get("id_entity_child")
        if cid is None:
            continue
        child_hist = writer.get_entity_history(int(cid))
        if (
            isinstance(child_hist, dict)
            and child_hist.get("code_entity_subtype") == subtype
        ):
            found.append(int(cid))
    return found


def _find_open_child(
    writer: TOSWriter, station_eid: int, subtype: str
) -> Optional[int]:
    """Return the id_entity of the open child of ``subtype`` joined to a station.

    Walks the station's ``children_connections``, keeps the ones with no
    ``time_to`` (open joins), and returns the first whose own
    ``code_entity_subtype`` matches ``subtype`` (e.g. ``"gnss_receiver"``,
    ``"modem_gsm"``, ``"sim_card"``).

    Used for the destination-displacement constraint: a station should have
    at most one open join per device subtype. If one exists, the operator
    must retire/transfer it before installing a replacement.

    Returns ``None`` when no open child of that subtype exists.
    """
    found = _find_open_children(writer, station_eid, subtype)
    return found[0] if found else None


def _find_open_gnss_receiver_child(
    writer: TOSWriter, station_eid: int
) -> Optional[int]:
    """Return the id_entity of the open gnss_receiver child of a station.

    Thin wrapper over :func:`_find_open_child` for the common receiver case.
    Returns ``None`` when no open receiver join exists at the station.
    """
    return _find_open_child(writer, station_eid, "gnss_receiver")


def _device_attribute(device_hist: Dict[str, Any], code: str) -> Optional[str]:
    """Pluck the currently-open value of one attribute from a device payload.

    TOS returns ``attributes`` as a flat denormalised list — each item IS
    an attribute_value row (not a wrapper). Multiple rows with the same
    ``code`` are temporal periods. Prefer the row where ``date_to is None``
    (the open period); fall back to the most recent ``date_from`` if none
    is open.
    """
    candidates = [
        a for a in (device_hist.get("attributes") or []) if a.get("code") == code
    ]
    if not candidates:
        return None
    open_rows = [a for a in candidates if a.get("date_to") is None]
    pool = open_rows or candidates
    latest = max(pool, key=lambda a: a.get("date_from") or "")
    return latest.get("value")


def _canonical_receiver_type(igs_name: Optional[str]) -> Optional[str]:
    """Map a TOS IGS-style receiver name to the stations.cfg short form.

    e.g. ``"SEPT POLARX5"`` → ``"PolaRX5"``. Returns the input
    unchanged if no canonical mapping is known, so unfamiliar models
    aren't silently corrupted.
    """
    if not igs_name:
        return igs_name
    try:
        from ..health.receiver_fingerprint import identify_receiver_type
    except ImportError:
        return igs_name
    canonical = identify_receiver_type({"receiver_model": igs_name})
    return canonical if canonical is not None else igs_name


def _apply_cfg_updates(
    cfg_path: Path,
    station_id: str,
    updates: Dict[str, Optional[str]],
) -> Dict[str, str]:
    """Apply a batch of ``key=value`` updates to one station section.

    Returns the subset of ``updates`` that actually changed the file
    (skipped keys had a matching value already). ``None`` values in
    ``updates`` are skipped entirely.
    """
    from ..config.receivers_config import _update_cfg_field

    applied: Dict[str, str] = {}
    for key, value in updates.items():
        if value is None:
            continue
        if _update_cfg_field(cfg_path, station_id, key, value):
            applied[key] = value
    return applied


def _find_receiver_at_station(
    writer: TOSWriter,
    station_eid: int,
) -> Optional[int]:
    """Find the gnss_receiver associated with a station for ``--serial``
    inference.

    Used by :func:`move_device` when the caller passes ``--from-station``
    instead of ``--serial``. The user's mental model is usually "the
    receiver at SAVI" — either currently physically there (open join)
    or recently closed off (still nearby, e.g. just brought to the
    workshop).

    Resolution order:

    1. The **currently open** gnss_receiver child of ``station_eid``.
       Covers the "move what's there" workflow ("the receiver at SAVI
       is broken, send it to B9").
    2. The **most recently closed** gnss_receiver child. Covers the
       transfer case ("move the receiver that just came off HRAC to
       SAVI") *only while HRAC has no fresh open receiver*. If HRAC
       has already been refilled, this picks up the new device — pass
       ``--serial`` explicitly in that situation.

    Returns the device's ``id_entity`` or None.
    """
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return None
    children = history.get("children_connections") or []

    # Prefer the currently-open receiver.
    for c in children:
        if c.get("time_to") is not None:
            continue
        cid = c.get("id_entity_child")
        if cid is None:
            continue
        chist = writer.get_entity_history(int(cid))
        if (
            isinstance(chist, dict)
            and chist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)

    # Fall back to most recently closed.
    closed = [c for c in children if c.get("time_to") is not None]
    closed.sort(key=lambda c: c.get("time_to") or "", reverse=True)
    for c in closed:
        cid = c.get("id_entity_child")
        if cid is None:
            continue
        chist = writer.get_entity_history(int(cid))
        if (
            isinstance(chist, dict)
            and chist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)
    return None


def _find_recently_left_receiver(
    writer: TOSWriter,
    station_eid: int,
    on_or_before: Optional[str] = None,
) -> Optional[int]:
    """Find the gnss_receiver that left ``station_eid`` at or just before
    ``on_or_before``. Returns the device's ``id_entity`` or None.

    Used for two purposes:

    * Auto-vitjun text on a station install — pass the install date
      as ``on_or_before`` so the helper picks the receiver whose join
      was just closed by a prior :func:`move_device`.
    * ``--serial`` inference when only ``--from-station`` is given —
      pass ``None`` to find the absolute most recent closed join.
    """
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return None
    cap = on_or_before
    candidates = [
        c
        for c in (history.get("children_connections") or [])
        if c.get("time_to") is not None and (cap is None or c.get("time_to") <= cap)
    ]
    candidates.sort(key=lambda c: c.get("time_to") or "", reverse=True)
    for c in candidates:
        cid = c.get("id_entity_child")
        if cid is None:
            continue
        child_hist = writer.get_entity_history(int(cid))
        if (
            isinstance(child_hist, dict)
            and child_hist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)
    return None


def _auto_vitjun_text(
    writer: TOSWriter,
    station_eid: int,
    new_device: Dict[str, Any],
    transition_date: str,
    from_station: Optional[str] = None,
) -> str:
    """Build a default vitjun work text from the operation context.

    Derives "old" from the receiver that left ``station_eid`` at or
    just before ``transition_date`` (closed children_connections);
    "new" from the device payload's attributes. Produces:

      - ``"Skipt um móttakara: <old> → <new>"`` for a swap
      - ``"Móttakari fluttur frá <from_station>: <new> (skipt um <old>)"`` for a transfer
      - ``"Móttekinn móttakari frá <from_station>: <new>"`` for transfer with empty dest
      - ``"Settur upp móttakari: <new>"`` for a fresh deploy
    """
    new_serial = _device_attribute(new_device, "serial_number") or "?"
    new_model = _canonical_receiver_type(_device_attribute(new_device, "model")) or "?"
    new_label = f"{new_model} {new_serial}".strip()

    old_id = _find_recently_left_receiver(writer, station_eid, transition_date)
    if old_id is None:
        if from_station:
            return f"Móttekinn móttakari frá {from_station}: {new_label}"
        return f"Settur upp móttakari: {new_label}"

    old_device = writer.get_entity_history(old_id)
    if not isinstance(old_device, dict):
        return f"Settur upp móttakari: {new_label}"

    old_serial = _device_attribute(old_device, "serial_number") or "?"
    old_model = _canonical_receiver_type(_device_attribute(old_device, "model")) or "?"
    old_label = f"{old_model} {old_serial}".strip()

    if from_station:
        return (
            f"Móttakari fluttur frá {from_station}: {new_label} (skipt um {old_label})"
        )
    return f"Skipt um móttakara: {old_label} → {new_label}"


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def _default_rinex_valid_from(install_iso: str) -> str:
    """Compute the default rinex_config_valid_from date from an install dt.

    Convention: stations.cfg ``rinex_config_valid_from`` is the *first
    full day* of the new equipment configuration. If install happened
    exactly at midnight, that day is fully under the new config
    → same date. If install happened later in the day (e.g. 23:00),
    that day is *split* between old and new equipment, so the first
    full day is the next one.

    Args:
        install_iso: ISO datetime string the install happened at.

    Returns:
        ``YYYY-MM-DD`` date string for ``rinex_config_valid_from``.
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    try:
        dt = _dt.fromisoformat(install_iso)
    except ValueError:
        # Bare YYYY-MM-DD already — treat as midnight, return as-is
        return install_iso.split("T", 1)[0]
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.date().isoformat()
    return (dt.date() + _td(days=1)).isoformat()


def add_antenna(
    writer: Optional[TOSWriter] = None,
    *,
    station_id: Optional[str] = None,
    warehouse: Optional[str] = None,
    model: str,
    radome: str = "NONE",
    serial: Optional[str] = None,
    radome_serial: Optional[str] = None,
    antenna_height: Optional[str] = None,
    owner: str = "Jarðeðlismælihópur",
    date_start: Optional[str] = None,
    comment: Optional[str] = None,
    force: bool = False,
    dry_run: bool = True,
) -> OperationResult:
    """Create a GNSS antenna (and radome, when present) in TOS and join to a station.

    Unlike :func:`add_receiver` (warehouse intake of a probed unit), an antenna
    cannot be probed — its identity comes from the operator / stations.cfg. This
    creates the ``antenna`` device entity, joins it to the station, and — when
    ``radome`` is not ``"NONE"`` — does the same for a separate ``radome`` device
    (TOS models antenna and radome as distinct children of the station).

    Antenna serials are frequently unrecorded. When ``serial`` is empty/None a
    synthetic ``antenna-<STID>-<YYYYMMDD>`` is generated (see
    :func:`tostools.device.synthetic_serial`), mirroring the existing radome
    convention, so the TOS non-empty-serial requirement is met and a provenance
    ``comment`` is auto-recorded.

    Args:
        writer: A configured :class:`TOSWriter`, or ``None`` to build one in
            ``dry_run`` mode (caller owns the writer's lifecycle if passed).
        station_id: 4-char station marker to install the antenna at.
        model: Antenna model (IGS name or known alias; validated).
        radome: Radome IGS code (default ``"NONE"`` → no radome device).
        serial: Antenna serial, or ``None``/empty → synthetic placeholder.
        antenna_height: ARP height in metres (string), or ``None`` to omit
            (RINEX ``ANTENNA: DELTA H`` then defaults to 0.0).
        owner: Owner label (must match the TOS OwnersCache).
        date_start: Install date (bare ``YYYY-MM-DD`` → noon, matching
            ``cfg move-device`` so co-installs share a TOS session); defaults
            to the station's own TOS ``date_start``, then to today.
        comment: Free-text comment; defaults to a synthetic-serial note when the
            serial was generated.
        force: Bypass the one-open-antenna-per-station guard and the
            duplicate-serial guard.
        dry_run: When ``True`` (default), no writes are sent.

    Returns:
        An :class:`OperationResult` with ``operation="add-antenna"`` and the
        per-step TOS payloads under ``tos_changes``.
    """
    from tostools.device import (
        build_antenna_attributes,
        build_required_attributes,
        synthetic_serial,
        validate_model,
    )

    if (station_id is None) == (warehouse is None):
        raise CfgOperationError(
            "add-antenna: pass either --station STID (install) OR --warehouse "
            "LOCATION (intake), not both and not neither."
        )
    # Warehouse intake registers a specific unit you are holding, so its serial
    # is on the label in front of you. A synthetic serial means "this station's
    # antenna serial was never recorded" — meaningless for inventory, and it
    # would not match at install time, so the unit would be duplicated instead
    # of reparented. Require the real one.
    if warehouse and (serial is None or str(serial).strip() == ""):
        raise CfgOperationError(
            "add-antenna --warehouse: --serial is required for intake. A "
            "synthetic placeholder cannot be matched when the unit is later "
            "installed, so the antenna would be duplicated rather than moved."
        )
    if warehouse and radome and radome.upper() != "NONE" and not radome_serial:
        raise CfgOperationError(
            "add-antenna --warehouse: --radome-serial is required alongside "
            "--radome for intake (same matching reason as --serial). If the "
            "radome carries no serial, omit --radome here and let "
            "`cfg replace-antenna` create it at install time."
        )

    w = _resolve_writer(writer, dry_run)
    station_eid = (
        _resolve_station(w, station_id)
        if station_id is not None
        else _b9_eid(w, warehouse=warehouse)
    )

    # Default install date = the station's own date_start, else today. For
    # warehouse intake there is no station to inherit from — today it is.
    if not date_start:
        st_date = None
        if station_id is not None:
            station_hist = w.get_entity_history(station_eid)
            st_date = (
                _device_attribute(station_hist, "date_start")
                if isinstance(station_hist, dict)
                else None
            )
        date_start = st_date or datetime.now().date().isoformat()
    # Resolve to a full datetime with the SAME field-work convention as
    # cfg move-device: bare YYYY-MM-DD -> noon (T12:00:00); a full ISO datetime
    # is preserved. This alignment matters when co-installing devices: TOS groups
    # a station's devices into "sessions" keyed on the exact join
    # (time_from, time_to) in tos_client._build_history_from_connections. An
    # antenna and a receiver installed on the same day but at different instants
    # split into SEPARATE sessions, and current_session() — the stream SKL's only
    # metadata source — then sees just one of them (a blank antenna or blank
    # receiver in the RINEX header). Using move-device's convention here means
    # passing the same --date-start / --date to every verb yields one shared
    # session. (move-device promotes bare dates to noon via _visit_default_time.)
    eff_date = _visit_default_time(date_start)

    # One open antenna per station (mirrors the receiver displacement guard).
    # Irrelevant for a warehouse, which holds any number of spare antennas.
    open_existing = (
        _find_open_child(w, station_eid, "antenna") if station_id is not None else None
    )
    if open_existing is not None and not force:
        raise CfgOperationError(
            f"{station_id} already has an open antenna child "
            f"(id_entity={open_existing}). Swap it with "
            f"`cfg replace-antenna --station {station_id} …`, or pass --force "
            f"to add a second (leaves TWO open antennas — ambiguous for every "
            f"RINEX header and station.info line derived from the session)."
        )

    igs_model = validate_model("antenna", model)

    synthetic = serial is None or str(serial).strip() == ""
    ant_serial = (
        synthetic_serial("antenna", str(station_id), eff_date)
        if synthetic
        else str(serial).strip()
    )
    if comment is None and synthetic:
        comment = "antenna serial unknown at install — synthetic placeholder"

    attrs = build_antenna_attributes(
        serial=ant_serial,
        model=igs_model,
        owner=owner,
        date_start=eff_date,
        antenna_height=antenna_height,
    )
    if antenna_height is None:
        logger.warning(
            "%s: no antenna height supplied — antenna created without "
            "antenna_height; RINEX 'ANTENNA: DELTA H' defaults to 0.0 until "
            "corrected.",
            station_id,
        )
    if comment:
        attrs.append(
            {
                "code": "comment",
                "value": comment,
                "date_from": eff_date,
                "date_to": None,
            }
        )

    result = OperationResult(
        operation="add-antenna",
        station_id=station_id,
        serial=ant_serial,
        date=eff_date,
        dry_run=dry_run,
    )
    result.tos_changes["antenna_attributes"] = attrs
    result.tos_changes["synthetic_serial"] = synthetic

    ant_resp = w.create_device("antenna", attrs, force=force)
    result.tos_changes["antenna_create"] = ant_resp
    ant_id = ant_resp.get("id_entity") if isinstance(ant_resp, dict) else None
    if ant_id is not None:
        # TOS POST /joins returns an empty body on success — wrap it so the
        # summary reads as "joined", not a bare null that looks like a failure.
        join_resp = w.create_entity_connection(station_eid, int(ant_id), eff_date)
        result.tos_changes["antenna_join"] = {
            "joined": True,
            "parent": station_eid,
            "child": int(ant_id),
            "response": join_resp,
        }
    else:
        result.tos_changes["antenna_join"] = {
            "joined": False,
            "parent": station_eid,
            "note": "device id unknown (dry-run) — join previewed",
        }

    # Radome — a separate TOS device. "NONE" means no radome at this station.
    igs_radome = validate_model("radome", radome or "NONE")
    if igs_radome != "NONE":
        rad_serial = (
            str(radome_serial).strip()
            if radome_serial and str(radome_serial).strip()
            else synthetic_serial("radome", str(station_id), eff_date)
        )
        rad_attrs = build_required_attributes(rad_serial, igs_radome, owner, eff_date)
        result.tos_changes["radome_serial"] = rad_serial
        result.tos_changes["radome_attributes"] = rad_attrs
        rad_resp = w.create_device("radome", rad_attrs, force=force)
        result.tos_changes["radome_create"] = rad_resp
        rad_id = rad_resp.get("id_entity") if isinstance(rad_resp, dict) else None
        if rad_id is not None:
            rad_join = w.create_entity_connection(station_eid, int(rad_id), eff_date)
            result.tos_changes["radome_join"] = {
                "joined": True,
                "parent": station_eid,
                "child": int(rad_id),
                "response": rad_join,
            }
        else:
            result.tos_changes["radome_join"] = {
                "joined": False,
                "parent": station_eid,
            }

    return result


def add_monument(
    writer: Optional[TOSWriter] = None,
    *,
    station_id: Optional[str] = None,
    warehouse: Optional[str] = None,
    height: str = "0.0",
    serial: Optional[str] = None,
    owner: str = "Jarðeðlismælihópur",
    date_start: Optional[str] = None,
    comment: Optional[str] = None,
    model: Optional[str] = None,
    force: bool = False,
    dry_run: bool = True,
) -> OperationResult:
    """Create a monument (survey mark/pillar) in TOS and join it to a station.

    The monument carries the ``antenna_height`` offset (mark → antenna reference
    point); TOS keeps one per height epoch. Like :func:`add_antenna` it can't be
    probed — identity is operator-supplied and the serial defaults to a synthetic
    ``monument-<STID>-<YYYYMMDD>`` placeholder (the fleet convention, e.g.
    ``monument-REYK-19980913``). The ``model`` (physical mark type, e.g.
    ``"GPS stál-fjórfótur"``) is optional free text — omit when unknown.

    Args:
        writer: A configured :class:`TOSWriter`, or ``None`` to build one in
            ``dry_run`` mode.
        station_id: 4-char station marker to install the monument at.
        height: Mark → ARP height in metres (string); defaults to ``"0.0"``.
        serial: Monument serial, or ``None``/empty → synthetic placeholder.
        owner: Owner label (must match the TOS OwnersCache).
        date_start: Install/epoch date (bare ``YYYY-MM-DD`` → noon, matching
            ``cfg move-device`` so co-installs share a TOS session); defaults to
            the station's own TOS ``date_start``, then to today.
        comment: Free-text note; defaults to a synthetic-serial note.
        force: Bypass the one-open-monument-per-station guard and the
            duplicate-serial guard.
        dry_run: When ``True`` (default), no writes are sent.

    Returns:
        An :class:`OperationResult` with ``operation="add-monument"``.
    """
    from tostools.device import build_monument_attributes, synthetic_serial

    # Exactly one destination, same contract as add_antenna.
    if (station_id is None) == (warehouse is None):
        raise CfgOperationError(
            "add-monument: pass either --station STID (install) OR --warehouse "
            "LOCATION (intake), not both and not neither."
        )
    # The synthetic serial is built from the station marker
    # (monument-<STID>-<YYYYMMDD>), which does not exist yet at intake — and a
    # warehoused monument is later joined with `move-device --subtype monument
    # --serial SN`, which matches BY SERIAL. So intake needs the real one.
    if warehouse and (serial is None or str(serial).strip() == ""):
        raise CfgOperationError(
            "add-monument --warehouse: --serial is required for intake. The "
            "synthetic monument-<STID>-<date> placeholder needs a station, and "
            "`move-device --subtype monument` matches the unit by serial when "
            "you later install it."
        )

    w = _resolve_writer(writer, dry_run)
    parent_eid = (
        _resolve_station(w, station_id)
        if station_id is not None
        else _b9_eid(w, warehouse=warehouse)
    )

    if not date_start:
        # At a station the monument inherits the station's own date_start so a
        # co-install shares one TOS session; a warehouse intake has no station
        # to inherit from, so it is simply today.
        if station_id is not None:
            station_hist = w.get_entity_history(parent_eid)
            st_date = (
                _device_attribute(station_hist, "date_start")
                if isinstance(station_hist, dict)
                else None
            )
            date_start = st_date or datetime.now().date().isoformat()
        else:
            date_start = datetime.now().date().isoformat()
    # Same field-work convention as cfg move-device (bare date → noon, full ISO
    # preserved) so a monument co-installed with the receiver/antenna shares one
    # TOS session — see add_antenna for the session-split rationale.
    eff_date = _visit_default_time(date_start)

    # One-open-monument-per-station is a station invariant; a warehouse holds
    # as many spare marks as it likes.
    if station_id is not None:
        open_existing = _find_open_child(w, parent_eid, "monument")
        if open_existing is not None and not force:
            raise CfgOperationError(
                f"{station_id} already has an open monument child "
                f"(id_entity={open_existing}). A new height epoch should close the "
                f"old monument first; or pass --force to add a second."
            )

    synthetic = serial is None or str(serial).strip() == ""
    mon_serial = (
        synthetic_serial("monument", station_id, eff_date)
        if synthetic
        else str(serial).strip()
    )
    if comment is None and synthetic:
        comment = (
            "raðnúmer búið til úr skammstöfun stöðvar + dagsetningu (height epoch)"
        )

    attrs = build_monument_attributes(
        serial=mon_serial,
        owner=owner,
        date_start=eff_date,
        monument_height=height,
        comment=comment,
        model=model,
    )

    result = OperationResult(
        operation="add-monument",
        station_id=station_id,
        serial=mon_serial,
        date=eff_date,
        dry_run=dry_run,
    )
    result.tos_changes["monument_attributes"] = attrs
    result.tos_changes["synthetic_serial"] = synthetic

    resp = w.create_device("monument", attrs, force=force)
    result.tos_changes["monument_create"] = resp
    mid = resp.get("id_entity") if isinstance(resp, dict) else None
    if mid is not None:
        join = w.create_entity_connection(parent_eid, int(mid), eff_date)
        result.tos_changes["monument_join"] = {
            "joined": True,
            "parent": parent_eid,
            "child": int(mid),
            "response": join,
        }
    else:
        result.tos_changes["monument_join"] = {
            "joined": False,
            "parent": parent_eid,
        }

    return result


# ---------------------------------------------------------------------------
# Campaign history import (station.info → TOS) + continuity transitions
# ---------------------------------------------------------------------------

# Station-level attribute code carrying the campaign/continuous classification.
_CONTINUITY_CODE = "continuity"
_CONTINUITY_VALUES = ("campaign", "continuous")


def _existing_session_index(
    writer: TOSWriter, station_eid: int
) -> set[tuple[Optional[str], Optional[str], str]]:
    """Index a station's existing device sessions for idempotent import.

    Returns a set of ``(subtype, serial_number, time_from_date)`` tuples — one
    per child join on the station (open or closed). The importer skips an
    occupation whose ``(gnss_receiver, serial, start-date)`` already appears
    here, so re-running the import is a no-op (the design dedup key:
    marker + session-start + receiver serial).

    Built from the station's ``children_connections`` + each child's
    ``serial_number`` attribute — not the lagging ``/basic_search/`` index — so
    it is reliable on a freshly-written station.
    """
    index: set[tuple[Optional[str], Optional[str], str]] = set()
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return index
    for conn in history.get("children_connections") or []:
        cid = conn.get("id_entity_child")
        tfrom = conn.get("time_from")
        if cid is None or not tfrom:
            continue
        child_hist = writer.get_entity_history(int(cid))
        if not isinstance(child_hist, dict):
            continue
        subtype = child_hist.get("code_entity_subtype")
        serial = _device_attribute(child_hist, "serial_number")
        index.add((subtype, serial, str(tfrom)[:10]))
    return index


def _ensure_device(
    writer: TOSWriter,
    subtype: str,
    attrs: List[Dict[str, Any]],
    serial: str,
    force: bool,
) -> Optional[int]:
    """Return an existing device's id_entity (by serial) or create it.

    Campaign occupations may reuse a device across stations/years, and a
    re-run must not duplicate it — so look the serial up first and reuse the
    entity when found, only creating when it is genuinely new. Returns ``None``
    in dry-run (no id assigned yet), which callers render as a previewed join.
    """
    existing = writer.find_device_by_serial(subtype, serial)
    if isinstance(existing, dict) and existing.get("id_entity") is not None:
        return int(existing["id_entity"])
    resp = writer.create_device(subtype, attrs, force=force)
    return resp.get("id_entity") if isinstance(resp, dict) else None


def import_campaigns(
    writer: Optional[TOSWriter] = None,
    *,
    station_id: str,
    station_info_path: Union[str, Path],
    marker: Optional[str] = None,
    owner: str = "Jarðeðlismælihópur",
    with_monument: bool = False,
    monument_height: Optional[str] = None,
    force: bool = False,
    dry_run: bool = True,
) -> OperationResult:
    """Import a station's GAMIT ``station.info`` campaign occupations into TOS.

    Each occupation (a closed receiver + antenna install span) becomes a pair
    of **closed** device sessions on the station: a ``gnss_receiver`` and an
    ``antenna`` (plus a ``radome`` when the dome is not ``NONE``), each joined
    over the occupation's exact ``[time_from, time_to]`` window. Because the
    receiver and antenna for one occupation share the *same* full datetimes,
    they land in a single TOS session (the session-split trap that bites
    ``add-antenna`` cannot occur here).

    Campaign occupations are **metadata-only** — no ``stations.cfg``, download,
    or monitoring footprint is created (campaigns never enter the operational
    system; only the continuous install does). This function therefore writes
    nothing outside TOS.

    Monuments: a campaign does *not* require a monument. When a campaign used a
    tripod over a benchmark, that tripod is recorded as a monument — pass
    ``with_monument=True`` (optionally ``monument_height``) to create one per
    occupation. Default is off (the common DHARP-on-benchmark case, e.g. VOTT).

    Idempotency: an occupation already present as a ``(gnss_receiver, serial,
    start-date)`` session on the station is skipped (see
    :func:`_existing_session_index`); existing device entities are reused by
    serial rather than duplicated.

    Args:
        writer: Configured :class:`TOSWriter`, or ``None`` to build one.
        station_id: 4-char marker — the TOS station to attach occupations to.
        station_info_path: Path to a GAMIT ``station.info`` file.
        marker: station.info marker to read (defaults to ``station_id``).
        owner: Owner label for created devices.
        with_monument: Create a monument per occupation (tripod-over-benchmark).
        monument_height: Mark→ARP height for the monument; defaults to the
            occupation's station.info antenna height.
        force: Bypass duplicate-serial / session-skip guards.
        dry_run: When ``True`` (default), no writes are sent.

    Returns:
        :class:`OperationResult` with ``operation="import-campaigns"``; per-
        occupation summaries live under ``tos_changes["occupations"]``.
    """
    from tostools.device import (
        build_antenna_attributes,
        build_monument_attributes,
        build_required_attributes,
        synthetic_serial,
        validate_model,
    )
    from tostools.standards.gamit_station_info import parse_station_info

    occupations = parse_station_info(station_info_path, marker=marker or station_id)

    result = OperationResult(
        operation="import-campaigns",
        station_id=station_id,
        dry_run=dry_run,
    )
    if not occupations:
        raise CfgOperationError(
            f"No station.info occupations found for marker "
            f"{(marker or station_id)!r} in {station_info_path}."
        )

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    existing = _existing_session_index(w, station_eid)

    summaries: List[Dict[str, Any]] = []
    created = skipped = 0

    for occ in occupations:
        time_from = occ.time_from.replace(microsecond=0).isoformat()
        time_to = (
            occ.time_to.replace(microsecond=0).isoformat() if occ.time_to else None
        )

        rx_serial = occ.receiver_sn or synthetic_serial(
            "gnss_receiver", station_id, time_from
        )
        key = ("gnss_receiver", rx_serial, time_from[:10])
        if key in existing and not force:
            skipped += 1
            summaries.append(
                {
                    "time_from": time_from,
                    "time_to": time_to,
                    "status": "skipped (already present)",
                    "receiver_serial": rx_serial,
                }
            )
            continue

        summary: Dict[str, Any] = {
            "time_from": time_from,
            "time_to": time_to,
            "status": "created",
        }

        # --- receiver -------------------------------------------------------
        rx_model = validate_model("gnss_receiver", occ.receiver_type)
        # Device attributes (serial/model/owner/status/firmware) are intrinsic
        # to the entity and stay OPEN (date_to=None) — exactly as add_receiver /
        # add_antenna create them, and as move_device leaves them when retiring a
        # unit (it closes the JOIN and transitions status, never the identity
        # attrs). Only the station↔device join is time-bounded to the occupation.
        rx_attrs = build_required_attributes(rx_serial, rx_model, owner, time_from)
        if occ.vers:
            rx_attrs.append(
                {
                    "code": "firmware_version",
                    "value": occ.vers,
                    "date_from": time_from,
                    "date_to": None,
                }
            )
        rx_id = _ensure_device(w, "gnss_receiver", rx_attrs, rx_serial, force)
        summary["receiver"] = {
            "serial": rx_serial,
            "model": rx_model,
            "id_entity": rx_id,
        }
        if rx_id is not None:
            w.create_entity_connection(station_eid, rx_id, time_from, time_to)
            summary["receiver"]["joined"] = True

        # --- antenna --------------------------------------------------------
        ant_synthetic = not occ.antenna_sn
        ant_serial = occ.antenna_sn or synthetic_serial(
            "antenna", station_id, time_from
        )
        ant_model = validate_model("antenna", occ.antenna_type)
        ant_attrs = build_antenna_attributes(
            serial=ant_serial,
            model=ant_model,
            owner=owner,
            date_start=time_from,
            antenna_height=occ.antenna_height or None,
        )
        ant_id = _ensure_device(w, "antenna", ant_attrs, ant_serial, force)
        summary["antenna"] = {
            "serial": ant_serial,
            "model": ant_model,
            "synthetic_serial": ant_synthetic,
            "height": occ.antenna_height,
            "id_entity": ant_id,
        }
        if ant_id is not None:
            w.create_entity_connection(station_eid, ant_id, time_from, time_to)
            summary["antenna"]["joined"] = True

        # --- radome (only when present) ------------------------------------
        if occ.dome and occ.dome != "NONE":
            rad_model = validate_model("radome", occ.dome)
            rad_serial = synthetic_serial("radome", station_id, time_from)
            rad_attrs = build_required_attributes(
                rad_serial, rad_model, owner, time_from
            )
            rad_id = _ensure_device(w, "radome", rad_attrs, rad_serial, force)
            summary["radome"] = {"serial": rad_serial, "id_entity": rad_id}
            if rad_id is not None:
                w.create_entity_connection(station_eid, rad_id, time_from, time_to)
                summary["radome"]["joined"] = True

        # --- monument (optional — tripod over benchmark) -------------------
        if with_monument:
            mon_height = monument_height or occ.antenna_height or "0.0"
            mon_serial = synthetic_serial("monument", station_id, time_from)
            mon_attrs = build_monument_attributes(
                serial=mon_serial,
                owner=owner,
                date_start=time_from,
                monument_height=mon_height,
            )
            mon_id = _ensure_device(w, "monument", mon_attrs, mon_serial, force)
            summary["monument"] = {
                "serial": mon_serial,
                "height": mon_height,
                "id_entity": mon_id,
            }
            if mon_id is not None:
                w.create_entity_connection(station_eid, mon_id, time_from, time_to)
                summary["monument"]["joined"] = True

        created += 1
        summaries.append(summary)

    result.tos_changes["occupations"] = summaries
    result.tos_changes["summary"] = {
        "total": len(occupations),
        "created": created,
        "skipped": skipped,
    }
    logger.info(
        "import-campaigns %s: %d occupation(s) — %d created, %d skipped%s",
        station_id,
        len(occupations),
        created,
        skipped,
        " (dry-run)" if dry_run else "",
    )
    return result


def set_continuity(
    writer: Optional[TOSWriter] = None,
    *,
    station_id: str,
    from_date: str,
    value: str,
    correct_current: Optional[str] = None,
    dry_run: bool = True,
) -> OperationResult:
    """Transition a station's ``continuity`` classification at a date.

    Continuity is fluid: a station can move campaign → continuous and back over
    time. This closes the currently-open ``continuity`` period at ``from_date``
    and opens a new one with ``value`` (via
    :meth:`TOSWriter.transition_attribute_value`), preserving history.

    ``correct_current`` handles the case where the currently-open period has the
    *wrong* value (e.g. VOTT was created ``continuous`` from 2012 when that span
    was actually ``campaign``): the open period's value is first PATCHed to
    ``correct_current`` in place, *then* the transition closes it at
    ``from_date`` and opens ``value``. The net result is
    ``correct_current`` over the historical span and ``value`` from
    ``from_date`` onward.

    Args:
        writer: Configured :class:`TOSWriter`, or ``None`` to build one.
        station_id: 4-char station marker.
        from_date: Transition date (bare ``YYYY-MM-DD`` → noon, matching the
            other verbs). The close of the old period and open of the new.
        value: New continuity value (``"campaign"`` or ``"continuous"``).
        correct_current: When set, relabel the currently-open period to this
            value before transitioning (historical mislabel fix).
        dry_run: When ``True`` (default), no writes are sent.
    """
    if value not in _CONTINUITY_VALUES:
        raise CfgOperationError(
            f"continuity value must be one of {_CONTINUITY_VALUES}, got {value!r}"
        )
    if correct_current is not None and correct_current not in _CONTINUITY_VALUES:
        raise CfgOperationError(
            f"--correct-current must be one of {_CONTINUITY_VALUES}, "
            f"got {correct_current!r}"
        )

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    eff_date = _visit_default_time(from_date)

    result = OperationResult(
        operation="set-continuity",
        station_id=station_id,
        date=eff_date,
        dry_run=dry_run,
    )

    if correct_current is not None:
        existing = w.get_attribute_values(station_eid, _CONTINUITY_CODE)
        open_periods = [a for a in existing if a.get("date_to") is None]
        if open_periods:
            current = max(open_periods, key=lambda a: a.get("date_from") or "")
            id_av = current.get("id_attribute_value") or current.get("id")
            if id_av is not None and current.get("value") != correct_current:
                result.tos_changes["corrected_current"] = w.patch_attribute_value(
                    int(id_av), value=correct_current
                )

    result.tos_changes["transition"] = w.transition_attribute_value(
        station_eid, _CONTINUITY_CODE, value, eff_date
    )
    logger.info(
        "set-continuity %s: → %s from %s%s",
        station_id,
        value,
        eff_date,
        " (dry-run)" if dry_run else "",
    )
    return result


# Receiver ports have no TOS attribute code (CLAUDE.md scope note), so a new
# station section gets type-appropriate defaults. Keyed on the canonical
# stations.cfg short receiver_type.
_STATION_PORT_DEFAULTS: Dict[str, Dict[str, str]] = {
    "PolaRX5": {
        "receiver_ftpport": "2160",
        "receiver_httpport": "8060",
        "receiver_controlport": "28784",
    },
    "mosaic-X5": {
        "receiver_ftpport": "2160",
        "receiver_httpport": "8060",
        "receiver_controlport": "28784",
    },
    "NetRS": {"receiver_httpport": "8060"},
    "NetR9": {"receiver_httpport": "8060"},
    "NetR5": {"receiver_httpport": "8060"},
}

# Field order for a generated stations.cfg section (mirrors existing sections).
_STATION_CFG_ORDER: tuple[str, ...] = (
    "router_ip",
    "router_type",
    "receiver_type",
    "receiver_ftpport",
    "receiver_httpport",
    "receiver_controlport",
    "station_id",
    "connection_type",
    "station_name",
    "rinex_run_by",
    "rinex_observer",
    "rinex_agency",
    "station_owner",
    "rinex_marker_name",
    "rinex_marker_number",
    "antenna_serial",
    "antenna_type",
    "antenna_radome",
    "antenna_height",
    "rinex_config_valid_from",
    "latitude",
    "longitude",
    "height",
    "receiver_firmware_version",
    "receiver_serial",
)


def _short_router_type(model: Optional[str]) -> Optional[str]:
    """Map a TOS modem model to the stations.cfg ``router_type`` short form.

    ``"Teltonika RUT240"`` → ``"RUT240"``; an unknown model passes through so a
    new vendor isn't silently dropped.
    """
    if not model:
        return None
    for prefix in ("Teltonika ", "Teltonica "):
        if model.startswith(prefix):
            return model[len(prefix) :].strip()
    return model.strip()


def add_station(
    client: Optional[Any] = None,
    *,
    station_id: str,
    cfg_path: Optional[Path] = None,
    connection_type: str = "3G-radio",
    router_ip: Optional[str] = None,
    router_type: Optional[str] = None,
    rinex_run_by: str = "IMO",
    rinex_observer: str = "IMO",
    rinex_agency: str = "IMO",
    station_owner: str = "IMO",
    firmware: Optional[str] = None,
    dry_run: bool = True,
) -> OperationResult:
    """Scaffold a new ``stations.cfg`` section for a TOS station (TOS → cfg).

    The inverse of the ``add-*`` device verbs: those write a probed device *to*
    TOS; this reads a station that already exists in TOS (its open receiver /
    antenna / radome / monument / SIM / modem session) and materialises a
    ``[STID]`` section so the rek scheduler will monitor it.

    TOS-sourced fields reuse the ``cfg reconcile`` backend
    (:mod:`receivers.cfg.tos_adapter`) so the two never drift: receiver
    type/serial/firmware, antenna type/serial/radome, the composite
    antenna_height (antenna ARP + monument), and lat/lon/height/name. The SIM's
    ``ip_address`` → ``router_ip`` and the modem ``model`` → ``router_type`` are
    read directly (they aren't in the reconcile device map). Ports /
    connection_type have no TOS attribute code and are type-defaulted / flagged.

    Faithful, not laundering: a non-IGS antenna name or a zero antenna_height in
    TOS is copied through **with a warning**, so the operator sees the data-quality
    issue before it syncs to rek (it does not refuse).

    Args:
        client: A read-only ``TOSClient`` (duck-typed: needs
            ``get_complete_station_metadata`` + ``get_entity_history``), or
            ``None`` to build one.
        station_id: 4-char marker — the TOS station to materialise.
        cfg_path: Target stations.cfg (default: resolved deployed/repo path).
        connection_type: cfg ``connection_type`` (no TOS source; default 3G-radio).
        router_ip: Override the SIM-derived IP.
        router_type: Override the modem-derived router type.
        rinex_run_by / rinex_observer / rinex_agency / station_owner: RINEX
            metadata constants (no TOS source).
        firmware: Receiver firmware for cfg (TOS often lacks it for old units).
        dry_run: When ``True`` (default), the section is built and returned but
            not written.

    Returns:
        :class:`OperationResult` ``operation="add-station"``; the generated
        section is in ``cfg_changes`` and any data-quality warnings in
        ``tos_changes["warnings"]``.
    """
    from tostools.standards.igs_equipment import ANTENNA_IGS

    from ..config.receivers_config import create_station_section
    from . import tos_adapter

    if client is None:
        from tostools.api.tos_client import TOSClient

        client = TOSClient()

    station = client.get_complete_station_metadata(station_id)
    if not station:
        raise CfgOperationError(
            f"No TOS station found for marker {station_id!r} "
            f"(get_complete_station_metadata returned nothing)."
        )
    session = tos_adapter.current_session(station)
    if session is None:
        raise CfgOperationError(
            f"{station_id} has no open device session in TOS — install the "
            f"continuous receiver/antenna first (add-receiver/move-device, "
            f"add-antenna), then re-run add-station."
        )

    warnings: List[str] = []
    rx_type = _canonical_receiver_type(tos_adapter.current_receiver_model(station))
    antenna_type = tos_adapter.current_antenna_model(station)
    antenna_height = tos_adapter.current_antenna_height(station)

    # Telemetry: SIM ip_address → router_ip, modem model → router_type. Not in
    # the reconcile device map, so read the open children directly (TOSClient
    # duck-types _find_open_child via get_entity_history).
    eid = station.get("id_entity")
    if router_ip is None and eid is not None:
        sim_id = _find_open_child(client, int(eid), "sim_card")
        if sim_id is not None:
            sim_hist = client.get_entity_history(sim_id)
            if isinstance(sim_hist, dict):
                router_ip = _device_attribute(sim_hist, "ip_address")
    if router_type is None and eid is not None:
        modem_id = _find_open_child(client, int(eid), "modem_gsm")
        if modem_id is not None:
            modem_hist = client.get_entity_history(modem_id)
            if isinstance(modem_hist, dict):
                router_type = _short_router_type(_device_attribute(modem_hist, "model"))

    # rinex_config_valid_from = the open session's start (NB: may be the TOS
    # data-entry date, not the true continuous-install date). TOS hands time_from
    # back as a datetime via _build_history_from_connections; tolerate str too.
    _tf = session.get("time_from")
    if _tf is None:
        valid_from = None
    else:
        valid_from = (_tf.isoformat() if hasattr(_tf, "isoformat") else str(_tf))[:10]

    raw: Dict[str, Optional[str]] = {
        "router_ip": router_ip,
        "router_type": router_type,
        "receiver_type": rx_type,
        "station_id": station_id,
        "connection_type": connection_type,
        "station_name": tos_adapter.station_name(station),
        "rinex_run_by": rinex_run_by,
        "rinex_observer": rinex_observer,
        "rinex_agency": rinex_agency,
        "station_owner": station_owner,
        "rinex_marker_name": station_id,
        # MARKER NUMBER = IERS DOMES only (MARKER NAME carries the 4-char id).
        # None when the station has no DOMES → omitted below → no cfg line.
        "rinex_marker_number": tos_adapter.iers_domes_number(station),
        "antenna_serial": tos_adapter.current_antenna_serial(station),
        "antenna_type": antenna_type,
        "antenna_radome": tos_adapter.current_radome_model(station) or "NONE",
        "antenna_height": antenna_height,
        "rinex_config_valid_from": valid_from,
        "latitude": tos_adapter.station_latitude(station),
        "longitude": tos_adapter.station_longitude(station),
        "height": tos_adapter.station_height(station),
        "receiver_firmware_version": firmware
        or tos_adapter.current_receiver_firmware(station),
        "receiver_serial": tos_adapter.current_receiver_serial(station),
    }
    if rx_type:
        raw.update(_STATION_PORT_DEFAULTS.get(rx_type, {}))

    # Data-quality warnings — surface, don't refuse.
    if antenna_type and antenna_type not in ANTENNA_IGS:
        warnings.append(
            f"antenna_type {antenna_type!r} is not an IGS-recognised name — "
            f"RINEX headers will carry a non-standard antenna; verify in TOS."
        )
    try:
        if antenna_height is not None and float(antenna_height) == 0.0:
            warnings.append(
                "antenna_height is 0.0 (likely a TOS placeholder, not the "
                "surveyed mark→ARP height) — confirm before production RINEX."
            )
    except (TypeError, ValueError):
        pass
    if not router_ip:
        warnings.append(
            "router_ip could not be derived from the SIM in TOS — set it with "
            "--router-ip or the station cannot be reached."
        )
    if valid_from and valid_from >= "2025":
        warnings.append(
            f"rinex_config_valid_from={valid_from} is the receiver's TOS "
            f"session start — confirm it's the real continuous-install date."
        )

    fields = {k: str(raw[k]) for k in _STATION_CFG_ORDER if raw.get(k) is not None}

    result = OperationResult(
        operation="add-station",
        station_id=station_id,
        date=valid_from,
        dry_run=dry_run,
    )
    result.cfg_changes = fields
    result.tos_changes["warnings"] = warnings

    if not dry_run:
        target = _resolve_cfg_path(cfg_path)
        create_station_section(target, station_id, fields)
        result.tos_changes["cfg_path"] = str(target)

    for w in warnings:
        logger.warning("add-station %s: %s", station_id, w)
    logger.info(
        "add-station %s: %d fields%s",
        station_id,
        len(fields),
        " (dry-run)" if dry_run else f" → {result.tos_changes.get('cfg_path')}",
    )
    return result


def move_device(
    serial: Optional[str] = None,
    *,
    subtype: str = "gnss_receiver",
    id_entity: Optional[int] = None,
    with_radome: bool = True,
    to: str = DEFAULT_WAREHOUSE,
    date: Optional[str] = None,
    from_station: Optional[str] = None,
    firmware: Optional[str] = None,
    rinex_valid_from: Optional[str] = None,
    vitjun: Optional[str] = None,
    vitjun_remaining: Optional[str] = None,
    participants: str = "",
    device_status: Optional[str] = None,
    device_comment: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
    skip_vitjun: bool = False,
    skip_cfg: bool = False,
    _assume_cleared_device_id: Optional[int] = None,
) -> OperationResult:
    """Move a receiver to a new parent — station OR warehouse.

    Auto-detects ``to`` by type:

    * **Station marker** (4-char, e.g. ``"HRAC"``) — runs the full
      install workflow:

      1. Destination-displacement check: refuse if the station already
         has an open ``gnss_receiver`` child. Move the old one out first.
      2. TOS Pattern 2 move: close the device's current parent join at
         ``date``, open a new join to the station at the same date.
      3. Vitjun ("Breyting") on the destination station with auto-text
         derived from the receiver that just left (``Skipt um móttakara:
         <old> → <new>``); override via ``vitjun``.
      4. Update ``stations.cfg`` (``receiver_serial`` / ``receiver_type``
         / ``receiver_firmware_version`` / ``rinex_config_valid_from``)
         from the device's TOS attributes.

    * **Location name** (e.g. ``"B9 - Kjallari - Jörð"``, default) —
      bookkeeping-only move:

      1. TOS Pattern 2 move to the warehouse.
      2. Optional vitjun on the *source* station — only when ``vitjun``
         is given (no default text).
      3. No ``stations.cfg`` update (the source station's
         ``receiver_*`` fields will be overwritten by the next station
         move into it, or hand-edited if it's being decommissioned).

    Args:
        serial: Device serial number (must exist in TOS — warehouse new
            arrivals via ``receivers cfg add-receiver`` first).
        to: Destination — a 4-char station marker OR a location name
            as recorded in TOS. Defaults to the B9 warehouse.
        date: ISO date/datetime the move happened. Accepts
            ``YYYY-MM-DD`` (promoted to midnight). Default: today.
            Backdating freely supported.
        from_station: 4-char marker of the source station (transfer
            case). When given on a station→station transfer, sanity-
            checks the device is currently at this station.
        firmware: Optional override of the firmware string written to
            stations.cfg. Does not modify the TOS firmware_version
            attribute. Station destinations only.
        rinex_valid_from: Optional override of the
            ``rinex_config_valid_from`` cfg field (YYYY-MM-DD). Default:
            :func:`_default_rinex_valid_from` applied to ``date``.
            Station destinations only.
        vitjun: Free-text override for the vitjun "Framkvæmt" field.
            Default for station destinations: auto-derived from
            context. For location destinations: no vitjun unless this
            is set.
        vitjun_remaining: Optional "Útistandandi" text for the vitjun.
        participants: Comma-separated emails for the vitjun
            ``participants`` field.
        device_status: When given, runs Pattern-2 on the device's
            ``status`` attribute — closes the current open period at
            ``date`` and opens a new period with this value. Use to
            mark a unit broken (``"bilað"``) when moving it to a
            workshop, or active (``"virkt"``) when redeploying after
            repair. Old devices without an existing ``status`` get
            the value added (no close).
        device_comment: Same Pattern-2 transition for the device's
            ``comment`` attribute — preserves the old comment in
            history. Pass the full new comment text.
        dry_run: When True (default), TOS writes use
            :class:`DryRunResult` and stations.cfg is left alone.
        writer: Optional pre-built TOSWriter.
        cfg_path: Override the stations.cfg location.
        skip_vitjun: When True, skip the vitjun step entirely.
        skip_cfg: When True, skip the stations.cfg update (station
            destinations only).

    Returns:
        :class:`OperationResult`.

    Raises:
        CfgOperationError: When ``to`` resolves to neither a station
            marker nor a warehouse, when the destination station has
            an open receiver child, or when the serial is unknown.
    """
    w = _resolve_writer(writer, dry_run)
    # Default to noon (field-work convention): bare YYYY-MM-DD →
    # YYYY-MM-DDT12:00:00, None → today noon. Joins, attribute
    # transitions, and the vitjun all share this resolved timestamp
    # so a single --date applies consistently. Explicit
    # YYYY-MM-DDTHH:MM:SS lets the operator pin a specific time
    # (e.g. HRAC's swap at 23:00).
    eff_date = _visit_default_time(date)

    # If --serial omitted, infer from --from-station's most recently
    # closed gnss_receiver child. Workflow case: user removed a unit
    # from STATION_A on day 1 (it's now at B9), and the next day
    # transfers it to STATION_B without typing the serial — they only
    # remember "the receiver that came off STATION_A".
    if serial is None and id_entity is None:
        if from_station is None:
            raise CfgOperationError(
                "move_device: --serial, --id or --from-station is required."
            )
        from_eid_for_infer = _resolve_station(w, from_station)
        # Prefer currently-open receiver, fall back to most recently closed.
        inferred = _find_receiver_at_station(w, from_eid_for_infer)
        if inferred is None:
            raise CfgOperationError(
                f"--from-station {from_station}: no gnss_receiver is "
                f"currently joined to this station and none has ever "
                f"been closed off it — cannot infer --serial. Pass "
                f"--serial X explicitly or check the station marker."
            )
        device_hist = w.get_entity_history(inferred)
        inferred_serial = (
            _device_attribute(device_hist, "serial_number")
            if isinstance(device_hist, dict)
            else None
        )
        if not inferred_serial:
            raise CfgOperationError(
                f"--from-station {from_station}: most recent receiver to "
                f"leave (id_entity={inferred}) has no serial_number "
                f"attribute readable — pass --serial explicitly."
            )
        serial = inferred_serial
        logger.info(
            "move_device: inferred --serial %s from --from-station %s",
            serial,
            from_station,
        )

    # The radome is screwed onto the antenna, so it travels with it. Resolve
    # the companion BEFORE the move — afterwards the antenna's most recent join
    # is the new one and the shared-period evidence is gone.
    companion_radome: Optional[int] = None
    if subtype == "antenna" and with_radome:
        probe_id = id_entity
        if probe_id is None and serial:
            found = w.find_device_by_serial("antenna", str(serial))
            probe_id = int(found["id_entity"]) if found else None
        if probe_id is not None:
            companion_radome = _find_companion_radome(w, int(probe_id))

    # Auto-detect target type: station marker first, then location name.
    station_eid = w.find_station_by_marker(to)
    if station_eid is not None:
        station_result = _move_to_station(
            w,
            serial=serial,
            subtype=subtype,
            id_entity=id_entity,
            station_id=to,
            station_eid=station_eid,
            eff_date=eff_date,
            from_station=from_station,
            firmware=firmware,
            rinex_valid_from=rinex_valid_from,
            vitjun=vitjun,
            vitjun_remaining=vitjun_remaining,
            participants=participants,
            device_status=device_status,
            device_comment=device_comment,
            dry_run=dry_run,
            cfg_path=cfg_path,
            skip_vitjun=skip_vitjun,
            skip_cfg=skip_cfg,
            assume_cleared_device_id=_assume_cleared_device_id,
        )
        return _follow_with_radome(
            w,
            station_result,
            companion_radome,
            to=to,
            date=date,
            dry_run=dry_run,
        )

    # Locations: try the warehouse subtype first (the common case), then
    # fall back to any non-station location so legitimate non-warehouse
    # entities (calibration labs, external storage, future subtypes) are
    # reachable without forcing the operator to learn the type system.
    location_eid = w.find_location_by_name(to, type_filter="vöruhús")
    if location_eid is None:
        # Empty string disables the filter per find_location_by_name's
        # documented contract (any location_eid subtype).
        location_eid = w.find_location_by_name(to, type_filter="")
    if location_eid is not None:
        # Suppress the source-station cfg-clear when chained from
        # replace_receiver (its install-new step writes the new cfg).
        # Also suppress when the caller passed --no-cfg (umbrella).
        skip_clear = skip_cfg or _assume_cleared_device_id is not None
        location_result = _move_to_location(
            w,
            serial=serial,
            subtype=subtype,
            id_entity=id_entity,
            location_name=to,
            location_eid=location_eid,
            eff_date=eff_date,
            vitjun=vitjun,
            vitjun_remaining=vitjun_remaining,
            participants=participants,
            device_status=device_status,
            device_comment=device_comment,
            dry_run=dry_run,
            skip_vitjun=skip_vitjun,
            cfg_path=cfg_path,
            skip_clear_cfg=skip_clear,
        )
        return _follow_with_radome(
            w,
            location_result,
            companion_radome,
            to=to,
            date=date,
            dry_run=dry_run,
        )

    raise CfgOperationError(
        f"--to {to!r} resolves to neither a station marker (type 'stöð') "
        f"nor any TOS location entity. Check spelling, or use the "
        f"full TOS-recorded name."
    )


def _move_to_station(
    w: TOSWriter,
    *,
    serial: Optional[str],
    station_id: str,
    station_eid: int,
    eff_date: str,
    from_station: Optional[str],
    firmware: Optional[str],
    rinex_valid_from: Optional[str],
    vitjun: Optional[str],
    vitjun_remaining: Optional[str],
    participants: str,
    device_status: Optional[str],
    device_comment: Optional[str],
    dry_run: bool,
    cfg_path: Optional[Path],
    skip_vitjun: bool,
    skip_cfg: bool,
    subtype: str = "gnss_receiver",
    id_entity: Optional[int] = None,
    assume_cleared_device_id: Optional[int] = None,
) -> OperationResult:
    """Station-destination path of :func:`move_device`.

    ``assume_cleared_device_id`` is an internal escape hatch for
    chained orchestration (:func:`replace_receiver`): when the caller
    has just (or is about to) close an open receiver join at this
    station, pass the device id so the displacement check treats it
    as already-resolved. **Honored in dry-run only** — in live mode
    the real TOS state must already reflect the close (step 2 must
    have actually landed); a still-open join is treated as a hard
    error to avoid creating two simultaneously-open receiver children
    on the station when ``--continue-from install-new`` is used after
    a partial step-2 failure.
    """
    open_existing = _find_open_child(w, station_eid, subtype)
    # The dry-run-only escape hatch: when replace_receiver previews
    # step 3 before step 2 has written, accept the assume-cleared id
    # so the preview is not blocked by its own simulated state.
    effective_open = (
        None
        if (dry_run and open_existing == assume_cleared_device_id)
        else open_existing
    )
    if effective_open is not None:
        raise CfgOperationError(
            f"{station_id} already has an open {subtype} child "
            f"(id_entity={effective_open}). Move the old one out "
            f"first: `receivers cfg move-device --subtype {subtype} "
            f"--serial <SERIAL>` (defaults to B9 warehouse) or "
            f"`… --to <ELSE>`."
        )

    device = _resolve_device_for_move(
        w, subtype=subtype, serial=serial, id_entity=id_entity
    )
    if device is None:  # pragma: no cover - resolver raises instead
        raise CfgOperationError(
            f"No {subtype} in TOS with serial {serial!r}. "
            f"If this is a new unit, warehouse it first with "
            f"`receivers cfg add-receiver`."
        )
    device_id = int(device["id_entity"])
    serial = serial or _device_attribute(device, "serial_number")
    new_model = _device_attribute(device, "model")
    new_firmware = _device_attribute(device, "firmware_version")

    # NB: --from-station is metadata at this layer (used for serial
    # inference and the auto-vitjun "from X" wording). We deliberately
    # do NOT pass it as from_id_entity to TOSWriter.move_device — that
    # would fail the in-transit case (receiver already at B9 between
    # the swap-out and swap-in). TOSWriter auto-detects the actual
    # current parent and closes that join correctly.
    move = w.move_device(device_id, station_eid, eff_date)

    result = OperationResult(
        operation="move",
        station_id=station_id,
        serial=serial,
        date=eff_date,
        tos_changes={"move": move},
        dry_run=dry_run,
    )

    # _auto_vitjun_text is receiver-worded ("Skipt um móttakara"). For other
    # subtypes the operator must supply --vitjun; auto-generating receiver
    # wording for an antenna move would write a false record.
    if not skip_vitjun and (subtype == "gnss_receiver" or vitjun):
        work = vitjun or _auto_vitjun_text(
            w, station_eid, device, eff_date, from_station=from_station
        )
        vit = w.add_maintenance_visit(
            station_eid,
            start_time=eff_date,
            maintenance_type="on_site",
            participants=participants,
            reasons=["change"],
            work=work,
            remaining=vitjun_remaining,
        )
        result.tos_changes["vitjun"] = vit
        result.vitjun_id = vit.get("id_maintenance")

    _apply_device_attribute_transitions(
        w,
        device_id,
        eff_date,
        device_status=device_status,
        device_comment=device_comment,
        result=result,
    )

    # stations.cfg's receiver_* keys describe a receiver; installing an
    # antenna/modem/SIM must not write them. Those subtypes have their own
    # verbs (replace-antenna / replace-modem / replace-sim) for the cfg side.
    if not skip_cfg and not dry_run and subtype == "gnss_receiver":
        target_cfg = _resolve_cfg_path(cfg_path)
        cfg_updates: Dict[str, Optional[str]] = {
            "receiver_serial": serial,
            "receiver_type": _canonical_receiver_type(new_model),
            "receiver_firmware_version": firmware or new_firmware,
            "rinex_config_valid_from": (
                rinex_valid_from or _default_rinex_valid_from(eff_date)
            ),
        }
        result.cfg_changes = _apply_cfg_updates(target_cfg, station_id, cfg_updates)
    return result


def _follow_with_radome(
    w: TOSWriter,
    result: OperationResult,
    radome_id: Optional[int],
    *,
    to: str,
    date: Optional[str],
    dry_run: bool,
) -> OperationResult:
    """Send the antenna's companion radome to the same destination.

    Physically the two are one assembly; TOS keeps them as separate sibling
    children, so without this the radome silently stays behind at the station
    the antenna just left. The radome move carries no vitjun and no cfg write —
    the antenna move already recorded the event, and a second vitjun would
    double-report one action.
    """
    if radome_id is None:
        return result
    result.tos_changes["companion_radome"] = {
        "id_entity": radome_id,
        "moved_to": to,
        "result": move_device(
            subtype="radome",
            id_entity=radome_id,
            with_radome=False,
            to=to,
            date=date,
            writer=w,
            dry_run=dry_run,
            skip_vitjun=True,
            skip_cfg=True,
        ).tos_changes.get("move"),
    }
    return result


def _find_companion_radome(w: TOSWriter, antenna_id: int) -> Optional[int]:
    """Find the radome that shared this antenna's station join.

    TOS models antenna and radome as SIBLING children of the station, with no
    link between them — but physically the radome is screwed onto the antenna
    and the pair moves as one. The join period is the only evidence of the
    pairing: the companion is the radome whose join to the same parent ends at
    the same instant as the antenna's (or is likewise open).

    Returns ``None`` when the pairing is not unambiguous — no radome, or more
    than one candidate. Guessing which of two radomes travelled with an antenna
    would write a location claim that cannot be distinguished from a real one.
    """
    hist = w._request("GET", f"/entity/parent_history/{int(antenna_id)}")
    if not isinstance(hist, list) or not hist:
        return None
    join = max(hist, key=lambda j: j.get("time_from") or "")
    parent = join.get("id_entity_parent")
    if parent is None:
        return None
    antenna_time_to = join.get("time_to")
    parent_hist = w.get_entity_history(int(parent))
    if not isinstance(parent_hist, dict):
        return None
    matches: List[int] = []
    for child in parent_hist.get("children_connections") or []:
        if child.get("time_to") != antenna_time_to:
            continue
        cid = child.get("id_entity_child")
        if cid is None:
            continue
        child_hist = w.get_entity_history(int(cid))
        if (
            isinstance(child_hist, dict)
            and child_hist.get("code_entity_subtype") == "radome"
        ):
            matches.append(int(cid))
    return matches[0] if len(matches) == 1 else None


def _resolve_device_for_move(
    w: TOSWriter,
    *,
    subtype: str,
    serial: Optional[str],
    id_entity: Optional[int],
) -> Dict[str, Any]:
    """Resolve the device a move targets, by id_entity or by serial.

    ``id_entity`` is the reliable selector and exists because TOS's
    ``POST /basic_search/`` — the only serial index available, and what
    :meth:`TOSWriter.find_device_by_serial` is built on — is fuzzy and has been
    observed to MISS an exact, currently-open serial (antenna id 4527 carries
    serial_number "262509" with date_to=None, yet a search for "262509" returns
    seven unrelated hits and not that one). Any workflow that can only address a
    device by serial is therefore one stale index entry away from "device not
    found" — or, on a create-or-reuse path, from silently creating a duplicate.
    Reading ``GET /entity/{id}/`` bypasses the index entirely.

    Raises:
        CfgOperationError: when neither/both selectors are given, when the id
            does not exist, or when the resolved entity is not ``subtype``.
    """
    if (serial is None) == (id_entity is None):
        raise CfgOperationError(
            "move-device: pass exactly one of --serial or --id (--id is exact; "
            "--serial goes through TOS's fuzzy search and can miss)."
        )
    if id_entity is not None:
        entity = w._request("GET", f"/entity/{int(id_entity)}/")
        if not isinstance(entity, dict) or not entity.get("id_entity"):
            raise CfgOperationError(f"No TOS entity with id_entity={id_entity}.")
        found = entity.get("code_entity_subtype")
        if found != subtype:
            raise CfgOperationError(
                f"id_entity={id_entity} is a {found!r}, not a {subtype!r}. "
                f"Pass --subtype {found} if that is what you meant to move."
            )
        return entity
    device: Optional[Dict[str, Any]] = w.find_device_by_serial(subtype, str(serial))
    if device is None:
        raise CfgOperationError(
            f"No {subtype} in TOS with serial {serial!r}. Note TOS's serial "
            f"search is fuzzy and can miss an existing device — if you know it "
            f"exists, address it exactly with `--id <ID_ENTITY>` "
            f"(`tos station show <STID> --all` lists ids for closed joins)."
        )
    return device


def _move_to_location(
    w: TOSWriter,
    *,
    serial: Optional[str],
    location_name: str,
    location_eid: int,
    eff_date: str,
    subtype: str = "gnss_receiver",
    id_entity: Optional[int] = None,
    vitjun: Optional[str],
    vitjun_remaining: Optional[str],
    participants: str,
    device_status: Optional[str],
    device_comment: Optional[str],
    dry_run: bool,
    skip_vitjun: bool,
    cfg_path: Optional[Path] = None,
    skip_clear_cfg: bool = False,
) -> OperationResult:
    """Location-destination (bookkeeping) path of :func:`move_device`."""
    device = _resolve_device_for_move(
        w, subtype=subtype, serial=serial, id_entity=id_entity
    )
    device_id = int(device["id_entity"])
    serial = serial or _device_attribute(device, "serial_number")

    open_join = w.get_open_parent_join(device_id)
    source_eid = open_join.get("id_entity_parent") if open_join else None

    move = w.move_device(device_id, location_eid, eff_date)

    result = OperationResult(
        operation="move",
        serial=serial,
        date=eff_date,
        tos_changes={"move": move, "to_location": location_name},
        dry_run=dry_run,
    )

    if not skip_vitjun and source_eid is not None and vitjun is not None:
        # Only write vitjun on a location move when caller explicitly
        # supplied --vitjun text (location moves don't auto-write).
        vit = w.add_maintenance_visit(
            int(source_eid),
            start_time=eff_date,
            maintenance_type="on_site",
            participants=participants,
            reasons=["change"],
            work=vitjun,
            remaining=vitjun_remaining,
        )
        result.tos_changes["vitjun"] = vit
        result.vitjun_id = vit.get("id_maintenance")

    _apply_device_attribute_transitions(
        w,
        device_id,
        eff_date,
        device_status=device_status,
        device_comment=device_comment,
        result=result,
    )

    # Auto-clear stations.cfg when the device just left a station with no
    # immediate replacement. Suppressed when chained from replace_receiver
    # (the install-new step will overwrite the cfg anyway).
    # The auto-clear NONEs stations.cfg's receiver_* keys, so it only makes
    # sense for a receiver. An antenna/radome/modem leaving a station must not
    # blank the receiver fields.
    if (
        not skip_clear_cfg
        and not dry_run
        and subtype == "gnss_receiver"
        and source_eid is not None
        and source_eid != location_eid  # not a warehouse-to-warehouse move
    ):
        source_marker = _marker_for_entity(w, int(source_eid))
        if source_marker:
            target_cfg = _resolve_cfg_path(cfg_path)
            cleared = _clear_station_receiver_cfg(target_cfg, source_marker, eff_date)
            if cleared:
                result.cfg_changes = cleared

    return result


def _marker_for_entity(w: TOSWriter, eid: int) -> Optional[str]:
    """Look up the ``marker`` attribute value on a *station* entity.

    Returns the 4-char RINEX marker iff the entity is a station
    (``code_entity_subtype == "stöð"``) and has a marker attribute,
    else None. The subtype guard prevents the cfg auto-clear path
    from NONE-ing an unrelated station section when the source entity
    is some non-station container that happens to carry a ``marker``
    attribute (admin-tagged grouping, future TOS schema).
    """
    hist = w.get_entity_history(eid)
    if not isinstance(hist, dict):
        return None
    if hist.get("code_entity_subtype") != "stöð":
        return None
    marker = _device_attribute(hist, "marker")
    return marker.upper() if marker else None


def _clear_station_receiver_cfg(
    cfg_path: Path,
    station_id: str,
    eff_date: str,
) -> Dict[str, str]:
    """Set the four receiver_* keys on a station section to the canonical
    "empty" sentinel so Grafana / scheduler auto-detection picks up the
    station as inactive.

    Uses ``NONE`` (uppercase) — matches the existing ``antenna_radome =
    NONE`` convention. The receivers scheduler's "None/empty/unknown"
    auto-inactive check (per receivers CLAUDE.md) accepts this.

    ``rinex_config_valid_from`` uses the same "first full day of new
    config" rule as :func:`_default_rinex_valid_from` so an install
    immediately afterwards (with the same ``eff_date``) lands on the
    same day — preventing a one-day ambiguity window where the cfg
    claims the OLD config ends one day and the NEW config starts the
    next.

    Returns the subset of fields that actually changed (skipping no-ops
    where the value was already NONE).
    """
    cfg_updates: Dict[str, Optional[str]] = {
        "receiver_type": "NONE",
        "receiver_serial": "NONE",
        "receiver_firmware_version": "NONE",
        "rinex_config_valid_from": _default_rinex_valid_from(eff_date),
    }
    return _apply_cfg_updates(cfg_path, station_id, cfg_updates)


def _apply_device_attribute_transitions(
    w: TOSWriter,
    device_id: int,
    eff_date: str,
    *,
    device_status: Optional[str],
    device_comment: Optional[str],
    result: OperationResult,
) -> None:
    """Apply optional Pattern-2 transitions on device attributes.

    Used after a move to record a status change (e.g. ``virkt`` →
    ``bilað`` when a broken unit goes to a workshop) and/or a comment
    update. The transition closes any existing open period at
    ``eff_date`` and opens a new one with the new value. When no open
    period exists (some older fleet devices have no ``status``
    attribute at all), the new value is simply added with the same
    date.

    Writes the responses into ``result.tos_changes[...]`` keys
    ``device_status`` / ``device_comment`` so the caller's
    OperationResult reflects the work.

    Empty strings (``""``) are treated as "skip" — matching the CLI
    help text for ``--old-status ""`` / ``--old-comment ""``. Pass
    ``None`` or ``""`` to leave the attribute untouched.
    """
    if device_status:
        result.tos_changes["device_status"] = w.transition_attribute_value(
            device_id, "status", device_status, eff_date
        )
    if device_comment:
        result.tos_changes["device_comment"] = w.transition_attribute_value(
            device_id, "comment", device_comment, eff_date
        )


# ---------------------------------------------------------------------------
# Install-attribute fill (station-install post-step)
# ---------------------------------------------------------------------------

#: Installation attributes filled on a station install. v1 is the position
#: group only — the only install-time fields that (a) have a real TOS
#: attribute code, (b) are sourced from stations.cfg, and (c) belong to the
#: station entity (so they survive a receiver swap). Receiver-derived attrs
#: (sampling_interval, FTP/HTTP/CTRL ports, ip_address) have **no** TOS
#: attribute code — there is nowhere to write them — so they are out of
#: scope here; see receivers todo #28 for the descope rationale. Antenna /
#: monument / radome attrs belong to the future ``cfg replace-antenna`` /
#: ``replace-radome`` verbs (todo #21), not a receiver move.
INSTALL_POSITION_FIELDS: tuple[str, ...] = ("latitude", "longitude", "height")


@dataclass
class InstallAttrProposal:
    """One proposed install-attribute write, handed to a confirm callback.

    The cfg value is always the value we propose to write to TOS — on
    install, stations.cfg (surveyed coordinates) is the ground truth and
    TOS is being populated/aligned from it (the inverse of ``cfg
    reconcile``, which treats TOS as authoritative for cfg).
    """

    cfg_key: str
    label: str
    cfg_value: str  # value proposed for the TOS write (cfg is ground truth)
    tos_value: Optional[str]  # current open TOS value, or None if absent
    differs: bool  # True when TOS already has a *different* value
    spec: Any  # FieldSpec (avoids a circular import at module load)


def fill_install_attributes(
    writer: TOSWriter,
    station_id: str,
    station_config: Dict[str, Any],
    tos_data: Optional[Dict[str, Any]],
    eff_date: str,
    *,
    confirm: Callable[[InstallAttrProposal], str],
    fields: Sequence[str] = INSTALL_POSITION_FIELDS,
    position_tolerance_m: float = 2.0,
) -> Dict[str, str]:
    """Fill station install attributes in TOS from stations.cfg, with confirm.

    For each field in ``fields`` that stations.cfg has a value for, compare
    against the current open TOS value and, when a write is warranted, ask
    ``confirm`` what to do. The caller's ``confirm`` callback owns all
    interaction (prompts, dry-run previews, ``--yes`` / ``--change`` /
    ``--correct`` policy) and returns one of:

    * ``"add"`` / ``"correct"`` — Pattern 1 upsert (write the open value).
      ``"add"`` is the natural choice when TOS has no value yet; ``"correct"``
      fixes a wrong existing value in place (no history).
    * ``"change"`` — Pattern 2 transition (close the open period at
      ``eff_date``, open a new one). Records history.
    * ``"skip"`` — leave TOS untouched for this field.

    Fields where stations.cfg is empty, or where TOS already matches cfg
    (within ``position_tolerance_m`` for the position group), are no-ops and
    ``confirm`` is never called for them.

    Dry-run is governed by the ``writer`` (a dry-run ``TOSWriter`` turns every
    push into a no-op ``DryRunResult``); this function does not branch on it.

    Returns a ``{cfg_key: outcome}`` map for the caller's summary, where
    outcome is ``"unchanged"``, ``"skipped"``, or a short
    ``"<verb>→<value>"`` description.

    Raises:
        CfgOperationError: when ``tos_data`` is missing its ``id_entity`` (no
            resolvable station entity to write to).
    """
    # Local imports keep operations.py import-light and avoid a load-time
    # cycle (reconciler imports field_manifest which is fine, but tos_push
    # imports back into this package's typing surface).
    from .field_manifest import with_position_tolerance
    from .reconciler import compare_station
    from .tos_push import push_field_to_tos, push_field_transition_to_tos

    if not tos_data or tos_data.get("id_entity") is None:
        raise CfgOperationError(
            f"fill_install_attributes: TOS has no resolvable station entity "
            f"for {station_id!r} (missing id_entity) — cannot write install "
            f"attributes."
        )

    specs = with_position_tolerance(position_tolerance_m)
    diffs = compare_station(
        station_id=station_id,
        station_config=station_config,
        receiver_identity=None,
        tos_data=tos_data,
        fields=list(fields),
        queried_sources={"cfg", "tos"},
        field_specs=specs,
    )

    changes: Dict[str, str] = {}
    for d in diffs:
        if d.cfg_value is None:
            # Nothing in stations.cfg to install for this field.
            continue
        differs = d.tos_value is not None and not d.spec.values_equal(
            d.cfg_value, d.tos_value
        )
        if d.tos_value is not None and not differs:
            changes[d.cfg_key] = "unchanged"
            continue

        proposal = InstallAttrProposal(
            cfg_key=d.cfg_key,
            label=d.label,
            cfg_value=d.cfg_value,
            tos_value=d.tos_value,
            differs=differs,
            spec=d.spec,
        )
        action = confirm(proposal)
        if action == "skip":
            changes[d.cfg_key] = "skipped"
            continue
        if action in ("add", "correct"):
            push_field_to_tos(
                writer=writer,
                spec=d.spec,
                value=d.cfg_value,
                tos_data=tos_data,
                date_from=eff_date,
            )
            changes[d.cfg_key] = f"upsert→{d.cfg_value}"
        elif action == "change":
            push_field_transition_to_tos(
                writer=writer,
                spec=d.spec,
                new_value=d.cfg_value,
                old_value=str(d.tos_value),
                tos_data=tos_data,
                transition_date=eff_date,
            )
            changes[d.cfg_key] = f"transition→{d.cfg_value}"
        else:
            raise CfgOperationError(
                f"fill_install_attributes: confirm() returned unknown action "
                f"{action!r} for {d.cfg_key!r} (expected add/correct/change/skip)."
            )
    return changes


def delete_join(
    id_connection: int,
    *,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Delete a single ``entity_connection`` row by id.

    Admin-level destructive operation — no undo on TOS. Use only to
    clean up known-bad rows such as zero-duration orphans left over
    from historical add-device workflows.

    To find the right id, query ``/entity/parent_history/{id_child}``
    and pick the row whose ``time_from == time_to`` (or whatever shape
    you've decided is junk). Never delete a row without inspecting it
    first.

    Args:
        id_connection: ``id`` of the join row to delete.
        dry_run: When True (default), logs the DELETE without sending.
        writer: Optional pre-built TOSWriter.

    Returns:
        :class:`OperationResult` with ``operation='delete-join'`` and
        ``tos_changes={'id_connection': N, 'deleted': <response>}``.
    """
    w = _resolve_writer(writer, dry_run)
    resp = w.delete_entity_connection(id_connection)
    return OperationResult(
        operation="delete-join",
        date=None,
        tos_changes={"id_connection": id_connection, "deleted": resp},
        dry_run=dry_run,
    )


def delete_visit(
    id_maintenance: int,
    *,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Delete a single vitjun (maintenance record) by id.

    Admin-level destructive operation — no undo on TOS. Use only to clean up
    known-bad records such as a vitjun created by accident. To preserve the
    history for a visit that genuinely happened, prefer :func:`update_visit`
    with ``completed=True`` (mark done but keep the record).

    To find the right id, list the station's vitjun records with
    :func:`list_visits` (or ``receivers cfg visit --station SID --history id``)
    and identify the bad one by date / work text. Never delete without
    inspecting first.

    Args:
        id_maintenance: ``id_maintenance`` of the vitjun to delete.
        dry_run: When True (default), logs the DELETE without sending.
        writer: Optional pre-built TOSWriter.

    Returns:
        :class:`OperationResult` with ``operation='delete-visit'``,
        ``vitjun_id=id_maintenance`` and
        ``tos_changes={'id_maintenance': N, 'deleted': <response>}``.
    """
    w = _resolve_writer(writer, dry_run)
    resp = w.delete_maintenance(id_maintenance)
    return OperationResult(
        operation="delete-visit",
        date=None,
        tos_changes={"id_maintenance": id_maintenance, "deleted": resp},
        vitjun_id=id_maintenance,
        dry_run=dry_run,
    )


def add_visit(
    station_id: str,
    *,
    work: str,
    date: Optional[str] = None,
    end_time: Optional[str] = None,
    maintenance_type: str = "on_site",
    reasons: Optional[List[str]] = None,
    comment: Optional[str] = None,
    remaining: Optional[str] = None,
    participants: str = "",
    completed: bool = True,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Add a standalone vitjun on ``station_id`` — no equipment change.

    Wraps :meth:`TOSWriter.add_maintenance_visit` after resolving the
    station marker to an ``id_entity``. Used for maintenance visits
    that don't trigger a join change: antenna-cable repair, environment
    cleanup, remote configuration tweak, etc.

    Args:
        station_id: 4-char marker of the station visited.
        work: "Framkvæmt" / "Vinna" — what was done. Required (a vitjun
            without a work description is rarely useful).
        date: ISO start time. Default: today midnight.
        end_time: ISO end time. Default: same as ``date``.
        maintenance_type: ``"on_site"`` (Staðarvitjun) or ``"remote"``
            (Fjarvitjun).
        reasons: Subset of
            ``{"change", "repairs", "inspection", "improvements",
              "other"}``. Default: ``["repairs"]`` (Viðgerð).
        comment: "Athugasemdir".
        remaining: "Útistandandi".
        participants: Comma-separated emails.
        completed: Whether the visit is closed. Default True.
        dry_run / writer: As :func:`install_device`.

    Returns:
        :class:`OperationResult` with ``vitjun_id`` set on live writes
        (or ``"<dry-run>"`` in dry-run).
    """
    w = _resolve_writer(writer, dry_run)
    eff_date = _visit_default_time(date)
    eff_end = _visit_default_end_time(end_time)

    station_eid = _resolve_station(w, station_id)

    vit = w.add_maintenance_visit(
        station_eid,
        start_time=eff_date,
        end_time=eff_end,
        maintenance_type=maintenance_type,
        participants=participants,
        reasons=reasons or ["repairs"],
        work=work,
        comment=comment,
        remaining=remaining,
        completed=completed,
    )
    return OperationResult(
        operation="visit",
        station_id=station_id,
        date=eff_date,
        tos_changes={"vitjun": vit},
        vitjun_id=vit.get("id_maintenance"),
        dry_run=dry_run,
    )


def show_visit(
    id_maintenance: int,
    *,
    writer: Optional[TOSWriter] = None,
) -> Dict[str, Any]:
    """Return the full detail of a single vitjun record.

    Read-only; no dry-run distinction. Returns the raw TOS dict from
    :meth:`TOSWriter.get_maintenance_visit`, which includes
    ``maintenance_attribute_values`` rows with their per-attribute IDs.

    Args:
        id_maintenance: ``id_maintenance`` of the visit to fetch.
        writer: Optional pre-built TOSWriter.

    Raises:
        CfgOperationError: If the id is unknown to TOS.
    """
    w = _resolve_writer(writer, dry_run=True)
    detail = w.get_maintenance_visit(id_maintenance)
    if not detail:
        raise CfgOperationError(
            f"No vitjun in TOS with id_maintenance={id_maintenance}."
        )
    return detail


def list_visits(
    station_id: str,
    *,
    writer: Optional[TOSWriter] = None,
) -> List[Dict[str, Any]]:
    """List all vitjun records on a station, oldest-first.

    Read-only; no dry-run distinction. Returns the flat web-UI shape
    used by :meth:`TOSWriter.list_maintenance_visits` (``id``,
    ``maintenance_type``, ``maintenance_type_is``, ``reason``,
    ``start_time``, ``end_time``, ``participants``,
    ``participants_names``, ``work``, ``remaining``, ``completed``).

    Args:
        station_id: 4-char RINEX marker.
        writer: Optional pre-built TOSWriter.

    Raises:
        CfgOperationError: When the station marker doesn't resolve.
    """
    w = _resolve_writer(writer, dry_run=True)
    station_eid = _resolve_station(w, station_id)
    return w.list_maintenance_visits(station_eid)


def update_visit(
    id_maintenance: int,
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    participants: Optional[str] = None,
    completed: Optional[bool] = None,
    reasons: Optional[List[str]] = None,
    work: Optional[str] = None,
    comment: Optional[str] = None,
    remaining: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Edit an existing vitjun in place; preserve fields you don't pass.

    Wraps :meth:`TOSWriter.update_maintenance_visit`. Any argument
    left as ``None`` keeps the current TOS value; an explicit empty
    string (``""``) clears the field.

    Args:
        id_maintenance: ``id_maintenance`` of the visit to edit.
        start_time / end_time / participants / completed / reasons /
        work / comment / remaining: New values; ``None`` preserves.
            ``reasons`` is a *replacement* set (passing it overwrites
            all reason booleans, not just one).
        dry_run / writer: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``vitjun_id=id_maintenance`` and
        ``tos_changes={'update': <writer_response>}``.
    """
    w = _resolve_writer(writer, dry_run)
    # Promote bare YYYY-MM-DD dates: start → noon (workday convention),
    # end → end-of-day (so a same-day edit with an afternoon start
    # doesn't land an end-time before start).
    norm_start = _visit_default_time(start_time) if start_time is not None else None
    norm_end = _visit_default_end_time(end_time)
    resp = w.update_maintenance_visit(
        id_maintenance,
        start_time=norm_start,
        end_time=norm_end,
        participants=participants,
        completed=completed,
        reasons=reasons,
        work=work,
        comment=comment,
        remaining=remaining,
    )
    return OperationResult(
        operation="visit-edit",
        date=None,
        tos_changes={"update": resp},
        vitjun_id=id_maintenance,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# replace_receiver — one-shot warehouse + retire + install
# ---------------------------------------------------------------------------


REPLACE_STEPS = ("warehouse", "move-old", "install-new")


def _b9_eid(w: TOSWriter, warehouse: Optional[str] = None) -> int:
    """Resolve the transit-warehouse ``id_entity`` once per replace operation.

    ``warehouse`` overrides :data:`DEFAULT_WAREHOUSE` (which itself is
    read from ``[tos] default_warehouse`` in receivers.cfg or falls
    back to the hardcoded B9 name). Allows operators to point
    ``replace-receiver`` at a different transit location without
    editing the config file (e.g. for a one-off swap routed through
    a calibration lab).
    """
    name = warehouse or DEFAULT_WAREHOUSE
    eid = w.find_location_by_name(name, type_filter="vöruhús")
    if eid is None:
        # Fall back to any location subtype so non-vöruhús transit
        # locations are reachable via --warehouse.
        eid = w.find_location_by_name(name, type_filter="")
    if eid is None:
        raise CfgOperationError(
            f"Could not resolve warehouse {name!r} in TOS — pass "
            f"--warehouse with the exact TOS-recorded location name, "
            f"or set [tos] default_warehouse in receivers.cfg."
        )
    return int(eid)


def _resolve_old_receiver(w: TOSWriter, station_eid: int) -> tuple[int, str]:
    """Find the currently-open gnss_receiver at a station; return (id, serial).

    Raises ``CfgOperationError`` when no open receiver exists (nothing to
    replace) or when its serial is unreadable.
    """
    old_id = _find_open_gnss_receiver_child(w, station_eid)
    if old_id is None:
        raise CfgOperationError(
            "Station has no currently-open gnss_receiver child — there is "
            "nothing to replace. Did the swap already happen, or did the "
            "previous unit already get moved out?"
        )
    old_hist = w.get_entity_history(old_id)
    old_serial = (
        _device_attribute(old_hist, "serial_number")
        if isinstance(old_hist, dict)
        else None
    )
    if not old_serial:
        raise CfgOperationError(
            f"Old receiver (id_entity={old_id}) has no readable serial_number "
            f"attribute — refusing to replace without identifying it first."
        )
    return old_id, old_serial


def _validate_marker_match(
    probed_marker: Optional[str],
    station_id: str,
) -> None:
    """Ensure the probed receiver's marker matches the destination station.

    Acceptable: ``None`` (probe couldn't read marker), the literal
    ``"TEST"`` (bench default that should be auto-corrected — handled
    later), or an exact case-insensitive match for ``station_id``.

    Any other value indicates a potential misinstall (the receiver was
    configured for a different station) — refuse the replace.
    """
    if probed_marker is None:
        return
    pm = probed_marker.strip().upper()
    if pm in ("TEST", station_id.upper()):
        return
    raise CfgOperationError(
        f"Receiver's RINEX marker_name is {probed_marker!r} — expected "
        f"{station_id.upper()!r} (the destination) or 'TEST' (the "
        f"bench default). Refusing to replace: the unit may be "
        f"configured for a different station, or you typed the wrong "
        f"--station. Verify physically or pass --skip-marker-check to "
        f"override."
    )


@dataclass
class ArchiveReceiverDate:
    """Result of deriving a receiver install date from the RINEX archive.

    Attributes:
        install_date: ISO datetime (``…T00:00:00``) of the current physical
            receiver's first appearance — ready to pass as ``replace_receiver``'s
            ``date``.
        current_type / current_serial: The current archive receiver's identity.
        tail_units: Distinct physical units (``(date, type, serial)``) installed
            after the TOS-open receiver's start — the *unrecorded* changes. A
            length > 1 is what the guard refuses on.
        timeline: Human-readable per-segment lines for display.
    """

    install_date: str
    current_type: Optional[str]
    current_serial: Optional[str]
    tail_units: List[tuple]
    timeline: List[str]


def archive_receiver_install_date(
    station_id: str,
    *,
    writer: Optional[TOSWriter] = None,
    dry_run: bool = True,
    archive_root: Optional[str] = None,
) -> ArchiveReceiverDate:
    """Derive the current receiver's install date for ``station_id`` from the archive.

    Builds the station's receiver timeline from archived RINEX headers
    (:func:`tostools.receiver_timeline.build_receiver_timeline`) and returns the
    current physical receiver's install date
    (:func:`current_receiver_install_date` — coalesces firmware-only bumps).

    **Multi-segment guard.** Collapses the timeline to distinct physical units
    (type + serial, firmware ignored) and counts those installed *after* the
    receiver currently open in TOS — i.e. the swaps not yet recorded. If more
    than one such unit exists, a single ``replace-receiver`` cannot record them
    faithfully (it would collapse the intermediate unit and mis-date the old
    receiver's end), so this raises :class:`CfgOperationError` with the timeline.
    The operator reconstructs oldest-first, or passes ``--date`` to override.

    Args:
        station_id: 4-char marker.
        writer / dry_run: TOS reader for the current-open-receiver cutoff (read
            only; never writes).
        archive_root: Override the cold-archive root (default
            ``cold_archive_prepath()``).

    Returns:
        :class:`ArchiveReceiverDate`.

    Raises:
        CfgOperationError: no archived RINEX for the station, or >1 unrecorded
            receiver change (multi-segment guard).
    """
    from tostools.receiver_timeline import (
        _same_unit,
        build_receiver_timeline,
        current_install,
        current_receiver_install_date,
    )

    timeline = build_receiver_timeline(station_id, root=archive_root)
    if not timeline:
        raise CfgOperationError(
            f"No archived RINEX found for {station_id} — cannot derive an install "
            f"date from the archive. Pass --date explicitly."
        )

    # Collapse to distinct physical units in install order (firmware bumps fold
    # into the unit they belong to).
    units: List[Any] = []
    for seg in timeline:
        if units and _same_unit(units[-1].header.key, seg.header.key):
            continue
        units.append(seg)

    # Cutoff = the receiver currently open in TOS (what's already recorded).
    # Units installed strictly after it are the unrecorded swaps.
    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    old_id = _find_open_gnss_receiver_child(w, station_eid)
    cutoff: Optional[str] = None
    if old_id is not None:
        open_join = w.get_open_parent_join(old_id)
        if open_join and open_join.get("time_from"):
            cutoff = str(open_join["time_from"])[:10]  # YYYY-MM-DD

    if cutoff is not None:
        tail = [u for u in units if u.start.isoformat() > cutoff]
    else:
        tail = units  # nothing recorded in TOS → every archive unit is unrecorded

    cur = current_install(timeline)
    inst = current_receiver_install_date(timeline)

    timeline_lines = [
        f"{s.start.isoformat()} → {s.end.isoformat()}  "
        f"{s.header.rtype} sn {s.header.serial} fw {s.header.firmware}"
        for s in timeline
    ]

    if len(tail) > 1:
        tail_lines = "\n  ".join(
            f"{u.start.isoformat()}  {u.header.rtype}  sn {u.header.serial}"
            for u in tail
        )
        raise CfgOperationError(
            f"{station_id}: the RINEX archive shows {len(tail)} receiver changes "
            f"not yet in TOS (since {cutoff or 'station start'}):\n  {tail_lines}\n"
            f"A single replace-receiver would record only the latest and mis-date "
            f"the rest. Reconstruct the timeline with one replace-receiver per "
            f"change (oldest first, --date each), or pass --date to override this "
            f"guard for a single swap."
        )

    base = inst or (cur.start if cur else None)
    if base is None:  # pragma: no cover — timeline non-empty implies cur set
        raise CfgOperationError(
            f"{station_id}: could not resolve a current receiver from the archive."
        )
    return ArchiveReceiverDate(
        install_date=f"{base.isoformat()}T00:00:00",
        current_type=cur.header.rtype if cur else None,
        current_serial=cur.header.serial if cur else None,
        tail_units=[
            (u.start.isoformat(), u.header.rtype, u.header.serial) for u in tail
        ],
        timeline=timeline_lines,
    )


def replace_receiver(
    station_id: str,
    new_type: str,
    *,
    date: Optional[str] = None,
    host: Optional[str] = None,
    new_serial: Optional[str] = None,
    new_model: Optional[str] = None,
    new_firmware: Optional[str] = None,
    new_marker: Optional[str] = None,
    owner: str = "Jarðeðlismælihópur",
    old_status: Optional[str] = "bilað",
    old_comment: Optional[str] = "can't connect to the receiver",
    vitjun: Optional[str] = None,
    participants: str = "",
    continue_from: Optional[str] = None,
    skip_marker_check: bool = False,
    warehouse: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
) -> OperationResult:
    """One-shot receiver replacement on a station: warehouse + retire + install.

    Encodes the canonical 3-step field workflow as a single operation:

      1. **Warehouse intake** — if the new receiver's serial is not yet
         in TOS, ``create_device`` + B9 join at ``eff_date``. If it's
         already in TOS and parked at B9 (or has no open parent),
         reuse the existing entity. If it's deployed elsewhere, refuse.
      2. **Move OLD** — close the station→OLD join at ``eff_date``,
         open B9→OLD join at the same date, apply
         ``device_status='bilað'`` + comment defaults so the unit
         shows up as out-of-service in B9.
      3. **Install NEW** — close B9→NEW join (if any), open
         station→NEW at ``eff_date``, write the Breyting vitjun on
         the station with auto-derived text, update ``stations.cfg``
         (``receiver_serial``/``receiver_type``/
         ``receiver_firmware_version``/``rinex_config_valid_from``).

    All three steps share the same ``eff_date`` — accepted UI-bug
    trade-off in exchange for a single coherent timestamp.

    Args:
        station_id: 4-char RINEX marker of the destination station.
        new_type: Probe-type for the new receiver — one of the
            entries in :data:`receivers.cfg.device_probe.PROBE_STRATEGIES`.
            Required for the probe protocol selection.
        date: When the swap happened. Default: now. Bare date →
            noon. Used identically for all three transitions.
        host: ``IP[:PORT]`` for the probe. Default: derived from
            ``stations.cfg[station_id].router_ip`` plus the
            probe-type's default port.
        new_serial / new_model / new_firmware: Manual override —
            when all three are given, the probe step is skipped.
            Use for offline entry days after the field visit.
        new_marker: Override the probed marker (when probe doesn't
            return one or the operator wants to assert it).
        owner: TOS owner attribute for the new device entity.
            Default ``"Jarðeðlismælihópur"``.
        old_status: ``status`` attribute value for the OLD device.
            Default ``"bilað"``. Pass ``None`` to leave unchanged.
        old_comment: ``comment`` attribute value for the OLD device.
            Default ``"can't connect to the receiver"``. Pass ``None``
            to skip.
        vitjun: Override the auto-derived vitjun work text on the
            destination station's install record.
        participants: Comma-separated emails for the vitjun.
        continue_from: Skip to step ``"warehouse"`` / ``"move-old"`` /
            ``"install-new"`` for recovery from partial failure.
        skip_marker_check: Bypass the probed-marker-vs-station check
            (use when the receiver's marker is intentionally weird).
        dry_run / writer / cfg_path: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``operation="replace"`` and
        per-step responses in ``tos_changes``.

    Raises:
        CfgOperationError: For any precondition failure — unknown
            station, no open receiver to replace, marker mismatch,
            probed serial equals old serial, new device already
            joined to a non-B9 parent, missing required identity
            (without probe and without --new-serial/model/firmware).
    """
    from .device_probe import (
        ProbeError,
        parse_host_port,
        probe_receiver,
    )

    if continue_from is not None and continue_from not in REPLACE_STEPS:
        raise CfgOperationError(
            f"--continue-from must be one of {REPLACE_STEPS}, got {continue_from!r}"
        )

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)

    # Identify the OLD device (currently-open receiver at the station)
    old_id, old_serial = _resolve_old_receiver(w, station_eid)

    # Identify the NEW device — probe unless full manual data given
    manual = all(v is not None for v in (new_serial, new_model, new_firmware))
    probed_marker: Optional[str] = new_marker
    if not manual:
        # Resolve probe host:port — operator override > stations.cfg
        probe_host: str
        probe_port: Optional[int]
        if host:
            probe_host, probe_port = parse_host_port(host)
        else:
            cfg_host = _station_router_ip(station_id, cfg_path)
            if cfg_host is None:
                raise CfgOperationError(
                    f"No --host given and stations.cfg[{station_id}] has "
                    f"no router_ip — pass --host IP[:PORT] explicitly."
                )
            probe_host, probe_port = cfg_host, None
        try:
            identity = probe_receiver(
                probe_host,
                probe_port,
                probe_type=new_type,
                station_id_hint=station_id,
            )
        except ProbeError as exc:
            raise CfgOperationError(
                f"Probe of {probe_host}:{probe_port} ({new_type}) failed: "
                f"{exc}. If you have the new receiver's identity from "
                f"field notes, pass --new-serial X --new-model Y "
                f"--new-firmware Z to skip the probe."
            ) from exc
        new_serial = new_serial or identity.serial
        new_model = new_model or identity.model_raw
        new_firmware = new_firmware or identity.firmware_version
        if probed_marker is None:
            probed_marker = identity.marker_name

    if not new_serial or not new_model:
        raise CfgOperationError(
            "replace_receiver: serial and model are required (either via "
            "probe or via --new-serial / --new-model)."
        )

    if new_serial == old_serial:
        raise CfgOperationError(
            f"Probed/given new serial {new_serial!r} matches the old "
            f"receiver at {station_id}. Did the physical swap actually "
            f"happen? Aborting to avoid creating a no-op TOS history."
        )

    if not skip_marker_check:
        _validate_marker_match(probed_marker, station_id)

    eff_date = _visit_default_time(date)

    # Pre-check: if the new serial is already in TOS, sanity-check its parent
    existing_new = w.find_device_by_serial("gnss_receiver", new_serial)
    new_device_id: Optional[int] = None
    needs_warehouse_intake = True
    # Resolve the transit warehouse once — re-used by steps 1 + 2.
    warehouse_name = warehouse or DEFAULT_WAREHOUSE

    if existing_new is not None:
        new_device_id = int(existing_new["id_entity"])
        open_join = w.get_open_parent_join(new_device_id)
        current_parent = open_join.get("id_entity_parent") if open_join else None
        b9_eid = _b9_eid(w, warehouse=warehouse_name)
        if current_parent is None or current_parent == b9_eid:
            needs_warehouse_intake = False  # already warehoused or floating — reuse
        else:
            raise CfgOperationError(
                f"Device with serial {new_serial!r} (id_entity="
                f"{new_device_id}) is already joined to TOS entity "
                f"{current_parent}, not {warehouse_name!r}. Either it's "
                f"still deployed on a station, or someone moved it "
                f"manually. Use the atomic verbs (cfg move-device "
                f"--serial {new_serial} ...) instead of replace-receiver."
            )

    # Build a unified result aggregating the three steps
    result = OperationResult(
        operation="replace",
        station_id=station_id,
        serial=new_serial,
        date=eff_date,
        tos_changes={
            "plan": {
                "old_serial": old_serial,
                "new_serial": new_serial,
                "new_model": new_model,
                "new_firmware": new_firmware,
                "needs_warehouse_intake": needs_warehouse_intake,
            }
        },
        dry_run=dry_run,
    )

    start_step = continue_from or "warehouse"

    # --- Step 1: Warehouse intake -----------------------------------------
    if start_step == "warehouse":
        if needs_warehouse_intake:
            from tostools.device import build_required_attributes
            from tostools.standards.igs_equipment import to_igs_receiver

            igs_model = to_igs_receiver(new_model) or new_model
            attrs = build_required_attributes(
                serial=new_serial,
                model=igs_model,
                owner=owner,
                date_start=eff_date,
            )
            if new_firmware:
                attrs.append(
                    {
                        "code": "firmware_version",
                        "value": new_firmware,
                        "date_from": eff_date,
                        "date_to": None,
                    }
                )
            created = w.create_device(
                entity_subtype="gnss_receiver",
                attributes=attrs,
                force=False,
            )
            result.tos_changes["warehouse_create"] = created
            new_device_id = (
                created.get("id_entity") if isinstance(created, dict) else None
            )
            if new_device_id is None and not dry_run:
                raise RuntimeError(
                    "warehouse step: create_device returned no id_entity"
                )
            connect = w.connect_device_to_location(
                int(new_device_id) if new_device_id else 0,
                location_name=warehouse_name,
                date_start=eff_date,
                type_filter="vöruhús",
            )
            result.tos_changes["warehouse_connect"] = connect
        else:
            result.tos_changes["warehouse_create"] = "skipped (already in TOS)"
        start_step = "move-old"

    # --- Step 2: Move OLD station → warehouse with status/comment --------
    if start_step == "move-old":
        move_old = move_device(
            old_serial,
            to=warehouse_name,
            date=eff_date,
            device_status=old_status,
            device_comment=old_comment,
            participants=participants,
            dry_run=dry_run,
            writer=w,
            skip_vitjun=True,
        )
        result.tos_changes["move_old"] = move_old.tos_changes
        start_step = "install-new"

    # --- Step 3: Install NEW B9 → station with auto-vitjun + cfg ---------
    if start_step == "install-new":
        # Tell move_device "the open receiver at the station is the one we
        # just moved out" — needed for dry-run preview, and harmless in
        # live mode (the join is already closed by step 2 there).
        install_new = move_device(
            new_serial,
            to=station_id,
            from_station=None,
            date=eff_date,
            firmware=new_firmware,
            vitjun=vitjun,
            participants=participants,
            dry_run=dry_run,
            writer=w,
            cfg_path=cfg_path,
            _assume_cleared_device_id=old_id,
        )
        result.tos_changes["install_new"] = install_new.tos_changes
        result.cfg_changes = install_new.cfg_changes
        result.vitjun_id = install_new.vitjun_id

    return result


# ---------------------------------------------------------------------------
# Telemetry swaps — modem_gsm (router) + sim_card
# ---------------------------------------------------------------------------


def _attr_serial(attributes: List[Dict[str, Optional[str]]]) -> Optional[str]:
    """Pull the ``serial_number`` value out of a ``build_*_attributes`` payload."""
    for attr in attributes:
        if attr.get("code") == "serial_number":
            return attr.get("value")
    return None


def _create_and_join_device(
    w: TOSWriter,
    *,
    subtype: str,
    attributes: List[Dict[str, Optional[str]]],
    station_eid: int,
    eff_date: str,
    dry_run: bool,
) -> tuple[Optional[int], Any, Any]:
    """Create (or reuse) a device and open a station join. Returns (id, create, join).

    If a device with the same serial already exists as ``subtype``, it is
    reused rather than recreated: bench intake often pre-registers (and
    warehouses) the new hardware in TOS before the field install, and
    ``create_device(force=False)`` refuses to add a duplicate. In that case the
    existing device is reparented to the station via
    :meth:`TOSWriter.move_device` — closes the open warehouse join and opens the
    station join (Pattern 2), tolerating a parentless device — mirroring how
    :func:`replace_receiver` skips its warehouse-intake step for an
    already-registered receiver. Optional intake attributes are left as the
    device already carries them (the join is what the install is about).

    In dry-run the create returns a :class:`DryRunResult` with no id; the
    join is still issued with a ``0`` placeholder child so the preview shows
    both calls (mirrors :func:`replace_receiver`'s warehouse-intake step).
    """
    existing = w.find_device_by_serial(subtype, _attr_serial(attributes) or "")
    if existing is not None:
        device_id = int(existing["id_entity"])
        join = w.move_device(device_id, station_eid, eff_date)
        return device_id, existing, join

    created = w.create_device(
        entity_subtype=subtype, attributes=attributes, force=False
    )
    device_id = created.get("id_entity") if isinstance(created, dict) else None
    if device_id is None and not dry_run:
        raise CfgOperationError(
            f"create_device({subtype}) returned no id_entity — cannot join to station."
        )
    join = w.create_entity_connection(
        id_parent=station_eid,
        id_child=int(device_id) if device_id else 0,
        time_from=eff_date,
        time_to=None,
    )
    return device_id, created, join


def _retire_old_child(
    w: TOSWriter,
    old_id: Optional[int],
    eff_date: str,
    *,
    to_warehouse_eid: Optional[int] = None,
) -> Any:
    """Close an old device's open station join at ``eff_date``.

    When ``to_warehouse_eid`` is given, reparents the device to that warehouse
    (Pattern-2 close+open via :meth:`TOSWriter.move_device`) — used for modems,
    which are trackable returnable hardware. Otherwise just closes the open
    join (device left parentless / retired) — used for SIM cards, which aren't
    warehoused inventory. Returns the writer response, or ``None`` when there
    was no ``old_id`` or no open join.

    Also closes the device's **installation-scoped** attribute periods at
    ``eff_date`` — see :func:`_close_install_scoped_attributes`. Closing the
    join alone is what left ISAK antenna 4527 (removed 2026-07-30) still
    reporting this mark's ``antenna_height`` as its current value.
    """
    if old_id is None:
        return None
    _close_install_scoped_attributes(w, old_id, eff_date)
    if to_warehouse_eid is not None:
        return w.move_device(old_id, to_warehouse_eid, eff_date)
    open_join = w.get_open_parent_join(old_id)
    if open_join and open_join.get("id") is not None:
        return w.patch_entity_connection(int(open_join["id"]), time_to=eff_date)
    return None


def _close_install_scoped_attributes(
    w: TOSWriter,
    device_id: int,
    eff_date: str,
) -> Dict[str, Any]:
    """End the attribute periods that described the installation just ended.

    A device's geometry — antenna height above the monument, the eccentricity
    offsets, the azimuth — is true of one INSTALLATION, not of the device. When
    it comes off the mark those values stop being true, but nothing succeeds
    them: kit in a warehouse has no height above anything. So they are closed,
    not transitioned.

    Left open they are actively wrong rather than merely stale: every consumer
    that asks TOS for "the current value" gets this mark's number for a device
    that is somewhere else. ISAK antenna 4527 sat that way from 2026-07-30
    until it was found by ``tos audit missing-attributes``.

    Device-state codes (``status``, ``comment``, ``owner``) are deliberately
    untouched — they describe the device wherever it now is, and the calling
    verbs already transition them via ``old_status``/``old_comment``.

    The code set is imported from ``tostools`` rather than restated here so the
    write path closes exactly what the audit reports.
    """
    from tostools.audit_missing_attributes import INSTALL_SCOPED_CODES

    closed: Dict[str, Any] = {}
    for code in sorted(INSTALL_SCOPED_CODES):
        try:
            response = w.close_attribute_period(device_id, code, eff_date)
        except Exception as exc:  # noqa: BLE001
            # A tidy-up must never sink the swap it is tidying up after: the
            # join close and the new device are the operation's real payload.
            logger.warning(
                "could not close %s on device %s at %s: %s",
                code,
                device_id,
                eff_date,
                exc,
            )
            continue
        if response is not None:
            closed[code] = response
    return closed


def replace_modem(
    station_id: str,
    *,
    new_serial: str,
    new_model: str,
    owner: str = "Jarðeðlismælihópur",
    new_router_type: Optional[str] = None,
    ip_address: Optional[str] = None,
    phone_number: Optional[str] = None,
    provider: Optional[str] = None,
    mac_address: Optional[str] = None,
    manufacturer: Optional[str] = None,
    io_type: Optional[str] = None,
    modem_subtype: Optional[str] = None,
    comment: Optional[str] = None,
    extra_attrs: Optional[Dict[str, Optional[str]]] = None,
    date: Optional[str] = None,
    old_status: Optional[str] = "bilað",
    old_comment: Optional[str] = None,
    vitjun: Optional[str] = None,
    participants: str = "",
    warehouse: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
) -> OperationResult:
    """Swap a station's GSM modem/router in TOS (Pattern-2) + stations.cfg.

    A site visit replaced the telemetry router. In TOS the router is a
    ``modem_gsm`` device child of the station (canonical serial/model/owner/
    status shape plus telemetry optionals). This:

      1. Retires the old modem (if any): moves it to the warehouse and applies
         ``old_status``/``old_comment`` (e.g. ``"bilað"``).
      2. Creates the new ``modem_gsm`` device (manual entry — a modem can't be
         probed) and opens a station join at ``date``.
      3. Writes a Breyting vitjun on the station ("Skipt um router/modem …").
      4. Updates ``stations.cfg[router_type]`` when ``new_router_type`` is given.
         The IP lives on the ``sim_card`` — use :func:`replace_sim` for that.

    Args:
        station_id: 4-char marker of the station.
        new_serial / new_model: New modem identity (required). ``new_model`` is
            free-text vendor naming, e.g. ``"Teltonika RUT200"``.
        owner: TOS owner attribute. Default ``"Jarðeðlismælihópur"``.
        new_router_type: stations.cfg ``router_type`` value (e.g. ``"Teltonika"``).
            When ``None``, stations.cfg is left untouched.
        ip_address, phone_number, provider, mac_address, manufacturer,
            io_type, modem_subtype, comment: Optional TOS attributes on the new
            ``modem_gsm`` (see :data:`tostools.device.MODEM_GSM_ATTR_CODES`).
            ``modem_subtype`` maps to the TOS ``subtype`` attribute (e.g.
            ``"4G"``). Omitted when falsy.
        extra_attrs: Escape hatch — ``{code: value}`` for any attribute not
            covered by the named params; merged last (can override).
        date: When the swap happened. Default now; bare date → noon.
        old_status / old_comment: Pattern-2 transitions on the OLD modem
            (``None``/``""`` to skip). Default status ``"bilað"``.
        vitjun: Override the auto-derived vitjun text.
        participants: Comma-separated emails for the vitjun.
        warehouse: Override the transit warehouse (default B9).
        dry_run / writer / cfg_path: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``operation="replace-modem"``.
    """
    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    eff_date = _visit_default_time(date)

    old_id = _find_open_child(w, station_eid, "modem_gsm")
    old_serial: Optional[str] = None
    if old_id is not None:
        old_hist = w.get_entity_history(old_id)
        if isinstance(old_hist, dict):
            old_serial = _device_attribute(old_hist, "serial_number")
        if old_serial and old_serial == new_serial:
            raise CfgOperationError(
                f"New modem serial {new_serial!r} matches the modem already "
                f"open at {station_id}. Did the swap actually happen?"
            )

    from tostools.device import build_modem_gsm_attributes

    # No IGS table for telemetry — new_model is free-text vendor naming.
    attrs = build_modem_gsm_attributes(
        serial=new_serial,
        model=new_model,
        owner=owner,
        date_start=eff_date,
        ip_address=ip_address,
        phone_number=phone_number,
        provider=provider,
        mac_address=mac_address,
        manufacturer=manufacturer,
        io_type=io_type,
        modem_subtype=modem_subtype,
        comment=comment,
        extra=extra_attrs,
    )

    result = OperationResult(
        operation="replace-modem",
        station_id=station_id,
        serial=new_serial,
        date=eff_date,
        tos_changes={
            "plan": {
                "old_serial": old_serial,
                "new_serial": new_serial,
                "new_model": new_model,
            }
        },
        dry_run=dry_run,
    )

    # 1: retire the old modem FIRST (move to warehouse + status transition),
    # then create + join the new one. Retire-first keeps the station with at
    # most one open modem_gsm child at any instant — a mid-run failure leaves
    # it momentarily modem-less (honest, recoverable) rather than with two
    # simultaneously-open modems (ambiguous for the child-walk reader).
    if old_id is not None:
        warehouse_eid = _b9_eid(w, warehouse=warehouse)
        result.tos_changes["retire_old"] = _retire_old_child(
            w, old_id, eff_date, to_warehouse_eid=warehouse_eid
        )
        _apply_device_attribute_transitions(
            w,
            old_id,
            eff_date,
            device_status=old_status,
            device_comment=old_comment,
            result=result,
        )

    # 2: create the new modem + open its station join.
    _new_id, created, join = _create_and_join_device(
        w,
        subtype="modem_gsm",
        attributes=attrs,
        station_eid=station_eid,
        eff_date=eff_date,
        dry_run=dry_run,
    )
    result.tos_changes["new_modem_create"] = created
    result.tos_changes["new_modem_join"] = join

    # 3: vitjun on the station.
    old_label = (old_serial or "?") if old_id is not None else None
    work = vitjun or (
        f"Skipt um router/modem: {old_label} → {new_model} {new_serial}"
        if old_label
        else f"Settur upp router/modem: {new_model} {new_serial}"
    )
    vit = w.add_maintenance_visit(
        station_eid,
        start_time=eff_date,
        maintenance_type="on_site",
        participants=participants,
        reasons=["change"],
        work=work,
    )
    result.tos_changes["vitjun"] = vit
    result.vitjun_id = vit.get("id_maintenance")

    # 4: stations.cfg router_type (only when given; IP is the SIM's job).
    if new_router_type and not dry_run:
        target_cfg = _resolve_cfg_path(cfg_path)
        result.cfg_changes = _apply_cfg_updates(
            target_cfg, station_id, {"router_type": new_router_type}
        )
    return result


def replace_sim(
    station_id: str,
    *,
    ip_address: str,
    phone_number: Optional[str] = None,
    serial_number: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    owner: Optional[str] = None,
    comment: Optional[str] = None,
    extra_attrs: Optional[Dict[str, Optional[str]]] = None,
    date: Optional[str] = None,
    vitjun: Optional[str] = None,
    participants: str = "",
    update_cfg_ip: bool = False,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
) -> OperationResult:
    """Swap a station's SIM card in TOS (new sim_card entity) + stations.cfg.

    A site visit replaced the SIM, giving a new IP. In TOS the SIM is a
    ``sim_card`` device child of the station carrying ``ip_address`` plus
    optional telemetry attributes — NOT the canonical device shape. This:

      1. Closes the old SIM's station join (SIMs aren't warehoused — the old
         entity is left retired, not reparented).
      2. Creates a new ``sim_card`` device (:func:`build_sim_card_attributes`)
         and opens a station join at ``date``.
      3. Writes a vitjun on the station ("Skipt um SIM-kort, nýtt IP …").
      4. When ``update_cfg_ip`` is True, writes the new IP to
         ``stations.cfg[router_ip]``. **Off by default**: cfg ``router_ip`` is
         frequently a DNS hostname (e.g. ``GSIG.gps.vedur.is``) that should not
         be overwritten with a literal IP without operator intent.

    Args:
        station_id: 4-char marker of the station.
        ip_address: The new SIM's IP (required).
        phone_number, serial_number, provider, model, owner, comment: Optional
            TOS attributes on the new ``sim_card`` (see
            :data:`tostools.device.SIM_CARD_ATTR_CODES`). Omitted when falsy.
        extra_attrs: Escape hatch — ``{code: value}`` for any attribute not
            covered by the named params; merged last (can override).
        date: When the swap happened. Default now; bare date → noon.
        vitjun: Override the auto-derived vitjun text.
        participants: Comma-separated emails for the vitjun.
        update_cfg_ip: Write ``router_ip`` in stations.cfg (default False).
        dry_run / writer / cfg_path: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``operation="replace-sim"``.
    """
    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    eff_date = _visit_default_time(date)

    old_id = _find_open_child(w, station_eid, "sim_card")
    old_ip: Optional[str] = None
    if old_id is not None:
        old_hist = w.get_entity_history(old_id)
        if isinstance(old_hist, dict):
            old_ip = _device_attribute(old_hist, "ip_address")
        if old_ip is not None and old_ip == ip_address:
            raise CfgOperationError(
                f"New IP {ip_address!r} matches the SIM already open at "
                f"{station_id}. Nothing changed — refusing to create a "
                f"duplicate sim_card. (Use `cfg visit` to record a visit "
                f"without an equipment change.)"
            )

    from tostools.device import build_sim_card_attributes

    attrs = build_sim_card_attributes(
        ip_address=ip_address,
        date_start=eff_date,
        phone_number=phone_number,
        serial_number=serial_number,
        provider=provider,
        model=model,
        owner=owner,
        comment=comment,
        extra=extra_attrs,
    )

    result = OperationResult(
        operation="replace-sim",
        station_id=station_id,
        date=eff_date,
        tos_changes={"plan": {"old_ip": old_ip, "new_ip": ip_address}},
        dry_run=dry_run,
    )

    # Retire the old SIM FIRST (close its station join — SIMs aren't
    # warehoused), then create + join the new one, so the station never has
    # two open sim_card children simultaneously (see replace_modem rationale).
    result.tos_changes["retire_old"] = _retire_old_child(w, old_id, eff_date)
    _new_id, created, join = _create_and_join_device(
        w,
        subtype="sim_card",
        attributes=attrs,
        station_eid=station_eid,
        eff_date=eff_date,
        dry_run=dry_run,
    )
    result.tos_changes["new_sim_create"] = created
    result.tos_changes["new_sim_join"] = join

    work = vitjun or (
        f"Skipt um SIM-kort, nýtt IP {ip_address}"
        + (f" (var {old_ip})" if old_ip else "")
    )
    vit = w.add_maintenance_visit(
        station_eid,
        start_time=eff_date,
        maintenance_type="on_site",
        participants=participants,
        reasons=["change"],
        work=work,
    )
    result.tos_changes["vitjun"] = vit
    result.vitjun_id = vit.get("id_maintenance")

    if update_cfg_ip and not dry_run:
        target_cfg = _resolve_cfg_path(cfg_path)
        result.cfg_changes = _apply_cfg_updates(
            target_cfg, station_id, {"router_ip": ip_address}
        )
    return result


def set_device_attribute(
    station_id: str,
    *,
    subtype: str,
    code: str,
    value: str,
    date: Optional[str] = None,
    change: bool = False,
    correct: bool = False,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Set ONE attribute on a station's open ``subtype`` device — no full swap.

    The swap verbs (``replace-sim``/``replace-modem``/``replace-receiver``) only
    create or replace a whole device; there was no way to add or correct a single
    attribute on a device already in TOS (e.g. record a ``phone_number`` on an
    existing ``sim_card`` after MSISDN discovery, where ``replace-sim``'s same-IP
    guard correctly refuses a duplicate). This fills that gap.

    Behaviour by current state of ``code`` on the open device:

    - **No open value** → ADD it (Pattern 3 POST), dated from the device's
      install (its open parent join ``time_from``) or ``date`` when given. No
      intent needed — purely additive.
    - **Open value already equals** ``value`` → no-op.
    - **Open value differs** → intent REQUIRED (same model as ``cfg
      update-device``): ``correct`` = Pattern 1 in-place PATCH (the recorded
      value was wrong; no history); ``change`` = Pattern 2 transition (close the
      open period at ``date`` and open a new one; records history).

    Args:
        station_id: 4-char marker.
        subtype: TOS device subtype to target (``sim_card``, ``modem_gsm``,
            ``gnss_receiver``, ``antenna`` …) — the station's OPEN child of that
            subtype is used.
        code: Attribute code (e.g. ``phone_number``).
        value: New value.
        date: Effective date. For an add, the new period's ``date_from`` (default
            the device install date). For a ``change`` transition, the cut date
            (default now).
        change / correct: Intent for the differing-value case (mutually
            exclusive; ignored for add / no-op).
        dry_run / writer: As :func:`replace_sim`.

    Returns:
        :class:`OperationResult` with ``operation="set-attr"``; the plan/mode
        live in ``tos_changes``.
    """
    if change and correct:
        raise CfgOperationError("set-attr: pass at most one of --change / --correct.")

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    device_id = _find_open_child(w, station_eid, subtype)
    if device_id is None:
        raise CfgOperationError(
            f"No open {subtype} child for {station_id} — add the device first "
            f"(e.g. a cfg replace-* / add-* verb) before setting attributes on it."
        )

    existing = w.get_attribute_values(device_id, code)
    open_vals = [a for a in existing if a.get("date_to") is None]
    current = (
        max(open_vals, key=lambda a: a.get("date_from") or "") if open_vals else None
    )

    result = OperationResult(
        operation="set-attr",
        station_id=station_id,
        date=date,
        tos_changes={
            "plan": {
                "subtype": subtype,
                "device_id": device_id,
                "code": code,
                "old_value": current.get("value") if current else None,
                "new_value": value,
            }
        },
        dry_run=dry_run,
    )

    if current is None:
        # Pattern 3 — attribute not set yet; add it dated from device install.
        if date:
            date_from = _visit_default_time(date)
        else:
            join = w.get_open_parent_join(device_id) or {}
            date_from = join.get("time_from") or _visit_default_time(None)
        result.tos_changes["mode"] = "add"
        result.tos_changes["write"] = w.add_attribute_value(
            device_id, code, value, date_from
        )
        return result

    if (current.get("value") or "") == value:
        result.tos_changes["mode"] = "noop"
        return result

    if not (change or correct):
        raise CfgOperationError(
            f"{code} on {station_id}'s {subtype} is already "
            f"{current.get('value')!r} (open since {current.get('date_from')}). "
            f"Pass --correct (recorded value was wrong → fix in place, no history) "
            f"or --change (it genuinely changed → new period, records history)."
        )

    if correct:
        result.tos_changes["mode"] = "correct"
        result.tos_changes["write"] = w.upsert_attribute_value(
            device_id,
            code,
            value,
            current.get("date_from") or _visit_default_time(date),
        )
    else:
        result.tos_changes["mode"] = "change"
        result.tos_changes["write"] = w.transition_attribute_value(
            device_id, code, value, _visit_default_time(date)
        )
    return result


def close_join(
    *,
    id_connection: Optional[int] = None,
    station_id: Optional[str] = None,
    subtype: Optional[str] = None,
    date: Optional[str] = None,
    warehouse: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Close an open ``entity_connection`` (join) by setting its ``time_to``.

    The non-destructive sibling of :func:`delete_join`. ``delete_join`` removes
    a join row that should never have existed; ``close_join`` ends a join that
    was real and is now over — the device left the station. History is
    preserved either way, which is the whole point of the temporal store.

    Two selection modes:

    * **By station + subtype** (preferred) — resolves the station's open child
      of ``subtype`` and closes *its* open parent join. Both lookups confirm
      the join is open before writing, so this mode cannot close an already-
      closed period.
    * **By raw id** (``id_connection``) — patches that row directly. TOS
      exposes no get-join-by-id endpoint, so **no open-check is performed**:
      passing a closed row's id silently rewrites its ``time_to``. Inspect
      ``GET /entity/parent_history/{id_child}`` first (same caveat as
      :func:`delete_join`).

    Args:
        id_connection: Join row ``id`` to close. Mutually exclusive with
            ``station_id``/``subtype``.
        station_id: 4-char marker whose open ``subtype`` child to retire.
        subtype: TOS device subtype (``"antenna"``, ``"radome"``,
            ``"gnss_receiver"``, ``"modem_gsm"``, ``"sim_card"`` …).
        date: When the join ended. Default now; bare date → noon.
        warehouse: When given, *reparent* the device to that warehouse
            (Pattern 2 close+open) instead of leaving it parentless.
            Station+subtype mode only — a raw id identifies a join, not a
            device, so there is nothing to reparent.
        dry_run: When True (default), no writes are sent.
        writer: Optional pre-built TOSWriter.

    Returns:
        :class:`OperationResult` with ``operation="close-join"``.

    Raises:
        CfgOperationError: On a bad selector combination, when the station has
            no open child of that subtype, or when that child has no open
            parent join.
    """
    if (id_connection is None) == (station_id is None):
        raise CfgOperationError(
            "close_join: pass either id_connection OR station_id+subtype "
            "(not both, not neither)."
        )
    if station_id is not None and not subtype:
        raise CfgOperationError("close_join: station_id requires subtype.")
    if id_connection is not None and warehouse:
        raise CfgOperationError(
            "close_join: --warehouse needs a device to reparent — use "
            "--station/--subtype selection, not --id."
        )

    w = _resolve_writer(writer, dry_run)
    eff_date = _visit_default_time(date)
    result = OperationResult(
        operation="close-join",
        station_id=station_id,
        date=eff_date,
        dry_run=dry_run,
    )

    if id_connection is not None:
        result.tos_changes["id_connection"] = id_connection
        result.tos_changes["closed"] = w.patch_entity_connection(
            int(id_connection), time_to=eff_date
        )
        return result

    station_eid = _resolve_station(w, str(station_id))
    open_ids = _find_open_children(w, station_eid, str(subtype))
    if not open_ids:
        raise CfgOperationError(
            f"{station_id} has no open {subtype} child — nothing to close."
        )
    if len(open_ids) > 1:
        raise CfgOperationError(
            f"{station_id} has {len(open_ids)} open {subtype} children "
            f"({', '.join(str(i) for i in open_ids)}). Close them one at a "
            f"time with `cfg close-join --id <ID_CONNECTION>` after "
            f"inspecting `tos device show --id-entity <ID>`."
        )
    device_id = open_ids[0]
    hist = w.get_entity_history(device_id)
    serial = (
        _device_attribute(hist, "serial_number") if isinstance(hist, dict) else None
    )
    result.serial = serial
    result.tos_changes["device"] = {
        "id_entity": device_id,
        "subtype": subtype,
        "serial": serial,
    }

    # Resolve the join in BOTH modes: the reparent path doesn't need the id
    # (move_device finds it itself), but reporting the same tos_changes shape
    # either way keeps `--json` consumers from having to special-case the
    # warehouse run — and it fails loudly here if there is nothing open.
    open_join = w.get_open_parent_join(device_id)
    if not open_join or open_join.get("id") is None:
        raise CfgOperationError(
            f"{station_id}: {subtype} {serial or device_id} has no open "
            f"parent join to close."
        )
    result.tos_changes["id_connection"] = int(open_join["id"])
    warehouse_eid = _b9_eid(w, warehouse=warehouse) if warehouse else None
    result.tos_changes["closed"] = _retire_old_child(
        w, device_id, eff_date, to_warehouse_eid=warehouse_eid
    )
    return result


def replace_antenna(
    station_id: str,
    *,
    new_model: str,
    new_serial: Optional[str] = None,
    antenna_height: Optional[str] = None,
    radome: Optional[str] = None,
    radome_serial: Optional[str] = None,
    keep_radome: bool = False,
    owner: str = "Jarðeðlismælihópur",
    date: Optional[str] = None,
    cfg_antenna_height: Optional[str] = None,
    rinex_valid_from: Optional[str] = None,
    old_status: Optional[str] = None,
    old_comment: Optional[str] = None,
    comment: Optional[str] = None,
    vitjun: Optional[str] = None,
    participants: str = "",
    warehouse: Optional[str] = None,
    skip_vitjun: bool = False,
    skip_cfg: bool = False,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
) -> OperationResult:
    """Swap a station's GNSS antenna in TOS (Pattern-2) + ``stations.cfg``.

    The counterpart to :func:`replace_receiver` for the other half of the
    RINEX header. :func:`add_antenna` is *intake* — it refuses when the station
    already has an open antenna, because adding a second open antenna makes
    ``current_session()`` (and therefore station.info, the stream SKL and every
    RINEX header) ambiguous. This verb is the swap:

      1. Retire the old antenna: close its station join at ``date`` (or
         reparent to ``warehouse``) and apply ``old_status``/``old_comment``.
      2. **The radome follows the antenna** — the two are screwed together and
         ~95% of field swaps take both down and put both up. So by default the
         old radome's join is closed and a new radome device is created and
         joined alongside the new antenna. See "Radome semantics" below.
      3. Create (or reuse, when pre-registered) the new antenna and open its
         station join at the same instant.
      4. Write a Breyting vitjun ("Skipt um loftnet: … → …").
      5. Update ``stations.cfg``: ``antenna_type``, ``antenna_serial``,
         ``antenna_radome``, ``antenna_height`` and ``rinex_config_valid_from``.

    **Radome semantics.** A radome is screwed onto the antenna, so the pair
    normally comes down and goes up together — but TOS models them as two
    independent device children, and either can be replaced alone (radome-only:
    :func:`replace_radome`). Three cases, keyed on ``radome`` / ``keep_radome``:

    ==========================  ==================================================
    Flags                       Effect
    ==========================  ==================================================
    *(neither given)*           **Same model, NEW unit.** The old radome's join is
                                closed and a fresh radome device is created with
                                the old one's model. This is the 95% case — a new
                                antenna arriving with its own radome of the usual
                                type. It is an *inferred* write: the carried-
                                forward model is reported in
                                ``tos_changes["plan"]["radome"]`` so a dry-run
                                shows what it decided. With **no** open radome to
                                carry forward, nothing is created and nothing is
                                closed (a station that never had one keeps not
                                having one).
    ``radome="CODE"``           New radome of that model. ``"NONE"`` means the new
                                antenna is bare: old join closed, nothing created.
    ``keep_radome=True``        The physical radome was unscrewed from the old
                                antenna and re-fitted to the new one. Entity and
                                join are left untouched, and cfg
                                ``antenna_radome`` is not rewritten.
    ==========================  ==================================================

    The cfg ``antenna_height`` is a **composite** — antenna ARP height plus the
    monument's mark→ARP offset — which TOS splits across two entities. It is
    derived as ``antenna_height + monument.monument_height`` from the station's
    open monument child; pass ``cfg_antenna_height`` to override. When neither
    is resolvable the operation raises rather than writing a height short by
    the monument offset (a wrong ``ANTENNA: DELTA H`` biases every downstream
    position without erroring anywhere).

    Args:
        station_id: 4-char marker of the station.
        new_model: New antenna model (IGS name or known alias; validated).
        new_serial: New antenna serial; ``None``/empty → synthetic
            ``antenna-<STID>-<YYYYMMDD>`` placeholder (as :func:`add_antenna`).
        antenna_height: New antenna ARP height in metres (TOS
            ``antenna.antenna_height``, RINEX ``ANTENNA: DELTA H``). Required —
            a replacement without a height would silently default to 0.0.
        radome: New radome IGS code, or ``"NONE"`` for "no radome fitted".
            Omit to carry the old radome's model forward — see "Radome
            semantics" above. Mutually exclusive with ``keep_radome``.
        radome_serial: Serial of the new radome. ``None`` → synthetic
            ``radome-<STID>-<YYYYMMDD>`` (the fleet convention — most radomes
            are unserialised), but modern units do carry one and it should be
            recorded when known.
        keep_radome: Re-fit the SAME physical radome onto the new antenna —
            leave its entity and join alone.
        owner: TOS owner attribute for the new devices.
        date: When the swap happened. Default now; bare date → noon (the
            field-work convention shared with ``move-device``/``add-antenna``,
            so co-installs land in one TOS session).
        cfg_antenna_height: Override for the cfg composite height.
        rinex_valid_from: Override ``rinex_config_valid_from``. Default:
            :func:`_default_rinex_valid_from` of the swap date.
        old_status / old_comment: Pattern-2 transitions on the OLD antenna
            (e.g. ``"bilað"``). ``None`` to leave untouched.
        comment: Free-text comment attribute on the NEW antenna.
        vitjun: Override the auto-derived vitjun text.
        participants: Comma-separated emails for the vitjun.
        warehouse: Reparent the old antenna to this warehouse instead of
            leaving it parentless. Off by default — unlike a receiver, a
            retired antenna is as often scrapped as returned, and a wrong
            reparent writes a location claim indistinguishable from a real one.
            Also applies to the **old radome** whenever it is retired: the two
            come off the mast together, so they land in the same place.
        skip_vitjun: Skip the vitjun step.
        skip_cfg: Skip the ``stations.cfg`` write.
        dry_run / writer / cfg_path: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``operation="replace-antenna"``.

    Raises:
        CfgOperationError: When the station has no open antenna (use
            ``add-antenna``), has more than one, when the new serial matches
            the open antenna's, when ``antenna_height`` is missing, or when the
            cfg composite height cannot be resolved.
    """
    from tostools.device import (
        build_antenna_attributes,
        build_required_attributes,
        synthetic_serial,
        validate_model,
    )

    if antenna_height is None or str(antenna_height).strip() == "":
        raise CfgOperationError(
            "replace-antenna: --antenna-height is required. The new antenna's "
            "ARP height drives RINEX 'ANTENNA: DELTA H'; omitting it would "
            "record 0.0 and bias every position computed from this station."
        )

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    eff_date = _visit_default_time(date)

    # --- Locate the antenna being replaced --------------------------------
    open_antennas = _find_open_children(w, station_eid, "antenna")
    if not open_antennas:
        raise CfgOperationError(
            f"{station_id} has no open antenna child — this is an intake, not "
            f"a swap. Use `receivers cfg add-antenna --station {station_id}`."
        )
    if len(open_antennas) > 1:
        raise CfgOperationError(
            f"{station_id} has {len(open_antennas)} open antenna children "
            f"({', '.join(str(i) for i in open_antennas)}) — ambiguous. Close "
            f"the stale one with `cfg close-join` first, then re-run."
        )
    old_id = open_antennas[0]
    old_hist = w.get_entity_history(old_id)
    old_serial = (
        _device_attribute(old_hist, "serial_number")
        if isinstance(old_hist, dict)
        else None
    )
    old_model = (
        _device_attribute(old_hist, "model") if isinstance(old_hist, dict) else None
    )

    if radome is not None and keep_radome:
        raise CfgOperationError(
            "replace-antenna: --radome and --keep-radome are mutually "
            "exclusive — either a new radome goes on (--radome CODE, or omit "
            "the flag to carry the old model forward) or the old one is "
            "re-fitted (--keep-radome)."
        )
    if radome_serial and keep_radome:
        raise CfgOperationError(
            "replace-antenna: --radome-serial has nothing to name under "
            "--keep-radome — no radome device is created. To correct the "
            "existing radome's serial use `cfg set-attr --subtype radome`."
        )

    # Validate BOTH models up front: a bad radome code discovered after the old
    # antenna's join was already closed would leave the station half-swapped.
    igs_model = validate_model("antenna", new_model)
    synthetic = new_serial is None or str(new_serial).strip() == ""
    ant_serial = (
        synthetic_serial("antenna", station_id, eff_date)
        if synthetic
        else str(new_serial).strip()
    )
    if old_serial and old_serial == ant_serial:
        raise CfgOperationError(
            f"New antenna serial {ant_serial!r} matches the antenna already "
            f"open at {station_id}. Did the swap actually happen? (To correct "
            f"a wrong model/height on the SAME unit use `cfg set-attr`.)"
        )

    # --- radome plan: resolved BEFORE any write ---------------------------
    # The radome is screwed to the antenna, so by default it comes down with it
    # and a new one goes up: close the old join, create a fresh device carrying
    # the SAME model (a new antenna normally ships with a radome of the usual
    # type). --radome CODE names a different one; --keep-radome means the same
    # physical unit was re-fitted and TOS should not be touched.
    old_radome_id = None if keep_radome else _find_open_child(w, station_eid, "radome")
    old_radome_model: Optional[str] = None
    if old_radome_id is not None:
        rad_hist = w.get_entity_history(old_radome_id)
        if isinstance(rad_hist, dict):
            old_radome_model = _device_attribute(rad_hist, "model")

    igs_radome: Optional[str] = None
    # cfg antenna_radome is written only when this run actually decided
    # something about the radome. Left None, cfg keeps whatever it had: with no
    # radome in TOS and no flag we have made no decision, and asserting "NONE"
    # from TOS's mere absence would clobber a real cfg value on a station whose
    # radome simply was never registered.
    radome_cfg_value: Optional[str] = None
    radome_plan: str
    if keep_radome:
        radome_plan = "kept (same physical unit re-fitted)"
    elif radome is not None:
        igs_radome = validate_model("radome", radome or "NONE")
        radome_cfg_value = igs_radome
        radome_plan = (
            "removed (none fitted)" if igs_radome == "NONE" else f"{igs_radome} (new)"
        )
    elif old_radome_model:
        # Carried forward — an inferred write, so name it in the plan where the
        # dry-run line will show it.
        igs_radome = validate_model("radome", old_radome_model)
        radome_cfg_value = igs_radome
        radome_plan = f"{igs_radome} (new unit, model carried forward)"
    else:
        radome_plan = "none in TOS and no --radome — cfg left as-is"

    # --- cfg composite height: ARP + monument offset ----------------------
    # Resolved BEFORE any write so an unresolvable height aborts with TOS
    # untouched rather than half-swapped.
    cfg_height = cfg_antenna_height
    monument_height: Optional[str] = None
    if cfg_height is None and not skip_cfg:
        monument_id = _find_open_child(w, station_eid, "monument")
        if monument_id is not None:
            mon_hist = w.get_entity_history(monument_id)
            if isinstance(mon_hist, dict):
                monument_height = _device_attribute(mon_hist, "monument_height")
        if monument_height is None:
            raise CfgOperationError(
                f"{station_id}: cannot derive the stations.cfg composite "
                f"antenna_height — no open monument child carries a "
                f"monument_height. stations.cfg antenna_height = antenna ARP + "
                f"monument offset; writing the bare ARP would understate it. "
                f"Pass --cfg-antenna-height explicitly (or --no-cfg), and "
                f"consider `cfg add-monument` so the split is recorded."
            )
        try:
            cfg_height = f"{float(antenna_height) + float(monument_height):.4f}"
        except (TypeError, ValueError) as exc:
            raise CfgOperationError(
                f"{station_id}: non-numeric height component "
                f"(antenna={antenna_height!r}, monument={monument_height!r}) — "
                f"pass --cfg-antenna-height explicitly."
            ) from exc

    result = OperationResult(
        operation="replace-antenna",
        station_id=station_id,
        serial=ant_serial,
        date=eff_date,
        tos_changes={
            "plan": {
                "old_serial": old_serial,
                "old_model": old_model,
                "new_serial": ant_serial,
                "new_model": igs_model,
                "synthetic_serial": synthetic,
                "old_radome_model": old_radome_model,
                "radome": radome_plan,
                "monument_height": monument_height,
            }
        },
        dry_run=dry_run,
    )

    # --- 1: retire the old antenna FIRST ----------------------------------
    # Retire-before-create keeps the station at no more than one open antenna
    # at any instant: a mid-run failure leaves it momentarily antenna-less
    # (honest, recoverable) rather than with two simultaneously-open antennas,
    # which is precisely the state that makes current_session() — and every
    # RINEX header and station.info line derived from it — ambiguous.
    warehouse_eid = _b9_eid(w, warehouse=warehouse) if warehouse else None
    result.tos_changes["retire_old_antenna"] = _retire_old_child(
        w, old_id, eff_date, to_warehouse_eid=warehouse_eid
    )
    _apply_device_attribute_transitions(
        w,
        old_id,
        eff_date,
        device_status=old_status,
        device_comment=old_comment,
        result=result,
    )

    # --- 2: radome — follows the antenna unless keep_radome ---------------
    # `old_radome_id` was resolved during the pre-write plan and is already
    # None under keep_radome, so this whole block is a no-op in that case.
    if old_radome_id is not None:
        result.tos_changes["retire_old_radome"] = _retire_old_child(
            w, old_radome_id, eff_date, to_warehouse_eid=warehouse_eid
        )
    if igs_radome is not None and igs_radome != "NONE":
        rad_serial = (
            str(radome_serial).strip()
            if radome_serial and str(radome_serial).strip()
            else synthetic_serial("radome", station_id, eff_date)
        )
        rad_attrs = build_required_attributes(rad_serial, igs_radome, owner, eff_date)
        _rad_id, rad_created, rad_join = _create_and_join_device(
            w,
            subtype="radome",
            attributes=rad_attrs,
            station_eid=station_eid,
            eff_date=eff_date,
            dry_run=dry_run,
        )
        result.tos_changes["new_radome_serial"] = rad_serial
        result.tos_changes["new_radome_create"] = rad_created
        result.tos_changes["new_radome_join"] = rad_join

    # --- 3: create + join the new antenna ---------------------------------
    if comment is None and synthetic:
        comment = "antenna serial unknown at install — synthetic placeholder"
    attrs = build_antenna_attributes(
        serial=ant_serial,
        model=igs_model,
        owner=owner,
        date_start=eff_date,
        antenna_height=str(antenna_height),
    )
    if comment:
        attrs.append(
            {
                "code": "comment",
                "value": comment,
                "date_from": eff_date,
                "date_to": None,
            }
        )
    result.tos_changes["new_antenna_attributes"] = attrs
    _new_id, created, join = _create_and_join_device(
        w,
        subtype="antenna",
        attributes=attrs,
        station_eid=station_eid,
        eff_date=eff_date,
        dry_run=dry_run,
    )
    result.tos_changes["new_antenna_create"] = created
    result.tos_changes["new_antenna_join"] = join

    # Pin the ARP height on the device we just joined — even when it was
    # REUSED. The canonical workflow warehouses an antenna first
    # (`tos device add --subtype antenna …`), which writes only the canonical
    # attributes: serial/model/owner/status/date_start, NO antenna_height. And
    # _create_and_join_device deliberately leaves a pre-registered device's
    # attributes alone (the join is what an install is about). Without this,
    # --antenna-height would be silently dropped for exactly the antennas that
    # went through warehouse intake, and RINEX 'ANTENNA: DELTA H' would sit at
    # 0.0 — the failure this verb makes --antenna-height mandatory to prevent.
    # Idempotent: for a freshly created device the value is already correct.
    if _new_id is not None:
        result.tos_changes["antenna_height_set"] = w.upsert_attribute_value(
            int(_new_id), "antenna_height", str(antenna_height), eff_date
        )

    # --- 4: vitjun --------------------------------------------------------
    if not skip_vitjun:
        old_label = f"{old_model or '?'} {old_serial or '?'}".strip()
        work = vitjun or f"Skipt um loftnet: {old_label} → {igs_model} {ant_serial}"
        if vitjun is None:
            # The radome went up or came down in the same visit — say so, so the
            # vitjun reads as the full record of what happened on the mast.
            if old_radome_id is not None and igs_radome and igs_radome != "NONE":
                work += f" (skipt um raðhlíf: {old_radome_model or '?'} → {igs_radome})"
            elif igs_radome and igs_radome != "NONE":
                work += f" (raðhlíf sett upp: {igs_radome})"
            elif old_radome_id is not None:
                work += f" (raðhlíf fjarlægð: {old_radome_model or '?'})"
            elif keep_radome:
                work += " (sama raðhlíf sett aftur á)"
        vit = w.add_maintenance_visit(
            station_eid,
            start_time=eff_date,
            maintenance_type="on_site",
            participants=participants,
            reasons=["change"],
            work=work,
        )
        result.tos_changes["vitjun"] = vit
        result.vitjun_id = vit.get("id_maintenance")

    # --- 5: stations.cfg --------------------------------------------------
    if not skip_cfg and not dry_run:
        updates: Dict[str, Optional[str]] = {
            "antenna_type": igs_model,
            "antenna_serial": ant_serial,
            "antenna_height": cfg_height,
            "rinex_config_valid_from": rinex_valid_from
            or _default_rinex_valid_from(eff_date),
        }
        # None = this run made no radome decision (--keep-radome, or nothing in
        # TOS and no flag) → leave cfg alone. See the radome plan block above.
        if radome_cfg_value is not None:
            updates["antenna_radome"] = radome_cfg_value
        target_cfg = _resolve_cfg_path(cfg_path)
        result.cfg_changes = _apply_cfg_updates(target_cfg, station_id, updates)
    return result


def replace_radome(
    station_id: str,
    *,
    new_model: str,
    new_serial: Optional[str] = None,
    owner: str = "Jarðeðlismælihópur",
    date: Optional[str] = None,
    old_status: Optional[str] = None,
    old_comment: Optional[str] = None,
    comment: Optional[str] = None,
    vitjun: Optional[str] = None,
    participants: str = "",
    warehouse: Optional[str] = None,
    skip_vitjun: bool = False,
    skip_cfg: bool = False,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
) -> OperationResult:
    """Swap a station's radome in TOS (Pattern-2) + ``stations.cfg``, antenna untouched.

    The radome-only half of :func:`replace_antenna`. A radome is screwed onto
    the antenna and usually travels with it — but the two are independent TOS
    device children of the station, and a radome does get replaced on its own
    (cracked, snow-loaded, upgraded to a different type over the same antenna).
    This verb covers exactly that case; when both change, use
    ``replace-antenna``, which handles the radome in the same command.

      1. Retire the open radome, if any: close its station join at ``date`` (or
         reparent to ``warehouse``) and apply ``old_status``/``old_comment``.
      2. Create the new ``radome`` device and open its station join.
      3. Write a Breyting vitjun ("Skipt um raðhlíf: … → …").
      4. Update ``stations.cfg[antenna_radome]``.

    With **no** open radome the verb still creates and joins the new one — the
    "a radome was fitted where there wasn't one" case — mirroring how
    :func:`replace_modem` handles a station with no modem on record. Use
    ``new_model="NONE"`` for the inverse (radome removed, none refitted): the
    old join is closed, nothing is created, and cfg records ``NONE``.

    Args:
        station_id: 4-char marker of the station.
        new_model: New radome IGS code (e.g. ``"SCIS"``), or ``"NONE"`` to
            record removal without a replacement.
        new_serial: Radome serial; ``None`` → synthetic
            ``radome-<STID>-<YYYYMMDD>`` (the fleet convention — radomes are
            rarely serialised).
        owner: TOS owner attribute for the new device.
        date: When the swap happened. Default now; bare date → noon.
        old_status / old_comment: Pattern-2 transitions on the OLD radome.
        comment: Free-text comment attribute on the NEW radome.
        vitjun: Override the auto-derived vitjun text.
        participants: Comma-separated emails for the vitjun.
        warehouse: Reparent the old radome instead of leaving it parentless.
            Off by default, as for :func:`replace_antenna`.
        skip_vitjun / skip_cfg: Skip the vitjun / ``stations.cfg`` step.
        dry_run / writer / cfg_path: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``operation="replace-radome"``.

    Raises:
        CfgOperationError: When the station has more than one open radome.
        ValueError: When ``new_model`` is not a known IGS radome code — raised
            before any write, so a typo cannot leave the station bare.
    """
    from tostools.device import (
        build_required_attributes,
        synthetic_serial,
        validate_model,
    )

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)
    eff_date = _visit_default_time(date)

    # Validate before any write — same discipline as replace_antenna: a bad
    # code discovered after the old join closed would leave the station bare.
    igs_radome = validate_model("radome", new_model or "NONE")

    open_radomes = _find_open_children(w, station_eid, "radome")
    if len(open_radomes) > 1:
        raise CfgOperationError(
            f"{station_id} has {len(open_radomes)} open radome children "
            f"({', '.join(str(i) for i in open_radomes)}) — ambiguous. Close "
            f"the stale one with `cfg close-join --station {station_id} "
            f"--subtype radome` first, then re-run."
        )
    old_id = open_radomes[0] if open_radomes else None
    old_model: Optional[str] = None
    if old_id is not None:
        old_hist = w.get_entity_history(old_id)
        if isinstance(old_hist, dict):
            old_model = _device_attribute(old_hist, "model")

    rad_serial = (
        str(new_serial).strip()
        if new_serial and str(new_serial).strip()
        else synthetic_serial("radome", station_id, eff_date)
    )

    result = OperationResult(
        operation="replace-radome",
        station_id=station_id,
        serial=rad_serial if igs_radome != "NONE" else None,
        date=eff_date,
        tos_changes={
            "plan": {
                "old_model": old_model,
                "new_model": igs_radome,
                "new_serial": rad_serial if igs_radome != "NONE" else None,
            }
        },
        dry_run=dry_run,
    )

    # 1: retire first — never two open radomes at once (same reasoning as the
    # antenna: a second open child makes current_session() ambiguous).
    if old_id is not None:
        warehouse_eid = _b9_eid(w, warehouse=warehouse) if warehouse else None
        result.tos_changes["retire_old_radome"] = _retire_old_child(
            w, old_id, eff_date, to_warehouse_eid=warehouse_eid
        )
        _apply_device_attribute_transitions(
            w,
            old_id,
            eff_date,
            device_status=old_status,
            device_comment=old_comment,
            result=result,
        )

    # 2: create + join the new radome (nothing to create when removing).
    if igs_radome != "NONE":
        attrs = build_required_attributes(rad_serial, igs_radome, owner, eff_date)
        if comment:
            attrs.append(
                {
                    "code": "comment",
                    "value": comment,
                    "date_from": eff_date,
                    "date_to": None,
                }
            )
        result.tos_changes["new_radome_attributes"] = attrs
        _new_id, created, join = _create_and_join_device(
            w,
            subtype="radome",
            attributes=attrs,
            station_eid=station_eid,
            eff_date=eff_date,
            dry_run=dry_run,
        )
        result.tos_changes["new_radome_create"] = created
        result.tos_changes["new_radome_join"] = join

    # 3: vitjun.
    if not skip_vitjun:
        if igs_radome == "NONE":
            work = vitjun or f"Raðhlíf fjarlægð: {old_model or '?'}"
        elif old_model:
            work = vitjun or f"Skipt um raðhlíf: {old_model} → {igs_radome}"
        else:
            work = vitjun or f"Sett upp raðhlíf: {igs_radome}"
        vit = w.add_maintenance_visit(
            station_eid,
            start_time=eff_date,
            maintenance_type="on_site",
            participants=participants,
            reasons=["change"],
            work=work,
        )
        result.tos_changes["vitjun"] = vit
        result.vitjun_id = vit.get("id_maintenance")

    # 4: stations.cfg — the radome model is the only cfg field this touches.
    # antenna_height is deliberately NOT recomputed: a radome carries no ARP
    # offset, so swapping one cannot change the mark→ARP composite.
    if not skip_cfg and not dry_run:
        target_cfg = _resolve_cfg_path(cfg_path)
        result.cfg_changes = _apply_cfg_updates(
            target_cfg, station_id, {"antenna_radome": igs_radome}
        )
    return result


def _station_router_ip(
    station_id: str,
    cfg_path: Optional[Path] = None,
) -> Optional[str]:
    """Read ``router_ip`` from ``stations.cfg`` for use as the probe host."""
    import configparser

    path = _resolve_cfg_path(cfg_path)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    if not parser.has_section(station_id):
        return None
    return parser.get(station_id, "router_ip", fallback=None) or None


# ---------------------------------------------------------------------------
# correct-date — Pattern 4 historical date correction (general)
# ---------------------------------------------------------------------------


def _scan_entity_ids(writer: TOSWriter, station_eid: int) -> List[int]:
    """Station + its child devices + their children (2-hop), de-duplicated.

    Covers the realistic swap topologies: a receiver/modem/SIM as a direct
    child of the station, and a SIM as a child of a modem.
    """
    ids: List[int] = [station_eid]
    seen = {station_eid}

    def _add_children(parent_id: int) -> List[int]:
        added: List[int] = []
        hist = writer.get_entity_history(parent_id) or {}
        for c in hist.get("children_connections") or []:
            cid = c.get("id_entity_child")
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
                added.append(cid)
        return added

    level1 = _add_children(station_eid)
    for dev in level1:
        _add_children(dev)
    return ids


def correct_date(
    station_id: str,
    from_date: str,
    to_date: str,
    *,
    writer: Optional[TOSWriter] = None,
    dry_run: bool = True,
) -> OperationResult:
    """Shift every TOS boundary at ``from_date`` to ``to_date`` for a station.

    Generalises the one-off "the swap was recorded on the wrong day" fix
    (Pattern 4 historical correction). Scans the station, its child devices
    and their children (e.g. a SIM under a modem), and the station's
    maintenance visits, and shifts every boundary whose instant equals
    ``from_date`` to ``to_date``:

      * entity_connection ``time_from`` / ``time_to`` (device joins)
      * attribute_value ``date_from`` / ``date_to`` (and ``value`` when the
        value is itself the from-instant, e.g. a ``date_start`` attribute)
      * maintenance ``start_time`` / ``end_time`` (the swap vitjun)

    Match is on the exact instant (bare ``YYYY-MM-DD`` → noon, the field-work
    convention), so unrelated same-day boundaries are never touched. Dry-run
    by default; on commit, re-reads every touched entity and asserts no
    ``from_date`` boundary remains.
    """
    w = _resolve_writer(writer, dry_run)
    eid = _resolve_station(w, station_id)

    from_iso = w._tos_date(_visit_default_time(from_date))
    to_iso = w._tos_date(_visit_default_time(to_date))
    if from_iso == to_iso:
        raise CfgOperationError(
            f"--from and --to resolve to the same instant ({from_iso}); "
            f"nothing to correct."
        )

    def _at_from(value: Optional[str]) -> bool:
        return bool(value) and w._tos_date(value) == from_iso

    entity_ids = _scan_entity_ids(w, eid)
    changes: List[Dict[str, Any]] = []
    conn_seen: set = set()

    for ent in entity_ids:
        eh = w.get_entity_history(ent) or {}

        # Attributes (date_from / date_to / a datetime-valued `value`).
        for a in eh.get("attributes") or []:
            fields = {}
            for fld in ("date_from", "date_to", "value"):
                if _at_from(a.get(fld)):
                    fields[fld] = to_iso
            if fields:
                changes.append(
                    {
                        "kind": "attr",
                        "id": a.get("id_attribute_value"),
                        "fields": fields,
                        "label": f"{a.get('code')} (entity {ent})",
                        "old": {k: a.get(k) for k in fields},
                    }
                )

        # Connections: children_connections (ent as parent) + parent_history
        # (ent as child — catches warehouse-return joins). Same join id can
        # appear in both views; dedupe by connection id.
        conn_rows = [
            (c.get("id_entity_connection"), c)
            for c in (eh.get("children_connections") or [])
        ]
        try:
            ph = w._request("GET", f"/entity/parent_history/{ent}") or []
        except Exception:  # noqa: BLE001 — parent_history optional per entity
            ph = []
        conn_rows += [(c.get("id"), c) for c in ph]

        for conn_id, c in conn_rows:
            if conn_id is None or conn_id in conn_seen:
                continue
            conn_seen.add(conn_id)
            fields = {}
            for fld in ("time_from", "time_to"):
                if _at_from(c.get(fld)):
                    fields[fld] = to_iso
            if fields:
                changes.append(
                    {
                        "kind": "join",
                        "id": conn_id,
                        "fields": fields,
                        "label": f"join {conn_id} (entity {ent})",
                        "old": {k: c.get(k) for k in fields},
                    }
                )

    # Maintenance visits on the station.
    for v in w.list_maintenance_visits(eid) or []:
        fields = {}
        for fld in ("start_time", "end_time"):
            if _at_from(v.get(fld)):
                fields[fld] = to_iso
        if fields:
            changes.append(
                {
                    "kind": "vitjun",
                    "id": v.get("id"),
                    "fields": fields,
                    "label": f"vitjun {v.get('id')}",
                    "old": {k: v.get(k) for k in fields},
                }
            )

    # Apply (no-op in dry-run — TOSWriter returns DryRunResult).
    for ch in changes:
        if ch["kind"] == "join":
            w.patch_entity_connection(ch["id"], **ch["fields"])
        elif ch["kind"] == "attr":
            w.patch_attribute_value(ch["id"], **ch["fields"])
        elif ch["kind"] == "vitjun":
            w.update_maintenance_visit(ch["id"], **ch["fields"])

    result = OperationResult(
        operation="correct-date",
        station_id=station_id,
        date=f"{from_iso} → {to_iso}",
        tos_changes={"from": from_iso, "to": to_iso, "changes": changes},
        dry_run=dry_run,
    )

    if not dry_run:
        leftover: List[str] = []
        for ent in entity_ids:
            eh = w.get_entity_history(ent) or {}
            for a in eh.get("attributes") or []:
                if any(_at_from(a.get(k)) for k in ("date_from", "date_to", "value")):
                    leftover.append(f"attr {a.get('id_attribute_value')}")
            for c in eh.get("children_connections") or []:
                if _at_from(c.get("time_from")) or _at_from(c.get("time_to")):
                    leftover.append(f"conn {c.get('id_entity_connection')}")
        for v in w.list_maintenance_visits(eid) or []:
            if _at_from(v.get("start_time")) or _at_from(v.get("end_time")):
                leftover.append(f"vitjun {v.get('id')}")
        result.tos_changes["leftover"] = leftover

    return result
