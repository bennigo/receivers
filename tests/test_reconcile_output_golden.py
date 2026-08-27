"""Golden-output harness for `_reconcile_one` — the safety net for splitting it.

`_reconcile_one` is ~566 lines with roughly 40 `print()` calls interleaved
through consent gates, the interactive resolution loop and LIVE TOS writes. The
architecture review wants a plan/apply layer extracted from it so a non-terminal
caller (the planned rek_new web UI) can reuse the decision logic. That refactor
was deliberately NOT attempted, for one reason: nothing could prove the terminal
output survived it, and operators diff that output against runbooks.

This file is that proof. It drives `_reconcile_one` entirely offline — its
docstring promises "no network I/O", probe data is injected by the caller — and
pins the exact bytes it prints in each non-interactive mode.

**Use it like this.** Before refactoring, run the suite: goldens exist and pass.
Refactor. Run again. Any change in what an operator sees turns these red, with a
diff. When a change IS intended, regenerate deliberately:

    RECONCILE_GOLDEN_UPDATE=1 pytest tests/test_reconcile_output_golden.py

and review the resulting diff in git as part of the change.

Only non-interactive modes are covered, because the interactive path calls
`input()`. Extending to it means injecting the prompt the same way `progress`
was injected into `receivers.cfg.probe` — which is itself a good first step of
the extraction.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import pathlib

import pytest

from receivers.cli.cfg import _reconcile_one

GOLDEN_DIR = pathlib.Path(__file__).parent / "data" / "reconcile_golden"

# A station with one clean match, one fuzzy match and one genuine conflict, so
# every verdict glyph the table can render is exercised.
STATION_CONFIG = {
    "receiver": {
        "type": "PolaRX5",
        "serial_number": "3067157",
        "firmware_version": "5.5.0",
    },
    "router": {"ip": "10.0.0.1"},
    "station": {"lat": "64.15", "lon": "-21.95", "altitude": "50.0"},
    "receiver_type": "PolaRX5",
    "receiver_serial": "3067157",
    "receiver_firmware_version": "5.5.0",
    "antenna_type": "LEIAR25.R4",
}
# SSRC7 is the IGS code for a Septentrio PolaRx5 — a FUZZY match, not a
# conflict. Firmware genuinely differs. Serial matches exactly.
RECEIVER_IDENTITY = {
    "receiver_model": "SSRC7",
    "serial_number": "3067157",
    "firmware_version": "5.6.0",
}
TOS_DATA: dict = {"device_history": [], "station": {}}

BASE_FLAGS = dict(
    json=False,
    only_diffs=False,
    dry_run=True,
    auto_fill=False,
    yes=False,
    push_tos=False,
    sync_devices=False,
    position_tolerance_m=2.0,
    canonicalize=False,
    no_dry_run=False,
    field=None,
)

MODES = {
    "dry_run_full_table": {},
    "only_diffs": {"only_diffs": True},
    "json_mode": {"json": True},
    "auto_fill_dry_run": {"auto_fill": True},
}


def _run(**overrides) -> str:
    args = argparse.Namespace(**{**BASE_FLAGS, **overrides})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _reconcile_one(
            "TEST",
            STATION_CONFIG,
            ["receiver"],
            None,
            args,
            RECEIVER_IDENTITY,
            TOS_DATA,
        )
    return buf.getvalue()


@pytest.mark.parametrize("mode", sorted(MODES))
def test_reconcile_output_is_unchanged(mode):
    """Pin the exact bytes an operator sees in each non-interactive mode."""
    produced = _run(**MODES[mode])
    golden = GOLDEN_DIR / f"{mode}.txt"

    if os.environ.get("RECONCILE_GOLDEN_UPDATE"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(produced, encoding="utf-8")
        pytest.skip(f"regenerated {golden.name}")

    assert golden.exists(), (
        f"missing golden {golden}; regenerate with "
        f"RECONCILE_GOLDEN_UPDATE=1 pytest {__file__}"
    )
    expected = golden.read_text(encoding="utf-8")
    assert produced == expected, (
        f"reconcile output changed for mode {mode!r}. If that is intended, "
        f"regenerate with RECONCILE_GOLDEN_UPDATE=1 and review the diff."
    )


def test_output_is_deterministic():
    """A golden is worthless if the same input renders differently twice."""
    assert _run() == _run()


def test_the_harness_actually_exercises_the_interesting_verdicts():
    """Guard the fixture, not just the output.

    If someone simplifies STATION_CONFIG until every field matches, the goldens
    still pass while covering nothing. Assert the fixture still produces a
    fuzzy match, an exact match and a real conflict.
    """
    out = _run()
    assert "≈" in out, "fixture no longer produces a fuzzy match (SSRC7 vs PolaRX5)"
    assert "✓" in out, "fixture no longer produces an exact match"
    assert "✗" in out, "fixture no longer produces a conflict (firmware 5.5.0 vs 5.6.0)"


def test_no_network_io_contract_holds(monkeypatch):
    """_reconcile_one documents 'no network I/O'. Prove it.

    This is what makes the whole harness trustworthy — and what makes the
    eventual plan/apply extraction verifiable offline.
    """
    import socket

    def _boom(*a, **k):
        raise AssertionError("_reconcile_one opened a socket; it must not")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    _run()  # must not raise


# ---------------------------------------------------------------------------
# Interactive path
# ---------------------------------------------------------------------------
# Reaching the prompt loop requires dry_run=False, because _reconcile_one
# returns early in dry-run — so these tests MUST stub the writers. That is not
# a compromise: it is the more valuable assertion. Recording which writes were
# attempted, in order, pins the DECISION behaviour, which is exactly what the
# planned plan/apply extraction has to reproduce. The goldens pin what the
# operator sees; these recordings pin what the machine does.


class _Scripted:
    """A prompt that replays a fixed sequence of answers, no TTY involved.

    `_interactive_prompt` already returns `(action, value)` and performs no
    writes, so this is a drop-in — the same property that will let a web form
    substitute for it.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen: list[str] = []

    def __call__(self, diff, **kwargs):
        self.seen.append(diff.cfg_key)
        return self.answers.pop(0) if self.answers else ("skip", None)


