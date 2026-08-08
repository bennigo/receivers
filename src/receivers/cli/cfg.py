"""``receivers cfg`` — three-way reconciliation CLI.

Compares values in ``stations.cfg`` against:

* the live receiver (via the health pipeline)
* TOS (via :mod:`tostools`)

The intended workflow is **TOS → cfg**: TOS is authoritative; the live
receiver is a validation source that flags issues for human review. The
silent auto-write that ``receivers health`` used to perform has been
removed in favour of this explicit, reviewable workflow.

Subcommands:

* ``reconcile`` — show diffs and (optionally) write fixes to stations.cfg
* ``list``      — show currently-open discrepancies (queryable audit log)
* ``history``   — show the full detection/resolution history for a station or field
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, cast

from ..cfg.field_manifest import (
    FIELDS,
    all_keys,
    fields_by_key,
    with_position_tolerance,
)
from ..cfg.reconciler import (
    FieldDiff,
    SourceUnavailableError,
    Verdict,
    apply_diff,
    compare_station,
    remove_diff,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# --global: write the gps-config-data repo (source of truth) + git commit
# ---------------------------------------------------------------------------


def _resolve_warehouse_arg(value):
    """Map the ``--warehouse`` arg to a warehouse name, or ``None`` for "don't".

    ``nargs="?" const=""`` gives three distinguishable states: flag absent
    (``None`` → leave the device parentless), bare ``--warehouse`` (``""`` →
    the fleet default, so nobody has to retype "B9 - Kjallari - Jörð" with its
    accents), or an explicit name.
    """
    if value is None:
        return None
    if value == "":
        from ..cfg.operations import DEFAULT_WAREHOUSE

        return DEFAULT_WAREHOUSE
    return value


def _add_list_models_flag(parser: argparse.ArgumentParser, *, what: str) -> None:
    """Add ``--list-models`` to a verb that validates an IGS equipment name.

    ``ANTENNA_IGS`` / ``RADOME_IGS`` are a hand-maintained subset of IGS
    ``rcvr_ant.tab`` covering what the fleet actually runs, so "is my model
    accepted?" is a real question with no other offline answer — the verbs
    resolve the station against TOS *before* validating the model, so you
    cannot even provoke the error message without network and credentials.
    """
    parser.add_argument(
        "--list-models",
        dest="list_models",
        action="store_true",
        help=(
            f"Print the accepted IGS {what} names and exit. Offline — no TOS "
            f"call, no other flags needed. An unlisted model means adding it "
            f"to tostools/standards/igs_equipment.py, not a different flag."
        ),
    )


def _print_igs_models(*, antennas: bool = True, radomes: bool = True) -> int:
    """Print the IGS lookup tables the ``--model``/``--radome`` flags validate against.

    Reuses ``tostools.device._format_known_models`` so this output is the same
    text an operator gets in the ValueError on a bad model — one rendering, so
    the two can't drift.
    """
    from tostools.device import _format_known_models
    from tostools.standards.igs_equipment import ANTENNA_IGS, RADOME_IGS

    blocks = []
    if antennas:
        blocks.append(
            _format_known_models(
                ANTENNA_IGS, "Accepted antenna models (aliases in parentheses):"
            )
        )
    if radomes:
        blocks.append(
            _format_known_models(
                RADOME_IGS, "Accepted radome codes (aliases in parentheses):"
            )
        )
    print("\n\n".join(blocks))
    print(
        "\nNot the full IGS rcvr_ant.tab — a hand-maintained subset of what the "
        "fleet runs.\nTo add one: tostools/standards/igs_equipment.py "
        "(how SEPPOLANT_X_MF/_SF and AS-ANT3BCAL got in)."
    )
    return 0


def _require_args_or_exit(args, *pairs: tuple) -> Optional[int]:
    """Enforce flags argparse can't mark ``required`` because ``--list-models`` skips them.

    Each pair is ``(dest, flag)`` — the flag spelling is carried explicitly
    because several dests differ from their flag (``new_model`` ↔ ``--model``),
    and an error naming a flag that doesn't exist is worse than no error.
    Returns an exit code to propagate when something is missing, else ``None``.
    """
    import sys

    missing = [flag for dest, flag in pairs if not getattr(args, dest, None)]
    if not missing:
        return None
    print(
        f"❌ missing required argument(s): {', '.join(missing)}",
        file=sys.stderr,
    )
    return 2


def _add_global_flags(
    parser: argparse.ArgumentParser, *, swap_warning: bool = False
) -> None:
    """Add ``--global`` / ``--push`` to a cfg-writing subparser.

    ``--global`` retargets the cfg write from the local/deployed config to the
    gps-config-data git repo and commits it. A real (non-dry-run) ``--global``
    commit **requires ``--push``**: an unpushed local commit leaves the clone
    ahead of origin and breaks the rek-d01 config-sync ``git pull --ff-only``.
    ``--global`` is a laptop-side finalize — use ``--dry-run`` to preview without
    committing.

    ``swap_warning=True`` (for the TOS-mutating verbs) appends a caution that
    ``--global`` finalizes cfg *as part of recording the swap* — it must not be
    used to backfill an already-recorded swap (that would re-mutate TOS).
    """
    help_text = (
        "Write the gps-config-data repo (source of truth) and git commit + push "
        "(requires --push), instead of the local/deployed config. Does NOT touch "
        "the local config. Laptop-side finalize; use --dry-run to preview."
    )
    if swap_warning:
        help_text += (
            " NOTE: finalizes cfg AS PART OF recording this swap — use only when "
            "performing the swap, never to backfill an already-recorded one."
        )
    parser.add_argument(
        "--global",
        dest="global_cfg",
        action="store_true",
        help=help_text,
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Required with --global for a real commit: git push so the clone "
        "stays even with origin (the config-sync timer ff-pulls it to rek-d01). "
        "Without it, --global refuses to commit.",
    )


def _is_dry_run(args) -> bool:
    """Resolve a verb's dry-run state across the two flag conventions.

    reconcile uses ``--dry-run`` (``args.dry_run``); the write verbs default to
    dry-run and gate commits behind ``--no-dry-run`` (``args.no_dry_run``).
    """
    dry = getattr(args, "dry_run", None)
    if dry is not None:
        return bool(dry)
    return not getattr(args, "no_dry_run", False)


def _resolve_global_target(args) -> Optional[Path]:
    """Return the gps-config-data ``stations.cfg`` path when ``--global`` is set.

    Mutually exclusive with ``--cfg-path`` (``--global`` owns the path). Returns
    ``None`` when ``--global`` was not given (caller uses its normal cfg_path).

    When ``--global`` will actually commit (not a dry-run), runs the divergence
    guardrail **before** the caller writes anything — so a refusal (missing
    ``--push`` / clone not even with origin) leaves no dirty working tree.
    """
    if not getattr(args, "global_cfg", False):
        return None
    from ..cfg.global_sync import assert_committable, resolve_global_repo

    if getattr(args, "cfg_path", None):
        from ..cfg.operations import CfgOperationError

        raise CfgOperationError("--global and --cfg-path are mutually exclusive")
    repo = resolve_global_repo()
    if not _is_dry_run(args):
        assert_committable(repo, push=getattr(args, "push", False))
    return repo / "stations.cfg"


def _maybe_commit_global(args, message: str, *, changed: bool, dry_run: bool) -> None:
    """Commit the gps-config-data edit when ``--global`` wrote something.

    No-op unless ``--global`` is set. Prints a one-line summary. ``changed``
    tells us whether the verb actually edited the repo's stations.cfg this run.
    """
    if not getattr(args, "global_cfg", False):
        return
    from ..cfg.global_sync import git_commit_cfg, resolve_global_repo

    repo = resolve_global_repo()
    if dry_run:
        res = git_commit_cfg(repo, ["stations.cfg"], message, dry_run=True)
        if res.get("diff"):
            print(f"🌵 would commit to {repo.name}: {message!r}")
        else:
            print(f"   (--global) no cfg changes to commit in {repo.name}")
        return
    if not changed:
        print(f"   (--global) = no cfg changes to commit in {repo.name}")
        return
    res = git_commit_cfg(
        repo, ["stations.cfg"], message, push=getattr(args, "push", False)
    )
    if not res.get("committed"):
        print(f"   (--global) = nothing to commit ({res.get('reason', '')})")
        return
    line = f"   (--global) ✓ committed {res['commit']} in {repo.name}"
    if getattr(args, "push", False):
        line += (
            " + pushed"
            if res.get("pushed")
            else f" — PUSH FAILED: {res.get('push_error')}"
        )
    print(line)


def _progress(message: str, *, json_mode: bool, **kwargs) -> None:
    """Print progress lines to stderr in JSON mode, stdout otherwise.

    Without this, every "↳ STATION: querying TOS…" line lands on stdout
    and corrupts the JSON document the caller expects.
    """
    stream = sys.stderr if json_mode else sys.stdout
    print(message, file=stream, **kwargs)


# ---------------------------------------------------------------------------
# Source acquisition
# ---------------------------------------------------------------------------


def _load_station_configs(
    station_ids: Sequence[str], *, json_mode: bool = False
) -> Dict[str, Dict[str, Any]]:
    """Return ``{station_id: station_config}`` for the requested stations.

    ``get_station_config()`` already merges raw stations.cfg keys into
    the typed dict (via ``setdefault``), so flat fields like
    ``receiver_serial`` and ``latitude`` are readable here.

    Skip warnings go to stderr when ``json_mode`` is set so they don't
    contaminate the JSON document on stdout.
    """
    from ..config_utils import get_station_config

    configs: Dict[str, Dict[str, Any]] = {}
    for sid in station_ids:
        # silent=True: the ⚠️ skip below is the user-facing signal; the absence
        # is expected for TOS-only stations not in the local cfg.
        cfg = get_station_config(sid, silent=True)
        if cfg is None:
            _progress(
                f"⚠️  {sid}: not found in stations.cfg — skipping",
                json_mode=json_mode,
            )
            continue
        configs[sid] = cfg
    return configs


def _all_station_ids() -> List[str]:
    """Return all 4-letter uppercase station IDs from stations.cfg.

    Passive (``station_role = passive``) stations are excluded: they are
    data-source-only descriptors with no TOS/runtime counterpart, so
    reconciling them would only generate noise
    (GLOBAL_SITES_investigation.md §4.4).
    """
    from ..config_utils import is_passive_role

    try:
        import gps_parser  # type: ignore
    except ImportError:
        print("❌ gps_parser not available — cannot enumerate stations")
        return []
    cp = gps_parser.ConfigParser()
    excluded = {"DEFAULT", "DEFAULTS", "Configs", "PATHS", "FILES"}
    return sorted(
        s
        for s in cp.config.sections()
        if s not in excluded
        and s.isupper()
        and len(s) == 4
        and not is_passive_role(cp.config.get(s, "station_role", fallback=None))
    )


def _query_receiver_identity(
    station_id: str, station_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Run a receiver health probe and return the identity dict, or None.

    The probe skips file/NTRIP checks since we only need identity. Returns
    ``None`` on any failure (unreachable, auth error, parse failure).
    """
    try:
        from ..base.receiver_factory import create_receiver

        receiver = create_receiver(station_id, station_config)
        # Identity-only path: bypass NTRIP/file checks. Most extractors
        # populate receiver_identity inside get_health_status() itself.
        health = receiver.get_health_status()
        if not isinstance(health, dict):
            return None

        identity = dict(health.get("receiver_identity") or {})

        # Enrich identity with position from PVT solution — receiver coordinates
        # are reconcilable QC values, but the extractor stores them under
        # metrics.position rather than receiver_identity. Promote them here so
        # the cfg reconcile field manifest can read everything from one dict.
        position = (health.get("metrics") or {}).get("position") or {}
        for key in ("latitude", "longitude", "height"):
            val = position.get(key)
            if val is not None:
                identity[key] = val

        # Antenna metadata (type/serial/radome/height delta) is only useful
        # for cfg reconcile, so the extractor doesn't probe it during routine
        # 5-min health checks. Run the dedicated ASCII probe here. Best-effort:
        # failure leaves the antenna fields blank in the diff, which the
        # reconciler renders as NO_DATA.
        antenna_info = _query_antenna_info(station_id, station_config)
        if antenna_info:
            identity.update(antenna_info)

        if not identity:
            logger.debug("[%s] receiver returned no identity dict", station_id)
            return None
        return identity
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] receiver probe failed: %s", station_id, exc)
        return None


