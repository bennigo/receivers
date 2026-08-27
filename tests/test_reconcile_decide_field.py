"""`decide_field` — the per-field rules, now that they are reachable.

These rules used to live inside `_reconcile_one`, each branch interleaved with
the `print()` explaining it. That made them untestable except through a
terminal, and unreachable for the planned rek_new web UI.

The golden harness covers the `--yes` path end to end, but not `--auto-fill`:
that branch only fires on a MISSING value with a suggestion, and the
integration fixture has no such field. Rather than contort the fixture, the
branches are exercised directly here — which is the point of having extracted
them.

The exact `message` strings are asserted because they are what the operator
sees. The golden files pin them in situ; these pin them at the source, so a
change shows up next to the rule that produced it.
"""

from __future__ import annotations

import pytest

from receivers.cfg.field_manifest import fields_by_key
from receivers.cfg.reconcile_plan import decide_field, is_receiver_primary
from receivers.cfg.reconcile_policy import ReconcilePolicy
from receivers.cfg.reconciler import FieldDiff, Verdict


def _diff(cfg_key="receiver_serial", **kw):
    spec = fields_by_key()[cfg_key]
    base = dict(
        spec=spec,
        cfg_value=None,
        receiver_value="RX-1",
        tos_value=None,
        verdict=Verdict.MISSING,
        suggestion="RX-1",
        suggestion_source="receiver",
    )
    base.update(kw)
    return FieldDiff(**base)


ASK = None


class TestAutoFill:
    """--auto-fill fills a MISSING value from a suggestion, and nothing else."""

    def test_fills_a_missing_value(self):
        d = decide_field(
            _diff(),
            ReconcilePolicy(dry_run=False, auto_fill=True),
            receiver_primary_active=False,
            tos_available=False,
        )
        assert d is not None
        assert (d.action, d.value) == ("set", "RX-1")
        assert d.message == "auto-fill from receiver: 'RX-1'"

    def test_does_not_touch_a_conflict(self):
        """A CONFLICT is not MISSING — it must still be asked about.

        This is the behaviour the integration golden also demonstrates: running
        `--auto-fill` against a firmware conflict still prompts.
        """
        d = decide_field(
            _diff(verdict=Verdict.CONFLICT, cfg_value="OLD"),
            ReconcilePolicy(dry_run=False, auto_fill=True),
            receiver_primary_active=False,
            tos_available=False,
        )
        assert d is ASK, "--auto-fill must not silently resolve a conflict"

    def test_does_nothing_without_a_suggestion(self):
        d = decide_field(
            _diff(suggestion=None, suggestion_source=None),
            ReconcilePolicy(dry_run=False, auto_fill=True),
            receiver_primary_active=False,
            tos_available=False,
        )
        assert d is ASK


class TestYes:
    def test_accepts_any_suggestion_including_a_conflict(self):
        d = decide_field(
            _diff(verdict=Verdict.CONFLICT, cfg_value="OLD"),
            ReconcilePolicy(dry_run=False, yes=True),
            receiver_primary_active=False,
            tos_available=False,
        )
        assert d is not None
        assert (d.action, d.value) == ("set", "RX-1")
        assert d.message == "accept suggestion (receiver): 'RX-1'"

    def test_takes_the_receiver_value_for_a_primary_field_with_no_suggestion(self):
        """--yes on a receiver-authoritative field still trusts the receiver."""
        d = decide_field(
            _diff(suggestion=None, suggestion_source=None),
            ReconcilePolicy(dry_run=False, yes=True),
            receiver_primary_active=True,
            tos_available=True,
        )
        assert d is not None
        assert (d.action, d.value) == ("set_and_push_tos", "RX-1")
        assert d.message == "accept receiver (primary): 'RX-1' (cfg + TOS)"


