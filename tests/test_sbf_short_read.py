"""`_find_sbf_block` must wait for the block's declared length.

Returning on block-ID match handed callers a truncated buffer whenever the
block straddled a recv() boundary. The parsers then degraded silently:
PVTSatCartesian read its satellite count from the header (so `total` stayed
right) while the SatInfo walk broke early and dropped the tail. SatInfo runs in
SVID order and BeiDou holds the highest SVIDs (201-263), so BeiDou was always
the casualty — measured on NPSK as 8 short reads out of 8, and 6/6 complete
after this fix.
"""

from __future__ import annotations

import struct

from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor


def _block(block_id: int, payload: bytes) -> bytes:
    """Build a minimal SBF block: $@ + CRC + ID + length + payload."""
    length = 8 + len(payload)
    return (
        b"$@"
        + b"\x00\x00"
        + struct.pack("<H", block_id)
        + struct.pack("<H", length)
        + payload
    )


def _find(data: bytes, block_id: int):
    return PolaRX5TCPExtractor._find_sbf_block(
        PolaRX5TCPExtractor("127.0.0.1", "TEST"), data, block_id
    )


def test_complete_block_is_returned():
    blk = _block(4008, b"\x01" * 40)
    assert _find(blk, 4008) == blk


def test_truncated_block_is_not_returned():
    """The whole point: a short buffer must read as 'keep going', not 'here it is'."""
    blk = _block(4008, b"\x01" * 40)
    assert _find(blk[:-10], 4008) is None


def test_block_is_sliced_to_its_declared_length():
    """A parser must not be able to read on into the following block."""
    first = _block(4008, b"\x01" * 20)
    second = _block(4013, b"\x02" * 20)
    got = _find(first + second, 4008)
    assert got == first
    assert len(got) == struct.unpack("<H", first[6:8])[0]


def test_a_later_block_is_still_found():
    stream = _block(4013, b"\x02" * 16) + _block(4008, b"\x03" * 16)
    assert _find(stream, 4008) == _block(4008, b"\x03" * 16)


def test_absent_block_returns_none():
    assert _find(_block(4013, b"\x02" * 16), 4008) is None


def test_truncated_target_after_a_complete_other_block():
    """Realistic split: an earlier block arrived whole, ours did not."""
    stream = _block(4013, b"\x02" * 16) + _block(4008, b"\x03" * 40)[:-12]
    assert _find(stream, 4008) is None
