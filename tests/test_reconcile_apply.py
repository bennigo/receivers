"""Unit tests for the extracted apply half of reconcile.

`tests/test_reconcile_output_golden.py` drives this code through
`_reconcile_one` and pins what an operator sees. That is the right test for the
orchestration, but it stubs `push_field_value` wholesale — so the *inside* of a
TOS push has never been tested by anything. In particular the Pattern 1 vs
Pattern 2 choice, which decides whether a metadata change OPENS A NEW PERIOD in
TOS or silently overwrites history, was previously unreachable: it lived in a
566-line CLI function behind a `silent` flag and an argparse namespace.

Being able to write these at all is most of the point of the extraction.
"""

from __future__ import annotations

import pytest

from receivers.cfg.field_manifest import fields_by_key
from receivers.cfg.reconcile_apply import (
    ApplyOutcome,
    CfgTargets,
    apply_decision,
    push_component_value,
    push_field_value,
    resolve_effective_date,
    silent_emit,
)
from receivers.cfg.reconcile_policy import ReconcilePolicy
from receivers.cfg.reconciler import FieldDiff, SourceUnavailableError, Verdict


def make_diff(cfg_key="receiver_firmware_version", **overrides) -> FieldDiff:
    kwargs = dict(
        spec=fields_by_key()[cfg_key],
        cfg_value="5.5.0",
        receiver_value="5.6.0",
        tos_value=None,
        sources_queried=frozenset({"cfg", "receiver", "tos"}),
        verdict=Verdict.CONFLICT,
    )
    kwargs.update(overrides)
    return FieldDiff(**kwargs)


@pytest.fixture
def emitted():
    lines: list[str] = []
    return lines, lines.append


# ---------------------------------------------------------------------------
# CfgTargets
# ---------------------------------------------------------------------------


def test_cfg_targets_writes_every_target():
    """`--global` writes the deployed config AND the repo copy, in lockstep.

    Writing only the first would leave the two out of step silently — the
    deployed file updated while gps-config-data, the source of truth, is not.
    """
    seen = []
    targets = CfgTargets(
        "TEST",
        [None, "/repo/stations.cfg"],
        apply_diff=lambda sid, d, v, **kw: seen.append(kw["cfg_path"]) or True,
        remove_diff=lambda sid, d, **kw: seen.append(kw["cfg_path"]) or True,
    )
    assert targets.apply(make_diff(), "5.6.0") is True
    assert seen == [None, "/repo/stations.cfg"]

    seen.clear()
    assert targets.remove(make_diff()) is True
    assert seen == [None, "/repo/stations.cfg"]


def test_cfg_targets_reports_changed_if_any_target_changed():
    """One already-correct file must not mask a real write to another."""
    results = iter([False, True])
    targets = CfgTargets(
        "TEST",
        [None, "/repo/stations.cfg"],
        apply_diff=lambda *a, **kw: next(results),
        remove_diff=lambda *a, **kw: False,
    )
    assert targets.apply(make_diff(), "5.6.0") is True


def test_cfg_targets_reports_unchanged_when_no_file_changed():
    targets = CfgTargets(
        "TEST",
        [None],
        apply_diff=lambda *a, **kw: False,
        remove_diff=lambda *a, **kw: False,
    )
    assert targets.apply(make_diff(), "5.6.0") is False


def test_cfg_targets_forwards_resolved_by():
    """`resolved_by` reaches the writer — it is what the audit log records."""
    seen = {}
    targets = CfgTargets(
        "TEST",
        [None],
        apply_diff=lambda sid, d, v, **kw: seen.update(kw) or True,
        remove_diff=lambda *a, **kw: True,
    )
    targets.apply(make_diff(), "5.6.0", resolved_by="canonicalize")
    assert seen["resolved_by"] == "canonicalize"


# ---------------------------------------------------------------------------
# push_field_value — Pattern 1 vs Pattern 2
# ---------------------------------------------------------------------------


