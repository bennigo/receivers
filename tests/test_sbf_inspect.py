"""Proving an SBF file is empty, before anything suggests deleting it.

sbf2rin's "No relevant data available in the SBF file" is a claim. The
runpkr00-exit-30 entry in failure_class is the standing reminder of what
happens when a converter error is read as "the data is bad" — that signature
once advised archive-rm for 1,367 files whose raw was fine. So the empty-SBF
signature points at this verifier, and the verifier is deliberately narrow.
"""

import gzip
import struct

import pytest

from receivers.rinex.failure_class import PERMANENT, classify_failure
from receivers.rinex.sbf_inspect import inspect_sbf


def _block(block_id: int, payload: bytes) -> bytes:
    """One SBF block: sync, crc, id, length, payload."""
    length = 8 + len(payload)
    return b"$@" + struct.pack("<HHH", 0, block_id, length) + payload


def _meas_epoch(n_sats: int) -> bytes:
    # hdr(8) TOW(4) WNc(2) N1(1) ... — N1 sits at byte 14 of the block.
    return _block(4027, struct.pack("<IHB", 0, 0, n_sats) + b"\x00" * 8)


def _write(tmp_path, name, blocks, gz=False):
    p = tmp_path / name
    data = b"".join(blocks)
    if gz:
        with gzip.open(p, "wb") as fh:
            fh.write(data)
    else:
        p.write_bytes(data)
    return p


class TestProvablyEmpty:
    def test_all_epochs_zero_satellites_is_provable(self, tmp_path):
        p = _write(tmp_path, "empty.sbf", [_meas_epoch(0) for _ in range(50)])
        r = inspect_sbf(p)
        assert r.epochs == 50
        assert r.epochs_with_satellites == 0
        assert r.is_provably_empty is True
        assert "zero satellites" in r.describe()

    def test_one_satellite_anywhere_defeats_it(self, tmp_path):
        blocks = [_meas_epoch(0) for _ in range(50)]
        blocks[30] = _meas_epoch(7)
        r = inspect_sbf(_write(tmp_path, "some.sbf", blocks))
        assert r.is_provably_empty is False
        assert r.has_observations is True

    def test_gzip_is_handled(self, tmp_path):
        p = _write(tmp_path, "e.sbf.gz", [_meas_epoch(0) for _ in range(10)], gz=True)
        assert inspect_sbf(p).is_provably_empty is True


class TestNarrowness:
    """Anything not positively proven empty must NOT be reported as empty."""

    def test_no_measurement_blocks_is_not_proven(self, tmp_path):
        # Only PVTGeodetic — a truncated or unusual file, not a proven-empty one.
        p = _write(tmp_path, "pvt.sbf", [_block(4007, b"\x00" * 20)] * 5)
        r = inspect_sbf(p)
        assert r.epochs == 0
        assert r.is_provably_empty is False
        assert "not proven empty" in r.describe()

    def test_unreadable_file_is_not_proven(self, tmp_path):
        missing = tmp_path / "nope.sbf"
        r = inspect_sbf(missing)
        assert r.readable is False
        assert r.is_provably_empty is False

    def test_garbage_is_not_proven(self, tmp_path):
        p = tmp_path / "junk.sbf"
        p.write_bytes(b"not an sbf file at all" * 40)
        assert inspect_sbf(p).is_provably_empty is False

    def test_meas3_encoding_counts_as_having_data(self, tmp_path):
        # Newer firmware (VMEY 2026) writes 4109-4113 and no 4027 at all.
        # Presence proves measurements; absence of 4027 must not read as empty.
        p = _write(tmp_path, "m3.sbf", [_block(4109, b"\x00" * 30)] * 20)
        r = inspect_sbf(p)
        assert r.encoding == "meas3"
        assert r.is_provably_empty is False
        assert r.has_observations is True


class TestFailureClassification:
    def test_empty_sbf_is_permanent(self):
        c = classify_failure(
            "sbf2rin failed with exit code 2\n"
            "Details: No relevant data available in the SBF file"
        )
        assert c.kind == PERMANENT
        assert c.retryable is False

    def test_the_reason_names_the_verifier_not_deletion(self):
        # The runpkr00 lesson: never route a converter error straight to
        # archive-rm. The operator must confirm against the bytes first.
        c = classify_failure("No relevant data available in the SBF file")
        assert "sbf_inspect" in c.reason
        assert "antenna" in c.reason.lower()

    @pytest.mark.parametrize(
        "msg", ["runpkr00 failed with exit code 30", "some brand new error"]
    )
    def test_unrelated_failures_keep_their_own_class(self, msg):
        assert "sbf_inspect" not in classify_failure(msg).reason
