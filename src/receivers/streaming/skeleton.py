"""RINEX header skeletons (.SKL) — stored on disk, refreshed from TOS on change.

BNC fills every hourly RINEX header from a per-station skeleton file
(``<rnxPath>/<SID>.SKL``, referenced by ``rnxSkel=SKL``). The skeleton is the
**stored active header**, so the write path never touches TOS. A separate periodic
refresh updates the stored skeleton from TOS only when the station's equipment
metadata changed (antenna/receiver swap, firmware update).

.. warning::
   **BNC caches the skeleton at process start — it does NOT re-read it per file
   write.** Rewriting the ``.SKL`` therefore has no effect on published headers
   until that station's BNC daemon is restarted. Measured on rek-d01 2026-08-31:
   the 06:00 refresh stripped a bad ``MARKER NUMBER`` row from all three SKLs, yet
   the 09:00 hourly file — created three hours later — still carried the old value;
   bouncing BNC produced a correct header on the very next file.

   This docstring previously claimed the opposite ("BNC reads it for every file
   write"), which is what made that refresh look sufficient. Nothing currently
   restarts BNC when the skeleton changes, so *every* equipment refresh is
   silently inert until an unrelated restart — tracked as todo #166.

Design: the skeleton is a *template* — its static lines (COMMENT, APPROX POSITION XYZ,
WAVELENGTH FACT, END OF HEADER) are preserved; only the equipment-dependent lines
(MARKER, OBSERVER/AGENCY, REC #/TYPE/VERS, ANT #/TYPE, ANTENNA DELTA) are (re)filled
from TOS. Equipment names are IGS-standardised via ``tostools.standards.igs_equipment``.

This avoids the geodetic→ECEF conversion (position lives in the stored skeleton from
the original survey) and keeps the per-file write path TOS-free.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: RINEX-2 header label column (0-indexed): data in [0:60], label in [60:80].
_LABEL_COL = 60

#: Sentinel returned by :func:`_refill` meaning "drop this line entirely".
#: Mirrors the ``STRIP_LINE`` sentinel the archive converter uses for the same
#: MARKER NUMBER policy — the line is removed, not blanked.
_STRIP_LINE = object()

# WGS84 / ITRF ellipsoid constants for geodetic→ECEF.
_WGS84_A = 6378137.0
_WGS84_E2 = 6.69437999014e-3

#: Default skeleton COMMENT line (matches the legacy IMO RT-stream skeletons).
DEFAULT_COMMENT = "File configured from IMO rt streams"

#: RINEX version the skeleton declares. 3.04 matches the authoritative sbf2rin
#: product so the stream interim and the daily SBF supersede agree on version.
#: NB: for the live stream output BNC regenerates this line from ``rnxV3`` — the
#: skeleton's version line is for self-documentation / standalone validity.
RINEX3_VERSION = "3.04"

#: RINEX placeholder for an unknown equipment serial. Matches our sbf2rin product,
#: which emits ``0000`` for serial-less equipment. TOS stores a *synthetic* serial
#: for serial-less devices (``<subtype>-<STATION>-<YYYYMMDD>``, e.g.
#: ``antenna-SEY9-20210325``) so the device stays unique — but that string is a
#: TOS-internal identity, not a real serial, and it overflows the 20-char RINEX
#: serial field. We replace any synthetic serial with this placeholder in headers.
UNKNOWN_SERIAL = "0000"

#: Matches a tostools synthetic serial ``<subtype>-<STATION>-<YYYYMMDD>``.
_SYNTHETIC_SERIAL_RE = re.compile(r"^[A-Za-z]+-[A-Za-z0-9]+-\d{8}$")

#: RINEX equipment serial field width (REC # and ANT # are both A20).
_SERIAL_W = 20


def _real_or_unknown(serial: Optional[str]) -> Optional[str]:
    """Map a synthetic TOS serial to the RINEX unknown placeholder, else pass through."""
    if serial and _SYNTHETIC_SERIAL_RE.match(serial):
        return UNKNOWN_SERIAL
    return serial


@dataclass
class SkeletonMetadata:
    """Equipment-dependent RINEX header fields sourced from TOS."""

    marker_name: Optional[str] = None
    marker_number: Optional[str] = None
    observer: Optional[str] = None
    agency: Optional[str] = None
    rec_serial: Optional[str] = None
    rec_type: Optional[str] = None  # IGS-standard receiver name
    rec_version: Optional[str] = None
    ant_serial: Optional[str] = None
    ant_type: Optional[str] = None  # IGS-standard antenna name
    ant_radome: Optional[str] = None
    antenna_h: Optional[float] = None
    antenna_e: Optional[float] = None
    antenna_n: Optional[float] = None


def _fmt_version(version: str = RINEX3_VERSION) -> str:
    # RINEX 3 "RINEX VERSION / TYPE": F9.2 version, 11X, A20 file type, A20 system.
    # Mixed-GNSS observation file → "OBSERVATION DATA" + "M". Matches sbf2rin.
    return f"{version:>9}{'':11}{'OBSERVATION DATA':<20}{'M':<20}"


def _fmt_marker(value: str) -> str:
    return f"{value:<60}"


def _domes(value: Optional[str]) -> Optional[str]:
    """Return *value* if it is an IERS DOMES, else ``None`` (= skip the line).

    Applied at the point of use rather than only where the metadata is built, so
    no caller can smuggle a non-DOMES into MARKER NUMBER. Delegates to tostools'
    single copy of the rule — deliberately not a local regex.
    """
    from tostools.rinex.domes import domes_or_skip

    return domes_or_skip(value) or None


def _fmt_observer_agency(observer: str, agency: str) -> str:
    return f"{observer:<20}{agency:<40}"


def _fit_serial(serial: str) -> str:
    """Clamp a serial to the 20-char RINEX field so it can't shift later columns.

    Synthetic serials are mapped to UNKNOWN_SERIAL upstream; this is a backstop so
    any other over-long serial truncates (with a warning) instead of silently
    misaligning REC/ANT TYPE and radome.
    """
    if len(serial) > _SERIAL_W:
        logger.warning(
            "Serial %r exceeds the %d-char RINEX field; truncating", serial, _SERIAL_W
        )
        return serial[:_SERIAL_W]
    return serial


def _fmt_rec(serial: str, rtype: str, version: str) -> str:
    return f"{_fit_serial(serial):<20}{rtype:<20}{version:<20}"


def _fmt_ant(serial: str, model: str, radome: str) -> str:
    # antenna number cols 1-20; antenna model cols 21-35 + radome cols 37-40
    return f"{_fit_serial(serial):<20}{model:<15} {radome:<4}".ljust(60)


def _fmt_delta(h: float, e: float, n: float) -> str:
    return f"{h:14.4f}{e:14.4f}{n:14.4f}".ljust(60)


def fill_skeleton(template: str, meta: SkeletonMetadata) -> str:
    """Return ``template`` with equipment lines (re)filled from ``meta``.

    Lines whose TOS value is ``None`` keep the template's existing data. Static
    lines (COMMENT, APPROX POSITION XYZ, WAVELENGTH FACT, END OF HEADER, …) are
    always preserved. Label columns (61-80) are never altered.

    **MARKER NUMBER is the one exception to "None keeps the existing data".** It
    carries the IERS DOMES and nothing else, so a stored non-DOMES value must be
    actively *removed* rather than preserved — otherwise a skeleton written with
    the 4-char station id keeps re-emitting it forever, which is exactly how
    GONH/HRIC/SEY9 published ``MARKER NUMBER = <station id>`` every hour.
    """
    out: List[str] = []
    for raw in template.splitlines():
        data = raw[:_LABEL_COL]
        label = raw[_LABEL_COL:].rstrip()
        new_data = _refill(label.strip(), data, meta)
        if new_data is _STRIP_LINE:
            continue
        out.append(f"{new_data:<{_LABEL_COL}}{label}".rstrip())
    text = "\n".join(out)
    return text + "\n" if template.endswith("\n") else text


def _refill(label: str, data: str, meta: SkeletonMetadata) -> str:
    """Rebuild the data portion for an equipment line, else keep ``data``."""
    if label == "MARKER NAME" and meta.marker_name:
        return _fmt_marker(meta.marker_name)
    if label == "MARKER NUMBER":
        # No truthiness fallthrough here: a non-DOMES means STRIP, never "keep
        # whatever the template had". See fill_skeleton's docstring.
        domes = _domes(meta.marker_number)
        return _fmt_marker(domes) if domes else _STRIP_LINE
    if label == "OBSERVER / AGENCY" and (meta.observer or meta.agency):
        return _fmt_observer_agency(meta.observer or "", meta.agency or "")
    if label == "REC # / TYPE / VERS" and (
        meta.rec_serial or meta.rec_type or meta.rec_version
    ):
        return _fmt_rec(
            meta.rec_serial or "", meta.rec_type or "", meta.rec_version or ""
        )
    if label == "ANT # / TYPE" and (meta.ant_serial or meta.ant_type):
        return _fmt_ant(
            meta.ant_serial or "", meta.ant_type or "", meta.ant_radome or "NONE"
        )
    if label == "ANTENNA: DELTA H/E/N" and meta.antenna_h is not None:
        return _fmt_delta(meta.antenna_h, meta.antenna_e or 0.0, meta.antenna_n or 0.0)
    return data


def refresh_skeleton(existing: str, meta: SkeletonMetadata) -> Tuple[str, bool]:
    """Refill ``existing`` from ``meta``; return (new_text, changed)."""
    updated = fill_skeleton(existing, meta)
    return updated, updated != existing


def geodetic_to_ecef(
    lat_deg: float, lon_deg: float, height_m: float
) -> Tuple[float, float, float]:
    """Convert geodetic lat/lon/height (WGS84, degrees/metres) to ECEF X/Y/Z."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    x = (n + height_m) * math.cos(lat) * math.cos(lon)
    y = (n + height_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _WGS84_E2) + height_m) * sin_lat
    return x, y, z


