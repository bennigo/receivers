"""Field manifest: declarative mapping from cfg keys to source extractors.

Each :class:`FieldSpec` describes one row of the reconciliation table:

* ``cfg_key`` — the key as it appears in ``stations.cfg``
* ``label`` — short human-readable name for prompts and tables
* ``receiver_extract`` — pulls the value from the receiver health identity
  dict; ``None`` means the field can't be derived from a live receiver
* ``tos_extract`` — pulls the value from a TOS station record; ``None``
  means the field is not present in TOS
* ``equal`` — custom equality check (defaults to case-insensitive string
  compare after normalisation); used for fuzzy matches like
  ``PolaRX5 == PolaRx5``
* ``normalize`` — value transform applied before storing/comparing

Adding a new reconcilable field is a matter of appending a ``FieldSpec`` to
``FIELDS`` here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

from tostools.standards.igs_equipment import (
    to_igs_antenna,
    to_igs_radome,
    to_igs_receiver,
)

from . import tos_adapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# Common placeholder strings used in stations.cfg / receivers when a real
# value is unknown. Treating these as ``None`` turns "cfg='0000000000' vs
# rx='Unknown'" from a CONFLICT into a MISSING — both sides admit ignorance.
_SERIAL_PLACEHOLDERS = frozenset({"unknown", "n/a", "na", "none", "—", "-"})


def _zero_default(value: Optional[str]) -> Optional[str]:
    """Normalize antenna offset fields where absence means zero.

    Maps None (field not in cfg) and zero-equivalent strings ("0", "0.0",
    "0.0000") to "0.0000" so a missing cfg entry compares as zero rather
    than triggering MISSING.  Non-zero values are returned as stripped strings.
    """
    s = _strip(value)
    if s is None:
        return "0.0000"
    try:
        if float(s) == 0.0:
            return "0.0000"
    except ValueError:
        pass
    return s


def _strip_placeholder(value: Optional[str]) -> Optional[str]:
    """Strip + treat known serial-number placeholders as missing.

    Used for ``receiver_serial`` and ``antenna_serial`` where operators and
    receivers both surface "I don't have a real value" via well-known
    sentinels (``"0000000000"``, ``"Unknown"``, ``""``).

    Also strips TOS synthetic device identifiers of the form
    ``{subtype}-{STATION}-{YYYYMMDD}`` (e.g. ``antenna-AFST-20210527``,
    ``receiver-OLKE-20001017``). TOS generates these when no real serial is
    recorded; they carry no hardware meaning and must not be written to
    stations.cfg. The date separator is optional — TOS has produced malformed
    variants without it (e.g. ``antenna-RVIT20150625``), which must also strip.
    """
    s = _strip(value)
    if s is None:
        return None
    if s.lower() in _SERIAL_PLACEHOLDERS:
        return None
    # All-zero serials of any length (0, 00, 000000, 0000000000, …).
    if s and set(s) == {"0"}:
        return None
    # TOS synthetic device identifier: {subtype}-{4-char station}[-]{YYYYMMDD},
    # any subtype (antenna/receiver/…), with or without the date dash.
    if re.match(r"^[a-z]+-[A-Za-z0-9]{4}-?\d{8}$", s, re.IGNORECASE):
        return None
    return s


def _normalize_firmware_version(value: Optional[str]) -> Optional[str]:
    """Normalize firmware version strings for robust comparison.

    Two known format families produce false-positive conflicts:

    1. Trimble NP/SP strings — ``"NP 4.81 / SP 4.81"`` (operator-entered in
       stations.cfg) vs ``"4.81"`` (what the health probe extracts).  Strip
       the ``NP`` prefix and everything from ``/ SP`` onward.

    2. Septentrio compact two-component — ``"5.50"`` means ``5.5.0``
       (major=5, combined minor+patch written as two digits).  Expand to the
       canonical three-component form so ``"5.50" == "5.5.0"`` and
       ``"5.35" == "5.3.5"``.
    """
    s = _strip(value)
    if s is None:
        return None

    # Strip Trimble NP/SP prefix: "NP 4.81 / SP 4.81" → "4.81"
    np_match = re.match(r"^NP\s+([\d.]+(?:-[\w.]+)?)", s, re.IGNORECASE)
    if np_match:
        s = np_match.group(1)

    # Expand Septentrio compact two-component: "5.50" → "5.5.0", "5.35" → "5.3.5"
    # Only applies when the minor part is exactly two ASCII digits.
    parts = s.split(".")
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 2:
        try:
            int(parts[0])  # guard: major must be numeric
            s = f"{parts[0]}.{parts[1][0]}.{parts[1][1]}"
        except ValueError:
            pass

    return s.lower()


def _strip_lower_eq(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return a.strip().lower() == b.strip().lower()


def _receiver_type_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Receiver type equality through the fingerprint matcher.

    ``PolaRX5`` (configured) and ``PolaRx5`` (reported) are considered equal
    because both resolve to the same canonical type via
    :func:`receiver_fingerprint.identify_receiver_type`.
    """
    if a is None or b is None:
        return False
    if a.strip().lower() == b.strip().lower():
        return True
    try:
        from ..health.receiver_fingerprint import identify_receiver_type
    except ImportError:
        return False
    type_a = identify_receiver_type({"receiver_model": a})
    type_b = identify_receiver_type({"receiver_model": b})
    if type_a is None or type_b is None:
        return False
    return type_a == type_b


