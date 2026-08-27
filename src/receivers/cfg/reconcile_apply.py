"""Performing a reconcile decision — the write half.

:mod:`~receivers.cfg.reconcile_plan` decides *what* to do about a field.
This module *does* it: writes ``stations.cfg``, pushes to TOS, and reports what
happened. Together they are the plan/apply pair the architecture review asked
for, and the reason the planned rek_new web UI can reuse this logic — the whole
thing used to be interleaved with ~40 ``print()`` calls inside a 566-line CLI
function.

Three deliberate design choices, each load-bearing:

**No presentation.** Operator text goes through the injected ``emit`` callable,
exactly the way ``progress`` is injected into :mod:`receivers.cfg.probe`. The
messages themselves still live here, because the wording is part of what an
apply *did* — operators diff it against runbooks — and splitting the two is how
they drift apart.

**Nothing is optional that must be decided.** ``emit`` and the cfg writers have
no defaults. A silent default would let a caller lose all operator output on a
LIVE run, which is worse than a crash; a defaulted writer would let a caller
think it had substituted one when it had not. ``dry_run`` reaches here only via
:class:`~receivers.cfg.reconcile_policy.ReconcilePolicy`, where it is required
with no default — see that module's docstring for why unifying its two former
fallbacks would authorise writes to production TOS.

**``effective_date`` stays a raw ``Optional[str]``** and is resolved to "now"
only at the moment of a push. Resolving it earlier would make an otherwise pure
value time-dependent, and any golden covering a push non-deterministic. It is
**required** — ``None`` must be said out loud. Defaulting it would let a caller
silently date a *historical* correction at "now", which opens a TOS attribute
period on the wrong day; that is the damage class recorded in the station.info
era-boundary notes, not a cosmetic slip. ``no_transition`` is required for the
same reason: omitted, it silently enables Pattern 2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional

from .field_manifest import FieldSpec
from .reconcile_policy import ReconcilePolicy
from .reconciler import FieldDiff, SourceUnavailableError

logger = logging.getLogger(__name__)

#: Where operator-facing text goes. ``print`` for a terminal; a sink for JSON
#: mode; a list append for a web UI.
Emit = Callable[[str], None]


def silent_emit(_message: str) -> None:
    """An ``emit`` that discards — for JSON mode, where chatter breaks the document."""


@dataclass(frozen=True)
class ApplyOutcome:
    """What one applied decision did.

    The counters mirror what ``_reconcile_one`` returns to its caller, which
    prints a fleet-wide summary from them. Note what is NOT counted: a TOS push
    is not a cfg mutation, so it never increments ``written``.
    """

    written: int = 0
    skipped: int = 0
    #: ``quit`` — abandon this station's remaining fields, do not fall through.
    stop: bool = False


def resolve_effective_date(effective_date: Optional[str]) -> str:
    """Return an ISO-8601 ``date_from`` for TOS attribute writes.

    Falls back to current UTC when the operator did not say — correct for
    serial/firmware corrections, where the actual change date is known only
    approximately.
    """
    if effective_date:
        return effective_date
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    logger.debug("--effective-date not set; defaulting to now (%s)", now)
    return now


class CfgTargets:
    """Every ``stations.cfg`` this run writes to, written in lockstep.

    ``None`` in ``targets`` means "the deployed local config" — ``apply_diff``
    and ``remove_diff`` resolve it themselves. With ``--global`` the
    gps-config-data repo's copy is appended, so one run syncs the deployed
    config and the source of truth together.

    ``apply_diff``/``remove_diff`` are **required injected** parameters rather
    than module-level imports. That is not ceremony: the CLI has a second
    ``apply_diff`` call site in the ``--global`` sync verb, and tests stub the
    writer by patching ``receivers.cli.cfg.apply_diff``. Importing it here
    instead would silently detach that patch from THIS module's calls — the
    same hazard review item 9 flagged — and a stubbed test would start writing
    real config files.
    """

    def __init__(
        self,
        station_id: str,
        targets: List[Optional[Any]],
        *,
        apply_diff: Callable[..., bool],
        remove_diff: Callable[..., bool],
    ) -> None:
        self.station_id = station_id
        self.targets = targets
        self._apply = apply_diff
        self._remove = remove_diff

    def apply(self, diff: FieldDiff, value: str, **kwargs: Any) -> bool:
        """Write *value* to every target; True if any file actually changed."""
        changed = False
        for target in self.targets:
            if self._apply(self.station_id, diff, value, cfg_path=target, **kwargs):
                changed = True
        return changed

    def remove(self, diff: FieldDiff, **kwargs: Any) -> bool:
        """Remove the key from every target; True if any file actually changed."""
        changed = False
        for target in self.targets:
            if self._remove(self.station_id, diff, cfg_path=target, **kwargs):
                changed = True
        return changed


def push_field_value(
    *,
    station_id: str,
    diff: FieldDiff,
    value: str,
    tos_data: Optional[Dict[str, Any]],
    dry_run: bool,
    no_transition: bool,
    effective_date: Optional[str],
    emit: Emit,
) -> None:
    """Push *value* for ``diff.spec`` to TOS; handles errors and dry-run.

    Routes to Pattern 2 (``transition_attribute_value``) when the TOS value
    differs from the new value — a *change*, which must open a new period
    rather than overwrite history — unless ``--no-transition`` says otherwise.
    Pattern 1 (``upsert_attribute_value``) otherwise.

    Best-effort by design: a failed push is reported and logged, never raised,
    because the cfg write it follows has already succeeded.
    """
    if tos_data is None:
        emit(f"     ❌ cannot push to TOS: no TOS data for {station_id}")
        return

    try:
        from tostools.api.tos_writer import TOSWriter

        from .tos_push import push_field_to_tos, push_field_transition_to_tos
    except ImportError as exc:
        emit(f"     ❌ tostools not available: {exc}")
        return

    writer = TOSWriter(dry_run=dry_run)
    date_from = resolve_effective_date(effective_date)

    use_transition = (
        not no_transition and diff.tos_value is not None and diff.tos_value != value
    )

    mode = "[DRY-RUN] " if dry_run else ""
    pattern = "Pattern 2 (transition)" if use_transition else "Pattern 1 (upsert)"
    emit(
        f"     {mode}→ push to TOS [{pattern}]: {diff.cfg_key} = {value!r} "
        f"(attr={diff.spec.tos_attribute_code!r}, "
        f"entity={diff.spec.tos_target_entity}, date_from={date_from})"
    )

    try:
        if use_transition:
            result = push_field_transition_to_tos(
                writer=writer,
                spec=diff.spec,
                new_value=value,
                old_value=str(diff.tos_value),
                tos_data=tos_data,
                transition_date=date_from,
            )
            if hasattr(result, "method"):  # DryRunResult
                emit(f"     ✅ [dry-run] would transition {diff.cfg_key}")
            elif isinstance(result, dict):
                closed = "closed" if result.get("closed") else "no prior period"
                emit(
                    f"     ✅ TOS transition: {diff.cfg_key} "
                    f"({diff.tos_value!r} → {value!r}, {closed})"
                )
        else:
            result = push_field_to_tos(
                writer=writer,
                spec=diff.spec,
                value=value,
                tos_data=tos_data,
                date_from=date_from,
            )
            if hasattr(result, "method"):  # DryRunResult
                emit(f"     ✅ [dry-run] would {result.method} {result.endpoint}")
            else:
                emit(f"     ✅ TOS updated: {diff.cfg_key} = {value!r}")
    except Exception as exc:  # noqa: BLE001
        emit(f"     ❌ TOS push failed: {exc}")
        logger.warning("[%s] TOS push failed for %s: %s", station_id, diff.cfg_key, exc)


def push_component_value(
    *,
    station_id: str,
    component: Dict[str, str],
    tos_data: Optional[Dict[str, Any]],
    dry_run: bool,
    effective_date: Optional[str],
    emit: Emit,
) -> None:
    """Push one sub-component of a composite field (e.g. the antenna ARP) to TOS.

    Composite cfg values — ``antenna_height`` is the ARP plus the monument
    height — have no single TOS attribute to write, so the operator edits one
    component at a time. Deliberately does NOT route through
    :func:`push_field_value`: it targets an explicit ``(entity, attribute_code)``
    rather than the field spec's.
    """
    entity = component["entity"]
    attribute_code = component["attribute_code"]
    value = component["value"]

    mode = "[DRY-RUN] " if dry_run else ""
    emit(f"     {mode}→ push component to TOS: {entity}.{attribute_code} = {value!r}")

    if tos_data is None:
        emit("     ❌ no TOS data — cannot push component")
        return

    try:
        # Imported inside the function on purpose: Wave 1 item 11 made tostools
        # lazy for a 21-33x import speedup, and a module-level import here would
        # pull pandas/numpy/requests/pyproj back in on every `receivers.cfg`
        # import.
        from tostools.api.tos_writer import TOSWriter

        from .tos_push import push_component_to_tos

        writer = TOSWriter(dry_run=dry_run)
        result = push_component_to_tos(
            writer=writer,
            entity=entity,
            attribute_code=attribute_code,
            value=value,
            tos_data=tos_data,
            date_from=resolve_effective_date(effective_date),
        )
        if hasattr(result, "method"):
            emit(f"     ✅ [dry-run] would {result.method} {result.endpoint}")
        else:
            emit(f"     ✅ TOS updated: {entity}.{attribute_code} = {value!r}")
    except Exception as exc:  # noqa: BLE001
        emit(f"     ❌ component push failed: {exc}")
        logger.warning(
            "[%s] component push failed for %s.%s: %s",
            station_id,
            entity,
            attribute_code,
            exc,
        )


def _normalise_for_cfg(
    diff: FieldDiff,
    value: str,
    spec: Optional[FieldSpec],
    emit: Emit,
) -> Optional[str]:
    """Map *value* into cfg vocabulary (TOS ``SEPT POLARX5`` → cfg ``PolaRX5``).

    Returns ``None`` when the value must not be written — either ``cfg_format``
    raised, or it normalised the value away entirely. Both are reported and
    both mean "skip this field", never "write something else".
    """
    if spec is None:
        return value
    try:
        mapped = spec.cfg_format(value)
    except Exception as exc:  # noqa: BLE001
        emit(f"     ❌ cfg_format failed for {diff.cfg_key}: {exc}")
        return None
    if mapped is None:
        emit(f"     ❌ cfg_format normalised {value!r} to None — skipping")
        return None
    if mapped != value:
        emit(f"     ↺ normalised {value!r} → {mapped!r} for cfg vocabulary")
    return mapped


@dataclass(frozen=True)
class _CfgWriteWording:
    """The four messages one cfg write can produce.

    ``set`` and ``set_and_push_tos`` do the same thing and say it DIFFERENTLY —
    "wrote X = Y" vs "wrote X = Y to cfg", "unchanged" vs "cfg unchanged",
    "could not write" vs "could not write cfg". Carried as data rather than
    normalised away: operators diff this output against runbooks, so unifying
    the wording would be a silent behaviour change, not a cleanup.
    """

    wrote_suffix: str
    unchanged: str
    could_not_write: str
    write_failed: str


_SET_WORDING = _CfgWriteWording(
    wrote_suffix="",
    unchanged="unchanged",
    could_not_write="could not write",
    write_failed="write failed",
)
_SET_AND_PUSH_WORDING = _CfgWriteWording(
    wrote_suffix=" to cfg",
    unchanged="cfg unchanged",
    could_not_write="could not write cfg",
    write_failed="cfg write failed",
)


def _write_cfg(
    diff: FieldDiff,
    value: str,
    targets: CfgTargets,
    emit: Emit,
    wording: _CfgWriteWording,
) -> Optional[bool]:
    """Write one field to cfg. ``None`` means the write failed and was reported."""
    try:
        changed = targets.apply(diff, value)
    except SourceUnavailableError as exc:
        emit(f"     ❌ {wording.could_not_write}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        emit(f"     ❌ {wording.write_failed}: {exc}")
        return None
    if changed:
        emit(f"     ✅ wrote {diff.cfg_key} = {value!r}{wording.wrote_suffix}")
    else:
        emit(f"     ⏭  {wording.unchanged} ({diff.cfg_key} already = {value!r})")
    return changed


def canonicalize_notation(
    diffs: List[FieldDiff],
    *,
    targets: CfgTargets,
    dry_run: bool,
    emit: Emit,
) -> int:
    """Rewrite notation-only mismatches to the receiver's spelling.

    ``--canonicalize`` handles fields where cfg is logically CORRECT but spelled
    differently from what the receiver reports ("NP 4.81 / SP 4.81" vs "4.81").
    Nothing is being decided here — the values already agree once normalised —
    which is why this needs no prompt and no consent gate beyond ``dry_run``.

    Deliberately does NOT route through :func:`apply_decision`: ``set`` runs
    ``cfg_format``, and the whole point is to write the receiver's RAW spelling.
    It also reports per field rather than per decision, and the wording differs.

    Returns the number of cfg files actually changed.
    """
    written = 0
    for diff in diffs:
        raw = diff.receiver_value  # guaranteed non-None by format_mismatch
        assert raw is not None
        if dry_run:
            emit(f"     ≈ {diff.cfg_key}: {diff.cfg_raw!r} → {raw!r} (dry-run)")
            continue
        try:
            changed = targets.apply(diff, raw, resolved_by="canonicalize")
        except SourceUnavailableError as exc:
            emit(f"     ❌ {diff.cfg_key}: could not write: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            emit(f"     ❌ {diff.cfg_key}: write failed: {exc}")
            continue
        if changed:
            written += 1
            emit(f"     ✅ {diff.cfg_key}: {diff.cfg_raw!r} → {raw!r}")
        else:
            emit(f"     ⏭  {diff.cfg_key} already canonical")
    return written


def remove_placeholders(
    diffs: List[FieldDiff],
    *,
    targets: CfgTargets,
    dry_run: bool,
    emit: Emit,
) -> int:
    """Drop cfg keys whose value is a recognised placeholder.

    A placeholder is a raw value ``normalize()`` strips to ``None`` — typically
    a TOS synthetic device identifier (``antenna-AFST-20210527``) that leaked
    into cfg. The key should be REMOVED rather than kept or written to, because
    a value that normalises away is worse than an absent one: it looks like
    data.

    This is the unattended ``--canonicalize`` path. The interactive path asks
    per key and still lives in the CLI, because it is a prompt, not a write —
    and its error handling is deliberately broader (see the note there).

    Returns the number of cfg files actually changed.
    """
    written = 0
    for diff in diffs:
        if dry_run:
            emit(f"     ~ {diff.cfg_key}: remove {diff.cfg_raw!r} (dry-run)")
            continue
        try:
            changed = targets.remove(diff, resolved_by="canonicalize")
        except SourceUnavailableError as exc:
            emit(f"     ❌ {diff.cfg_key}: could not remove: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            emit(f"     ❌ {diff.cfg_key}: removal failed: {exc}")
            continue
        if changed:
            written += 1
            emit(f"     ✅ {diff.cfg_key}: removed {diff.cfg_raw!r}")
        else:
            emit(f"     ⏭  {diff.cfg_key} already absent")
    return written


def apply_decision(
    action: str,
    value: Any,
    diff: FieldDiff,
    *,
    targets: CfgTargets,
    policy: ReconcilePolicy,
    tos_data: Optional[Dict[str, Any]],
    field_specs_by_key: Dict[str, FieldSpec],
    emit: Emit,
    station_id: str,
    no_transition: bool,
    effective_date: Optional[str],
) -> ApplyOutcome:
    """Carry out one decision about one field.

    ``action`` is the vocabulary the prompt and :func:`decide_field` already
    share: ``set``, ``set_and_push_tos``, ``push_tos``, ``push_cfg_to_tos``,
    ``push_component``, ``skip``, ``quit``.

    An unrecognised action, or an action whose ``value`` is ``None`` where one
    is required, is a no-op — matching the original fall-through. It is NOT
    counted as a skip, because the operator did not choose to skip.
    """
    if action == "quit":
        return ApplyOutcome(stop=True)

    if action == "skip":
        return ApplyOutcome(skipped=1)

    if action == "push_tos" and value is not None:
        push_field_value(
            station_id=station_id,
            diff=diff,
            value=value,
            tos_data=tos_data,
            dry_run=policy.dry_run,
            no_transition=no_transition,
            effective_date=effective_date,
            emit=emit,
        )
        return ApplyOutcome()

    if action == "push_cfg_to_tos" and value is not None:
        emit(f"     → push cfg value to TOS: {diff.cfg_key} = {value!r}")
        push_field_value(
            station_id=station_id,
            diff=diff,
            value=value,
            tos_data=tos_data,
            dry_run=policy.dry_run,
            no_transition=no_transition,
            effective_date=effective_date,
            emit=emit,
        )
        return ApplyOutcome()

    if action == "push_component" and isinstance(value, dict):
        push_component_value(
            station_id=station_id,
            component=value,
            tos_data=tos_data,
            dry_run=policy.dry_run,
            effective_date=effective_date,
            emit=emit,
        )
        return ApplyOutcome()

    if action == "set_and_push_tos" and value is not None:
        mapped = _normalise_for_cfg(
            diff, value, field_specs_by_key.get(diff.cfg_key), emit
        )
        if mapped is None:
            return ApplyOutcome()
        changed = _write_cfg(diff, mapped, targets, emit, _SET_AND_PUSH_WORDING)
        if changed is None:  # cfg write failed — do NOT push a value cfg rejected
            return ApplyOutcome()
        # The push happens even when cfg was already correct: an unchanged cfg
        # still means TOS may be stale. And the RECEIVER value wins over the
        # typed one — an `or`, not a None-check — so TOS records what the
        # hardware actually reports even if the operator edited cfg by hand.
        push_field_value(
            station_id=station_id,
            diff=diff,
            value=diff.receiver_value or mapped,
            tos_data=tos_data,
            dry_run=policy.dry_run,
            no_transition=no_transition,
            effective_date=effective_date,
            emit=emit,
        )
        return ApplyOutcome(written=1 if changed else 0)

    if action == "set" and value is not None:
        mapped = _normalise_for_cfg(
            diff, value, field_specs_by_key.get(diff.cfg_key), emit
        )
        if mapped is None:
            return ApplyOutcome()
        changed = _write_cfg(diff, mapped, targets, emit, _SET_WORDING)
        return ApplyOutcome(written=1 if changed else 0)

    return ApplyOutcome()
