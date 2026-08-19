"""Raw-presence / RINEX regenerability check for the fix-headers safety net.

`rinex --fix-headers` overwrites a RINEX header **in place** on the archive.
That is only safe when the RINEX is *regenerable* — i.e. the raw receiver file
it was converted from still exists AND is in a format we know how to convert.
When it is NOT regenerable, the archived RINEX is the sole surviving copy of
that observation; overwriting it in place risks irreversible data loss, so the
original must be preserved to a permanent ``rinex_org/`` sibling first.

"Regenerable" therefore requires BOTH:
  * a raw file for the same station+date in the ``raw/`` sibling directory, and
  * that raw file being in a recognised format (a converter exists for it).

A raw file present in an UNRECOGNISED format is treated exactly like raw-absent
— we cannot regenerate from it, so the RINEX is still irreplaceable.

Recognised raw extensions mirror the converter registry in
``async_converter._create_converter``:
    .sbf  (Septentrio / PolaRX / mosaic)   .T02 (Trimble NetR9)
    .T00  (Trimble NetRS)                  .m00 (Leica / G10)
    .r00  (Trimble 4000SSi/4000Si)
each optionally compressed (``.gz`` / ``.Z``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.rinex.raw_presence")

# Base raw extensions (lowercase, compression stripped) we have a converter for.
# Keep in sync with async_converter._create_converter's raw_extension returns.
#: NOTE — ``.r00`` is deliberately ABSENT despite R00Converter existing.
#:
#: The candidate match below is ``date_tag in p.name``, and the archive names
#: R00 files by SESSION START: 951 of VMEY's 977 are ``…YYYYMMDD2359a.r00``,
#: whose data belongs to the NEXT day. So for day D the matcher picks the file
#: stamped D — which actually contains D+1. Listing ``.r00`` here therefore
#: reports ``regenerable=True`` on the strength of the WRONG DAY's raw, and
#: fix-headers skips the ``rinex_org`` preservation for ~977 irreplaceable VMEY
#: days. Un-recognised is the SAFE direction: those days get preserved.
#:
#: Re-add only once the filename→observation-date offset rule ships AND one
#: R00 day has round-tripped through re-rinex past the identity gate.
KNOWN_RAW_EXTENSIONS: frozenset[str] = frozenset({".sbf", ".t02", ".t00", ".m00"})

# Compression suffixes stripped before matching the base extension.
_COMPRESSION_SUFFIXES = (".gz", ".z")


def strip_raw_compression(name: str) -> str:
    """Return ``name`` with a single trailing .gz/.Z compression suffix removed."""
    low = name.lower()
    for suf in _COMPRESSION_SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


def raw_format_recognised(filename: str) -> bool:
    """True if ``filename`` is a raw file in a format we can convert to RINEX."""
    base = strip_raw_compression(filename)
    return Path(base).suffix.lower() in KNOWN_RAW_EXTENSIONS


@dataclass
class RegenerabilityResult:
    """Outcome of the regenerability check for one archived RINEX file."""

    regenerable: bool
    reason: str                       # human-readable explanation
    raw_file: Optional[Path] = None   # the convertible raw, when regenerable


def _raw_sibling_dir(rinex_file: Path) -> Path:
    """`.../<session>/rinex/FILE` → `.../<session>/raw/` (sibling of rinex/)."""
    return rinex_file.parent.parent / "raw"


def check_regenerable(
    rinex_file: Path,
    observation_date,
    *,
    station_id: str,
    session_type: Optional[str] = None,
) -> RegenerabilityResult:
    """Decide whether ``rinex_file`` can be regenerated from an archived raw file.

    Looks in the ``raw/`` sibling directory for a raw file whose name carries the
    same date stamp as ``observation_date`` (raw files are date-stamped, e.g.
    ``RHOF202606210000a.T02.gz``). Returns regenerable only when such a file
    exists AND its format is recognised.

    Granularity is set by ``session_type``: an HOURLY session (``…1hr``) must
    match ``YYYYMMDDHH`` — a daily/day-only match would falsely accept a raw for
    a DIFFERENT hour of the same day and skip preservation (data-loss direction).
    A daily session matches ``YYYYMMDD``. When ``session_type`` is None we assume
    daily (the only granularity fix-headers currently parses); hourly callers
    MUST pass it.
    """
    raw_dir = _raw_sibling_dir(Path(rinex_file))
    if not raw_dir.is_dir():
        return RegenerabilityResult(False, f"no raw/ dir alongside {rinex_file.name}")

    hourly = bool(session_type and "1hr" in session_type.lower())
    date_tag = observation_date.strftime("%Y%m%d%H" if hourly else "%Y%m%d")
    # Raw files for the day: name contains the date tag (station prefix + stamp).
    candidates = [
        p for p in raw_dir.iterdir()
        if p.is_file() and date_tag in p.name
    ]
    if not candidates:
        return RegenerabilityResult(
            False, f"raw absent for {station_id} {date_tag}"
        )

    recognised = [p for p in candidates if raw_format_recognised(p.name)]
    if not recognised:
        names = ", ".join(sorted(p.name for p in candidates)[:3])
        return RegenerabilityResult(
            False,
            f"raw present but format unrecognised ({names})",
        )
    return RegenerabilityResult(
        True, f"regenerable from {recognised[0].name}", raw_file=recognised[0]
    )
