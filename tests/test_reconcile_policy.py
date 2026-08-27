"""`ReconcilePolicy` — the decision inputs of a reconcile run, as data.

`_reconcile_one` read fifteen inputs straight off an argparse Namespace, mostly
through `getattr(args, "...", default)`. That made the decision logic reachable
only by fabricating a Namespace with the right attribute superset, and made a
misspelling silently become a default instead of an error.

These tests pin the properties that are safety-relevant. The consent rule in
particular exists because of a real incident: interactive mode without --yes or
--dry-run means "show me the table and ask again", NOT "write to TOS for every
actionable field".
"""

from __future__ import annotations

import argparse

import pytest

from receivers.cfg.reconcile_policy import ReconcilePolicy


class TestDryRunIsRequired:
    """The one field with no default, and the reason is not style.

    `dry_run` used to be read with TWO different fallbacks:
    `getattr(args, "dry_run", False)` where it feeds consent, and
    `getattr(args, "dry_run", True)` where it constructs the TOSWriter. That
    looks like drift but is not — each is the CONSERVATIVE choice for its own
    site. Absent dry_run, the consent sites must not treat it as consent, and
    the writer sites must not build a live writer.

    Unifying them onto one value would necessarily make one side unsafe, so
    absence is an error instead.
    """

    def test_constructing_without_dry_run_is_a_type_error(self):
        with pytest.raises(TypeError):
            ReconcilePolicy()  # type: ignore[call-arg]

    def test_from_args_refuses_a_namespace_without_dry_run(self):
        with pytest.raises(AttributeError, match="dry_run"):
            ReconcilePolicy.from_args(argparse.Namespace(yes=True))

    def test_from_args_accepts_it_when_present(self):
        p = ReconcilePolicy.from_args(argparse.Namespace(dry_run=True))
        assert p.dry_run is True


class TestConsent:
    """Live writes require explicit --yes or --dry-run. Born of an incident."""

    def test_bare_interactive_is_not_consent(self):
        p = ReconcilePolicy(dry_run=False)
        assert p.consent_given is False, (
            "interactive mode without --yes or --dry-run must NOT authorise "
            "writes; it means 'show me the table and ask again'"
        )

    @pytest.mark.parametrize(
        "kwargs", [{"yes": True}, {"dry_run": True}, {"yes": True, "dry_run": True}]
    )
    def test_explicit_yes_or_dry_run_is_consent(self, kwargs):
        base = {"dry_run": False}
        base.update(kwargs)
        assert ReconcilePolicy(**base).consent_given is True


class TestReceiverPrimarySuppression:
    """Auto-pushing a receiver-authoritative value must respect BOTH flags."""

    def test_active_by_default(self):
        assert ReconcilePolicy(dry_run=True).receiver_primary_active is True

    @pytest.mark.parametrize("flag", ["no_receiver_primary", "interactive"])
    def test_either_flag_suppresses_it(self, flag):
        p = ReconcilePolicy(dry_run=True, **{flag: True})
        assert p.receiver_primary_active is False, (
            f"--{flag.replace('_', '-')} must suppress receiver-primary auto-push; "
            "an operator who asked to be consulted per field must not have "
            "values pushed for them"
        )


class TestRenderingFlags:
    def test_json_implies_silent(self):
        assert ReconcilePolicy(dry_run=True, json=True).silent is True

    @pytest.mark.parametrize("flag", ["only_diffs", "open_only"])
    def test_either_flag_hides_matching_fields(self, flag):
        assert ReconcilePolicy(dry_run=True, **{flag: True}).show_ok is False

    def test_matching_fields_shown_by_default(self):
        assert ReconcilePolicy(dry_run=True).show_ok is True


def test_policy_is_immutable():
    """A policy is a decision already taken; args-as-mutable-state caused bugs."""
    p = ReconcilePolicy(dry_run=True)
    with pytest.raises(Exception):
        p.dry_run = False  # type: ignore[misc]


def test_from_args_matches_the_real_parser_defaults():
    """from_args must be a faithful re-expression, not a new set of defaults.

    Builds the actual `cfg reconcile` parser, parses an argument-free invocation,
    and checks the policy matches what the CLI would have produced.
    """
    from receivers.cli.cfg import create_cfg_parser

    root = argparse.ArgumentParser()
    subs = root.add_subparsers()
    create_cfg_parser(subs)
    cfg = next(
        a for a in root._actions if isinstance(a, argparse._SubParsersAction)
    ).choices["cfg"]
    sub = next(a for a in cfg._actions if isinstance(a, argparse._SubParsersAction))
    args = sub.choices["reconcile"].parse_args([])

    p = ReconcilePolicy.from_args(args)
    assert p.dry_run is False
    assert p.yes is False
    assert p.auto_fill is False
    assert p.push_tos is False
    assert p.position_tolerance_m == 2.0
    assert p.position_abort_m == 50.0
    assert p.consent_given is False, "a bare `cfg reconcile` must not authorise writes"
