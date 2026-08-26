"""Tests for the ``receivers station onboard`` super-verb (todo #150).

Covers the orchestration contract, not the underlying verbs: stage ordering,
the dry-run/pause gate, detached long-job launch, stage selection/resume, and
the archive-facts review + bounds resolution.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from receivers.cli import station_onboard as so
from receivers.cli.station_onboard import STAGES, OnboardContext


def _ctx(tmp_path: Path, **kw) -> OnboardContext:
    defaults = dict(
        station="VMEY",
        root=str(tmp_path),
        start=None,
        end=None,
        work_dir=None,
    )
    defaults.update(kw)
    return OnboardContext(**defaults)


# ── stage table ─────────────────────────────────────────────────────────────


def test_stage_order_matches_recipe():
    assert [s.key for s in STAGES] == [
        "tos-review",
        "rinex-review",
        "re-rinex",
        "constellation-audit",
        "fix-headers",
        "sitelog",
        "m3g",
        "sync-yaml",
        "epos-disseminate",
        "record-visit",
    ]
    assert [s.key for s in STAGES if s.long_running] == [
        "re-rinex",
        "epos-disseminate",
    ]
    mutating = {s.key for s in STAGES if s.mutating}
    assert mutating == {
        "re-rinex",
        "fix-headers",
        "sitelog",
        "m3g",
        "epos-disseminate",
        "record-visit",
    }
    # constellation-audit must sit AFTER re-rinex and BEFORE fix-headers:
    # the R3 header set is the authoritative probe, fix-headers mops up the
    # R2-stuck remainder (pipeline-refinement memory 2026-08-23).
    keys = [s.key for s in STAGES]
    assert keys.index("re-rinex") < keys.index("constellation-audit") < keys.index("fix-headers")
    # record-visit is the final auditable record, after epos-disseminate.
    assert keys[-1] == "record-visit"


def test_rerinex_argv_includes_required_bounds(tmp_path):
    ctx = _ctx(tmp_path, start="20130801", end="20260820")
    argv = so._rerinex_argv(ctx)
    assert "-s" in argv and "20130801" in argv
    assert "-e" in argv and "20260820" in argv
    assert "--from-archive" in argv and "--push" in argv and "--backup-old" in argv


def test_rerinex_bounds_resolved_from_raw_coverage(tmp_path):
    # raw dirs for 2013 and 2016 only → bounds span those years
    for y, mon in ((2013, "aug"), (2016, "dec")):
        (tmp_path / f"{y}" / mon / "VMEY" / "15s_24hr" / "raw").mkdir(parents=True)
    ctx = _ctx(tmp_path)
    argv = so._rerinex_argv(ctx)
    i_s = argv.index("-s")
    i_e = argv.index("-e")
    assert argv[i_s + 1] == "20130101"
    assert argv[i_e + 1] == "20161231"


# ── gating ──────────────────────────────────────────────────────────────────


def _ns(**kw) -> argparse.Namespace:
    defaults = dict(
        station="VMEY", session="15s_24hr", root=None, start=None, end=None,
        work_dir=None, stages=None, from_stage=None, yes=False, dry_run=False,
        log_dir=so.DEFAULT_LOG_DIR, receivers_bin="receivers", tos_bin="tos",
        participants="bgo@vedur.is", visit_date="now",
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_dry_run_never_executes(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, *, detached=False, log=None):
        calls.append(argv)

    monkeypatch.setattr(so, "_run", fake_run)
    monkeypatch.setattr(so, "_confirm", lambda p: True)
    rc = so.cmd_station_onboard(_ns(dry_run=True, root=str(tmp_path), yes=True))
    assert rc == 0
    assert calls == []  # nothing executed in dry-run


def test_yes_executes_mutating_stages(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, *, detached=False, log=None):
        calls.append((argv, detached, log))

    monkeypatch.setattr(so, "_run", fake_run)
    monkeypatch.setattr(so, "_confirm", lambda p: True)
    rc = so.cmd_station_onboard(_ns(yes=True, root=str(tmp_path)))
    assert rc == 0
    # re-rinex and epos-disseminate are long-running → detached with a log
    assert any(c[1] for c in calls), "expected detached launches"
    assert all(c[2] is not None for c in calls if c[1])


def test_confirm_false_skips_mutating(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, *, detached=False, log=None):
        calls.append(argv)

    monkeypatch.setattr(so, "_run", fake_run)
    monkeypatch.setattr(so, "_confirm", lambda p: False)
    rc = so.cmd_station_onboard(_ns(root=str(tmp_path)))
    assert rc == 0
    assert calls == []  # every gate declined


def test_stage_selection_and_resume(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, *, detached=False, log=None):
        calls.append(argv)

    monkeypatch.setattr(so, "_run", fake_run)
    monkeypatch.setattr(so, "_confirm", lambda p: True)
    so.cmd_station_onboard(
        _ns(yes=True, root=str(tmp_path), stages="sitelog,m3g", from_stage="m3g")
    )
    # from_stage=m3g within stages=sitelog,m3g → only m3g runs
    executed = calls
    assert len(executed) == 1
    assert "m3g" in executed[0]


# ── archive review ──────────────────────────────────────────────────────────


def _write_rinex(path: Path, version="3.04", marker="10217M001", agency="X"):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"{version:>9}           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE\n"
        f"VMEY                                                        MARKER NAME\n"
        f"{marker:<20}                                        MARKER NUMBER\n"
        f"{'obs':<20}{agency:<20}                                        OBSERVER / AGENCY\n"
        f"{'R1':<20}{'SEPT POLARX5':<20}{'5.5.0':<20}                        REC # / TYPE / VERS\n"
        "                                                            END OF HEADER\n"
    )
    path.write_text(content)


def test_rinex_review_reports_per_year(tmp_path):
    _write_rinex(
        tmp_path / "2018" / "jan" / "VMEY" / "15s_24hr" / "rinex" / "VMEY0010.18D",
        version="3.04", marker="10217M001", agency="Icelandic Meteorolog",
    )
    _write_rinex(
        tmp_path / "2000" / "jan" / "VMEY" / "15s_24hr" / "rinex" / "VMEY0020.00D",
        version="2.10", marker="10217M001", agency="Vedurstofa Islands",
    )
    lines = so._rinex_review_lines("VMEY", str(tmp_path), "15s_24hr")
    assert any("2018:" in ln and "3.04" in ln for ln in lines)
    assert any("2000:" in ln and "2.10" in ln for ln in lines)
    assert any("10217M001" in ln for ln in lines)


def test_rinex_review_no_root_graceful(tmp_path):
    ctx = _ctx(tmp_path, root=None)
    assert "no archive root" in so._preview_rinex_review(ctx)


# ── sync.yaml stanza ────────────────────────────────────────────────────────


def test_sync_yaml_stanza_contains_station():
    ctx = _ctx(Path("/tmp"))
    out = so._preview_sync_yaml(ctx)
    assert "    - VMEY" in out
    assert "git push" in out


def test_record_visit_argv_carries_standard_fields():
    ctx = _ctx(Path("/tmp"), participants="bgo@vedur.is", visit_date="2026-08-24")
    argv = so._visit_argv(ctx)
    assert "--type" in argv and "remote" in argv
    assert "--participants" in argv and "bgo@vedur.is" in argv
    assert "--start" in argv and "2026-08-24" in argv
    assert "--no-dry-run" in argv
    assert so.VISIT_WORK_TEXT in argv
    assert "TOS reviewed, re-rinexed to R3" in so.VISIT_WORK_TEXT