def build_skeleton(
    meta: SkeletonMetadata,
    *,
    latitude: float,
    longitude: float,
    height: float,
    comment: str = DEFAULT_COMMENT,
    version: str = RINEX3_VERSION,
) -> str:
    """Build a fresh ``.SKL`` for a new stream station from metadata + position.

    Produces a RINEX 3.04 header skeleton matching our sbf2rin product: it carries
    the ``RINEX VERSION / TYPE`` line and a (blank) ``MARKER TYPE`` line, and omits
    the RINEX-2-only ``WAVELENGTH FACT L1/2``. APPROX POSITION XYZ is computed from
    the geodetic coordinates (the one piece a refresh-only flow can't supply).

    The skeleton is a *partial* header by design: the obs-data-dependent lines
    (``PGM / RUN BY``, ``SYS / # / OBS TYPES``, ``TIME OF FIRST OBS``,
    ``SYS / PHASE SHIFT``) are left to BNC, which regenerates them per file.
    ``MARKER TYPE`` is left blank to mirror our sbf2rin product. Once written,
    updates go through :func:`refresh_skeleton` (equipment) / :func:`upgrade_skeleton`
    (structure), neither of which touches the position line.
    """
    x, y, z = geodetic_to_ecef(latitude, longitude, height)
    rows: List[Tuple[str, str]] = [
        (_fmt_version(version), "RINEX VERSION / TYPE"),
        (comment, "COMMENT"),
        (_fmt_marker(meta.marker_name or ""), "MARKER NAME"),
    ]
    # MARKER NUMBER is emitted only for a real IERS DOMES; absent one the line is
    # omitted entirely (matching our sbf2rin product), never filled with the
    # 4-char id — MARKER NAME already carries that.
    _mn = _domes(meta.marker_number)
    if _mn:
        rows.append((_fmt_marker(_mn), "MARKER NUMBER"))
    rows += [
        ("", "MARKER TYPE"),
        (
            _fmt_observer_agency(meta.observer or "", meta.agency or ""),
            "OBSERVER / AGENCY",
        ),
        (
            _fmt_rec(
                meta.rec_serial or "", meta.rec_type or "", meta.rec_version or ""
            ),
            "REC # / TYPE / VERS",
        ),
        (
            _fmt_ant(
                meta.ant_serial or "", meta.ant_type or "", meta.ant_radome or "NONE"
            ),
            "ANT # / TYPE",
        ),
        (f"{x:14.4f}{y:14.4f}{z:14.4f}".ljust(_LABEL_COL), "APPROX POSITION XYZ"),
        (
            _fmt_delta(
                meta.antenna_h or 0.0, meta.antenna_e or 0.0, meta.antenna_n or 0.0
            ),
            "ANTENNA: DELTA H/E/N",
        ),
        ("", "END OF HEADER"),
    ]
    return (
        "\n".join(f"{data:<{_LABEL_COL}}{label}".rstrip() for data, label in rows)
        + "\n"
    )


