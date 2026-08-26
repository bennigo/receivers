"""Tests for incremental push batching in fix_headers_station (flush_fn/flush_every).

The interruption guard: fixes are flushed to the archive every flush_every files,
so a crash loses at most one batch and a re-run resumes. These tests monkeypatch
the per-file work so they run offline and assert the flush cadence.
"""

from datetime import datetime

import pytest

from receivers.rinex import header_fix as hf


@pytest.fixture
def fake_files(monkeypatch, tmp_path):
    def _make(n, fixed=True):
        files = [f"{tmp_path}/F{i}.26D.Z" for i in range(n)]
        monkeypatch.setattr(hf, "discover_all_rinex_files", lambda *a, **k: list(files))

        def fake_fix(f, station, **k):
            return {
                "file": str(f),
                "fixed": fixed,
                "changed_labels": ["ANTENNA: DELTA H/E/N"] if fixed else [],
                "preserved_org": None,
                "error": None,
            }

        monkeypatch.setattr(hf, "fix_headers_in_file", fake_fix)
        return files

    return _make


def _run(tmp_path, *, flush_fn=None, flush_every=0, dry_run=False):
    return hf.fix_headers_station(
        "RHOF",
        "15s_24hr",
        datetime(2000, 1, 1),
        datetime(2030, 1, 1),
        all_files=True,
        work_dir=tmp_path,
        source_dir=tmp_path,
        tos_cache=object(),
        dry_run=dry_run,
        flush_fn=flush_fn,
        flush_every=flush_every,
    )


def test_flushes_every_n_plus_remainder(fake_files, tmp_path):
    fake_files(25)
    batches = []
    summary = _run(tmp_path, flush_fn=lambda b: batches.append(len(b)), flush_every=10)
    # 25 fixed, batch 10 → 10, 20, then final remainder of 5.
    assert [len(range(x)) for x in batches] == batches  # sanity
    assert batches == [10, 10, 5]
    assert summary["fixed"] == 25


def test_single_final_flush_when_batch_larger_than_count(fake_files, tmp_path):
    fake_files(7)
    batches = []
    _run(tmp_path, flush_fn=lambda b: batches.append(len(b)), flush_every=100)
    assert batches == [7]  # one flush at the end (old "push once" behaviour)


def test_no_flush_when_disabled(fake_files, tmp_path):
    fake_files(5)
    calls = []
    _run(tmp_path, flush_fn=lambda b: calls.append(b), flush_every=0)
    assert calls == []  # flush_every=0 → never flushed


def test_no_flush_on_dry_run(fake_files, tmp_path):
    fake_files(5)
    calls = []
    _run(tmp_path, flush_fn=lambda b: calls.append(b), flush_every=2, dry_run=True)
    assert calls == []  # dry-run writes nothing → never flushed


def test_clean_files_dont_trigger_flush(fake_files, tmp_path):
    fake_files(5, fixed=False)  # nothing discrepant → no fixes
    batches = []
    _run(tmp_path, flush_fn=lambda b: batches.append(len(b)), flush_every=2)
    assert batches == []  # 0 fixed → nothing to flush


def test_summary_shows_value_change_and_preservation(monkeypatch, tmp_path, capsys):
    """The run summary prints the old→new transition and a preservation count,
    while per-file preservation logs stay quiet (DEBUG)."""
    files = [f"{tmp_path}/F{i}.26D.Z" for i in range(4)]
    monkeypatch.setattr(hf, "discover_all_rinex_files", lambda *a, **k: list(files))

    def fake_fix(f, station, **k):
        # First 2 regenerable, last 2 preserved to rinex_org.
        idx = int(str(f).split("/F")[1].split(".")[0])
        preserved = idx >= 2
        return {
            "file": str(f),
            "fixed": True,
            "changed_labels": ["ANTENNA: DELTA H/E/N"],
            "changes": {"ANTENNA: DELTA H/E/N": ("1.0070", "1.0140")},
            "preserved_org": f"{f}.org" if preserved else None,
            "preserve_reason": "raw absent for RHOF 20250901" if preserved else None,
        }

    monkeypatch.setattr(hf, "fix_headers_in_file", fake_fix)
    _run(tmp_path, flush_fn=lambda b: None, flush_every=100)
    out = capsys.readouterr().out
    assert "1.0070 → 1.0140" in out  # value change shown
    assert "2 un-regenerable original(s) preserved" in out  # summarized count
    assert "raw absent" in out  # reason collapsed
    # No per-file spam in captured stdout (those are logger.debug now).
    assert "before header fix" not in out
