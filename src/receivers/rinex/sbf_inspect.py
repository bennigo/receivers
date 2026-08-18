"""Prove whether an SBF file actually contains satellite observations.

``sbf2rin`` exits 2 with "No relevant data available in the SBF file" for a raw
file that carries no usable measurements. That message is a *claim*. Before
anything recommends removing an archive file on the strength of it, the claim
should be checked against the bytes — the ``runpkr00 exit 30`` entry in
:mod:`receivers.rinex.failure_class` is the standing reminder of what happens
when a converter error is read as "the data is bad": that signature once advised
``archive-rm`` for 1,367 files whose raw was perfectly fine.

What an empty file looks like
-----------------------------

Measured on VMEY 2022-11-01 (88 KB gzipped, 670 KB raw):

    MeasEpoch  (4027)   5760 blocks
    PVTGeodetic(4007)   5760 blocks

5760 epochs x 15 s = exactly 86,400 s. The receiver was powered, healthy and
logging on schedule for the full day — and **every** ``MeasEpoch`` carries
``N1 == 0``, i.e. zero satellites. That is an antenna-side fault (disconnected
or broken cable, failed element, water ingress): the receiver keeps writing
epochs and observes nothing. A whole day compresses to ~1 % of normal because
every epoch is identical and empty.

Two measurement encodings
-------------------------

Older firmware writes ``MeasEpoch`` (4027) with a plain satellite count. Newer
firmware writes the compressed ``Meas3`` family (4109-4113) — VMEY 2026-06-14
carries 5760 of each and no 4027 at all. Meas3 payloads are not worth decoding
here: their *presence* already means measurements were recorded, which is the
only question this module answers.
"""

from __future__ import annotations

import gzip
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["SbfObservations", "inspect_sbf", "SBF_SYNC"]

SBF_SYNC = b"$@"

#: MeasEpoch — the classic measurement block. Byte 14 of the block is ``N1``,
#: the number of type-1 sub-blocks, i.e. satellites in that epoch.
_MEAS_EPOCH = 4027
_MEAS_EPOCH_N1_OFFSET = 14

#: The Meas3 family. Presence alone proves measurements exist; the payload is
#: compressed and decoding it would tell us nothing more for this purpose.
_MEAS3_BLOCKS = frozenset({4109, 4110, 4111, 4112, 4113})


@dataclass(frozen=True)
class SbfObservations:
    """What an SBF file contains, for the "is this junk?" question only."""

    epochs: int  #: measurement epochs seen (any encoding)
    epochs_with_satellites: int  #: epochs carrying at least one satellite
    max_satellites: int  #: highest satellite count in any epoch
    encoding: str  #: 'meas_epoch' | 'meas3' | 'none'
    readable: bool = True  #: False when the file could not be parsed at all

    @property
    def has_observations(self) -> bool:
        """True when at least one epoch recorded at least one satellite."""
        return self.epochs_with_satellites > 0

    @property
    def is_provably_empty(self) -> bool:
        """True only when the file parsed, HAS epochs, and none carry a satellite.

        Deliberately narrow. A file we could not read, or one with no
        measurement blocks at all, is *unproven* — it may be truncated, a
        different format, or something this parser does not understand. Only
        "the receiver logged epochs and saw nothing" is provable here, and only
        that should ever justify removing an archive file.
        """
        return self.readable and self.epochs > 0 and self.epochs_with_satellites == 0

    def describe(self) -> str:
        if not self.readable:
            return "unreadable — not proven empty"
        if self.epochs == 0:
            return "no measurement blocks found — not proven empty"
        if self.is_provably_empty:
            return (
                f"{self.epochs} epochs, ALL with zero satellites "
                f"(receiver logging, antenna dead)"
            )
        return (
            f"{self.epochs} epochs, {self.epochs_with_satellites} with satellites "
            f"(max {self.max_satellites})"
        )


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as fh:
                return fh.read()
        return path.read_bytes()
    except Exception as exc:  # noqa: BLE001 — unreadable is a valid answer
        logger.debug("sbf_inspect: cannot read %s: %s", path, exc)
        return None


def inspect_sbf(
    path: str | Path, *, max_epochs: Optional[int] = None
) -> SbfObservations:
    """Walk ``path``'s SBF blocks and report whether any epoch saw a satellite.

    ``max_epochs`` caps the scan for a quick verdict on a large file; the empty
    case is uniform, so an early exit on the first satellite-bearing epoch makes
    a healthy file cheap to check.
    """
    data = _read_bytes(Path(path))
    if data is None:
        return SbfObservations(0, 0, 0, "none", readable=False)

    epochs = 0
    with_sats = 0
    max_sats = 0
    encoding = "none"
    i = 0
    n = len(data)
    while i < n - 8:
        if data[i : i + 2] != SBF_SYNC:
            i += 1
            continue
        try:
            _crc, ident, length = struct.unpack_from("<HHH", data, i + 2)
        except struct.error:
            break
        # A plausible block: non-zero length that stays inside the file. Anything
        # else is a false sync match inside a payload, so step one byte and retry.
        if length < 8 or i + length > n:
            i += 1
            continue
        block = ident & 0x1FFF
        if block == _MEAS_EPOCH and length > _MEAS_EPOCH_N1_OFFSET:
            encoding = "meas_epoch"
            epochs += 1
            sats = data[i + _MEAS_EPOCH_N1_OFFSET]
            if sats:
                with_sats += 1
                max_sats = max(max_sats, sats)
                if max_epochs is None:
                    # One satellite anywhere is enough to answer the question.
                    return SbfObservations(epochs, with_sats, max_sats, encoding)
        elif block in _MEAS3_BLOCKS:
            # Presence proves measurements; there is nothing further to decide.
            return SbfObservations(1, 1, 0, "meas3")
        i += length
        if max_epochs is not None and epochs >= max_epochs:
            break

    return SbfObservations(epochs, with_sats, max_sats, encoding)