def _receiver_type_to_cfg(value: Optional[str]) -> Optional[str]:
    """Map IGS-style receiver names to the short cfg vocabulary on write.

    TOS surfaces ``"SEPT POLARX5"`` while ``stations.cfg`` (and the rest of
    the codebase) uses the short form ``"PolaRX5"``. Without this mapping,
    accepting a TOS suggestion writes an unrecognised value that breaks
    type-detection.

    Returns the canonical short form when the value matches a known
    fingerprint pattern; otherwise returns the input unchanged so unknown
    types aren't silently corrupted.
    """
    if value is None:
        return None
    try:
        from ..health.receiver_fingerprint import identify_receiver_type
    except ImportError:
        return value
    canonical = identify_receiver_type({"receiver_model": value})
    return canonical if canonical is not None else value


def _approx_eq(decimals: int) -> Callable[[Optional[str], Optional[str]], bool]:
    """Equality up to ``decimals`` decimal places, for floats stored as strings."""

    def _check(a: Optional[str], b: Optional[str]) -> bool:
        if a is None or b is None:
            return False
        try:
            return round(float(a), decimals) == round(float(b), decimals)
        except (TypeError, ValueError):
            return a.strip() == b.strip()

    return _check


def _abs_tol(tol: float) -> Callable[[Optional[str], Optional[str]], bool]:
    """Equality with an absolute tolerance, for floats stored as strings.

    Used for position fields where receiver values come from a real-time
    PVT solution (~1-2 m accuracy) and only need to roughly match the
    surveyed coordinates in TOS to confirm "right station".
    """

    def _check(a: Optional[str], b: Optional[str]) -> bool:
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return a.strip() == b.strip()

    return _check


# Default position tolerance (degrees for lat/lon, meters for height).
# At Iceland latitude (~64°): 2e-5° lat ≈ 2.2 m; 5e-5° lon ≈ 2.4 m.
# CLI passes a meters-based override; values_equal is rebuilt at call time.
DEFAULT_POSITION_TOLERANCE_M = 2.0


def _meters_to_lat_deg(meters: float) -> float:
    return meters / 111111.0  # ~1 deg latitude = 111.111 km


def _meters_to_lon_deg(meters: float, latitude_deg: float = 64.0) -> float:
    import math

    return meters / (111111.0 * max(math.cos(math.radians(latitude_deg)), 0.01))


