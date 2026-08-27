#!/usr/bin/env python3
"""Mutation-test the reconcile write path.

    python3 scripts/dev/mutate_reconcile.py

Run it after ANY change to `cli/cfg.py`'s reconcile path or to
`cfg/reconcile_apply.py`. Exits non-zero and names the survivors.

Break one guard at a time, assert the test that claims to cover it goes RED.
A mutation that leaves the suite green means the test proves nothing.

Two traps this harness exists to avoid, both of which produced a confident
wrong answer during this work:

* **Stale bytecode.** Two mutations adding the same number of characters within
  the same second yield a `.pyc` that mtime+size invalidation accepts, so the
  second run silently imports the first mutation's bytecode. Hence
  PYTHONDONTWRITEBYTECODE=1.
* **Ambiguous anchors**, in two flavours, both of which mutate the WRONG code
  while still changing the file — so "did it apply?" says yes and a healthy
  guard is reported as undetected:
    - identical lines (six `n_written += 1`) — hence the `LINE:...#n` form;
    - substring matches — `'    if raw == "C":'` is a substring of
      `'        if raw == "C":'` in another function. Hence text anchors are
      line-anchored and must match EXACTLY once.

Anchors are literal source text, so they WILL rot as the code moves. A rotted
anchor reports "ANCHOR NOT FOUND" and fails the run rather than passing
silently — fix the anchor, never delete the mutation.
"""

import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CLI = REPO / "src/receivers/cli/cfg.py"
APPLY = REPO / "src/receivers/cfg/reconcile_apply.py"
INTAKE = REPO / "src/receivers/cfg/intake_request.py"
TESTS = [
    "tests/test_reconcile_output_golden.py",
    "tests/test_reconcile_apply.py",
    "tests/test_intake_request.py",
    "tests/test_cli_add_receiver.py",
]
#: `no_network_plugin` lives beside this file — the suite is known to reach live
#: receivers and the live TOS API when a mock detaches.
PP = str(pathlib.Path(__file__).resolve().parent)
PYTHON = REPO / ".venv/bin/python"

