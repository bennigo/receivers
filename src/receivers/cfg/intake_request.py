"""What one warehouse-intake run is asking for — as data, not as a Namespace.

The second worked example of the pattern
:class:`~receivers.cfg.reconcile_policy.ReconcilePolicy` established, applied to
the verb the architecture review singled out (§4.2). ``cmd_cfg_add_receiver``
resolved three business defaults by **mutating ``args`` in place** —
``args.owner = "Jarðeðlismælihópur"``, the B9 warehouse string, today's date —
so the intake policy was expressible only by fabricating an
``argparse.Namespace``, and a misspelled attribute five levels down silently
became its default instead of failing.

**The precedence is the logic.** Three sources, resolved in one place:

    CLI argument  >  --from-file value  >  built-in default

Each of the three was previously a separate ``if not getattr(args, key, None)``
pass over the same names, at three different points in a 458-line function.

Two rules that look like details and are not:

* **The gates test FALSINESS, not ``None``.** ``--owner ""`` takes the default.
  An ``Optional[str]`` field with an ``is None`` check would carry the empty
  string through to a TOS write that then fails validation. Pinned by
  ``test_empty_string_owner_falls_back_to_the_default``.
* **``date_start`` is NOT resolved at construction.** ``date.today()`` inside a
  frozen value object makes it time-dependent and any test covering it
  non-deterministic — the same argument that keeps ``effective_date`` a raw
  ``Optional[str]`` in :mod:`receivers.cfg.reconcile_apply`. The caller passes
  today in, or asks for it explicitly via :meth:`IntakeRequest.resolved`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Dict, Mapping, Optional

#: Fields ``--from-file`` may supply when the CLI did not. Order is the file's
#: own; it does not affect precedence.
FILE_FILLABLE = (
    "owner",
    "location",
    "date_start",
    "station_hint",
    "firmware",
    "comment",
    "galvos",
    "probe_type",
)

#: Jarðeðlismælihópur owns the GPS receiver fleet for IMO. Every existing open
#: child of B9 - Kjallari - Jörð (id_entity=4) carries this owner, so it is the
#: right default for any new warehouse intake.
DEFAULT_OWNER = "Jarðeðlismælihópur"

#: ~71% of historical intakes land at the main GPS warehouse.
DEFAULT_LOCATION = "B9 - Kjallari - Jörð"

#: Required on the device record; an intake missing any of these cannot be written.
REQUIRED_FIELDS = ("owner", "location", "date_start")


def _supplied(value: Any) -> bool:
    """Whether a value counts as given.

    Falsiness, deliberately — see the module docstring. ``""`` is "not
    supplied", which is what an operator typing ``--owner ""`` means.
    """
    return bool(value)


@dataclass(frozen=True)
class IntakeRequest:
    """One resolved intake, with every source already merged.

    Frozen: the precedence is decided once, at the edge. Mutating it mid-run is
    how ``args``-as-state produced defaults that appeared out of nowhere five
    frames down.
    """

    owner: Optional[str] = None
    location: Optional[str] = None
    date_start: Optional[str] = None
    station_hint: Optional[str] = None
    firmware: Optional[str] = None
    comment: Optional[str] = None
    galvos: Optional[str] = None
    probe_type: Optional[str] = None

    @classmethod
    def from_args(cls, args: Any) -> IntakeRequest:
        """Read the CLI layer only. No file, no defaults — those come after."""
        return cls(**{f: getattr(args, f, None) for f in FILE_FILLABLE})

    def merged_with_file(self, data: Mapping[str, Any]) -> IntakeRequest:
        """Fill in from ``--from-file`` ONLY what the CLI did not supply.

        The file never overrides an explicit argument. ``None`` and ``""`` in
        the file are both ignored, matching the original ``not in (None, "")``.
        """
        updates: Dict[str, Any] = {}
        for field in FILE_FILLABLE:
            if _supplied(getattr(self, field)):
                continue
            file_val = data.get(field)
            if file_val not in (None, ""):
                updates[field] = file_val
        return replace(self, **updates) if updates else self

    def with_defaults(self, *, today: Optional[str] = None) -> IntakeRequest:
        """Apply the built-in defaults to whatever is still unsupplied.

        ``today`` is a parameter rather than a ``date.today()`` call inside a
        frozen object; omit it and it is resolved here, at use, which is the
        only place it is allowed to be non-deterministic.
        """
        updates: Dict[str, Any] = {}
        if not _supplied(self.owner):
            updates["owner"] = DEFAULT_OWNER
        if not _supplied(self.location):
            updates["location"] = DEFAULT_LOCATION
        if not _supplied(self.date_start):
            updates["date_start"] = today or date.today().isoformat()
        return replace(self, **updates) if updates else self

    @classmethod
    def resolved(
        cls,
        args: Any,
        file_data: Optional[Mapping[str, Any]] = None,
        *,
        today: Optional[str] = None,
    ) -> IntakeRequest:
        """The whole precedence chain in one call: CLI > file > default."""
        req = cls.from_args(args)
        if file_data:
            req = req.merged_with_file(file_data)
        return req.with_defaults(today=today)

    def missing_required(self) -> list[str]:
        """Required fields still unsupplied AFTER defaults.

        Can only fire if a default is itself empty — which is the original
        behaviour and is deliberately preserved. Checking before defaults would
        start rejecting valid invocations.
        """
        return [f for f in REQUIRED_FIELDS if not _supplied(getattr(self, f))]
