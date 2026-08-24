"""Binary raw-file format identification + decoded-date validation.

Born from the 2026-07-06 ``.atc`` findings: the archive's ``.atc`` extension
covers THREE different raw formats (Ashtech µZ-12 "U-file", Ashtech Z-XII3
"R-file", and Septentrio PolaRx2 SBF mislabeled ``.atc``), and one batch
(RHOF ``2000/``+``2001/``) is filed a decade away from the data it contains.
Two lessons encoded here:

* **Classify by magic bytes, never by extension** — the wrong decoder either
  segfaults (``teqc -ash r`` on a U-file) or silently emits 0 bytes
  (``-ash u`` on anything else). Extension is only a last-resort hint for
  formats without a printable magic (Trimble .T02/.T00).
* **The filename's date is a claim, not a fact** — the receiver's embedded
  GPS week is authoritative. ``decoded_span()`` reads the true first/last
  epoch via ``teqc +meta`` so callers can check it against the filename
  (the misfiled RHOF batches pass every coordinate/identity check — only the
  decoded-date-vs-filename comparison catches them).
"""

from __future__ import annotations

import gzip
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.archive.raw_format")

# Format identifiers (plain strings so they serialize/log cleanly).
# ONE position-identity gate for the whole system (bgo 2026-07-06: 10 m —
# the raw-derived position is accurate to a few metres, stations are km
# apart; the converter identity gate, archive-sort AND the tostools
# header-QC coord_tolerance all use the same metric).
# Override via receivers.cfg [rinex] position_gate_m.
DEFAULT_POSITION_GATE_M = 10.0

SBF = "sbf"
ASHTECH_U = "ashtech_u"
ASHTECH_R = "ashtech_r"
TRIMBLE = "trimble"
#: Trimble "R00" raw image — the 4000SSi/4000Si era (pre-2013 archive).
#: Unlike .T02/.T00 this container carries PRINTABLE magic ("TNL RAW DATA
#: IMAGE" at offset 2), so it is identified by content, not by extension.
TRIMBLE_R00 = "trimble_r00"
UNKNOWN = "unknown"

# teqc decoder flags per format — the dispatch table the magic check feeds.
# Trimble .T02 is NOT directly teqc-readable (needs runpkr00 first) → no entry.
_TEQC_FLAGS = {
    SBF: ["-sep", "sbf"],
    ASHTECH_U: ["-ash", "u"],
    ASHTECH_R: ["-ash", "r"],
}

