"""What a reconcile run is allowed to do — as data, not as an argparse Namespace.

``_reconcile_one`` read fifteen distinct decision inputs straight off ``args``,
mostly through ``getattr(args, "...", default)``. Two consequences:

* A non-terminal caller — the planned rek_new web UI — had to fabricate an
  ``argparse.Namespace`` carrying the right attribute superset to express
  intent, and a misspelling silently became a default instead of an error.
* ``dry_run`` was read with **two different fallbacks**:
  ``getattr(args, "dry_run", False)`` where it feeds consent
  (``consent_given = yes or dry_run``), and ``getattr(args, "dry_run", True)``
  where it constructs the ``TOSWriter``.

That second point looks like drift and is not: each fallback is the
*conservative* choice for its own site. Absent ``dry_run``, the consent sites
must not treat it as consent (so ``False``), and the writer sites must not
build a live writer (so ``True``). Unifying them onto one value would make one
side unsafe, which is exactly the kind of "cleanup" that writes to production
TOS.

So ``dry_run`` here is **required, with no default**. Making absence impossible
retires both fallbacks without having to pick a winner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReconcilePolicy:
    """Immutable description of one reconcile run's intent.

    Frozen because a policy is a decision already taken; mutating it mid-run is
    how ``args``-as-state produced surprises.
    """

    # No default. See the module docstring — this is load-bearing.
    dry_run: bool

    yes: bool = False
    auto_fill: bool = False
    push_tos: bool = False
    sync_devices: bool = False
    only_diffs: bool = False
    json: bool = False
    open_only: bool = False
    canonicalize: bool = False
    no_receiver_primary: bool = False
    # --interactive ALSO suppresses receiver-primary auto-push: an operator
    # who asked to be consulted per field must not have values pushed for
    # them. Folded into receiver_primary_active below rather than left as a
    # separate read, which is how it got missed the first time.
    interactive: bool = False
    position_tolerance_m: float = 2.0
    position_abort_m: float = 50.0

    @property
    def consent_given(self) -> bool:
        """Whether live writes are authorised.

        Interactive mode without ``--yes`` or ``--dry-run`` is deliberately NOT
        consent: it means "show me the table and ask again", not "write to TOS
        for every actionable field". That rule exists because of a real
        incident; do not relax it.
        """
        return bool(self.yes) or bool(self.dry_run)

    @property
    def silent(self) -> bool:
        """JSON mode suppresses progress chatter so the document stays valid."""
        return bool(self.json)

    @property
    def show_ok(self) -> bool:
        """Whether to render fields that already agree."""
        return not (self.only_diffs or self.open_only)

    @property
    def receiver_primary_active(self) -> bool:
        """Whether a receiver-authoritative value may be pushed without asking.

        Suppressed by --no-receiver-primary AND by --interactive. Note the
        caller ANDs this with "no position warning": a position sanity failure
        disables auto-push regardless of flags, and that gate stays at the call
        site because it depends on the diffs, not on the policy.
        """
        return not (self.no_receiver_primary or self.interactive)

    @classmethod
    def from_args(cls, args: Any) -> ReconcilePolicy:
        """Build a policy from a CLI namespace, resolving every fallback once.

        The ``getattr`` defaults here are the ones the call sites used, kept so
        this is a pure re-expression of existing behaviour. ``dry_run`` is read
        without a fallback chain because the CLI always sets it; if a caller
        somehow omits it, failing loudly beats silently picking a side.
        """
        if not hasattr(args, "dry_run"):
            raise AttributeError(
                "ReconcilePolicy.from_args: 'dry_run' is required. It used to be "
                "read with two different fallbacks (False at the consent sites, "
                "True at the TOSWriter sites) because each is the conservative "
                "choice for its own context. Rather than pick a winner, absence "
                "is now an error — say what you mean."
            )
        return cls(
            dry_run=bool(args.dry_run),
            yes=bool(getattr(args, "yes", False)),
            auto_fill=bool(getattr(args, "auto_fill", False)),
            push_tos=bool(getattr(args, "push_tos", False)),
            sync_devices=bool(getattr(args, "sync_devices", False)),
            only_diffs=bool(getattr(args, "only_diffs", False)),
            json=bool(getattr(args, "json", False)),
            open_only=bool(getattr(args, "open", False)),
            canonicalize=bool(getattr(args, "canonicalize", False)),
            no_receiver_primary=bool(getattr(args, "no_receiver_primary", False)),
            interactive=bool(getattr(args, "interactive", False)),
            position_tolerance_m=float(
                getattr(args, "position_tolerance_m", 2.0) or 0.0
            ),
            position_abort_m=float(getattr(args, "position_abort_m", 50.0)),
        )