def position_equality_for(
    field_key: str, tolerance_m: float
) -> Callable[[Optional[str], Optional[str]], bool]:
    """Build an equality predicate for a position field at the given tolerance.

    The CLI passes a meters value; lat/lon are converted to degree tolerances
    using an Iceland-centric latitude (64°) for the longitude factor — good
    enough for a sanity check across the network.
    """
    if field_key == "latitude":
        return _abs_tol(_meters_to_lat_deg(tolerance_m))
    if field_key == "longitude":
        return _abs_tol(_meters_to_lon_deg(tolerance_m))
    if field_key == "height":
        return _abs_tol(tolerance_m)
    raise ValueError(f"position_equality_for: unsupported field {field_key!r}")


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


def _identity(value: Optional[str]) -> Optional[str]:
    """Default :attr:`FieldSpec.cfg_format` — write source values verbatim."""
    return value


@dataclass(frozen=True)
class TOSComponent:
    """One component of a composite TOS field (e.g. antenna.antenna_height)."""

    label: str  # display label, e.g. "antenna.antenna_height"
    entity: str  # TOS entity subtype, e.g. "antenna" or "monument"
    attribute_code: str  # TOS attribute code, e.g. "antenna_height"
    key: str  # single letter for interactive prompt, e.g. "a" or "m"
    current_value_key: str  # key to read current value from tos_adapter session dict


@dataclass(frozen=True)
class FieldSpec:
    cfg_key: str
    label: str
    receiver_extract: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    tos_extract: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    equal: Optional[Callable[[Optional[str], Optional[str]], bool]] = None
    normalize: Callable[[Optional[str]], Optional[str]] = _strip
    # Vocabulary mapping applied at WRITE time, not comparison time. Used for
    # fields where the source vocabulary differs from cfg's (e.g. TOS uses the
    # IGS name "SEPT POLARX5"; cfg uses "PolaRX5"). Default is identity.
    cfg_format: Callable[[Optional[str]], Optional[str]] = _identity
    description: str = ""
    # If False, the receiver value is for QC/flagging only — never used as
    # an auto-fill suggestion when cfg is missing. Used for fields where the
    # receiver carries operator-typed values (antenna metadata) or PVT
    # solutions that should not overwrite surveyed coordinates from TOS.
    receiver_authoritative: bool = True
    # If True, the receiver is the ground-truth source for this field. In
    # interactive mode, the default action for a conflict becomes "accept
    # receiver value and push it to TOS in one step." The user can still
    # choose any other action; --no-receiver-primary disables this default.
    # Only applies to hardware-identity fields (type, serial, firmware) where
    # the receiver hardware is literally the authoritative source. Position
    # fields are flag-only even though the receiver can supply them.
    receiver_primary: bool = False
    # If True, ``cfg sync-from-tos`` overwrites this cfg field with the current
    # TOS value (TOS→cfg). This is DISTINCT from ``tos_writable`` (cfg→TOS push
    # via ``reconcile --push-tos``): a field can be TOS-canonical for cfg
    # without being safe to push cfg→TOS. ``rinex_marker_number`` (IERS DOMES)
    # is the canonical example — TOS/IGS owns it, cfg must follow it but must
    # never push its (often wrong 4-char) value up to TOS.
    sync_from_tos: bool = False
    # TOS write-side metadata. ``tos_attribute_code=None`` means the field is
    # not writable via ``--push-tos`` (e.g. composite fields).
    # ``tos_target_entity`` is the entity type that owns the attribute:
    # "station" | "gnss_receiver" | "antenna" | "radome".
    # ``tos_format`` transforms the value from receiver/cfg vocabulary to TOS
    # attribute vocabulary before the write (default: identity).
    tos_attribute_code: Optional[str] = None
    tos_target_entity: Optional[str] = None
    tos_format: Callable[[Optional[str]], Optional[str]] = _identity
    # Optional callable that takes the raw TOS station dict and returns a
    # human-readable breakdown string shown in the interactive prompt.
    # Used for composite fields (antenna_height/east/north) where the TOS
    # value is a sum of two attributes and the operator needs to see the
    # split to know which one to fix in TOS.
    tos_breakdown: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    # For composite fields: individual TOS components that can be edited and
    # pushed to TOS separately.  Each TOSComponent has its own entity and
    # attribute code.  When set, the interactive prompt offers lettered
    # options ([a]/[m]) for editing each component independently.
    tos_components: Optional[List[TOSComponent]] = None

    @property
    def tos_writable(self) -> bool:
        """True if this field can be pushed to TOS via ``--push-tos``."""
        return (
            self.tos_attribute_code is not None and self.tos_target_entity is not None
        )

    def values_equal(self, a: Optional[str], b: Optional[str]) -> bool:
        a_n = self.normalize(a)
        b_n = self.normalize(b)
        if a_n is None and b_n is None:
            return True
        if a_n is None or b_n is None:
            return False
        if self.equal is not None:
            return self.equal(a_n, b_n)
        return _strip_lower_eq(a_n, b_n)


