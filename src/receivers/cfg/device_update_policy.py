"""Why a device attribute is being written — as data, not as a Namespace.

`cfg update-device` writes a TOS device attribute for one of two reasons, and
they corrupt the temporal record in **opposite** directions if confused:

* ``--change`` — the value genuinely changed in the world (firmware upgrade,
  marker rename). Closes the open attribute period and opens a new one, so TOS
  remembers "ran 5.6.0 until 2026-05-30, 5.7.0 from then on". Pattern 2.
* ``--correct`` — the recorded value was simply **wrong** and the real world
  never changed. Overwrites the open value in place, keeping its dates.
  Pattern 1.

Using ``--correct`` for a real upgrade **erases** that upgrade from history.
Using ``--change`` for a typo **invents** an upgrade that never happened.

**So there is no default, and absence is an error.** The CLI enforces
exactly-one through an argparse mutually-exclusive group with
``required=True`` — but that guard lives in the parser, not in the logic, and
the logic read ``in_place = bool(args.correct)``. A non-terminal caller — the
planned rek_new web UI, fabricating a Namespace — that set neither flag would
silently get ``in_place=False`` and a Pattern 2 transition. That is the same
shape as the ``dry_run`` fallback documented in
:mod:`receivers.cfg.reconcile_policy`, and it is closed the same way: make the
absence impossible to express.

A ``--correct`` also does **not** file a vitjun. Fixing a record is not a field
event, and logging one would fabricate a site visit — the same class of damage
as picking the wrong pattern, so it is derived here rather than re-tested at
the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional


class WriteIntent(StrEnum):
    """Why the attribute is being written. No default — see the module docstring."""

    #: The world changed. Close the open period, open a new one (Pattern 2).
    CHANGE = "change"
    #: The record was wrong. Overwrite in place, keep the dates (Pattern 1).
    CORRECT = "correct"


class IntentNotDeclaredError(ValueError):
    """Raised when neither --change nor --correct was declared.

    Deliberately loud. The alternative — picking one — writes to production TOS
    on a guess, and the two guesses damage the record in opposite directions.
    """


@dataclass(frozen=True)
class DeviceUpdatePolicy:
    """What one ``cfg update-device`` run is allowed to do."""

    # No default. Absence is an error, not a silent Pattern 2.
    intent: WriteIntent

    dry_run: bool = True
    no_vitjun: bool = False
    visit_type: str = "remote"
    #: Raw operator value; resolved to "today" at use, never at construction,
    #: so the frozen object is not time-dependent.
    date: Optional[str] = None

    @property
    def in_place(self) -> bool:
        """Pattern 1 (upsert, no history) rather than Pattern 2 (transition)."""
        return self.intent is WriteIntent.CORRECT

    @property
    def mode_label(self) -> str:
        """The line an operator reads back before confirming a write."""
        return (
            "--correct → Pattern 1 (in-place upsert, no history)"
            if self.in_place
            else "--change → Pattern 2 (transition, records history)"
        )

    @property
    def visit_label(self) -> str:
        return "Fjarvitjun" if self.visit_type == "remote" else "Staðarvitjun"

    def wants_vitjun(self, *, anything_changed: bool) -> bool:
        """Whether to file a maintenance visit.

        Only for a real ``--change`` that actually wrote something, and only
        when the operator did not opt out. A ``--correct`` never files one.
        """
        return (not self.in_place) and (not self.no_vitjun) and bool(anything_changed)

    @classmethod
    def from_args(cls, args: Any) -> DeviceUpdatePolicy:
        """Build from a CLI namespace, refusing an undeclared intent.

        ``argparse`` already makes this unreachable from the terminal. The
        check is here for every OTHER caller, which is the whole point.
        """
        change = bool(getattr(args, "change", False))
        correct = bool(getattr(args, "correct", False))
        if change == correct:
            raise IntentNotDeclaredError(
                "exactly one of --change / --correct is required. "
                "--change records a real-world change as a new attribute period; "
                "--correct fixes a wrong record in place. Guessing corrupts the "
                "TOS history in opposite directions, so there is no default."
            )
        return cls(
            intent=WriteIntent.CORRECT if correct else WriteIntent.CHANGE,
            # NOTE the polarity: this verb's parser defines `--no-dry-run`,
            # not `--dry-run`, so the namespace attribute is `no_dry_run` and
            # the safe reading of ABSENCE is "still a dry run".
            dry_run=not bool(getattr(args, "no_dry_run", False)),
            no_vitjun=bool(getattr(args, "no_vitjun", False)),
            visit_type=getattr(args, "visit_type", "remote") or "remote",
            date=getattr(args, "date", None),
        )
