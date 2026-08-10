"""Repo-wide guard: no read may leave a transaction open on a borrowed connection.

This test exists because the first fix for the leak was incomplete and I only
found out because a monitor was watching production. The original guard walked
`file_tracker.py` alone, so `archive/state.py` — which parked a transaction for
25 minutes reading the sync watermark — sailed straight past it. A guard scoped
to the modules you happened to remember is not a guard.

`dev/audits/tx_audit.py` walks the AST of every file under `src/` and reports any
function that runs a query through a connection it does not own (a long-lived
`self._conn`, another object's `_conn`, or a caller-supplied `conn` parameter)
without committing or rolling back. Reads through
`with DatabaseConnectionFactory.connection(...)` are not reported — that
context manager commits on exit.

Why it matters, measured on rek-d01 2026-08-10:
  * an open transaction pins the vacuum xmin horizon, so VACUUM cannot reclaim
    dead tuples on a table taking tens of millions of updates; and
  * CREATE/DROP INDEX CONCURRENTLY can never finish — migration 065 died on
    lock_timeout six times before the parked sessions were killed by hand.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Sites that MUST keep a plain cursor, with the reason. The rule the audit
#: encodes is "a read may end its transaction iff it is the first statement on
#: that connection" — these are the ones where it is not, so rolling back would
#: discard the caller's pending writes. Adding to this list is a deliberate act
#: that should come with a comment at the call site too.
ALLOWED = {
    (
        "src/receivers/archive/reindex.py",
        "_existing_sha",
    ): "inside the per-file upsert loop; the caller's transaction owns it",
}


def _run_audit() -> list[tuple[str, str, int]]:
    """Return [(relative_path, function, line)] for every reported site."""
    # cwd=REPO with a RELATIVE root, so the reported paths are repo-relative
    # and the ALLOWED keys stay portable across checkouts.
    proc = subprocess.run(
        [sys.executable, "dev/audits/tx_audit.py", "src"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    findings = []
    path = line = None
    for raw in proc.stdout.splitlines():
        stripped = raw.strip()
        if stripped.startswith("src/") and ":" in stripped:
            path, _, lineno = stripped.rpartition(":")
            line = int(lineno)
        elif stripped.startswith("clean"):
            continue
        elif "()" in stripped and path:
            fn = stripped.split("()")[0].strip()
            findings.append((path, fn, line))
            path = None
    return findings


def test_no_unaudited_transaction_leaks():
    findings = _run_audit()
    unexpected = [(p, fn, ln) for p, fn, ln in findings if (p, fn) not in ALLOWED]

    assert not unexpected, (
        "these reads leave a transaction open on a connection they do not own.\n"
        "Use `receivers.db.tx.read_only_cursor(conn)` (or "
        "`FileTracker.read_cursor()`), OR — if the read can follow a write on "
        "that same connection — leave it and add it to ALLOWED with a reason:\n"
        + "\n".join(f"  {p}:{ln} {fn}()" for p, fn, ln in unexpected)
    )


def test_allowlist_has_no_stale_entries():
    """A allowlisted site that got fixed must be removed, or the list rots."""
    found = {(p, fn) for p, fn, _ in _run_audit()}
    stale = [entry for entry in ALLOWED if entry not in found]

    assert not stale, (
        "these are allowlisted but no longer reported — the exemption is stale, "
        f"delete it: {stale}"
    )


def test_the_audit_actually_detects_a_leak(tmp_path):
    """Guard the guard: a deliberately leaky file must be reported.

    Without this, a bug in the audit (a mis-cased keyword silently classified
    every dynamic query as a write — that happened) would make the suite green
    while detecting nothing.
    """
    leaky = tmp_path / "src" / "leaky.py"
    leaky.parent.mkdir(parents=True)
    leaky.write_text(
        "def read_something(conn):\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('SELECT 1 FROM stations')\n"
        "        return cur.fetchone()\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "dev" / "audits" / "tx_audit.py"),
            str(tmp_path / "src"),
        ],
        capture_output=True,
        text=True,
    )
    assert "read_something" in proc.stdout, proc.stdout


def test_the_audit_accepts_a_correct_read(tmp_path):
    """The inverse: a read that ends its transaction must NOT be reported."""
    ok = tmp_path / "src" / "fine.py"
    ok.parent.mkdir(parents=True)
    ok.write_text(
        "def read_something(conn):\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('SELECT 1 FROM stations')\n"
        "        row = cur.fetchone()\n"
        "    conn.rollback()\n"
        "    return row\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "dev" / "audits" / "tx_audit.py"),
            str(tmp_path / "src"),
        ],
        capture_output=True,
        text=True,
    )
    assert "read_something" not in proc.stdout, proc.stdout
