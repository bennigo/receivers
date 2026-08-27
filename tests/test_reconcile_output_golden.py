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