def upgrade_skeleton(existing: str) -> Tuple[str, bool]:
    """Normalise a legacy RINEX-2 skeleton to the 3.04 schema; return (text, changed).

    Structural-only migration that :func:`refresh_skeleton` (equipment-only) cannot
    do: prepend ``RINEX VERSION / TYPE`` if absent, insert a blank ``MARKER TYPE``
    after ``MARKER NUMBER`` if absent, and drop the RINEX-2-only
    ``WAVELENGTH FACT L1/2``. Every other line — crucially ``APPROX POSITION XYZ``,
    which may be a surveyed ECEF migrated from a legacy skeleton — is preserved
    verbatim. Idempotent: an already-3.04 skeleton returns unchanged.
    """
    rows: List[Tuple[str, str]] = []
    for raw in existing.splitlines():
        rows.append((raw[:_LABEL_COL], raw[_LABEL_COL:].strip()))

    labels = {label for _, label in rows}
    changed = False

    # Drop the RINEX-2-only wavelength-factor line.
    if "WAVELENGTH FACT L1/2" in labels:
        rows = [(d, lbl) for d, lbl in rows if lbl != "WAVELENGTH FACT L1/2"]
        changed = True

    # Insert a blank MARKER TYPE after MARKER NUMBER (fallback MARKER NAME).
    if "MARKER TYPE" not in labels:
        anchor = next(
            (i for i, (_, lbl) in enumerate(rows) if lbl == "MARKER NUMBER"),
            next((i for i, (_, lbl) in enumerate(rows) if lbl == "MARKER NAME"), -1),
        )
        rows.insert(anchor + 1, ("", "MARKER TYPE"))
        changed = True

    # Prepend the version line if missing.
    if "RINEX VERSION / TYPE" not in labels:
        rows.insert(0, (_fmt_version(), "RINEX VERSION / TYPE"))
        changed = True

    text = "\n".join(f"{d:<{_LABEL_COL}}{lbl}".rstrip() for d, lbl in rows)
    if existing.endswith("\n"):
        text += "\n"
    return text, changed


