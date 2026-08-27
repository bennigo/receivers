"""Unit tests for the intake precedence — CLI > file > default.

`tests/test_cli_add_receiver.py` drives this through `cmd_cfg_add_receiver`,
which is the right test for the verb. These test the rule directly, which is
what a web form calling `IntakeRequest.resolved()` will actually exercise —
and what makes the ordering visible instead of implied by three `if not
getattr(...)` passes scattered through a 450-line function.
"""

from __future__ import annotations

import argparse

import pytest

from receivers.cfg.intake_request import (
    DEFAULT_LOCATION,
    DEFAULT_OWNER,
    FILE_FILLABLE,
    IntakeRequest,
)


def ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# ---------------------------------------------------------------------------
# The precedence chain
# ---------------------------------------------------------------------------


def test_cli_beats_file_beats_default():
    """All three levels at once, on one field — the whole rule in one assert."""
    req = IntakeRequest.resolved(
        ns(owner="CLI Group"), {"owner": "File Group"}, today="2026-01-01"
    )
    assert req.owner == "CLI Group"

    req = IntakeRequest.resolved(ns(owner=None), {"owner": "File Group"})
    assert req.owner == "File Group"

    req = IntakeRequest.resolved(ns(owner=None), {})
    assert req.owner == DEFAULT_OWNER


def test_the_file_never_overrides_an_explicit_argument():
    """Every fillable field, not just the one that happens to be tested."""
    cli = {f: f"cli-{f}" for f in FILE_FILLABLE}
    file_data = {f: f"file-{f}" for f in FILE_FILLABLE}
    req = IntakeRequest.resolved(ns(**cli), file_data)
    for f in FILE_FILLABLE:
        assert getattr(req, f) == f"cli-{f}", f"file overrode CLI for {f}"


def test_an_empty_string_counts_as_not_supplied():
    """`--owner ""` takes the default; the gates test falsiness, not None.

    An `Optional[str]` with an `is None` check would carry `""` through to a
    TOS write that then fails validation. This is the distinction the
    extraction was most likely to erase.
    """
    req = IntakeRequest.resolved(ns(owner="", location=""), {})
    assert req.owner == DEFAULT_OWNER
    assert req.location == DEFAULT_LOCATION


def test_an_empty_string_in_the_file_is_ignored_too():
    """A blank key in the YAML must not be treated as a supplied value.

    Asserted on `firmware`, NOT on `owner`: owner has a DEFAULT, so an empty
    file value produces the default either way and the test cannot tell the
    two apart. Verified by mutation — the owner-based version of this test
    stayed green when `not in (None, "")` was relaxed to `is not None`.

    `firmware` has no default, so `""` vs `None` is observable, and `None` is
    the honest answer: the file did not say.
    """
    req = IntakeRequest.resolved(ns(firmware=None), {"firmware": ""})
    assert req.firmware is None, "a blank file value was treated as supplied"

    # And the owner case still resolves correctly, for the same input shape.
    assert IntakeRequest.resolved(ns(owner=None), {"owner": ""}).owner == DEFAULT_OWNER


def test_missing_attributes_on_the_namespace_are_not_an_error():
    """A caller need not supply every flag — a bare Namespace resolves."""
    req = IntakeRequest.resolved(ns(), {})
    assert req.owner == DEFAULT_OWNER
    assert req.location == DEFAULT_LOCATION
    assert req.date_start


# ---------------------------------------------------------------------------
# date_start and determinism
# ---------------------------------------------------------------------------


def test_today_is_injectable_so_the_object_is_not_time_dependent():
    """`date.today()` inside a frozen value object makes tests flaky.

    Same argument that keeps `effective_date` a raw Optional[str] in
    reconcile_apply: resolve "now" at use, never at construction.
    """
    req = IntakeRequest.resolved(ns(), {}, today="2026-05-12")
    assert req.date_start == "2026-05-12"


def test_from_args_does_not_resolve_today():
    """Reading the CLI layer must not invent a date."""
    assert IntakeRequest.from_args(ns()).date_start is None


def test_an_explicit_date_survives_the_default():
    req = IntakeRequest.resolved(ns(date_start="2020-01-02"), {}, today="2026-05-12")
    assert req.date_start == "2020-01-02"


def test_a_file_date_beats_today_but_not_the_cli():
    assert (
        IntakeRequest.resolved(ns(), {"date_start": "2021-03-04"}, today="2026-05-12")
    ).date_start == "2021-03-04"
    assert (
        IntakeRequest.resolved(
            ns(date_start="2019-09-09"), {"date_start": "2021-03-04"}
        )
    ).date_start == "2019-09-09"


# ---------------------------------------------------------------------------
# Immutability and validation
# ---------------------------------------------------------------------------


def test_the_request_is_frozen():
    """The precedence is decided once, at the edge.

    `args`-as-mutable-state is how defaults appeared out of nowhere five frames
    down — the thing this object exists to stop.
    """
    req = IntakeRequest.resolved(ns(), {})
    with pytest.raises(Exception):
        req.owner = "somebody else"  # type: ignore[misc]


def test_merging_returns_a_new_object():
    base = IntakeRequest.from_args(ns(owner=None))
    merged = base.merged_with_file({"owner": "File Group"})
    assert base.owner is None, "merged_with_file mutated the original"
    assert merged.owner == "File Group"


def test_required_check_runs_AFTER_defaults():
    """It can only fire if a default is itself empty — preserved deliberately.

    Checking before defaults would start rejecting invocations that are valid
    today (no --owner, no file owner: the default supplies it).
    """
    assert IntakeRequest.resolved(ns(), {}).missing_required() == []
    # Pre-default, the same input IS missing all three — proving the ordering
    # is what makes it pass.
    assert set(IntakeRequest.from_args(ns()).missing_required()) == {
        "owner",
        "location",
        "date_start",
    }
