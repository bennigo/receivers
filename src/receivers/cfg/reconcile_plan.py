"""What to do about one field — decided, not performed.

The per-field rules inside ``_reconcile_one`` were pure decisions wrapped in
``print()`` calls: given a diff and a policy, choose ``set`` / ``set_and_push_tos``
/ ``skip``, or fall through to asking the operator. Being tangled with rendering
made them unreachable from anything but a terminal, which is the concrete thing
blocking the planned rek_new web UI.

This module is the decision half. It performs no I/O, prints nothing, and knows
nothing about argparse — hand it a :class:`~receivers.cfg.reconciler.FieldDiff`
and a :class:`~receivers.cfg.reconcile_policy.ReconcilePolicy` and it returns a
:class:`FieldDecision`, or ``None`` meaning "this one needs a human".

The ``message`` on each decision is the text the CLI prints. It lives here
rather than at the call site so the reason a decision was made travels with the
decision — a web UI wants to show it too, and keeping the two together is what
stops them drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .reconcile_policy import ReconcilePolicy
from .reconciler import FieldDiff, Verdict

#: Sources whose suggestion is trustworthy enough to also push to TOS when the
#: field is receiver-authoritative. "agree" means receiver and TOS already match.
_PUSHABLE_SOURCES = ("receiver", "agree")


@dataclass(frozen=True)
class FieldDecision:
    """One resolved decision about one field.

    ``action`` matches the vocabulary ``_reconcile_one`` already used:
    ``set`` (write cfg), ``set_and_push_tos`` (write cfg AND push to TOS), or
    ``skip``.
    """

    action: str
    value: Any
    message: str = ""


def is_receiver_primary(
    diff: FieldDiff,
    *,
    receiver_primary_active: bool,
    tos_available: bool,
) -> bool:
    """Whether this field's receiver value may be auto-pushed to TOS.

    Every clause matters:

    * ``receiver_primary_active`` — the operator has not asked to be consulted
      (``--interactive``) or opted out (``--no-receiver-primary``), AND the
      position sanity check passed. That last part is decided by the caller,
      because it depends on the whole diff set rather than this field.
    * ``spec.receiver_primary`` — the field is receiver-authoritative at all.
    * ``receiver_value is not None`` — we actually probed something.
    * ``spec.tos_writable`` — TOS will accept a write for this field. Notably
      ``rinex_marker_number`` is deliberately NOT writable: cfg follows TOS, and
      pushing cfg's value up would re-introduce the wrong marker.
    * ``tos_available`` — we queried TOS; without it there is nothing to push to.
    """
    return bool(
        receiver_primary_active
        and diff.spec.receiver_primary
        and diff.receiver_value is not None
        and diff.spec.tos_writable
        and tos_available
    )


def decide_field(
    diff: FieldDiff,
    policy: ReconcilePolicy,
    *,
    receiver_primary_active: bool,
    tos_available: bool,
) -> Optional[FieldDecision]:
    """Decide what to do about one field, or return ``None`` to ask a human.

    Rule order is significant and preserved from the original loop:

    1. ``--auto-fill`` fills a MISSING value from an agreed suggestion.
    2. ``--yes`` accepts any suggestion.
    3. ``--yes`` takes the receiver value for a receiver-primary field even with
       no agreed suggestion.
    4. JSON mode cannot prompt, so it skips.
    5. Otherwise: ask.

    Returning ``None`` rather than an "ask" action is deliberate — a decision
    and the absence of one are different things, and a caller that forgets to
    handle the absence gets a ``None`` it must deal with rather than a silently
    plausible action.
    """
    primary = is_receiver_primary(
        diff,
        receiver_primary_active=receiver_primary_active,
        tos_available=tos_available,
    )
    pushable = primary and diff.suggestion_source in _PUSHABLE_SOURCES
    suffix = " (cfg + TOS)" if pushable else ""
    act = "set_and_push_tos" if pushable else "set"

    if (
        policy.auto_fill
        and diff.verdict == Verdict.MISSING
        and diff.suggestion is not None
    ):
        return FieldDecision(
            act,
            diff.suggestion,
            f"auto-fill from {diff.suggestion_source}: {diff.suggestion!r}{suffix}",
        )

    if policy.yes and diff.suggestion is not None:
        return FieldDecision(
            act,
            diff.suggestion,
            f"accept suggestion ({diff.suggestion_source}): {diff.suggestion!r}{suffix}",
        )

    if policy.yes and primary and diff.receiver_value is not None:
        # --yes with a receiver-primary field but no agreed suggestion: still
        # take the receiver, because the receiver is authoritative for it.
        return FieldDecision(
            "set_and_push_tos",
            diff.receiver_value,
            f"accept receiver (primary): {diff.receiver_value!r} (cfg + TOS)",
        )

    if policy.silent:
        # JSON mode without an applicable auto-rule: cannot prompt, so skip.
        return FieldDecision("skip", None)

    return None