# English month abbreviations, index 1-12 — the archive's directory names.
# Explicit (not strftime %b) so a non-English locale can't corrupt paths.
MONTH_DIRS = (
    "",
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def read_head(path: Path, n: int = 64) -> bytes:
    """First ``n`` bytes of ``path``, transparently decompressing gzip."""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(2)
        fh.seek(0)
        if head == b"\x1f\x8b":
            with gzip.open(fh) as gz:
                return gz.read(n)
        return fh.read(n)


def classify_raw(path: Optional[Path] = None, head: Optional[bytes] = None) -> str:
    """Identify a raw file's format from its content (magic bytes).

    Extension participates only as a fallback hint for Trimble containers,
    which carry no printable magic. Everything else is content-only.
    """
    if head is None:
        if path is None:
            raise ValueError("classify_raw needs a path or a head")
        try:
            head = read_head(Path(path))
        except OSError as exc:
            logger.warning("cannot read %s: %s", path, exc)
            return UNKNOWN
    if head[:2] == b"$@":
        return SBF
    if head[4:8] == b"BHDR":
        return ASHTECH_U
    if head[:4] == b"Z-12":
        return ASHTECH_R
    # Trimble R00 raw image (4000SSi/4000Si). Printable magic at offset 2, so
    # this one IS content-identified — checked before the extension fallback
    # below so a mislabelled .r00 still routes correctly.
    if b"TNL RAW DATA IMAGE" in head[:32]:
        return TRIMBLE_R00
    if path is not None:
        suffixes = "".join(Path(path).suffixes).lower()
        if ".t02" in suffixes or ".t00" in suffixes:
            return TRIMBLE
    return UNKNOWN


@dataclass(frozen=True)
class RawMeta:
    """What ``teqc +meta`` reveals about a raw file's TRUE identity."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    station_code: Optional[str] = None  # receiver-embedded 4-char code (soft)
    lat: Optional[float] = None  # antenna position — the identity that
    lon: Optional[float] = None  # cannot be faked by a filename
    elevation: Optional[float] = None

    @property
    def span(self) -> Optional[tuple[datetime, datetime]]:
        if self.start is None or self.end is None:
            return None
        return self.start, self.end


def teqc_meta(path: Path, fmt: str, *, timeout: int = 120) -> Optional[RawMeta]:
    """Decode a raw file's metadata (epoch span, embedded station code and
    antenna position) via ``teqc +meta`` — one cheap pass, no conversion.

    Returns None when the format has no teqc decoder (Trimble — needs
    runpkr00 first) or teqc is unavailable. The values come from the
    receiver's embedded records — trust them over the filename, always.
    Position availability varies by format (Ashtech: yes; SBF: usually no).
    """
    flags = _TEQC_FLAGS.get(fmt)
    if flags is None:
        return None
    from ..dissemination.convert import resolve_tool  # avoid import cycle

    try:
        teqc = resolve_tool("teqc")
    except Exception as exc:  # noqa: BLE001 - tool resolution is environment
        logger.warning("teqc not available: %s", exc)
        return None
    proc = subprocess.run(
        [teqc, *flags, "+meta", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    vals: dict = {}
    for line in proc.stdout.splitlines():
        if line.startswith("start date & time:"):
            vals["start"] = _parse_meta_dt(line)
        elif line.startswith("final date & time:"):
            vals["end"] = _parse_meta_dt(line)
        elif line.startswith("4-char station code:"):
            code = line.split(":", 1)[1].strip()
            if code and code != "-Unknown-":
                vals["station_code"] = code.upper()
        elif line.startswith("antenna latitude (deg):"):
            vals["lat"] = _parse_meta_float(line)
        elif line.startswith("antenna longitude (deg):"):
            vals["lon"] = _parse_meta_float(line)
        elif line.startswith("antenna elevation (m):"):
            vals["elevation"] = _parse_meta_float(line)
    if not vals:
        logger.warning(
            "teqc +meta gave nothing for %s (fmt=%s, rc=%s)", path, fmt, proc.returncode
        )
        return None
    return RawMeta(**_drop_placeholders(vals, path, fmt))


#: teqc's "I could not decode this" values, which it reports as if they were
#: measurements. On Septentrio SBF it emits ALL of them for every file:
#: ``start/final date & time: 1980-01-01``, ``antenna latitude 90``,
#: ``antenna longitude 0``. They must become None — a placeholder taken as
#: data is worse than no data, because every downstream check trusts it.
_GPS_TIME_ZERO = datetime(1980, 1, 6)
_PLACEHOLDER_LATLON = (90.0, 0.0)


def _drop_placeholders(vals: dict, path, fmt: str) -> dict:
    """Blank teqc's undecodable-input placeholders so they read as absent.

    A date before GPS time zero (1980-01-06) cannot be an observation — it
    is teqc's epoch default. ``(lat, lon) == (90, 0)`` is the north pole,
    which is not a station. Both mean "teqc could not decode this input",
    and both are what teqc reports for EVERY Septentrio ``.sbf.gz``.

    Left as data, the date placeholder made ``archive-sort`` classify every
    correctly-filed Septentrio file as ``wrong-date`` and propose relocating
    it to ``1980/jan/`` — where every file of a station also collides on one
    filename. That is the entire Septentrio fleet, which is most of it.
    """
    out = dict(vals)
    for key in ("start", "end"):
        dt = out.get(key)
        if dt is not None and dt < _GPS_TIME_ZERO:
            logger.debug(
                "teqc +meta: %s is a pre-GPS placeholder (%s) for %s (fmt=%s) "
                "— treating the date as undecodable",
                key,
                dt,
                path,
                fmt,
            )
            out[key] = None
    if (out.get("lat"), out.get("lon")) == _PLACEHOLDER_LATLON:
        logger.debug(
            "teqc +meta: position is the (90,0) placeholder for %s (fmt=%s) "
            "— treating the position as undecodable",
            path,
            fmt,
        )
        out["lat"] = None
        out["lon"] = None
    return out


def _parse_meta_float(line: str) -> Optional[float]:
    try:
        return float(line.split(":", 1)[1].strip())
    except (ValueError, IndexError):
        return None


def decoded_span(
    path: Path, fmt: str, *, timeout: int = 120
) -> Optional[tuple[datetime, datetime]]:
    """True (first, last) epoch of a raw file — see :func:`teqc_meta`."""
    meta = teqc_meta(path, fmt, timeout=timeout)
    if meta is None or meta.span is None:
        if meta is not None:
            logger.warning("teqc +meta gave no epoch span for %s", path)
        return None
    return meta.span


def _parse_meta_dt(line: str) -> Optional[datetime]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", line)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, s)


# Archive raw filename: STATION + YYYYMMDDHHMM + session letter + extension,
# e.g. RHOF201004020000a.atc / HUSM202606270000a.sbf.gz
_RAW_NAME_RE = re.compile(
    r"^(?P<sta>[A-Z0-9]{4})"
    r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})(?P<h>\d{2})(?P<mi>\d{2})"
    r"(?P<letter>[a-z])\.(?P<ext>[A-Za-z0-9.]+)$"
)


@dataclass(frozen=True)
class ParsedRawName:
    station: str
    claimed: datetime  # the date+time the FILENAME claims
    session_letter: str
    ext: str


def parse_raw_name(name: str) -> Optional[ParsedRawName]:
    m = _RAW_NAME_RE.match(name)
    if not m:
        return None
    try:
        claimed = datetime(
            int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"])
        )
    except ValueError:
        return None
    return ParsedRawName(m["sta"], claimed, m["letter"], m["ext"])


def build_raw_name(
    parsed: ParsedRawName,
    true_start: datetime,
    *,
    station: Optional[str] = None,
    ext: Optional[str] = None,
) -> str:
    """The corrected filename: DECODED date, optionally the TRUE station and
    the extension matching the actual content."""
    sta = (station or parsed.station).upper()
    e = ext or parsed.ext
    return f"{sta}{true_start:%Y%m%d%H%M}{parsed.session_letter}.{e}"


# The extension a format's files should carry so extension-keyed tooling
# (converter selection, session globs) picks the right chain. Ashtech has no
# modern canonical extension — .atc is its home in this archive.
CANONICAL_EXT = {
    SBF: "sbf",
    ASHTECH_U: "atc",
    ASHTECH_R: "atc",
}
