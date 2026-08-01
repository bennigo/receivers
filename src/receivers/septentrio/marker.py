"""Build identity-safe PolaRX5 marker-number config from stations.cfg.

``rec-config --set-domes`` emits ONLY ``setMarkerParameters`` with the second
argument filled (+ boot save) — it never touches the marker NAME, the station
code, antenna, NTRIP mounts, log sessions, or tracking. It is the marker-side
sibling of :mod:`receivers.septentrio.antenna`, deliberately a separate module
so that ``--set-antenna``'s "never touches the marker" guarantee stays literally
true.

Command signature, from the 5.7.0 Reference Guide's ``getMarkerParameters``
block::

    setMarkerParameters, MarkerName(60), MarkerNumber(20), MarkerType(20),
                         StationCode(10), MonumentIdx, ReceiverIdx

Septentrio treats a blank argument as "leave unchanged" (the same convention
``setAntennaOffset, Main, , , 0.6610`` relies on), so writing the DOMES is a
one-field edit::

    setMarkerParameters, , "10214M001"
    eccf, Current, Boot

Note the 4-char station designator that names RINEX files is *StationCode*
(arg 4), NOT MarkerNumber — the guide's own example is
``setMarkerParameters, , , , LEUV``. Confusing the two is exactly how a 4-char
id ends up in MARKER NUMBER, which this module exists to prevent.

**Policy (bgo, 2026-07-13): MARKER NUMBER carries the IERS DOMES and nothing
else.** Absent a real DOMES the field is left alone rather than filled with the
station id. Enforced here by ``tostools.rinex.domes.domes_or_skip`` — the same
guard the RINEX writers and comparators use — so the rule now holds at the
receiver boundary too, not only in files we generate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Septentrio's MarkerNumber field width (c1[20] in the ReceiverSetup block).
MARKER_NUMBER_MAX = 20


class NoDomesError(ValueError):
    """Raised when a station has no valid IERS DOMES to push.

    Distinct from a plain ``ValueError`` because it is a *skip*, not a
    failure: most of the fleet legitimately has no DOMES, and those stations
    must be quietly left alone rather than reported as errors.
    """


def normalize_domes(value: object) -> str:
    """Return the normalized IERS DOMES, or ``""`` when *value* is not one.

    Thin wrapper over ``tostools.rinex.domes.domes_or_skip`` so this module
    has a single import site for the policy guard, and so callers get the
    identical accept/reject behaviour as the RINEX write and compare paths
    (regex ``^\\d{5}[A-Z]\\d{3}$``; a 4-char station id collapses to ``""``).
    """
    from tostools.rinex.domes import domes_or_skip

    normalized: str = domes_or_skip(value)
    return normalized


def build_domes_commands(domes: object) -> List[str]:
    """Return the set* commands to write MARKER NUMBER, or raise.

    ``setMarkerParameters, , "<DOMES>"`` + boot save. Only the second argument
    is supplied, so the marker name / station code / monument index the box
    already holds survive untouched.

    Raises:
        NoDomesError: when *domes* is blank or not a real IERS DOMES. Callers
            should treat this as "skip this station", never as a reason to
            write something else into the field.
    """
    value = normalize_domes(domes)
    if not value:
        raise NoDomesError(
            f"{str(domes or '').strip()!r} is not an IERS DOMES "
            "(expected NNNNNMNNN, e.g. 10214M001) — MARKER NUMBER carries the "
            "DOMES and nothing else, so the receiver field is left unchanged"
        )
    if len(value) > MARKER_NUMBER_MAX:
        raise ValueError(
            f"DOMES {value!r} exceeds the {MARKER_NUMBER_MAX}-char MarkerNumber field"
        )
    return [
        f'setMarkerParameters, , "{value}"',
        "eccf, Current, Boot",
    ]


def build_domes_commands_from_station_config(
    station_config: Dict[str, Any],
) -> List[str]:
    """Build the DOMES push from a station's stations.cfg entry.

    Reads ``rinex_marker_number`` — flat key first, then the nested ``rinex``
    section — mirroring how ``cfg.reconciler`` resolves cfg values. cfg is
    TOS-canonical for this field (filled by ``cfg sync-from-tos``), so the
    value pushed here is whatever TOS last supplied.

    Raises:
        NoDomesError: when the station has no DOMES (the common case — most of
            the fleet).
    """

    def _get(key: str) -> Optional[str]:
        val = station_config.get(key)
        if val is None:
            sub = key[len("rinex_") :] if key.startswith("rinex_") else key
            val = (station_config.get("rinex") or {}).get(sub)
        if val is None or str(val).strip() == "":
            return None
        return str(val).strip()

    return build_domes_commands(_get("rinex_marker_number"))