@pytest.fixture
def tos_spy(monkeypatch):
    """Capture what would be sent to TOS without sending anything."""
    import tostools.api.tos_writer as tw

    from receivers.cfg import tos_push

    calls: dict = {"writer_dry_run": None, "upsert": None, "transition": None}
    monkeypatch.setattr(
        tw, "TOSWriter", lambda dry_run: calls.__setitem__("writer_dry_run", dry_run)
    )
    monkeypatch.setattr(
        tos_push,
        "push_field_to_tos",
        lambda **kw: calls.__setitem__("upsert", kw) or {"ok": True},
    )
    monkeypatch.setattr(
        tos_push,
        "push_field_transition_to_tos",
        lambda **kw: calls.__setitem__("transition", kw) or {"closed": True},
    )
    return calls


def test_push_field_value_upserts_when_tos_has_nothing(tos_spy, emitted):
    """Pattern 1: no prior TOS value, so there is no period to close."""
    lines, emit = emitted
    push_field_value(
        station_id="TEST",
        diff=make_diff(tos_value=None),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=True,
        emit=emit,
        no_transition=False,
        effective_date=None,
    )
    assert tos_spy["upsert"] is not None
    assert tos_spy["transition"] is None
    assert "Pattern 1 (upsert)" in "\n".join(lines)


def test_push_field_value_transitions_on_a_real_change(tos_spy, emitted):
    """Pattern 2: TOS holds a DIFFERENT value, so this is a change.

    The distinction is not cosmetic — Pattern 1 overwrites the open period in
    place and loses the fact that the old value was ever true. For a firmware
    upgrade or an instrument swap that is a corrupted history, not a typo fix.
    """
    lines, emit = emitted
    push_field_value(
        station_id="TEST",
        diff=make_diff(tos_value="5.5.0"),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=True,
        emit=emit,
        no_transition=False,
        effective_date=None,
    )
    assert tos_spy["transition"] is not None, "a change must open a new period"
    assert tos_spy["upsert"] is None
    assert tos_spy["transition"]["old_value"] == "5.5.0"
    assert tos_spy["transition"]["new_value"] == "5.6.0"
    assert "Pattern 2 (transition)" in "\n".join(lines)


def test_push_field_value_upserts_when_tos_already_agrees(tos_spy):
    """Same value is not a change; re-opening a period for it would be wrong."""
    push_field_value(
        station_id="TEST",
        diff=make_diff(tos_value="5.6.0"),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=True,
        emit=silent_emit,
        no_transition=False,
        effective_date=None,
    )
    assert tos_spy["upsert"] is not None
    assert tos_spy["transition"] is None


def test_no_transition_forces_pattern_1(tos_spy):
    """`--no-transition` is the operator saying 'this is a correction'."""
    push_field_value(
        station_id="TEST",
        diff=make_diff(tos_value="5.5.0"),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=True,
        no_transition=True,
        effective_date=None,
        emit=silent_emit,
    )
    assert tos_spy["upsert"] is not None
    assert tos_spy["transition"] is None


@pytest.mark.parametrize("dry_run", [True, False])
def test_push_field_value_hands_dry_run_to_the_writer(tos_spy, dry_run):
    """The single choke point that decides whether production TOS is touched."""
    push_field_value(
        station_id="TEST",
        diff=make_diff(),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=dry_run,
        emit=silent_emit,
        no_transition=False,
        effective_date=None,
    )
    assert tos_spy["writer_dry_run"] is dry_run


def test_push_field_value_passes_the_operator_date_through(tos_spy):
    push_field_value(
        station_id="TEST",
        diff=make_diff(),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=True,
        effective_date="2026-01-02T03:04:05",
        emit=silent_emit,
        no_transition=False,
    )
    assert tos_spy["upsert"]["date_from"] == "2026-01-02T03:04:05"


