"""Transient vs permanent classification for batch retrofit failures (todo #70).

Driving case: the ELEY 1Hz retrofit (2026-07-14) ended with 18 errors, ~13 of
them ``compress -f … exit 1``. The same files compressed fine on a manual
re-run — subprocess pressure, not bad data — so a human had to sweep a finished
job by hand.

The counter-case is equally important. ISAK's 2026-08-03 retrofit produced 6
errors, all 3–115 byte unreadable stubs, plus 2 truncated raw files in the R3
run. Those produce the identical error every time; retrying them would waste
attempts and bury the `archive-rm` signal an operator needs.
"""

from __future__ import annotations

from receivers.rinex.failure_class import (
    PERMANENT,
    TRANSIENT,
    classify_failure,
    partition_failures,
)


class TestPermanentSignatures:
    """Real error strings from the 2026-08-03 ISAK runs."""

    def test_unreadable_stub(self):
        """The 6 ISAK fix-headers errors — 3-byte and 115-byte files."""
        c = classify_failure("could not read RINEX header")
        assert c.kind == PERMANENT
        assert not c.retryable
        assert "archive-rm" in c.reason

    def test_truncated_raw(self):
        """ISAK201806270000a.sbf.gz / ISAK201907170000a.sbf.gz in the R3 run."""
        c = classify_failure(
            "in-place corrector failed: Failed to decompress "
            "ISAK201806270000a.sbf.gz: Compressed file ended before the "
            "end-of-stream marker was reached"
        )
        assert c.kind == PERMANENT

    def test_missing_tos_session(self):
        assert classify_failure("no TOS session covers this date").kind == PERMANENT

    def test_preservation_refusal_is_never_retried(self):
        """A deliberate fail-safe: retrying it would defeat the guard."""
        c = classify_failure(
            "un-regenerable (no raw file) and rinex_org preservation failed "
            "— refusing to overwrite"
        )
        assert c.kind == PERMANENT

    def test_file_not_found(self):
        assert classify_failure("file not found").kind == PERMANENT

    def test_unparseable_filename(self):
        assert (
            classify_failure("could not parse observation date from filename").kind
            == PERMANENT
        )

    def test_matching_is_case_insensitive(self):
        assert classify_failure("COULD NOT READ RINEX HEADER").kind == PERMANENT

    def test_other_corrupt_stream_phrasings(self):
        for msg in (
            "gzip: not in gzip format",
            "zlib error: invalid block type",
            "CRC check failed",
            "unexpected end of file",
        ):
            assert classify_failure(msg).kind == PERMANENT, msg

    def test_runpkr00_exit_30_is_permanent(self):
        """runpkr00 reading a Trimble .T02 and exiting non-zero is a data
        problem, not subprocess pressure — a retry reads the same bad file. exit
        30 was the fleet-wide runaway on the 2026-08-09 backfill (1367 files /
        83 stations, each retried ~2x, burning CPU that then load-gated live
        downloads)."""
        c = classify_failure("/usr/local/bin/runpkr00 failed with exit code 30")
        assert c.kind == PERMANENT
        assert not c.retryable
        assert "archive-rm" in c.reason

    def test_runpkr00_other_exit_stays_transient_until_known(self):
        """Conservative by design: an unenumerated runpkr00 code is still
        retried (fold new permanent codes in explicitly as they surface)."""
        assert classify_failure("runpkr00 failed with exit code 1").kind == TRANSIENT


class TestTransientSignatures:
    def test_the_eley_compress_failure(self):
        """~13 of ELEY's 18 errors — the reason todo #70 exists."""
        c = classify_failure("in-place corrector failed: compress -f exited 1")
        assert c.kind == TRANSIENT
        assert c.retryable

    def test_generic_subprocess_failure(self):
        assert (
            classify_failure(
                "in-place corrector failed: [Errno 11] Resource temporarily unavailable"
            ).kind
            == TRANSIENT
        )

    def test_push_failure(self):
        assert (
            classify_failure("rsync exited 12: connection reset by peer").kind
            == TRANSIENT
        )

    def test_unknown_failure_defaults_to_retryable(self):
        """Asymmetric on purpose: a wrongly-retried permanent failure costs
        seconds; a wrongly-surfaced transient one costs a manual re-run of a
        35,000-file job."""
        assert classify_failure("something nobody has seen before").kind == TRANSIENT

    def test_empty_message_is_retryable(self):
        assert classify_failure("").kind == TRANSIENT
        assert classify_failure(None).kind == TRANSIENT

    def test_accepts_an_exception_object(self):
        assert classify_failure(RuntimeError("compress -f exited 1")).kind == TRANSIENT

    def test_exception_with_permanent_text(self):
        exc = ValueError("could not read RINEX header")
        assert classify_failure(exc).kind == PERMANENT


class TestPartitionFailures:
    def test_splits_by_class(self):
        details = [
            {"source": "a", "error": "could not read RINEX header"},
            {"source": "b", "error": "compress -f exited 1"},
            {"source": "c", "error": None},
            {"source": "d"},
        ]
        retryable, permanent = partition_failures(details)
        assert [d["source"] for d in retryable] == ["b"]
        assert [d["source"] for d in permanent] == ["a"]

    def test_clean_details_are_ignored(self):
        retryable, permanent = partition_failures(
            [{"source": "a", "fixed": True, "error": None}]
        )
        assert retryable == [] and permanent == []

    def test_empty_input(self):
        assert partition_failures([]) == ([], [])

    def test_the_isak_run_would_retry_nothing(self):
        """All 6 ISAK errors were permanent — the retry pass is a no-op, which
        is the correct outcome and worth locking in."""
        details = [
            {"source": f"ISAK{i}", "error": "could not read RINEX header"}
            for i in range(6)
        ]
        retryable, permanent = partition_failures(details)
        assert retryable == []
        assert len(permanent) == 6