def metadata_from_tos(
    station: Dict[str, Any],
    *,
    station_id: str,
    station_config: Optional[Dict[str, Any]] = None,
) -> SkeletonMetadata:
    """Map a TOS ``get_complete_station_metadata`` dict to skeleton fields.

    Reuses the battle-tested ``receivers.cfg.tos_adapter`` accessors and
    IGS-name standardisation from tostools.

    ``OBSERVER / AGENCY`` is not a TOS attribute — it is operational metadata
    that lives in ``stations.cfg`` (``rinex_observer`` / ``rinex_agency``),
    exactly like our sbf2rin product header (e.g. ``HMF/BGO  /  JH/IMO``). When
    a ``station_config`` is supplied these fill the otherwise-blank line; without
    it the line is left for the template to preserve.
    """
    from tostools.rinex.domes import domes_or_skip
    from tostools.standards.igs_equipment import (
        to_igs_antenna,
        to_igs_radome,
        to_igs_receiver,
    )

    from ..cfg import tos_adapter as ta

    def _f(value: Optional[str]) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    observer = agency = marker_number = None
    if station_config:
        from .config import _lookup

        observer = _lookup(station_config, "rinex_observer")
        agency = _lookup(station_config, "rinex_agency")
        # MARKER NUMBER = IERS DOMES only. cfg is the TOS-canonical carrier
        # (`cfg sync-from-tos --only rinex_marker_number`); anything that is not
        # a DOMES — including the 4-char station id — collapses to "" and the
        # line is skipped. Enforcement stays in tostools' single copy of the
        # rule rather than a local regex.
        marker_number = (
            domes_or_skip(_lookup(station_config, "rinex_marker_number")) or None
        )

    # Fall back to the raw TOS value when the IGS table has no mapping — better a
    # valid raw name than a blank header. (The tostools IGS table currently misses
    # e.g. TRM115000.10 and maps mosaic-X5 to "SEPT MOSAICX5" vs the rcvr_ant.tab
    # spelling "SEPT MOSAIC-X5" — tracked as a tostools fix.)
    rec_model = ta.current_receiver_model(station)
    ant_model = ta.current_antenna_model(station)
    return SkeletonMetadata(
        marker_name=station_id,
        marker_number=marker_number,
        observer=observer,
        agency=agency,
        rec_serial=_real_or_unknown(ta.current_receiver_serial(station)),
        rec_type=to_igs_receiver(rec_model) or rec_model,
        rec_version=ta.current_receiver_firmware(station),
        ant_serial=_real_or_unknown(ta.current_antenna_serial(station)),
        ant_type=to_igs_antenna(ant_model) or ant_model,
        ant_radome=to_igs_radome(ta.current_radome_model(station)) or "NONE",
        antenna_h=_f(ta.current_antenna_height(station)),
        # The east/north eccentricity, which TOS splits across the monument and
        # antenna entities exactly as it splits the height. SkeletonMetadata has
        # carried these two fields since it was written and nothing ever filled
        # them, so _fmt_delta fell back to `or 0.0` and every stream skeleton
        # published a zeroed eccentricity. The composite helpers below already
        # existed and already summed both entities correctly -- only the call
        # was missing. Live effect today is nil (GONH/HRIC/SEY9 are all zero on
        # both entities, so no skeleton changes and no BNC bounce), but a stream
        # station with a real offset would have silently published 0.0000.
        antenna_e=_f(ta.current_antenna_east(station)),
        antenna_n=_f(ta.current_antenna_north(station)),
    )