def test_push_field_value_without_tos_data_reports_and_stops(tos_spy, emitted):
    lines, emit = emitted
    push_field_value(
        station_id="TEST",
        diff=make_diff(),
        value="5.6.0",
        tos_data=None,
        dry_run=False,
        emit=emit,
        no_transition=False,
        effective_date=None,
    )
    assert tos_spy["writer_dry_run"] is None, "built a writer with no TOS data"
    assert "cannot push to TOS" in "\n".join(lines)


def test_push_field_value_reports_a_failure_instead_of_raising(monkeypatch, emitted):
    """Best-effort by design: the cfg write it follows has already succeeded."""
    import tostools.api.tos_writer as tw

    from receivers.cfg import tos_push

    monkeypatch.setattr(tw, "TOSWriter", lambda dry_run: object())

    def _boom(**kw):
        raise RuntimeError("TOS said no")

    monkeypatch.setattr(tos_push, "push_field_to_tos", _boom)
    lines, emit = emitted
    push_field_value(
        station_id="TEST",
        diff=make_diff(),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=False,
        emit=emit,
        no_transition=False,
        effective_date=None,
    )
    assert "TOS push failed: TOS said no" in "\n".join(lines)


# ---------------------------------------------------------------------------
# push_component_value
# ---------------------------------------------------------------------------

COMPONENT = {
    "entity": "antenna",
    "attribute_code": "antenna_height",
    "value": "0.1234",
}


@pytest.mark.parametrize("dry_run", [True, False])
def test_component_push_hands_dry_run_to_the_writer(monkeypatch, dry_run):
    import tostools.api.tos_writer as tw

    from receivers.cfg import tos_push

    seen: dict = {}
    monkeypatch.setattr(
        tw, "TOSWriter", lambda dry_run: seen.__setitem__("dry_run", dry_run)
    )
    monkeypatch.setattr(
        tos_push,
        "push_component_to_tos",
        lambda **kw: seen.__setitem__("kw", kw) or {"ok": True},
    )
    push_component_value(
        station_id="TEST",
        component=COMPONENT,
        tos_data={"id_entity": 1},
        dry_run=dry_run,
        emit=silent_emit,
        effective_date=None,
    )
    assert seen["dry_run"] is dry_run
    assert seen["kw"]["attribute_code"] == "antenna_height"


def test_component_push_without_tos_data_reports_and_stops(emitted):
    lines, emit = emitted
    push_component_value(
        station_id="TEST",
        component=COMPONENT,
        tos_data=None,
        dry_run=False,
        emit=emit,
        effective_date=None,
    )
    assert "no TOS data — cannot push component" in "\n".join(lines)


# ---------------------------------------------------------------------------
# apply_decision
# ---------------------------------------------------------------------------

POLICY = ReconcilePolicy(dry_run=False)


def _targets(apply_result=True, apply_exc=None):
    def _apply(sid, d, v, **kw):
        if apply_exc is not None:
            raise apply_exc
        return apply_result

    return CfgTargets(
        "TEST", [None], apply_diff=_apply, remove_diff=lambda *a, **k: True
    )


def _decide(action, value, diff=None, **kw):
    return apply_decision(
        action,
        value,
        diff if diff is not None else make_diff(),
        targets=kw.pop("targets", _targets()),
        policy=kw.pop("policy", POLICY),
        tos_data=kw.pop("tos_data", None),
        field_specs_by_key=fields_by_key(),
        emit=kw.pop("emit", silent_emit),
        station_id="TEST",
        no_transition=kw.pop("no_transition", False),
        effective_date=kw.pop("effective_date", None),
        **kw,
    )


def test_quit_stops_without_counting_anything():
    assert _decide("quit", None) == ApplyOutcome(stop=True)


def test_skip_counts_as_skipped():
    assert _decide("skip", None) == ApplyOutcome(skipped=1)


def test_unknown_action_is_a_noop_and_NOT_a_skip():
    """An unrecognised action is not the operator choosing to skip.

    Counting it as a skip would inflate the fleet summary and make a broken
    prompt look like an operator declining every field.
    """
    assert _decide("teleport", "x") == ApplyOutcome()


