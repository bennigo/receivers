"""No network call may block forever (#144).

On 2026-08-10 the health executor starved and fleet monitoring went blind
for 5.5 h with no error, no warning and no APScheduler message — a
ThreadPoolExecutor queues rather than rejects, so pool exhaustion is
silent. One hung remote holding a pool thread is enough to start that.

The health path was already clean when audited; this is what keeps it that
way. Same shape as `test_transaction_audit.py`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "dev" / "audits" / "timeout_audit.py"


def _analyse(src: str, tmp_path: Path):
    """Run the audit's analyser over a source snippet."""
    sys.path.insert(0, str(AUDIT.parent))
    try:
        import importlib

        mod = importlib.import_module("timeout_audit")
        importlib.reload(mod)
        p = tmp_path / "sample.py"
        p.write_text(src, encoding="utf-8")
        return mod.analyse(p)
    finally:
        sys.path.remove(str(AUDIT.parent))


class TestTheTreeIsClean:
    def test_no_network_call_can_block_forever(self):
        """The audit over src/ — the assertion this file exists for."""
        proc = subprocess.run(
            [sys.executable, str(AUDIT), "src"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        assert proc.returncode == 0, (
            "a network call has no timeout — a hung remote will hold its "
            f"worker thread forever:\n\n{proc.stdout}"
        )


class TestItCatchesRealGaps:
    """An audit nobody can make fail is not a guard."""

    def test_bare_requests_get_is_flagged(self, tmp_path):
        f = _analyse("import requests\nrequests.get('http://x')\n", tmp_path)
        assert len(f) == 1 and "timeout" in f[0][2]

    def test_bare_ftp_constructor_is_flagged(self, tmp_path):
        f = _analyse("from ftplib import FTP\nftp = FTP()\nftp.login()\n", tmp_path)
        assert len(f) == 1

    def test_psycopg2_connect_needs_connect_timeout(self, tmp_path):
        f = _analyse("import psycopg2\npsycopg2.connect(dsn='x')\n", tmp_path)
        assert len(f) == 1 and "connect_timeout" in f[0][2]

    def test_socket_create_connection_is_flagged(self, tmp_path):
        f = _analyse("import socket\nsocket.create_connection(('h', 1))\n", tmp_path)
        assert len(f) == 1


class TestItDoesNotCryWolf:
    """Two earlier grep passes produced hundreds of false positives.

    A noisy audit earns a `# noqa` and stops being read, so the
    not-flagged cases matter as much as the flagged ones.
    """

    def test_a_dict_get_is_not_a_network_call(self, tmp_path):
        # the exact shape that produced 2480 false hits: dicts named s/http/ftp
        src = (
            "def f(d):\n"
            "    s = d['sessions']\n"
            "    http = d['http']\n"
            "    ftp = d['ftp']\n"
            "    return s.get('x'), http.get('open'), ftp.get('port')\n"
        )
        assert _analyse(src, tmp_path) == []

    def test_timeout_on_connect_guards_the_constructor(self, tmp_path):
        """`ftp = FTP(); ftp.connect(h, p, timeout=30)` is correct ftplib use.

        Missing this produced 13 false positives on the first run.
        """
        src = "from ftplib import FTP\nftp = FTP()\nftp.connect('h', 21, timeout=30)\n"
        assert _analyse(src, tmp_path) == []

    def test_timeout_on_the_constructor_guards_it(self, tmp_path):
        src = "from ftplib import FTP\nftp = FTP(timeout=30)\nftp.connect('h')\n"
        assert _analyse(src, tmp_path) == []

    def test_settimeout_guards_a_socket_connect(self, tmp_path):
        src = (
            "import socket\n"
            "sock = socket.socket()\n"
            "sock.settimeout(5)\n"
            "sock.connect(('h', 1))\n"
        )
        assert _analyse(src, tmp_path) == []

    def test_star_params_are_not_flagged(self, tmp_path):
        """The value may legitimately live in the params dict."""
        src = "import psycopg2\npsycopg2.connect(**params)\n"
        assert _analyse(src, tmp_path) == []

    def test_an_explicit_timeout_is_accepted(self, tmp_path):
        src = "import requests\nrequests.get('http://x', timeout=10)\n"
        assert _analyse(src, tmp_path) == []


class TestTheDsnBranchIsGuarded:
    """The gap this audit actually found."""

    def test_connect_timeout_is_resolved_from_one_place(self):
        from receivers.health.database_factory import DatabaseConnectionFactory

        assert DatabaseConnectionFactory._default_connect_timeout({}) == "10"
        assert (
            DatabaseConnectionFactory._default_connect_timeout({"connect_timeout": "3"})
            == "3"
        )

    def test_env_wins_over_config(self, monkeypatch):
        from receivers.health.database_factory import DatabaseConnectionFactory

        monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT", "7")
        assert (
            DatabaseConnectionFactory._default_connect_timeout({"connect_timeout": "3"})
            == "7"
        )

    def test_params_and_dsn_branches_agree(self):
        """They had drifted: the DSN branch inherited libpq's indefinite wait."""
        from receivers.health.database_factory import DatabaseConnectionFactory as F

        assert F.get_connection_params().get("connect_timeout") == (
            F._default_connect_timeout()
        )


class TestAuditShape:
    def test_the_audit_is_executable_and_reports_cleanly(self):
        proc = subprocess.run(
            [sys.executable, str(AUDIT), "src"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        assert "clean" in proc.stdout or "block forever" in proc.stdout

    def test_it_parses_as_python(self):
        ast.parse(AUDIT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ["src/receivers/health", "src/receivers/monitoring"])
def test_the_health_and_monitoring_paths_are_clean(path):
    """Named explicitly — these are the pools whose starvation is silent."""
    proc = subprocess.run(
        [sys.executable, str(AUDIT), path], capture_output=True, text=True, cwd=REPO
    )
    assert proc.returncode == 0, proc.stdout