def _query_antenna_info(
    station_id: str, station_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Probe antenna metadata via the PolaRX5 ASCII control channel.

    Currently only PolaRX5 exposes antenna config over the control port. For
    other receiver types this is a no-op — the cfg reconcile flow falls back
    to TOS as the only authoritative source, which is correct.
    """
    try:
        receiver_type = (station_config.get("receiver_type") or "").lower()
        if "polarx" not in receiver_type:
            return None
        from ..health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        host = (
            station_config.get("router_ip")
            or station_config.get("ip_number")
            or (station_config.get("router") or {}).get("ip")
        )
        if not host:
            return None
        control_port = int(
            station_config.get("receiver_controlport")
            or station_config.get("control_port")
            or (station_config.get("receiver") or {}).get("controlport")
            or 28784
        )
        extractor = PolaRX5TCPExtractor(host, station_id, port=control_port)
        return extractor.query_antenna_info()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] antenna probe failed: %s", station_id, exc)
        return None


def _query_tos(station_id: str) -> Optional[Dict[str, Any]]:
    try:
        from tostools.api.tos_client import TOSClient
    except ImportError:
        return None
    try:
        client = TOSClient()
        data = client.get_complete_station_metadata(station_id)
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] TOS query failed: %s", station_id, exc)
        return None


# ---------------------------------------------------------------------------
# Per-station probe (parallelisable I/O — no side effects, no prompts)
# ---------------------------------------------------------------------------


def _probe_station(
    station_id: str,
    station_config: Dict[str, Any],
    sources: List[str],
    json_mode: bool,
    verbose: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Query receiver and TOS for one station.

    Pure I/O — creates its own network connections, returns data only.
    Safe to call from a thread.  When ``verbose=False`` (parallel mode)
    per-station progress lines are suppressed so interleaved output doesn't
    corrupt the terminal.

    Returns ``(receiver_identity, tos_data)``.
    """
    receiver_identity: Optional[Dict[str, Any]] = None
    tos_data: Optional[Dict[str, Any]] = None

    if "receiver" in sources:
        if station_config.get("_adhoc"):
            if verbose:
                _progress(
                    f"   ↳ {station_id}: ad-hoc config, skipping receiver probe",
                    json_mode=json_mode,
                )
        else:
            if verbose:
                _progress(
                    f"   ↳ {station_id}: probing receiver…",
                    json_mode=json_mode,
                    flush=True,
                )
            receiver_identity = _query_receiver_identity(station_id, station_config)
            if receiver_identity is None and verbose:
                _progress(
                    f"   ↳ {station_id}: receiver unreachable or no identity",
                    json_mode=json_mode,
                )

    if "tos" in sources:
        if verbose:
            _progress(
                f"   ↳ {station_id}: querying TOS…",
                json_mode=json_mode,
                flush=True,
            )
        tos_data = _query_tos(station_id)
        if tos_data is None and verbose:
            _progress(
                f"   ↳ {station_id}: not in TOS or TOS unavailable",
                json_mode=json_mode,
            )

    return receiver_identity, tos_data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_VERDICT_GLYPH = {
    Verdict.OK: "✓",
    Verdict.MISSING: "?",
    Verdict.CONFLICT: "✗",
    Verdict.SOURCES_DISAGREE: "!",
    Verdict.NO_DATA: "·",
    Verdict.NOT_QUERYABLE: "·",
}


def _render(value: Optional[str], width: int = 22) -> str:
    if value is None:
        s = "—"
    else:
        s = value
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s.ljust(width)


def _print_setup_header(station_id: str, station_config: Dict[str, Any]) -> None:
    """One-screen overview of the station's deployment setup.

    These are non-reconcilable, contextual fields the operator needs in
    order to decide whether a flagged value is a real discrepancy or a
    stale TOS entry (e.g. is the unit on a Teltonika modem? what's the
    install date? which port is the FTP daemon on?).
    """
    name = station_config.get("station_name") or ""
    header = f"\n=== {station_id}"
    if name:
        header += f" — {name}"
    header += " ==="
    print(header)

    rows = [
        ("Owner", station_config.get("station_owner")),
        ("Connection", station_config.get("connection_type")),
        ("Power", station_config.get("power_type")),
        ("Router IP", station_config.get("router_ip")),
        ("Router type", station_config.get("router_type")),
        (
            "Receiver ports",
            ", ".join(
                f"{label}={station_config.get(key)}"
                for label, key in (
                    ("ftp", "receiver_ftpport"),
                    ("http", "receiver_httpport"),
                    ("ctl", "receiver_controlport"),
                )
                if station_config.get(key)
            )
            or None,
        ),
        ("Config valid from", station_config.get("rinex_config_valid_from")),
        ("Lifecycle status", station_config.get("station_status")),
        ("Health check", station_config.get("health_check")),
    ]
    for label, val in rows:
        if val:
            print(f"  {label:<20} {val}")


def _is_tos_fillable(d: FieldDiff) -> bool:
    """True when cfg has a real value, TOS was queried but returned nothing, and the field is TOS-writable.

    Catches two cases:
    - NO_DATA: both receiver and TOS have no value (only cfg has it)
    - OK: cfg and receiver agree, but TOS has nothing — these show as ✓ but
      TOS still needs to be populated
    """
    return (
        d.verdict in (Verdict.NO_DATA, Verdict.OK)
        and d.cfg_value is not None
        and d.tos_value is None
        and "tos" in d.sources_queried
        and d.spec.tos_writable
    )


def _print_diff_table(
    diffs: List[FieldDiff],
    show_ok: bool = True,
) -> None:
    print()
    print(f"   {'Field':<24} {'stations.cfg':<22} {'Receiver':<22} {'TOS':<22}")
    print(f"   {'-' * 24} {'-' * 22} {'-' * 22} {'-' * 22}")
    for d in diffs:
        # Always show format_mismatch, cfg_placeholder, and tos_fillable rows
        if (
            not show_ok
            and d.verdict == Verdict.OK
            and not d.format_mismatch
            and not _is_tos_fillable(d)
        ):
            continue
        if (
            not show_ok
            and d.verdict == Verdict.NO_DATA
            and not d.cfg_placeholder
            and not _is_tos_fillable(d)
        ):
            continue
        if d.cfg_placeholder:
            glyph = "~"
        elif d.format_mismatch:
            glyph = "≈"
        elif _is_tos_fillable(d):
            glyph = "↑"
        else:
            glyph = _VERDICT_GLYPH.get(d.verdict, "?")
        cfg_display = d.cfg_raw if d.cfg_raw is not None else d.cfg_value
        print(
            f" {glyph} {d.label:<24} {_render(cfg_display)} "
            f"{_render(d.receiver_value)} {_render(d.tos_value)}"
        )
        if d.note:
            print(f"     ↳ {d.note}")


def _summary_counts(diffs: List[FieldDiff]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for d in diffs:
        counts[d.verdict.value] = counts.get(d.verdict.value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------


_HELP = """
Actions:
  s   set the field to the suggested value (when shown)
  r   set to the receiver-reported value (cfg only)
  R   set cfg to receiver value AND push to TOS in one step (tos_writable fields only)
  t   set to the TOS value (cfg only, no TOS push)
  T   push the receiver value to TOS only (cfg unchanged) — use only after confirming this is a
      data-entry error (Pattern 1: correct the open value in-place).
      If the discrepancy reflects an instrument change (new receiver, fw
      upgrade, antenna swap), close the old TOS period and add a new one
      manually — do NOT use T in that case.
  C   push the cfg value to TOS (when cfg is correct but TOS is wrong)
  e   enter a custom value
  k   keep the existing cfg value (skip)
  q   quit reconciliation for this station
  ?   show this help

Receiver-primary fields (type, serial, firmware) show a warning when TOS
disagrees. Check the TOS history first:
  data-entry error (wrong value typed) → fix cfg with r, then T to correct TOS in-place.
  instrument change (swap/upgrade not logged) → k to keep cfg, then add new period in TOS manually.

For antenna fields (type, serial, radome): TOS is canonical but the operator
may have a correct value in cfg that TOS lacks. Use C to push cfg → TOS
without changing cfg itself.

Table glyphs:
  ✓  OK — cfg matches all queried sources
  ?  MISSING — cfg empty, at least one source has a value
  ✗  CONFLICT — cfg disagrees with a source
  !  SOURCES_DISAGREE — receiver and TOS give different values
  ~  cfg holds a placeholder value (e.g. a synthetic TOS serial) — should be removed
  ≈  format mismatch — normalized values agree but raw notation differs
  ↑  TOS has no value for this field but cfg does — offered after the main diff loop
  ·  no actionable data (both sources empty)
""".rstrip()


def _interactive_prompt(
    diff: FieldDiff,
    *,
    receiver_primary_active: bool = True,
    tos_data: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Any]:
    """Ask the user what to do for one field.

    Returns ``(action, value)`` where action is one of
    ``set``, ``push_tos``, ``push_cfg_to_tos``, ``push_component``, ``skip``, ``quit``
    and ``value`` is the chosen value (or a component dict for ``push_component``).

    When ``receiver_primary_active`` is True and ``diff.spec.receiver_primary``
    is set, a warning is shown reminding the operator to check whether the
    TOS discrepancy is a data-entry error (fix with T) or an unlogged
    instrument change (requires manual TOS period management).
    """
    is_primary = (
        receiver_primary_active
        and diff.spec.receiver_primary
        and diff.receiver_value is not None
    )

    options: List[str] = []
    if diff.suggestion is not None:
        src = diff.suggestion_source or "?"
        options.append(f"[s]et to {diff.suggestion!r} ({src})")
    if diff.receiver_value is not None and diff.receiver_value != diff.suggestion:
        options.append(f"[r]eceiver={diff.receiver_value!r}")
    if diff.tos_value is not None and diff.tos_value != diff.suggestion:
        options.append(f"[t]os={diff.tos_value!r}")
    if diff.spec.tos_writable and diff.receiver_value is not None:
        options.append("[T]push-receiver-to-TOS")
        options.append(f"[R]receiver→cfg+TOS ({diff.receiver_value!r})")
    if diff.spec.tos_writable and diff.cfg_value is not None:
        options.append("[C]push-cfg-to-TOS")
    if diff.spec.tos_components:
        for comp in diff.spec.tos_components:
            options.append(f"[{comp.key}]edit {comp.label}")
    options.extend(["[e]dit", "[k]eep", "[q]uit", "[?]help"])

    if is_primary:
        print(
            "     ⚠  TOS discrepancy on hardware-identity field — check TOS history first:\n"
            "        data-entry error (wrong value typed) → fix cfg with [r], then [T] to correct TOS in-place.\n"
            "        instrument change (swap/upgrade not logged) → [k] keep cfg, then add new period in TOS manually."
        )

    if diff.note:
        print(f"     ↳ {diff.note}")

    while True:
        print(f"     {' · '.join(options)}")
        try:
            raw = input("     > ").strip()
            choice = raw.lower()
        except EOFError:
            return ("quit", None)

        if choice == "?" or choice == "help":
            print(_HELP)
            continue
        if choice in ("k", "keep"):
            return ("skip", None)
        if choice in ("q", "quit"):
            return ("quit", None)
        # Uppercase-sensitive actions must be checked against `raw` BEFORE the
        # lowercase fallbacks that share the same letter (r/R, t/T, c/C).
        if raw == "R":  # uppercase R = set cfg to receiver value AND push to TOS
            if not diff.spec.tos_writable:
                print(
                    f"     (field {diff.cfg_key!r} is not TOS-writable — use r for cfg only)"
                )
                continue
            if diff.receiver_value is None:
                print("     (no receiver value available)")
                continue
            return ("set_and_push_tos", diff.receiver_value)
        if raw == "T":  # uppercase T = push receiver value to TOS only
            if not diff.spec.tos_writable:
                print(f"     (field {diff.cfg_key!r} is not TOS-writable)")
                continue
            if diff.receiver_value is None:
                print("     (no receiver value to push — use C to push cfg value)")
                continue
            return ("push_tos", diff.receiver_value)
        if raw == "C":  # uppercase C = push cfg value to TOS
            if not diff.spec.tos_writable:
                print(f"     (field {diff.cfg_key!r} is not TOS-writable)")
                continue
            if diff.cfg_value is None:
                print("     (no cfg value to push)")
                continue
            return ("push_cfg_to_tos", diff.cfg_value)
        if choice in ("r", "receiver", ""):
            if diff.receiver_value is None:
                print("     (receiver value not available)")
                continue
            return ("set", diff.receiver_value)
        if choice in ("s", "set"):
            if diff.suggestion is None:
                print("     (no suggestion available — pick r/t/e)")
                continue
            return ("set", diff.suggestion)
        if choice in ("t", "tos"):
            if diff.tos_value is None:
                print("     (TOS value not available)")
                continue
            return ("set", diff.tos_value)
        if diff.spec.tos_components:
            for comp in diff.spec.tos_components:
                if raw == comp.key:
                    from ..cfg import tos_adapter as _ta

                    current = (
                        _ta.current_component_value(
                            tos_data, comp.entity, comp.current_value_key
                        )
                        if tos_data is not None
                        else None
                    )
                    hint = f" [{current}]" if current is not None else ""
                    try:
                        new_val = input(f"     {comp.label}{hint}: ").strip()
                    except EOFError:
                        return ("quit", None)
                    if not new_val:
                        print("     (empty — skipping)")
                        break
                    return (
                        "push_component",
                        {
                            "entity": comp.entity,
                            "attribute_code": comp.attribute_code,
                            "value": new_val,
                        },
                    )
        if choice in ("e", "edit"):
            try:
                custom = input("     value: ").strip()
            except EOFError:
                return ("quit", None)
            if not custom:
                print("     (empty — skipping)")
                return ("skip", None)
            return ("set", custom)
        print(f"     unknown action {choice!r}")


# ---------------------------------------------------------------------------
# TOS push helper
# ---------------------------------------------------------------------------


def _effective_date_for(args: argparse.Namespace) -> str:
    """Return an ISO-8601 date_from for TOS attribute writes.

    Uses ``args.effective_date`` when set, otherwise falls back to current UTC
    with a warning — correct for serial/firmware corrections where the operator
    knows the actual change date only approximately.
    """
    ed = getattr(args, "effective_date", None)
    if ed:
        return ed
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    logger.debug("--effective-date not set; defaulting to now (%s)", now)
    return now


def _sync_devices_to_tos(
    station_id: str,
    station_config: Dict[str, Any],
    receiver_identity: Optional[Dict[str, Any]],
    tos_data: Optional[Dict[str, Any]],
    args: argparse.Namespace,
    silent: bool,
) -> int:
    """Create missing device entities in TOS for a station.

    Checks whether the receiver/antenna/radome reported by health probe exist
    in TOS as child entities of the station. For any that don't, creates the
    device entity via ``TOSWriter.create_device()``.

    Returns the number of devices created (0 in dry-run mode counts as a
    "would create" tally).
    """
    if tos_data is None:
        if not silent:
            print(f"     ❌ cannot sync devices: no TOS data for {station_id}")
        return 0
    if receiver_identity is None:
        if not silent:
            print(
                f"     ⚠️  no receiver identity for {station_id} — skipping device sync"
            )
        return 0

    try:
        from tostools.api.tos_writer import TOSWriter
        from tostools.device import build_required_attributes, validate_model

        from ..cfg.tos_push import resolve_entity_id
    except ImportError as exc:
        if not silent:
            print(f"     ❌ tostools not available: {exc}")
        return 0

    dry_run: bool = getattr(args, "dry_run", True)
    writer = TOSWriter(dry_run=dry_run)
    date_from = _effective_date_for(args)
    station_entity_id = tos_data.get("id_entity")
    if station_entity_id is None:
        return 0

    # Device types to check: (health_key, entity_subtype, serial_field, model_field)
    device_types = [
        ("receiver_type", "gnss_receiver", "receiver_serial", "receiver_type"),
        ("antenna_type", "antenna", "antenna_serial", "antenna_type"),
        ("radome_type", "radome", None, "radome_type"),
    ]

    created = 0
    for model_key, entity_subtype, serial_key, _model_key in device_types:
        model_raw = receiver_identity.get(model_key) or station_config.get(model_key)
        if not model_raw:
            continue

        serial_raw = None
        if serial_key:
            serial_raw = receiver_identity.get(serial_key) or station_config.get(
                serial_key
            )
        if not serial_raw and serial_key:
            # Radome doesn't require serial; receiver/antenna do.
            if not silent:
                print(
                    f"     ⚠️  no serial for {entity_subtype} ({model_key}={model_raw}) — skipping"
                )
            continue

        # Check if entity already exists in TOS
        existing_id = resolve_entity_id(writer, station_entity_id, entity_subtype)
        if existing_id is not None:
            continue

        # Also check by serial to catch devices that exist but aren't connected
        if serial_raw:
            existing = writer.find_device_by_serial(entity_subtype, str(serial_raw))
            if existing is not None:
                if not silent:
                    print(
                        f"     ℹ️  {entity_subtype} serial={serial_raw!r} exists in TOS "
                        f"(id={existing.get('id_entity')}) but not connected to station "
                        f"{station_id} — use 'receivers cfg add-receiver' to connect"
                    )
                continue

        # Validate and normalise the model name
        try:
            model_igs = validate_model(entity_subtype, str(model_raw))
        except ValueError as exc:
            if not silent:
                print(f"     ⚠️  {entity_subtype} model {model_raw!r}: {exc}")
            continue

        if not silent:
            mode = "[DRY-RUN] " if dry_run else ""
            serial_info = f", serial={serial_raw!r}" if serial_raw else ""
            print(
                f"     {mode}→ create {entity_subtype}: model={model_igs!r}{serial_info}"
            )

        try:
            attributes = build_required_attributes(
                serial=serial_raw or "",
                model=model_igs,
                owner=station_id,
                location=station_id,
                date_start=date_from,
            )
            result = writer.create_device(
                entity_subtype=entity_subtype,
                attributes=attributes,
            )
            if not silent:
                if hasattr(result, "method"):  # DryRunResult
                    print(
                        f"     ✅ [dry-run] would create {entity_subtype} "
                        f"(serial={serial_raw!r})"
                    )
                else:
                    entity_id = (
                        result.get("id_entity") if isinstance(result, dict) else "?"
                    )
                    print(
                        f"     ✅ created {entity_subtype} "
                        f"(id={entity_id}, serial={serial_raw!r})"
                    )
            created += 1
        except Exception as exc:  # noqa: BLE001
            if not silent:
                print(f"     ❌ device creation failed ({entity_subtype}): {exc}")
            logger.warning(
                "[%s] device sync failed for %s: %s",
                station_id,
                entity_subtype,
                exc,
            )

    return created


def _do_push_tos(
    station_id: str,
    diff: FieldDiff,
    value: str,
    tos_data: Optional[Dict[str, Any]],
    args: argparse.Namespace,
    silent: bool,
) -> None:
    """Push *value* for *diff.spec* to TOS; handles errors and dry-run.

    Routes to Pattern 2 (transition_attribute_value) when:
    - The TOS value differs from the new value (it's a change, not a new add)
    - ``--no-transition`` is not set
    - The field has a TOS value to compare against (diff.tos_value)

    Otherwise uses Pattern 1 (upsert_attribute_value).
    """
    if tos_data is None:
        if not silent:
            print(f"     ❌ cannot push to TOS: no TOS data for {station_id}")
        return

    try:
        from tostools.api.tos_writer import TOSWriter

        from ..cfg.tos_push import push_field_to_tos, push_field_transition_to_tos
    except ImportError as exc:
        if not silent:
            print(f"     ❌ tostools not available: {exc}")
        return

    dry_run: bool = getattr(args, "dry_run", True)
    no_transition: bool = getattr(args, "no_transition", False)
    writer = TOSWriter(dry_run=dry_run)
    date_from = _effective_date_for(args)

    # Decide Pattern 1 vs Pattern 2
    use_transition = (
        not no_transition and diff.tos_value is not None and diff.tos_value != value
    )

    if not silent:
        mode = "[DRY-RUN] " if dry_run else ""
        pattern = "Pattern 2 (transition)" if use_transition else "Pattern 1 (upsert)"
        print(
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
            if not silent:
                if hasattr(result, "method"):  # DryRunResult
                    print(f"     ✅ [dry-run] would transition {diff.cfg_key}")
                elif isinstance(result, dict):
                    closed = "closed" if result.get("closed") else "no prior period"
                    print(
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
            if not silent:
                if hasattr(result, "method"):  # DryRunResult
                    print(f"     ✅ [dry-run] would {result.method} {result.endpoint}")
                else:
                    print(f"     ✅ TOS updated: {diff.cfg_key} = {value!r}")
    except Exception as exc:  # noqa: BLE001
        if not silent:
            print(f"     ❌ TOS push failed: {exc}")
        logger.warning("[%s] TOS push failed for %s: %s", station_id, diff.cfg_key, exc)


# ---------------------------------------------------------------------------
# Position sanity gate
# ---------------------------------------------------------------------------


def _position_sanity_check(
    diffs: List[FieldDiff],
    abort_m: float,
) -> Optional[str]:
    """Return an error message if the receiver position is suspiciously far from TOS.

    Computes the receiver-vs-TOS delta for each position field in metres and
    returns a message when any single axis exceeds ``abort_m``.  Returns None
    when the position looks sane or when position data is unavailable.

    This is intentionally a per-axis check rather than a 3-D distance so that
    a single bad axis (e.g. PVT height spike) is still caught even when
    horizontal looks fine.
    """
    import math

    _DEG_PER_M_LAT = 1.0 / 111111.0
    _DEG_PER_M_LON = 1.0 / (111111.0 * math.cos(math.radians(64.0)))

    for d in diffs:
        if d.cfg_key not in ("latitude", "longitude", "height"):
            continue
        if d.receiver_value is None or d.tos_value is None:
            continue
        try:
            delta = abs(float(d.receiver_value) - float(d.tos_value))
        except (ValueError, TypeError):
            continue
        if d.cfg_key == "latitude":
            delta_m = delta / _DEG_PER_M_LAT
        elif d.cfg_key == "longitude":
            delta_m = delta / _DEG_PER_M_LON
        else:
            delta_m = delta

        if delta_m > abort_m:
            return (
                f"⛔  POSITION SANITY FAIL: {d.label} differs by {delta_m:.0f} m "
                f"(receiver={d.receiver_value}, TOS={d.tos_value}, threshold={abort_m:.0f} m). "
                f"Receiver may be at the wrong station — "
                f"hardware-identity auto-push blocked."
            )
    return None


# ---------------------------------------------------------------------------
# Per-station reconciliation
# ---------------------------------------------------------------------------


def _reconcile_one(
    station_id: str,
    station_config: Dict[str, Any],
    sources: List[str],
    fields: Optional[List[str]],
    args: argparse.Namespace,
    receiver_identity: Optional[Dict[str, Any]] = None,
    tos_data: Optional[Dict[str, Any]] = None,
    global_target: Optional[Path] = None,
) -> Tuple[List[FieldDiff], int, int]:
    """Reconcile one station given pre-fetched probe data.

    ``receiver_identity`` and ``tos_data`` must already be fetched by the
    caller (via :func:`_probe_station`). This function only handles
    comparison, display, and writes — no network I/O.

    Returns ``(diffs, n_written, n_skipped)``.
    """
    if not args.json:
        _print_setup_header(station_id, station_config)

    tolerance_m = getattr(args, "position_tolerance_m", 2.0)
    field_specs = with_position_tolerance(tolerance_m) if tolerance_m else None

    diffs = compare_station(
        station_id=station_id,
        station_config=station_config,
        receiver_identity=receiver_identity,
        tos_data=tos_data,
        fields=fields,
        queried_sources=set(sources) | {"cfg"},
        field_specs=field_specs,
    )

    silent = args.json
    show_ok = not (args.only_diffs or getattr(args, "open", False))
    if not silent:
        _print_diff_table(diffs, show_ok=show_ok)

    # Position sanity gate — must run before any write logic so the warning
    # is visible even in dry-run mode.
    abort_m: float = getattr(args, "position_abort_m", 50.0)
    position_warn = _position_sanity_check(diffs, abort_m)
    if position_warn and not silent:
        print(f"\n   {position_warn}")

    n_written = 0
    n_skipped = 0
    # Write targets: always the local stations.cfg (None → apply_diff/remove_diff
    # resolve it); with --global, ALSO the gps-config-data repo's stations.cfg
    # (the source of truth), so one `cfg reconcile … --global --push` run syncs
    # the local deployed config AND the repo in lockstep. Resolved once by the
    # handler (incl. the divergence preflight).
    _write_targets: List[Optional[Path]] = [None]
    if global_target is not None:
        _write_targets.append(global_target)

    def _apply_to_targets(_d, _value, **_kw) -> bool:
        _changed = False
        for _tgt in _write_targets:
            if apply_diff(station_id, _d, _value, cfg_path=_tgt, **_kw):
                _changed = True
        return _changed

    def _remove_from_targets(_d, **_kw) -> bool:
        _changed = False
        for _tgt in _write_targets:
            if remove_diff(station_id, _d, cfg_path=_tgt, **_kw):
                _changed = True
        return _changed

    actionable = [d for d in diffs if d.needs_attention]
    canonicalize_on = getattr(args, "canonicalize", False)
    fmt_mismatches = [d for d in diffs if d.format_mismatch] if canonicalize_on else []
    # cfg_placeholder rows are always collected — cleaned in --canonicalize or
    # interactively, independent of whether other fields need attention.
    cfg_placeholders = [d for d in diffs if d.cfg_placeholder]
    # tos_fillable: cfg has a value but TOS has none — offered after the main loop.
    # Computed here so the early-return guard can include them.
    tos_fillable_list = [d for d in diffs if _is_tos_fillable(d)] if not silent else []
    if (
        not actionable
        and not fmt_mismatches
        and not cfg_placeholders
        and not tos_fillable_list
    ):
        return diffs, 0, 0

    if args.dry_run:
        if not silent and actionable:
            print(
                f"\n   {len(actionable)} field(s) need attention (dry-run, no writes)"
            )
        if not silent and cfg_placeholders:
            keys = ", ".join(d.cfg_key for d in cfg_placeholders)
            print(
                f"\n   {len(cfg_placeholders)} placeholder value(s) to remove: {keys} (dry-run)"
            )
        if not silent and tos_fillable_list:
            # Annotate fields where TOS is not truly empty but holds a
            # placeholder (synthetic serial = recorded-as-unknown) — pushing
            # the cfg value there is usually wrong (see interactive prompt).
            keys = ", ".join(
                f"{d.cfg_key}(TOS=unknown)" if d.tos_placeholder else d.cfg_key
                for d in tos_fillable_list
            )
            print(
                f"\n   {len(tos_fillable_list)} field(s) with cfg value but TOS empty: {keys} (use C to populate)"
            )
        # canonicalize dry-run section handled below; fall through
        if not canonicalize_on:
            return diffs, 0, len(actionable)

    # In JSON mode without an auto-resolution flag we can't make decisions —
    # nothing to do beyond returning diffs for the caller to report.
    push_to_tos_on = getattr(args, "push_tos", False)
    if silent and not (args.auto_fill or args.yes or push_to_tos_on):
        return diffs, 0, 0

    # --push-tos batch mode: push receiver values to TOS for all writable fields
    # that have a receiver value, independent of the cfg reconciliation loop below.
    if push_to_tos_on and "tos" in sources and tos_data is not None:
        # Live writes require either explicit --yes or --dry-run. Interactive
        # mode without either is treated as "show me the table, ask again" —
        # not as implicit consent to write to TOS for every actionable field.
        dry_run_flag: bool = getattr(args, "dry_run", False)
        consent_given: bool = bool(args.yes) or dry_run_flag
        if not silent:
            writable = [
                d
                for d in actionable
                if d.spec.tos_writable and d.receiver_value is not None
            ]
            if writable:
                if consent_given:
                    print(f"\n   Pushing {len(writable)} field(s) to TOS…")
                else:
                    print(
                        f"\n   ⚠️  --push-tos batch mode would write {len(writable)} "
                        f"field(s) to TOS. Re-run with --yes to confirm or "
                        f"--dry-run to preview."
                    )
        if consent_given:
            for d in actionable:
                if not d.spec.tos_writable or d.receiver_value is None:
                    continue
                _do_push_tos(
                    station_id=station_id,
                    diff=d,
                    value=d.receiver_value,
                    tos_data=tos_data,
                    args=args,
                    silent=silent,
                )
    elif push_to_tos_on and "tos" not in sources:
        if not silent:
            print("   ⚠️  --push-tos requires --source tos or --source both")

    # --sync-devices: create missing device entities in TOS
    sync_devices_on: bool = getattr(args, "sync_devices", False)
    if sync_devices_on and push_to_tos_on and "tos" in sources:
        consent_given: bool = bool(getattr(args, "yes", False)) or bool(
            getattr(args, "dry_run", False)
        )
        if consent_given:
            _sync_devices_to_tos(
                station_id=station_id,
                station_config=station_config,
                receiver_identity=receiver_identity,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
        elif not silent:
            print("   ⚠️  --sync-devices requires --yes or --dry-run to proceed")

    field_specs_by_key = fields_by_key()
    no_receiver_primary: bool = getattr(args, "no_receiver_primary", False) or getattr(
        args, "interactive", False
    )
    # Position sanity failure disables receiver-primary auto-push regardless of flags.
    receiver_primary_active = not no_receiver_primary and position_warn is None

    if not silent:
        print()
    for idx, d in enumerate(actionable, start=1):
        if not silent:
            header = f"  [{idx}/{len(actionable)}] {d.cfg_key} ({d.verdict.value})"
            if d.spec.receiver_primary and receiver_primary_active:
                header += "  [receiver-primary]"
            print(header)
            cfg_disp = d.cfg_raw if d.cfg_raw is not None else d.cfg_value
            print(f"     cfg:      {cfg_disp if cfg_disp is not None else '[missing]'}")
            if "receiver" in sources:
                rx = d.receiver_value if d.receiver_value is not None else "[N/A]"
                print(f"     receiver: {rx}")
            if "tos" in sources:
                tos = d.tos_value if d.tos_value is not None else "[N/A]"
                print(f"     TOS:      {tos}")

        # Resolve action
        action: str
        new_value: Optional[str]
        is_primary = (
            receiver_primary_active
            and d.spec.receiver_primary
            and d.receiver_value is not None
            and d.spec.tos_writable
            and tos_data is not None
        )
        if args.auto_fill and d.verdict == Verdict.MISSING and d.suggestion is not None:
            if is_primary and d.suggestion_source in ("receiver", "agree"):
                action, new_value = "set_and_push_tos", d.suggestion
                if not silent:
                    print(
                        f"     → auto-fill from {d.suggestion_source}: {d.suggestion!r} (cfg + TOS)"
                    )
            else:
                action, new_value = "set", d.suggestion
                if not silent:
                    print(
                        f"     → auto-fill from {d.suggestion_source}: {d.suggestion!r}"
                    )
        elif args.yes and d.suggestion is not None:
            if is_primary and d.suggestion_source in ("receiver", "agree"):
                action, new_value = "set_and_push_tos", d.suggestion
                if not silent:
                    print(
                        f"     → accept suggestion ({d.suggestion_source}): {d.suggestion!r} (cfg + TOS)"
                    )
            else:
                action, new_value = "set", d.suggestion
                if not silent:
                    print(
                        f"     → accept suggestion ({d.suggestion_source}): {d.suggestion!r}"
                    )
        elif args.yes and is_primary and d.receiver_value is not None:
            # --yes with receiver_primary but no agreed suggestion: still take receiver
            action, new_value = "set_and_push_tos", d.receiver_value
            if not silent:
                print(
                    f"     → accept receiver (primary): {d.receiver_value!r} (cfg + TOS)"
                )
        elif silent:
            # JSON mode without an applicable auto-rule: cannot prompt; skip.
            action, new_value = "skip", None
        else:
            action, new_value = _interactive_prompt(
                d, receiver_primary_active=receiver_primary_active, tos_data=tos_data
            )

        if action == "quit":
            if not silent:
                print(f"\n     stopped at field {idx}/{len(actionable)}")
            break
        if action == "skip":
            n_skipped += 1
            continue
        if action == "push_tos" and new_value is not None:
            _do_push_tos(
                station_id=station_id,
                diff=d,
                value=new_value,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
            continue
        if action == "push_cfg_to_tos" and new_value is not None:
            if not silent:
                print(f"     → push cfg value to TOS: {d.cfg_key} = {new_value!r}")
            _do_push_tos(
                station_id=station_id,
                diff=d,
                value=new_value,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
            continue
        if action == "push_component" and isinstance(new_value, dict):
            component_info = cast(Dict[str, str], new_value)
            entity = component_info["entity"]
            attribute_code = component_info["attribute_code"]
            value = component_info["value"]
            if not silent:
                mode = "[DRY-RUN] " if getattr(args, "dry_run", True) else ""
                print(
                    f"     {mode}→ push component to TOS: {entity}.{attribute_code} = {value!r}"
                )
            if tos_data is None:
                if not silent:
                    print("     ❌ no TOS data — cannot push component")
                continue
            try:
                from tostools.api.tos_writer import TOSWriter

                from ..cfg.tos_push import push_component_to_tos

                writer = TOSWriter(dry_run=getattr(args, "dry_run", True))
                result = push_component_to_tos(
                    writer=writer,
                    entity=entity,
                    attribute_code=attribute_code,
                    value=value,
                    tos_data=tos_data,
                    date_from=_effective_date_for(args),
                )
                if not silent:
                    if hasattr(result, "method"):
                        print(
                            f"     ✅ [dry-run] would {result.method} {result.endpoint}"
                        )
                    else:
                        print(
                            f"     ✅ TOS updated: {entity}.{attribute_code} = {value!r}"
                        )
            except Exception as exc:  # noqa: BLE001
                if not silent:
                    print(f"     ❌ component push failed: {exc}")
                logger.warning(
                    "[%s] component push failed for %s.%s: %s",
                    station_id,
                    entity,
                    attribute_code,
                    exc,
                )
            continue
        if action == "set_and_push_tos" and new_value is not None:
            # Apply cfg vocabulary mapping first (same as "set")
            spec = field_specs_by_key.get(d.cfg_key)
            if spec is not None:
                try:
                    mapped = spec.cfg_format(new_value)
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        print(f"     ❌ cfg_format failed for {d.cfg_key}: {exc}")
                    continue
                if mapped is None:
                    if not silent:
                        print(
                            f"     ❌ cfg_format normalised {new_value!r} to None — skipping"
                        )
                    continue
                if mapped != new_value and not silent:
                    print(
                        f"     ↺ normalised {new_value!r} → {mapped!r} for cfg vocabulary"
                    )
                new_value = mapped
            try:
                changed = _apply_to_targets(d, new_value)
                if changed:
                    n_written += 1
                    if not silent:
                        print(f"     ✅ wrote {d.cfg_key} = {new_value!r} to cfg")
                elif not silent:
                    print(
                        f"     ⏭  cfg unchanged ({d.cfg_key} already = {new_value!r})"
                    )
            except SourceUnavailableError as exc:
                if not silent:
                    print(f"     ❌ could not write cfg: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                if not silent:
                    print(f"     ❌ cfg write failed: {exc}")
                continue
            # Now push to TOS — best-effort, cfg write already succeeded
            _do_push_tos(
                station_id=station_id,
                diff=d,
                value=d.receiver_value or new_value,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
            continue
        if action == "set" and new_value is not None:
            # Apply per-field cfg vocabulary mapping (e.g. TOS "SEPT POLARX5"
            # → cfg "PolaRX5"). Identity for fields without an explicit map.
            spec = field_specs_by_key.get(d.cfg_key)
            if spec is not None:
                try:
                    mapped = spec.cfg_format(new_value)
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        print(f"     ❌ cfg_format failed for {d.cfg_key}: {exc}")
                    continue
                if mapped is None:
                    if not silent:
                        print(
                            f"     ❌ cfg_format normalised {new_value!r} to None — skipping"
                        )
                    continue
                if mapped != new_value and not silent:
                    print(
                        f"     ↺ normalised {new_value!r} → {mapped!r} for cfg vocabulary"
                    )
                new_value = mapped
            try:
                changed = _apply_to_targets(d, new_value)
                if changed:
                    n_written += 1
                    if not silent:
                        print(f"     ✅ wrote {d.cfg_key} = {new_value!r}")
                elif not silent:
                    print(f"     ⏭  unchanged ({d.cfg_key} already = {new_value!r})")
            except SourceUnavailableError as exc:
                if not silent:
                    print(f"     ❌ could not write: {exc}")
            except Exception as exc:  # noqa: BLE001
                if not silent:
                    print(f"     ❌ write failed: {exc}")

    # --canonicalize: rewrite format-mismatch fields to receiver notation
    if canonicalize_on and fmt_mismatches:
        if not silent:
            print(f"\n   Canonicalizing {len(fmt_mismatches)} notation-only field(s)…")
        for d in fmt_mismatches:
            rx_val = d.receiver_value  # guaranteed non-None by format_mismatch
            assert rx_val is not None
            if args.dry_run:
                if not silent:
                    print(f"     ≈ {d.cfg_key}: {d.cfg_raw!r} → {rx_val!r} (dry-run)")
            else:
                try:
                    changed = _apply_to_targets(d, rx_val, resolved_by="canonicalize")
                    if changed:
                        n_written += 1
                        if not silent:
                            print(f"     ✅ {d.cfg_key}: {d.cfg_raw!r} → {rx_val!r}")
                    elif not silent:
                        print(f"     ⏭  {d.cfg_key} already canonical")
                except SourceUnavailableError as exc:
                    if not silent:
                        print(f"     ❌ {d.cfg_key}: could not write: {exc}")
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        print(f"     ❌ {d.cfg_key}: write failed: {exc}")

    # Placeholder cleanup: remove keys whose raw cfg value is a recognized placeholder
    # (e.g. TOS synthetic serials like antenna-AFST-20210527).
    # --canonicalize auto-removes without prompting; interactive mode asks [d]elete.
    if cfg_placeholders:
        if canonicalize_on:
            if not silent:
                print(f"\n   Removing {len(cfg_placeholders)} placeholder value(s)…")
            for d in cfg_placeholders:
                if args.dry_run:
                    if not silent:
                        print(f"     ~ {d.cfg_key}: remove {d.cfg_raw!r} (dry-run)")
                else:
                    try:
                        changed = _remove_from_targets(d, resolved_by="canonicalize")
                        if changed:
                            n_written += 1
                            if not silent:
                                print(f"     ✅ {d.cfg_key}: removed {d.cfg_raw!r}")
                        elif not silent:
                            print(f"     ⏭  {d.cfg_key} already absent")
                    except SourceUnavailableError as exc:
                        if not silent:
                            print(f"     ❌ {d.cfg_key}: could not remove: {exc}")
                    except Exception as exc:  # noqa: BLE001
                        if not silent:
                            print(f"     ❌ {d.cfg_key}: removal failed: {exc}")
        elif not silent and not args.dry_run:
            # Interactive: prompt for each placeholder
            print(f"\n   {len(cfg_placeholders)} placeholder value(s) in cfg:")
            for d in cfg_placeholders:
                print(
                    f"\n     ~ {d.cfg_key} = {d.cfg_raw!r}  (placeholder — no real value)"
                )
                print("       [d]elete · [k]eep · [q]uit")
                try:
                    choice = input("       > ").strip().lower()
                except EOFError:
                    choice = "q"
                if choice in ("q", "quit"):
                    break
                if choice in ("d", "delete", ""):
                    try:
                        changed = _remove_from_targets(d)
                        if changed:
                            n_written += 1
                            print(f"       ✅ removed {d.cfg_key}")
                        else:
                            print("       ⏭  already absent")
                    except Exception as exc:  # noqa: BLE001
                        print(f"       ❌ removal failed: {exc}")
                else:
                    n_skipped += 1

    # TOS-fillable fields: cfg has a real value but TOS was queried and has nothing.
    # These are NOT "needs_attention" conflicts — they're silent gaps the operator
    # may want to fill. Show them separately so `C` is available without cluttering
    # the main diff flow.
    if not silent and not args.dry_run and tos_fillable_list:
        print(
            f"\n   {len(tos_fillable_list)} field(s) where cfg has a value but TOS has none:"
        )
        for d in tos_fillable_list:
            print(f"\n     ↑ {d.label} ({d.cfg_key})")
            print(f"       cfg: {d.cfg_value!r}")
            if d.tos_placeholder:
                # TOS holds a synthetic placeholder (e.g. antenna-ODDF-20230706):
                # the device EXISTS and its serial is recorded-as-UNKNOWN. Very
                # different from "no value" — the cfg value may belong to a
                # PREVIOUS device, so pushing it blindly would mislabel the
                # current one. Offer the cfg-side unknown-marker instead.
                print(f"       TOS: recorded as UNKNOWN (placeholder {d.tos_raw!r})")
                print(
                    "       ⚠ push C only if the cfg value truly belongs to the "
                    "CURRENT device"
                )
                print(
                    "       [z]set cfg = '0000000000' (unknown) · "
                    "[C]push-cfg-to-TOS · [k]eep · [q]uit"
                )
            else:
                print("       TOS: [no value — use C to populate]")
                print("       [C]push-cfg-to-TOS · [k]eep · [q]uit")
            try:
                raw = input("       > ").strip()
            except EOFError:
                break
            if raw in ("q", "quit"):
                break
            if raw == "z" and d.tos_placeholder:
                # Translate TOS's "unknown" into the cfg convention (all-zeros,
                # the fleet-wide unknown-serial marker) — deliberately bypassing
                # cfg_format: writing a value normalize() strips is the point.
                try:
                    changed = _apply_to_targets(d, "0000000000")
                    if changed:
                        n_written += 1
                        print(f"       ✅ wrote {d.cfg_key} = '0000000000' to cfg")
                    else:
                        print("       ⏭  cfg unchanged")
                except Exception as exc:  # noqa: BLE001
                    print(f"       ❌ cfg write failed: {exc}")
                continue
            if raw == "C":
                cfg_val = d.cfg_value
                assert cfg_val is not None  # guaranteed by _is_tos_fillable
                if not silent:
                    print(f"       → push cfg value to TOS: {d.cfg_key} = {cfg_val!r}")
                _do_push_tos(
                    station_id=station_id,
                    diff=d,
                    value=cfg_val,
                    tos_data=tos_data,
                    args=args,
                    silent=silent,
                )
            else:
                n_skipped += 1

    return diffs, n_written, n_skipped


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def cmd_cfg_reconcile(args) -> int:
    if args.list_fields:
        print("Reconcilable fields:")
        for f in FIELDS:
            sources = []
            if f.receiver_extract:
                sources.append("receiver")
            if f.tos_extract:
                sources.append("tos")
            print(f"  {f.cfg_key:30s} sources={','.join(sources):17s} {f.description}")
        return 0

    # Stations
    open_mode = getattr(args, "open", False)
    if open_mode:
        try:
            from ..cfg import discrepancy_log as _dlog

            station_ids = _dlog.open_station_ids()
        except Exception as exc:  # noqa: BLE001
            _progress(f"❌ could not read discrepancy log: {exc}", json_mode=args.json)
            return 1
        if not station_ids:
            _progress("✅ no open discrepancies — config is clean", json_mode=args.json)
            return 0
    elif args.all:
        station_ids = _all_station_ids()
    elif args.station:
        from .arguments import normalize_station_tokens

        station_ids = normalize_station_tokens(args.station)
    else:
        print("❌ specify station IDs, --all, or --open")
        print("   try: receivers cfg reconcile --help")
        return 2

    if not station_ids:
        print("❌ no stations to reconcile")
        return 1

    # Sources
    if args.source == "both":
        sources = ["receiver", "tos"]
    elif args.source == "receiver":
        sources = ["receiver"]
    elif args.source == "tos":
        sources = ["tos"]
    else:
        sources = ["receiver", "tos"]

    # Disable TOS if tostools unavailable, fall back gracefully
    if "tos" in sources:
        try:
            from tostools.api.tos_client import TOSClient  # noqa: F401
        except ImportError:
            _progress(
                "⚠️  tostools not installed — disabling TOS source",
                json_mode=args.json,
            )
            sources = [s for s in sources if s != "tos"]
            if not sources:
                _progress("❌ no usable sources remain", json_mode=args.json)
                return 1

    # Fields
    fields: Optional[List[str]] = None
    if args.field:
        valid = set(all_keys())
        invalid = [f for f in args.field if f not in valid]
        if invalid:
            print(f"❌ unknown fields: {invalid}")
            print(f"   available: {sorted(valid)}")
            return 2
        fields = list(args.field)
    elif open_mode:
        # Auto-derive fields that have open conflicts — no point probing unaffected fields.
        try:
            from ..cfg import discrepancy_log as _dlog

            fields = _dlog.open_field_keys(station_ids=station_ids) or None
        except Exception:  # noqa: BLE001
            fields = None  # fall back to all fields

    # Resolve the --global target ONCE up front. This also runs the divergence
    # preflight (require --push, clone even with origin) before any write, so a
    # refusal aborts cleanly with no dirty work-tree.
    from ..cfg.operations import CfgOperationError as _CfgOpErr

    try:
        global_target = _resolve_global_target(args)
    except _CfgOpErr as exc:
        _progress(f"❌ {exc}", json_mode=args.json)
        return 1

    # Load configs. When finalizing the repo (--global), read the BASELINE from
    # the gps-config-data repo (not the local deployed config), so a stale repo
    # is detected even when the local config was already reconciled. Writes still
    # go to BOTH local and repo (see _reconcile_one). GPS_CONFIG_PATH is restored
    # right after the load so cfg_path=None resolves the local stations.cfg again.
    if global_target is not None:
        import os as _os

        _orig_cfg_path = _os.environ.get("GPS_CONFIG_PATH")
        _os.environ["GPS_CONFIG_PATH"] = str(global_target.parent)
        try:
            configs = _load_station_configs(station_ids, json_mode=args.json)
        finally:
            if _orig_cfg_path is None:
                _os.environ.pop("GPS_CONFIG_PATH", None)
            else:
                _os.environ["GPS_CONFIG_PATH"] = _orig_cfg_path
    else:
        configs = _load_station_configs(station_ids, json_mode=args.json)
    if not configs:
        return 1

    # Skip discontinued / inactive stations — probing them is pointless and,
    # in some cases, harmful (e.g. BLAL shares an IP with SODU).
    _SKIP_STATUSES = frozenset({"discontinued", "inactive"})
    skipped_status = {
        sid: cfg.get("station_status")
        for sid, cfg in configs.items()
        if cfg.get("station_status") in _SKIP_STATUSES
    }
    skipped = list(skipped_status)
    for sid in skipped:
        del configs[sid]
    if skipped:
        _progress(
            f"⏭  skipping {len(skipped)} non-active station(s): {', '.join(sorted(skipped))}",
            json_mode=args.json,
        )
        # Auto-close any stale log entries so they don't resurface on the next --open run.
        try:
            from ..cfg import discrepancy_log as _dlog

            open_rows = _dlog.list_open(station_ids=skipped)
            for row in open_rows:
                _dlog.record_resolution(
                    row.station_id,
                    row.cfg_key,
                    action=_dlog.ACTION_IGNORED,
                    resolved_value=None,
                    note=f"station_status={skipped_status.get(row.station_id, '?')} — no longer reconciled",
                )
            if open_rows:
                _progress(
                    f"   closed {len(open_rows)} stale log entries for non-active stations",
                    json_mode=args.json,
                )
        except Exception as exc:  # noqa: BLE001
            _progress(
                f"⚠️  could not close stale log entries: {exc}", json_mode=args.json
            )
    if not configs:
        _progress("✅ no active stations with open discrepancies", json_mode=args.json)
        return 0

    # Header — keep stdout clean in JSON mode so output stays parseable
    _progress(
        f"Reconciling {len(configs)} station(s) — sources={','.join(sources)}"
        + (" — open discrepancies only" if open_mode else "")
        + (f" — fields={','.join(fields)}" if fields else "")
        + (" — dry-run" if args.dry_run else "")
        + (" — auto-fill" if args.auto_fill else "")
        + (" — canonicalize" if getattr(args, "canonicalize", False) else ""),
        json_mode=args.json,
    )

    # Resolve effective worker count (0 = auto).
    workers: int = getattr(args, "workers", 1)
    if workers == 0:
        workers = min(8, len(configs))

    # Single-station or single-worker: keep per-station verbose progress.
    parallel = workers > 1 and len(configs) > 1

    # --- Probe phase (parallel-safe I/O) ------------------------------------
    probe_results: Dict[
        str, Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]
    ] = {}

    if parallel:
        _progress(
            f"Probing {len(configs)} station(s) with {workers} workers…",
            json_mode=args.json,
        )
        ordered_sids = [sid for sid in station_ids if sid in configs]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _probe_station, sid, configs[sid], sources, args.json, False
                ): sid
                for sid in ordered_sids
            }
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    probe_results[sid] = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] probe thread raised: %s", sid, exc)
                    probe_results[sid] = (None, None)
    else:
        for sid in station_ids:
            if sid in configs:
                probe_results[sid] = _probe_station(
                    sid, configs[sid], sources, args.json, verbose=True
                )

    # --- Process phase (sequential: display, write, prompt) -----------------
    json_collect: List[Dict[str, Any]] = []
    total_written = 0
    total_skipped = 0
    n_with_issues = 0

    for sid in station_ids:
        cfg = configs.get(sid)
        if cfg is None:
            continue
        rx_identity, tos_d = probe_results.get(sid, (None, None))
        try:
            diffs, n_written, n_skipped = _reconcile_one(
                sid,
                cfg,
                sources,
                fields,
                args,
                rx_identity,
                tos_d,
                global_target=global_target,
            )
        except KeyboardInterrupt:
            print("\nInterrupted")
            break

        total_written += n_written
        total_skipped += n_skipped
        if any(d.needs_attention for d in diffs):
            n_with_issues += 1

        if args.json:
            entry: Dict[str, Any] = {
                "station_id": sid,
                "diffs": [d.as_dict() for d in diffs],
                "summary": _summary_counts(diffs),
            }
            if args.auto_fill or args.yes:
                entry["writes"] = n_written
                entry["skipped"] = n_skipped
            json_collect.append(entry)

    # --global: commit the gps-config-data stations.cfg edits this run made,
    # once, bundling all reconciled stations/fields. (No-op unless --global.)
    if getattr(args, "global_cfg", False):
        _sids = sorted(configs)
        _sid_label = "/".join(_sids) if len(_sids) <= 5 else f"{len(_sids)} stations"
        _flds = ",".join(args.field) if getattr(args, "field", None) else "fields"
        _maybe_commit_global(
            args,
            f"stations({_sid_label}): cfg reconcile {_flds}",
            changed=total_written > 0,
            dry_run=args.dry_run,
        )

    if args.json:
        print(json.dumps(json_collect, indent=2, default=str))
        return 0

    # Final summary
    print("\n" + "=" * 60)
    print(
        f"Stations: {len(configs)}   "
        f"with issues: {n_with_issues}   "
        f"writes: {total_written}   skipped: {total_skipped}"
    )
    return 0


# ---------------------------------------------------------------------------
# `cfg list` / `cfg history`
# ---------------------------------------------------------------------------


def _parse_since(spec: str) -> datetime:
    """Parse ``--since`` values: ``30d``, ``12h``, ``45m`` or ISO 8601."""
    s = spec.strip().lower()
    if s and s[-1] in ("d", "h", "m"):
        try:
            n = int(s[:-1])
        except ValueError as exc:
            raise ValueError(f"invalid --since value {spec!r}") from exc
        unit = s[-1]
        delta = (
            timedelta(days=n)
            if unit == "d"
            else timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
        )
        return datetime.now(UTC) - delta
    try:
        # Accept "2026-04-01" or "2026-04-01T12:00:00+00:00"
        dt = datetime.fromisoformat(spec)
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value {spec!r} (use 30d/12h/45m or ISO 8601)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _fmt_ts(ts: Optional[datetime]) -> str:
    if ts is None:
        return "—"
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _print_records_table(records, *, show_resolution: bool) -> None:
    if not records:
        print("(no rows)")
        return
    if show_resolution:
        header = (
            f"{'Station':<8} {'Field':<28} {'Verdict':<18} "
            f"{'Detected':<17} {'By':<14} {'Resolved':<17} {'Action':<14}"
        )
    else:
        header = (
            f"{'Station':<8} {'Field':<28} {'Verdict':<18} "
            f"{'Detected':<17} {'By':<14} {'cfg':<14} {'rx':<14} {'tos':<14}"
        )
    print(header)
    print("-" * len(header))
    for r in records:
        if show_resolution:
            print(
                f"{r.station_id:<8} {r.cfg_key:<28} {r.verdict:<18} "
                f"{_fmt_ts(r.detected_at):<17} {(r.detected_by or ''):<14} "
                f"{_fmt_ts(r.resolved_at):<17} {(r.resolved_action or '—'):<14}"
            )
        else:
            print(
                f"{r.station_id:<8} {r.cfg_key:<28} {r.verdict:<18} "
                f"{_fmt_ts(r.detected_at):<17} {(r.detected_by or ''):<14} "
                f"{_render(r.cfg_value, 14)}{_render(r.receiver_value, 14)}"
                f"{_render(r.tos_value, 14)}"
            )


def cmd_cfg_list(args) -> int:
    """``cfg list`` — open discrepancies, optionally filtered."""
    from ..cfg import discrepancy_log as _dlog  # type: ignore[attr-defined]
    from .arguments import normalize_station_tokens

    station_ids = normalize_station_tokens(args.station) if args.station else None
    fields: Optional[List[str]] = None
    if args.field:
        valid = set(all_keys())
        invalid = [f for f in args.field if f not in valid]
        if invalid:
            print(f"❌ unknown fields: {invalid}")
            print(f"   available: {sorted(valid)}")
            return 2
        fields = list(args.field)
    verdicts = list(args.verdict) if args.verdict else None

    try:
        records = _dlog.list_open(
            station_ids=station_ids,
            cfg_keys=fields,
            verdicts=verdicts,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ could not query cfg_discrepancy: {exc}")
        return 1

    if args.json:
        print(json.dumps([r.as_dict() for r in records], indent=2, default=str))
        return 0

    print(f"Open discrepancies: {len(records)}")
    if records:
        print()
        _print_records_table(records, show_resolution=False)
    return 0


def cmd_cfg_history(args) -> int:
    """``cfg history`` — full audit trail for a station and/or field."""
    from ..cfg import discrepancy_log as _dlog  # type: ignore[attr-defined]

    if not args.station and not args.field:
        print("❌ specify a station ID, --field KEY, or both")
        return 2

    from .arguments import normalize_station_tokens

    _normalized_station = normalize_station_tokens(args.station) if args.station else []
    station_id = _normalized_station[0] if _normalized_station else None
    if args.field:
        valid = set(all_keys())
        invalid = [f for f in args.field if f not in valid]
        if invalid:
            print(f"❌ unknown fields: {invalid}")
            print(f"   available: {sorted(valid)}")
            return 2
        cfg_keys: List[str] = list(args.field)
    else:
        cfg_keys = []

    since: Optional[datetime] = None
    if args.since:
        try:
            since = _parse_since(args.since)
        except ValueError as exc:
            print(f"❌ {exc}")
            return 2

    try:
        if cfg_keys:
            records = []
            for key in cfg_keys:
                records.extend(
                    _dlog.get_history(
                        station_id=station_id,
                        cfg_key=key,
                        since=since,
                        limit=args.limit,
                    )
                )
            records.sort(key=lambda r: r.detected_at, reverse=True)
            records = records[: args.limit]
        else:
            records = _dlog.get_history(
                station_id=station_id,
                since=since,
                limit=args.limit,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ could not query cfg_discrepancy: {exc}")
        return 1

    if args.json:
        print(json.dumps([r.as_dict() for r in records], indent=2, default=str))
        return 0

    target = station_id or f"--field {','.join(cfg_keys)}"
    print(f"History for {target} ({len(records)} row(s)):")
    if records:
        print()
        _print_records_table(records, show_resolution=True)
        if args.verbose:
            print("\nResolution notes:")
            for r in records:
                if r.resolution_note:
                    print(f"  [{r.id}] {r.station_id} {r.cfg_key}: {r.resolution_note}")
    return 0


def _tos_base_url(args) -> str:
    """Build the TOS API base URL from ``--server`` / ``--port``.

    The path segment is taken from tostools' own ``DEFAULT_TOS_URL`` rather than
    written literally here. tostools moved off ``/tos/v1`` onto ``/tos/internal``
    in ``bdfb9f3`` ("adapt to new TOS backend"), but the six copies of the
    literal in this module were never updated, and ``/tos/v1`` is now gone
    server-side::

        POST /tos/v1/login        -> 404
        POST /tos/internal/login  -> 401 (exists, wants credentials)

    That broke ``add-receiver``, ``add-antenna``, ``add-monument``,
    ``add-station``, ``import-campaigns`` and ``set-continuity`` outright:
    :meth:`TOSWriter.login` builds its URL as ``f"{base_url}/login"`` — the one
    request that does *not* go through ``canonical_tos_url`` — so every one of
    them died on a 404 before doing any work, including in dry-run (the
    duplicate-serial guard authenticates before the dry-run check).

    Verbs that build no base_url of their own were unaffected, because
    ``TOSWriter()`` already defaults to ``/tos/internal`` — which is why
    ``move-device`` / ``replace-*`` / ``reconcile`` kept working and masked this.

    Deriving the path means the two packages cannot desynchronise again.
    """
    from urllib.parse import urlsplit

    from tostools.api.tos_writer import DEFAULT_TOS_URL

    scheme = "https" if args.port == 443 else "http"
    path = urlsplit(DEFAULT_TOS_URL).path.rstrip("/")
    return f"{scheme}://{args.server}:{args.port}{path}"


# ---------------------------------------------------------------------------
# cfg extract
# ---------------------------------------------------------------------------

_EXTRACT_TODO_FIELDS = [
    "station_name",
    "router_type",
    "connection_type",
    "station_owner",
    "rinex_observer",
    "rinex_agency",
    "rinex_run_by",
    "rinex_config_valid_from",
]


#: cfg keys whose probed value is a receiver PVT navigation fix, not a survey.
#: Metre-level and provisional — see ``_print_probed_fields``.
_PROBE_PROVISIONAL_POSITION = ("latitude", "longitude", "height")


def _probe_cfg_fields(
    sid: str, station_config: Dict[str, Any], host: Optional[str]
) -> tuple[Dict[str, Any], Dict[str, str]] | None:
    """Probe a receiver and shape its identity into stations.cfg field values.

    Shared by ``cfg extract`` and ``cfg add-tos-station``: both need the same
    probe → field-manifest → ordered-fields pipeline, and a second copy would
    let the port defaults drift apart.

    Returns ``(identity, fields)``, or ``None`` when the receiver is unreachable
    so the caller can report in its own voice.
    """
    from ..cfg.field_manifest import FIELDS

    print(f"↳ {sid}: probing receiver…", flush=True)
    identity = _query_receiver_identity(sid, station_config)
    if identity is None:
        return None

    # Extract fields via field manifest (reuses receiver_extract lambdas + cfg_format)
    manifest_fields: Dict[str, str] = {}
    for spec in FIELDS:
        if spec.receiver_extract is None:
            continue
        raw_val = spec.receiver_extract(identity)
        if raw_val is None:
            continue
        formatted = spec.cfg_format(raw_val) if spec.cfg_format else str(raw_val)
        if formatted is not None and str(formatted).strip():
            manifest_fields[spec.cfg_key] = str(formatted)

    # Build ordered fields dict
    fields: Dict[str, str] = {}
    fields["station_id"] = sid

    if host:
        fields["router_ip"] = host

    if "receiver_type" in manifest_fields:
        fields["receiver_type"] = manifest_fields.pop("receiver_type")

    fields["receiver_ftpport"] = "2160"
    fields["receiver_httpport"] = "8060"
    fields["receiver_controlport"] = "28784"

    fields["rinex_marker_name"] = sid
    # MARKER NUMBER = IERS DOMES only; MARKER NAME already carries the 4-char id.
    # Bench extract has no TOS DOMES handy, so leave it unset (the converter skips
    # an absent value); `cfg sync-from-tos --only rinex_marker_number` fills the
    # real DOMES later if/when the station is registered.

    # Remaining manifest fields (antenna, position, serial, firmware)
    fields.update(manifest_fields)
    return identity, fields


def _print_probed_fields(fields: Dict[str, str], host: Optional[str]) -> None:
    """Print probed cfg fields, flagging the ones that are NOT ground truth.

    Two values look authoritative in the output but are not: ``router_ip`` is
    whatever host we dialled (a bench IP on an intake run), and the position
    triple is a single-epoch PVT navigation fix — metre-level, not a survey.
    stations.cfg position is otherwise treated as ground truth for surveyed
    coordinates, and ``cfg reconcile`` compares the receiver's PVT against it at
    a 2 m tolerance, so an unflagged probe value turns that check into a
    self-confirming PVT-vs-PVT comparison.
    """
    for key, val in fields.items():
        if key == "router_ip" and host:
            note = "  ← bench/probe IP — update to deployment IP before use"
        elif key in _PROBE_PROVISIONAL_POSITION:
            note = "  ← receiver PVT fix (~metre-level) — PROVISIONAL, not surveyed"
        else:
            note = ""
        print(f"  {key} = {val}{note}")


def cmd_cfg_extract(args) -> int:
    """``cfg extract`` — probe a receiver and add a new station section to stations.cfg."""
    import re
    from datetime import date
    from pathlib import Path

    from ..config.receivers_config import create_station_section
    from ..config_utils import resolve_receiver_endpoint

    sid = args.station_id.upper()
    host = getattr(args, "host", None)

    station_config = resolve_receiver_endpoint(args, sid)
    if station_config is None:
        print(f"❌ {sid}: not found in stations.cfg and no --host given")
        print("   Provide --host <IP> to connect to the receiver directly.")
        return 1

    try:
        import gps_parser  # type: ignore

        cfg_path = Path(gps_parser.ConfigParser().get_stations_config_path())
    except Exception as exc:
        print(f"❌ Cannot locate stations.cfg: {exc}")
        return 1

    cfg_text = cfg_path.read_text()
    if re.search(r"^\[" + re.escape(sid) + r"\]", cfg_text, re.MULTILINE):
        print(f"❌ [{sid}] already exists in stations.cfg")
        print(f"   Use: receivers cfg reconcile {sid}  to update individual fields")
        return 1

    probed = _probe_cfg_fields(sid, station_config, host)
    if probed is None:
        print(f"❌ {sid}: receiver unreachable or returned no identity data")
        return 1
    _identity, fields = probed

    # Display extracted fields
    print(f"\nFields extracted for [{sid}]:")
    _print_probed_fields(fields, host)
    print(f"\nFill in manually: {', '.join(_EXTRACT_TODO_FIELDS)}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    source = f"receiver on {date.today().isoformat()}"
    if host:
        source += f" (bench/probe IP: {host})"
    header = (
        f"Extracted from {source}\n"
        f"Review all fields — router_ip is the probe IP, not the deployment IP.\n"
        f"Fill in manually: {', '.join(_EXTRACT_TODO_FIELDS)}"
    )

    try:
        create_station_section(cfg_path, sid, fields, header_comment=header)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    print(f"\n✅ [{sid}] added to {cfg_path}")
    print(
        "   Next: fill in manual fields, then run 'receivers seed stations' to sync to DB"
    )
    return 0


# ---------------------------------------------------------------------------
# add-tos-station — greenfield orchestrator: one probe → TOS site+station+cfg
# ---------------------------------------------------------------------------


def cmd_cfg_add_tos_station(args) -> int:
    """``cfg add-tos-station`` — one probe → TOS site + station + stations.cfg.

    Collapses steps 0 and 1 of the greenfield onboarding sequence into a single
    command: probe the receiver once for its identity and PVT position, hand the
    coordinates to ``tos station add`` (which find-or-creates the ``land`` site,
    creates the station entity and joins them), then write the matching
    ``[STID]`` stations.cfg section from the same probe.

    Ordering matters and is the reason this exists: ``cfg move-device --to
    STATION`` fills the TOS station position attributes *from stations.cfg*, so
    the cfg section has to be in place before the receiver is installed —
    otherwise the fill silently no-ops. Doing both halves here from one probe
    removes the circular "cfg needs the probe, TOS wants the cfg" shuffle.

    **Why the probe lives here and not on ``tos station add``**: ``tostools``
    never dials a receiver (its SBF modules parse archived files), and
    ``receivers`` depends on ``tostools`` — pinned by direct URL. A ``--probe``
    flag on the tostools verb would need tostools to import receivers, i.e. a
    dependency cycle that makes both SHA pins unresolvable. So the orchestrator
    sits on the receivers side, where the probe already is, and drives tostools
    through its public CLI entry point (``tostools.tos.main``) rather than any
    private function.

    Dry-run by default in **both** halves — neither TOS nor stations.cfg is
    written without ``--no-dry-run``. Note that a created ``land`` site is not
    deletable in TOS, so preview first.
    """
    import io
    import json as _json
    import re
    import sys
    from argparse import Namespace
    from contextlib import nullcontext, redirect_stdout
    from datetime import date
    from pathlib import Path

    from tostools.tos import main as tos_main

    from ..config.receivers_config import create_station_section
    from ..config_utils import resolve_receiver_endpoint

    sid = args.station_id.upper()
    host = getattr(args, "probe", None)
    dry_run = not args.no_dry_run
    want_json = bool(getattr(args, "json", False))
    write_cfg = not getattr(args, "no_cfg", False)
    write_tos = not getattr(args, "no_tos", False)

    def say(*parts: Any) -> None:
        """Human-facing output. Silenced under --json so stdout stays parseable."""
        if not want_json:
            print(*parts)

    def warn(*parts: Any) -> None:
        """Errors and cautions. Always emitted, on stderr — never corrupts --json."""
        print(*parts, file=sys.stderr)

    if not write_cfg and not write_tos:
        warn("❌ --no-cfg and --no-tos together leave nothing to do")
        return 2

    # ---- Resolve the cfg path + refuse an existing section early ---------
    # Checked BEFORE the probe and before any TOS write: discovering a
    # collision after minting an undeletable land site is the bad ordering.
    cfg_path: Optional[Path] = None
    if write_cfg:
        try:
            import gps_parser  # type: ignore

            cfg_path = Path(gps_parser.ConfigParser().get_stations_config_path())
        except Exception as exc:
            warn(f"❌ Cannot locate stations.cfg: {exc}")
            return 1
        if re.search(
            r"^\[" + re.escape(sid) + r"\]", cfg_path.read_text(), re.MULTILINE
        ):
            warn(f"❌ [{sid}] already exists in stations.cfg")
            warn(f"   Use: receivers cfg reconcile {sid}  to update individual fields")
            warn("   Or:  --no-cfg  to create only the TOS records")
            return 1

    # ---- Coordinate overrides (read before the probe) --------------------
    # Explicit flags win — a real survey must never lose to a PVT fix. Read
    # first so a fully-specified position can carry a probe-less run.
    overrides = {
        "latitude": getattr(args, "lat", None),
        "longitude": getattr(args, "lon", None),
        "height": getattr(args, "height", None),
    }
    have_all_coords = all(v is not None for v in overrides.values())

    # ---- Probe (optional) ------------------------------------------------
    fields: Dict[str, str] = {}
    probe_ok = False
    if host:
        # Shim rather than passing `args` straight through:
        # resolve_receiver_endpoint reads args.port as the RECEIVER control
        # port, while --port here is the TOS API port (tostools convention).
        # Handing it `args` would dial the receiver on 443.
        probe_args = Namespace(
            host=host,
            receiver_type=getattr(args, "receiver_type", None) or "PolaRX5",
            port=getattr(args, "control_port", None) or 28784,
        )
        station_config = resolve_receiver_endpoint(probe_args, sid)
        # The shared probe helper prints its own progress line; swallow it under
        # --json (logging still goes to stderr) so stdout carries only the JSON.
        probe_sink = io.StringIO()
        with redirect_stdout(probe_sink) if want_json else nullcontext():
            probed = (
                _probe_cfg_fields(sid, station_config, host) if station_config else None
            )
        if probed is not None:
            _identity, fields = probed
            probe_ok = True
        else:
            control_port = getattr(args, "control_port", None) or 28784
            warn(f"⚠️  {sid}: receiver unreachable or returned no identity data")
            warn(
                f"   The probe uses the SBF control port ({control_port}). If the "
                "receiver answers on\n"
                "   HTTP but not here, that port is most likely not forwarded on "
                "the station router."
            )
            if not have_all_coords:
                warn(
                    "   Cannot continue: no position. Either fix the port forward, "
                    "or pass all of\n"
                    "   --lat/--lon/--height to register TOS from surveyed "
                    "coordinates instead."
                )
                return 1

    if not host and not have_all_coords:
        warn(f"❌ {sid}: nothing to go on — no --probe and no explicit position")
        warn("   Pass --probe <IP>, or all of --lat/--lon/--height.")
        return 2

    # Without a probe there is no receiver identity, so a stations.cfg section
    # would be a stub of ports and nothing else. Refuse rather than write one:
    # `cfg extract` is the right tool once the receiver is reachable.
    if not probe_ok and write_cfg:
        warn(
            "❌ A stations.cfg section needs receiver identity, which requires a "
            "successful probe."
        )
        warn(
            f"   Re-run with --no-cfg to register only the TOS records, then "
            f"`receivers cfg extract {sid} --host <IP>`\n"
            "   once the control port is reachable."
        )
        return 2

    # ---- Coordinates -----------------------------------------------------
    coords: Dict[str, Optional[str]] = {}
    surveyed: Dict[str, bool] = {}
    for cfg_key in ("latitude", "longitude", "height"):
        override = overrides[cfg_key]
        if override is not None:
            coords[cfg_key] = str(override)
            surveyed[cfg_key] = True
            if probe_ok:
                fields[cfg_key] = str(override)
        else:
            coords[cfg_key] = fields.get(cfg_key)
            surveyed[cfg_key] = False

    missing = [k for k, v in coords.items() if not v]
    if missing:
        warn(
            f"❌ {sid}: no position available for {', '.join(missing)} — the probe "
            "returned no PVT fix"
        )
        warn("   Supply --lat/--lon/--height explicitly.")
        return 2

    def _position_note(key: str) -> str:
        if surveyed.get(key):
            return "  ← supplied (surveyed)"
        return "  ← receiver PVT fix (~metre-level) — PROVISIONAL, not surveyed"

    if fields:
        # Not _print_probed_fields(): that helper cannot tell a PVT value from a
        # surveyed override, and the distinction is the whole point here.
        say(f"\nFields for [{sid}]:")
        for key, val in fields.items():
            if key == "router_ip" and host:
                note = "  ← probe IP — update to deployment IP if it differs"
            elif key in _PROBE_PROVISIONAL_POSITION:
                note = _position_note(key)
            else:
                note = ""
            say(f"  {key} = {val}{note}")
    else:
        say(f"\nPosition for [{sid}] (no probe — TOS records only):")
        for key in _PROBE_PROVISIONAL_POSITION:
            say(f"  {key} = {coords[key]}{_position_note(key)}")

    site_name = getattr(args, "site_name", None) or args.name

    # ---- TOS: site + station + join, via the public tos CLI --------------
    tos_result: Dict[str, Any] = {}
    if write_tos:
        argv = [
            "station",
            "add",
            "--marker",
            sid,
            "--name",
            args.name,
            "--lat",
            str(coords["latitude"]),
            "--lon",
            str(coords["longitude"]),
            "--altitude",
            str(coords["height"]),
            "--date-start",
            args.date_start,
            "--continuity",
            args.continuity,
            "--location-name",
            site_name,
        ]
        for flag, value in (
            ("--subtype", getattr(args, "subtype", None)),
            ("--operational-class", getattr(args, "operational_class", None)),
            ("--in-network-epos", getattr(args, "in_network_epos", None)),
            ("--server", getattr(args, "server", None)),
        ):
            if value:
                argv += [flag, str(value)]
        if getattr(args, "port", None):
            argv += ["--port", str(args.port)]
        if not dry_run:
            argv.append("--no-dry-run")

        say(f"\n{'🌵 [dry-run] ' if dry_run else ''}TOS: tos {' '.join(argv)}")
        try:
            if want_json:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = tos_main(argv + ["--json"])
                raw = buf.getvalue()
                try:
                    tos_result = _json.loads(raw)
                except ValueError:
                    tos_result = {"raw": raw}
            else:
                # Stream tostools' own output through: it carries the
                # duplicate-marker guard, the site-reuse report and the
                # "a land entity is NOT deletable" warning. --json would
                # suppress exactly those, so only capture when asked.
                rc = tos_main(argv)
        except SystemExit as exc:  # argparse bailed on a value we built
            rc = int(exc.code or 1)
        if rc != 0:
            warn(f"❌ TOS station add failed (exit {rc}) — stations.cfg left untouched")
            return rc

    # ---- stations.cfg section -------------------------------------------
    if write_cfg and cfg_path is not None:
        if dry_run:
            say(f"\n🌵 [dry-run] would add [{sid}] to {cfg_path}")
        else:
            pos_note = (
                "position is a receiver PVT fix (~metre-level) — replace with "
                "surveyed coordinates when available"
                if not all(surveyed.values())
                else "position supplied explicitly (surveyed)"
            )
            source = f"receiver on {date.today().isoformat()}"
            if host:
                source += f" (probe IP: {host})"
            header = (
                f"Created by 'cfg add-tos-station' from {source}\n"
                f"TOS site + station registered in the same run.\n"
                f"NOTE: {pos_note}.\n"
                f"Fill in manually: {', '.join(_EXTRACT_TODO_FIELDS)}"
            )
            try:
                create_station_section(cfg_path, sid, fields, header_comment=header)
            except ValueError as exc:
                warn(f"❌ {exc}")
                return 1
            say(f"\n✅ [{sid}] added to {cfg_path}")

    if want_json:
        print(
            _json.dumps(
                {
                    "station": sid,
                    "name": args.name,
                    "site_name": site_name,
                    "dry_run": dry_run,
                    "probe_host": host,
                    "position_surveyed": surveyed,
                    "cfg_fields": fields,
                    "tos": tos_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if dry_run:
        print("\n(dry run — nothing written to TOS or stations.cfg)")
        print("   Re-run with --no-dry-run to commit.")
    else:
        print("\nNext steps:")
        print(
            f"   receivers cfg add-receiver --probe {host or '<IP>'} "
            f"--date-start {args.date_start}"
        )
        print(
            f"   receivers cfg move-device --serial <SN> --to {sid} "
            f"--date {args.date_start}"
        )
        print(
            f"   receivers cfg add-antenna  --station {sid} --model <IGS> "
            f"--date-start {args.date_start}"
        )
        print(
            f"   receivers cfg add-monument --station {sid} --height <m> "
            f"--date-start {args.date_start}"
        )
        print("   (same --date-start throughout — a mismatch splits the TOS session)")
    return 0


# ---------------------------------------------------------------------------
# add-receiver — warehouse intake (step 6 of device-warehouse interface)
# ---------------------------------------------------------------------------


def build_constellation_attrs(
    constellations: Optional[Iterable[str]],
    *,
    skip: bool = False,
) -> tuple[list[tuple[str, str]], Optional[str], bool]:
    """Turn probed constellation codes into TOS optional attributes.

    Returns ``(attrs, note, note_is_warning)`` where ``attrs`` is a sorted list
    of ``(code, "true")`` pairs ready to append to ``add-receiver``'s optional
    list, and ``note`` is an operator-facing line (``None`` if there is nothing
    to say).

    Why this exists: without it the constellation attributes are simply never
    written at intake, and the only thing that notices later is
    ``tos audit missing-attributes`` — which suggests the *catalog defaults*
    (GPS + GLO), not what the receiver reports. Applying that suggestion to a
    Galileo/BeiDou-capable unit silently records it as a two-constellation
    station. Measured on NPSK 2026-08-08: the probe found GPS/GLO/GAL/BDS while
    the triage file offered GPS/GLO only.

    **Only ``"true"`` is ever emitted.** A system absent from the PVT solution
    may merely have had nothing in view at that instant, and QZSS/IRN are
    regional systems never visible from Iceland even when fully enabled. So
    absence is not evidence of "disabled", and writing ``false`` would be worse
    than writing nothing — it reads as a deliberate operator decision.
    """
    codes = sorted(constellations or ())
    if skip:
        note = (
            f"  ↷ skipping probed constellations ({', '.join(codes)}) "
            "— --no-constellations"
            if codes
            else None
        )
        return [], note, False
    if not codes:
        return (
            [],
            "  ⚠️  no constellations read from the probe — set them later with\n"
            "      `tos audit missing-attributes --id <station> --triage <file>`,\n"
            "      but verify against the receiver, not the suggested defaults.",
            True,
        )
    return (
        [(code, "true") for code in codes],
        f"  🛰  constellations from PVT solution: {', '.join(codes)}",
        False,
    )


def cmd_cfg_add_receiver(args) -> int:
    """``cfg add-receiver`` — probe a receiver and register it in TOS.

    Flow: parse ``--probe``, run :func:`receivers.cfg.device_probe.probe_receiver`,
    apply CLI overrides, validate against the tostools OwnersCache, IGS-normalise
    the model via :func:`tostools.device.validate_model`, then call
    :meth:`tostools.api.tos_writer.TOSWriter.create_device` + a per-optional-attr
    ``upsert_attribute_value``. Exit 0 on success, 1 on TOS write failure / probe
    failure, 2 on input-validation failure.
    """
    import json as _json
    import sys

    from tostools.api.tos_writer import TOSWriter
    from tostools.device import (
        build_required_attributes,
        iter_optional_attributes,
        normalize_date_start,
        validate_model,
    )
    from tostools.owners import OwnersCache

    from ..cfg.device_probe import (
        ProbeError,
        ProbeIncompleteError,
        ProbeNotIdentifiedError,
        ProbeUnreachableError,
        ReceiverIdentity,
        StationProbeResolveError,
        parse_host_port,
        probe_receiver,
        resolve_station_probe,
        to_subtype_attrs,
    )

    # ---- --probe / --station / --from-file mutual exclusion -------------
    from_file = getattr(args, "from_file", None)
    station = getattr(args, "station", None)
    if sum(bool(x) for x in (args.probe, station, from_file)) != 1:
        print(
            "❌ exactly one of --probe, --station, or --from-file is required",
            file=sys.stderr,
        )
        return 2
    if station:
        try:
            args.probe = resolve_station_probe(station)
        except StationProbeResolveError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        print(f"Resolved {station} → {args.probe}")

    # ---- --from-file: load identity + defaults from YAML ----------------
    # CLI args take precedence; file fills in only what was not supplied
    # via CLI. Required fields (owner/location/date_start) must come from
    # either side. This decouples the probe step (needs USB) from the TOS
    # write (needs VPN) — capture once, write later.
    file_identity: Optional[ReceiverIdentity] = None
    if from_file:
        from pathlib import Path as _Path

        import yaml as _yaml

        path = _Path(from_file).expanduser()
        if not path.exists():
            print(f"❌ --from-file path does not exist: {path}", file=sys.stderr)
            return 2
        try:
            data = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — surface YAML / IO errors equally
            print(f"❌ failed to parse --from-file YAML: {e}", file=sys.stderr)
            return 2
        if not isinstance(data, dict):
            print("❌ --from-file must contain a YAML mapping", file=sys.stderr)
            return 2

        # Fill in CLI args from file when not already supplied
        for key in (
            "owner",
            "location",
            "date_start",
            "station_hint",
            "firmware",
            "comment",
            "galvos",
            "probe_type",
        ):
            if not getattr(args, key, None):
                file_val = data.get(key)
                if file_val not in (None, ""):
                    setattr(args, key, file_val)

        # Build a ReceiverIdentity from file fields — replaces probe call.
        # Accept either 'model_raw' (probe-shaped) or 'model' as a
        # convenience for hand-edited intake records.
        file_identity = ReceiverIdentity(
            subtype=data.get("subtype", "gnss_receiver"),
            probe_type=data.get("probe_type") or args.probe_type or "polarx5",
            serial=data.get("serial"),
            model_raw=data.get("model_raw") or data.get("model"),
            firmware_version=data.get("firmware_version"),
            marker_name=data.get("marker_name"),
            partial=bool(data.get("partial", False)),
        )

    # ---- Apply default --owner if neither CLI nor file supplied one -----
    # Jarðeðlismælihópur owns the GPS receiver fleet for IMO. Every
    # existing open child of B9 - Kjallari - Jörð (id_entity=4) has this
    # as its owner attribute, so it's the right default for any new
    # warehouse intake of receivers/antennas/radomes/monuments. Operators
    # add devices owned by another group with --owner Vatnamælihópur
    # (etc.) or via the owner key in --from-file.
    if not getattr(args, "owner", None):
        args.owner = "Jarðeðlismælihópur"

    # ---- Apply default --location ---------------------------------------
    # ~71% of historical intakes land at B9 - Kjallari - Jörð (id_entity=4
    # in TOS — the main GPS warehouse). Saves the operator typing the
    # exact string every time; override via --location or via the
    # `location` key in --from-file for non-B9 warehouses.
    if not getattr(args, "location", None):
        args.location = "B9 - Kjallari - Jörð"

    # ---- Apply default --date-start (today) -----------------------------
    # The intake almost always happens "now" — registering today's
    # warehouse arrival. For back-dated intakes pass --date-start
    # explicitly or set `date_start:` in --from-file.
    if not getattr(args, "date_start", None):
        args.date_start = date.today().isoformat()

    # ---- Required-field validation (CLI-or-file) ------------------------
    missing = [
        f for f in ("owner", "location", "date_start") if not getattr(args, f, None)
    ]
    if missing:
        print(
            "❌ missing required field(s): "
            + ", ".join("--" + m.replace("_", "-") for m in missing)
            + " (supply via CLI or via --from-file)",
            file=sys.stderr,
        )
        return 2

    # ---- Cheap validation first (so the user doesn't wait through a
    # ---- 20-second probe timeout to learn the owner string is wrong).
    host: Optional[str] = None
    port: Optional[int] = None
    if args.probe:
        try:
            host, port = parse_host_port(args.probe)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2

    try:
        date_start = normalize_date_start(args.date_start)
    except ValueError as e:
        print(f"❌ Invalid --date-start: {e}", file=sys.stderr)
        return 2

    owners_cache = (
        OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    )
    if args.owner not in owners_cache.load():
        print(
            f"❌ Unknown owner: {args.owner!r}. "
            f"Run 'tos owners list' to see allowed values, or "
            f"'tos owners list --refresh' if you recently added one in TOS.",
            file=sys.stderr,
        )
        return 2

    # ---- Probe (network) — or load identity from file -------------------
    if file_identity is not None:
        identity = file_identity
        print(
            f"  Loaded identity from {from_file}: "
            f"serial={identity.serial!r} model={identity.model_raw!r} "
            f"fw={identity.firmware_version!r} (no probe)"
        )
    else:
        try:
            identity = probe_receiver(
                host,
                port,
                probe_type=args.probe_type,
                # --station resolves credentials from that station's cfg; fall
                # back to the explicit --station-hint for bench/warehouse intake.
                station_id_hint=station or args.station_hint,
            )
        except ProbeUnreachableError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        except ProbeNotIdentifiedError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        except ProbeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1

    # ---- Overrides + completeness ----------------------------------------
    try:
        merged = to_subtype_attrs(
            identity,
            serial_override=args.serial,
            model_override=args.model,
            firmware_override=args.firmware,
        )
    except ProbeIncompleteError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    # ---- IGS model normalisation ----------------------------------------
    try:
        igs_model = validate_model(merged["subtype"], merged["model_raw"])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    # The probed firmware lives in merged; CLI --firmware already won inside
    # to_subtype_attrs. Carry it into the optional-attr list when present so
    # the warehouse record reflects what was on the box at intake time.
    firmware_attr = merged.get("firmware_version")

    required = build_required_attributes(
        serial=merged["serial"],
        model=igs_model,
        owner=args.owner,
        date_start=date_start,
    )
    optional = list(
        iter_optional_attributes(
            firmware=firmware_attr,
            comment=args.comment,
            galvos=args.galvos,
        )
    )

    # Derive software_version from firmware in the Septentrio TOS style
    # (X.Y.Z → X.YZ, e.g. 5.7.0 → 5.70). The probe only surfaces firmware, so a
    # device that doesn't expose software separately gets it derived here — same
    # convention as `cfg update-device`. See firmware_to_software().
    if firmware_attr:
        software_value, sw_warn = firmware_to_software(firmware_attr)
        if sw_warn:
            print(f"  ⚠️  {sw_warn}", file=sys.stderr)
        optional.append(("software_version", software_value))

    constellation_attrs, note, note_is_warning = build_constellation_attrs(
        getattr(identity, "constellations", None),
        skip=getattr(args, "no_constellations", False),
    )
    optional.extend(constellation_attrs)
    if note:
        print(note, file=sys.stderr if note_is_warning else sys.stdout)

    # ---- Writer setup ---------------------------------------------------
    base_url = _tos_base_url(args)
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    # ---- Create entity --------------------------------------------------
    try:
        response = writer.create_device(merged["subtype"], required, force=args.force)
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg and not args.force:
            print(
                f"❌ {msg}\nPass --force to add the duplicate anyway.",
                file=sys.stderr,
            )
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 1

    id_entity = None
    if isinstance(response, dict):
        id_entity = response.get("id_entity")

    # ---- Location join (parent area entity → child device) --------------
    # Replaces the old "location as a free-text attribute on the device"
    # path; TOS conveys physical placement via entity_connection rows
    # (parent=area-entity, child=device). Without this join the device
    # is invisible to web UI pages that list "devices at <warehouse>".
    connection_result: Any
    if dry_run or id_entity is None:
        if not getattr(args, "json", False):
            print(
                f"DRY RUN: would resolve location {args.location!r} → "
                f"entity_id and create entity_connection(parent=<area>, "
                f"child={id_entity if id_entity is not None else '<new>'}, "
                f"time_from={date_start})"
            )
        connection_result = {"location": args.location, "dry_run": True}
    else:
        try:
            connection_result = writer.connect_device_to_location(
                id_device=id_entity,
                location_name=args.location,
                date_start=date_start,
            )
        except ValueError as e:
            print(
                f"❌ Device created (id_entity={id_entity}) but location "
                f"join failed: {e}",
                file=sys.stderr,
            )
            return 1

    # ---- Optional attribute upserts -------------------------------------
    upsert_results = []
    for code, value in optional:
        if dry_run or id_entity is None:
            print(
                f"DRY RUN: would upsert {code}={value!r} from {date_start} "
                f"on id_entity="
                f"{id_entity if id_entity is not None else '<new entity>'}"
            )
            upsert_results.append({"code": code, "value": value, "dry_run": True})
        else:
            r = writer.upsert_attribute_value(
                id_entity, code=code, value=value, date_from=date_start
            )
            upsert_results.append({"code": code, "value": value, "response": r})

    # ---- Summary --------------------------------------------------------
    probe_origin = (f"{host}:{port}" if port else host) or f"file:{from_file}"
    if args.json:
        payload = {
            "subtype": merged["subtype"],
            "serial": merged["serial"],
            "model": igs_model,
            "model_raw_from_probe": identity.model_raw,
            "owner": args.owner,
            "location": args.location,
            "date_start": date_start,
            "id_entity": id_entity,
            "dry_run": dry_run,
            "probe_type": identity.probe_type,
            "probe_origin": probe_origin,
            "required_attributes": required,
            "optional_attributes": [{"code": c, "value": v} for c, v in optional],
            "upsert_results": upsert_results,
            "location_connection": connection_result,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        suffix = " (dry-run)" if dry_run else ""
        id_str = id_entity if id_entity is not None else "<would be assigned>"
        print(
            f"Created {merged['subtype']} via probe={identity.probe_type} "
            f"@ {probe_origin}: serial={merged['serial']} model={igs_model} "
            f"id_entity={id_str}{suffix}"
        )
        if not dry_run and isinstance(connection_result, dict):
            conn_id = connection_result.get("id_connection")
            if conn_id is not None:
                print(
                    f"Connected to location {args.location!r} (connection id={conn_id})"
                )

    # ---- Triage-file substitution (parity with `tos device add`) ------------
    # Drop the freshly-created id_entity into a waiting join triage file so the
    # add-receiver → `tos audit apply` handoff needs no manual id copy-paste.
    triage = getattr(args, "triage", None)
    placeholder = getattr(args, "placeholder", None)
    if triage is not None or placeholder is not None:
        if triage is None or placeholder is None:
            print("--triage and --placeholder must be used together", file=sys.stderr)
            return 2
    if triage is not None and placeholder is not None:
        if dry_run or id_entity is None:
            if not args.json:
                print(
                    f"Triage update skipped: dry-run / no real id_entity "
                    f"(use --no-dry-run to substitute <{placeholder}> in {triage}).",
                    file=sys.stderr,
                )
        else:
            from tostools.tos import _substitute_id_in_triage

            try:
                result = _substitute_id_in_triage(triage, placeholder, id_entity)
            except OSError as exc:
                print(f"Could not read triage file {triage}: {exc}", file=sys.stderr)
                return 1
            if result["count"] == 0:
                print(
                    f"Triage update: placeholder {result['token']!r} not found "
                    f"in {triage} (no changes written).",
                    file=sys.stderr,
                )
            elif not args.json:
                n = result["count"]
                print(
                    f"Updated {triage}: {result['token']} → {id_entity} "
                    f"({n} replacement{'s' if n != 1 else ''})"
                )

    # ---- Creation audit log (gps-tos-corrections) ---------------------------
    # Symmetric with `tos device delete --commit`: record the creation so
    # additions leave a corrections-repo trail like deletions do.
    if getattr(args, "commit", False) and not dry_run and id_entity is not None:
        from tostools.tos import audit_log_device_creation

        audit = audit_log_device_creation(
            id_entity,
            subtype=merged["subtype"],
            serial=merged["serial"],
            model=igs_model,
            location=args.location,
            date_start=date_start,
            source="receivers cfg add-receiver",
            note=getattr(args, "note", None),
        )
        if audit.get("logged") and not args.json:
            print(f"--commit: creation logged → {audit.get('log')}")
    return 0


def _audit_log_op_creations(result, *, source: str, note) -> None:
    """Audit-log every device an add-antenna/add-monument op created (--commit).

    Reads the create responses + attribute lists the operation stored in
    ``result.tos_changes`` and appends one record per created device to
    ``additions/device_additions.jsonl`` via tostools ``audit_log_device_creation``
    (symmetric with ``cfg add-receiver --commit`` / ``tos device add --commit``).
    Best-effort; a logging/git failure warns but never fails the create.
    """
    from tostools.tos import audit_log_device_creation

    changes = result.tos_changes
    for create_key, attrs_key, subtype in (
        ("antenna_create", "antenna_attributes", "antenna"),
        ("radome_create", "radome_attributes", "radome"),
        ("monument_create", "monument_attributes", "monument"),
    ):
        resp = changes.get(create_key)
        if not isinstance(resp, dict):
            continue
        mid = resp.get("id_entity")
        if mid is None:
            continue
        attrs = changes.get(attrs_key) or []
        vals = {a.get("code"): a.get("value") for a in attrs if isinstance(a, dict)}
        audit = audit_log_device_creation(
            int(mid),
            subtype=subtype,
            serial=vals.get("serial_number"),
            model=vals.get("model"),
            date_start=vals.get("date_start"),
            source=source,
            note=note,
        )
        if audit.get("logged"):
            print(f"--commit: creation logged → dev {mid} ({subtype})")


# ---------------------------------------------------------------------------
# cmd_cfg_add_antenna — create an antenna (+radome) in TOS and join to a station
# ---------------------------------------------------------------------------


def cmd_cfg_add_antenna(args) -> int:
    """``cfg add-antenna`` — register a GNSS antenna in TOS and join it to a station.

    Antennas cannot be probed, so identity comes from CLI flags (and the radome,
    when present, becomes a second TOS device). Unknown antenna serials — common
    in practice — get a synthetic ``antenna-<STID>-<YYYYMMDD>`` placeholder
    (mirrors the radome convention). Delegates to
    :func:`receivers.cfg.operations.add_antenna`. Exit 0 on success, 1 on TOS
    write failure, 2 on input-validation failure.
    """
    import json as _json
    import sys

    from tostools.api.tos_writer import TOSWriter
    from tostools.owners import OwnersCache

    from ..cfg.operations import CfgOperationError, add_antenna

    # Before the owner gate and any TOS call — --list-models must work offline.
    if getattr(args, "list_models", False):
        return _print_igs_models()
    missing = _require_args_or_exit(args, ("model", "--model"))
    if missing is not None:
        return missing
    if not args.station and not args.warehouse:
        print(
            "❌ missing required argument(s): --station (install) or "
            "--warehouse (intake)",
            file=sys.stderr,
        )
        return 2

    owner = args.owner or "Jarðeðlismælihópur"

    # Owner gate — same OwnersCache check as add-receiver.
    owners_cache = (
        OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    )
    if owner not in owners_cache.load():
        print(
            f"❌ Unknown owner: {owner!r}. Run 'tos owners list' to see allowed "
            f"values, or 'tos owners list --refresh' if you recently added one.",
            file=sys.stderr,
        )
        return 2

    if not getattr(args, "antenna_height", None):
        print(
            "  ⚠️  no --antenna-height given: antenna created without a height; "
            "RINEX 'ANTENNA: DELTA H' will be 0.0 until corrected.",
            file=sys.stderr,
        )

    base_url = _tos_base_url(args)
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    try:
        result = add_antenna(
            writer,
            station_id=args.station,
            warehouse=args.warehouse,
            model=args.model,
            radome=args.radome,
            serial=args.serial,
            radome_serial=args.radome_serial,
            antenna_height=args.antenna_height,
            owner=owner,
            date_start=args.date_start,
            comment=args.comment,
            force=args.force,
            dry_run=dry_run,
        )
    except CfgOperationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg and not args.force:
            print(
                f"❌ {msg}\nPass --force to add the duplicate anyway.",
                file=sys.stderr,
            )
            return 1
        print(f"❌ {msg}", file=sys.stderr)
        return 2

    synthetic = bool(result.tos_changes.get("synthetic_serial"))
    has_radome = (args.radome or "NONE").upper() != "NONE"
    if args.json:
        payload = {
            "operation": result.operation,
            "station_id": result.station_id,
            "antenna_serial": result.serial,
            "synthetic_serial": synthetic,
            "model": args.model,
            "radome": args.radome,
            "radome_serial": result.tos_changes.get("radome_serial"),
            "antenna_height": args.antenna_height,
            "date_start": result.date,
            "owner": owner,
            "dry_run": result.dry_run,
            "tos_changes": result.tos_changes,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        suffix = " (dry-run)" if result.dry_run else ""
        synth = " [synthetic serial]" if synthetic else ""
        print(
            f"Antenna {args.model} @ {result.station_id}: serial={result.serial}"
            f"{synth} date_start={result.date}{suffix}"
        )
        if has_radome:
            print(
                f"  + radome {args.radome} "
                f"(serial={result.tos_changes.get('radome_serial')})"
            )
    if getattr(args, "commit", False) and not result.dry_run:
        _audit_log_op_creations(
            result, source="receivers cfg add-antenna", note=getattr(args, "note", None)
        )
    return 0


# ---------------------------------------------------------------------------
# cmd_cfg_add_monument — create a monument (survey mark) in TOS and join a station
# ---------------------------------------------------------------------------


def cmd_cfg_add_monument(args) -> int:
    """``cfg add-monument`` — register a survey monument in TOS, joined to a station.

    Monuments carry the ``antenna_height`` (mark → ARP) offset and have no model;
    an unknown serial gets a synthetic ``monument-<STID>-<YYYYMMDD>`` placeholder.
    Delegates to :func:`receivers.cfg.operations.add_monument`. Exit 0 on success,
    1 on TOS write failure, 2 on input-validation failure.
    """
    import json as _json
    import sys

    from tostools.api.tos_writer import TOSWriter
    from tostools.owners import OwnersCache

    from ..cfg.operations import CfgOperationError, add_monument

    owner = args.owner or "Jarðeðlismælihópur"

    owners_cache = (
        OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    )
    if owner not in owners_cache.load():
        print(
            f"❌ Unknown owner: {owner!r}. Run 'tos owners list' to see allowed "
            f"values, or 'tos owners list --refresh' if you recently added one.",
            file=sys.stderr,
        )
        return 2

    base_url = _tos_base_url(args)
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    try:
        result = add_monument(
            writer,
            station_id=args.station,
            warehouse=args.warehouse,
            height=args.height,
            serial=args.serial,
            owner=owner,
            date_start=args.date_start,
            comment=args.comment,
            model=getattr(args, "model", None),
            force=args.force,
            dry_run=dry_run,
        )
    except CfgOperationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg and not args.force:
            print(
                f"❌ {msg}\nPass --force to add the duplicate anyway.",
                file=sys.stderr,
            )
            return 1
        print(f"❌ {msg}", file=sys.stderr)
        return 2

    synthetic = bool(result.tos_changes.get("synthetic_serial"))
    if args.json:
        payload = {
            "operation": result.operation,
            "station_id": result.station_id,
            "monument_serial": result.serial,
            "synthetic_serial": synthetic,
            "height": args.height,
            "date_start": result.date,
            "owner": owner,
            "dry_run": result.dry_run,
            "tos_changes": result.tos_changes,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        suffix = " (dry-run)" if result.dry_run else ""
        synth = " [synthetic serial]" if synthetic else ""
        print(
            f"Monument @ {result.station_id}: serial={result.serial}{synth} "
            f"height={args.height} date_start={result.date}{suffix}"
        )
    if getattr(args, "commit", False) and not result.dry_run:
        _audit_log_op_creations(
            result,
            source="receivers cfg add-monument",
            note=getattr(args, "note", None),
        )
    return 0


# ---------------------------------------------------------------------------
# cmd_cfg_import_campaigns — station.info campaign occupations → TOS
# ---------------------------------------------------------------------------


def cmd_cfg_import_campaigns(args) -> int:
    """``cfg import-campaigns`` — import GAMIT station.info occupations into TOS.

    Creates closed receiver+antenna (and optional radome/monument) sessions for
    each historical campaign occupation. Metadata-only — no stations.cfg /
    download footprint. Delegates to
    :func:`receivers.cfg.operations.import_campaigns`. Exit 0 on success,
    1 on TOS write / precondition failure, 2 on input-validation failure.
    """
    import json as _json
    import sys

    from tostools.api.tos_writer import TOSWriter
    from tostools.owners import OwnersCache

    from ..cfg.operations import CfgOperationError, import_campaigns

    owner = args.owner or "Jarðeðlismælihópur"
    owners_cache = (
        OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    )
    if owner not in owners_cache.load():
        print(
            f"❌ Unknown owner: {owner!r}. Run 'tos owners list' to see allowed "
            f"values, or 'tos owners list --refresh' if you recently added one.",
            file=sys.stderr,
        )
        return 2

    base_url = _tos_base_url(args)
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    try:
        result = import_campaigns(
            writer,
            station_id=args.station,
            station_info_path=args.station_info,
            marker=args.marker,
            owner=owner,
            with_monument=args.with_monument,
            monument_height=args.monument_height,
            force=args.force,
            dry_run=dry_run,
        )
    except CfgOperationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    summary = result.tos_changes.get("summary", {})
    if args.json:
        payload = {
            "operation": result.operation,
            "station_id": result.station_id,
            "dry_run": result.dry_run,
            "tos_changes": result.tos_changes,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        suffix = " (dry-run)" if result.dry_run else ""
        print(
            f"Campaign import @ {result.station_id}: "
            f"{summary.get('total', 0)} occupation(s) — "
            f"{summary.get('created', 0)} created, "
            f"{summary.get('skipped', 0)} skipped{suffix}"
        )
        for occ in result.tos_changes.get("occupations", []):
            span = f"{occ.get('time_from')} → {occ.get('time_to') or 'open'}"
            if occ.get("status", "").startswith("skipped"):
                print(f"  · {span}: skipped (already present)")
                continue
            rx = occ.get("receiver", {})
            ant = occ.get("antenna", {})
            extras = []
            if "radome" in occ:
                extras.append("radome")
            if "monument" in occ:
                extras.append("monument")
            extra = f" +{'+'.join(extras)}" if extras else ""
            print(
                f"  · {span}: rx {rx.get('model')} ({rx.get('serial')}), "
                f"ant {ant.get('model')} ({ant.get('serial')}){extra}"
            )
    return 0


# ---------------------------------------------------------------------------
# cmd_cfg_set_continuity — transition a station's campaign/continuous tag
# ---------------------------------------------------------------------------


def cmd_cfg_set_continuity(args) -> int:
    """``cfg set-continuity`` — transition a station's continuity classification.

    Closes the open ``continuity`` period at ``--from`` and opens a new one with
    ``--campaign`` / ``--continuous``; ``--correct-current`` first relabels a
    mislabeled open period. Delegates to
    :func:`receivers.cfg.operations.set_continuity`. Exit 0 success, 1 TOS/
    precondition failure, 2 input-validation failure.
    """
    import json as _json
    import sys

    from tostools.api.tos_writer import TOSWriter

    from ..cfg.operations import CfgOperationError, set_continuity

    value = "continuous" if args.continuous else "campaign"

    base_url = _tos_base_url(args)
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    try:
        result = set_continuity(
            writer,
            station_id=args.station,
            from_date=getattr(args, "from"),
            value=value,
            correct_current=args.correct_current,
            dry_run=dry_run,
        )
    except CfgOperationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "operation": result.operation,
            "station_id": result.station_id,
            "value": value,
            "from": result.date,
            "correct_current": args.correct_current,
            "dry_run": result.dry_run,
            "tos_changes": result.tos_changes,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        suffix = " (dry-run)" if result.dry_run else ""
        corrected = (
            f" (relabelled prior period → {args.correct_current})"
            if args.correct_current
            else ""
        )
        print(
            f"Continuity @ {result.station_id}: → {value} from "
            f"{result.date}{corrected}{suffix}"
        )
    return 0


# ---------------------------------------------------------------------------
# cmd_cfg_add_station — scaffold a stations.cfg section from TOS (TOS → cfg)
# ---------------------------------------------------------------------------


def cmd_cfg_add_station(args) -> int:
    """``cfg add-station`` — create a stations.cfg section for a TOS station.

    Reads a station that already exists in TOS (its open device session) and
    materialises a ``[STID]`` section so the rek scheduler will monitor it.
    Inverse of the ``add-*`` device verbs. Delegates to
    :func:`receivers.cfg.operations.add_station`. Exit 0 success, 1 on TOS /
    precondition / section-exists failure.
    """
    import json as _json
    import sys

    from tostools.api.tos_client import TOSClient

    from ..cfg.operations import CfgOperationError, add_station

    dry_run = not args.no_dry_run

    try:
        global_target = _resolve_global_target(args)  # None unless --global
    except CfgOperationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    base_url = _tos_base_url(args)
    client = TOSClient(base_url=base_url)

    try:
        result = add_station(
            client,
            station_id=args.station,
            cfg_path=global_target,
            connection_type=args.connection_type,
            router_ip=args.router_ip,
            router_type=args.router_type,
            rinex_run_by=args.rinex_run_by,
            rinex_observer=args.rinex_observer,
            rinex_agency=args.rinex_agency,
            station_owner=args.station_owner,
            firmware=args.firmware,
            dry_run=dry_run,
        )
    except CfgOperationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        # Section already exists (create_station_section guard).
        print(f"❌ {e}", file=sys.stderr)
        return 1

    warnings = result.tos_changes.get("warnings", [])
    if args.json:
        print(
            _json.dumps(
                {
                    "operation": result.operation,
                    "station_id": result.station_id,
                    "dry_run": result.dry_run,
                    "section": result.cfg_changes,
                    "warnings": warnings,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        suffix = " (dry-run)" if result.dry_run else ""
        target = (
            "gps-config-data repo"
            if getattr(args, "global_cfg", False)
            else "deployed stations.cfg"
        )
        print(
            f"add-station {result.station_id}: {len(result.cfg_changes)} fields "
            f"→ {target}{suffix}\n"
        )
        print(f"[{result.station_id}]")
        for k, v in result.cfg_changes.items():
            print(f"{k} = {v}")
        if warnings:
            print()
            for w in warnings:
                print(f"⚠ {w}")

    _maybe_commit_global(
        args,
        f"feat: add {args.station} to monitoring (from TOS)",
        changed=not dry_run,
        dry_run=dry_run,
    )
    return 0


# ---------------------------------------------------------------------------
# cmd_cfg_discover_phone — reveal a router SIM's own phone number (MSISDN)
# ---------------------------------------------------------------------------


def cmd_cfg_discover_phone(args) -> int:
    """``cfg discover-phone`` — reveal a router SIM's own phone number (MSISDN).

    A SIM can't read its own number locally, so the field router texts a catcher
    number (``--to``) and the operator reads the sender off that phone. Sent via
    ``gsmctl`` over SSH — universal across Teltonika routers (works where the REST
    API is off, e.g. legacy RUT240). Outward-facing + costs a message; dry-run
    default. Exit 0 on success, 1 on send failure, 2 on input-validation failure.
    """
    import sys
    from datetime import date as _date

    from ..cfg.telemetry_probe import (
        ProbeError,
        query_ussd_ssh,
        resolve_discover_phone_to,
        send_sms_ssh,
    )

    host = args.host
    if not host and args.station:
        from ..config_utils import get_station_config

        cfg = get_station_config(args.station, silent=True)
        host = (cfg or {}).get("router_ip") or (cfg or {}).get("ip_address")
        if not host:
            print(
                f"❌ no router_ip for {args.station} in stations.cfg — pass --host",
                file=sys.stderr,
            )
            return 2
    if not host:
        print("❌ --host or --station is required", file=sys.stderr)
        return 2

    dry_run = not args.no_dry_run

    # USSD mode (--ussd CODE): query the network directly for the SIM's own number
    # — no catcher phone, no SMS delivery. More reliable when the modem's SMS
    # subsystem is flaky. The code is operator-specific.
    if args.ussd:
        try:
            result = query_ussd_ssh(
                host, args.ussd, password=args.password, dry_run=dry_run
            )
        except ProbeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if dry_run:
            print(
                f"DRY RUN: would run USSD {args.ussd!r} on {host} via "
                f"{result['cmd']}. Add --no-dry-run to query (a network request)."
            )
        else:
            resp = result.get("response")
            print(f"✅ USSD {args.ussd!r} on {host} →")
            print(
                f"   {resp}"
                if resp
                else "   (no reply — code may be wrong or USSD unsupported on this network)"
            )
        return 0

    to = args.to or resolve_discover_phone_to()
    if not to:
        print(
            "❌ --to <catcher mobile> is required (or set [teltonika] "
            "discover_phone_to in receivers.cfg)",
            file=sys.stderr,
        )
        return 2

    # Identify the source in the body so the received SMS says which station/SIM
    # it's from: '<marker> <ip> GPS SIM MSISDN discovery <date>'. --station labels
    # the message even when --host is given (they can be passed together).
    if args.message:
        message = args.message
    else:
        bits = []
        if args.station:
            bits.append(args.station)
        bits += [host, "GPS SIM MSISDN discovery", _date.today().isoformat()]
        message = " ".join(bits)

    try:
        result = send_sms_ssh(
            host, to, message, password=args.password, dry_run=dry_run
        )
    except ProbeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if dry_run:
        print(
            f"DRY RUN: would SSH to {host} and send SMS to {to} via "
            f"{result['cmd']}. Add --no-dry-run to send (costs a message)."
        )
    else:
        print(f"✅ SMS sent from {host} SIM to {to}.")
        print(
            f"   Check {to}: the SENDER number of that message is this SIM's own "
            f"phone number."
        )
    return 0


# ---------------------------------------------------------------------------
# cmd_cfg_update_device — probe an existing TOS device and update its attrs
# ---------------------------------------------------------------------------


def firmware_to_software(firmware: str) -> tuple[str, Optional[str]]:
    """Convert a firmware version to the TOS ``software_version`` style.

    Septentrio TOS records ``software_version`` as the firmware with the
    patch-dot dropped: firmware ``X.Y.Z`` → software ``X.YZ`` (e.g. ``5.7.0``
    → ``5.70``, matching the fleet's existing ``5.50``). Probes only expose
    ``firmware_version``, so when a device doesn't surface software separately
    we derive it from firmware in this style.

    Best-effort: a clean 3-part ``X.Y.Z`` (optionally with extra parts, which
    are appended dot-less too) converts cleanly. Anything that doesn't start
    with at least ``MAJOR.MINOR.PATCH`` numeric is passed through unchanged and
    a warning string is returned so the caller can surface it.

    Returns ``(software_version, warning_or_None)``.
    """
    parts = firmware.split(".")
    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
        # 5.7.0 -> 5.70 ; 5.7.0.1 -> 5.701 (drop every dot after the first)
        software = parts[0] + "." + "".join(parts[1:])
        return software, None
    return (
        firmware,
        f"firmware {firmware!r} is not a clean X.Y.Z version — passing it "
        f"through to software_version unchanged; override with the actual "
        f"value if the receiver uses a different software string.",
    )


def _create_update_vitjun(
    writer, device_eid, changed, field_values, when_iso, label, args
) -> None:
    """Create a maintenance visit (default Fjarvitjun) on the device's station.

    Resolves the station from the device's open parent join. Skips cleanly when
    the device is warehoused (parent is a ``Lager``) or has no open parent.
    Retries once on the intermittent maintenance-endpoint 401 — the attribute
    change has already landed, so a vitjun failure is a warning, not fatal.
    """
    try:
        join = writer.get_open_parent_join(device_eid)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  {label} skipped — couldn't resolve station: {e}", file=sys.stderr)
        return
    parent_eid = (join or {}).get("id_entity_parent")
    if not parent_eid:
        print(f"  ⚠️  {label} skipped — device has no open station parent")
        return
    try:
        sub = (writer.get_entity_history(parent_eid) or {}).get(
            "code_entity_subtype", ""
        )
    except Exception:  # noqa: BLE001
        sub = ""
    if isinstance(sub, str) and "lager" in sub.lower():
        print(f"  ⚠️  {label} skipped — device is in a warehouse, not at a station")
        return

    mtype = "remote" if args.visit_type == "remote" else "on_site"
    work = args.work or (
        "Uppfærði " + ", ".join(f"{c} í {field_values[c]}" for c in changed)
    )

    def _do():
        return writer.add_maintenance_visit(
            parent_eid,
            start_time=when_iso,
            maintenance_type=mtype,
            reasons=[args.reason],
            work=work,
            participants=args.participants or "",
        )

    try:
        res = _do()
    except Exception:  # noqa: BLE001 — intermittent endpoint 401; retry once
        try:
            res = _do()
        except Exception as e:  # noqa: BLE001
            print(
                f"  ⚠️  {label} create failed (attribute change already applied): {e}",
                file=sys.stderr,
            )
            return
    vid = (res.get("id_maintenance") or res.get("id")) if isinstance(res, dict) else res
    print(f"  ✓ {label} created (id={vid}): {work!r}")


def cmd_cfg_update_device(args) -> int:
    """Probe a receiver and update the matching TOS device entity's attribute(s).

    Use case: a receiver that's already in TOS (typically warehoused at B9 from
    an earlier `cfg add-receiver` intake) had something change off-network — e.g.
    firmware upgrade on the bench — and you want TOS to reflect the new value.

    A mutable attribute can be written for two fundamentally different reasons,
    and the operator MUST declare which — guessing wrong corrupts the temporal
    record in opposite directions, so there is no safe default:

    - **--change (real-world change → transition, Pattern 2)** — the value
      genuinely changed in the world (firmware upgrade, marker rename). Closes
      the open attribute period at ``--date`` and opens a new one from that
      date, so TOS remembers "ran fw 5.6.0 from install to 2026-05-30, fw 5.7.0
      from 2026-05-30 on". Records history.
    - **--correct (fix a wrong record → in-place, Pattern 1)** — the recorded
      value was simply *wrong* (a typo / bad earlier entry) and the real-world
      value did not change. Overwrites the open value, keeping its dates. Does
      NOT create a history period. Using this for a real upgrade would erase
      the upgrade history; using --change for a typo would invent an upgrade
      that never happened.

    No-op guard: if the probed value already matches the current open TOS value,
    nothing is written (avoids spurious same-value transitions).

    Dry-run by default. Does NOT touch stations.cfg — for that, use
    ``cfg reconcile <SID> --field receiver_firmware_version --source receiver``
    once the device is deployed.
    """
    from datetime import date as _date

    from tostools.api.tos_writer import TOSWriter

    from ..cfg.device_probe import (
        ProbeError,
        ProbeNotIdentifiedError,
        ProbeUnreachableError,
        StationProbeResolveError,
        parse_host_port,
        probe_receiver,
        resolve_station_probe,
    )

    if not args.field:
        print(
            "❌ --field is required (e.g. --field firmware_version)",
            file=sys.stderr,
        )
        return 2

    # ---- Resolve probe target (--probe HOST or --station SID) ------------
    if bool(args.probe) == bool(args.station):
        print(
            "❌ pass exactly one of --probe HOST[:PORT] or --station SID",
            file=sys.stderr,
        )
        return 2
    if args.station:
        try:
            args.probe = resolve_station_probe(args.station)
        except StationProbeResolveError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        print(f"Resolved {args.station} → {args.probe}")

    try:
        host, port = parse_host_port(args.probe)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    when_iso = args.date or _date.today().isoformat()

    # ---- Probe the receiver ---------------------------------------------
    print(f"Probing {args.probe} …")
    try:
        identity = probe_receiver(
            host,
            port,
            probe_type=args.probe_type,
            tcp_username=args.username,
            tcp_password=args.password,
            # With --station, let the extractor pull that station's stored
            # tcp_username/tcp_password from stations.cfg (so an authenticated
            # PolaRX5 probes without re-passing --username/--password).
            station_id_hint=args.station,
        )
    except (ProbeUnreachableError, ProbeNotIdentifiedError, ProbeError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if not identity.serial:
        print(
            "❌ probe did not return a serial — can't look up the TOS device",
            file=sys.stderr,
        )
        return 1
    print(
        f"  identity: serial={identity.serial!r} "
        f"model={identity.model_raw!r} fw={identity.firmware_version!r}"
    )

    # ---- Map requested fields → values from probe -----------------------
    # software_version is derived from the probed firmware (X.Y.Z → X.YZ) since
    # the probe only surfaces firmware_version; see firmware_to_software().
    software_value: Optional[str] = None
    if identity.firmware_version:
        software_value, sw_warn = firmware_to_software(identity.firmware_version)
        if sw_warn and "software_version" in args.field:
            print(f"  ⚠️  {sw_warn}", file=sys.stderr)

    field_sources: Dict[str, Optional[str]] = {
        "firmware_version": identity.firmware_version,
        "software_version": software_value,
        "model": identity.model_raw,
        "marker_name": identity.marker_name,
    }
    field_values: Dict[str, str] = {}
    for f in args.field:
        if f not in field_sources:
            print(
                f"❌ field {f!r} not supported by --probe "
                f"(supported: {sorted(field_sources)})",
                file=sys.stderr,
            )
            return 2
        val = field_sources[f]
        if not val:
            print(f"❌ probe didn't return a {f} value", file=sys.stderr)
            return 1
        field_values[f] = val

    # ---- Find the device in TOS -----------------------------------------
    dry_run = not args.no_dry_run
    writer = TOSWriter(dry_run=dry_run)

    device = writer.find_device_by_serial(args.subtype, identity.serial)
    if not device:
        print(
            f"❌ no TOS device of subtype {args.subtype!r} with serial "
            f"{identity.serial!r}",
            file=sys.stderr,
        )
        print(
            f"   If this is a brand-new intake, use `receivers cfg add-receiver "
            f"--probe {args.probe}` instead.",
            file=sys.stderr,
        )
        return 1
    id_entity = device.get("id_entity") if isinstance(device, dict) else None
    if not id_entity:
        print(
            f"❌ TOS lookup returned a device without id_entity: {device!r}",
            file=sys.stderr,
        )
        return 1

    # Exactly one of --change / --correct is set (argparse required mutex group).
    in_place = bool(args.correct)
    mode = (
        "--correct → Pattern 1 (in-place upsert, no history)"
        if in_place
        else "--change → Pattern 2 (transition, records history)"
    )
    print(f"  TOS device: id_entity={id_entity}")
    print(f"  Mode: {mode}")
    print(f"  Date: {when_iso}")
    if dry_run:
        print("  DRY RUN — no writes (use --no-dry-run to commit)")

    def _current_open_value(code: str) -> Optional[str]:
        """Return the value of the currently-open (date_to=None) period, or None."""
        try:
            rows = writer.get_attribute_values(id_entity, code=code)
        except Exception:  # noqa: BLE001 — read failure shouldn't block the write
            return None
        if not isinstance(rows, list):
            return None
        open_rows = [r for r in rows if isinstance(r, dict) and not r.get("date_to")]
        if not open_rows:
            return None
        # If multiple open rows somehow exist, the last is the most recent.
        return open_rows[-1].get("value")

    # ---- Apply each field -----------------------------------------------
    rc = 0
    changed: list[str] = []
    for code, new_value in field_values.items():
        current = _current_open_value(code)
        if current is not None and str(current) == str(new_value):
            print(f"  = {code} already {new_value!r} — no change")
            continue
        arrow = (
            f"{current!r} → {new_value!r}" if current is not None else f"{new_value!r}"
        )
        try:
            if in_place:
                writer.upsert_attribute_value(id_entity, code, new_value, when_iso)
            else:
                writer.transition_attribute_value(id_entity, code, new_value, when_iso)
            print(f"  ✓ {code}: {arrow}")
            changed.append(code)
        except Exception as e:  # noqa: BLE001 — surface any TOS write error
            # TOS's public attribute_value endpoint sometimes COMMITS the write
            # but still returns 401 "invalid token" (a known server quirk). Don't
            # trust the exception alone — re-read and check whether it landed.
            if not dry_run and str(_current_open_value(code)) == str(new_value):
                print(f"  ✓ {code}: {arrow}  (committed despite {type(e).__name__})")
                changed.append(code)
            else:
                print(f"  ❌ {code}: {e}", file=sys.stderr)
                rc = 1

    # ---- Auto-vitjun: a real --change is a maintenance event ------------
    # Default to a Fjarvitjun (remote) — firmware/marker updates are done
    # remotely. Skipped for --correct (fixing a record is not a field event)
    # and for --no-vitjun. Mirrors the vitjun replace-modem/replace-receiver
    # write for a hardware swap, just remote-by-default.
    want_vitjun = (not in_place) and (not args.no_vitjun) and bool(changed)
    label = "Fjarvitjun" if args.visit_type == "remote" else "Staðarvitjun"
    if want_vitjun and not dry_run:
        _create_update_vitjun(
            writer, id_entity, changed, field_values, when_iso, label, args
        )
    elif want_vitjun and dry_run:
        print(f"  (would also create a {label} on the station — --no-vitjun to skip)")

    return rc


# ---------------------------------------------------------------------------
# sync-from-tos: bring stations.cfg in line with current TOS device state
# ---------------------------------------------------------------------------

# Surveyed coordinates: stations.cfg / survey is the ground truth — never
# overwrite from TOS (flag-only). antenna_height/east/north are non-writable
# composites already excluded by the tos_writable filter.
_SYNC_POSITION_EXCLUDE = frozenset({"latitude", "longitude", "height"})


def _sync_overwrite_specs():
    """FieldSpecs sync-from-tos overwrites: TOS-canonical-for-cfg fields.

    Marked declaratively with ``sync_from_tos=True`` in the manifest:
    receiver type/serial/firmware, antenna type/serial/radome, station_name,
    and rinex_marker_number (IERS DOMES). This is deliberately NOT the
    ``tos_writable`` set — DOMES is TOS-canonical for cfg (TOS→cfg) yet must
    never be pushed cfg→TOS, so it carries no ``tos_attribute_code``. Surveyed
    position stays cfg-canonical and is excluded via ``sync_from_tos=False``.
    """
    from ..cfg.field_manifest import FIELDS

    return [s for s in FIELDS if s.sync_from_tos]


def _is_literal_ip(value) -> bool:
    import ipaddress

    if not value:
        return False
    try:
        ipaddress.ip_address(str(value).strip())
        return True
    except ValueError:
        return False


def _plan_router_ip(cfg_ip, sim_ip):
    """Decide what to do with ``router_ip`` given the cfg value + TOS SIM ip.

    Returns ``("set", ip)`` to overwrite, ``("flag", (cfg, sim))`` to report-only,
    or ``None`` (in sync / nothing to do):

    * cfg missing             → ``("set", sim)``  (fill)
    * cfg literal IP, drifted → ``("set", sim)``  (a pinned IP fell out of date)
    * cfg hostname, mismatch  → ``("flag", …)``   (a stale hostname is a DNS fix, not
                                                   a cfg edit — never auto-clobber)
    * equal / no SIM ip       → ``None``
    """
    if not sim_ip:
        return None
    sim_ip = str(sim_ip).strip()
    cfg_ip = (str(cfg_ip).strip() if cfg_ip else "") or None
    if cfg_ip is None:
        return ("set", sim_ip)
    if _is_literal_ip(cfg_ip):
        return ("set", sim_ip) if cfg_ip != sim_ip else None
    return ("flag", (cfg_ip, sim_ip)) if cfg_ip != sim_ip else None


def _fetch_sim_ip(station_id: str):
    """Current TOS ``sim_card`` ``ip_address`` for a station, or ``None``.

    Walks the station's open children to the sim_card (~N TOS round-trips) —
    SINGLE station only; far too many calls for ``--all``.
    """
    try:
        from tostools.api.tos_writer import TOSWriter

        from ..cfg.operations import (
            _device_attribute,
            _find_open_child,
            _resolve_station,
        )

        writer = TOSWriter(dry_run=True)
        eid = _resolve_station(writer, station_id)
        sim_eid = _find_open_child(writer, eid, "sim_card")
        if sim_eid is None:
            return None
        hist = writer.get_entity_history(sim_eid)
        if not isinstance(hist, dict):
            return None
        return _device_attribute(hist, "ip_address")
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync-from-tos: SIM ip lookup failed for %s: %s", station_id, exc)
        return None


def _local_stations_cfg_path() -> Path:
    import gps_parser as _gps  # type: ignore

    return Path(_gps.ConfigParser().get_stations_config_path())


def cmd_cfg_sync_from_tos(args) -> int:
    """Overwrite stations.cfg device fields from current TOS state (TOS-authoritative)."""
    from tostools.rinex import is_iers_domes

    from ..cfg.reconciler import apply_diff, compare_station
    from ..config.receivers_config import _remove_cfg_field, _update_cfg_field
    from ..config_utils import get_station_config
    from .arguments import normalize_station_tokens

    # GUARDRAIL: never bulk-apply blindly. TOS is not yet the single source of
    # truth — stale/legacy records exist (e.g. an unclosed decade-old receiver
    # join), so a no-review bulk overwrite can clobber correct cfg. --all is a
    # report/per-station sweep; the dangerous --all + --yes combo is refused.
    if args.all and args.yes:
        print(
            "❌ Refusing --all with --yes. TOS can be stale (errors surface from "
            "anywhere), so a blind bulk overwrite can clobber correct cfg.\n"
            "   Review `cfg sync-from-tos --all` (dry-run), then apply the ones you "
            "trust per-station: `cfg sync-from-tos <SID> --no-dry-run`."
        )
        return 1

    if args.all:
        station_ids = _all_station_ids()
        include_sim = False  # SIM lookup is ~N TOS calls/station — too slow for --all
    else:
        station_ids = normalize_station_tokens(args.station)
        include_sim = not args.no_sim
    if not station_ids:
        print("Specify one or more station markers, or --all.")
        return 1

    dry = _is_dry_run(args)
    try:
        global_target = _resolve_global_target(args)  # preflight; None unless --global
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}")
        return 1

    overwrite_keys = {s.cfg_key for s in _sync_overwrite_specs()}

    # --only: restrict the sync to a subset of fields (e.g. just the IERS DOMES).
    # router_ip is a valid target even though it comes from the SIM, not a spec.
    only = getattr(args, "only", None)
    only_set = None
    if only:
        valid_keys = overwrite_keys | {"router_ip"}
        unknown = [f for f in only if f not in valid_keys]
        if unknown:
            print(
                f"❌ Unknown --only field(s): {', '.join(unknown)}.\n"
                f"   Valid sync fields: {', '.join(sorted(valid_keys))}"
            )
            return 1
        only_set = set(only)
        overwrite_keys &= only_set
        if "router_ip" not in only_set:
            include_sim = False  # skip the SIM lookup entirely when not selected

    # local always; also the gps-config-data repo when --global.
    write_targets = [None] + ([global_target] if global_target is not None else [])

    any_global_change = False
    for sid in station_ids:
        station_config = get_station_config(sid, silent=True)
        if not station_config:
            print(f"⏭️  {sid}: no stations.cfg section — skipping")
            continue
        tos_data = _query_tos(sid)
        if not tos_data:
            print(f"⏭️  {sid}: no TOS record — skipping")
            continue

        diffs = compare_station(
            sid,
            station_config,
            receiver_identity=None,  # TOS-only: no live receiver probe
            tos_data=tos_data,
            queried_sources={"cfg", "tos"},
        )
        planned = [
            d
            for d in diffs
            if d.cfg_key in overwrite_keys
            and d.tos_value is not None
            and d.needs_attention
        ]
        flagged = [
            d
            for d in diffs
            if d.cfg_key in _SYNC_POSITION_EXCLUDE
            and d.needs_attention
            and d.tos_value is not None
            # under --only, suppress flags for non-selected fields (e.g. position)
            and (only_set is None or d.cfg_key in only_set)
        ]

        # SIM -> router_ip (single-station only)
        sim_overwrite = None  # literal IP to write
        sim_flag = None  # (cfg_hostname, tos_ip) — reported, not changed
        if include_sim:
            plan = _plan_router_ip(station_config.get("router_ip"), _fetch_sim_ip(sid))
            if plan and plan[0] == "set":
                sim_overwrite = plan[1]
            elif plan and plan[0] == "flag":
                sim_flag = plan[1]

        # rinex_marker_number strip: a cfg value that is NOT a valid IERS DOMES is
        # invalid as a MARKER NUMBER by definition (MARKER NAME already carries the
        # 4-char id). Remove the line — but only when TOS has no real DOMES to fill
        # in its place (that fill is already in `planned`), and NEVER strip a cfg
        # value that IS a real DOMES that TOS momentarily lacks (the "absent TOS
        # never clobbers cfg" rule). Scoped by --only like every other field.
        strip_marker = False
        if only_set is None or "rinex_marker_number" in only_set:
            _cur_mn = (station_config.get("rinex_marker_number") or "").strip()
            _tos_fills = any(d.cfg_key == "rinex_marker_number" for d in planned)
            if _cur_mn and not is_iers_domes(_cur_mn) and not _tos_fills:
                strip_marker = True

        if (
            not planned
            and sim_overwrite is None
            and not flagged
            and sim_flag is None
            and not strip_marker
        ):
            print(f"✅ {sid}: already in sync with TOS")
            continue

        print(f"━━ {sid} ━━")
        for d in planned:
            print(
                f"  → {d.cfg_key}: {d.cfg_value!r} → {d.spec.cfg_format(d.tos_value)!r}"
            )
        if sim_overwrite is not None:
            cur = station_config.get("router_ip") or "(missing)"
            print(f"  → router_ip: {cur!r} → {sim_overwrite!r}  (TOS SIM)")
        for d in flagged:
            print(
                f"  ⚠ {d.cfg_key}: cfg={d.cfg_value!r} TOS={d.tos_value!r} — flag only "
                "(survey is ground truth; not changed)"
            )
        if sim_flag is not None:
            print(
                f"  ⚠ router_ip: cfg hostname {sim_flag[0]!r} vs TOS SIM {sim_flag[1]!r}"
                " — not changed (fix DNS or pin the IP manually)"
            )
        if strip_marker:
            print(
                f"  ✂ rinex_marker_number: {station_config.get('rinex_marker_number')!r}"
                " → (removed — not an IERS DOMES)"
            )

        if dry or (not planned and sim_overwrite is None and not strip_marker):
            continue
        if not args.yes:
            n = (
                len(planned)
                + (1 if sim_overwrite is not None else 0)
                + (1 if strip_marker else 0)
            )
            if input(f"  Apply {n} change(s) to {sid}? [y/N] ").strip().lower() != "y":
                print(f"  skipped {sid}")
                continue

        for d in planned:
            new_val = d.spec.cfg_format(d.tos_value)
            if new_val is None:
                continue
            for tgt in write_targets:
                if apply_diff(sid, d, new_val, cfg_path=tgt) and tgt is not None:
                    any_global_change = True
            print(f"  ✓ {d.cfg_key} = {new_val!r}")
        if sim_overwrite is not None:
            for tgt in write_targets:
                path = tgt if tgt is not None else _local_stations_cfg_path()
                if _update_cfg_field(path, sid, "router_ip", sim_overwrite) and (
                    tgt is not None
                ):
                    any_global_change = True
            print(f"  ✓ router_ip = {sim_overwrite!r}")
        if strip_marker:
            for tgt in write_targets:
                path = tgt if tgt is not None else _local_stations_cfg_path()
                if _remove_cfg_field(path, sid, "rinex_marker_number") and (
                    tgt is not None
                ):
                    any_global_change = True
            print("  ✓ rinex_marker_number removed (not an IERS DOMES)")

    if dry:
        print(
            "\n[DRY-RUN] — review each change above: TOS is not yet authoritative, "
            "so a shown overwrite may be STALE TOS, not a real update.\n"
            "Rerun with --no-dry-run to apply the ones you trust."
        )
        _maybe_commit_global(
            args, "stations: sync-from-tos", changed=False, dry_run=True
        )
    else:
        scope = f" ({', '.join(only)})" if only else " (TOS→cfg device fields)"
        _maybe_commit_global(
            args,
            f"stations: sync-from-tos{scope}",
            changed=any_global_change,
            dry_run=False,
        )
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def create_cfg_parser(subparsers) -> None:
    """Register the ``cfg`` subcommand on the main parser."""
    cfg_parser = subparsers.add_parser(
        "cfg",
        help="Manage stations.cfg (reconcile, audit)",
        description=(
            "Three-way reconciliation between stations.cfg, the live "
            "receiver, and TOS. The intended workflow is TOS → cfg with "
            "the receiver as a validation source."
        ),
    )
    cfg_subparsers = cfg_parser.add_subparsers(
        dest="cfg_command", help="cfg subcommands"
    )

    rec = cfg_subparsers.add_parser(
        "reconcile",
        help="Compare cfg vs receiver vs TOS and (optionally) update cfg",
        description=(
            "For each station, query the requested sources, compare against "
            "stations.cfg, and present discrepancies. Default is interactive "
            "review per field; --auto-fill writes missing values where "
            "sources agree."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg reconcile ELDC
  receivers cfg reconcile ELDC THOB --source tos
  receivers cfg reconcile --open                   # QC: review all known open issues
  receivers cfg reconcile --open --dry-run --json  # machine-readable health report
  receivers cfg reconcile --all --auto-fill --field receiver_serial
  receivers cfg reconcile --all --dry-run --json
  receivers cfg reconcile --list-fields

Fixing firmware notation mismatches (e.g. 'NP 4.81 / SP 4.81' → '4.81'):
  receivers cfg reconcile --all --field receiver_firmware_version \\
      --source receiver --canonicalize --dry-run   # preview
  receivers cfg reconcile --all --field receiver_firmware_version \\
      --source receiver --canonicalize             # apply

Diagnosing TCP authentication failures:
  If health checks log "TCP command denied: receiver requires authentication"
  for a PolaRX5 station, the most common cause is a stale
  receiver_firmware_version in stations.cfg (e.g. recorded as 5.2.0 while
  the receiver has been upgraded to 5.7.0+).  The TCP login command was
  introduced in firmware 5.7.0; if stations.cfg records an older version the
  health probe skips sending credentials and the receiver rejects subsequent
  commands.  Fix with:
    receivers cfg reconcile <SID> --field receiver_firmware_version
  or in bulk (accepts receiver-reported version without prompting):
    receivers cfg reconcile --all --yes --field receiver_firmware_version \\
        --source receiver --dry-run   # preview first
    receivers cfg reconcile --all --yes --field receiver_firmware_version \\
        --source receiver             # then apply
        """,
    )
    rec.add_argument(
        "station",
        nargs="*",
        metavar="SID",
        help="Station IDs to reconcile (4-letter markers)",
    )
    rec.add_argument(
        "--all",
        action="store_true",
        help="Reconcile every station in stations.cfg",
    )
    rec.add_argument(
        "--open",
        action="store_true",
        help=(
            "Reconcile only stations with open discrepancies in the log "
            "(faster than --all; implies --only-diffs). "
            "Fields default to those with open discrepancies unless --field is given."
        ),
    )
    rec.add_argument(
        "--source",
        choices=["receiver", "tos", "both"],
        default="both",
        help="Which source(s) to compare against (default: both)",
    )
    rec.add_argument(
        "--field",
        nargs="+",
        metavar="KEY",
        help="Restrict reconciliation to specific cfg keys",
    )
    rec.add_argument(
        "--auto-fill",
        action="store_true",
        help="Auto-write missing cfg values where sources agree (no prompt)",
    )
    rec.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Accept all suggested values without prompting (use with care)",
    )
    rec.add_argument(
        "--canonicalize",
        action="store_true",
        help=(
            "Rewrite cfg values that are logically correct but stored in a "
            "different notation than the receiver uses (e.g. "
            "'NP 4.81 / SP 4.81' → '4.81', '4.8.1' → '4.81'). "
            "Only touches verdict=OK fields where raw cfg ≠ receiver value. "
            "Combine with --field receiver_firmware_version and --source receiver "
            "to canonicalize firmware notation across all stations."
        ),
    )
    rec.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show diffs but never write to stations.cfg",
    )
    rec.add_argument(
        "--only-diffs",
        action="store_true",
        help="Only print fields that need attention (hide OK rows)",
    )
    rec.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of tables; implies no interactive prompts",
    )
    rec.add_argument(
        "--list-fields",
        action="store_true",
        help="List reconcilable fields and exit",
    )
    rec.add_argument(
        "--position-tolerance-m",
        type=float,
        default=2.0,
        metavar="METERS",
        help=(
            "Tolerance for receiver↔TOS position comparison (default: 2.0 m). "
            "Receiver coordinates come from a real-time PVT solution and are "
            "used as a sanity check that the receiver is at the expected mark."
        ),
    )
    rec.add_argument(
        "--position-abort-m",
        type=float,
        default=50.0,
        metavar="METERS",
        help=(
            "Position discrepancy threshold above which receiver-primary "
            "auto-push (type/serial/firmware → TOS) is blocked for that "
            "station (default: 50.0 m). A large position delta suggests the "
            "receiver may be at the wrong station or TOS coordinates are "
            "corrupt — hardware identity should not be written automatically "
            "in that case. A warning is printed even in dry-run mode."
        ),
    )
    rec.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    rec.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help=(
            "Parallel probe workers when reconciling multiple stations "
            "(default: 8; 0=auto=min(8,N); 1=sequential). "
            "Only the network probe phase is parallelised — writes and "
            "interactive prompts always run sequentially."
        ),
    )
    rec.add_argument(
        "--push-tos",
        action="store_true",
        help=(
            "For each field that has a receiver-reported value and is TOS-writable, "
            "push the receiver value to TOS (in addition to — or instead of — writing "
            "to stations.cfg). Requires --source to include 'tos'. "
            "Combine with --field to target specific fields (e.g. "
            "--field receiver_firmware_version --push-tos --yes). "
            "Honours --dry-run. "
            "By default, value changes use Pattern 2 (close old period + open new) "
            "to preserve history; use --no-transition to revert to Pattern 1 "
            "(overwrite open value in place)."
        ),
    )
    rec.add_argument(
        "--no-transition",
        action="store_true",
        default=False,
        help=(
            "When used with --push-tos, overwrite the open TOS value in place "
            "(Pattern 1) instead of closing the old period and opening a new one "
            "(Pattern 2). Use when the value change is a correction (e.g. typo fix) "
            "rather than a genuine instrument change."
        ),
    )
    rec.add_argument(
        "--sync-devices",
        action="store_true",
        default=False,
        help=(
            "When used with --push-tos, also create device entities in TOS "
            "(gnss_receiver, antenna, radome) for any station whose health data "
            "references a serial number not yet registered in TOS. "
            "Requires --push-tos and --source to include 'tos'. "
            "Honours --dry-run."
        ),
    )
    rec.add_argument(
        "--no-receiver-primary",
        action="store_true",
        default=False,
        help=(
            "Disable receiver-primary mode for hardware-identity fields "
            "(receiver_type, receiver_serial, receiver_firmware_version). "
            "By default, conflicts on these fields show a single combined "
            "'accept receiver → cfg + TOS' action as the default choice. "
            "Use this flag to revert to the traditional per-action prompt "
            "for the rare cases where the receiver value is not trustworthy "
            "(e.g. just-provisioned hardware with stale factory defaults)."
        ),
    )
    rec.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help=(
            "Force full interactive prompt for every field, including "
            "receiver-primary fields (type/serial/firmware) that would "
            "otherwise auto-accept the receiver value. Implies "
            "--no-receiver-primary. Useful for testing and manual review."
        ),
    )
    rec.add_argument(
        "--effective-date",
        metavar="ISO8601",
        help=(
            "date_from timestamp for TOS attribute writes (ISO-8601, e.g. "
            "2025-03-15T00:00:00+00:00). Defaults to the current UTC time when "
            "omitted — correct for write-now corrections; use an explicit date "
            "for historical fixes (e.g. the actual firmware upgrade date)."
        ),
    )
    _add_global_flags(rec)
    rec.set_defaults(func=cmd_cfg_reconcile)

    # ----- cfg sync-from-tos ---------------------------------------------
    sync = cfg_subparsers.add_parser(
        "sync-from-tos",
        help="Overwrite stations.cfg device fields from current TOS state",
        description=(
            "Bring stations.cfg in line with TOS after a device change is recorded "
            "there. Overwrites TOS-canonical fields (receiver type/serial/firmware, "
            "antenna type/serial/radome, station_name, rinex_marker_number ← IERS "
            "DOMES) and a single station's SIM router_ip; surveyed position stays "
            "cfg-canonical (flag-only). A field is filled only when TOS actually "
            "carries a value — an absent TOS DOMES never clobbers cfg. No receiver "
            "probe — TOS is the source. Default is dry-run."
        ),
    )
    sync.add_argument("station", nargs="*", help="Station marker(s); omit with --all")
    sync.add_argument(
        "--all",
        action="store_true",
        help="Every station in stations.cfg (skips the per-station SIM lookup)",
    )
    sync.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply without the per-station confirm prompt",
    )
    sync.add_argument(
        "--no-dry-run", action="store_true", help="Apply writes (default: dry-run)"
    )
    sync.add_argument(
        "--no-sim",
        action="store_true",
        help="Skip the SIM/router_ip lookup (single-station)",
    )
    sync.add_argument(
        "--only",
        nargs="+",
        metavar="FIELD",
        help=(
            "Restrict the sync to these cfg field(s) instead of every "
            "sync_from_tos field — e.g. `--only rinex_marker_number` to fill just "
            "the IERS DOMES. Run `cfg reconcile --list-fields` for the field names."
        ),
    )
    _add_global_flags(sync)
    sync.set_defaults(func=cmd_cfg_sync_from_tos)

    # ----- cfg list -------------------------------------------------------
    lst = cfg_subparsers.add_parser(
        "list",
        help="List currently-open cfg discrepancies",
        description=(
            "Show every discrepancy that has been detected and not yet "
            "resolved. Filter by station, field, or verdict."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg list
  receivers cfg list ELDC THOB
  receivers cfg list --field receiver_serial receiver_firmware_version
  receivers cfg list --verdict conflict --json
        """,
    )
    lst.add_argument(
        "station",
        nargs="*",
        metavar="SID",
        help="Restrict to specific station IDs",
    )
    lst.add_argument(
        "--field",
        nargs="+",
        metavar="KEY",
        help="Restrict to specific cfg keys",
    )
    lst.add_argument(
        "--verdict",
        nargs="+",
        choices=[
            Verdict.MISSING.value,
            Verdict.CONFLICT.value,
            Verdict.SOURCES_DISAGREE.value,
        ],
        help="Restrict to specific verdicts (default: all open)",
    )
    lst.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    lst.set_defaults(func=cmd_cfg_list)

    # ----- cfg history ----------------------------------------------------
    hist = cfg_subparsers.add_parser(
        "history",
        help="Show the detection/resolution history for a station or field",
        description=(
            "Print every cfg_discrepancy row matching the given station "
            "and/or field, including resolved rows. At least one of "
            "<SID> or --field is required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg history ELDC
  receivers cfg history ELDC --field receiver_serial
  receivers cfg history --field receiver_firmware_version --since 30d
  receivers cfg history ELDC --json
        """,
    )
    hist.add_argument(
        "station",
        nargs="*",
        metavar="SID",
        help="Station ID to inspect (one)",
    )
    hist.add_argument(
        "--field",
        nargs="+",
        metavar="KEY",
        help="Restrict to specific cfg keys",
    )
    hist.add_argument(
        "--since",
        metavar="WHEN",
        help="Only rows detected on/after WHEN (e.g. 30d, 12h, 2026-04-01)",
    )
    hist.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum rows to return (default 200)",
    )
    hist.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a table"
    )
    hist.add_argument(
        "-v", "--verbose", action="store_true", help="Show resolution notes"
    )
    hist.set_defaults(func=cmd_cfg_history)

    ext = cfg_subparsers.add_parser(
        "extract",
        help="Probe a receiver and add a new station section to stations.cfg",
        description=(
            "Connect to a physical receiver, read its identity (model, serial, firmware), "
            "antenna configuration, and PVT position, then append a new [STATIONID] section "
            "to stations.cfg. Use --host for bench/direct connections when the station does "
            "not yet exist in stations.cfg."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg extract HEKR --host 192.168.20.1          # bench (WiFi AP)
  receivers cfg extract HEKR --host 192.168.3.1           # bench (USB)
  receivers cfg extract HEKR --host 192.168.20.1 --dry-run
""",
    )
    ext.add_argument("station_id", help="4-letter station ID (e.g. HEKR)")
    ext.add_argument(
        "--host",
        metavar="IP",
        help="Direct IP address to connect to (required for new stations not in stations.cfg)",
    )
    ext.add_argument(
        "--dry-run",
        action="store_true",
        help="Show extracted fields without writing to stations.cfg",
    )
    ext.add_argument(
        "--receiver-type",
        default="PolaRX5",
        metavar="TYPE",
        help="Receiver type hint for --host mode (default: PolaRX5)",
    )
    ext.set_defaults(func=cmd_cfg_extract)

    # ------------------------------------------------------------------
    # add-tos-station — greenfield orchestrator (TOS site+station + cfg)
    # ------------------------------------------------------------------
    ats = cfg_subparsers.add_parser(
        "add-tos-station",
        help="Probe a receiver, create its TOS site + station, and write stations.cfg",
        description=(
            "Greenfield-station orchestrator: collapse onboarding steps 0 and 1 into "
            "one command. Probes the receiver once for identity + PVT position, hands "
            "the coordinates to `tos station add` (which find-or-creates the land "
            "site, creates the station entity and joins them), then writes the "
            "matching [STID] stations.cfg section from the same probe. "
            "This ordering matters: `cfg move-device --to STATION` fills the TOS "
            "position attributes FROM stations.cfg, so the cfg section must exist "
            "before the receiver is installed or that fill silently no-ops. "
            "Dry-run by default in both halves — a created land site is NOT "
            "deletable in TOS, so preview before committing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview everything (no TOS write, no cfg write):
  receivers cfg add-tos-station NPSK --probe 10.6.1.71 --name "Núpskatla" \\
      --date-start 2026-08-08

  # Commit both halves:
  receivers cfg add-tos-station NPSK --probe 10.6.1.71 --name "Núpskatla" \\
      --date-start 2026-08-08 --no-dry-run

  # Surveyed coordinates instead of the receiver's PVT fix:
  receivers cfg add-tos-station NPSK --probe 10.6.1.71 --name "Núpskatla" \\
      --date-start 2026-08-08 --lat 66.2 --lon -16.1 --height 12.3

  # Colocated site that already exists in TOS under its own name:
  receivers cfg add-tos-station NPSK --probe 10.6.1.71 --name "Núpskatla" \\
      --site-name "Núpskatla SIL stöð" --date-start 2026-08-08

  # TOS records only / cfg section only:
  receivers cfg add-tos-station NPSK --probe … --name … --date-start … --no-cfg
  receivers cfg add-tos-station NPSK --probe … --name … --date-start … --no-tos

Reuse the SAME --date-start on the following add-receiver / move-device /
add-antenna / add-monument steps — a mismatched instant splits the TOS session
and every RINEX header comes out incomplete.
""",
    )
    ats.add_argument("station_id", help="4-char station marker (e.g. NPSK)")
    ats.add_argument(
        "--probe",
        metavar="IP",
        help="Receiver IP to probe for identity + position (station need not exist yet)",
    )
    ats.add_argument(
        "--name",
        required=True,
        help="Station name, e.g. 'Núpskatla' (also the default land-site name)",
    )
    ats.add_argument(
        "--date-start",
        required=True,
        metavar="DATE",
        help=(
            "Station start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Reuse this "
            "exact string on every co-installed device."
        ),
    )
    ats.add_argument(
        "--site-name",
        metavar="NAME",
        help=(
            "Land-site name to find-or-create (default: --name). Use when the site "
            "already exists in TOS under a different spelling, e.g. a colocated SIL "
            "station — the match is exact and case-sensitive."
        ),
    )
    ats.add_argument(
        "--continuity",
        default="continuous",
        help="Measurement continuity (default: continuous)",
    )
    ats.add_argument("--subtype", help="Station kind attribute (TOS catalog default)")
    ats.add_argument("--operational-class", help="Operational class (TOS default: B)")
    ats.add_argument(
        "--in-network-epos",
        metavar="ja|nei",
        help="In the EPOS network (TOS default: nei)",
    )
    ats.add_argument(
        "--lat",
        help="Override the probed latitude with a surveyed value (decimal degrees)",
    )
    ats.add_argument(
        "--lon",
        help="Override the probed longitude with a surveyed value (decimal degrees)",
    )
    ats.add_argument(
        "--height", help="Override the probed height with a surveyed value (metres)"
    )
    ats.add_argument(
        "--receiver-type",
        default="PolaRX5",
        metavar="TYPE",
        help="Receiver type hint for the probe (default: PolaRX5)",
    )
    ats.add_argument(
        "--control-port",
        type=int,
        metavar="PORT",
        help="Receiver SBF control port to probe (default: 28784)",
    )
    ats.add_argument(
        "--no-cfg",
        action="store_true",
        help="Create the TOS records only — skip the stations.cfg section",
    )
    ats.add_argument(
        "--no-tos",
        action="store_true",
        help="Write the stations.cfg section only — skip the TOS records",
    )
    ats.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit both halves; without this flag nothing is written",
    )
    ats.add_argument("--server", help="TOS API host (default: vi-api.vedur.is)")
    ats.add_argument("--port", type=int, help="TOS API port")
    ats.add_argument(
        "--json", action="store_true", help="Emit a structured JSON summary"
    )
    ats.set_defaults(func=cmd_cfg_add_tos_station)

    # ------------------------------------------------------------------
    # add-receiver — warehouse intake via tostools.device (step 6)
    # ------------------------------------------------------------------
    from ..cfg.device_probe import PROBE_TYPE_CHOICES

    add_rx = cfg_subparsers.add_parser(
        "add-receiver",
        help="Probe a receiver and register it as a new device in TOS",
        description=(
            "Connect to a receiver at the given IP, auto-extract identity "
            "(serial/model/firmware) from SBF block 5902 (PolaRX5) or the "
            "vendor HTTP interface, IGS-normalise the model, and call "
            "tostools.device.create_device. Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # PolaRX5 bench intake — relies on defaults for owner + location +
  # date-start (Jarðeðlismælihópur, 'B9 - Kjallari - Jörð', today):
  receivers cfg add-receiver --probe 192.168.3.1

  # Same, committing live (overrides the dry-run default):
  receivers cfg add-receiver --probe 192.168.3.1 --no-dry-run

  # Non-standard warehouse — override --location:
  receivers cfg add-receiver --probe 192.168.20.1 \\
      --location "Vagnhöfði - Kjallari - Jörð"

  # Backdating an intake that physically happened earlier:
  receivers cfg add-receiver --probe 192.168.3.1 --date-start 2026-05-12

  # Trimble NetR9 at a deployed station:
  receivers cfg add-receiver --probe 10.20.30.40 --probe-type netr9 \\
      --location "Reykjavík warehouse"

  # Leica G10 — probe only confirms reachability, serial must be supplied:
  receivers cfg add-receiver --probe 10.20.30.41 --probe-type g10 \\
      --serial G10-12345
""",
    )
    add_rx.add_argument(
        "--probe",
        metavar="HOST[:PORT]",
        help=(
            "Receiver host/IP, optionally with explicit port. Mutually "
            "exclusive with --station/--from-file; one of the three is required."
        ),
    )
    add_rx.add_argument(
        "--station",
        metavar="SID",
        help=(
            "Deployed station ID — resolves router_ip:receiver_controlport from "
            "stations.cfg (disambiguates port-forwarded stations sharing a "
            "router IP). Mutually exclusive with --probe/--from-file."
        ),
    )
    add_rx.add_argument(
        "--from-file",
        dest="from_file",
        metavar="PATH",
        help=(
            "Load receiver identity (serial, model, firmware_version, "
            "probe_type, marker_name) from a YAML file instead of probing. "
            "The file may also default owner/location/date_start/station_hint/"
            "comment/galvos/firmware. CLI args override file values when "
            "both are given. Mutually exclusive with --probe. See "
            "~/.cache/gps_receivers/intake/<serial>.yaml for the canonical "
            "shape — captured offline so the intake can be written later "
            "without USB access to the receiver."
        ),
    )
    add_rx.add_argument(
        "--probe-type",
        choices=PROBE_TYPE_CHOICES,
        default="auto",
        help="Receiver protocol; 'auto' tries PolaRX5 only (default).",
    )
    add_rx.add_argument(
        "--station-hint",
        metavar="STID",
        help="Optional 4-char marker hint for logging (e.g. BENCH).",
    )
    add_rx.add_argument(
        "--owner",
        help=(
            "Owner label; must match the tostools OwnersCache. Defaults "
            "to 'Jarðeðlismælihópur' (the IMO Geophysical Measurements "
            "Group, which owns the GPS receiver fleet — matches the "
            "owner attribute on every existing open child of B9 - "
            "Kjallari - Jörð) when neither CLI nor --from-file supplies "
            "a value. Override via CLI or via the `owner` key in "
            "--from-file when the device belongs to a different group "
            "(e.g. 'Vatnamælihópur', 'ÍSOR')."
        ),
    )
    add_rx.add_argument(
        "--location",
        # No argparse default — applied in cmd_cfg_add_receiver AFTER the
        # --from-file merge so a `location:` key in the file isn't silently
        # overridden by the CLI fallback.
        help=(
            "Physical warehouse / bench location. "
            "Default (when neither CLI nor --from-file supplies one): "
            "'B9 - Kjallari - Jörð' — the standard bench-intake location for "
            "the GPS receiver fleet. Override for other warehouses "
            "(e.g. 'Vagnhöfði - Kjallari - Jörð', 'Ísafjörður')."
        ),
    )
    add_rx.add_argument(
        "--date-start",
        metavar="YYYY-MM-DD",
        # Same reasoning as --location: applied post-merge so file values
        # win over the today-fallback.
        help=(
            "Start date for all attribute values. "
            "Default (when neither CLI nor --from-file supplies one): today "
            "(YYYY-MM-DD). Override when registering a device whose intake "
            "actually happened on a different day."
        ),
    )
    add_rx.add_argument(
        "--firmware",
        help="Override the probed firmware_version (optional attribute).",
    )
    add_rx.add_argument("--comment", help="Optional free-form comment attribute.")
    add_rx.add_argument(
        "--galvos", help="Optional galvos (inventory/registration) number."
    )
    add_rx.add_argument(
        "--no-constellations",
        action="store_true",
        help=(
            "Do not write the constellation toggles (GPS/GLO/GAL/BDS/…) read "
            "from the receiver's PVT solution. Only systems actually in the "
            "solution are ever written, and only as 'true'."
        ),
    )
    add_rx.add_argument(
        "--serial",
        help="Override the probed serial (required for G10).",
    )
    add_rx.add_argument(
        "--model",
        help="Override the probed model (required for G10).",
    )
    add_rx.add_argument(
        "--force",
        action="store_true",
        help="Bypass the tostools duplicate-serial guard.",
    )
    add_rx.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    add_rx.add_argument(
        "--owners-cache",
        help="Override the tostools owners.yaml path.",
    )
    add_rx.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    add_rx.add_argument("--port", type=int, default=443)
    add_rx.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    add_rx.add_argument(
        "--triage",
        default=None,
        help=(
            "After a live (--no-dry-run) creation, substitute the returned "
            "id_entity into a triage file in-place (parity with `tos device "
            "add`). Requires --placeholder. Resolves cwd-safely under the "
            "gps-tos-corrections repo, e.g. --triage bald/bald_join.txt."
        ),
    )
    add_rx.add_argument(
        "--placeholder",
        default=None,
        help=(
            "Token to substitute in the --triage file; the match is the "
            "angle-bracketed form '<TOKEN>'. Required when --triage is given."
        ),
    )
    add_rx.add_argument(
        "--commit",
        action="store_true",
        help=(
            "After a live creation, append a record to "
            "additions/device_additions.jsonl in gps-tos-corrections and "
            "git-commit it (audit trail, symmetric with deletions/). "
            "Best-effort; never fails the create."
        ),
    )
    add_rx.add_argument(
        "--note",
        default=None,
        help="Free-text reason stored in the --commit creation record.",
    )
    add_rx.set_defaults(func=cmd_cfg_add_receiver)

    # ---- add-antenna -----------------------------------------------------
    add_ant = cfg_subparsers.add_parser(
        "add-antenna",
        help="Register a GNSS antenna (and radome) in TOS and join it to a station",
        description=(
            "Create an 'antenna' device entity in TOS and join it to a station. "
            "Antennas cannot be probed, so identity is supplied via flags. When "
            "--radome is not NONE a separate 'radome' device is created and "
            "joined too. Unknown antenna serials get a synthetic "
            "'antenna-<STID>-<YYYYMMDD>' placeholder (mirrors the radome "
            "convention). Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # SEY9 antenna, serial unknown (synthetic), date defaults to the station's:
  receivers cfg add-antenna --station SEY9 --model SEPPOLANT_X_MF

  # Commit live, with a known ARP height and explicit install date:
  receivers cfg add-antenna --station SEY9 --model SEPPOLANT_X_MF \\
      --antenna-height 0.0083 --date-start 2021-03-25 --no-dry-run

  # Choke-ring with a radome and a real serial:
  receivers cfg add-antenna --station REYK --model LEIAR25.R4 \\
      --radome LEIT --serial 725281 --no-dry-run

  # Which model names are accepted? (offline, no TOS call)
  receivers cfg add-antenna --list-models
""",
    )
    _add_list_models_flag(add_ant, what="antenna and radome")
    add_ant_dest = add_ant.add_mutually_exclusive_group()
    add_ant_dest.add_argument(
        "--station",
        metavar="STID",
        help="4-char station marker to install the antenna at (must exist in TOS).",
    )
    add_ant_dest.add_argument(
        "--warehouse",
        metavar="LOCATION",
        help=(
            "WAREHOUSE INTAKE instead of a station install: register the "
            "antenna (and its radome) against this location, e.g. "
            '"B9 - Kjallari - Jörð". The antenna arrives with the radome '
            "screwed on, so both are taken in together. `cfg replace-antenna` "
            "later matches them BY SERIAL and reparents them to the station, "
            "so --serial (and --radome-serial) are required here."
        ),
    )
    add_ant.add_argument(
        "--model",
        help="Antenna model (IGS name or known alias, e.g. SEPPOLANT_X_MF).",
    )
    add_ant.add_argument(
        "--radome",
        default="NONE",
        help="Radome IGS code (default: NONE → no radome device created).",
    )
    add_ant.add_argument(
        "--radome-serial",
        dest="radome_serial",
        help=(
            "Radome serial. Omit at a station for the fleet convention (a "
            "synthetic 'radome-<STID>-<YYYYMMDD>'); REQUIRED with --warehouse "
            "alongside --radome, since a synthetic serial cannot be matched at "
            "install time."
        ),
    )
    add_ant.add_argument(
        "--serial",
        help=(
            "Antenna serial. Omit when unknown — a synthetic "
            "'antenna-<STID>-<YYYYMMDD>' placeholder is generated."
        ),
    )
    add_ant.add_argument(
        "--antenna-height",
        dest="antenna_height",
        metavar="METRES",
        help=(
            "Antenna ARP height in metres (RINEX 'ANTENNA: DELTA H'). Omit if "
            "unknown — the antenna is created without it (DELTA H defaults to 0.0)."
        ),
    )
    add_ant.add_argument(
        "--owner",
        help=(
            "Owner label; must match the tostools OwnersCache. Defaults to "
            "'Jarðeðlismælihópur' (the IMO Geophysical Measurements Group)."
        ),
    )
    add_ant.add_argument(
        "--date-start",
        dest="date_start",
        metavar="YYYY-MM-DD",
        help=(
            "Install date. Bare YYYY-MM-DD → noon, matching `cfg move-device`, "
            "so passing the SAME date to both lands the antenna and receiver in "
            "one TOS session (else the stream SKL drops one of them). Defaults to "
            "the station's own TOS date_start, then to today."
        ),
    )
    add_ant.add_argument(
        "--comment",
        help="Optional comment attribute (auto-set to a note when serial is synthetic).",
    )
    add_ant.add_argument(
        "--force",
        action="store_true",
        help="Bypass the one-open-antenna-per-station and duplicate-serial guards.",
    )
    add_ant.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    add_ant.add_argument(
        "--owners-cache",
        help="Override the tostools owners.yaml path.",
    )
    add_ant.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    add_ant.add_argument("--port", type=int, default=443)
    add_ant.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    add_ant.add_argument(
        "--commit",
        action="store_true",
        help=(
            "After a live creation, append a record per created device "
            "(antenna + radome) to additions/device_additions.jsonl in "
            "gps-tos-corrections and git-commit it (audit trail, symmetric with "
            "deletions/). Best-effort; never fails the create."
        ),
    )
    add_ant.add_argument(
        "--note",
        default=None,
        help="Free-text reason stored in the --commit creation record(s).",
    )
    add_ant.set_defaults(func=cmd_cfg_add_antenna)

    # ---- add-monument ----------------------------------------------------
    add_mon = cfg_subparsers.add_parser(
        "add-monument",
        help="Register a survey monument in TOS and join it to a station",
        description=(
            "Create a 'monument' device entity in TOS and join it to a station. "
            "The monument carries the antenna_height (mark → ARP) offset; TOS "
            "keeps one per height epoch. Monuments can't be probed — an unknown "
            "serial gets a synthetic 'monument-<STID>-<YYYYMMDD>' placeholder, "
            "and the mark type goes in --model (free text). Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # VOTT monument, flush mount (height 0.0), serial synthetic, date defaults:
  receivers cfg add-monument --station VOTT --height 0.0

  # Commit live with an explicit epoch date:
  receivers cfg add-monument --station VOTT --height 0.0 \\
      --date-start 2026-05-01T00:00:00 --no-dry-run
""",
    )
    add_mon.add_argument(
        "--station",
        metavar="STID",
        help="4-char station marker to install the monument at (must exist in TOS).",
    )
    add_mon.add_argument(
        "--warehouse",
        metavar="LOCATION",
        help=(
            "WAREHOUSE INTAKE instead of a station install: register the "
            'monument against this location, e.g. "B9 - Kjallari - Jörð". '
            "Mirrors `add-antenna --warehouse`. --serial is REQUIRED here: the "
            "synthetic monument-<STID>-<date> placeholder needs a station, and "
            "`move-device --subtype monument` matches the unit by serial when "
            "you later install it. Not every mark can be warehoused — a pillar "
            "cast in place goes straight to --station."
        ),
    )
    add_mon.add_argument(
        "--height",
        default="0.0",
        metavar="METRES",
        help="Mark → ARP antenna_height in metres (default: 0.0).",
    )
    add_mon.add_argument(
        "--serial",
        help=(
            "Monument serial. Omit when unknown — a synthetic "
            "'monument-<STID>-<YYYYMMDD>' placeholder is generated."
        ),
    )
    add_mon.add_argument(
        "--model",
        help=(
            "Physical mark type (free text), e.g. 'GPS stál-fjórfótur' (steel "
            "quadripod), 'GPS stál-staur' (steel post), 'steyptur stöpull' "
            "(concrete pillar). Omit when the mark type is unknown."
        ),
    )
    add_mon.add_argument(
        "--owner",
        help=(
            "Owner label; must match the tostools OwnersCache. Defaults to "
            "'Jarðeðlismælihópur'."
        ),
    )
    add_mon.add_argument(
        "--date-start",
        dest="date_start",
        metavar="YYYY-MM-DD",
        help=(
            "Install/epoch date. Bare YYYY-MM-DD → noon, matching `cfg "
            "move-device`. Defaults to the station's own TOS date_start, then today."
        ),
    )
    add_mon.add_argument(
        "--comment",
        help="Optional comment attribute (auto-set when serial is synthetic).",
    )
    add_mon.add_argument(
        "--force",
        action="store_true",
        help="Bypass the one-open-monument-per-station and duplicate-serial guards.",
    )
    add_mon.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    add_mon.add_argument(
        "--owners-cache",
        help="Override the tostools owners.yaml path.",
    )
    add_mon.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    add_mon.add_argument("--port", type=int, default=443)
    add_mon.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    add_mon.add_argument(
        "--commit",
        action="store_true",
        help=(
            "After a live creation, append a record to "
            "additions/device_additions.jsonl in gps-tos-corrections and "
            "git-commit it (audit trail, symmetric with deletions/). "
            "Best-effort; never fails the create."
        ),
    )
    add_mon.add_argument(
        "--note",
        default=None,
        help="Free-text reason stored in the --commit creation record.",
    )
    add_mon.set_defaults(func=cmd_cfg_add_monument)

    # ---- import-campaigns ------------------------------------------------
    imp = cfg_subparsers.add_parser(
        "import-campaigns",
        help="Import GAMIT station.info campaign occupations into TOS",
        description=(
            "Read a station's historical occupations from a GAMIT "
            "station.info file and create one CLOSED receiver+antenna "
            "session per occupation in TOS (plus radome when the dome is "
            "not NONE, and a monument with --with-monument). Campaign "
            "occupations are metadata-only — NO stations.cfg / download / "
            "monitoring footprint is created. Idempotent: an occupation "
            "already present (by receiver serial + start date) is skipped. "
            "Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview VOTT's campaign occupations from the SOPAC station.info:
  receivers cfg import-campaigns --station VOTT \\
      --station-info ~/git/tostools/data/station_config/station.info.sopac.apr05

  # Commit live:
  receivers cfg import-campaigns --station VOTT \\
      --station-info path/to/station.info.sopac.apr05 --no-dry-run

  # A campaign that used a tripod over the benchmark → record monuments too:
  receivers cfg import-campaigns --station XXXX \\
      --station-info path/to/station.info --with-monument --no-dry-run
""",
    )
    imp.add_argument(
        "--station",
        required=True,
        metavar="STID",
        help="4-char station marker to attach the occupations to (must exist in TOS).",
    )
    imp.add_argument(
        "--station-info",
        dest="station_info",
        required=True,
        metavar="PATH",
        help="Path to a GAMIT station.info file (SOPAC format).",
    )
    imp.add_argument(
        "--marker",
        help="station.info marker to read (default: same as --station).",
    )
    imp.add_argument(
        "--with-monument",
        dest="with_monument",
        action="store_true",
        help=(
            "Create a monument per occupation — use when the campaign sat on a "
            "tripod over a benchmark (the tripod is recorded as a monument). "
            "Off by default: a campaign needs no monument when the antenna was "
            "mounted directly on the benchmark (DHARP)."
        ),
    )
    imp.add_argument(
        "--monument-height",
        dest="monument_height",
        metavar="METRES",
        help="Mark→ARP height for --with-monument (default: occupation's antenna height).",
    )
    imp.add_argument(
        "--owner",
        help="Owner label (must match the TOS OwnersCache). Default: Jarðeðlismælihópur.",
    )
    imp.add_argument(
        "--force",
        action="store_true",
        help="Bypass duplicate-serial and already-present-session guards.",
    )
    imp.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    imp.add_argument("--owners-cache", help="Override the tostools owners.yaml path.")
    imp.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    imp.add_argument("--port", type=int, default=443)
    imp.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    imp.set_defaults(func=cmd_cfg_import_campaigns)

    # ---- set-continuity --------------------------------------------------
    cont = cfg_subparsers.add_parser(
        "set-continuity",
        help="Transition a station's campaign/continuous continuity tag",
        description=(
            "Close the station's currently-open 'continuity' period at --from "
            "and open a new one (campaign or continuous), preserving history. "
            "Continuity is fluid — a station can move campaign→continuous and "
            "back over time. Use --correct-current to first relabel a "
            "mislabeled open period (e.g. a station created 'continuous' from a "
            "date that was actually 'campaign'). Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Mark VOTT continuous from its permanent install date:
  receivers cfg set-continuity --station VOTT --from 2017-06-01 --continuous

  # Fix VOTT (created 'continuous' from 2012 when 2012→install was 'campaign'):
  receivers cfg set-continuity --station VOTT --from 2017-06-01 \\
      --continuous --correct-current campaign --no-dry-run

  # Return a decommissioned continuous station to campaign use:
  receivers cfg set-continuity --station XXXX --from 2026-01-01 --campaign --no-dry-run
""",
    )
    cont.add_argument(
        "--station",
        required=True,
        metavar="STID",
        help="4-char station marker (must exist in TOS).",
    )
    cont.add_argument(
        "--from",
        dest="from",
        required=True,
        metavar="YYYY-MM-DD",
        help="Transition date. Bare YYYY-MM-DD → noon, matching the other verbs.",
    )
    group = cont.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--campaign",
        action="store_true",
        help="Set continuity to 'campaign' from --from.",
    )
    group.add_argument(
        "--continuous",
        action="store_true",
        help="Set continuity to 'continuous' from --from.",
    )
    cont.add_argument(
        "--correct-current",
        dest="correct_current",
        choices=("campaign", "continuous"),
        help="Relabel the currently-open period to this value before transitioning.",
    )
    cont.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    cont.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    cont.add_argument("--port", type=int, default=443)
    cont.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    cont.set_defaults(func=cmd_cfg_set_continuity)

    # ---- add-station -----------------------------------------------------
    addst = cfg_subparsers.add_parser(
        "add-station",
        help="Create a stations.cfg section for a station that exists in TOS",
        description=(
            "Materialise a [STID] stations.cfg section from TOS so the rek "
            "scheduler will monitor the station. The INVERSE of the add-* "
            "device verbs (those write a probed device TO TOS; this reads TOS "
            "INTO stations.cfg). TOS-sourced fields reuse the cfg-reconcile "
            "backend (receiver/antenna/radome/position/name); the SIM's IP and "
            "modem model fill router_ip/router_type; ports/connection_type have "
            "no TOS source and are type-defaulted/flagged. Non-IGS antenna names "
            "and zero antenna heights are copied through WITH A WARNING. Writes "
            "the deployed config by default; --global writes the gps-config-data "
            "repo. Defaults to dry-run (prints the section it would write)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview the [VOTT] section add-station would generate from TOS:
  receivers cfg add-station VOTT

  # Write it to the deployed stations.cfg:
  receivers cfg add-station VOTT --no-dry-run

  # Finalize into gps-config-data (source of truth) and push → syncs to rek:
  receivers cfg add-station VOTT --global --push --no-dry-run

  # Override fields TOS can't supply:
  receivers cfg add-station VOTT --connection-type IP,radio --router-ip 10.4.2.163
""",
    )
    addst.add_argument(
        "station",
        metavar="STID",
        help="4-char station marker (must already exist in TOS with an open session).",
    )
    addst.add_argument(
        "--connection-type",
        dest="connection_type",
        default="3G-radio",
        help="cfg connection_type (no TOS source; default: 3G-radio).",
    )
    addst.add_argument(
        "--router-ip",
        dest="router_ip",
        help="Override the SIM-derived router IP (else read from the TOS SIM card).",
    )
    addst.add_argument(
        "--router-type",
        dest="router_type",
        help="Override the modem-derived router type (else from the TOS modem model).",
    )
    addst.add_argument(
        "--firmware",
        help="Receiver firmware for cfg (TOS often lacks it for older units).",
    )
    addst.add_argument("--rinex-run-by", dest="rinex_run_by", default="IMO")
    addst.add_argument("--rinex-observer", dest="rinex_observer", default="IMO")
    addst.add_argument("--rinex-agency", dest="rinex_agency", default="IMO")
    addst.add_argument("--station-owner", dest="station_owner", default="IMO")
    addst.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Write the section; without this flag, the section is printed only.",
    )
    _add_global_flags(addst)
    addst.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    addst.add_argument("--port", type=int, default=443)
    addst.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    addst.set_defaults(func=cmd_cfg_add_station)

    # ---- discover-phone --------------------------------------------------
    disc = cfg_subparsers.add_parser(
        "discover-phone",
        help="Reveal a router SIM's own phone number by texting a catcher mobile",
        description=(
            "A SIM can't read its own MSISDN locally, so the field router texts a "
            "catcher number (--to) and you read the sender off that phone. Sent "
            "via gsmctl over SSH — works on every Teltonika router, including "
            "legacy units whose REST API is off (e.g. a RUT240). Outward-facing "
            "and costs a message; defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # VOTT (router not in cfg) — text your mobile to learn the SIM's number:
  receivers cfg discover-phone --host 10.4.2.163 --to +3548XXXXXX --no-dry-run

  # By station marker (router_ip from stations.cfg):
  receivers cfg discover-phone --station GSIG --to +3548XXXXXX --no-dry-run
""",
    )
    disc.add_argument("--host", help="Router IP/hostname (or use --station).")
    disc.add_argument(
        "--station", help="Resolve the router IP from stations.cfg instead of --host."
    )
    disc.add_argument(
        "--to",
        help=(
            "Catcher mobile that receives the SMS (digits, optional leading +). "
            "Falls back to [teltonika] discover_phone_to in receivers.cfg."
        ),
    )
    disc.add_argument(
        "--message",
        help="SMS body (default: '<marker> <ip> GPS SIM MSISDN discovery <date>').",
    )
    disc.add_argument(
        "--ussd",
        metavar="CODE",
        help=(
            "USSD-fallback mode: query the network directly for the SIM's own "
            "number with this operator-specific USSD code (gsmctl -U), instead of "
            "the SMS catcher. No --to needed; the reply is printed. Use when SMS "
            "is flaky."
        ),
    )
    disc.add_argument(
        "--password",
        help="Router SSH password override (default: resolved from [teltonika] config).",
    )
    disc.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually send the SMS (costs a message); without this, dry-run only.",
    )
    disc.set_defaults(func=cmd_cfg_discover_phone)

    # ---- update-device ---------------------------------------------------
    upd = cfg_subparsers.add_parser(
        "update-device",
        help="Probe a receiver and update its TOS device attribute(s)",
        description=(
            "Update an already-in-TOS device entity's attribute(s) by probing "
            "the live receiver for the current value. Use case: firmware "
            "upgrade on a bench/warehoused receiver — the device entity "
            "already exists (from a prior `add-receiver`) and needs the new "
            "value reflected in TOS. You MUST declare intent with exactly one "
            "of --change (the value really changed → records history via a new "
            "attribute period) or --correct (the recorded value was wrong → "
            "overwrite in place, no history). There is no default: choosing "
            "wrong corrupts the temporal record. No-op when the value already "
            "matches."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # A firmware upgrade really happened — record it as history (close old fw
  # period, open a new one). Dry-run by default:
  receivers cfg update-device --probe 192.168.3.1 --field firmware_version --change

  # Same, committing live:
  receivers cfg update-device --probe 192.168.3.1 --field firmware_version \\
      --change --no-dry-run

  # Back-date the upgrade to when it actually happened:
  receivers cfg update-device --probe 192.168.3.1 --field firmware_version \\
      --change --date 2026-05-28 --no-dry-run

  # Multiple changed fields at once:
  receivers cfg update-device --probe 192.168.3.1 \\
      --field firmware_version --field marker_name --change --no-dry-run

  # The recorded value was a typo — CORRECT it in place (no history entry):
  receivers cfg update-device --probe 192.168.3.1 \\
      --field firmware_version --correct --no-dry-run
""",
    )
    upd.add_argument(
        "--probe",
        metavar="HOST[:PORT]",
        help="Receiver IP/host (with optional port). Mutually exclusive with "
        "--station; one of the two is required.",
    )
    upd.add_argument(
        "--station",
        metavar="SID",
        help="Deployed station ID — resolves router_ip:receiver_controlport from "
        "stations.cfg (handles port-forwarded stations sharing a router IP). "
        "Mutually exclusive with --probe.",
    )
    upd.add_argument(
        "--probe-type",
        choices=PROBE_TYPE_CHOICES,
        default="auto",
        help="Receiver protocol; 'auto' tries PolaRX5 only (default).",
    )
    upd.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="CODE",
        help=(
            "Attribute code to update; repeatable. Supported: "
            "firmware_version, software_version, model, marker_name. "
            "software_version is derived from the probed firmware "
            "(X.Y.Z → X.YZ, e.g. 5.7.0 → 5.70) since probes don't expose it "
            "separately."
        ),
    )
    upd.add_argument(
        "--subtype",
        default="gnss_receiver",
        help="TOS subtype to search for. Default: gnss_receiver.",
    )
    upd.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help=(
            "Effective date of the change. Default: today. This is the "
            "transition date — the old attribute period is closed at this "
            "date and the new one opens from it. Back-date it to when the "
            "upgrade actually happened."
        ),
    )
    # Intent is mandatory and exclusive — a mutable attribute write is either a
    # real-world change (transition, records history) or a correction of a wrong
    # record (in-place, no history). No safe default; the operator must declare.
    intent = upd.add_mutually_exclusive_group(required=True)
    intent.add_argument(
        "--change",
        action="store_true",
        help=(
            "The value genuinely CHANGED in the real world (firmware upgrade, "
            "marker rename). Pattern 2 transition: close the open attribute "
            "period at --date and open a new one — records history."
        ),
    )
    intent.add_argument(
        "--correct",
        action="store_true",
        help=(
            "The recorded value was WRONG (typo / bad earlier entry) and the "
            "real value did not change. Pattern 1 in-place upsert: overwrite "
            "the open value keeping its dates — no history entry."
        ),
    )
    upd.add_argument(
        "--username",
        help=(
            "TCP login username, override receivers.cfg [polarx5] tcp_username. "
            "Use when the bench receiver has different credentials than the "
            "deployed fleet (e.g. brand-new unit still on TEST creds, or "
            "newly upgraded firmware where the fleet default doesn't yet "
            "match)."
        ),
    )
    upd.add_argument(
        "--password",
        help=(
            "TCP login password, override receivers.cfg [polarx5] tcp_password. "
            "Pair with --username."
        ),
    )
    upd.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    # Auto-vitjun for a committed --change (a real maintenance event).
    upd.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        default="",
        help="Participant emails for the auto-created vitjun (--change only).",
    )
    upd.add_argument(
        "--visit-type",
        dest="visit_type",
        choices=("remote", "onsite"),
        default="remote",
        help="Auto-vitjun type — 'remote' (Fjarvitjun, default; firmware/marker "
        "updates are remote) or 'onsite' (Staðarvitjun).",
    )
    upd.add_argument(
        "--reason",
        choices=("change", "repairs", "inspection", "improvements", "other"),
        default="change",
        help="Reason for the auto-vitjun (default: change).",
    )
    upd.add_argument(
        "--work",
        metavar="TEXT",
        help="Override the auto-derived vitjun work text "
        "(default: 'Uppfærði <field> í <value>').",
    )
    upd.add_argument(
        "--no-vitjun",
        dest="no_vitjun",
        action="store_true",
        help="Don't auto-create a vitjun for the change.",
    )
    upd.set_defaults(func=cmd_cfg_update_device)

    # ---- move-device (unified: station OR warehouse target) -------------
    move = cfg_subparsers.add_parser(
        "move-device",
        help="Move a receiver to a station OR a warehouse — TOS Pattern 2",
        description=(
            "Move a receiver between parent entities in TOS. The "
            "destination is auto-detected from --to: a 4-char station "
            "marker triggers the full station-install workflow "
            "(TOS join move + Breyting vitjun on the destination with "
            "auto-derived 'Skipt um móttakara' text + stations.cfg "
            "update); any other value is treated as a warehouse name "
            "and runs the bookkeeping-only path (move + optional "
            "vitjun on the source station, no stations.cfg). Refuses "
            "to install onto a station that already has an open "
            "gnss_receiver child — move the old receiver out first."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Move HRAC's old receiver back to B9 warehouse (default target):
  receivers cfg move-device --serial 5545R50370 --date 2026-05-21T23:00:00

  # Transfer HRAC's old receiver directly to SAVI (station→station):
  receivers cfg move-device --serial 5545R50370 --to SAVI \\
      --from-station HRAC --date 2026-05-23

  # Install a warehoused receiver at HRAC:
  receivers cfg move-device --serial 4101524 --to HRAC \\
      --date 2026-05-21T23:00:00 --participants bgo@vedur.is

  # Override the auto-generated vitjun text:
  receivers cfg move-device --serial 4101524 --to HRAC --date 2026-05-23 \\
      --vitjun "Skipt um móttakara og lagaði loftnetskapal"

  # Move to a non-default warehouse:
  receivers cfg move-device --serial XYZ --to "Reykjavík - tæknibílskúr"
""",
    )
    move.add_argument(
        "--subtype",
        default="gnss_receiver",
        help=(
            "TOS device subtype to move (gnss_receiver, antenna, radome, "
            "monument, modem_gsm, sim_card …). Default: gnss_receiver. Any "
            "device can be moved — including one already retired (no open "
            "parent), which is how you file a unit into the warehouse after "
            "the fact."
        ),
    )
    move.add_argument(
        "--id",
        dest="id_entity",
        type=int,
        metavar="ID_ENTITY",
        help=(
            "Address the device by TOS id_entity instead of --serial. EXACT: "
            "--serial goes through TOS's fuzzy /basic_search/, which has been "
            "observed to miss an existing open serial. Get ids from "
            "`tos station show <STID> --all`."
        ),
    )
    move.add_argument(
        "--no-radome",
        dest="no_radome",
        action="store_true",
        help=(
            "Move the antenna ALONE. By default a --subtype antenna move also "
            "moves the radome that shared its station join — they are screwed "
            "together and travel as one assembly. Ignored for other subtypes."
        ),
    )
    move.add_argument(
        "--serial",
        help=(
            "Device serial number (must already exist in TOS). "
            "Optional when --from-station is given: the serial is "
            "inferred from the receiver most recently closed off that "
            "station (workflow case: 'the receiver that came off HRAC')."
        ),
    )
    move.add_argument(
        "--to",
        default="B9 - Kjallari - Jörð",
        metavar="MARKER_OR_LOCATION",
        help=(
            "Destination: a 4-char station marker (e.g. HRAC) → "
            "station-install workflow, OR a location name as recorded "
            "in TOS (e.g. 'B9 - Kjallari - Jörð'). Default: the B9 "
            "warehouse."
        ),
    )
    move.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help=(
            "When the move happened. Three modes: "
            "(1) flag omitted → right now (current timestamp), for "
            "live entries; "
            "(2) bare YYYY-MM-DD or 'today'/'yesterday' → promoted to "
            "12:00 noon on that date, for backdated workday entries; "
            "(3) full ISO datetime (YYYY-MM-DDTHH:MM:SS) → exact "
            "moment preserved. The resolved timestamp is used for the "
            "join transition, any device attribute transitions "
            "(status, comment), and the vitjun's start_time."
        ),
    )
    move.add_argument(
        "--from-station",
        metavar="MARKER",
        help=(
            "Source station marker — sanity-checks the device is "
            "currently at this station. Default: auto-detect from the "
            "open parent join."
        ),
    )
    move.add_argument(
        "--firmware",
        help=(
            "Station-destination only: override the firmware string "
            "written to stations.cfg (does not touch the TOS "
            "firmware_version attribute)."
        ),
    )
    move.add_argument(
        "--rinex-valid-from",
        dest="rinex_valid_from",
        metavar="YYYY-MM-DD",
        help=(
            "Station-destination only: override the stations.cfg "
            "rinex_config_valid_from date. Default: install date if "
            "install was at midnight, else install date + 1 day "
            "(first full day of new config)."
        ),
    )
    move.add_argument(
        "--vitjun",
        metavar="TEXT",
        help=(
            "Override the 'Framkvæmt' text. For station destinations, "
            "default is auto-derived ('Skipt um móttakara: <old> → "
            "<new>'). For location destinations, no vitjun is written "
            "unless this is set."
        ),
    )
    move.add_argument(
        "--vitjun-remaining",
        dest="vitjun_remaining",
        metavar="TEXT",
        help="Optional 'Útistandandi' text for the vitjun.",
    )
    move.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails for the vitjun (e.g. bgo@vedur.is).",
    )
    move.add_argument(
        "--device-status",
        dest="device_status",
        metavar="VALUE",
        help=(
            "Pattern-2 transition on the device's `status` attribute "
            "on the move date. Closes the existing 'virkt' (or whatever) "
            "period and opens a new period with VALUE — e.g. 'bilað' "
            "when moving a broken unit to a workshop, or 'virkt' when "
            "redeploying after repair. Devices with no existing status "
            "get the value added (no close)."
        ),
    )
    move.add_argument(
        "--device-comment",
        dest="device_comment",
        metavar="TEXT",
        help=(
            "Pattern-2 transition on the device's `comment` attribute "
            "on the move date — preserves the old comment in history. "
            "Pass the FULL new comment text."
        ),
    )
    move.add_argument(
        "--no-vitjun",
        action="store_true",
        help="Skip the vitjun step entirely.",
    )
    move.add_argument(
        "--no-cfg",
        action="store_true",
        help=(
            "Skip the stations.cfg write entirely. For station "
            "destinations: don't write the new receiver_* values. For "
            "warehouse destinations: don't clear the source station's "
            "receiver_* fields to NONE (the auto-clear that flags a "
            "station as inactive when its receiver leaves with no "
            "immediate replacement)."
        ),
    )
    move.add_argument(
        "--cfg-path",
        dest="cfg_path",
        help=(
            "Override the stations.cfg location. Default: "
            "$GPS_CONFIG_DATA_REPO/stations.cfg if set, else the "
            "gps_parser-resolved deployed copy."
        ),
    )
    move.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    move.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a structured JSON summary instead of plain text. "
            "Disables the interactive install-attribute fill (its prompts "
            "would corrupt the JSON document)."
        ),
    )
    # ---- Install-attribute fill (station destinations only) -------------
    move.add_argument(
        "--no-install-attrs",
        dest="no_install_attrs",
        action="store_true",
        help=(
            "Skip the post-install fill of station position attributes "
            "(latitude/longitude/height) into TOS from stations.cfg. The "
            "fill runs by default on station destinations and is a no-op "
            "for warehouse moves."
        ),
    )
    move.add_argument(
        "--position-tolerance-m",
        dest="position_tolerance_m",
        type=float,
        default=2.0,
        metavar="METERS",
        help=(
            "Tolerance (m) for treating a cfg vs TOS position value as equal "
            "during the install-attribute fill. Default: 2.0."
        ),
    )
    move.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        help=(
            "Install-attr fill: add missing TOS position values without "
            "prompting. Differing values are still skipped unless "
            "--change/--correct says how to write them."
        ),
    )
    # Intent for install-attr values that DIFFER between cfg and TOS. Optional
    # here (unlike `cfg update-device`): missing values just get added; only a
    # genuine cfg-vs-TOS disagreement needs an intent, and absent one the fill
    # prompts (or skips under --yes / dry-run). Mirrors the update-device
    # --change/--correct semantics so operators learn one model.
    move_intent = move.add_mutually_exclusive_group(required=False)
    move_intent.add_argument(
        "--change",
        dest="change",
        action="store_true",
        help=(
            "Install-attr fill: when a position value differs, treat it as a "
            "real-world change → Pattern 2 transition (close the open TOS "
            "period at --date, open a new one). Records history."
        ),
    )
    move_intent.add_argument(
        "--correct",
        dest="correct",
        action="store_true",
        help=(
            "Install-attr fill: when a position value differs, treat the TOS "
            "record as simply wrong → Pattern 1 in-place upsert (overwrite the "
            "open value, keep its dates). No history."
        ),
    )
    _add_global_flags(move, swap_warning=True)
    move.set_defaults(func=cmd_cfg_move_device)

    # ---- visit (create / edit / show / list) ----------------------------
    visit = cfg_subparsers.add_parser(
        "visit",
        help="Vitjun (maintenance record): list, show, create, or edit",
        description=(
            "One verb, four modes, picked from the args:\n\n"
            "  • --station STAT --history {id|full}   → list the station's "
            "vitjun records\n"
            "  • --id N                                → show one vitjun "
            "(read-only)\n"
            "  • --id N --work TEXT [...]              → edit an existing "
            "vitjun in place\n"
            "  • --station STAT --work TEXT [...]      → create a new "
            "vitjun on the station\n\n"
            "Used for field visits that do NOT change equipment joins "
            "(cable repairs, environment cleanup, remote SSH tweaks). "
            "Receiver swaps via `cfg move-device --to STAT` already "
            "write a vitjun automatically — use this verb to amend "
            "that vitjun afterwards via --id."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List a station's recent vitjun records (id mode = compact):
  receivers cfg visit --station HRAC --history id

  # Same, with all fields (full mode):
  receivers cfg visit --station HRAC --history full

  # Show one record by id:
  receivers cfg visit --id 5146

  # Create a new vitjun (cable repair, no equipment swap):
  receivers cfg visit --station HEDI --reason repairs \\
      --work "Lagaði loftnetskapal" --date 2026-05-22 \\
      --participants bgo@vedur.is

  # Edit an existing vitjun — append outstanding work:
  receivers cfg visit --id 5147 --remaining "Þarf að mála trappan næst"

  # Edit — replace the work text:
  receivers cfg visit --id 5147 --work "Skipt um móttakara og lagaði kapal"

  # Edit — re-open as not-yet-completed:
  receivers cfg visit --id 5147 --incomplete
""",
    )
    visit.add_argument(
        "--station",
        metavar="MARKER",
        help="4-char RINEX marker (required for create / list modes).",
    )
    visit.add_argument(
        "--id",
        dest="vitjun_id",
        type=int,
        metavar="ID_MAINTENANCE",
        help="Maintenance id (selects show or edit mode).",
    )
    visit.add_argument(
        "--history",
        choices=("id", "full"),
        help=(
            "List mode. 'id' (compact: id/date/type/reason/staff/work-preview) "
            "or 'full' (every field per record). Requires --station."
        ),
    )
    visit.add_argument(
        "--work",
        metavar="TEXT",
        help=(
            "Required for create. In edit mode, overwrites the work "
            "('Framkvæmt') field."
        ),
    )
    visit.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help=(
            "When the visit started. Three modes: "
            "(1) flag omitted → right now (current timestamp), for "
            "live entries; "
            "(2) bare YYYY-MM-DD or 'today'/'yesterday' → 12:00 noon "
            "on that date, for backdated workday entries; "
            "(3) full ISO datetime → exact moment preserved."
        ),
    )
    visit.add_argument(
        "--end-time",
        dest="end_time",
        metavar="ISO_DATETIME",
        help="When the visit ended. Default: same as --date.",
    )
    visit.add_argument(
        "--type",
        choices=("onsite", "remote"),
        default="onsite",
        help=(
            "Visit type — 'onsite' (Staðarvitjun, default) or 'remote' "
            "(Fjarvitjun). Create mode only."
        ),
    )
    visit.add_argument(
        "--reason",
        action="append",
        choices=("change", "repairs", "inspection", "improvements", "other"),
        help=(
            "Reason for the visit; repeatable (multi-select). Maps to "
            "the reason_* booleans. In create mode default is 'repairs'; "
            "in edit mode the reason set is REPLACED only when given."
        ),
    )
    visit.add_argument(
        "--comment",
        metavar="TEXT",
        help="'Athugasemdir' text.",
    )
    visit.add_argument(
        "--remaining",
        metavar="TEXT",
        help="'Útistandandi' (outstanding work) text.",
    )
    visit.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails (e.g. bgo@vedur.is,bhb@vedur.is).",
    )
    visit.add_argument(
        "--incomplete",
        action="store_true",
        help=(
            "Mark the visit as not completed (completed=false in TOS). "
            "In create mode applies to the new record; in edit mode "
            "explicitly flips the existing record's completed flag."
        ),
    )
    visit.add_argument(
        "--delete",
        action="store_true",
        help=(
            "DELETE mode. Requires --id ID_MAINTENANCE. Permanently removes "
            "the vitjun from TOS (no undo) — use only to clean up an "
            "accidentally-created record. To keep a real visit's record, edit "
            "it with --id N instead of deleting. Dry-run by default; add "
            "--no-dry-run to commit."
        ),
    )
    visit.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes (irrelevant for read modes).",
    )
    visit.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON output instead of plain text.",
    )
    visit.set_defaults(func=cmd_cfg_visit)

    # ---- replace-receiver ----------------------------------------------
    rr = cfg_subparsers.add_parser(
        "replace-receiver",
        help="One-shot warehouse + retire + install for a receiver swap",
        description=(
            "Encodes the canonical 3-step receiver replacement workflow as "
            "a single verb. Probes the new receiver at the station's "
            "router_ip (or --host), warehouses it in TOS (skipped if "
            "already registered), moves the OLD receiver to B9 marked "
            "'bilað' with a comment, and installs the new one at the "
            "station with auto-vitjun + stations.cfg update. All three "
            "steps share the same timestamp."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Probe-driven (most common — new receiver reachable on network):
  receivers cfg replace-receiver --station ARHO --new-type polarx5

  # Manual mode (offline entry days after the field visit):
  receivers cfg replace-receiver --station ARHO --new-type polarx5 \\
      --new-serial 4101525 --new-model "SEPT POLARX5" --new-firmware 5.7.0 \\
      --date 2026-05-23

  # Continue from a partially-failed run:
  receivers cfg replace-receiver --station ARHO --new-type polarx5 \\
      --continue-from install-new --no-dry-run

Note: --dry-run validates inputs and the warehouse-intake step but
cannot fully preview later steps (move-old + install-new depend on
TOS state that only changes when the prior step actually writes).
For full preview, use --dry-run + --continue-from to preview each
step in isolation, or just --no-dry-run after eyeballing the args.
""",
    )
    rr.add_argument(
        "--station",
        required=True,
        metavar="MARKER",
        help="4-char RINEX marker of the destination station.",
    )
    rr.add_argument(
        "--new-type",
        dest="new_type",
        required=True,
        choices=("polarx5", "netr9", "netrs", "netr5", "g10"),
        help="Probe protocol for the new receiver.",
    )
    rr.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help=(
            "Single timestamp used for all three transitions. Default: "
            "now. Bare date → noon. Use this when entering days after "
            "the field visit."
        ),
    )
    rr.add_argument(
        "--host",
        metavar="IP[:PORT]",
        help=(
            "Override the probe target. Default: stations.cfg[STATION]."
            "router_ip + probe-type's default port."
        ),
    )
    rr.add_argument(
        "--new-serial",
        dest="new_serial",
        help="Manual override (skips probe when --new-model and --new-firmware also given).",
    )
    rr.add_argument(
        "--new-model",
        dest="new_model",
        help="Manual model override (e.g. 'SEPT POLARX5').",
    )
    rr.add_argument(
        "--new-firmware",
        dest="new_firmware",
        help="Manual firmware override (e.g. '5.7.0').",
    )
    rr.add_argument(
        "--new-marker",
        dest="new_marker",
        help="Override the probed marker_name (use when probe can't read it).",
    )
    rr.add_argument(
        "--owner",
        default="Jarðeðlismælihópur",
        help="Owner attribute on the new device. Default: Jarðeðlismælihópur.",
    )
    rr.add_argument(
        "--old-status",
        dest="old_status",
        default="bilað",
        help=(
            "status attribute applied to the OLD device when it moves "
            "to B9. Default: 'bilað'. Pass empty string to skip."
        ),
    )
    rr.add_argument(
        "--old-comment",
        dest="old_comment",
        default="can't connect to the receiver",
        help=(
            "comment attribute applied to the OLD device. Default: "
            '"can\'t connect to the receiver". Pass empty string to skip.'
        ),
    )
    rr.add_argument(
        "--vitjun",
        metavar="TEXT",
        help="Override the auto-derived 'Skipt um móttakara' vitjun text.",
    )
    rr.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails for the vitjun.",
    )
    rr.add_argument(
        "--continue-from",
        dest="continue_from",
        choices=("warehouse", "move-old", "install-new"),
        help=(
            "Skip to a later step for recovery from partial failure. "
            "Re-runs that step and the ones after, assumes earlier "
            "steps already landed."
        ),
    )
    rr.add_argument(
        "--skip-marker-check",
        dest="skip_marker_check",
        action="store_true",
        help=(
            "Don't refuse on marker_name mismatch (use when the "
            "receiver's marker is intentionally non-standard)."
        ),
    )
    rr.add_argument(
        "--cfg-path",
        dest="cfg_path",
        help="Override the stations.cfg location.",
    )
    rr.add_argument(
        "--date-from-archive",
        dest="date_from_archive",
        action="store_true",
        help=(
            "Derive --date from the RINEX archive instead of passing it: builds "
            "the station's receiver timeline (tostools build_receiver_timeline) "
            "and uses the current unit's install date "
            "(current_receiver_install_date — coalesces firmware-only bumps). "
            "REFUSES with the timeline if the archive shows MORE THAN ONE "
            "receiver change not yet in TOS (a single swap can't record those — "
            "reconstruct oldest-first, or pass --date to override). Mutually "
            "exclusive with --date."
        ),
    )
    rr.add_argument(
        "--archive-root",
        dest="archive_root",
        metavar="PATH",
        help=(
            "Override the cold-archive root for --date-from-archive "
            "(default: tostools cold_archive_prepath())."
        ),
    )
    rr.add_argument(
        "--warehouse",
        dest="warehouse",
        metavar="LOCATION_NAME",
        help=(
            "Override the warehouse used for the intake (step 1) + "
            "old-device retire (step 2). Default: receivers.cfg "
            "[tos] default_warehouse, else 'B9 - Kjallari - Jörð'."
        ),
    )
    rr.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes.",
    )
    rr.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary.",
    )
    _add_global_flags(rr, swap_warning=True)
    rr.set_defaults(func=cmd_cfg_replace_receiver)

    # ---- replace-modem (telemetry: GSM modem / router swap) -------------
    rm = cfg_subparsers.add_parser(
        "replace-modem",
        help="Swap a station's GSM modem/router — TOS modem_gsm Pattern 2 + cfg",
        description=(
            "Record a telemetry router/modem replacement. In TOS the router "
            "is a `modem_gsm` device child of the station (canonical "
            "serial/model/owner/status shape). Creates the new modem, opens a "
            "station join at --date, retires the old modem to the warehouse "
            "(with --old-status, default 'bilað'), writes a Breyting vitjun, "
            "and updates stations.cfg `router_type` when --router-type is "
            "given. A modem can't be probed — identity is manual entry. The "
            "IP lives on the SIM card; use `cfg replace-sim` for that."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run a router swap at GSIG:
  receivers cfg replace-modem --station GSIG \\
      --new-serial 6001312345 --new-model "Teltonika RUT241" \\
      --router-type Teltonika --date 2026-06-06

  # Commit it:
  receivers cfg replace-modem --station GSIG \\
      --new-serial 6001312345 --new-model "Teltonika RUT241" \\
      --router-type Teltonika --participants bgo@vedur.is --no-dry-run
""",
    )
    rm.add_argument(
        "--station",
        required=True,
        metavar="MARKER",
        help="4-char RINEX marker of the station.",
    )
    rm.add_argument(
        "--new-serial",
        dest="new_serial",
        help=(
            "Serial number of the new modem/router. Required unless --probe "
            "supplies it (probe value is overridden when this is given)."
        ),
    )
    rm.add_argument(
        "--new-model",
        dest="new_model",
        help=(
            "Model of the new modem (free-text, e.g. 'Teltonika RUT241'). "
            "Required unless --probe supplies it."
        ),
    )
    rm.add_argument(
        "--probe",
        metavar="HOST[:PORT]",
        nargs="?",
        const="@station",
        help=(
            "Auto-extract the new modem's identity (serial/model/mac/"
            "manufacturer/subtype) live from the Teltonika router at HOST via "
            "the RutOS REST API. Pass --probe with no value to probe --station's "
            "router_ip from stations.cfg. Explicit --new-* / --mac / etc. "
            "override probed values. Credentials from receivers.cfg [teltonika]."
        ),
    )
    rm.add_argument(
        "--username",
        help="Override receivers.cfg [teltonika] username for the probe.",
    )
    rm.add_argument(
        "--password",
        help="Override receivers.cfg [teltonika] password for the probe.",
    )
    rm.add_argument(
        "--owner",
        default="Jarðeðlismælihópur",
        help="TOS owner attribute. Default: Jarðeðlismælihópur.",
    )
    rm.add_argument(
        "--router-type",
        dest="router_type",
        metavar="TYPE",
        help=(
            "stations.cfg `router_type` value (e.g. 'Teltonika'). When "
            "omitted, stations.cfg is left untouched."
        ),
    )
    # Optional TOS attributes on the new modem_gsm (MODEM_GSM_ATTR_CODES).
    rm.add_argument(
        "--ip",
        metavar="IP",
        help=(
            "Router LAN/management IP (e.g. 192.168.100.1). With --probe this "
            "is auto-filled from the router's 'lan' interface. NOTE: this is "
            "the router's own IP, not the mobile WAN IP (that's a SIM "
            "attribute — see replace-sim)."
        ),
    )
    rm.add_argument("--phone", metavar="NUMBER", help="modem phone_number attribute.")
    rm.add_argument(
        "--provider",
        metavar="NAME",
        help=(
            "provider attribute — rarely set on a modem (provider is a SIM "
            "attribute; --probe does NOT auto-fill it here)."
        ),
    )
    rm.add_argument("--mac", metavar="MAC", help="mac_address attribute.")
    rm.add_argument(
        "--manufacturer",
        metavar="NAME",
        help="manufacturer (e.g. Teltonika, Conel).",
    )
    rm.add_argument(
        "--io-type",
        dest="io_type",
        metavar="SPEC",
        help="io_type attribute (e.g. 'Ethernet+RS232').",
    )
    rm.add_argument(
        "--modem-subtype",
        dest="modem_subtype",
        metavar="VALUE",
        help="TOS `subtype` attribute on the modem (e.g. '3G'/'4G').",
    )
    rm.add_argument(
        "--comment",
        metavar="TEXT",
        help="comment attribute on the new modem.",
    )
    rm.add_argument(
        "--attr",
        action="append",
        metavar="CODE=VALUE",
        help=(
            "Generic escape hatch — set any TOS attribute code not covered by "
            "a named flag. Repeatable. Example: --attr io_type=Ethernet+RS232. "
            "Overrides a named flag if the same code is given both ways."
        ),
    )
    rm.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help="When the swap happened. Default: now. Bare date → noon.",
    )
    rm.add_argument(
        "--old-status",
        dest="old_status",
        default="bilað",
        help=(
            "status attribute applied to the OLD modem when it moves to the "
            "warehouse. Default: 'bilað'. Pass empty string to skip."
        ),
    )
    rm.add_argument(
        "--old-comment",
        dest="old_comment",
        help="comment attribute applied to the OLD modem (optional).",
    )
    rm.add_argument(
        "--vitjun",
        metavar="TEXT",
        help="Override the auto-derived 'Skipt um router/modem' vitjun text.",
    )
    rm.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails for the vitjun.",
    )
    rm.add_argument(
        "--warehouse",
        dest="warehouse",
        metavar="LOCATION_NAME",
        help="Override the transit warehouse for the old modem (default B9).",
    )
    rm.add_argument(
        "--cfg-path",
        dest="cfg_path",
        help="Override the stations.cfg location.",
    )
    rm.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes.",
    )
    rm.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary.",
    )
    _add_global_flags(rm, swap_warning=True)
    rm.set_defaults(func=cmd_cfg_replace_modem)

    # ---- replace-sim (telemetry: SIM card / IP swap) -------------------
    rs = cfg_subparsers.add_parser(
        "replace-sim",
        help="Swap a station's SIM card (new IP) — TOS sim_card Pattern 2 + cfg",
        description=(
            "Record a SIM-card replacement. In TOS the SIM is a `sim_card` "
            "device child carrying `ip_address` (+ optional `phone_number`) — "
            "not the canonical device shape. Creates a NEW sim_card entity, "
            "opens a station join at --date, closes the old SIM's join (SIMs "
            "aren't warehoused), and writes a vitjun. stations.cfg `router_ip` "
            "is left alone unless --update-cfg-ip is given (cfg router_ip is "
            "often a DNS hostname, not a literal IP)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run a SIM swap at GSIG (new IP):
  receivers cfg replace-sim --station GSIG --ip 10.4.1.240 --date 2026-06-06

  # Commit, also writing the literal IP into stations.cfg router_ip:
  receivers cfg replace-sim --station GSIG --ip 10.4.1.240 \\
      --phone 8400754 --update-cfg-ip --no-dry-run
""",
    )
    rs.add_argument(
        "--station",
        required=True,
        metavar="MARKER",
        help="4-char RINEX marker of the station.",
    )
    rs.add_argument(
        "--ip",
        metavar="IP_ADDRESS",
        help=(
            "The new SIM's IP address (e.g. 10.4.1.240). Required unless "
            "--probe supplies it (probe value overridden when this is given)."
        ),
    )
    rs.add_argument(
        "--probe",
        metavar="HOST[:PORT]",
        nargs="?",
        const="@station",
        help=(
            "Auto-extract the SIM identity (ip/iccid/provider) live from the "
            "Teltonika router at HOST via the RutOS REST API. Pass --probe with "
            "no value to probe --station's router_ip from stations.cfg. Explicit "
            "--ip / --serial / --provider override probed values. Credentials "
            "from receivers.cfg [teltonika]."
        ),
    )
    rs.add_argument(
        "--username",
        help="Override receivers.cfg [teltonika] username for the probe.",
    )
    rs.add_argument(
        "--password",
        help="Override receivers.cfg [teltonika] password for the probe.",
    )
    rs.add_argument(
        "--phone",
        metavar="NUMBER",
        help="Optional phone number / MSISDN for the new SIM.",
    )
    # MSISDN discovery (opt-in, outward-facing): a SIM can't read its own
    # number, so text a catcher from the field router and read the sender off it.
    rs.add_argument(
        "--discover-phone",
        dest="discover_phone",
        action="store_true",
        help=(
            "When --phone is not given: send ONE SMS from the field router "
            "(--probe / --discover-phone-from) to a catcher number "
            "(--discover-phone-to or [teltonika] discover_phone_to), then "
            "prompt for the number it arrives from = this SIM's MSISDN. "
            "Outward-facing + costs a message; only sends with --no-dry-run."
        ),
    )
    rs.add_argument(
        "--discover-phone-to",
        dest="discover_phone_to",
        metavar="NUMBER",
        help=(
            "Catcher number that receives the discovery SMS (your mobile). "
            "Default: [teltonika] discover_phone_to in receivers.cfg."
        ),
    )
    rs.add_argument(
        "--discover-phone-from",
        dest="discover_phone_from",
        metavar="HOST",
        help=("Router to send the discovery SMS from. Default: the --probe host."),
    )
    # Optional TOS attributes on the new sim_card (SIM_CARD_ATTR_CODES).
    rs.add_argument("--serial", metavar="SERIAL", help="serial_number attribute.")
    rs.add_argument("--provider", metavar="NAME", help="provider (e.g. Síminn, Nova).")
    rs.add_argument(
        "--model", metavar="MODEL", help="model attribute (e.g. 'sim kort')."
    )
    rs.add_argument("--owner", metavar="OWNER", help="owner attribute.")
    rs.add_argument(
        "--comment", metavar="TEXT", help="comment attribute on the new SIM."
    )
    rs.add_argument(
        "--attr",
        action="append",
        metavar="CODE=VALUE",
        help=(
            "Generic escape hatch — set any TOS attribute code not covered by "
            "a named flag (e.g. --attr date_end=2027-01-01). Repeatable. "
            "Overrides a named flag if the same code is given both ways."
        ),
    )
    rs.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help="When the swap happened. Default: now. Bare date → noon.",
    )
    rs.add_argument(
        "--vitjun",
        metavar="TEXT",
        help="Override the auto-derived 'Skipt um SIM-kort' vitjun text.",
    )
    rs.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails for the vitjun.",
    )
    rs.add_argument(
        "--update-cfg-ip",
        dest="update_cfg_ip",
        action="store_true",
        help=(
            "Also write the new IP to stations.cfg `router_ip`. Off by "
            "default — cfg router_ip is often a DNS hostname that shouldn't "
            "be overwritten with a literal IP."
        ),
    )
    rs.add_argument(
        "--cfg-path",
        dest="cfg_path",
        help="Override the stations.cfg location.",
    )
    rs.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes.",
    )
    rs.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary.",
    )
    _add_global_flags(rs, swap_warning=True)
    rs.set_defaults(func=cmd_cfg_replace_sim)

    # ---- set-attr (single attribute on an existing device) --------------
    sa = cfg_subparsers.add_parser(
        "set-attr",
        help="Set ONE attribute on a station's open device — no full swap",
        description=(
            "Add or correct a single attribute on the station's OPEN device of a "
            "given subtype (sim_card, modem_gsm, gnss_receiver, antenna …) — for "
            "when the device is already in TOS and you just need to set one value "
            "(e.g. a phone_number on a sim_card after MSISDN discovery, where "
            "replace-sim's same-IP guard refuses a duplicate). Value-driven (no "
            "probe). Dry-run by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Record the discovered MSISDN on RIFC's existing SIM (adds it, no swap):
  receivers cfg set-attr --station RIFC --subtype sim_card \\
      --field phone_number --value +3548372739 --no-dry-run

  # Fix a wrong value in place (no history):
  receivers cfg set-attr --station GSIG --subtype sim_card \\
      --field provider --value Siminn --correct --no-dry-run

  # It genuinely changed → new period from --date (records history):
  receivers cfg set-attr --station GSIG --subtype modem_gsm \\
      --field io_type --value "Ethernet+RS232" --change --date 2026-06-20 --no-dry-run
""",
    )
    sa.add_argument(
        "--station", required=True, metavar="MARKER", help="4-char RINEX marker."
    )
    sa.add_argument(
        "--subtype",
        required=True,
        help="TOS device subtype whose OPEN child on the station is targeted "
        "(e.g. sim_card, modem_gsm, gnss_receiver, antenna).",
    )
    sa.add_argument(
        "--field",
        required=True,
        metavar="CODE",
        help="Attribute code to set (e.g. phone_number, provider, comment).",
    )
    sa.add_argument("--value", required=True, help="New attribute value.")
    sa.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help="Effective date. For a new attribute → the period's date_from "
        "(default: the device's install date). For --change → the transition "
        "date (default: now). Bare date → noon.",
    )
    # Intent — only consulted when an open value already EXISTS and differs.
    # A first-time add or a no-op needs no intent.
    sa_intent = sa.add_mutually_exclusive_group(required=False)
    sa_intent.add_argument(
        "--change",
        action="store_true",
        help="Differing value genuinely CHANGED → Pattern 2 transition (close "
        "open period at --date, open new — records history).",
    )
    sa_intent.add_argument(
        "--correct",
        action="store_true",
        help="Recorded value was WRONG → Pattern 1 in-place PATCH (keep dates, "
        "no history).",
    )
    sa.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the write; without this flag the plan is shown only.",
    )
    sa.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    sa.set_defaults(func=cmd_cfg_set_attr)

    # ---- ensure-port-forwards (Teltonika router DNAT) -------------------
    epf = cfg_subparsers.add_parser(
        "ensure-port-forwards",
        help="Ensure a receiver's control/ftp/http DNAT forwards on its Teltonika router",
        description=(
            "Idempotently create the WAN→LAN port forwards a PolaRX5 receiver "
            "needs to be reachable through its Teltonika router (control 28784, "
            "ftp 2160, http 8060), then apply. Additive only — never deletes or "
            "edits existing rules, and never touches conntrack/raw iptables, so "
            "it cannot sever the router's management path. The receiver's LAN "
            "dest IP is inferred from an existing forward, or pass --dest-ip. "
            "Credentials from receivers.cfg [teltonika]. Dry-run by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: show which forwards are missing for the router at 10.6.1.228
  receivers cfg ensure-port-forwards --host 10.6.1.228

  # Create the missing ones (control+ftp+http) and apply:
  receivers cfg ensure-port-forwards --host 10.6.1.228 --no-dry-run

  # Only the control port (what the PolaRX5 probe needs), explicit dest IP:
  receivers cfg ensure-port-forwards --host 10.6.1.228 \\
      --dest-ip 192.168.100.60 --ports control --no-dry-run

  # By station marker — router_ip resolved from stations.cfg:
  receivers cfg ensure-port-forwards ISAK
""",
    )
    epf.add_argument(
        "station",
        nargs="?",
        metavar="STID",
        help=(
            "4-char station marker; the router host is read from that "
            "station's stations.cfg router_ip. Alternative to --host, which "
            "stays available for bench units and stations not yet in cfg."
        ),
    )
    epf.add_argument(
        "--host",
        metavar="IP[:PORT]",
        help="Router IP/hostname (the Teltonika unit). Optional when STID is given.",
    )
    epf.add_argument(
        "--dest-ip",
        dest="dest_ip",
        metavar="LAN_IP",
        help="Receiver's LAN IP (e.g. 192.168.100.60). Default: inferred from "
        "an existing forward.",
    )
    epf.add_argument(
        "--ports",
        nargs="+",
        choices=("control", "ftp", "http"),
        help="Which forwards to ensure. Default: all three (control ftp http).",
    )
    epf.add_argument(
        "--username",
        help="Override receivers.cfg [teltonika] username.",
    )
    epf.add_argument(
        "--password",
        help="Override receivers.cfg [teltonika] password.",
    )
    epf.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the forwards (additive firewall change on the router).",
    )
    epf.add_argument(
        "--json",
        action="store_true",
        help="(reserved) structured output.",
    )
    epf.set_defaults(func=cmd_cfg_ensure_port_forwards)

    # ---- ensure-conntrack-helper (Teltonika FTP passive-mode fix, SSH) --
    ecth = cfg_subparsers.add_parser(
        "ensure-conntrack-helper",
        help="Enable the RutOS conntrack FTP helper over SSH (fixes passive-mode FTP through NAT)",
        description=(
            "RutOS 7+ ships with net.netfilter.nf_conntrack_helper=0, which "
            "disables automatic conntrack-helper assignment — so the FTP helper "
            "never attaches and passive-mode data ports are unreachable through "
            "NAT, stalling downloads. This enables it (sysctl, live + persisted "
            "in /etc/sysctl.conf) over SSH, since it is not in the RutOS REST "
            "API. Touches only connection tracking — never firewall/routing — so "
            "it cannot sever the management path. SSH login is root (the REST "
            "admin password); credentials from receivers.cfg [teltonika]. "
            "Idempotent, dry-run by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: show what would change on the router at 10.6.1.228
  receivers cfg ensure-conntrack-helper --host 10.6.1.228

  # Enable the helper (live + persisted):
  receivers cfg ensure-conntrack-helper --host 10.6.1.228 --no-dry-run

  # Also extend the FTP helper to track ports 21,2160 (needs a module reload):
  receivers cfg ensure-conntrack-helper --host 10.6.1.228 \\
      --ftp-ports 21,2160 --no-dry-run
""",
    )
    ecth.add_argument(
        "--host",
        required=True,
        metavar="IP",
        help="Router IP/hostname (the Teltonika unit).",
    )
    ecth.add_argument(
        "--ssh-user",
        default="root",
        help="SSH login user (default: root — RutOS dropbear).",
    )
    ecth.add_argument(
        "--ssh-port",
        type=int,
        default=22,
        help="SSH port (default: 22).",
    )
    ecth.add_argument(
        "--ftp-ports",
        metavar="P1,P2",
        help="Also set nf_conntrack_ftp module ports (e.g. 21,2160). Requires a "
        "module reload; off by default since port-21 tracking + the 2160→21 "
        "DNAT already covers the receiver.",
    )
    ecth.add_argument(
        "--username",
        help="Override receivers.cfg [teltonika] username (for password lookup).",
    )
    ecth.add_argument(
        "--password",
        help="Override receivers.cfg [teltonika] password.",
    )
    ecth.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Apply the change on the router (connection-tracking only).",
    )
    ecth.add_argument(
        "--json",
        action="store_true",
        help="(reserved) structured output.",
    )
    ecth.set_defaults(func=cmd_cfg_ensure_conntrack_helper)

    # ---- correct-date (Pattern 4 historical date correction) ------------
    cd = cfg_subparsers.add_parser(
        "correct-date",
        help="Shift every TOS boundary at one date to another (fix a mis-dated swap)",
        description=(
            "Correct a swap/change that was recorded in TOS on the wrong day. "
            "Scans the station, its child devices (and their children, e.g. a "
            "SIM under a modem), and the station's vitjuns, and shifts EVERY "
            "boundary whose instant equals --from to --to: join time_from/"
            "time_to, attribute date_from/date_to (and a datetime `value` like "
            "date_start), and vitjun start/end. Exact-instant match (bare date "
            "→ noon, the field-work convention), so unrelated same-day "
            "boundaries are never touched. Dry-run by default; on commit it "
            "re-reads and verifies no --from boundary remains. Credentials from "
            "database.cfg [tos]."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Review the shift (no writes):
  receivers cfg correct-date --station ROTH --from 2026-06-08 --to 2026-06-04

  # Apply it (with read-back verify):
  receivers cfg correct-date --station ROTH --from 2026-06-08 --to 2026-06-04 \\
      --no-dry-run
""",
    )
    cd.add_argument(
        "--station",
        required=True,
        metavar="MARKER",
        help="4-char RINEX marker of the station.",
    )
    cd.add_argument(
        "--from",
        dest="from_date",
        required=True,
        metavar="DATE",
        help="The wrong instant to find (YYYY-MM-DD → noon, or full ISO datetime).",
    )
    cd.add_argument(
        "--to",
        dest="to_date",
        required=True,
        metavar="DATE",
        help="The correct instant to shift matched boundaries to.",
    )
    cd.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the date shift (live TOS writes).",
    )
    cd.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary.",
    )
    cd.set_defaults(func=cmd_cfg_correct_date)

    # ---- delete-join ----------------------------------------------------
    dj = cfg_subparsers.add_parser(
        "delete-join",
        help="Permanently delete a TOS entity_connection (join) row by id",
        description=(
            "Admin-level destructive operation. Removes a join row "
            "via DELETE /admin_entity_connection_row/{id}. No undo on "
            "TOS — always inspect the row first via "
            "`/entity/parent_history/{id_child}` and confirm with a "
            "dry-run before committing. Intended for cleaning up "
            "known-bad rows: zero-duration orphans from historical "
            "add-receiver workflows, duplicates, etc."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Inspect the device's joins, identify the stale id:
  curl -s https://vi-api.vedur.is/tos/internal/entity/parent_history/21197 | jq

  # Dry-run the delete (default — no writes):
  receivers cfg delete-join --id 27836

  # Commit the delete:
  receivers cfg delete-join --id 27836 --no-dry-run
""",
    )
    dj.add_argument(
        "--id",
        type=int,
        required=True,
        metavar="ID_CONNECTION",
        help="entity_connection id (the `id` field in parent_history rows).",
    )
    dj.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the delete (irreversible).",
    )
    dj.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary.",
    )
    dj.set_defaults(func=cmd_cfg_delete_join)

    # ---- close-join ------------------------------------------------------
    cj = cfg_subparsers.add_parser(
        "close-join",
        help="Close an OPEN join (set time_to) — the non-destructive delete-join",
        description=(
            "End a device↔station join that was real and is now over: the "
            "device left the site. Sets `time_to` on the open join row, "
            "preserving history — unlike `delete-join`, which removes a row "
            "that should never have existed. Use it to retire an antenna, "
            "radome, receiver, modem or SIM from a station without installing "
            "a replacement, or to clean up a duplicate open join before "
            "`cfg replace-antenna` will run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Retire ISAK's open antenna (preferred form — resolves + verifies the join):
  receivers cfg close-join --station ISAK --subtype antenna --date 2026-07-30
  receivers cfg close-join --station ISAK --subtype antenna --date 2026-07-30 --no-dry-run

  # Same, but return the unit to the B9 warehouse instead of leaving it parentless:
  receivers cfg close-join --station ISAK --subtype antenna --warehouse "B9 - Kjallari - Jörð"

  # Close one specific join row (no open-check — inspect it first):
  tos device show --serial 262509            # read the id_connection
  receivers cfg close-join --id 27836 --date 2026-07-30 --no-dry-run
""",
    )
    cj_target = cj.add_mutually_exclusive_group(required=True)
    cj_target.add_argument(
        "--station",
        metavar="MARKER",
        help="4-char marker whose open --subtype child to retire.",
    )
    cj_target.add_argument(
        "--id",
        dest="id_connection",
        type=int,
        metavar="ID_CONNECTION",
        help=(
            "Join row id (the `id` in parent_history). TOS has no "
            "get-join-by-id endpoint, so NO open-check is performed — inspect "
            "the row first."
        ),
    )
    cj.add_argument(
        "--subtype",
        help=(
            "TOS device subtype to close (antenna, radome, gnss_receiver, "
            "modem_gsm, sim_card, monument …). Required with --station."
        ),
    )
    cj.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help=(
            "When the join ended. Omitted → now; bare date → 12:00 noon; "
            "full ISO datetime → exact moment."
        ),
    )
    cj.add_argument(
        "--warehouse",
        nargs="?",
        const="",
        metavar="LOCATION",
        help=(
            "Reparent the device to a warehouse instead of leaving it "
            "parentless (--station mode only). Bare --warehouse uses the fleet "
            "default from [tos] default_warehouse."
        ),
    )
    cj.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the write; without this flag the payload is logged only.",
    )
    cj.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary.",
    )
    cj.set_defaults(func=cmd_cfg_close_join)

    # ---- replace-antenna -------------------------------------------------
    ra = cfg_subparsers.add_parser(
        "replace-antenna",
        help="Swap a station's GNSS antenna — TOS Pattern 2 + stations.cfg",
        description=(
            "Record an antenna replacement. Retires the station's open antenna "
            "(closes its join at --date), creates and joins the new one, writes "
            "a Breyting vitjun, and updates stations.cfg (antenna_type, "
            "antenna_serial, antenna_height, rinex_config_valid_from, and "
            "antenna_radome when --radome is given). The counterpart of "
            "`add-antenna`, which is intake-only and refuses when an antenna is "
            "already open. Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Radome: it is screwed to the antenna, so it FOLLOWS the antenna by default —
the old radome's join is closed and a new radome device is created carrying the
same model. That is the ~95%% field case (antenna + radome down, antenna +
radome up). Three ways to say otherwise:
  (flag omitted)   same model, NEW unit — model carried forward from the old one
  --radome CODE    a different radome type went on
  --radome NONE    the new antenna is bare
  --keep-radome    the SAME physical radome was re-fitted; TOS left untouched
Replacing ONLY the radome (antenna stays) is `cfg replace-radome`.

Height: --antenna-height is the NEW antenna's ARP height (TOS
antenna.antenna_height / RINEX 'ANTENNA: DELTA H'). The stations.cfg
antenna_height is a COMPOSITE (ARP + monument offset) and is derived from the
station's open monument; override with --cfg-antenna-height.

Examples:
  # Antenna + radome both swapped, radome same type as before (dry-run, commit):
  receivers cfg replace-antenna --station ISAK \\
      --model TRM115000.10 --serial 1441234567 \\
      --antenna-height 0.0083 --date 2026-07-30

  receivers cfg replace-antenna --station ISAK \\
      --model TRM115000.10 --serial 1441234567 \\
      --antenna-height 0.0083 --date 2026-07-30 \\
      --participants bgo@vedur.is --no-dry-run

  # New antenna, the old radome re-fitted onto it:
  receivers cfg replace-antenna --station ISAK --model TRM115000.10 \\
      --serial 1441234567 --antenna-height 0.0083 --keep-radome

  # New antenna AND a different radome type, old units returned to B9, cfg
  # written to gps-config-data and pushed:
  receivers cfg replace-antenna --station ISAK --model TRM115000.10 \\
      --serial 1441234567 --radome SNOW --antenna-height 0.0083 \\
      --warehouse "B9 - Kjallari - Jörð" --old-status "bilað" \\
      --global --push --no-dry-run

  # Which model names are accepted? (offline, no TOS call)
  receivers cfg replace-antenna --list-models
""",
    )
    _add_list_models_flag(ra, what="antenna and radome")
    ra.add_argument(
        "--station",
        metavar="STID",
        help="4-char station marker whose antenna is being replaced.",
    )
    ra.add_argument(
        "--model",
        dest="new_model",
        help=(
            "New antenna model. Must match an accepted IGS name EXACTLY "
            "(case-insensitively) — see --list-models. e.g. TRM115000.10."
        ),
    )
    ra.add_argument(
        "--serial",
        dest="new_serial",
        help=(
            "New antenna serial. Omit when unknown — a synthetic "
            "'antenna-<STID>-<YYYYMMDD>' placeholder is generated."
        ),
    )
    ra.add_argument(
        "--antenna-height",
        dest="antenna_height",
        metavar="METRES",
        help=(
            "New antenna ARP height in metres (RINEX 'ANTENNA: DELTA H'). "
            "Required — a swap recorded without it would default to 0.0."
        ),
    )
    ra_radome = ra.add_mutually_exclusive_group()
    ra_radome.add_argument(
        "--radome",
        help=(
            "New radome IGS code, or NONE if the antenna is now bare. OMIT to "
            "carry the old radome's model forward as a NEW unit — the 95%% "
            "case, where antenna and radome come down and go up together."
        ),
    )
    ra.add_argument(
        "--radome-serial",
        dest="radome_serial",
        help=(
            "Serial of the NEW radome. Omit for the fleet convention — a "
            "synthetic 'radome-<STID>-<YYYYMMDD>' placeholder. Applies to the "
            "carried-forward radome too, not just an explicit --radome."
        ),
    )
    ra_radome.add_argument(
        "--keep-radome",
        dest="keep_radome",
        action="store_true",
        help=(
            "The SAME physical radome was unscrewed from the old antenna and "
            "re-fitted to the new one: leave its TOS entity and join untouched "
            "and don't rewrite cfg antenna_radome."
        ),
    )
    ra.add_argument(
        "--cfg-antenna-height",
        dest="cfg_antenna_height",
        metavar="METRES",
        help=(
            "Override the stations.cfg composite height (antenna ARP + "
            "monument offset). Default: derived from the open monument child."
        ),
    )
    ra.add_argument(
        "--rinex-valid-from",
        dest="rinex_valid_from",
        metavar="YYYY-MM-DD",
        help=(
            "Override stations.cfg rinex_config_valid_from. Default: the swap "
            "date if it was at midnight, else the next day (first full day on "
            "the new antenna)."
        ),
    )
    ra.add_argument(
        "--owner",
        help=(
            "Owner label for the new device(s); must match the tostools "
            "OwnersCache. Default: 'Jarðeðlismælihópur'."
        ),
    )
    ra.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help=(
            "When the swap happened. Omitted → now; bare date → 12:00 noon "
            "(pass the same date to move-device/add-monument so the devices "
            "land in one TOS session); full ISO datetime → exact moment."
        ),
    )
    ra.add_argument(
        "--warehouse",
        nargs="?",
        const="",
        metavar="LOCATION",
        help=(
            "Reparent the OLD antenna (and the old radome — they come off the "
            "mast together) to a warehouse. Bare --warehouse uses the fleet "
            "default from [tos] default_warehouse; pass a name to override. "
            "OFF when the flag is absent: a retired antenna is as often "
            "scrapped or left on site as returned, and a wrong location claim "
            "is indistinguishable from a real one afterwards."
        ),
    )
    ra.add_argument(
        "--old-status",
        dest="old_status",
        metavar="VALUE",
        help="Pattern-2 transition on the OLD antenna's `status` (e.g. 'bilað').",
    )
    ra.add_argument(
        "--old-comment",
        dest="old_comment",
        metavar="TEXT",
        help="Pattern-2 transition on the OLD antenna's `comment`.",
    )
    ra.add_argument(
        "--comment",
        help="Optional comment attribute on the NEW antenna.",
    )
    ra.add_argument(
        "--vitjun",
        metavar="TEXT",
        help=(
            "Override the 'Framkvæmt' text. Default: 'Skipt um loftnet: "
            "<old model> <old serial> → <new model> <new serial>'."
        ),
    )
    ra.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails for the vitjun (e.g. bgo@vedur.is).",
    )
    ra.add_argument(
        "--no-vitjun",
        dest="no_vitjun",
        action="store_true",
        help="Skip the vitjun step entirely.",
    )
    ra.add_argument(
        "--no-cfg",
        dest="no_cfg",
        action="store_true",
        help="Skip the stations.cfg write entirely.",
    )
    ra.add_argument(
        "--cfg-path",
        dest="cfg_path",
        help=(
            "Override the stations.cfg location. Default: "
            "$GPS_CONFIG_DATA_REPO/stations.cfg if set, else the "
            "gps_parser-resolved deployed copy."
        ),
    )
    ra.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    ra.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    _add_global_flags(ra, swap_warning=True)
    ra.set_defaults(func=cmd_cfg_replace_antenna)

    # ---- replace-radome --------------------------------------------------
    rrad = cfg_subparsers.add_parser(
        "replace-radome",
        help="Swap a station's radome only — TOS Pattern 2 + stations.cfg",
        description=(
            "Record a radome replacement with the antenna left in place. The "
            "radome is screwed onto the antenna and usually travels with it — "
            "when both change, use `replace-antenna`, which handles the radome "
            "in the same command. This verb is for the radome-only case: "
            "cracked, snow-damaged, or upgraded to a different type over the "
            "same antenna. Retires the open radome, creates and joins the new "
            "one, writes a Breyting vitjun, and updates stations.cfg "
            "antenna_radome. Defaults to dry-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
With no open radome the verb still creates and joins the new one — the "a radome
was fitted where there wasn't one" case. Use --model NONE for the inverse: the
old join is closed, nothing is created, and cfg records NONE.

antenna_height is deliberately NOT recomputed — a radome carries no ARP offset,
so swapping one cannot change the mark→ARP composite.

Examples:
  # Cracked SCIS replaced like-for-like:
  receivers cfg replace-radome --station ISAK --model SCIS --date 2026-07-30
  receivers cfg replace-radome --station ISAK --model SCIS --date 2026-07-30 \\
      --participants bgo@vedur.is --no-dry-run

  # Upgraded to a snow radome, old one returned to B9:
  receivers cfg replace-radome --station ISAK --model SNOW \\
      --warehouse "B9 - Kjallari - Jörð" --no-dry-run

  # Radome removed, antenna now bare:
  receivers cfg replace-radome --station ISAK --model NONE --no-dry-run

  # Which radome codes are accepted? (offline, no TOS call)
  receivers cfg replace-radome --list-models
""",
    )
    _add_list_models_flag(rrad, what="radome")
    rrad.add_argument(
        "--station",
        metavar="STID",
        help="4-char station marker whose radome is being replaced.",
    )
    rrad.add_argument(
        "--model",
        dest="new_model",
        help=(
            "New radome code. Must match an accepted IGS code EXACTLY "
            "(case-insensitively) — see --list-models. e.g. SCIS, LEIT, SNOW. "
            "NONE records removal."
        ),
    )
    rrad.add_argument(
        "--serial",
        dest="new_serial",
        help=(
            "Radome serial. Omit for the fleet convention — a synthetic "
            "'radome-<STID>-<YYYYMMDD>' placeholder (radomes are rarely "
            "serialised)."
        ),
    )
    rrad.add_argument(
        "--owner",
        help=(
            "Owner label for the new radome; must match the tostools "
            "OwnersCache. Default: 'Jarðeðlismælihópur'."
        ),
    )
    rrad.add_argument(
        "--date",
        metavar="YYYY-MM-DD[THH:MM:SS]",
        help=(
            "When the swap happened. Omitted → now; bare date → 12:00 noon; "
            "full ISO datetime → exact moment."
        ),
    )
    rrad.add_argument(
        "--warehouse",
        nargs="?",
        const="",
        metavar="LOCATION",
        help=(
            "Reparent the OLD radome to a warehouse. Bare --warehouse uses the "
            "fleet default from [tos] default_warehouse; pass a name to "
            "override. Off when absent — a retired radome is as often scrapped "
            "as returned."
        ),
    )
    rrad.add_argument(
        "--old-status",
        dest="old_status",
        metavar="VALUE",
        help="Pattern-2 transition on the OLD radome's `status` (e.g. 'bilað').",
    )
    rrad.add_argument(
        "--old-comment",
        dest="old_comment",
        metavar="TEXT",
        help="Pattern-2 transition on the OLD radome's `comment`.",
    )
    rrad.add_argument(
        "--comment",
        help="Optional comment attribute on the NEW radome.",
    )
    rrad.add_argument(
        "--vitjun",
        metavar="TEXT",
        help=(
            "Override the 'Framkvæmt' text. Default: 'Skipt um raðhlíf: "
            "<old> → <new>'."
        ),
    )
    rrad.add_argument(
        "--participants",
        metavar="EMAIL[,EMAIL...]",
        help="Participant emails for the vitjun (e.g. bgo@vedur.is).",
    )
    rrad.add_argument(
        "--no-vitjun",
        dest="no_vitjun",
        action="store_true",
        help="Skip the vitjun step entirely.",
    )
    rrad.add_argument(
        "--no-cfg",
        dest="no_cfg",
        action="store_true",
        help="Skip the stations.cfg antenna_radome write.",
    )
    rrad.add_argument(
        "--cfg-path",
        dest="cfg_path",
        help="Override the stations.cfg location.",
    )
    rrad.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes; without this flag, payloads are logged only.",
    )
    rrad.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    _add_global_flags(rrad, swap_warning=True)
    rrad.set_defaults(func=cmd_cfg_replace_radome)


def _parse_attr_pairs(pairs: Optional[List[str]]) -> Dict[str, Optional[str]]:
    """Parse repeatable ``--attr code=value`` tokens into a ``{code: value}`` dict.

    Backs the generic telemetry escape hatch on ``replace-modem`` /
    ``replace-sim`` — lets an operator set any TOS attribute code not covered
    by a named flag (e.g. ``--attr io_type=Ethernet+RS232``). The value may
    itself contain ``=`` (only the first is the separator). Raises ``ValueError``
    on a token with no ``=`` so a typo surfaces instead of being silently
    dropped.
    """
    out: Dict[str, Optional[str]] = {}
    for tok in pairs or []:
        if "=" not in tok:
            raise ValueError(
                f"--attr expects code=value, got {tok!r} (no '='). "
                f"Example: --attr io_type=Ethernet+RS232"
            )
        code, value = tok.split("=", 1)
        code = code.strip()
        if not code:
            raise ValueError(f"--attr {tok!r}: empty attribute code")
        out[code] = value
    return out


def _normalise_date_arg(value: Optional[str]) -> Optional[str]:
    """Resolve --date input to a date string for the operations layer.

    Pass *bare dates* (YYYY-MM-DD) through unchanged so the operations
    layer's :func:`_visit_default_time` can apply the noon-promotion
    convention. Only return a full ISO datetime when the caller
    explicitly typed one (then noon is suppressed and their time wins).

    Accepts:
        * None — caller's default applies (operations module → today noon)
        * "today" / "yesterday" → bare YYYY-MM-DD (noon-promoted downstream)
        * YYYY-MM-DD → unchanged (noon-promoted downstream)
        * YYYY-MM-DDTHH:MM:SS → unchanged (explicit time preserved)
    """
    if value is None:
        return None
    import re as _re
    from datetime import date as _date
    from datetime import timedelta as _td

    v = value.strip().lower()
    today = _date.today()
    if v == "today":
        return today.isoformat()
    if v == "yesterday":
        return (today - _td(days=1)).isoformat()
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value  # bare date → operations.py noon-promotes
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", value):
        return value  # explicit time preserved
    raise argparse.ArgumentTypeError(
        f"--date {value!r}: use YYYY-MM-DD, "
        f"YYYY-MM-DDTHH:MM:SS, 'today', or 'yesterday'."
    )


def _print_result_summary(result, *, json_output: bool, dry_run: bool) -> None:
    """Format an OperationResult to stdout (text or JSON)."""
    import json as _json
    import sys

    from ..cfg.operations import OperationResult

    if not isinstance(result, OperationResult):
        return
    # Compute a visit-edit diff so users can spot what would change in
    # dry-run mode before they commit.
    edit_diff = None
    if result.operation == "visit-edit":
        inner = result.tos_changes.get("update") or {}
        before = inner.get("before") or {}
        after = inner.get("after") or {}
        edit_diff = _compute_visit_edit_diff(before, after)

    if json_output:
        payload = {
            "operation": result.operation,
            "station_id": result.station_id,
            "serial": result.serial,
            "date": result.date,
            "vitjun_id": result.vitjun_id,
            "cfg_changes": result.cfg_changes,
            "dry_run": result.dry_run,
            "tos_changes_summary": {
                k: type(v).__name__ for k, v in result.tos_changes.items()
            },
        }
        if edit_diff is not None:
            payload["edit_diff"] = edit_diff
        _json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    prefix = "🌵 DRY-RUN" if dry_run else "✅ APPLIED"
    op = result.operation.upper()
    bits = [prefix, op]
    if result.station_id:
        bits.append(f"station={result.station_id}")
    if result.serial:
        bits.append(f"serial={result.serial}")
    if result.date:
        bits.append(f"date={result.date.split('T', 1)[0]}")
    if result.vitjun_id is not None:
        bits.append(f"vitjun_id={result.vitjun_id}")
    if result.operation == "delete-join":
        bits.append(f"id_connection={result.tos_changes.get('id_connection')}")
    print(" ".join(bits))
    if result.operation == "set-attr":
        plan = result.tos_changes.get("plan") or {}
        mode = result.tos_changes.get("mode", "?")
        print(
            f"   {plan.get('subtype')} (eid={plan.get('device_id')}) "
            f"{plan.get('code')}: {plan.get('old_value')!r} → "
            f"{plan.get('new_value')!r}  [{mode}]"
        )
    if result.cfg_changes:
        print(f"   stations.cfg updates: {result.cfg_changes}")
    elif not dry_run and result.operation == "install":
        print("   stations.cfg: no changes (values already matched)")
    if edit_diff:
        print("   field changes:")
        for k, (old, new) in edit_diff.items():
            print(f"     {k}: {old!r} → {new!r}")
    elif edit_diff == {}:
        print("   (no field changes — payload identical to current state)")


def _compute_visit_edit_diff(before: dict, after: dict) -> dict:
    """Return a {field: (old, new)} dict for changed fields on a visit edit.

    Compares top-level start_time / end_time / participants / completed,
    plus each maintenance_attribute_value row (keyed by code).
    """
    diff: dict = {}
    for key in ("start_time", "end_time", "participants", "completed"):
        if key in after and before.get(key) != after.get(key):
            diff[key] = (before.get(key), after.get(key))

    before_attrs = {
        av.get("code"): av.get("value")
        for av in (before.get("maintenance_attribute_values") or [])
    }
    # `after` has attribute_values as a list of {id_maintenance_attribute_value,
    # value} only — to label them we need to map id_av → code via `before`.
    id_to_code = {
        av.get("id_maintenance_attribute_value"): av.get("code")
        for av in (before.get("maintenance_attribute_values") or [])
    }
    for row in after.get("maintenance_attribute_values") or []:
        av_id = row.get("id_maintenance_attribute_value")
        code = id_to_code.get(av_id, f"id_av={av_id}")
        new_val = row.get("value")
        old_val = before_attrs.get(code)
        if old_val != new_val:
            diff[code] = (old_val, new_val)
    return diff


def _run_install_attr_fill(args, result, *, dry_run: bool) -> None:
    """Fill station install attributes (position group) in TOS after a move.

    Runs only for station-destination moves (``result.station_id`` set).
    Reuses the reconcile engine via
    :func:`receivers.cfg.operations.fill_install_attributes`, sourcing values
    from stations.cfg (ground truth for surveyed coordinates) and writing them
    to the TOS station entity. Interaction lives entirely in the ``confirm``
    callback below; the data layer just executes the returned decisions.

    Best-effort: any failure is reported and swallowed so it never masks the
    successful move that already landed.
    """
    from tostools.api.tos_writer import TOSWriter

    from ..cfg.operations import (
        InstallAttrProposal,
        fill_install_attributes,
    )
    from ..config_utils import get_station_config

    station_id = result.station_id
    intent_default = (
        "change"
        if getattr(args, "change", False)
        else ("correct" if getattr(args, "correct", False) else None)
    )
    assume_yes = bool(getattr(args, "yes", False))
    tolerance_m = getattr(args, "position_tolerance_m", 2.0)
    eff_date = result.date or _effective_date_for(args)

    # silent=True: a TOS-only operation (e.g. a --no-cfg move of a station not in
    # the local stations.cfg) legitimately has no local section — the absence is
    # handled by the ⚠️ message below, so don't also log it at ERROR.
    station_config = get_station_config(station_id, silent=True)
    if not station_config:
        print(
            f"   ⚠️  install attrs: no stations.cfg section for {station_id} — skipped"
        )
        return
    tos_data = _query_tos(station_id)
    if not tos_data or tos_data.get("id_entity") is None:
        print(f"   ⚠️  install attrs: no TOS station record for {station_id} — skipped")
        return

    def confirm(p: InstallAttrProposal) -> str:
        if not p.differs:
            # TOS has no value yet — propose to add it.
            if dry_run:
                print(
                    f"   🌵 [dry-run] would ADD {p.label} = {p.cfg_value!r} "
                    f"to TOS station entity (date_from={eff_date.split('T', 1)[0]})"
                )
                return "add"
            if assume_yes:
                print(f"   → add {p.label} = {p.cfg_value!r} (--yes)")
                return "add"
            ans = (
                input(f"   Add {p.label} = {p.cfg_value!r} to TOS? [y/N] ")
                .strip()
                .lower()
            )
            return "add" if ans in ("y", "yes") else "skip"

        # TOS already has a different value — this is the "else confirm" path.
        print(f"   {p.label}: cfg={p.cfg_value!r}  TOS={p.tos_value!r}  (differ)")
        if dry_run:
            if intent_default:
                print(
                    f"   🌵 [dry-run] would {intent_default.upper()} {p.label} "
                    f"→ {p.cfg_value!r}"
                )
                return intent_default
            print(
                "   🌵 [dry-run] differing value — pass --change (history) or "
                "--correct (in-place) to write; skipping"
            )
            return "skip"
        if intent_default:
            if assume_yes:
                return intent_default
            ans = (
                input(f"     apply --{intent_default} ({p.cfg_value!r})? [y/N] ")
                .strip()
                .lower()
            )
            return intent_default if ans in ("y", "yes") else "skip"
        if assume_yes:
            # Never guess change-vs-correct under --yes; require an explicit flag.
            print("     ⏭  differing value and no --change/--correct given — skipping")
            return "skip"
        ans = (
            input("     [c]hange (records history) / [f]ix in place / [s]kip? ")
            .strip()
            .lower()
        )
        return {"c": "change", "f": "correct"}.get(ans, "skip")

    try:
        changes = fill_install_attributes(
            TOSWriter(dry_run=dry_run),
            station_id,
            station_config,
            tos_data,
            eff_date,
            confirm=confirm,
            position_tolerance_m=tolerance_m,
        )
    except Exception as exc:  # noqa: BLE001 — never mask the completed move
        print(f"   ⚠️  install-attr fill failed (move already applied): {exc}")
        logger.warning("[%s] install-attr fill failed: %s", station_id, exc)
        return

    written = {k: v for k, v in changes.items() if v not in ("unchanged", "skipped")}
    if written:
        verb = "would write" if dry_run else "wrote"
        print(f"   install attrs {verb}: {written}")
    elif changes:
        print("   install attrs: no changes (TOS already matches stations.cfg)")


def cmd_cfg_move_device(args) -> int:
    """``cfg move-device`` — move a receiver to a station OR a warehouse.

    Auto-detects target by looking up ``--to`` first as a station marker
    then as a location name. See :func:`receivers.cfg.operations.move_device`.

    On a station-destination move, also fills the station's position
    install-attributes in TOS from stations.cfg (unless ``--no-install-attrs``).
    """
    import sys

    from ..cfg.operations import (
        CfgOperationError,
        move_device,
    )

    dry_run = not args.no_dry_run
    try:
        _cfg_path = _resolve_global_target(args) or (
            Path(args.cfg_path) if args.cfg_path else None
        )
        result = move_device(
            args.serial,
            subtype=args.subtype,
            id_entity=args.id_entity,
            with_radome=not args.no_radome,
            to=args.to,
            date=_normalise_date_arg(args.date),
            from_station=args.from_station,
            firmware=args.firmware,
            rinex_valid_from=args.rinex_valid_from,
            vitjun=args.vitjun,
            vitjun_remaining=args.vitjun_remaining,
            participants=args.participants or "",
            device_status=args.device_status,
            device_comment=args.device_comment,
            dry_run=dry_run,
            cfg_path=_cfg_path,
            skip_vitjun=args.no_vitjun,
            skip_cfg=args.no_cfg,
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    _maybe_commit_global(
        args,
        f"stations({args.to or args.serial}): cfg move-device",
        changed=bool(result.cfg_changes),
        dry_run=dry_run,
    )

    # Station installs get a position install-attribute fill in TOS. Skipped
    # for warehouse moves (result.station_id is None there), when disabled, and
    # in JSON mode (the interactive prompts would corrupt the JSON document).
    if (
        result.station_id is not None
        and not getattr(args, "no_install_attrs", False)
        and not args.json
    ):
        _run_install_attr_fill(args, result, dry_run=dry_run)
    return 0


def cmd_cfg_replace_receiver(args) -> int:
    """``cfg replace-receiver`` — one-shot warehouse + retire + install."""
    import sys

    from ..cfg.operations import (
        CfgOperationError,
        archive_receiver_install_date,
        replace_receiver,
    )

    dry_run = not args.no_dry_run

    # Resolve the install date. --date-from-archive derives it from the RINEX
    # timeline (with the multi-segment guard) instead of taking --date.
    date_arg = _normalise_date_arg(args.date)
    if getattr(args, "date_from_archive", False):
        if args.date:
            print("❌ pass only one of --date or --date-from-archive.", file=sys.stderr)
            return 2
        try:
            derived = archive_receiver_install_date(
                args.station,
                dry_run=dry_run,
                archive_root=getattr(args, "archive_root", None),
            )
        except CfgOperationError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        date_arg = derived.install_date
        print(
            f"   📅 install date from archive: {date_arg} "
            f"({derived.current_type} sn {derived.current_serial})"
        )
        for line in derived.timeline:
            print(f"      {line}")

    try:
        _cfg_path = _resolve_global_target(args) or (
            Path(args.cfg_path) if args.cfg_path else None
        )
        result = replace_receiver(
            args.station,
            args.new_type,
            date=date_arg,
            host=args.host,
            new_serial=args.new_serial,
            new_model=args.new_model,
            new_firmware=args.new_firmware,
            new_marker=args.new_marker,
            owner=args.owner or "Jarðeðlismælihópur",
            old_status=args.old_status,
            old_comment=args.old_comment,
            vitjun=args.vitjun,
            participants=args.participants or "",
            continue_from=args.continue_from,
            skip_marker_check=args.skip_marker_check,
            warehouse=args.warehouse,
            dry_run=dry_run,
            cfg_path=_cfg_path,
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    _maybe_commit_global(
        args,
        f"stations({args.station}): cfg replace-receiver",
        changed=bool(result.cfg_changes),
        dry_run=dry_run,
    )
    return 0


def _probe_telemetry_or_exit(args):
    """Probe the router at ``args.probe`` and return the TelemetryIdentity.

    Returns ``None`` when ``--probe`` was not given. On a probe failure prints
    an actionable error and raises ``SystemExit`` (the caller is a CLI handler).
    """
    host = getattr(args, "probe", None)
    if not host:
        return None
    import sys

    # `--probe` with no value (const "@station") → resolve the target station's
    # router_ip from stations.cfg (router-level probe, no receiver port needed).
    if host == "@station":
        from ..cfg.device_probe import (
            StationProbeResolveError,
            resolve_station_probe,
        )

        station = getattr(args, "station", None)
        if not station:
            print("❌ --probe with no value requires --station SID", file=sys.stderr)
            raise SystemExit(2)
        try:
            host = resolve_station_probe(station, router_only=True)
        except StationProbeResolveError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"Resolved {station} → router {host}")

    from ..cfg.telemetry_probe import (
        ProbeAuthError,
        ProbeCredentialsError,
        ProbeError,
        ProbeUnreachableError,
        probe_teltonika,
    )

    print(f"Probing Teltonika router at {host} …")
    try:
        identity = probe_teltonika(
            host,
            username=getattr(args, "username", None),
            password=getattr(args, "password", None),
        )
    except (ProbeCredentialsError, ProbeAuthError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (ProbeUnreachableError, ProbeError) as exc:
        print(f"❌ probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    # Echo what the probe found so the operator sees the source values before
    # any write (they still go through the dry-run preview below).
    print(
        f"  router: serial={identity.router_serial!r} model={identity.router_model!r} "
        f"mac={identity.router_mac!r} subtype={identity.modem_subtype!r}"
    )
    print(
        f"  SIM:    iccid={identity.sim_iccid!r} ip={identity.sim_ip_address!r} "
        f"provider={identity.provider!r}"
    )
    return identity


def cmd_cfg_replace_modem(args) -> int:
    """``cfg replace-modem`` — swap a station's GSM modem/router in TOS + cfg.

    With ``--probe HOST`` the new modem's identity (serial/model/mac/manufacturer/
    subtype) is read live from the Teltonika router; explicit flags override the
    probed values.
    """
    import sys

    from ..cfg.operations import (
        CfgOperationError,
        replace_modem,
    )

    dry_run = not args.no_dry_run
    try:
        extra_attrs = _parse_attr_pairs(args.attr)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    probe = _probe_telemetry_or_exit(args)
    # Explicit flag wins over the probed value (operator override).
    new_serial = args.new_serial or (probe.router_serial if probe else None)
    new_model = args.new_model or (probe.router_model if probe else None)
    if not new_serial or not new_model:
        missing = "serial" if not new_serial else "model"
        print(
            f"❌ modem {missing} unknown — pass --new-{missing} or use --probe "
            f"against a reachable router.",
            file=sys.stderr,
        )
        return 2

    try:
        _cfg_path = _resolve_global_target(args) or (
            Path(args.cfg_path) if args.cfg_path else None
        )
        result = replace_modem(
            args.station,
            new_serial=new_serial,
            new_model=new_model,
            owner=args.owner or "Jarðeðlismælihópur",
            new_router_type=args.router_type,
            # Modem IP = the router's own LAN/management IP (e.g. 192.168.100.1),
            # NOT the mobile WAN IP — that's a SIM attribute (see replace-sim).
            ip_address=args.ip or (probe.router_lan_ip if probe else None),
            phone_number=args.phone,
            # provider is a SIM attribute (the carrier of the subscription) —
            # never auto-filled onto the modem. Honour an explicit --provider
            # only (rare), but the probe value feeds replace-sim, not here.
            provider=args.provider,
            mac_address=args.mac or (probe.router_mac if probe else None),
            manufacturer=args.manufacturer
            or (probe.router_manufacturer if probe else None),
            io_type=args.io_type,
            modem_subtype=args.modem_subtype
            or (probe.modem_subtype if probe else None),
            comment=args.comment,
            extra_attrs=extra_attrs or None,
            date=_normalise_date_arg(args.date),
            old_status=args.old_status,
            old_comment=args.old_comment,
            vitjun=args.vitjun,
            participants=args.participants or "",
            warehouse=args.warehouse,
            dry_run=dry_run,
            cfg_path=_cfg_path,
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    _maybe_commit_global(
        args,
        f"stations({args.station}): cfg replace-modem",
        changed=bool(result.cfg_changes),
        dry_run=dry_run,
    )
    return 0


def _discover_phone(args, *, dry_run: bool) -> Optional[str]:
    """Discover the field SIM's MSISDN by texting a catcher, then prompt.

    A SIM can't read its own number, so we send one SMS *from* the field router
    (``--probe`` host, or ``--discover-phone-from``) to a catcher number (the
    operator's mobile, from ``--discover-phone-to`` or ``receivers.cfg
    [teltonika] discover_phone_to``). The catcher's received-message sender =
    this SIM's number; since we can't read the operator's phone, we send + then
    prompt them to type what they received.

    Outward-facing + costs a message → in dry-run we only preview the send.
    Returns the entered number, or ``None`` (operator skipped / dry-run).
    """
    import sys

    from ..cfg.telemetry_probe import (
        ProbeError,
        resolve_discover_phone_to,
        send_sms,
    )

    from_host = getattr(args, "discover_phone_from", None) or getattr(
        args, "probe", None
    )
    if not from_host:
        print(
            "❌ --discover-phone needs a sending router: use --probe HOST or "
            "--discover-phone-from HOST.",
            file=sys.stderr,
        )
        return None
    to_number = getattr(args, "discover_phone_to", None) or resolve_discover_phone_to()
    if not to_number:
        print(
            "❌ --discover-phone needs a catcher number: pass --discover-phone-to "
            "or set [teltonika] discover_phone_to in receivers.cfg.",
            file=sys.stderr,
        )
        return None

    station = args.station
    body = f"{station} SIM number check ({_normalise_date_arg(args.date) or 'now'})"
    mode = "[DRY-RUN] would send" if dry_run else "sending"
    print(
        f"   📲 {mode} SMS from {from_host} → {to_number}: {body!r} "
        f"(reveals the {station} SIM's own number on your phone)"
    )
    try:
        send_sms(
            from_host,
            to_number,
            body,
            username=getattr(args, "username", None),
            password=getattr(args, "password", None),
            dry_run=dry_run,
        )
    except ProbeError as exc:
        print(f"   ❌ SMS send failed: {exc}", file=sys.stderr)
        return None

    if dry_run:
        print(
            "   (dry-run: no SMS sent. Re-run with --no-dry-run to send, then "
            "enter the number you receive.)"
        )
        return None
    print(
        f"   ✅ SMS sent. Check {to_number} for the message; its sender is the "
        f"{station} SIM's number."
    )
    try:
        entered = input("   Enter the number you received (blank to skip): ").strip()
    except EOFError:
        entered = ""
    return entered or None


def cmd_cfg_replace_sim(args) -> int:
    """``cfg replace-sim`` — swap a station's SIM card (new IP) in TOS + cfg.

    With ``--probe HOST`` the SIM's identity (ip/iccid/provider) is read live
    from the Teltonika router; explicit flags override the probed values.
    ``--discover-phone`` additionally texts a catcher to reveal the SIM's MSISDN.
    """
    import sys

    from ..cfg.operations import (
        CfgOperationError,
        replace_sim,
    )

    dry_run = not args.no_dry_run
    try:
        extra_attrs = _parse_attr_pairs(args.attr)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    probe = _probe_telemetry_or_exit(args)
    ip_address = args.ip or (probe.sim_ip_address if probe else None)
    if not ip_address:
        print(
            "❌ SIM ip unknown — pass --ip or use --probe against a reachable router.",
            file=sys.stderr,
        )
        return 2

    # MSISDN discovery: --phone wins; else --discover-phone texts a catcher from
    # the field router so its sender header reveals this SIM's own number.
    phone = args.phone
    if not phone and getattr(args, "discover_phone", False):
        phone = _discover_phone(args, dry_run=dry_run)

    try:
        _cfg_path = _resolve_global_target(args) or (
            Path(args.cfg_path) if args.cfg_path else None
        )
        result = replace_sim(
            args.station,
            ip_address=ip_address,
            phone_number=phone,
            serial_number=args.serial or (probe.sim_iccid if probe else None),
            provider=args.provider or (probe.provider if probe else None),
            model=args.model,
            owner=args.owner,
            comment=args.comment,
            extra_attrs=extra_attrs or None,
            date=_normalise_date_arg(args.date),
            vitjun=args.vitjun,
            participants=args.participants or "",
            update_cfg_ip=args.update_cfg_ip,
            dry_run=dry_run,
            cfg_path=_cfg_path,
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    _maybe_commit_global(
        args,
        f"stations({args.station}): cfg replace-sim",
        changed=bool(result.cfg_changes),
        dry_run=dry_run,
    )
    return 0


def cmd_cfg_set_attr(args) -> int:
    """``cfg set-attr`` — set ONE attribute on a station's open <subtype> device."""
    from ..cfg.operations import CfgOperationError, set_device_attribute

    dry_run = not args.no_dry_run
    try:
        result = set_device_attribute(
            args.station,
            subtype=args.subtype,
            code=args.field,
            value=args.value,
            date=_normalise_date_arg(args.date),
            change=args.change,
            correct=args.correct,
            dry_run=dry_run,
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    return 0


def cmd_cfg_ensure_port_forwards(args) -> int:
    """``cfg ensure-port-forwards`` — ensure receiver DNAT forwards on the router.

    Idempotently creates the control(28784)/ftp(2160)/http(8060) WAN→LAN port
    forwards a PolaRX5 receiver needs to be reachable through its Teltonika
    router, then applies. The receiver's LAN dest IP is taken from an existing
    forward (e.g. the http one) or ``--dest-ip``.
    """
    import sys

    from ..cfg.telemetry_probe import (
        ProbeError,
        ensure_port_forwards,
        list_port_forwards,
    )

    # Accept a station marker as well as --host: every other probe verb
    # resolves the router from stations.cfg, and making this one the exception
    # meant hand-copying router_ip for a station the config already describes.
    host = args.host
    if not host:
        if not args.station:
            print(
                "❌ pass a station marker (e.g. `ISAK`) or --host IP[:PORT]",
                file=sys.stderr,
            )
            return 2
        from ..cfg.operations import _station_router_ip

        host = _station_router_ip(args.station)
        if not host:
            print(
                f"❌ no router_ip in stations.cfg for {args.station!r} — "
                f"pass --host explicitly.",
                file=sys.stderr,
            )
            return 2
        print(f"   host {host} (from stations.cfg [{args.station}] router_ip)")
    dry_run = not args.no_dry_run

    # Resolve the receiver's LAN dest IP: explicit flag wins; else infer from an
    # existing forward (they all point at the receiver), else error.
    dest_ip = args.dest_ip
    try:
        existing = list_port_forwards(
            host, username=args.username, password=args.password
        )
    except ProbeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    if not dest_ip:
        dests = {r.get("dest_ip") for r in existing if r.get("dest_ip")}
        if len(dests) == 1:
            dest_ip = dests.pop()
            print(f"   dest_ip {dest_ip} (inferred from existing forward)")
        elif len(dests) > 1:
            print(
                f"❌ multiple dest_ip in existing forwards ({sorted(dests)}); "
                f"pass --dest-ip to disambiguate.",
                file=sys.stderr,
            )
            return 2
        else:
            print(
                "❌ no existing forward to infer the receiver LAN IP from; "
                "pass --dest-ip 192.168.x.y.",
                file=sys.stderr,
            )
            return 2

    # Default wanted set: control + ftp + http (the PolaRX5 trio). Operator can
    # narrow with --ports. WAN src_dport → receiver LAN dest_port:
    #   control 28784 → 28784 (unchanged — Septentrio TCP command port)
    #   ftp     2160  → 21     (WAN-exposed port maps to the receiver's FTP 21)
    #   http    8060  → 80     (WAN-exposed port maps to the receiver's web 80)
    port_map = {
        "control": {"name": "GPS_control", "src_dport": "28784"},
        "ftp": {"name": "GPS_ftp", "src_dport": "2160", "dest_port": "21"},
        "http": {"name": "GPS_http", "src_dport": "8060", "dest_port": "80"},
    }
    which = args.ports or ["control", "ftp", "http"]
    wanted = [port_map[p] for p in which if p in port_map]

    try:
        res = ensure_port_forwards(
            host,
            dest_ip,
            wanted,
            username=args.username,
            password=args.password,
            dry_run=dry_run,
        )
    except ProbeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    prefix = "🌵 DRY-RUN" if dry_run else "✅ APPLIED"
    print(f"{prefix} ensure-port-forwards host={host} dest_ip={dest_ip}")
    if res.get("skipped"):
        print(f"   already present: {res['skipped']}")
    if dry_run:
        for w in res.get("would_create", []):
            print(
                f"   would create: {w['name']} wan:{w['src_dport']} → "
                f"{w['dest_ip']}:{w['dest_port']} {w['proto']}"
            )
        if not res.get("would_create"):
            print("   nothing to create — all forwards present.")
    else:
        print(
            f"   created: {res.get('created') or '(none)'}; applied={res.get('applied')}"
        )
        if res.get("apply_note"):
            print(f"   ⚠️  {res['apply_note']}")
    return 0


def cmd_cfg_ensure_conntrack_helper(args) -> int:
    """``cfg ensure-conntrack-helper`` — enable the RutOS FTP conntrack helper via SSH."""
    import sys

    from ..cfg.conntrack_helper import ProbeError, ensure_conntrack_helper

    dry_run = not args.no_dry_run
    try:
        res = ensure_conntrack_helper(
            args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            ftp_ports=args.ftp_ports,
            username=args.username,
            password=args.password,
            dry_run=dry_run,
        )
    except ProbeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    if args.json:
        import json

        print(json.dumps(res, indent=2))
        return 0

    prefix = "🌵 DRY-RUN" if dry_run else "✅ APPLIED"
    b = res.get("before", {})
    print(
        f"{prefix} ensure-conntrack-helper host={args.host}  "
        f"(before: helper={b.get('helper')} persisted={b.get('persisted')} "
        f"ftp_ports={b.get('ftp_ports')})"
    )
    if not res.get("changed"):
        print("   already enabled — nothing to do.")
        return 0
    if dry_run:
        print("   would run:")
        for cmd in res.get("planned", []):
            print(f"     $ {cmd}")
    else:
        a = res.get("after", {})
        print(
            f"   after: helper={a.get('helper')} persisted={a.get('persisted')} "
            f"ftp_ports={a.get('ftp_ports')}"
        )
        for note in res.get("notes", []):
            print(f"   ⚠️  {note}")
    return 0


def cmd_cfg_correct_date(args) -> int:
    """``cfg correct-date`` — shift all TOS boundaries at --from to --to."""
    import sys

    from ..cfg.operations import CfgOperationError, correct_date

    dry_run = not args.no_dry_run
    try:
        result = correct_date(
            args.station, args.from_date, args.to_date, dry_run=dry_run
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    frm = result.tos_changes["from"]
    to = result.tos_changes["to"]
    changes = result.tos_changes["changes"]

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "station": result.station_id,
                    "from": frm,
                    "to": to,
                    "dry_run": result.dry_run,
                    "changes": changes,
                    "leftover": result.tos_changes.get("leftover"),
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 0

    prefix = "🌵 DRY-RUN" if dry_run else "✅ APPLIED"
    print(
        f"{prefix} correct-date {args.station}: {frm} → {to}  "
        f"({len(changes)} boundaries)"
    )
    if not changes:
        print(f"   no boundaries found at {frm} — nothing to correct.")
        return 0
    for kind, header in (
        ("join", "joins"),
        ("attr", "attributes"),
        ("vitjun", "vitjuns"),
    ):
        rows = [c for c in changes if c["kind"] == kind]
        if rows:
            print(f"   {header} ({len(rows)}):")
            for c in rows:
                flds = "+".join(c["fields"])
                print(f"     #{c['id']}  {flds}  — {c['label']}")
    if not dry_run:
        leftover = result.tos_changes.get("leftover") or []
        if leftover:
            print(f"   ⚠️  {len(leftover)} STILL at {frm}: {leftover}")
        else:
            print(f"   ✅ verified: no {frm} boundaries remain.")
    return 0


def cmd_cfg_delete_join(args) -> int:
    """``cfg delete-join`` — delete a stale entity_connection row by id."""
    from ..cfg.operations import delete_join

    dry_run = not args.no_dry_run
    result = delete_join(args.id, dry_run=dry_run)
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    return 0


def cmd_cfg_close_join(args) -> int:
    """``cfg close-join`` — end an open join (set time_to), preserving history."""
    import sys

    from ..cfg.operations import CfgOperationError, close_join

    dry_run = not args.no_dry_run
    if args.station and not args.subtype:
        print(
            "❌ --station requires --subtype (e.g. --subtype antenna)", file=sys.stderr
        )
        return 2
    try:
        result = close_join(
            id_connection=args.id_connection,
            station_id=args.station,
            subtype=args.subtype,
            date=_normalise_date_arg(args.date),
            warehouse=_resolve_warehouse_arg(args.warehouse),
            dry_run=dry_run,
        )
    except (CfgOperationError, RuntimeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    return 0


def cmd_cfg_replace_antenna(args) -> int:
    """``cfg replace-antenna`` — swap a station's antenna in TOS + stations.cfg."""
    import sys

    from ..cfg.operations import CfgOperationError, replace_antenna

    if getattr(args, "list_models", False):
        return _print_igs_models()
    missing = _require_args_or_exit(
        args,
        ("station", "--station"),
        ("new_model", "--model"),
        ("antenna_height", "--antenna-height"),
    )
    if missing is not None:
        return missing

    dry_run = not args.no_dry_run
    try:
        _cfg_path = _resolve_global_target(args) or (
            Path(args.cfg_path) if args.cfg_path else None
        )
        result = replace_antenna(
            args.station,
            new_model=args.new_model,
            new_serial=args.new_serial,
            antenna_height=args.antenna_height,
            radome=args.radome,
            radome_serial=args.radome_serial,
            keep_radome=args.keep_radome,
            owner=args.owner or "Jarðeðlismælihópur",
            date=_normalise_date_arg(args.date),
            cfg_antenna_height=args.cfg_antenna_height,
            rinex_valid_from=args.rinex_valid_from,
            old_status=args.old_status,
            old_comment=args.old_comment,
            comment=args.comment,
            vitjun=args.vitjun,
            participants=args.participants or "",
            warehouse=_resolve_warehouse_arg(args.warehouse),
            skip_vitjun=args.no_vitjun,
            skip_cfg=args.no_cfg,
            dry_run=dry_run,
            cfg_path=_cfg_path,
        )
    except (CfgOperationError, RuntimeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    _maybe_commit_global(
        args,
        f"stations({args.station}): cfg replace-antenna",
        changed=bool(result.cfg_changes),
        dry_run=dry_run,
    )
    return 0


def cmd_cfg_replace_radome(args) -> int:
    """``cfg replace-radome`` — swap a station's radome, antenna untouched."""
    import sys

    from ..cfg.operations import CfgOperationError, replace_radome

    if getattr(args, "list_models", False):
        return _print_igs_models(antennas=False)
    missing = _require_args_or_exit(
        args, ("station", "--station"), ("new_model", "--model")
    )
    if missing is not None:
        return missing

    dry_run = not args.no_dry_run
    try:
        _cfg_path = _resolve_global_target(args) or (
            Path(args.cfg_path) if args.cfg_path else None
        )
        result = replace_radome(
            args.station,
            new_model=args.new_model,
            new_serial=args.new_serial,
            owner=args.owner or "Jarðeðlismælihópur",
            date=_normalise_date_arg(args.date),
            old_status=args.old_status,
            old_comment=args.old_comment,
            comment=args.comment,
            vitjun=args.vitjun,
            participants=args.participants or "",
            warehouse=_resolve_warehouse_arg(args.warehouse),
            skip_vitjun=args.no_vitjun,
            skip_cfg=args.no_cfg,
            dry_run=dry_run,
            cfg_path=_cfg_path,
        )
    except (CfgOperationError, RuntimeError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    _maybe_commit_global(
        args,
        f"stations({args.station}): cfg replace-radome",
        changed=bool(result.cfg_changes),
        dry_run=dry_run,
    )
    return 0


# Edit-mode flags on `cfg visit`. Used by `cmd_cfg_visit` to decide
# whether `--id N` means "show" (no edit flags) or "edit" (some present).
_VISIT_EDIT_FIELDS = (
    "work",
    "comment",
    "remaining",
    "reason",
    "participants",
    "date",
    "end_time",
    "incomplete",
)


def _has_any_edit_flag(args) -> bool:
    for name in _VISIT_EDIT_FIELDS:
        val = getattr(args, name, None)
        # `incomplete` is a flag (default False); only treat as edit if explicitly set.
        if name == "incomplete" and val:
            return True
        if name != "incomplete" and val is not None:
            return True
    return False


def cmd_cfg_visit(args) -> int:
    """``cfg visit`` — list / show / create / edit / delete vitjun.

    Mode is picked from args:
      * ``--station S --history {id|full}`` → list
      * ``--id N --delete`` → DELETE (permanent removal)
      * ``--id N`` alone → show one
      * ``--id N`` + edit flags → edit existing
      * ``--station S --work TEXT`` → create new
    """
    import sys

    from ..cfg.operations import (
        CfgOperationError,
        add_visit,
        delete_visit,
        list_visits,
        show_visit,
        update_visit,
    )

    # --- Delete mode -------------------------------------------------------
    # Checked first: --delete is a distinct destructive mode, not an edit flag.
    if getattr(args, "delete", False):
        if args.vitjun_id is None:
            print("❌ --delete requires --id ID_MAINTENANCE", file=sys.stderr)
            return 2
        dry_run = not args.no_dry_run
        try:
            result = delete_visit(args.vitjun_id, dry_run=dry_run)
        except (CfgOperationError, RuntimeError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        _print_result_summary(result, json_output=args.json, dry_run=dry_run)
        return 0

    # --- List mode ---------------------------------------------------------
    if args.history is not None:
        if not args.station:
            print("❌ --history requires --station MARKER", file=sys.stderr)
            return 2
        try:
            visits = list_visits(args.station)
        except CfgOperationError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        _print_visit_list(args.station, visits, mode=args.history, as_json=args.json)
        return 0

    # --- Show mode ---------------------------------------------------------
    if args.vitjun_id is not None and not _has_any_edit_flag(args):
        try:
            detail = show_visit(args.vitjun_id)
        except CfgOperationError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        _print_visit_detail(detail, as_json=args.json)
        return 0

    # --- Edit mode ---------------------------------------------------------
    if args.vitjun_id is not None:
        dry_run = not args.no_dry_run
        try:
            result = update_visit(
                args.vitjun_id,
                start_time=_normalise_date_arg(args.date),
                end_time=_normalise_date_arg(args.end_time),
                participants=args.participants,  # None preserves
                completed=(False if args.incomplete else None),
                reasons=args.reason,  # None preserves
                work=args.work,
                comment=args.comment,
                remaining=args.remaining,
                dry_run=dry_run,
            )
        except (CfgOperationError, RuntimeError, ValueError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        _print_result_summary(result, json_output=args.json, dry_run=dry_run)
        return 0

    # --- Create mode -------------------------------------------------------
    if not args.station:
        print(
            "❌ create mode requires --station MARKER (or use --id N to "
            "show/edit an existing vitjun, or --station S --history "
            "{id|full} to list)",
            file=sys.stderr,
        )
        return 2
    if not args.work:
        print("❌ create mode requires --work TEXT", file=sys.stderr)
        return 2
    dry_run = not args.no_dry_run
    maintenance_type = "remote" if args.type == "remote" else "on_site"
    try:
        result = add_visit(
            args.station,
            work=args.work,
            date=_normalise_date_arg(args.date),
            end_time=_normalise_date_arg(args.end_time),
            maintenance_type=maintenance_type,
            reasons=args.reason or None,
            comment=args.comment,
            remaining=args.remaining,
            participants=args.participants or "",
            completed=not args.incomplete,
            dry_run=dry_run,
        )
    except CfgOperationError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    _print_result_summary(result, json_output=args.json, dry_run=dry_run)
    return 0


def _print_visit_list(
    station_id: str,
    visits: list,
    *,
    mode: str,
    as_json: bool,
) -> None:
    """Format a list of vitjun records for stdout."""
    import json as _json
    import sys

    if as_json:
        _json.dump(visits, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return

    if not visits:
        print(f"{station_id} — no vitjun records.")
        return

    if mode == "id":
        print(f"{station_id} — {len(visits)} vitjun records")
        print(
            f"{'id':>6}  {'date':<19}  {'type':<12}  "
            f"{'reason':<10}  {'starfsmenn':<28}  framkvæmt"
        )
        print("─" * 110)
        for v in visits:
            vid = v.get("id") or v.get("id_maintenance") or "?"
            date = (v.get("start_time") or "")[:19]
            mt_is = v.get("maintenance_type_is") or v.get("maintenance_type") or ""
            reason = v.get("reason") or ""
            names = v.get("participants_names") or v.get("participants") or "(none)"
            work = (v.get("work") or "").replace("\n", " ")
            if len(work) > 60:
                work = work[:57] + "..."
            print(
                f"{vid:>6}  {date:<19}  {mt_is:<12}  "
                f"{reason:<10}  {names[:28]:<28}  {work}"
            )
        return

    # mode == "full"
    print(f"{station_id} — {len(visits)} vitjun records (full)")
    for i, v in enumerate(visits, 1):
        vid = v.get("id") or v.get("id_maintenance") or "?"
        print(f"\n[{i}] id_maintenance={vid}")
        for key in (
            "maintenance_type_is",
            "reason",
            "start_time",
            "end_time",
            "participants",
            "participants_names",
            "completed",
            "work",
            "remaining",
            "creation_time",
        ):
            if key in v and v[key] not in (None, ""):
                print(f"     {key}: {v[key]}")


def _print_visit_detail(detail: dict, *, as_json: bool) -> None:
    """Format one vitjun's full detail for stdout."""
    import json as _json
    import sys

    if as_json:
        # Drop the noisy employees list from text/JSON unless asked
        compact = {k: v for k, v in detail.items() if k != "employees"}
        _json.dump(compact, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return

    vid = detail.get("id_maintenance")
    print(f"vitjun id_maintenance={vid}")
    for key in (
        "maintenance_type",
        "start_time",
        "end_time",
        "completed",
        "participants",
    ):
        if key in detail:
            print(f"   {key}: {detail[key]}")
    avs = detail.get("maintenance_attribute_values") or []
    if avs:
        print("   attributes:")
        for av in avs:
            code = av.get("code")
            value = av.get("value")
            av_id = av.get("id_maintenance_attribute_value")
            print(f"     [id_av={av_id}] {code} = {value!r}")


def handle_cfg_command(args) -> int:
    """Handle cfg subcommands; called from main()."""
    if not getattr(args, "cfg_command", None):
        print("❌ No cfg subcommand specified")
        print(
            "Available: reconcile, extract, list, history, add-receiver, "
            "update-device, move-device, replace-receiver, replace-modem, "
            "replace-sim, ensure-port-forwards, visit, delete-join"
        )
        print("\nTry: receivers cfg move-device --help")
        return 2
    return args.func(args)
