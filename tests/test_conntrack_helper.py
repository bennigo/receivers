"""Tests for receivers.cfg.conntrack_helper — RutOS FTP conntrack-helper via SSH.

paramiko/SSH is fully mocked (no network). A FakeSSH maps command substrings to
canned ``(rc, stdout, stderr)`` so the apply/idempotency/dry-run logic is tested
against the real RUT241 state shapes captured live 2026-06-07.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.conntrack_helper import (
    HostKeyMismatchError,
    ProbeAuthError,
    ProbeError,
    ProbeUnreachableError,
    TofuHostKeyPolicy,
    _connect,
    check_conntrack_helper,
    ensure_conntrack_helper,
    resolve_known_hosts_path,
)


class FakeSSH:
    """Minimal paramiko.SSHClient stand-in driven by a (substr → (rc,out,err)) map."""

    def __init__(self, responses):
        self._responses = responses  # list of (substr, (rc, out, err))
        self.commands = []

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        rc, out, err = 0, "", ""
        for substr, resp in self._responses:
            if substr in cmd:
                rc, out, err = resp
                break
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = rc
        stdout.read.return_value = out.encode()
        stderr = MagicMock()
        stderr.read.return_value = err.encode()
        return MagicMock(), stdout, stderr

    def close(self):
        pass


def _patch(fake, *, pw="secret"):
    """Patch _connect → fake client and resolve_credentials → (admin, pw)."""
    return (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake),
        patch(
            "receivers.cfg.conntrack_helper.resolve_credentials",
            lambda **_k: ("admin", pw),
        ),
    )


# State of a fresh router: helper disabled, not persisted, ftp tracks 21.
_FRESH = [
    ("sysctl -n net.netfilter.nf_conntrack_helper", (0, "0", "")),
    ("grep -E", (1, "", "")),  # not persisted
    ("parameters/ports", (0, "21", "")),
]
# State of an already-fixed router.
_FIXED = [
    ("sysctl -n net.netfilter.nf_conntrack_helper", (0, "1", "")),
    ("grep -E", (0, "net.netfilter.nf_conntrack_helper=1", "")),
    ("parameters/ports", (0, "21", "")),
]


def test_check_reads_state():
    fake = FakeSSH(_FRESH)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        st = check_conntrack_helper("10.6.1.228")
    assert st.helper_value == "0"
    assert st.helper_persisted is False
    assert st.ftp_ports == "21"


def test_dry_run_plans_but_does_not_write():
    fake = FakeSSH(_FRESH)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", dry_run=True)
    assert res["dry_run"] is True
    assert res["changed"] is True
    assert res["applied"] is False
    # plan: live sysctl + persist; no ftp-ports op (not requested)
    assert any(
        "sysctl -w net.netfilter.nf_conntrack_helper=1" in c for c in res["planned"]
    )
    assert any("/etc/sysctl.conf" in c for c in res["planned"])
    assert not any("modprobe" in c for c in res["planned"])
    # dry-run must not have executed any apply command (only the 3 read probes)
    assert not any("sysctl -w" in c for c in fake.commands)


def test_live_apply_enables_and_verifies():
    # reads return fresh first, then fixed on the post-apply re-read
    class SeqSSH(FakeSSH):
        def exec_command(self, cmd, timeout=None):
            self.commands.append(cmd)
            if "sysctl -n" in cmd:
                # before → "0", after (once an apply cmd has run) → "1"
                applied = any("sysctl -w" in c for c in self.commands)
                val = "1" if applied else "0"
                return self._mk(0, val)
            if "grep -E" in cmd:
                applied = any(
                    "sysctl.conf" in c and "echo" in c or "sed" in c
                    for c in self.commands
                )
                return self._mk(
                    0 if applied else 1,
                    "net.netfilter.nf_conntrack_helper=1" if applied else "",
                )
            if "parameters/ports" in cmd:
                return self._mk(0, "21")
            return self._mk(0, "")

        def _mk(self, rc, out):
            so = MagicMock()
            so.channel.recv_exit_status.return_value = rc
            so.read.return_value = out.encode()
            se = MagicMock()
            se.read.return_value = b""
            return MagicMock(), so, se

    fake = SeqSSH([])
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", dry_run=False)
    assert res["applied"] is True
    assert res["after"]["helper"] == "1"
    assert any(
        "sysctl -w net.netfilter.nf_conntrack_helper=1" in c for c in fake.commands
    )


def test_idempotent_when_already_enabled():
    fake = FakeSSH(_FIXED)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", dry_run=False)
    assert res["changed"] is False
    assert res["applied"] is False
    assert res["planned"] == []
    assert not any("sysctl -w" in c for c in fake.commands)


def test_ftp_ports_extension_adds_modprobe_and_reload():
    fake = FakeSSH(_FRESH)  # ftp_ports currently "21", we want "21,2160"
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", ftp_ports="21,2160", dry_run=True)
    assert any("modprobe.d/nf_conntrack_ftp.conf" in c for c in res["planned"])
    assert any("modprobe nf_conntrack_ftp ports=21,2160" in c for c in res["planned"])


def test_ftp_ports_noop_when_already_matching():
    fixed_ports = [
        ("sysctl -n net.netfilter.nf_conntrack_helper", (0, "1", "")),
        ("grep -E", (0, "net.netfilter.nf_conntrack_helper=1", "")),
        ("parameters/ports", (0, "21,2160", "")),
    ]
    fake = FakeSSH(fixed_ports)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", ftp_ports="21,2160", dry_run=True)
    assert res["changed"] is False  # helper already on AND ports already match


def test_invalid_ftp_ports_rejected():
    """--ftp-ports with shell metacharacters must be rejected (no injection)."""
    fake = FakeSSH(_FRESH)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        with pytest.raises(ProbeError):
            ensure_conntrack_helper("10.6.1.228", ftp_ports="21; reboot", dry_run=True)


def test_no_password_raises():
    cm_conn, cm_creds = _patch(FakeSSH(_FRESH), pw="")
    with cm_conn, cm_creds:
        with pytest.raises(ProbeError):
            ensure_conntrack_helper("10.6.1.228", dry_run=True)


def test_module_reload_busy_is_not_fatal():
    """rmmod failing (module busy) must be a note, not a hard error."""
    seq_responses = [
        ("sysctl -n", (0, "1", "")),  # helper already on so only ports change
        ("grep -E", (0, "net.netfilter.nf_conntrack_helper=1", "")),
        ("parameters/ports", (0, "21", "")),
        ("rmmod nf_conntrack_ftp", (1, "", "rmmod: module is in use")),
        ("modprobe.d", (0, "", "")),
    ]
    fake = FakeSSH(seq_responses)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", ftp_ports="21,2160", dry_run=False)
    assert res["applied"] is True
    assert any("busy" in n for n in res.get("notes", []))


# ---------------------------------------------------------------------------
# SSH host-key trust (the tests above mock _connect wholesale, so none of them
# exercise host-key handling — these drive _connect itself with paramiko's
# SSHClient patched out).
# ---------------------------------------------------------------------------


def _connect_kwargs(known_hosts):
    return dict(
        ssh_user="root",
        password="secret",
        port=22,
        timeout=5,
        known_hosts=str(known_hosts),
    )


def _fake_key(b64: str, blob: bytes, name: str = "ssh-ed25519"):
    key = MagicMock()
    key.get_base64.return_value = b64
    key.asbytes.return_value = blob
    key.get_name.return_value = name
    return key


def test_connect_pins_host_keys_in_known_hosts_file(tmp_path):
    """_connect must load a known_hosts file (creating it 0600) and use TOFU."""
    kh = tmp_path / "state" / "router_known_hosts"
    fake_client = MagicMock()
    with patch("paramiko.SSHClient", return_value=fake_client):
        client = _connect("10.6.1.228", **_connect_kwargs(kh))

    assert client is fake_client
    # load_host_keys() is what sets paramiko's _host_keys_filename — without it
    # a first-contact key would live in memory only and be forgotten on exit.
    fake_client.load_host_keys.assert_called_once_with(str(kh))
    policy = fake_client.set_missing_host_key_policy.call_args[0][0]
    assert isinstance(policy, TofuHostKeyPolicy)
    assert kh.exists()
    assert stat.S_IMODE(os.stat(kh).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(kh.parent).st_mode) == 0o700


def test_changed_host_key_is_refused_loudly(tmp_path):
    """A CHANGED key must raise HostKeyMismatchError, not 'unreachable'.

    Regression guard for the ordering of the except clauses: BadHostKeyException
    is an SSHException, so a generic handler would report a MITM as a flaky link
    and invite a retry that hands over the router password.
    """
    paramiko = pytest.importorskip("paramiko")
    kh = tmp_path / "router_known_hosts"
    fake_client = MagicMock()
    fake_client.connect.side_effect = paramiko.BadHostKeyException(
        "10.6.1.228", _fake_key("QUFB", b"got"), _fake_key("QkJC", b"expected")
    )
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(HostKeyMismatchError) as excinfo:
            _connect("10.6.1.228", **_connect_kwargs(kh))

    msg = str(excinfo.value)
    assert "MISMATCH" in msg
    assert "SHA256:" in msg  # both fingerprints, for out-of-band comparison
    assert f"ssh-keygen -R 10.6.1.228 -f {kh}" in msg  # the escape hatch
    assert not isinstance(excinfo.value, ProbeUnreachableError)


def test_changed_host_key_message_uses_bracketed_nonstandard_port(tmp_path):
    paramiko = pytest.importorskip("paramiko")
    kh = tmp_path / "router_known_hosts"
    fake_client = MagicMock()
    fake_client.connect.side_effect = paramiko.BadHostKeyException(
        "10.6.1.228", _fake_key("QUFB", b"got"), _fake_key("QkJC", b"expected")
    )
    kwargs = _connect_kwargs(kh)
    kwargs["port"] = 2222
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(HostKeyMismatchError) as excinfo:
            _connect("10.6.1.228", **kwargs)
    assert f'ssh-keygen -R "[10.6.1.228]:2222" -f {kh}' in str(excinfo.value)


def test_auth_and_network_failures_still_map_to_their_own_errors(tmp_path):
    """The new BadHostKey clause must not shadow the existing mappings."""
    paramiko = pytest.importorskip("paramiko")
    kh = tmp_path / "router_known_hosts"

    fake_client = MagicMock()
    fake_client.connect.side_effect = paramiko.AuthenticationException("nope")
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(ProbeAuthError):
            _connect("10.6.1.228", **_connect_kwargs(kh))

    fake_client = MagicMock()
    fake_client.connect.side_effect = OSError("timed out")
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(ProbeUnreachableError):
            _connect("10.6.1.228", **_connect_kwargs(kh))


def test_unreadable_known_hosts_fails_closed(tmp_path):
    """A corrupt/unreadable store must stop the connect, not fall back to TOFU."""
    kh = tmp_path / "router_known_hosts"
    fake_client = MagicMock()
    fake_client.load_host_keys.side_effect = OSError("boom")
    with patch("paramiko.SSHClient", return_value=fake_client):
        with pytest.raises(ProbeError):
            _connect("10.6.1.228", **_connect_kwargs(kh))
    fake_client.connect.assert_not_called()


def test_tofu_policy_records_first_contact_key_on_disk(tmp_path):
    """First contact is trusted, persisted 0600, and known to the next session."""
    paramiko = pytest.importorskip("paramiko")
    kh = tmp_path / "router_known_hosts"
    kh.write_text("")
    os.chmod(kh, 0o600)

    client = paramiko.SSHClient()
    client.load_host_keys(str(kh))
    key = paramiko.RSAKey.generate(2048)
    TofuHostKeyPolicy().missing_host_key(client, "10.6.1.228", key)

    assert "10.6.1.228" in kh.read_text()
    assert stat.S_IMODE(os.stat(kh).st_mode) == 0o600

    # A later session loads the recorded key, so paramiko itself (not the
    # policy) is what a changed key now has to get past.
    next_client = paramiko.SSHClient()
    next_client.load_host_keys(str(kh))
    stored = next_client.get_host_keys().lookup("10.6.1.228")
    assert stored is not None
    assert stored.get(key.get_name()) == key


def test_known_hosts_path_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "receivers.cfg"
    cfg.write_text(f"[teltonika]\nssh_known_hosts = {tmp_path / 'from_cfg'}\n")
    empty_cfg = tmp_path / "empty.cfg"
    empty_cfg.write_text("[teltonika]\nusername = admin\n")
    monkeypatch.setenv("RECEIVERS_ROUTER_KNOWN_HOSTS", str(tmp_path / "from_env"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert resolve_known_hosts_path(str(tmp_path / "explicit"), str(cfg)) == (
        tmp_path / "explicit"
    )
    assert resolve_known_hosts_path(None, str(cfg)) == tmp_path / "from_cfg"
    assert resolve_known_hosts_path(None, str(empty_cfg)) == tmp_path / "from_env"

    # configparser.get(..., fallback=…) still raises NoSectionError when the
    # whole section is missing — which is the normal shape of a laptop
    # receivers.cfg, so the fall-through has to survive it.
    no_section = tmp_path / "no_teltonika.cfg"
    no_section.write_text("[archive_paths]\ndata_prepath = /data\n")
    assert resolve_known_hosts_path(None, str(no_section)) == tmp_path / "from_env"

    monkeypatch.delenv("RECEIVERS_ROUTER_KNOWN_HOSTS")
    # Default is XDG *data*, never ~/.cache: a cache wipe would silently reset
    # trust-on-first-use for the whole fleet.
    assert resolve_known_hosts_path(None, str(empty_cfg)) == (
        tmp_path / "xdg" / "gps_receivers" / "router_known_hosts"
    )
