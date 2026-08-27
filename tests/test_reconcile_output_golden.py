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

**Two fixture stations, and the reason there are two.** `STATION_CONFIG` drives
the main decision loop. It reaches neither the `--push-tos` batch gate, nor
`--sync-devices`, nor the placeholder-cleanup block, nor the tos-fillable block:
the first two are off in `BASE_FLAGS`, and the last two produce empty work-lists
for this station (verified — see
`test_the_primary_fixture_genuinely_cannot_reach_the_write_blocks`). Those four
blocks contain the live-TOS-write consent gate, so leaving them unpinned while
extracting an apply layer is the one part of this refactor that can regress a
safety guard with a green suite.

`STATION_CONFIG_B` / `STATION_CONFIG_C` exist to reach them. They are SEPARATE
constants rather than edits to `STATION_CONFIG`, deliberately: editing the shared
fixture would regenerate all seven existing goldens and destroy the
before/after byte-identity property that is the whole point of having them.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import pathlib
from unittest import mock

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
    # Record the VALUE, not just that a push happened. The earlier version
    # stored `(a, tuple(sorted(kw)))` — positional args are always empty and
    # kwargs were reduced to their names — so it could only ever assert "a push
    # occurred". That let `d.receiver_value or new_value` (the `or` matters: an
    # unchanged-but-successful cfg write still pushes the RECEIVER value)
    # change to anything at all without a test noticing.
    # Patched on `reconcile_apply`, not on the CLI: the push moved there, and
    # `cli.cfg` deliberately calls it module-qualified so this one patch covers
    # BOTH the applier's calls and the two the CLI still makes directly.
    from receivers.cfg import reconcile_apply

    monkeypatch.setattr(
        reconcile_apply,
        "push_field_value",
        lambda **kw: writes.append(
            ("tos_push", kw["station_id"], kw["diff"].cfg_key, kw["value"])
        ),
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


# ---------------------------------------------------------------------------
# The automatic decision branches
# ---------------------------------------------------------------------------
# --auto-fill and --yes only reach the per-field decision loop when
# dry_run=False; in dry-run _reconcile_one returns early. So the dry-run goldens
# above do NOT cover those branches, and extracting them into
# cfg/reconcile_plan.py would have been unverified without these.


@pytest.mark.parametrize("mode", ["yes", "auto_fill"])
def test_automatic_decision_output_is_unchanged(mode, recorded_writes):
    """Golden for the branches that only run with dry_run=False.

    Both get a scripted prompt, because neither flag resolves EVERY field:
    --auto-fill only fills MISSING values, so a genuine conflict (here,
    firmware 5.5.0 vs 5.6.0) still falls through and asks. That is correct
    behaviour and worth having pinned — a future change that made --auto-fill
    silently resolve conflicts would show up here.
    """
    flags = {"yes": True} if mode == "yes" else {"auto_fill": True}
    args = argparse.Namespace(**{**BASE_FLAGS, "dry_run": False, **flags})
    scripted = _Scripted([("skip", None)] * 20)
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
    produced = buf.getvalue()
    if mode == "auto_fill":
        assert scripted.seen, (
            "--auto-fill resolved every field without asking; it must only fill "
            "MISSING values and leave conflicts to the operator"
        )

    golden = GOLDEN_DIR / f"auto_{mode}.txt"
    if os.environ.get("RECONCILE_GOLDEN_UPDATE"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(produced, encoding="utf-8")
        pytest.skip(f"regenerated {golden.name}")
    assert golden.exists(), f"missing golden {golden}"
    assert produced == golden.read_text(encoding="utf-8")


def test_yes_accepts_the_suggestion_and_writes_it(recorded_writes):
    """--yes must actually write, and write the suggested value."""
    args = argparse.Namespace(**{**BASE_FLAGS, "dry_run": False, "yes": True})
    with contextlib.redirect_stdout(io.StringIO()):
        _reconcile_one(
            "TEST",
            STATION_CONFIG,
            ["receiver"],
            None,
            args,
            RECEIVER_IDENTITY,
            TOS_DATA,
        )
    cfg_writes = [w for w in recorded_writes if w[0] == "cfg"]
    assert cfg_writes, "--yes wrote nothing; the decision branch is not reached"
    assert all(w[3] is not None for w in cfg_writes), "wrote a None value"


def test_json_mode_never_prompts_and_never_writes(recorded_writes, monkeypatch):
    """JSON mode cannot ask, so it must skip — not guess."""

    def _boom(*a, **k):
        raise AssertionError("JSON mode prompted; it has no way to receive an answer")

    monkeypatch.setattr("builtins.input", _boom)
    args = argparse.Namespace(**{**BASE_FLAGS, "dry_run": False, "json": True})
    with contextlib.redirect_stdout(io.StringIO()):
        _reconcile_one(
            "TEST",
            STATION_CONFIG,
            ["receiver"],
            None,
            args,
            RECEIVER_IDENTITY,
            TOS_DATA,
        )
    assert recorded_writes == [], f"JSON mode wrote {recorded_writes}"


# ---------------------------------------------------------------------------
# The write blocks the primary fixture cannot reach
# ---------------------------------------------------------------------------
# Four blocks of `_reconcile_one` are invisible to everything above:
#
#   --push-tos batch + consent gate   push_tos=False in BASE_FLAGS
#   --sync-devices                    sync_devices=False in BASE_FLAGS
#   placeholder cleanup               STATION_CONFIG has no placeholder value
#   tos-fillable offer                sources=["receiver"], so nothing is fillable
#
# The last two also call `input()` DIRECTLY rather than the injected `prompt` —
# so `test_prompt_is_injectable_and_no_tty_is_touched` passes not because the
# seam is honoured everywhere, but because the blocks that bypass it are
# unreachable in that fixture. Pinned here so the gap is recorded rather than
# invisible; closing it is a separate step (inject a prompt there too).

# Fixture B — antenna_serial holds a TOS synthetic device identifier, which
# `_strip_placeholder` normalises to None, making that row a cfg_placeholder.
# Querying TOS with an EMPTY device history makes the receiver/antenna fields
# tos-fillable (cfg has a value, TOS has none).
STATION_CONFIG_B = {
    "receiver_type": "PolaRX5",
    "receiver_serial": "3067157",
    "receiver_firmware_version": "5.5.0",
    "antenna_serial": "antenna-TEST-20210527",
    "antenna_type": "LEIAR25.R4",
    "router_ip": "10.0.0.1",
}
RECEIVER_IDENTITY_B = {
    "receiver_model": "SSRC7",
    "serial_number": "3067157",
    "firmware_version": "5.6.0",
}
TOS_DATA_B: dict = {"device_history": [], "station": {}, "id_entity": 4242}

# Fixture C — TOS carries a synthetic antenna serial: the device EXISTS and its
# serial is recorded-as-unknown. Semantically very different from "TOS has no
# value", and the only way to reach the `[z]` branch, which exists so an
# operator does not blindly push a cfg serial that may belong to a PREVIOUS
# device onto the current one.
STATION_CONFIG_C = {
    "receiver_type": "PolaRX5",
    "receiver_serial": "3067157",
    "receiver_firmware_version": "5.6.0",
    "antenna_type": "LEIAR25.R4",
    "antenna_serial": "CR52000123",
}
TOS_DATA_C: dict = {
    "id_entity": 4242,
    "device_history": [
        {
            "time_to": None,
            "gnss_receiver": {
                "model": "SEPT POLARX5",
                "serial_number": "3067157",
                "firmware_version": "5.6.0",
            },
            "antenna": {
                "model": "LEIAR25.R4",
                "serial_number": "antenna-TEST-20230706",
            },
        }
    ],
}


_UNSET = object()


def _run_b(
    answers=None,
    *,
    station_config=None,
    tos_data=_UNSET,
    sources=("receiver", "tos"),
    keys="q",
    **overrides,
):
    """Drive `_reconcile_one` against fixture B with both sources queried.

    ``keys`` is what `input()` returns — needed because the placeholder-cleanup
    and tos-fillable blocks call `input()` directly rather than the injected
    ``prompt``. Having to pass it AT ALL is the gap being recorded; the default
    ``"q"`` makes both blocks a no-op so tests aimed at the other branches are
    not perturbed by them.
    """
    args = argparse.Namespace(**{**BASE_FLAGS, "dry_run": False, **overrides})
    scripted = _Scripted(answers if answers is not None else [("skip", None)] * 20)
    buf = io.StringIO()
    with (
        contextlib.redirect_stdout(buf),
        mock.patch("builtins.input", lambda *a, **k: keys),
    ):
        _, n_written, n_skipped = _reconcile_one(
            "TEST",
            station_config if station_config is not None else STATION_CONFIG_B,
            list(sources),
            None,
            args,
            RECEIVER_IDENTITY_B,
            TOS_DATA_B if tos_data is _UNSET else tos_data,
            prompt=scripted,
        )
    scripted.counts = (n_written, n_skipped)
    return buf.getvalue(), scripted


def test_the_primary_fixture_genuinely_cannot_reach_the_write_blocks():
    """Justify fixture B's existence — and catch it becoming redundant.

    If STATION_CONFIG ever grows a placeholder or a tos-fillable field, the
    reasoning in this module's docstring stops being true and the duplicate
    fixture should be reconsidered. Assert the premise rather than trusting it.
    """
    from receivers.cfg.reconciler import compare_station
    from receivers.cli.cfg import _is_tos_fillable

    diffs = compare_station(
        station_id="TEST",
        station_config=STATION_CONFIG,
        receiver_identity=RECEIVER_IDENTITY,
        tos_data=TOS_DATA,
        fields=None,
        queried_sources={"receiver", "cfg"},
        field_specs=None,
    )
    assert not [d for d in diffs if d.cfg_placeholder]
    assert not [d for d in diffs if _is_tos_fillable(d)]


def test_fixture_b_reaches_all_four_uncovered_blocks():
    """Guard the fixture, not just the output.

    Mirrors `test_the_harness_actually_exercises_the_interesting_verdicts`: if
    someone simplifies fixture B, the tests below would keep passing while
    covering nothing.
    """
    from receivers.cfg.reconciler import compare_station
    from receivers.cli.cfg import _is_tos_fillable

    diffs = compare_station(
        station_id="TEST",
        station_config=STATION_CONFIG_B,
        receiver_identity=RECEIVER_IDENTITY_B,
        tos_data=TOS_DATA_B,
        fields=None,
        queried_sources={"receiver", "tos", "cfg"},
        field_specs=None,
    )
    assert [d for d in diffs if d.cfg_placeholder], "no placeholder row"
    assert [d for d in diffs if _is_tos_fillable(d)], "nothing tos-fillable"
    assert [
        d
        for d in diffs
        if d.needs_attention and d.spec.tos_writable and d.receiver_value is not None
    ], "nothing for --push-tos to push"


# --- --push-tos batch mode + its consent gate ------------------------------


def test_push_tos_without_consent_pushes_nothing(recorded_writes):
    """`--push-tos` alone is NOT consent to write to production TOS.

    Interactive mode without `--yes` or `--dry-run` means "show me the table and
    ask again". §4.2 of the architecture review records this as a safety feature
    born of a real incident. A regression here writes to live TOS.
    """
    out, _ = _run_b(push_tos=True)
    assert not [w for w in recorded_writes if w[0] == "tos_push"], (
        f"--push-tos without --yes pushed to TOS: {recorded_writes}"
    )
    assert "--push-tos batch mode would write" in out
    assert "Re-run with --yes to confirm" in out


def test_push_tos_with_yes_pushes_every_writable_field(recorded_writes):
    """With explicit consent the batch push happens — and only for writable fields."""
    _run_b(push_tos=True, yes=True)
    pushes = [w for w in recorded_writes if w[0] == "tos_push"]
    assert pushes, "--push-tos --yes pushed nothing; the batch branch is unreached"


def test_push_tos_batch_refuses_without_a_tos_source(recorded_writes):
    """`--push-tos --source receiver` has nowhere to push; it must say so.

    `tos_data` is deliberately POPULATED even though TOS is not a source. With
    `tos_data=None` — which is what `_probe_station` really returns in this
    case — the test is tautological: there is nothing to push to either way, so
    deleting the `"tos" in sources` clause leaves it green. Verified by
    mutation; the first version of this test proved nothing.

    The assertion is therefore on the BATCH announcement, not on pushes in
    general: with `--yes` the per-field loop legitimately pushes too, and
    conflating the two is what made the guard invisible.
    """
    out, _ = _run_b(sources=("receiver",), push_tos=True, yes=True)
    assert "--push-tos requires --source tos" in out
    assert "field(s) to TOS" not in out, (
        "the --push-tos batch block ran without a TOS source"
    )


def test_push_tos_in_dry_run_returns_before_the_batch_block(recorded_writes):
    """Pin a surprising truth: `--push-tos --dry-run` previews no pushes.

    `_reconcile_one` returns early in dry-run (unless `--canonicalize`), so the
    batch block is never reached — even though `consent_given` would be True
    there. Recorded as observed behaviour, not as an endorsement: an extraction
    that "helpfully" made dry-run fall through would change what an operator
    sees, and should do so deliberately.
    """
    out, _ = _run_b(push_tos=True, dry_run=True)
    assert not [w for w in recorded_writes if w[0] == "tos_push"]
    assert "Pushing" not in out


# --- --sync-devices --------------------------------------------------------


@pytest.fixture
def recorded_sync(monkeypatch):
    """Stub device creation in TOS; record whether it was invoked."""
    from receivers.cli import cfg as mod

    calls: list = []
    monkeypatch.setattr(mod, "_sync_devices_to_tos", lambda **kw: calls.append(kw) or 0)
    return calls


def test_sync_devices_without_consent_creates_nothing(recorded_sync, recorded_writes):
    out, _ = _run_b(push_tos=True, sync_devices=True)
    assert recorded_sync == [], "created TOS device entities without consent"
    assert "--sync-devices requires --yes or --dry-run" in out


def test_sync_devices_with_consent_creates(recorded_sync, recorded_writes):
    _run_b(push_tos=True, sync_devices=True, yes=True)
    assert recorded_sync, "--sync-devices --yes never reached the creation path"


def test_sync_devices_needs_push_tos(recorded_sync, recorded_writes):
    """`--sync-devices` is gated on `--push-tos` as well as on consent."""
    _run_b(sync_devices=True, yes=True)
    assert recorded_sync == [], "--sync-devices ran without --push-tos"


# --- placeholder cleanup ---------------------------------------------------
# NOTE: the interactive half of this block calls `input()` directly instead of
# the injected `prompt`. That is a real gap in the seam, pinned rather than
# fixed here.


def test_canonicalize_removes_placeholders(recorded_writes):
    _, scripted = _run_b(canonicalize=True)
    removals = [w for w in recorded_writes if w[0] == "remove"]
    assert removals, "--canonicalize did not remove the placeholder"
    assert removals[0][2] == "antenna_serial"
    # The canonicalize path has its OWN n_written increment, separate from the
    # interactive one. Asserting only the recorded removal left that counter
    # free to drift — verified by mutation.
    assert scripted.counts == (2, 1), (
        "expected 1 notation rewrite + 1 placeholder removal written, 1 skipped"
    )


def test_canonicalize_dry_run_removes_nothing(recorded_writes):
    out, _ = _run_b(canonicalize=True, dry_run=True)
    assert not [w for w in recorded_writes if w[0] == "remove"]
    assert "remove 'antenna-TEST-20210527' (dry-run)" in out


def test_placeholder_keep_removes_nothing(recorded_writes):
    """Interactive `[k]eep` must leave the placeholder in cfg."""
    _run_b(keys="k")
    assert not [w for w in recorded_writes if w[0] == "remove"]


def test_placeholder_delete_removes_it(recorded_writes):
    _run_b(keys="d")
    removals = [w for w in recorded_writes if w[0] == "remove"]
    assert removals, "interactive [d]elete removed nothing"
    assert removals[0][2] == "antenna_serial"


# --- tos-fillable offer ----------------------------------------------------


def test_tos_fillable_keep_pushes_nothing(recorded_writes):
    _run_b(keys="k")
    assert not [w for w in recorded_writes if w[0] == "tos_push"]


def test_tos_fillable_capital_c_pushes_the_cfg_value(recorded_writes):
    """`C` is the only way cfg's value reaches TOS from this block."""
    _run_b(keys="C")
    assert [w for w in recorded_writes if w[0] == "tos_push"], (
        "C pushed nothing; the tos-fillable branch is unreached"
    )


def test_tos_fillable_quit_stops_the_block(recorded_writes):
    _run_b(keys="q")
    assert not [w for w in recorded_writes if w[0] == "tos_push"]


def test_tos_placeholder_offers_z_and_writes_the_unknown_marker(recorded_writes):
    """TOS records the serial as UNKNOWN → `z` writes cfg's unknown marker.

    Deliberately bypasses `cfg_format`: writing a value that `normalize` strips
    is the point. Fixture C is the only one that reaches this.
    """
    out, _ = _run_b(keys="z", station_config=STATION_CONFIG_C, tos_data=TOS_DATA_C)
    cfg_writes = [w for w in recorded_writes if w[0] == "cfg"]
    assert ("cfg", "TEST", "antenna_serial", "0000000000") in cfg_writes, (
        f"z did not write the unknown marker; got {cfg_writes}"
    )
    assert "recorded as UNKNOWN" in out


# --- prompt-driven push actions -------------------------------------------
# These DO go through the injected prompt, but no existing test returns them,
# so the branches were unexecuted.


def test_prompt_push_tos_action_pushes_without_writing_cfg(recorded_writes):
    _run_b([("push_tos", "5.6.0")] + [("skip", None)] * 20)
    assert [w for w in recorded_writes if w[0] == "tos_push"], "push_tos pushed nothing"
    assert not [w for w in recorded_writes if w[0] == "cfg"], (
        "push_tos wrote cfg; it must only touch TOS"
    )


def test_prompt_set_and_push_tos_does_both(recorded_writes):
    out, _ = _run_b([("set_and_push_tos", "5.6.0")] + [("skip", None)] * 20)
    assert [w for w in recorded_writes if w[0] == "cfg"], "no cfg write"
    assert [w for w in recorded_writes if w[0] == "tos_push"], "no TOS push"
    # The "set" and "set_and_push_tos" branches print DIFFERENT text for the
    # same event. Pin it: an extraction that unified the message would be a
    # silent change to what operators diff against runbooks.
    assert "wrote receiver_firmware_version = '5.6.0' to cfg" in out


def test_prompt_component_push_uses_the_policy_dry_run(monkeypatch, recorded_writes):
    """The component branch builds its OWN TOSWriter — with policy.dry_run.

    It does not route through `_do_push_tos`, so `recorded_writes` does not see
    it. That independence is exactly why it needs its own test: an extraction
    that let this writer default `dry_run` would authorise live TOS writes.
    """
    import tostools.api.tos_writer as tw

    from receivers.cfg import tos_push

    seen: dict = {}
    monkeypatch.setattr(
        tw, "TOSWriter", lambda dry_run: seen.setdefault("dry_run", dry_run)
    )
    monkeypatch.setattr(
        tos_push,
        "push_component_to_tos",
        lambda **kw: seen.setdefault("pushed", kw) or {"ok": True},
    )

    component = {
        "entity": "antenna",
        "attribute_code": "antenna_height",
        "value": "0.1234",
    }
    out, _ = _run_b([("push_component", component)] + [("skip", None)] * 20)
    assert seen.get("pushed"), f"component push never happened; output was:\n{out}"
    assert seen["dry_run"] is False, "live run built a dry-run writer"

    seen.clear()
    _run_b(
        [("push_component", component)] + [("skip", None)] * 20,
        canonicalize=True,
        dry_run=True,
    )
    if seen:  # dry-run returns early unless it falls through; only assert if reached
        assert seen["dry_run"] is True, "dry-run built a LIVE TOS writer"


# --- the returned counters -------------------------------------------------
# `_reconcile_one` returns `(diffs, n_written, n_skipped)` and the caller prints
# a fleet-wide summary from them. Nothing above reads that tuple, so the two
# counters could drift silently through an extraction — they accumulate across
# FOUR separate blocks with different rules:
#
#   n_written  cfg writes + placeholder removals   (NOT TOS pushes)
#   n_skipped  `skip` decisions + placeholder-keep + tos-fillable-keep


def test_counters_when_everything_is_declined(recorded_writes):
    """Quitting both interactive blocks: one skipped field, nothing written."""
    _, scripted = _run_b(keys="q")
    assert scripted.counts == (0, 1)


def test_counters_count_placeholder_removal_as_a_write(recorded_writes):
    """A placeholder REMOVAL increments n_written — it is a cfg mutation.

    `keys="d"` deletes the placeholder and, in the tos-fillable block, falls
    through to "keep" for each of the three fillable fields.
    """
    _, scripted = _run_b([("set", "5.6.0")], keys="d")
    n_written, n_skipped = scripted.counts
    assert n_written == 2, "expected 1 cfg write + 1 placeholder removal"
    assert n_skipped == 3, "expected the 3 tos-fillable fields to count as kept"


def test_tos_push_is_not_counted_as_a_cfg_write(recorded_writes):
    """A TOS push is not a cfg mutation and must not inflate n_written."""
    _, scripted = _run_b([("push_tos", "5.6.0")], keys="q")
    assert [w for w in recorded_writes if w[0] == "tos_push"], "no push happened"
    assert scripted.counts[0] == 0, "a TOS push was counted as a cfg write"


def test_set_and_push_tos_pushes_the_receiver_value_not_the_typed_one(
    recorded_writes,
):
    """`d.receiver_value or new_value` — the receiver value wins when present.

    An operator can `[e]dit` a different value into cfg; TOS still receives what
    the receiver actually reports. Pinned because the fallback is an `or`, not a
    None-check, and a "tidy-up" to `new_value` would silently change what
    reaches production TOS.
    """
    _run_b([("set_and_push_tos", "9.9.9")], keys="q")
    pushes = [w for w in recorded_writes if w[0] == "tos_push"]
    assert pushes, "set_and_push_tos pushed nothing"
    assert pushes[0][3] == "5.6.0", (
        f"pushed {pushes[0][3]!r}; the receiver value 5.6.0 must win over the "
        f"typed value"
    )


# --- the two answers nothing pinned -----------------------------------------
# Both are properties of the raw `input()` handling in the placeholder and
# tos-fillable blocks. They are pinned HERE, against the un-extracted code, so
# the prompt extraction that follows is verified against them rather than
# defining them.


def test_bare_enter_DELETES_the_placeholder(recorded_writes):
    """Empty input is `delete`, not skip — a destructive default.

    `if choice in ("d", "delete", "")`. Whether that default is wise is a
    separate question; it is what the tool does today, and an extraction that
    quietly turned bare Enter into "keep" would change behaviour operators rely
    on without anyone noticing.
    """
    _run_b(keys="")
    removals = [w for w in recorded_writes if w[0] == "remove"]
    assert removals, "bare Enter no longer deletes the placeholder"
    assert removals[0][2] == "antenna_serial"


def test_lowercase_c_does_NOT_push_cfg_to_tos(recorded_writes):
    """The tos-fillable prompt is case-SENSITIVE, unlike the placeholder one.

    It reads `.strip()` where the placeholder block reads `.strip().lower()`.
    That asymmetry is load-bearing: `C` pushes a cfg value into TOS, and a
    stray lowercase `c` must not. Normalising both while extracting would make
    `c` start writing to production TOS.
    """
    _run_b(keys="c")
    assert not [w for w in recorded_writes if w[0] == "tos_push"], (
        "lowercase 'c' pushed to TOS; the prompt must stay case-sensitive"
    )
