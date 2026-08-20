"""Tests for the vendor (RxTools ``rxupgrade``) firmware backend.

The load-bearing behaviour is that the flash verdict comes from the TRANSCRIPT and
not from the exit status: the vendor reconnects after the reboot to confirm, and
that reconnect times out whenever the reboot closed the port it arrived on. The
VMEY 5.6.0 → 5.7.0 upgrade on 2026-08-20 succeeded and still ended in an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from receivers.septentrio import rxupgrade as rxu

# Verbatim tail of ~/.cache/gps_receivers/logs/retrofits/vmey_fw570_20260820_0015.log
# — a SUCCESSFUL flash that reports an error.
VMEY_SUCCESS_TRANSCRIPT = """\
Opening connection port to receiver
Connected to the receiver's IP11 port
Storing receiver configuration to
  "/home/bgo/.septentrio/rxupgrade/PolaRx5_3018426_260820_001538_<configName>.txt"
Rebooting receiver in Upgrade mode
Uploading and processing "/home/bgo/git/gps_taeki/PolaRx5/firmware/5.7.0/firmware/PolaRx5-5.7.0.suf"
Upgrade Finished
Checking if upgrade succeeded
Error: Connection timed out.
"""

FAILED_TRANSCRIPT = """\
Opening connection port to receiver
Error: Connection refused.
"""


class TestParseTranscript:
    def test_vmey_success_is_a_flash_despite_the_trailing_error(self):
        flashed, verify_failed = rxu.parse_transcript(VMEY_SUCCESS_TRANSCRIPT)
        assert flashed is True
        assert verify_failed is True

    def test_no_upgrade_finished_is_not_a_flash(self):
        flashed, verify_failed = rxu.parse_transcript(FAILED_TRANSCRIPT)
        assert flashed is False
        assert verify_failed is False

    def test_clean_run_without_the_verify_error(self):
        text = VMEY_SUCCESS_TRANSCRIPT.replace("Error: Connection timed out.", "OK")
        flashed, verify_failed = rxu.parse_transcript(text)
        assert flashed is True
        assert verify_failed is False

    def test_empty_transcript_is_not_a_flash(self):
        assert rxu.parse_transcript("") == (False, False)


class TestBuildTarget:
    def test_native_port_is_a_bare_hostname(self):
        # The form proven against VMEY.
        assert rxu.build_target("vonc.gps.vedur.is") == "vonc.gps.vedur.is"
        assert rxu.build_target("vonc.gps.vedur.is", 28784) == "vonc.gps.vedur.is"

    def test_forwarded_port_is_appended(self):
        # Shared-IP stations such as OLAC/KASC.
        assert rxu.build_target("10.4.1.43", 28794) == "10.4.1.43:28794"


class TestBuildCommand:
    def _cmd(self, **kw):
        return rxu.build_command(
            Path("/usr/local/rxtools/bin/runRxupgrade"),
            host="vonc.gps.vedur.is",
            suf=Path("/fw/PolaRx5-5.7.0.suf"),
            **kw,
        )

    def test_restores_config_by_default(self):
        # Omitting -n is the whole point: letting the vendor restore the config is
        # what keeps plaintext 28784 open across the flash.
        assert "-n" not in self._cmd()

    def test_no_restore_config_passes_n(self):
        assert "-n" in self._cmd(restore_config=False)

    def test_always_batch_mode(self):
        assert self._cmd()[-1] == "-q"

    def test_credentials_and_log(self):
        cmd = self._cmd(username="user", password="secret", log_path=Path("/l/x.log"))
        assert cmd[cmd.index("-u") + 1] == "user"
        assert cmd[cmd.index("-p") + 1] == "secret"
        assert cmd[cmd.index("-l") + 1] == "/l/x.log"

    def test_ssl_and_legacy_flags(self):
        assert "-i" in self._cmd(ignore_ssl_errors=True)
        assert "-L" in self._cmd(legacy=True)
        assert "-i" not in self._cmd()
        assert "-L" not in self._cmd()


class TestRedact:
    def test_password_is_masked(self):
        cmd = rxu.build_command(
            Path("/bin/runRxupgrade"),
            host="h",
            suf=Path("/fw.suf"),
            username="user",
            password="hunter2",
        )
        out = rxu.redact(cmd)
        assert "hunter2" not in out
        assert "<password>" in out
        assert "user" in out  # the username is not a secret


class TestResolveBinary:
    def test_explicit_missing_path_raises(self):
        with pytest.raises(rxu.RxUpgradeError):
            rxu.resolve_binary("/nonexistent/runRxupgrade")

    def test_explicit_existing_path_is_used(self, tmp_path):
        exe = tmp_path / "runRxupgrade"
        exe.write_text("#!/bin/sh\n")
        assert rxu.resolve_binary(str(exe)) == exe


class TestRunUpgrade:
    def test_missing_suf_raises_before_running_anything(self, tmp_path):
        exe = tmp_path / "runRxupgrade"
        exe.write_text("#!/bin/sh\n")
        with pytest.raises(rxu.RxUpgradeError, match="firmware image not found"):
            rxu.run_upgrade(
                station_id="VONC",
                host="h",
                suf=tmp_path / "absent.suf",
                binary=str(exe),
            )

    def test_verdict_comes_from_the_log_file_not_the_exit_code(self, tmp_path):
        """A non-zero exit with 'Upgrade Finished' in the log is a SUCCESS."""
        suf = tmp_path / "PolaRx5-5.7.0.suf"
        suf.write_bytes(b"x")
        log = tmp_path / "run.log"
        exe = tmp_path / "fake_rxupgrade"
        exe.write_text(
            "#!/bin/sh\n"
            # write the VMEY-shaped transcript to the -l path, then fail
            f"cat > {log} <<'EOF'\n{VMEY_SUCCESS_TRANSCRIPT}EOF\n"
            "exit 1\n"
        )
        exe.chmod(0o755)

        res = rxu.run_upgrade(
            station_id="VONC",
            host="vonc.gps.vedur.is",
            suf=suf,
            log_path=log,
            binary=str(exe),
        )
        assert res.returncode == 1
        assert res.flashed is True
        assert res.ok is True
        assert res.verify_step_failed is True

    def test_zero_exit_without_upgrade_finished_is_a_failure(self, tmp_path):
        suf = tmp_path / "PolaRx5-5.7.0.suf"
        suf.write_bytes(b"x")
        exe = tmp_path / "fake_rxupgrade"
        exe.write_text("#!/bin/sh\necho 'Error: Connection refused.'\nexit 0\n")
        exe.chmod(0o755)

        res = rxu.run_upgrade(station_id="VONC", host="h", suf=suf, binary=str(exe))
        assert res.returncode == 0
        assert res.flashed is False
        assert res.ok is False