# (name, file, old, new, -k selector)
MUTATIONS = [
    (
        "M1  --push-tos consent gate always True",
        CLI,
        "        consent_given: bool = bool(policy.yes) or dry_run_flag",
        "        consent_given: bool = True",
        "test_push_tos_without_consent_pushes_nothing",
    ),
    (
        "M2  --sync-devices consent gate always True",
        CLI,
        "        consent_given: bool = bool(policy.yes) or bool(policy.dry_run)",
        "        consent_given: bool = True",
        "test_sync_devices_without_consent_creates_nothing",
    ),
    (
        "M3  --sync-devices no longer requires --push-tos",
        CLI,
        '    if sync_devices_on and push_to_tos_on and "tos" in sources:',
        '    if sync_devices_on and "tos" in sources:',
        "test_sync_devices_needs_push_tos",
    ),
    (
        "M4  placeholder cleanup block disabled",
        CLI,
        "    if cfg_placeholders:\n        if canonicalize_on:",
        "    if False and cfg_placeholders:\n        if canonicalize_on:",
        "test_placeholder_delete_removes_it or test_canonicalize_removes_placeholders",
    ),
    (
        "M5  tos-fillable block disabled",
        CLI,
        "    if not silent and not policy.dry_run and tos_fillable_list:",
        "    if False and not silent and not policy.dry_run and tos_fillable_list:",
        "test_tos_fillable_capital_c or test_tos_placeholder_offers_z",
    ),
    (
        "M6  placeholder [k]eep silently deletes anyway",
        CLI,
        '    if choice in ("d", "delete", ""):\n        return ("remove", None)',
        '    if True:\n        return ("remove", None)',
        "test_placeholder_prompt_parsing",
    ),
    (
        "M7  tos-fillable [k]eep pushes anyway",
        CLI,
        '    if raw == "C":',
        "    if True:",
        "test_tos_fillable_prompt_parsing",
    ),
    (
        "M11 --push-tos no longer requires a tos source",
        CLI,
        '    if push_to_tos_on and "tos" in sources and tos_data is not None:',
        "    if push_to_tos_on and tos_data is not None:",
        "test_push_tos_batch_refuses_without_a_tos_source",
    ),
    (
        "M13 interactive placeholder removal not counted as a write",
        CLI,
        "LINE:n_written += 1#1",
        "n_written += 0",
        "test_counters_count_placeholder_removal_as_a_write",
    ),
    (
        "M15 canonicalize placeholder removal not counted as a write",
        APPLY,
        "LINE:written += 1#2",
        "written += 0",
        "test_remove_placeholders_removes",
    ),
    # --- the extracted module ---
    (
        "M8  component push builds a LIVE writer regardless of dry_run",
        APPLY,
        "        writer = TOSWriter(dry_run=dry_run)",
        "        writer = TOSWriter(dry_run=False)",
        "test_prompt_component_push or test_component",
    ),
    (
        "M9  push_tos action also writes cfg",
        APPLY,
        '    if action == "push_tos" and value is not None:\n        push_field_value(',
        '    if action == "push_tos" and value is not None:\n        targets.apply(diff, value)\n        push_field_value(',
        "test_prompt_push_tos_action",
    ),
    (
        "M10 set_and_push_tos wording unified with set",
        APPLY,
        '    wrote_suffix=" to cfg",',
        '    wrote_suffix="",',
        "test_prompt_set_and_push_tos",
    ),
    (
        "M12 set_and_push_tos pushes the typed value, not the receiver's",
        APPLY,
        "            value=diff.receiver_value or mapped,",
        "            value=mapped,",
        "test_set_and_push_tos_pushes_the_receiver_value",
    ),
    (
        "M14 skip no longer counted",
        APPLY,
        '    if action == "skip":\n        return ApplyOutcome(skipped=1)',
        '    if action == "skip":\n        return ApplyOutcome()',
        "test_counters_when_everything_is_declined",
    ),
    (
        "M16 push_field_value ignores dry_run (LIVE writer in a dry run)",
        APPLY,
        "    writer = TOSWriter(dry_run=dry_run)",
        "    writer = TOSWriter(dry_run=False)",
        "test_push_field_value",
    ),
    (
        "M17 Pattern 2 never chosen — a CHANGE overwrites history in place",
        APPLY,
        "        not no_transition and diff.tos_value is not None and diff.tos_value != value",
        "        False",
        "test_push_field_value",
    ),
    (
        "M18 CfgTargets writes only the first target (--global silently skipped)",
        APPLY,
        "        for target in self.targets:\n            if self._apply(",
        "        for target in self.targets[:1]:\n            if self._apply(",
        "test_cfg_targets",
    ),
    (
        "M19 bare Enter no longer deletes the placeholder",
        CLI,
        '    if choice in ("d", "delete", ""):',
        '    if choice in ("d", "delete"):',
        "test_placeholder_prompt_parsing",
    ),
    (
        "M20 tos-fillable push made case-insensitive — 'c' starts pushing to TOS",
        CLI,
        '    if raw == "C":',
        '    if raw.lower() == "c":',
        "test_tos_fillable_prompt_parsing",
    ),
    (
        "M21 canonicalize writes in a DRY RUN",
        APPLY,
        '        if dry_run:\n            emit(f"     ≈ {diff.cfg_key}',
        '        if False:\n            emit(f"     ≈ {diff.cfg_key}',
        "test_canonicalize_dry_run_writes_nothing",
    ),
    (
        "M22 placeholder removal happens in a DRY RUN",
        APPLY,
        '        if dry_run:\n            emit(f"     ~ {diff.cfg_key}',
        '        if False:\n            emit(f"     ~ {diff.cfg_key}',
        "test_remove_placeholders_dry_run",
    ),
    (
        "M23 canonicalize aborts the station on the first failure",
        APPLY,
        '            emit(f"     ❌ {diff.cfg_key}: could not write: {exc}")\n            continue',
        '            emit(f"     ❌ {diff.cfg_key}: could not write: {exc}")\n            break',
        "test_canonicalize_keeps_going",
    ),
    (
        "M24 canonicalize loses the resolved_by audit tag",
        APPLY,
        '            changed = targets.apply(diff, raw, resolved_by="canonicalize")',
        "            changed = targets.apply(diff, raw)",
        "test_canonicalize_writes_the_receivers_RAW_spelling",
    ),
    # --- cfg add-receiver intake precedence: CLI > --from-file > default ---
    (
        "P1  file value overrides an explicit CLI arg (precedence inverted)",
        INTAKE,
        "            if _supplied(getattr(self, field)):\n                continue",
        "            if False:\n                continue",
        "test_the_file_never_overrides or test_cli_beats_file",
    ),
    (
        "P2  default owner overwrites a supplied one",
        INTAKE,
        '        if not _supplied(self.owner):\n            updates["owner"] = DEFAULT_OWNER',
        '        if True:\n            updates["owner"] = DEFAULT_OWNER',
        "test_cli_beats_file_beats_default",
    ),
    (
        "P3  default location overwrites a supplied one",
        INTAKE,
        '        if not _supplied(self.location):\n            updates["location"] = DEFAULT_LOCATION',
        '        if True:\n            updates["location"] = DEFAULT_LOCATION',
        "test_the_file_never_overrides_an_explicit_argument",
    ),
    (
        "P4  date_start default overwrites a supplied one",
        INTAKE,
        '        if not _supplied(self.date_start):\n            updates["date_start"] =',
        '        if True:\n            updates["date_start"] =',
        "test_an_explicit_date_survives or test_a_file_date_beats_today",
    ),
    (
        "P5  no default owner at all",
        INTAKE,
        '        if not _supplied(self.owner):\n            updates["owner"] = DEFAULT_OWNER',
        '        if False:\n            updates["owner"] = DEFAULT_OWNER',
        "test_cli_beats_file_beats_default",
    ),
    (
        "P6  falsiness -> is-None: an empty --owner stops taking the default",
        INTAKE,
        "    return bool(value)",
        "    return value is not None",
        "test_an_empty_string_counts_as_not_supplied",
    ),
    (
        "P7  required-field check no longer reports anything",
        INTAKE,
        "        return [f for f in REQUIRED_FIELDS if not _supplied(getattr(self, f))]",
        "        return []",
        "test_required_check_runs_AFTER_defaults",
    ),
    (
        "P8  a blank file value counts as supplied",
        INTAKE,
        '            if file_val not in (None, ""):',
        "            if file_val is not None:",
        "test_an_empty_string_in_the_file_is_ignored_too",
    ),
]