def test_set_with_no_value_writes_nothing():
    """Guards against a prompt returning `("set", None)` clearing a cfg key."""
    seen = []
    targets = CfgTargets(
        "TEST",
        [None],
        apply_diff=lambda *a, **kw: seen.append(a) or True,
        remove_diff=lambda *a, **kw: True,
    )
    assert _decide("set", None, targets=targets) == ApplyOutcome()
    assert seen == []


def test_set_counts_a_write_only_when_the_file_changed():
    assert _decide("set", "5.6.0", targets=_targets(True)).written == 1
    assert _decide("set", "5.6.0", targets=_targets(False)).written == 0


def test_a_failed_cfg_write_is_not_counted():
    outcome = _decide(
        "set", "5.6.0", targets=_targets(apply_exc=SourceUnavailableError("no cfg"))
    )
    assert outcome == ApplyOutcome()


def test_set_and_push_tos_does_not_push_when_the_cfg_write_failed(tos_spy):
    """A value cfg rejected must never reach TOS.

    Otherwise TOS records a value the config could not store — the two sources
    disagree, and the next reconcile run reports a conflict caused by the tool.
    """
    outcome = _decide(
        "set_and_push_tos",
        "5.6.0",
        targets=_targets(apply_exc=SourceUnavailableError("no cfg")),
        tos_data={"id_entity": 1},
    )
    assert outcome == ApplyOutcome()
    assert tos_spy["upsert"] is None and tos_spy["transition"] is None


def test_set_and_push_tos_still_pushes_when_cfg_was_already_correct(tos_spy):
    """An unchanged cfg does not mean TOS is up to date."""
    outcome = _decide(
        "set_and_push_tos",
        "5.6.0",
        targets=_targets(apply_result=False),
        tos_data={"id_entity": 1},
    )
    assert outcome.written == 0
    assert tos_spy["upsert"] is not None, "cfg unchanged suppressed the TOS push"


def test_apply_decision_takes_dry_run_from_the_policy(tos_spy):
    """`dry_run` reaches the writer only via the policy, which requires it."""
    _decide(
        "push_tos",
        "5.6.0",
        policy=ReconcilePolicy(dry_run=True),
        tos_data={"id_entity": 1},
    )
    assert tos_spy["writer_dry_run"] is True


# ---------------------------------------------------------------------------
# resolve_effective_date
# ---------------------------------------------------------------------------


def test_resolve_effective_date_prefers_the_operator_value():
    assert resolve_effective_date("2026-01-02T03:04:05") == "2026-01-02T03:04:05"


def test_resolve_effective_date_falls_back_to_now():
    got = resolve_effective_date(None)
    assert got.endswith("+00:00") and got[4] == "-"


def test_resolve_effective_date_treats_empty_string_as_absent():
    """`--effective-date ''` is not a date; falling through to now is correct."""
    assert resolve_effective_date("") != ""


# ---------------------------------------------------------------------------
# The required-argument discipline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("omit", ["dry_run", "no_transition", "effective_date", "emit"])
def test_push_field_value_refuses_to_guess(omit):
    """Omitting any TOS-write input must be a TypeError, never a default.

    `effective_date` is the sharpest of the four: defaulted, a *historical*
    correction would silently be dated "now", opening a TOS attribute period on
    the wrong day. That is a corrupted equipment history, not a cosmetic slip,
    and it is invisible until someone regenerates a site log years later.
    `no_transition` omitted silently enables Pattern 2; `dry_run` omitted is the
    difference between a preview and a production write; `emit` omitted would
    lose all operator output on a live run.

    Guards against a future "tidy-up" adding defaults back for convenience.
    """
    kwargs = dict(
        station_id="TEST",
        diff=make_diff(),
        value="5.6.0",
        tos_data={"id_entity": 1},
        dry_run=True,
        no_transition=False,
        effective_date=None,
        emit=silent_emit,
    )
    kwargs.pop(omit)
    with pytest.raises(TypeError):
        push_field_value(**kwargs)