@pytest.fixture
def recorded_writes(monkeypatch):
    """Stub the cfg writer and the TOS push; record every attempt."""
    from receivers.cli import cfg as mod

    writes: list[tuple] = []
    monkeypatch.setattr(
        mod,
        "apply_diff",
        lambda sid, d, v, **kw: writes.append(("cfg", sid, d.cfg_key, v)) or True,
    )
    monkeypatch.setattr(
        mod,
        "remove_diff",
        lambda sid, d, **kw: writes.append(("remove", sid, d.cfg_key)) or True,
    )
    monkeypatch.setattr(
        mod,
        "_do_push_tos",
        lambda *a, **kw: writes.append(("tos_push", a, tuple(sorted(kw)))),
    )
    return writes


def _run_interactive(answers, recorded, **overrides):
    args = argparse.Namespace(**{**BASE_FLAGS, "dry_run": False, **overrides})
    scripted = _Scripted(answers)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _reconcile_one(
            "TEST",
            STATION_CONFIG,
            ["receiver"],
            None,
            args,
            RECEIVER_IDENTITY,
            TOS_DATA,
            prompt=scripted,
        )
    return buf.getvalue(), scripted, recorded


def test_prompt_is_injectable_and_no_tty_is_touched(recorded_writes, monkeypatch):
    """The seam exists and is honoured — builtins.input must never be called."""

    def _boom(*a, **k):
        raise AssertionError(
            "_reconcile_one called input(); the prompt seam was bypassed"
        )

    monkeypatch.setattr("builtins.input", _boom)
    out, scripted, writes = _run_interactive([("skip", None)] * 20, recorded_writes)
    assert scripted.seen, "the injected prompt was never called"


def test_skipping_everything_writes_nothing(recorded_writes):
    _, scripted, writes = _run_interactive([("skip", None)] * 20, recorded_writes)
    assert writes == [], f"skip must not write, got {writes}"


def test_quit_stops_at_the_first_field(recorded_writes):
    """`quit` must abandon the station, not fall through to the next field."""
    _, scripted, writes = _run_interactive([("quit", None)], recorded_writes)
    assert len(scripted.seen) == 1, f"prompted for {scripted.seen} after quit"
    assert writes == []


def test_set_writes_exactly_the_chosen_field(recorded_writes):
    """One `set`, then skip: exactly one cfg write, for that field only."""
    _, scripted, writes = _run_interactive(
        [("set", "5.6.0")] + [("skip", None)] * 20, recorded_writes
    )
    cfg_writes = [w for w in writes if w[0] == "cfg"]
    assert len(cfg_writes) == 1, f"expected 1 cfg write, got {cfg_writes}"
    assert cfg_writes[0][3] == "5.6.0"
    assert cfg_writes[0][2] == scripted.seen[0]


def test_interactive_output_is_unchanged(recorded_writes):
    """Golden for the interactive path, now that it can be driven headlessly."""
    produced, _, _ = _run_interactive(
        [("set", "5.6.0")] + [("skip", None)] * 20, recorded_writes
    )
    golden = GOLDEN_DIR / "interactive_set_then_skip.txt"
    if os.environ.get("RECONCILE_GOLDEN_UPDATE"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(produced, encoding="utf-8")
        pytest.skip(f"regenerated {golden.name}")
    assert golden.exists(), f"missing golden {golden}"
    assert produced == golden.read_text(encoding="utf-8")
