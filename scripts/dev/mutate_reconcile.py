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
* **Ambiguous anchors.** Six identical `n_written += 1` lines exist; mutating
  the wrong one is indistinguishable from an undetected guard. Hence LINE:.

Anchors are literal source text, so they WILL rot as the code moves. A rotted
anchor reports "ANCHOR NOT FOUND" and fails the run rather than passing
silently — fix the anchor, never delete the mutation.
"""

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CLI = REPO / "src/receivers/cli/cfg.py"
APPLY = REPO / "src/receivers/cfg/reconcile_apply.py"
TESTS = ["tests/test_reconcile_output_golden.py", "tests/test_reconcile_apply.py"]
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
        '                if choice in ("d", "delete", ""):',
        "                if True:",
        "test_placeholder_keep_removes_nothing",
    ),
    (
        "M7  tos-fillable [k]eep pushes anyway",
        CLI,
        '            if raw == "C":',
        "            if True:",
        "test_tos_fillable_keep_pushes_nothing",
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
        "LINE:n_written += 1#3",
        "n_written += 0",
        "test_counters_count_placeholder_removal_as_a_write",
    ),
    (
        "M15 canonicalize placeholder removal not counted as a write",
        CLI,
        "LINE:n_written += 1#2",
        "n_written += 0",
        "test_canonicalize",
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
]

ORIGINALS = {CLI: CLI.read_text(), APPLY: APPLY.read_text()}


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
    return r.returncode, (r.stdout.strip().splitlines()[-1] if r.stdout else "")


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
    if old not in s:
        return False
    path.write_text(s.replace(old, new, 1))
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
