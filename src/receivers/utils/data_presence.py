"""Does this raw file actually contain observations?

Size is a proxy and a fragile one: turning off GLONASS/Galileo/BeiDou legitimately
shrinks a file several-fold, and a size rule would keep quarantining that station
until its baseline caught up. The honest question is whether the file contains
observation epochs at all — a receiver that has lost its sky view writes files on
schedule that decode to nothing, and that is true regardless of which
constellations are enabled.

Two decoders, because the fleet has two families:

* **SBF** (Septentrio) — count ``MeasEpoch`` blocks (ID 4027) by walking the
  block chain. Pure Python, one pass, no subprocess. Only the first
  :data:`_SBF_SCAN_BYTES` are read, so the count is a floor, not a total: a
  healthy 1 Hz hour holds 3600 epochs but reports ~1264 from a 2 MB scan
  (measured on AUST 2026-08-16 09:00). That is deliberate — the question is
  whether ANY epochs exist, and reading 6 MB to refine a number we do not use
  would cost far more than it tells us.
* **Trimble** ``.T00``/``.T02`` — needs ``runpkr00`` then ``teqc +meta``. An
  observation-less file reports a degenerate span at the GPS epoch
  (``start == final == 1980-01-01``) with no sample interval — measured on
  RFEL 2026-08-14 23:00.

The three-valued return is the whole point. :data:`UNKNOWN` means "could not
determine", and callers must treat it as *keep the file* — never as "no data".
Most reasons for not knowing (missing tool, unsupported format, decode error)
say nothing whatsoever about whether the data is good.
"""

from __future__ import annotations

import gzip
import logging
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Tri-state: True = has observations, False = provably none, None = unknown.
UNKNOWN: Optional[bool] = None

# SBF block ID carrying a measurement epoch. The ID field's low 13 bits are the
# block number; the top 3 are a revision we must mask off.
SBF_MEAS_EPOCH = 4027
SBF_ID_MASK = 0x1FFF
SBF_SYNC = b"$@"

# Reading the whole file would mean 6 MB per check for no benefit — an hour of
# 1 Hz data puts thousands of epochs in the first megabyte, and we only need to
# know whether ANY exist.
_SBF_SCAN_BYTES = 2_000_000


def _open_maybe_gz(path: Path, size: int):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return open(path, "rb")


def sbf_epoch_count(path: Path, *, scan_bytes: int = _SBF_SCAN_BYTES) -> Optional[int]:
    """Count SBF measurement epochs in the first ``scan_bytes``, or None on error.

    Walks the block chain via each header's length field rather than scanning for
    every ``$@``, so a payload that happens to contain the sync bytes cannot
    inflate the count. On a malformed length we step forward and resynchronise
    instead of giving up — a partially corrupt file may still prove it has data.
    """
    try:
        with _open_maybe_gz(path, scan_bytes) as fh:
            data = fh.read(scan_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("data_presence: cannot read %s: %s", path, exc)
        return None

    count = 0
    i = 0
    n = len(data)
    while i < n:
        i = data.find(SBF_SYNC, i)
        if i < 0 or i + 8 > n:
            break
        try:
            _crc, idfield, length = struct.unpack_from("<HHH", data, i + 2)
        except struct.error:
            break
        # A valid SBF block is >= 8 bytes and 4-byte aligned.
        if length < 8 or length % 4 or i + length > n:
            i += 2
            continue
        if (idfield & SBF_ID_MASK) == SBF_MEAS_EPOCH:
            count += 1
        i += length
    return count


def _trimble_has_observations(path: Path, timeout: int = 120) -> Optional[bool]:
    """Decode a Trimble file far enough to see whether it spans any real time."""
    try:
        from ..dissemination.convert import resolve_tool

        runpkr = resolve_tool("runpkr00")
        teqc = resolve_tool("teqc")
    except Exception as exc:  # noqa: BLE001 - a missing tool proves nothing
        logger.debug("data_presence: trimble tools unavailable (%s)", exc)
        return UNKNOWN

    with tempfile.TemporaryDirectory(prefix="datapresence_") as td:
        work = Path(td) / path.name.replace(".gz", "")
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rb") as src, open(work, "wb") as dst:
                    dst.write(src.read())
            else:
                work.write_bytes(path.read_bytes())
            subprocess.run(
                [runpkr, "-g", "-d", str(work)],
                capture_output=True,
                timeout=timeout,
                cwd=td,
            )
            dat = next(Path(td).glob("*.dat"), None)
            if dat is None:
                return UNKNOWN
            proc = subprocess.run(
                [teqc, "+meta", str(dat)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("data_presence: trimble decode failed for %s: %s", path, exc)
            return UNKNOWN

    start = final = None
    for line in proc.stdout.splitlines():
        if line.startswith("start date & time:"):
            start = line.split(":", 1)[1].strip()
        elif line.startswith("final date & time:"):
            final = line.split(":", 1)[1].strip()
    if start is None or final is None:
        return UNKNOWN
    # An observation-less file reports the GPS epoch for both ends.
    if start == final or start.startswith("1980-01-01"):
        return False
    return True


def has_observations(path: Path) -> Optional[bool]:
    """True / False / UNKNOWN for whether ``path`` contains observation epochs.

    UNKNOWN is not a soft "no" — callers must keep the file. Being unable to
    decode says nothing about whether the data is good.
    """
    try:
        name = path.name.lower().replace(".gz", "")
        if name.endswith(".sbf"):
            count = sbf_epoch_count(path)
            if count is None:
                return UNKNOWN
            return count > 0
        if name.endswith((".t00", ".t02")):
            return _trimble_has_observations(path)
        return UNKNOWN
    except Exception as exc:  # noqa: BLE001 - never let this decide by crashing
        logger.debug("data_presence: check failed for %s: %s", path, exc)
        return UNKNOWN
