"""Unit tests for the --change / --correct write intent.

`tests/test_cli_update_device.py` drives this through the verb, which is the
right test for the verb. These test the rule directly — which is what a
non-terminal caller gets, and where the actual hazard lives: argparse enforces
exactly-one from a terminal, but the logic used to read
`in_place = bool(args.correct)`, so any OTHER caller that set neither flag
silently got a Pattern 2 transition.

Getting it wrong damages the TOS temporal record in opposite directions:
`--correct` on a real upgrade erases the upgrade; `--change` on a typo invents
one.
"""

from __future__ import annotations

import argparse

import pytest

from receivers.cfg.device_update_policy import (
    DeviceUpdatePolicy,
    IntentNotDeclaredError,
    WriteIntent,
)


def ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# ---------------------------------------------------------------------------
# The intent must be declared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {},  # a bare namespace — the web-UI case
        {"change": False, "correct": False},  # neither declared
        {"change": True, "correct": True},  # both, which is meaningless
    ],
)
def test_an_undeclared_intent_is_an_error_not_a_default(kw):
    """Absence must be impossible to express, not silently Pattern 2.

    This is the hazard the policy object exists for. argparse makes it
    unreachable from a terminal; nothing made it unreachable from anywhere
    else, and the silent answer was "record a history transition".
    """
    with pytest.raises(IntentNotDeclaredError):
        DeviceUpdatePolicy.from_args(ns(**kw))


def test_the_refusal_explains_both_directions_of_damage():
    """An operator hitting this needs to know which flag to pick, and why."""
    with pytest.raises(IntentNotDeclaredError) as exc:
        DeviceUpdatePolicy.from_args(ns())
    msg = str(exc.value)
    assert "--change" in msg and "--correct" in msg
    assert "no default" in msg


# ---------------------------------------------------------------------------
# Which pattern each intent selects
# ---------------------------------------------------------------------------


def test_correct_is_pattern_1_in_place():
    p = DeviceUpdatePolicy.from_args(ns(change=False, correct=True))
    assert p.intent is WriteIntent.CORRECT
    assert p.in_place is True
    assert "Pattern 1" in p.mode_label


def test_change_is_pattern_2_transition():
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False))
    assert p.intent is WriteIntent.CHANGE
    assert p.in_place is False
    assert "Pattern 2" in p.mode_label


# ---------------------------------------------------------------------------
# The vitjun rule
# ---------------------------------------------------------------------------


def test_correct_never_files_a_vitjun():
    """Fixing a record is not a field event — a vitjun would fabricate a visit."""
    p = DeviceUpdatePolicy.from_args(ns(change=False, correct=True))
    assert p.wants_vitjun(anything_changed=True) is False


def test_change_files_a_vitjun_when_something_was_written():
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False))
    assert p.wants_vitjun(anything_changed=True) is True


def test_no_vitjun_opts_out_even_for_a_change():
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False, no_vitjun=True))
    assert p.wants_vitjun(anything_changed=True) is False


def test_a_change_that_wrote_nothing_files_no_vitjun():
    """A no-op run is not a maintenance event either."""
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False))
    assert p.wants_vitjun(anything_changed=False) is False


def test_visit_label_follows_visit_type():
    change = {"change": True, "correct": False}
    assert (
        DeviceUpdatePolicy.from_args(ns(**change, visit_type="remote")).visit_label
        == "Fjarvitjun"
    )
    assert (
        DeviceUpdatePolicy.from_args(ns(**change, visit_type="onsite")).visit_label
        == "Staðarvitjun"
    )


# ---------------------------------------------------------------------------
# dry-run polarity
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_when_the_flag_is_absent():
    """This verb's parser defines `--no-dry-run`, so absence means PREVIEW.

    Reading the wrong polarity turns every unflagged call into a live TOS
    write. The conservative reading of absence is the safe one.
    """
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False))
    assert p.dry_run is True


def test_no_dry_run_commits():
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False, no_dry_run=True))
    assert p.dry_run is False


# ---------------------------------------------------------------------------
# Immutability and time
# ---------------------------------------------------------------------------


def test_the_policy_is_frozen():
    p = DeviceUpdatePolicy.from_args(ns(change=True, correct=False))
    with pytest.raises(Exception):
        p.intent = WriteIntent.CORRECT  # type: ignore[misc]


def test_date_is_kept_raw_so_the_object_is_not_time_dependent():
    """`today` is resolved at use, never at construction — see reconcile_apply."""
    assert DeviceUpdatePolicy.from_args(ns(change=True, correct=False)).date is None
    assert (
        DeviceUpdatePolicy.from_args(
            ns(change=True, correct=False, date="2026-05-30")
        ).date
        == "2026-05-30"
    )
