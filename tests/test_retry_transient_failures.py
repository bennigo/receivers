"""The retry pass folds recoveries back into the run's own counters (todo #70).

A retry that fixes a file but leaves it counted as an error is worse than no
retry at all — the operator still sweeps by hand, and now the summary lies. So
these tests are mostly about `summary` being consistent afterwards.
"""

from __future__ import annotations

from unittest.mock import patch

from receivers.rinex.header_fix import _retry_transient_failures

COMMON = dict(
    archive_old=False,
    dry_run=False,
    work_dir=None,
    source_base=None,
    session="15s_24hr",
    tos_cache=None,
    correct_hardware=frozenset(),
    loglevel=20,
)


def _summary(details, errors):
    return {
        "station": "ISAK",
        "fixed": 0,
        "skipped": 0,
        "errors": errors,
        "details": details,
    }


def _run(summary, results, *, attempts=2, pending=None):
    """Drive the retry pass with a scripted sequence of retry outcomes."""
    with patch(
        "receivers.rinex.header_fix.fix_headers_in_file", side_effect=results
    ) as m:
        _retry_transient_failures(
            summary, "ISAK", attempts=attempts, backoff=0, pending=pending, **COMMON
        )
    return m


class TestRecovery:
    def test_recovered_file_leaves_the_error_count(self):
        d = [{"source": "/a/ISAK0010.26D.Z", "error": "compress -f exited 1"}]
        s = _summary(d, errors=1)
        _run(s, [{"error": None, "changed_labels": ["MARKER NUMBER"], "fixed": True}])
        assert s["errors"] == 0
        assert s["fixed"] == 1
        assert s["recovered_on_retry"] == 1
        assert d[0]["error"] is None
        assert d[0]["recovered_after"] == 1

    def test_recovery_with_no_changes_counts_as_skipped(self):
        d = [{"source": "/a/f.Z", "error": "compress -f exited 1"}]
        s = _summary(d, errors=1)
        _run(s, [{"error": None, "changed_labels": []}])
        assert s["errors"] == 0
        assert s["skipped"] == 1
        assert s["fixed"] == 0

    def test_second_attempt_succeeds(self):
        d = [{"source": "/a/f.Z", "error": "compress -f exited 1"}]
        s = _summary(d, errors=1)
        m = _run(
            s,
            [
                {"error": "compress -f exited 1"},
                {"error": None, "changed_labels": ["MARKER NUMBER"], "fixed": True},
            ],
        )
        assert m.call_count == 2
        assert s["errors"] == 0
        assert d[0]["recovered_after"] == 2

    def test_recovered_file_joins_the_push_batch(self):
        """Otherwise the fix is made but never pushed to the archive."""
        d = [{"source": "/a/f.Z", "error": "compress -f exited 1"}]
        s = _summary(d, errors=1)
        pending: list = []
        _run(
            s,
            [{"error": None, "changed_labels": ["X"], "fixed": True}],
            pending=pending,
        )
        assert len(pending) == 1


class TestExhaustion:
    def test_still_failing_after_all_attempts(self):
        d = [{"source": "/a/f.Z", "error": "compress -f exited 1"}]
        s = _summary(d, errors=1)
        m = _run(s, [{"error": "compress -f exited 1"}] * 2)
        assert m.call_count == 2
        assert s["errors"] == 1
        assert s["recovered_on_retry"] == 0
        assert d[0]["retry_attempts"] == 2


class TestPermanentIsNeverRetried:
    def test_corrupt_stub_is_not_reattempted(self):
        """The ISAK case — 6 unreadable stubs. Zero retry calls."""
        d = [
            {"source": f"/a/{i}.Z", "error": "could not read RINEX header"}
            for i in range(6)
        ]
        s = _summary(d, errors=6)
        m = _run(s, [])
        assert m.call_count == 0
        assert s["errors"] == 6
        assert s["permanent_failures"] == 6

    def test_preservation_refusal_is_not_reattempted(self):
        d = [
            {
                "source": "/a/f.Z",
                "error": "un-regenerable (no raw) and rinex_org preservation failed",
            }
        ]
        s = _summary(d, errors=1)
        m = _run(s, [])
        assert m.call_count == 0
        assert s["errors"] == 1

    def test_mixed_batch_retries_only_the_transient(self):
        d = [
            {"source": "/a/bad.Z", "error": "could not read RINEX header"},
            {"source": "/a/flaky.Z", "error": "compress -f exited 1"},
        ]
        s = _summary(d, errors=2)
        m = _run(s, [{"error": None, "changed_labels": ["X"], "fixed": True}])
        assert m.call_count == 1
        assert s["errors"] == 1
        assert s["recovered_on_retry"] == 1


class TestNoWork:
    def test_no_failures_at_all(self):
        s = _summary([{"source": "/a/f.Z", "error": None}], errors=0)
        m = _run(s, [])
        assert m.call_count == 0

    def test_detail_without_a_source_path_is_skipped(self):
        s = _summary([{"error": "compress -f exited 1"}], errors=1)
        m = _run(s, [])
        assert m.call_count == 0
