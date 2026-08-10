#!/usr/bin/env python3
"""Find reads that leave a transaction open on a connection they do not own.

psycopg2 opens a transaction on the FIRST statement — a bare SELECT counts.
So any function that runs a query on a connection whose lifetime it does not
control, and then neither commits nor rolls back, parks that connection in
`idle in transaction` for as long as the owner keeps it.

Two consequences, measured in production 2026-08-10:
  * an open transaction pins the vacuum xmin horizon → dead tuples accumulate;
  * CREATE/DROP INDEX CONCURRENTLY can never finish.

SAFE (not reported):
  * `with DatabaseConnectionFactory.connection(...) as conn:` in the same
    function — the context manager commits on exit.
  * a function that commits or rolls back the same connection.
  * a function that closes the connection it opened.

REPORTED: everything else that runs `<conn>.cursor()` where <conn> is
  * `self._conn` / `self.conn`      — a long-lived attribute,
  * `<name>._conn`                  — someone else's long-lived attribute,
  * a parameter named conn/connection — caller-owned, lifetime unknown.

KNOWN BLIND SPOT — this audit models "the transaction is never ended". It does
NOT catch "the transaction is ended, but only after minutes of slow non-DB
work". Both shapes park a session in `idle in transaction`; only the first is
statically detectable this way.

Observed 2026-08-10 while this very audit reported clean:

    sync_archive_to_db      (health/file_tracker.py)  373 s
    verify_archive_catalog  (archive/verify.py)       371 s

Both interleave DB statements with filesystem walks and SHA-256 hashing, and
both commit eventually — so they are classified as writes and skipped here.
Fixing them means restructuring (read all → slow work → write all, or commit
between phases), not a mechanical cursor swap. Tracked as vault todo #143.

The monitor, not this audit, is what catches that class: alert on
`pg_stat_activity` sessions in `idle in transaction` older than a few minutes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "src")

CONN_PARAM_NAMES = {"conn", "connection", "db_conn", "dbconn"}


def expr_name(node: ast.AST) -> str | None:
    """Render `self._conn` / `tracker._conn` / `conn` as a stable string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = expr_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def analyse(path: Path):
    src = path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    findings = []
    for fn in [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]:
        params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}

        cursor_owners: set[str] = set()
        ended: set[str] = set()
        closed: set[str] = set()
        cm_owned: set[str] = set()  # bound by `with ...connection(...) as X`
        has_execute = False
        sql_seen: list[str] = []

        for node in ast.walk(fn):
            # `with DatabaseConnectionFactory.connection(...) as conn:` — the CM
            # commits on exit, so anything read through it is already safe.
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    call = item.context_expr
                    if isinstance(call, ast.Call):
                        fname = expr_name(call.func) or ""
                        if (
                            fname.endswith("connection")
                            and item.optional_vars is not None
                        ):
                            nm = expr_name(item.optional_vars)
                            if nm:
                                cm_owned.add(nm)

            if not isinstance(node, ast.Call):
                continue
            fname = expr_name(node.func)
            if not fname:
                continue
            if fname.endswith(".execute") or fname.endswith(".executemany"):
                has_execute = True
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    sql_seen.append(node.args[0].value)
                elif node.args and isinstance(node.args[0], ast.JoinedStr):
                    sql_seen.append(
                        "".join(
                            v.value
                            for v in node.args[0].values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)
                        )
                    )
                else:
                    sql_seen.append("<dynamic>")
            if fname.endswith(".cursor"):
                owner = fname[: -len(".cursor")]
                cursor_owners.add(owner)
            elif fname.endswith(".commit") or fname.endswith(".rollback"):
                ended.add(fname.rsplit(".", 1)[0])
            elif fname.endswith(".close"):
                closed.add(fname.rsplit(".", 1)[0])

        if not has_execute:
            continue

        # A WRITE helper that does not commit is almost always part of a batch
        # its caller commits (db_writer._write_* inside write_health_data,
        # migrator._record_migration inside migrate) — correct design, not a
        # leak. A PURE READ has no such excuse: nothing later in the caller is
        # obliged to end the transaction, and both leaks actually observed in
        # production (storage_location, sync_state) were exactly this shape.
        WRITE = (
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
            "GRANT",
            "REVOKE",
            "REFRESH",
            "COPY",
            "SET ",
            # SQL built at runtime cannot be classified — assume it writes.
            "LOCK",
            "UPSERT_",
            "<DYNAMIC>",
        )
        writes = any(
            any(w in s.upper() for w in WRITE)
            # `SELECT upsert_file_tracking(...)` is a write spelled as a select
            or "UPSERT_" in s.upper()
            for s in sql_seen
        )
        if writes:
            continue

        for owner in cursor_owners:
            if owner in cm_owned or owner in ended or owner in closed:
                continue
            root = owner.split(".")[0]
            if owner.startswith("self.") and owner.split(".")[-1] in {"_conn", "conn"}:
                kind = "long-lived self attribute"
            elif owner.endswith("._conn") or owner.endswith(".conn"):
                kind = "another object's long-lived connection"
            elif root in params and root in CONN_PARAM_NAMES:
                kind = "caller-owned connection parameter"
            else:
                continue
            findings.append((path, fn.lineno, fn.name, owner, kind))
    return findings


all_findings = []
for py in sorted(ROOT.rglob("*.py")):
    if "__pycache__" in str(py):
        continue
    all_findings.extend(analyse(py))

if not all_findings:
    print("clean — no read leaves a transaction open on a connection it does not own")
else:
    print(f"{len(all_findings)} site(s) leave a transaction open:\n")
    for p, line, fn, owner, kind in all_findings:
        print(f"  {p}:{line}\n      {fn}()  via {owner}  ({kind})")
sys.exit(1 if all_findings else 0)
