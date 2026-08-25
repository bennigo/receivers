"""Plan corrective moves for misfiled/misnamed raw archive files.

For each candidate the TRUE identity is decoded from the file content
(``teqc +meta`` — the receiver's embedded records): observation date, and
where available the antenna position and embedded station code. The plan
fixes everything the filename/path claims wrongly:

* **wrong date** — decoded first epoch ≠ filename date (e.g. the RHOF
  ``2000/2001`` batches holding 2010/2011 data);
* **wrong station** (``verify_station=True``) — the antenna position matches
  a DIFFERENT station's surveyed coordinates; the file moves to that
  station's tree and is renamed accordingly. Position decides (bgo's rule:
  coordinates confirm identity — embedded codes and filenames are claims);
  a position matching NO station within the gate is reported, never moved.
* **wrong extension** — content format's canonical extension differs (e.g.
  Septentrio SBF bytes in a ``.atc`` name → ``.sbf`` so extension-keyed
  tooling picks the right chain).

Planning is read-only (works off the read-only mount). Execution goes
through :func:`~receivers.archive.relocate.relocate_archive_files` (rawdata
gateway, dry-run default, never overwrites).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .raw_format import (
    CANONICAL_EXT,
    MONTH_DIRS,
    TRIMBLE,
    UNKNOWN,
    build_raw_name,
    classify_raw,
    parse_raw_name,
    teqc_meta,
)

logger = logging.getLogger("receivers.archive.sort")

# Files smaller than this are stubs (0-byte / truncated header fragments seen
# in the .atc sweeps) — flagged, never relocated.
MIN_RAW_BYTES = 4096

# The RINEX equivalent. A raw-sized floor is wrong here: a compressed hourly
# Hatanaka observation file is legitimately a few kB, so MIN_RAW_BYTES would
# skip real files as stubs. This only has to exclude 0-byte and
# truncated-header fragments — anything with a readable header clears it.
MIN_RINEX_BYTES = 256

# Position-identity gate: SAME metric as the converter's RINEX-header check
# (one knob: receivers.cfg [rinex] position_gate_m; default 30 m).
STATION_GATE_M = 10.0

Xyz = tuple


def resolve_position_gate_m(override=None) -> float:
    """explicit override > receivers.cfg [rinex] position_gate_m (default 10)."""
    if override is not None:
        return float(override)
    try:
        from ..config.receivers_config import get_receivers_config

        return get_receivers_config().get_position_gate_m()
    except Exception:  # noqa: BLE001 - config optional
        return STATION_GATE_M


@dataclass(frozen=True)
class MovePlan:
    src_rel: str
    dst_rel: str
    fmt: str
    # The date this file is filed under AFTER the move. Normally teqc's
    # decoded start; when the format has no date decoder (Septentrio SBF —
    # teqc reports only its 1980-01-01 placeholder) it is the CLAIMED date,
    # because such a move is station/extension-only and must not rewrite the
    # date. Never None: three report writers format it. 'wrong-date' in
    # ``reasons`` is what tells you the date actually changed.
    decoded_start: object  # datetime
    claimed: object  # datetime
    reasons: tuple = ()  # subset of ('wrong-date','wrong-station','wrong-ext')
    true_station: str = ""
    station_dist_m: Optional[float] = None
    # Where the identity evidence came from: "raw teqc +meta" or
    # "RINEX header <rel>" (the --check-station fallback for undecodable raw,
    # e.g. Septentrio SBF where teqc +meta reports the (90,0) placeholder).
    evidence: str = ""


@dataclass(frozen=True)
class SkipInfo:
    rel: str
    reason: str
    detail: str = ""


def fleet_coordinates() -> dict:
    """station -> (lat, lon) for the whole fleet, from stations.cfg."""
    import configparser

    import gps_parser

    path = gps_parser.ConfigParser().get_stations_config_path()
    cp = configparser.ConfigParser()
    cp.read(path)
    fleet: dict = {}
    for sec in cp.sections():
        if len(sec) != 4 or not sec.isupper():
            continue
        lat, lon = cp[sec].get("latitude"), cp[sec].get("longitude")
        if lat and lon:
            try:
                fleet[sec] = (float(lat), float(lon))
            except ValueError:
                continue
    return fleet


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_station(lat: float, lon: float, fleet: dict) -> tuple[Optional[str], float]:
    """(station, distance_m) of the fleet station closest to (lat, lon)."""
    best, best_d = None, float("inf")
    for sta, (slat, slon) in fleet.items():
        d = _haversine_m(lat, lon, slat, slon)
        if d < best_d:
            best, best_d = sta, d
    return best, best_d


def _ecef_to_latlon(xyz) -> Optional[tuple[float, float]]:
    """ECEF metres → (lat, lon) degrees via pyproj; ``None`` if unavailable.

    Fail-open by design — a probe must never break the surrounding sweep.
    Shared with :mod:`receivers.archive.file_identity`.
    """
    try:
        import pyproj

        tr = pyproj.Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
        lon, lat, _h = tr.transform(xyz[0], xyz[1], xyz[2])
        return (lat, lon)
    except Exception as exc:  # noqa: BLE001 - probe is fail-open
        logger.debug("ecef->latlon failed: %s", exc)
        return None


def _position_verdict(
    lat: Optional[float],
    lon: Optional[float],
    path_station: str,
    fleet: dict,
    gate_m: float,
) -> tuple[str, Optional[str], Optional[float]]:
    """Classify a decoded position against the filed station.

    Returns ``(verdict, nearest_station, dist_m)`` where verdict is one of:

    * ``no-position`` — lat/lon unavailable (teqc gave nothing usable);
    * ``confirmed``  — nearest station IS the filed one, within the gate;
    * ``noisy-self`` — nearest is the filed one but outside the gate
      (degraded single-point solution, not an identity problem);
    * ``unknown``    — no fleet station within the gate (genuine remote
      position, or teqc's (90,0) placeholder for undecodable raw);
    * ``wrong``      — a DIFFERENT station's mark is within the gate (stray).
    """
    if lat is None or lon is None:
        return ("no-position", None, None)
    near, dist = nearest_station(lat, lon, fleet)
    if near == path_station.upper():
        return (("confirmed" if dist <= gate_m else "noisy-self"), near, dist)
    if near is None or dist > gate_m:
        return ("unknown", near, dist)
    return ("wrong", near, dist)


def _rinex_name_matches_date(name: str, station: str, claimed) -> bool:
    """Whether a RINEX filename names ``station`` on ``claimed``'s date.

    Matches both name shapes: RINEX 2 short Hatanaka
    (``<STA>DDDn.YYD.Z`` — station + day-of-year + session digit + year + D/O)
    and RINEX 3 long IGS (``<STA>…_YYYYDDD0000_…``). A bare ``startswith``
    + day-of-year substring is too weak (day 123 would match ``0123``
    inside a DOMES/marker number), so each shape is pinned to its field.
    """
    if not name.startswith(station):
        return False
    doy = claimed.timetuple().tm_yday
    # RINEX 2 short: STA + 3-digit doy + session digit + '.' + 2-digit year
    # + [DO] + optional .Z/.gz — e.g. VMOS0300.24D.Z
    if (
        len(name) >= 11
        and name[4:7] == f"{doy:03d}"
        and name[7].isdigit()
        and name[8] == "."
        and name[9:11] == f"{claimed:%y}"
    ):
        return True
    # RINEX 3 long: STA + 4-char country/marker + '_R_' + YYYYDDD0000…
    return f"_{claimed:%Y}{doy:03d}" in name


@dataclass(frozen=True)
class ParsedRinexName:
    """Station + date a RINEX FILENAME claims (the raw analogue is
    ``ParsedRawName``)."""

    station: str
    claimed: datetime


def parse_rinex_name(name: str) -> Optional[ParsedRinexName]:
    """Station + claimed date from a RINEX filename, or None.

    ``parse_raw_name`` returns None for a ``.d.Z`` — the raw name grammar is
    ``STA + YYYYMMDDHHMM + letter``, and a RINEX carries neither a full date
    nor a session letter in that position. Hence a separate parser rather than
    a widened one.

    Handles both archive shapes:

    - RINEX 2 short Hatanaka — ``VMOS0300.24D.Z`` (station, day-of-year,
      session digit, 2-digit year, D/O);
    - RINEX 3 long IGS — ``VMOS00ISL_R_20240300000_01D_30S_MO.crx.gz``.
    """
    if len(name) < 11 or not name[:4].isalnum():
        return None
    station = name[:4].upper()

    # RINEX 3 long: …_R_YYYYDDDHHMM_…
    m = re.search(r"_(\d{4})(\d{3})\d{4}_", name)
    if m:
        year, doy = int(m.group(1)), int(m.group(2))
        try:
            return ParsedRinexName(station, datetime(year, 1, 1) + timedelta(doy - 1))
        except (ValueError, OverflowError):
            return None

    # RINEX 2 short: STA + DDD + session digit + '.' + YY + [DOdo]
    m = re.match(r"^[A-Z0-9]{4}(\d{3})(\d)\.(\d{2})[A-Za-z]", name, re.IGNORECASE)
    if not m:
        return None
    doy, yy = int(m.group(1)), int(m.group(3))
    if doy < 1 or doy > 366:
        return None
    # The archive starts in the late 1990s and RINEX 2 short names are gone
    # well before 2080, so the standard pivot is unambiguous here.
    year = 1900 + yy if yy >= 80 else 2000 + yy
    try:
        return ParsedRinexName(station, datetime(year, 1, 1) + timedelta(doy - 1))
    except (ValueError, OverflowError):
        return None


def _read_rinex_approx_position(path: Path):
    """(lat, lon) from a RINEX header's first APPROX POSITION XYZ, or None.

    Deferred imports avoid the module-level cycle (``file_identity`` imports
    the fleet-geometry helpers from this module).
    """
    from tostools.rinex.reader import read_rinex_file

    from .file_identity import parse_first_approx_xyz

    try:
        content = read_rinex_file(str(path))
    except Exception as exc:  # noqa: BLE001 - probe is fail-open
        logger.debug("rinex fallback: cannot read %s: %s", path, exc)
        return None
    if not content:
        return None
    text = content.decode("utf-8", errors="ignore")
    return _ecef_to_latlon(parse_first_approx_xyz(text))


def _sibling_rinex_position(
    root: Path, rel: str, parsed
) -> Optional[tuple[float, float, str]]:
    """The sibling RINEX's position for the raw file at ``rel``.

    Looks in the sibling ``…/<session>/rinex/`` directory for a file naming
    the same station + date as ``parsed.claimed``, and returns
    ``(lat, lon, rinex_rel)`` from its first ``APPROX POSITION XYZ``.
    ``None`` when there is no sibling tree, no date-matching file, or the
    header is unreadable. This is the ``--check-station`` fallback for raw
    files whose position teqc cannot decode (Septentrio SBF reports the
    (90,0) placeholder), because the archive RINEX header carries the
    converter-embedded position.
    """
    parts = rel.split("/")
    if len(parts) != 6 or parts[4] != "raw":
        return None
    station = parts[2]
    rinex_dir = Path(root) / "/".join(parts[:4] + ["rinex"])
    if not rinex_dir.is_dir():
        return None
    for f in sorted(rinex_dir.iterdir()):
        if not f.is_file():
            continue
        if not _rinex_name_matches_date(f.name, station, parsed.claimed):
            continue
        latlon = _read_rinex_approx_position(f)
        if latlon is not None:
            return (latlon[0], latlon[1], str(f.relative_to(root)))
    return None


def _expected_rel(
    rel: str, decoded_start, new_name: str, *, station: Optional[str] = None
) -> Optional[str]:
    """Correct archive path: fix year/month dirs + filename (+ station dir),
    keep session/category segments as they are."""
    parts = rel.split("/")
    if len(parts) != 6:
        return None
    _y, _mon, path_sta, session, category, _name = parts
    return "/".join(
        [
            f"{decoded_start:%Y}",
            MONTH_DIRS[decoded_start.month],
            (station or path_sta).upper(),
            session,
            category,
            new_name,
        ]
    )


def plan_relocations(
    root: Path,
    rel_files: list[str],
    *,
    min_bytes: int = MIN_RAW_BYTES,
    verify_station: bool = False,
    station_gate_m: float = STATION_GATE_M,
    progress=None,
) -> tuple[list[MovePlan], list[SkipInfo]]:
    """Classify + decode each file under ``root`` and propose corrective moves.

    Returns ``(plans, skips)``: plans only for files whose decoded identity
    (date / station / content-format) disagrees with the filename/path claim;
    everything else lands in skips with a reason. With ``verify_station`` a
    decoded position matching a different station RELOCATES the file there;
    a position matching no station within the gate is reported
    (``unknown-station``) and never moved.
    """
    root = Path(root)
    fleet = fleet_coordinates() if verify_station else {}
    plans: list[MovePlan] = []
    skips: list[SkipInfo] = []
    total = len(rel_files)
    for idx, rel in enumerate(rel_files, 1):
        if progress is not None:
            progress(idx, total, len(plans))
        path = root / rel
        name = path.name
        parsed = parse_raw_name(name)
        if parsed is None:
            skips.append(SkipInfo(rel, "unparseable-name"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skips.append(SkipInfo(rel, "unreadable", str(exc)))
            continue
        if size < min_bytes:
            skips.append(SkipInfo(rel, "stub", f"{size} bytes < {min_bytes}"))
            continue
        fmt = classify_raw(path)
        if fmt == UNKNOWN:
            skips.append(SkipInfo(rel, "unknown-format"))
            continue
        parts = rel.split("/")
        dir_ym = (parts[0], parts[1]) if len(parts) == 6 else None
        claimed_ym = (f"{parsed.claimed:%Y}", MONTH_DIRS[parsed.claimed.month])

        meta = teqc_meta(path, fmt) if fmt != TRIMBLE else None
        no_date = meta is None or meta.start is None
        path_station = rel.split("/")[2] if len(rel.split("/")) == 6 else parsed.station

        if no_date:
            # The NAME-vs-PATH consistency check needs no decoder: a file
            # claiming a different year/month than its directory is wrong
            # SOMEWHERE (which side lies needs a decode/eyes). Caught the
            # 324 MB 'RHOF202101031833a.T02' living in 2017/dec.
            if dir_ym is not None and dir_ym != claimed_ym:
                skips.append(
                    SkipInfo(
                        rel,
                        "path-name-mismatch",
                        f"filename claims {parsed.claimed:%Y-%m-%d} but sits in "
                        f"{dir_ym[0]}/{dir_ym[1]} — no {fmt} date decoder; "
                        "needs eyes (convert or inspect to learn the truth)",
                    )
                )
                continue
            # An undecodable DATE must not disable the STATION check. On
            # Septentrio SBF teqc decodes neither (it reports 1980-01-01 and
            # (90,0) for every file), but the sibling RINEX header carries a
            # real position — which is the whole point of --check-station for
            # this fleet, and the class the VMOS/GRVV strays belonged to.
            if not (verify_station and _sibling_rinex_position(root, rel, parsed)):
                reason = "no-date-decoder" if fmt == TRIMBLE else "decode-failed"
                skips.append(SkipInfo(rel, reason, fmt))
                continue

        start = None if no_date else meta.start
        reasons: list[str] = []

        # Station identity: the decoded position decides — with a RINEX-header
        # fallback when the raw decode cannot (Septentrio SBF: teqc +meta
        # reports the (90,0) placeholder for every .sbf.gz, so every SBF
        # station otherwise reads 'unknown-station' — the VMOS/GRVV class).
        true_station = ""
        dist: Optional[float] = None
        evidence = "raw teqc +meta"
        rinex_evidence_rel: Optional[str] = None
        if verify_station:
            raw_lat = meta.lat if meta is not None else None
            raw_lon = meta.lon if meta is not None else None
            verdict, near, dist = _position_verdict(
                raw_lat, raw_lon, path_station, fleet, station_gate_m
            )
            if verdict in ("no-position", "unknown", "noisy-self"):
                fb = _sibling_rinex_position(root, rel, parsed)
                if fb is not None:
                    rx_lat, rx_lon, rx_rel = fb
                    v2, near2, dist2 = _position_verdict(
                        rx_lat, rx_lon, path_station, fleet, station_gate_m
                    )
                    if v2 in ("confirmed", "wrong"):
                        # The RINEX header carries a decision the raw could
                        # not give — adopt it as the identity evidence.
                        verdict, near, dist = v2, near2, dist2
                        evidence = f"RINEX header {rx_rel}"
                        rinex_evidence_rel = rx_rel
            if verdict == "noisy-self":
                # Nearest station IS the claimed one, just outside the tight
                # gate — a degraded single-point solution, not a mystery.
                # Informational only; never blocks the date/ext checks.
                skips.append(
                    SkipInfo(
                        rel,
                        "position-noisy",
                        f"nearest is {near} (as filed) at {dist:.0f} m — "
                        f"outside the {station_gate_m:.0f} m gate; solution "
                        "quality, not identity",
                    )
                )
                continue
            if verdict == "unknown":
                where = (
                    f"({raw_lat:.5f},{raw_lon:.5f})"
                    if raw_lat is not None and raw_lon is not None
                    else "the decoded position"
                )
                skips.append(
                    SkipInfo(
                        rel,
                        "unknown-station",
                        f"position {where} matches no "
                        f"station within {station_gate_m:.0f} m "
                        f"(nearest {near} at {dist / 1000:.1f} km)",
                    )
                )
                continue
            if verdict == "wrong":
                reasons.append("wrong-station")
                true_station = near

        if start is not None and start.date() != parsed.claimed.date():
            reasons.append("wrong-date")

        canon_ext = CANONICAL_EXT.get(fmt)
        new_ext = None
        if canon_ext and not parsed.ext.lower().startswith(canon_ext):
            reasons.append("wrong-ext")
            new_ext = canon_ext + (".gz" if parsed.ext.lower().endswith(".gz") else "")

        if not reasons:
            detail = fmt
            if verify_station and rinex_evidence_rel is not None:
                detail += " (station confirmed via RINEX header)"
            skips.append(SkipInfo(rel, "verified-correct", detail))
            continue

        # With no decodable date the claimed one stands — the move is then a
        # station (and/or extension) correction only, and must NOT rewrite the
        # date portion of the name or the YYYY/mon directories. This is the
        # Septentrio path: teqc decodes no date for SBF, so every SBF
        # relocation is date-preserving by construction.
        move_date = start if start is not None else parsed.claimed
        new_name = build_raw_name(
            parsed, move_date, station=true_station or None, ext=new_ext
        )
        dst_rel = _expected_rel(rel, move_date, new_name, station=true_station or None)
        if dst_rel is None:
            skips.append(SkipInfo(rel, "unexpected-layout"))
            continue
        plans.append(
            MovePlan(
                src_rel=rel,
                dst_rel=dst_rel,
                fmt=fmt,
                decoded_start=move_date,
                claimed=parsed.claimed,
                reasons=tuple(reasons),
                true_station=true_station or path_station.upper(),
                station_dist_m=dist,
                evidence=evidence if "wrong-station" in reasons else "",
            )
        )

        # The stray RINEX that provided the identity evidence is itself a
        # stray by its own content — plan its co-move (station prefix swap)
        # when the ONLY disagreement is the station (a date mismatch would
        # also need a date rename in the RINEX name, which needs eyes).
        if (
            rinex_evidence_rel is not None
            and true_station
            and reasons == ["wrong-station"]
        ):
            rx_parts = rinex_evidence_rel.split("/")
            rx_name = rx_parts[-1]
            rx_dst = "/".join(
                [
                    rx_parts[0],
                    rx_parts[1],
                    true_station.upper(),
                    rx_parts[3],
                    rx_parts[4],
                    true_station.upper() + rx_name[len(path_station) :],
                ]
            )
            plans.append(
                MovePlan(
                    src_rel=rinex_evidence_rel,
                    dst_rel=rx_dst,
                    fmt="rinex",
                    decoded_start=move_date,
                    claimed=parsed.claimed,
                    reasons=("wrong-station",),
                    true_station=true_station.upper(),
                    station_dist_m=dist,
                    evidence=f"RINEX header {rinex_evidence_rel}",
                )
            )

        logger.info("remediation: %s [%s] -> %s", rel, ",".join(reasons), dst_rel)
    return plans, skips


def split_raw_and_rinex(rel_files: list[str]) -> tuple[list[str], list[str]]:
    """Route explicit paths to the raw pass or the RINEX pass.

    The two passes need different parsers, size floors and decoders, so a
    mixed ``--file``/``--list`` has to be split before planning. Routing is by
    the ``rinex`` path segment ANYWHERE in the path rather than only at the
    canonical 6-segment position, so a non-canonical RINEX path is reported as
    ``unexpected-layout`` by the pass that understands it rather than
    ``unparseable-name`` by the one that does not.

    Returns ``(raw_rels, rinex_rels)``.
    """
    raw: list[str] = []
    rinex: list[str] = []
    for rel in rel_files:
        parts = rel.split("/")
        (rinex if "rinex" in parts[:-1] else raw).append(rel)
    return raw, rinex


def plan_rinex_relocations(
    root: Path,
    rel_files: list[str],
    *,
    min_bytes: int = MIN_RINEX_BYTES,
    verify_station: bool = False,
    station_gate_m: float = STATION_GATE_M,
    progress=None,
) -> tuple[list[MovePlan], list[SkipInfo]]:
    """Propose corrective moves for misfiled RINEX, from the header alone.

    The gap this closes: ``plan_relocations`` reaches a RINEX only as the
    *sibling* of a stray raw, so a stray RINEX whose raw is correctly filed is
    never enumerated at all — the VMOS/GRVV shape, where GRVV's raw already sat
    in GRVV's tree. ``archive-audit --identity`` and the integrity checker
    already FIND these and print "relocate with: receivers archive-sort …";
    until now that instruction could not be carried out.

    Cheaper than the raw pass by construction: position comes from the file's
    own ``APPROX POSITION XYZ`` — a header read, no ``teqc``, no
    decompress-to-decode.

    Deliberately narrower than the raw planner in what it will MOVE. Only a
    station stray is planned, because a station fix is a pure prefix swap
    (``TRUE + name[4:]``) that is correct for both the RINEX 2 short and
    RINEX 3 long shapes. A date disagreement would additionally require
    rewriting a day-of-year field, so it is REPORTED and left for eyes — the
    same rule the raw planner's co-move path already applies.

    ``verify_station`` gates the position work exactly as it does for raw:
    without it this pass reports name-vs-path consistency only and reads no
    headers. Identity-by-coordinates is opt-in on both passes, not a policy
    the RINEX branch quietly adopts on its own.
    """
    root = Path(root)
    fleet = fleet_coordinates() if verify_station else {}
    plans: list[MovePlan] = []
    skips: list[SkipInfo] = []
    total = len(rel_files)
    for idx, rel in enumerate(rel_files, 1):
        if progress is not None:
            progress(idx, total, len(plans))
        path = root / rel
        parsed = parse_rinex_name(path.name)
        if parsed is None:
            skips.append(SkipInfo(rel, "unparseable-name"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skips.append(SkipInfo(rel, "unreadable", str(exc)))
            continue
        if size < min_bytes:
            skips.append(SkipInfo(rel, "stub", f"{size} bytes < {min_bytes}"))
            continue

        parts = rel.split("/")
        if len(parts) != 6:
            skips.append(SkipInfo(rel, "unexpected-layout"))
            continue
        path_station = parts[2].upper()

        # Name-vs-path consistency needs no header read: a file whose name
        # claims a different year/month than its directory is wrong somewhere,
        # and which side lies needs eyes.
        dir_ym = (parts[0], parts[1])
        claimed_ym = (f"{parsed.claimed:%Y}", MONTH_DIRS[parsed.claimed.month])
        if dir_ym != claimed_ym:
            skips.append(
                SkipInfo(
                    rel,
                    "path-name-mismatch",
                    f"filename claims {parsed.claimed:%Y-%m-%d} but sits in "
                    f"{dir_ym[0]}/{dir_ym[1]} — needs eyes",
                )
            )
            continue

        if not verify_station:
            skips.append(SkipInfo(rel, "path-name-consistent", "rinex"))
            continue

        latlon = _read_rinex_approx_position(path)
        if latlon is None:
            skips.append(SkipInfo(rel, "no-header-position", "no APPROX POSITION XYZ"))
            continue

        verdict, near, dist = _position_verdict(
            latlon[0], latlon[1], path_station, fleet, station_gate_m
        )
        if verdict == "confirmed":
            skips.append(SkipInfo(rel, "verified-correct", "rinex"))
            continue
        if verdict == "noisy-self":
            skips.append(
                SkipInfo(
                    rel,
                    "position-noisy",
                    f"nearest is {near} (as filed) at {dist:.0f} m — outside the "
                    f"{station_gate_m:.0f} m gate; solution quality, not identity",
                )
            )
            continue
        if verdict in ("unknown", "no-position"):
            skips.append(
                SkipInfo(
                    rel,
                    "unknown-station",
                    f"position ({latlon[0]:.5f},{latlon[1]:.5f}) matches no station "
                    f"within {station_gate_m:.0f} m"
                    + (f" (nearest {near} at {dist / 1000:.1f} km)" if near else ""),
                )
            )
            continue

        # verdict == "wrong": the header says this file belongs elsewhere.
        true_station = near.upper()
        dst_rel = "/".join(
            parts[:2] + [true_station, parts[3], parts[4], true_station + path.name[4:]]
        )
        plans.append(
            MovePlan(
                src_rel=rel,
                dst_rel=dst_rel,
                fmt="rinex",
                decoded_start=parsed.claimed,
                claimed=parsed.claimed,
                reasons=("wrong-station",),
                true_station=true_station,
                station_dist_m=dist,
                evidence=f"RINEX header {rel}",
            )
        )
        logger.info("remediation: %s [wrong-station] -> %s", rel, dst_rel)
    return plans, skips


def scan_station_rinex(
    root: Path,
    station: str,
    session: str = "15s_24hr",
    *,
    years: Optional[list] = None,
) -> list[str]:
    """Enumerate a station/session's RINEX files as archive-relative paths.

    The ``rinex/`` sibling of :func:`scan_station_raw`.

    Only files whose NAME starts with the directory's station id are returned,
    which is the stray shape that actually occurs: the file is named for the
    tree it sits in and only its *header* betrays it (VMOS/GRVV). The inverse —
    a file named ``GRVV…`` sitting in ``VMOS/…/rinex/`` — is excluded, and that
    exclusion was measured rather than assumed: of **123,443** canonical RINEX
    files in rek-d01's collection window (2026-08-25), **zero** were
    foreign-named and zero sat outside the canonical 6-segment layout. The same
    filter is what ``file_identity._iter_rinex_files`` applies, so the audit and
    the sorter agree on what a station's files are.

    Note the coupling: :func:`plan_rinex_relocations` builds its destination
    as ``TRUE + name[4:]``, which assumes those first four characters are the
    FILED station. Widening this walk to foreign-named files would need that
    renaming rule revisited at the same time.
    """
    root = Path(root)
    station = station.upper()
    rels: list[str] = []
    for ydir in sorted(root.iterdir()):
        if not (ydir.is_dir() and ydir.name.isdigit() and len(ydir.name) == 4):
            continue
        if years and int(ydir.name) not in years:
            continue
        for mon in MONTH_DIRS[1:]:
            rinex_dir = ydir / mon / station / session / "rinex"
            if not rinex_dir.is_dir():
                continue
            for f in sorted(rinex_dir.iterdir()):
                if f.is_file() and f.name.upper().startswith(station):
                    rels.append(str(f.relative_to(root)))
    return rels


def scan_station_raw(
    root: Path,
    station: str,
    session: str = "15s_24hr",
    *,
    years: Optional[list] = None,
) -> list[str]:
    """Enumerate a station/session's raw files as archive-relative paths.

    The station-first entry (mirrors archive-audit): the verb walks
    ``root/YYYY/mon/STATION/session/raw/`` itself — no hand-built lists.
    """
    root = Path(root)
    station = station.upper()
    rels: list[str] = []
    for ydir in sorted(root.iterdir()):
        if not (ydir.is_dir() and ydir.name.isdigit() and len(ydir.name) == 4):
            continue
        if years and int(ydir.name) not in years:
            continue
        for mon in MONTH_DIRS[1:]:
            raw_dir = ydir / mon / station / session / "raw"
            if not raw_dir.is_dir():
                continue
            for f in sorted(raw_dir.iterdir()):
                if f.is_file():
                    rels.append(str(f.relative_to(root)))
    return rels