class TestPushSuffix:
    """`(cfg + TOS)` appears only when the value is genuinely pushable."""

    def test_primary_and_agreed_source_pushes(self):
        d = decide_field(
            _diff(),
            ReconcilePolicy(dry_run=False, yes=True),
            receiver_primary_active=True,
            tos_available=True,
        )
        assert d.action == "set_and_push_tos"
        assert d.message.endswith("(cfg + TOS)")

    def test_no_tos_queried_means_no_push(self):
        """Nothing to push to if TOS was never queried."""
        d = decide_field(
            _diff(),
            ReconcilePolicy(dry_run=False, yes=True),
            receiver_primary_active=True,
            tos_available=False,
        )
        assert d.action == "set"
        assert "(cfg + TOS)" not in d.message

    def test_suppressed_receiver_primary_means_no_push(self):
        d = decide_field(
            _diff(),
            ReconcilePolicy(dry_run=False, yes=True),
            receiver_primary_active=False,
            tos_available=True,
        )
        assert d.action == "set"

    def test_a_tos_sourced_suggestion_is_not_pushed_back(self):
        """Pushing a TOS-derived value back to TOS is circular."""
        d = decide_field(
            _diff(suggestion_source="tos"),
            ReconcilePolicy(dry_run=False, yes=True),
            receiver_primary_active=True,
            tos_available=True,
        )
        assert d.action == "set"


class TestSilentAndAsk:
    def test_json_mode_skips_rather_than_guessing(self):
        """JSON mode has no way to receive an answer, so it must not invent one."""
        d = decide_field(
            _diff(verdict=Verdict.CONFLICT, cfg_value="OLD"),
            ReconcilePolicy(dry_run=False, json=True),
            receiver_primary_active=False,
            tos_available=False,
        )
        assert d is not None
        assert d.action == "skip"
        assert d.value is None

    def test_bare_run_asks(self):
        """No flags: the operator decides. `None`, not a 'skip' decision."""
        d = decide_field(
            _diff(verdict=Verdict.CONFLICT, cfg_value="OLD"),
            ReconcilePolicy(dry_run=False),
            receiver_primary_active=False,
            tos_available=False,
        )
        assert d is ASK, (
            "absence of a decision must be distinguishable from deciding to skip"
        )


class TestIsReceiverPrimary:
    """Every clause of the gate is load-bearing; drop one and TOS gets written."""

    @pytest.mark.parametrize(
        "kw,expected",
        [
            (dict(receiver_primary_active=True, tos_available=True), True),
            (dict(receiver_primary_active=False, tos_available=True), False),
            (dict(receiver_primary_active=True, tos_available=False), False),
        ],
    )
    def test_gate(self, kw, expected):
        assert is_receiver_primary(_diff(), **kw) is expected

    def test_no_receiver_value_is_not_primary(self):
        assert (
            is_receiver_primary(
                _diff(receiver_value=None),
                receiver_primary_active=True,
                tos_available=True,
            )
            is False
        )

    def test_a_non_tos_writable_field_is_never_pushed(self):
        """The tos_writable clause must gate on its own.

        Written first against ``rinex_marker_number`` (deliberately not
        tos_writable: cfg follows TOS, and pushing cfg's value up re-introduces
        the wrong marker). That test PASSED WITH THE CLAUSE DELETED — mutation
        testing caught it — because no real field is currently both
        ``receiver_primary`` and non-``tos_writable``, so the earlier
        ``receiver_primary`` clause was doing all the work.

        The clause is defence in depth for the next field that combines them,
        so it is tested against a spec that actually combines them.
        """
        import dataclasses

        assert not fields_by_key()["rinex_marker_number"].tos_writable

        primary_spec = fields_by_key()["receiver_serial"]
        assert primary_spec.receiver_primary and primary_spec.tos_writable
        # tos_writable is DERIVED (tos_attribute_code and tos_target_entity are
        # both set), not an init field — that is the manifest's design, so drop
        # the underlying code rather than the property.
        not_writable = dataclasses.replace(primary_spec, tos_attribute_code=None)
        assert not not_writable.tos_writable

        d = _diff()
        d = dataclasses.replace(d, spec=not_writable)
        assert (
            is_receiver_primary(d, receiver_primary_active=True, tos_available=True)
            is False
        ), "a receiver-primary field that TOS will not accept must not be pushed"
