#!/usr/bin/env python3
"""Find network calls that can block forever.

The enforcement half of #144. On 2026-08-10 the health executor starved and
fleet monitoring went blind for 5.5 h with no error, no warning and no
APScheduler message — a ThreadPoolExecutor queues rather than rejects, so
pool exhaustion is completely silent. One hung remote holding a pool thread
forever is enough to start that.

Auditing the health path in 2026-08 found it already clean: every genuine
network call carries a timeout. This audit is what KEEPS it clean — a
future unguarded call would silently reintroduce the failure mode, and the
symptom (monitoring quietly stopping) is one nobody notices.

Same shape as ``tx_audit.py``: a standalone AST pass, printing findings and
exiting non-zero, pinned by a test.

**Precision over recall, deliberately.** Two earlier grep-based passes at
this produced hundreds of false positives by matching ``dict.get()`` and
``ftp.get("port")``. A noisy audit gets disabled, so this only flags calls
whose receiver is *resolvably* a network client:

  * ``requests.<verb>(...)`` / ``<session>.<verb>(...)`` where the variable
    was assigned from ``requests.Session()``, or is ``self.session``;
  * ``socket.create_connection(...)``;
  * ``FTP(...)`` / ``FTP_TLS(...)`` / ``SMTP(...)`` constructors;
  * ``psycopg2.connect(...)`` — which needs ``connect_timeout``, not
    ``timeout``.

A call is considered guarded when the timeout is passed explicitly, or —
for ``sock.connect()`` — when ``settimeout`` is called on the same variable
in that module. Constructors that receive ``**params`` are NOT flagged: the
value may legitimately live in the params dict (which is how
``database_factory`` supplies ``connect_timeout``).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "src")

HTTP_VERBS = {"get", "post", "put", "delete", "head", "patch", "request"}
SOCKET_FUNCS = {"create_connection"}
NET_CTORS = {"FTP", "FTP_TLS", "SMTP", "Telnet"}

#: psycopg2 spells it differently.
CONNECT_TIMEOUT_KW = "connect_timeout"


def _attr_chain(node: ast.AST) -> str | None:
    """``a.b.c`` -> "a.b.c" for Name/Attribute chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _session_vars(tree: ast.AST) -> set[str]:
    """Variables assigned from ``requests.Session()``."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
            continue
        chain = _attr_chain(n.value.func) or ""
        if chain.endswith("requests.Session") or chain == "Session":
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def _settimeout_vars(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "settimeout"
            and isinstance(n.func.value, ast.Name)
        ):
            out.add(n.func.value.id)
    return out


def _connect_guarded_vars(tree: ast.AST) -> set[str]:
    """Variables that later get ``.connect(..., timeout=...)``.

    ``ftp = FTP(); ftp.connect(host, port, timeout=30)`` is correct and
    common ftplib usage — the timeout belongs on either call. Flagging the
    bare constructor without checking for this produced 13 false positives
    on first run, which is exactly how an audit earns a ``# noqa`` and
    stops being read.
    """
    out: set[str] = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "connect"
            and isinstance(n.func.value, ast.Name)
            and (
                any(k.arg == "timeout" for k in n.keywords)
                or any(k.arg is None for k in n.keywords)  # **kwargs
                or len(n.args) >= 3  # positional timeout
            )
        ):
            out.add(n.func.value.id)
    return out


def _assigned_to(call: ast.Call, tree: ast.AST) -> str | None:
    """The variable name ``call``'s result is assigned to, if any."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and n.value is call:
            for t in n.targets:
                if isinstance(t, ast.Name):
                    return t.id
    return None


def analyse(path: Path) -> list[tuple[int, str, str]]:
    """Return ``[(lineno, call, why)]`` for unguarded network calls."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    sessions = _session_vars(tree)
    guarded_socks = _settimeout_vars(tree)
    connect_guarded = _connect_guarded_vars(tree)
    findings: list[tuple[int, str, str]] = []

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        chain = _attr_chain(n.func)
        if chain is None:
            continue
        kwargs = {k.arg for k in n.keywords}
        has_star = any(k.arg is None for k in n.keywords)  # **params
        head, _, tail = chain.rpartition(".")

        need, kind = None, None
        if tail in HTTP_VERBS and (
            head == "requests"
            or head in sessions
            or head.endswith(".session")
            or head == "self.session"
        ):
            need, kind = "timeout", "http"
        elif tail in SOCKET_FUNCS and head.endswith("socket"):
            need, kind = "timeout", "socket"
        elif tail in NET_CTORS:
            if _assigned_to(n, tree) in connect_guarded:
                continue
            need, kind = "timeout", "ctor"
        elif chain.endswith("psycopg2.connect"):
            need, kind = CONNECT_TIMEOUT_KW, "postgres"
        elif tail == "connect" and head in guarded_socks:
            continue  # settimeout() seen on this variable

        if need is None:
            continue
        if need in kwargs or has_star:
            continue
        findings.append((n.lineno, chain + "()", f"no {need}= ({kind})"))
    return findings


def main() -> int:
    all_findings: list[tuple[str, int, str, str]] = []
    for py in sorted(ROOT.rglob("*.py")):
        for lineno, call, why in analyse(py):
            all_findings.append((str(py), lineno, call, why))

    if not all_findings:
        print("clean — every network call carries a timeout")
        return 0
    print(f"{len(all_findings)} network call(s) can block forever:\n")
    for p, line, call, why in all_findings:
        print(f"  {p}:{line}\n      {call}  {why}")
    print(
        "\nA hung remote holds its worker thread indefinitely. In the health "
        "pool that is silent (a ThreadPoolExecutor queues rather than "
        "rejects) — the 2026-08-10 blackout shape."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