def _refuse_if_pytest_is_running():
    """This harness REWRITES source files in a loop. Anything else importing
    them at the same time reads a file that is changing underneath it.

    That is not theoretical: running it beside a full `pytest tests/` produced
    four spurious failures in `test_push_degradation_gate.py`, whose assertions
    use `inspect.getsource` — the line offsets shifted mid-run, so it sliced a
    completely different function and reported a healthy guard as missing.
    Twenty minutes went into "which of my changes broke this".
    """
    me = os.getpid()
    try:
        pids = subprocess.run(
            ["pgrep", "-f", "-x", r".*python.* -m pytest.*"],
            capture_output=True,
            text=True,
        ).stdout.split()
    except OSError:
        return
    others = []
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            # Match on the process CWD, not on the command line: pytest is
            # usually invoked with a RELATIVE path, so a command-line filter
            # for the repo path silently matches nothing. (It did — this guard
            # failed to fire the first time and let the collision happen twice.)
            if os.readlink(f"/proc/{pid}/cwd") != str(REPO):
                continue
            argv = pathlib.Path(f"/proc/{pid}/cmdline").read_text().split(chr(0))
            # argv[0] must BE a python, not merely a shell whose command line
            # mentions pytest — otherwise the wrapper of a compound command
            # like `pytest ... && mutate.py` trips the guard against itself,
            # and a guard that cries wolf gets deleted.
            if "python" not in pathlib.Path(argv[0]).name:
                continue
            others.append(f"{pid} {' '.join(argv)[:110]}")
        except OSError:
            continue
    if others:
        print("REFUSING: a pytest run is already using this working tree:")
        for ln in others[:3]:
            print("   ", ln[:120])
        print("This harness rewrites src/ in a loop; concurrent runs corrupt both.")
        sys.exit(2)