# ---------------------------------------------------------------------------
# Field manifest
# ---------------------------------------------------------------------------

FIELDS: List[FieldSpec] = [
    # Receiver identity — both receiver and TOS can supply these.
    FieldSpec(
        cfg_key="receiver_type",
        label="Receiver Type",
        receiver_extract=lambda identity: identity.get("receiver_model"),
        tos_extract=tos_adapter.current_receiver_model,
        equal=_receiver_type_eq,
        cfg_format=_receiver_type_to_cfg,
        description="Receiver model/type (e.g. PolaRX5, NetR9)",
        receiver_primary=True,
        sync_from_tos=True,
        tos_attribute_code="model",
        tos_target_entity="gnss_receiver",
        tos_format=to_igs_receiver,
    ),
    FieldSpec(
        cfg_key="receiver_serial",
        label="Receiver Serial",
        receiver_extract=lambda identity: identity.get("serial_number"),
        tos_extract=tos_adapter.current_receiver_serial,
        normalize=_strip_placeholder,
        description="Receiver serial number",
        receiver_primary=True,
        sync_from_tos=True,
        tos_attribute_code="serial_number",
        tos_target_entity="gnss_receiver",
    ),
    FieldSpec(
        cfg_key="receiver_firmware_version",
        label="Receiver Firmware",
        receiver_extract=lambda identity: identity.get("firmware_version"),
        tos_extract=tos_adapter.current_receiver_firmware,
        normalize=_normalize_firmware_version,
        description="Active firmware version reported by the receiver",
        receiver_primary=True,
        sync_from_tos=True,
        tos_attribute_code="firmware_version",
        tos_target_entity="gnss_receiver",
    ),
    # Antenna fields — TOS is canonical; receiver values are operator-typed
    # so they're flagged on mismatch but never auto-fill cfg from receiver alone.
    FieldSpec(
        cfg_key="antenna_type",
        label="Antenna Type",
        receiver_extract=lambda identity: identity.get("antenna_type"),
        tos_extract=tos_adapter.current_antenna_model,
        receiver_authoritative=False,
        description="IGS-style antenna model code",
        sync_from_tos=True,
        tos_attribute_code="model",
        tos_target_entity="antenna",
        tos_format=to_igs_antenna,
    ),
    FieldSpec(
        cfg_key="antenna_serial",
        label="Antenna Serial",
        receiver_extract=lambda identity: identity.get("antenna_serial"),
        tos_extract=tos_adapter.current_antenna_serial,
        normalize=_strip_placeholder,
        receiver_authoritative=False,
        description="Antenna serial number",
        sync_from_tos=True,
        tos_attribute_code="serial_number",
        tos_target_entity="antenna",
    ),
    FieldSpec(
        cfg_key="antenna_radome",
        label="Radome",
        receiver_extract=lambda identity: identity.get("antenna_radome"),
        tos_extract=tos_adapter.current_radome_model,
        receiver_authoritative=False,
        description="Radome model code (NONE if absent)",
        sync_from_tos=True,
        tos_attribute_code="model",
        tos_target_entity="radome",
        tos_format=to_igs_radome,
    ),
    FieldSpec(
        cfg_key="antenna_height",
        label="Antenna Height",
        receiver_extract=lambda identity: (
            None
            if identity.get("antenna_height_delta") is None
            else f"{identity['antenna_height_delta']:.4f}"
        ),
        tos_extract=tos_adapter.current_antenna_height,
        equal=_approx_eq(4),
        receiver_authoritative=False,
        description="Antenna height above mark including monument offset (m)",
        tos_breakdown=tos_adapter.antenna_height_breakdown,
        tos_components=[
            TOSComponent(
                "antenna.antenna_height",
                "antenna",
                "antenna_height",
                "a",
                "antenna_height",
            ),
            TOSComponent(
                "monument.monument_height",
                "monument",
                "monument_height",
                "m",
                "monument_height",
            ),
        ],
        # Composite of antenna.antenna_height + monument.monument_height — cannot
        # be written back without knowing the split; tos_attribute_code stays None.
    ),
    FieldSpec(
        cfg_key="antenna_east",
        label="Antenna East",
        receiver_extract=lambda identity: (
            None
            if identity.get("antenna_east_delta") is None
            else f"{identity['antenna_east_delta']:.4f}"
        ),
        tos_extract=tos_adapter.current_antenna_east,
        normalize=_zero_default,
        equal=_approx_eq(4),
        receiver_authoritative=False,
        description="Marker-to-ARP East offset (m); absent from cfg = 0.0000",
        tos_breakdown=tos_adapter.antenna_east_breakdown,
        tos_components=[
            TOSComponent(
                "antenna.antenna_offset_east",
                "antenna",
                "antenna_offset_east",
                "a",
                "antenna_offset_east",
            ),
            TOSComponent(
                "monument.monument_offset_east",
                "monument",
                "monument_offset_east",
                "m",
                "monument_offset_east",
            ),
        ],
        # Composite of antenna + monument offsets — not directly writable.
    ),
    FieldSpec(
        cfg_key="antenna_north",
        label="Antenna North",
        receiver_extract=lambda identity: (
            None
            if identity.get("antenna_north_delta") is None
            else f"{identity['antenna_north_delta']:.4f}"
        ),
        tos_extract=tos_adapter.current_antenna_north,
        normalize=_zero_default,
        equal=_approx_eq(4),
        receiver_authoritative=False,
        description="Marker-to-ARP North offset (m); absent from cfg = 0.0000",
        tos_breakdown=tos_adapter.antenna_north_breakdown,
        tos_components=[
            TOSComponent(
                "antenna.antenna_offset_north",
                "antenna",
                "antenna_offset_north",
                "a",
                "antenna_offset_north",
            ),
            TOSComponent(
                "monument.monument_offset_north",
                "monument",
                "monument_offset_north",
                "m",
                "monument_offset_north",
            ),
        ],
        # Composite of antenna + monument offsets — not directly writable.
    ),
    # Coordinates — TOS is canonical (surveyed); receiver values come from a
    # real-time PVT solution (~1-2 m accuracy) and are used purely as a sanity
    # check that the receiver is at the expected mark. Equality tolerance is
    # rebuilt at CLI invocation from --position-tolerance-m.
    # tos_attribute_code is populated so operator-driven corrections are possible;
    # CLI policy (not manifest) guards against pushing PVT estimates.
    FieldSpec(
        cfg_key="latitude",
        label="Latitude",
        receiver_extract=lambda identity: (
            None
            if identity.get("latitude") is None
            else f"{float(identity['latitude']):.8f}"
        ),
        tos_extract=tos_adapter.station_latitude,
        equal=_abs_tol(_meters_to_lat_deg(DEFAULT_POSITION_TOLERANCE_M)),
        receiver_authoritative=False,
        description="Decimal-degree latitude",
        tos_attribute_code="lat",
        tos_target_entity="station",
    ),
    FieldSpec(
        cfg_key="longitude",
        label="Longitude",
        receiver_extract=lambda identity: (
            None
            if identity.get("longitude") is None
            else f"{float(identity['longitude']):.8f}"
        ),
        tos_extract=tos_adapter.station_longitude,
        equal=_abs_tol(_meters_to_lon_deg(DEFAULT_POSITION_TOLERANCE_M)),
        receiver_authoritative=False,
        description="Decimal-degree longitude",
        tos_attribute_code="lon",
        tos_target_entity="station",
    ),
    FieldSpec(
        cfg_key="height",
        label="Height",
        receiver_extract=lambda identity: (
            None
            if identity.get("height") is None
            else f"{float(identity['height']):.3f}"
        ),
        tos_extract=tos_adapter.station_height,
        equal=_abs_tol(DEFAULT_POSITION_TOLERANCE_M),
        receiver_authoritative=False,
        description="Ellipsoidal height (m)",
        tos_attribute_code="altitude",
        tos_target_entity="station",
    ),
    FieldSpec(
        cfg_key="station_name",
        label="Station Name",
        tos_extract=tos_adapter.station_name,
        description="Long-form station name (TOS-only; receiver MarkerName carries the 4-char ID)",
        sync_from_tos=True,
        tos_attribute_code="name",
        tos_target_entity="station",
    ),
    # IERS DOMES number → RINEX MARKER NUMBER. TOS/IGS is canonical; cfg follows
    # TOS via ``cfg sync-from-tos`` but is NEVER pushed up (no tos_attribute_code,
    # so not tos_writable — reconcile --push-tos won't offer to send cfg's value,
    # which for most of the fleet is the wrong 4-char marker, back to TOS). The
    # RINEX converter reads this cfg key for MARKER NUMBER, so drift here writes
    # the 4-char ID into headers instead of the DOMES (the NYLA cross-contamination).
    FieldSpec(
        cfg_key="rinex_marker_number",
        label="Marker Number (DOMES)",
        # The receiver's ReceiverSetup MarkerNumber field. Flag-only: TOS/IGS
        # owns the DOMES, so a receiver mismatch means the BOX is stale and
        # wants `rec-config --set-domes` — it never means cfg should follow the
        # receiver. Surfacing it is what makes that drift visible at all.
        receiver_extract=lambda identity: identity.get("marker_number"),
        tos_extract=tos_adapter.iers_domes_number,
        receiver_authoritative=False,
        description="IERS DOMES number for RINEX MARKER NUMBER (TOS-canonical)",
        sync_from_tos=True,
    ),
    # Runtime-observed: no receiver health probe or TOS source. The download
    # path records the observed value into cfg_discrepancy when the live FTP
    # behavior differs from cfg (e.g. cfg says 'passive' but only 'active'
    # mode succeeds). The config loader applies any open observation as a
    # runtime override so subsequent downloads start in the right mode.
    # Operator promotes the observation to upstream cfg via cfg reconcile.
    FieldSpec(
        cfg_key="ftp_mode",
        label="FTP Mode",
        receiver_authoritative=True,
        description="Active vs passive FTP. Inferred from runtime handshake, not a probe.",
    ),
]


def with_position_tolerance(tolerance_m: float) -> List[FieldSpec]:
    """Return a copy of FIELDS with position equality bound to ``tolerance_m``.

    The CLI calls this when the user passes ``--position-tolerance-m`` and
    threads the result into ``compare_station`` as ``fields=...`` is opaque to
    a tolerance knob — instead we rebuild the affected specs and let
    :func:`fields_by_key` resolve them.
    """
    overrides: Dict[str, Callable[[Optional[str], Optional[str]], bool]] = {
        "latitude": position_equality_for("latitude", tolerance_m),
        "longitude": position_equality_for("longitude", tolerance_m),
        "height": position_equality_for("height", tolerance_m),
    }
    return [
        (
            replace(spec, equal=overrides[spec.cfg_key])
            if spec.cfg_key in overrides
            else spec
        )
        for spec in FIELDS
    ]


def fields_by_key() -> Dict[str, FieldSpec]:
    return {f.cfg_key: f for f in FIELDS}


def all_keys() -> List[str]:
    return [f.cfg_key for f in FIELDS]
