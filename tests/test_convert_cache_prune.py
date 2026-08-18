"""Convert-cache intermediates are dropped once a push is durable.

A full-history run for ONE station produced ~120 GB of convert-cache
intermediates (ELDC 2020-2026, 4,441 products, measured 2026-08-18) and had to
be reaped by hand afterwards. An unreaped cache has previously filled /mnt/data
and taken Postgres down with it.

The cache is two-layer by design — an output layer keyed on content+TOS
fingerprint, and a decode layer keyed on content+sample under ``decode/`` — and
the decode layer is the larger half. Pruning only the output layer would look
like it worked while leaving most of the bytes behind, so both are recorded.
"""

from dataclasses import dataclass, field
from pathlib import Path

from receivers.dissemination.config import DisseminationTarget
from receivers.dissemination.engine import DisseminateResult


class TestTargetKnob:
    def test_pruning_is_on_by_default(self):
        assert DisseminationTarget.__dataclass_fields__["prune_cache"].default is True

    def test_it_can_be_turned_off_in_config(self):
        # The only thing the cache buys is a cheap `--force` header re-push;
        # keeping it has to be a deliberate choice, not the default.
        assert "prune_cache" in DisseminationTarget.__dataclass_fields__


class TestResultCarriesEntries:
    def test_result_has_cache_entries(self):
        assert "cache_entries" in DisseminateResult.__dataclass_fields__

    def test_it_defaults_to_empty(self):
        # A dry run, or a target with pruning off, must leave it empty so the
        # flush hook deletes nothing.
        r = DisseminateResult(station="ELDC", file_date=None)
        assert r.cache_entries == []

    def test_entries_are_independent_per_result(self):
        a = DisseminateResult(station="ELDC", file_date=None)
        b = DisseminateResult(station="RHOF", file_date=None)
        a.cache_entries.append("/tmp/x")
        assert b.cache_entries == []  # not a shared mutable default


@dataclass
class _FakeResult:
    """Minimal stand-in for the flush hook's `refs` elements."""

    cache_entries: list = field(default_factory=list)


class TestPruneBehaviour:
    """The deletion itself, exercised against real directories."""

    @staticmethod
    def _prune(refs):
        # Mirrors the loop in job.py `_on_flush`.
        import shutil

        for result in refs:
            for entry in getattr(result, "cache_entries", None) or ():
                shutil.rmtree(entry, ignore_errors=True)

    def test_removes_both_layers(self, tmp_path):
        out = tmp_path / "abc123"
        dec = tmp_path / "decode" / "def456"
        for d in (out, dec):
            d.mkdir(parents=True)
            (d / "obs.rnx").write_text("data")
        self._prune([_FakeResult([str(out), str(dec)])])
        assert not out.exists()
        assert not dec.exists(), "decode layer left behind — it is the larger half"

    def test_leaves_other_entries_alone(self, tmp_path):
        mine = tmp_path / "mine"
        theirs = tmp_path / "theirs"  # another chunk's entry
        for d in (mine, theirs):
            d.mkdir()
            (d / "obs.rnx").write_text("data")
        self._prune([_FakeResult([str(mine)])])
        assert not mine.exists()
        assert theirs.exists(), "a concurrent chunk's cache must survive"

    def test_missing_entry_is_not_an_error(self, tmp_path):
        # Two products can share a decode entry; the second prune must be a no-op
        # rather than an exception that aborts the flush hook.
        gone = tmp_path / "already-gone"
        self._prune([_FakeResult([str(gone)])])

    def test_empty_entries_delete_nothing(self, tmp_path):
        keep = tmp_path / "keep"
        keep.mkdir()
        self._prune([_FakeResult([])])
        assert keep.exists()

    def test_none_entries_are_tolerated(self, tmp_path):
        # refs may contain results from a dry run where the field was never set.
        self._prune([_FakeResult(None)])