_refuse_if_pytest_is_running()

ORIGINALS = {CLI: CLI.read_text(), APPLY: APPLY.read_text(), INTAKE: INTAKE.read_text()}


def restore():
    for path, text in ORIGINALS.items():
        path.write_text(text)


def pytest_k(selector):
    r = subprocess.run(
        [
            str(PYTHON),
            "-m",
            "pytest",
            *TESTS,
            "-p",
            "no_network_plugin",
            "-q",
            "-k",
            selector,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={
            "PYTHONPATH": PP,
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/bgo",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    last = r.stdout.strip().splitlines()[-1] if r.stdout else ""
    if r.returncode == 5:
        # pytest exit 5 = no tests collected. Non-zero, so it reads as
        # "DETECTED" — a selector that matches nothing would silently certify
        # a mutation nobody tested. Surface it as a harness error instead.
        return "NO_TESTS", last
    return r.returncode, last


def apply_mutation(path, old, new):
    """Return True only if the mutation was really applied."""
    s = path.read_text()
    if old.startswith("LINE:"):
        needle, _, nth = old[len("LINE:") :].partition("#")
        nth = int(nth)
        lines = s.splitlines(keepends=True)
        hits = [i for i, ln in enumerate(lines) if ln.strip() == needle]
        if len(hits) < nth:
            return False
        i = hits[nth - 1]
        indent = len(lines[i]) - len(lines[i].lstrip())
        lines[i] = " " * indent + new + "\n"
        path.write_text("".join(lines))
        return True
    # Anchor at a line boundary and require EXACTLY one match. Plain
    # `old in s` matches mid-line: `'    if raw == "C":'` is a substring of
    # `'        if raw == "C":'` in a different function, so `.replace(..., 1)`
    # silently mutated the wrong one — the file changed, so the "did it apply?"
    # check passed, and a good guard was reported as undetected.
    hits = s.count("\n" + old)
    if hits != 1:
        print(
            f"       ({'ambiguous' if hits else 'absent'}: {hits} line-anchored matches)"
        )
        return False
    path.write_text(s.replace("\n" + old, "\n" + new, 1))
    return True


bad = []
try:
    for name, path, old, new, selector in MUTATIONS:
        restore()
        if not apply_mutation(path, old, new):
            print(f"⚠️  {name}: ANCHOR NOT FOUND — mutation never applied")
            bad.append(name)
            continue
        assert path.read_text() != ORIGINALS[path]
        rc, last = pytest_k(selector)
        if rc == "NO_TESTS":
            verdict = "⚠️  NO TESTS MATCHED"
            bad.append(name)
        else:
            verdict = "DETECTED" if rc != 0 else "❌ NOT DETECTED"
            if rc == 0:
                bad.append(name)
        print(f"{verdict:16} {name}\n{'':17}{last}")
finally:
    restore()

rc, last = pytest_k("")
print(f"\nrestored tree: {last}")
if bad:
    print("\nMutations that survived (their tests prove nothing):")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print("\nEvery mutation was detected.")
