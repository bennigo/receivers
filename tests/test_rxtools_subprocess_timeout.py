"""A wedged bin2asc must never block its caller.

Regression cover for the 2026-08-11 rek-d01 incident: bin2asc hung on a
52-byte DiskStatus block and the unbounded ``subprocess.run`` blocked the
health worker that spawned it. The 5-minute health cycle kept adding more,
77 wedged processes accumulated over 2.9 h, and health checks went stale
fleet-wide. Every RxTools invocation must therefore pass a timeout and
degrade instead of hanging.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.utils import rxtools_extractor as rx


class TestTimeoutIsAlwaysPassed:
    """Every RxTools subprocess call must be bounded."""

    def test_extract_sbf_message_passes_timeout(self, tmp_path: Path):
        sbf = tmp_path / "data.sbf"
        sbf.write_bytes(b"\x24\x40" + b"\x00" * 50)

        def fake_run(cmd, **kwargs):
            # The output file bin2asc would have produced.
            (tmp_path / f"{sbf.name}_SBF_DiskStatus.txt").write_text("h1,h2\n1,2\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(Path, "exists", return_value=True), patch(
            "subprocess.run", side_effect=fake_run
        ) as mock_run:
            rx.extract_sbf_message(sbf, "DiskStatus", output_dir=tmp_path)

        assert mock_run.call_args.kwargs.get("timeout") == rx.RXTOOLS_TIMEOUT_S

    def test_explicit_timeout_overrides_default(self, tmp_path: Path):
        sbf = tmp_path / "data.sbf"
        sbf.write_bytes(b"\x24\x40" + b"\x00" * 50)

        def fake_run(cmd, **kwargs):
            (tmp_path / f"{sbf.name}_SBF_DiskStatus.txt").write_text("h1\n1\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(Path, "exists", return_value=True), patch(
            "subprocess.run", side_effect=fake_run
        ) as mock_run:
            rx.extract_sbf_message(sbf, "DiskStatus", output_dir=tmp_path, timeout=7)

        assert mock_run.call_args.kwargs.get("timeout") == 7

    def test_list_available_messages_passes_timeout(self):
        with patch.object(Path, "exists", return_value=True), patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "- DiskStatus\n", ""),
        ) as mock_run:
            assert rx.list_available_messages() == ["DiskStatus"]

        assert mock_run.call_args.kwargs.get("timeout") == rx.RXTOOLS_BYTES_TIMEOUT_S


class TestTimeoutDegradesGracefully:
    """A timeout must surface as a handled failure, never as a hang."""

    def test_extract_sbf_message_raises_runtime_error(self, tmp_path: Path):
        """TimeoutExpired becomes RuntimeError, which every caller handles."""
        sbf = tmp_path / "data.sbf"
        sbf.write_bytes(b"\x24\x40")

        with patch.object(Path, "exists", return_value=True), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["bin2asc"], timeout=30),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                rx.extract_sbf_message(sbf, "DiskStatus", output_dir=tmp_path)

    def test_parse_sbf_bytes_returns_empty_on_timeout(self):
        """The path that actually wedged: 52-byte DiskStatus block."""
        wedging_block = b"\x24\x40\x3c\xab\xdb\x2f\x34\x00" + b"\x00" * 44
        assert len(wedging_block) == 52

        with patch.object(Path, "exists", return_value=True), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["bin2asc"], timeout=30),
        ):
            assert rx.parse_sbf_bytes(wedging_block, "DiskStatus") == []

    def test_parse_sbf_bytes_uses_short_timeout(self):
        """A single block is tiny — it must not wait the full file budget."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

        with patch.object(Path, "exists", return_value=True), patch(
            "subprocess.run", side_effect=fake_run
        ):
            rx.parse_sbf_bytes(b"\x24\x40" + b"\x00" * 50, "DiskStatus")

        assert captured["timeout"] == rx.RXTOOLS_BYTES_TIMEOUT_S
        assert rx.RXTOOLS_BYTES_TIMEOUT_S < rx.RXTOOLS_TIMEOUT_S

    def test_parse_sbf_bytes_cleans_up_tmpdir_on_timeout(self):
        """A wedge must not leak /tmp/sbf_parse_* dirs (60 were orphaned)."""
        created = []
        real_mkdtemp = rx.tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        with patch.object(Path, "exists", return_value=True), patch.object(
            rx.tempfile, "mkdtemp", side_effect=tracking_mkdtemp
        ), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["bin2asc"], timeout=30),
        ):
            rx.parse_sbf_bytes(b"\x24\x40" + b"\x00" * 50, "DiskStatus")

        assert created, "expected a temp dir to have been created"
        assert not any(p.exists() for p in created)
