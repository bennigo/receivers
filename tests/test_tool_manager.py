"""Tests for tool_manager download integrity and safe archive extraction.

The tool installer fetches binaries that the daily conversion pipeline then
executes, so two properties matter here:

1. a download whose SHA-256 does not match a pinned digest must never reach the
   tools bin/ directory, and
2. no archive member may be written outside the extraction directory
   (zip-slip / tar-slip).

Every test drives the real installers with ``urlretrieve`` monkeypatched to
copy a locally built archive, so the checks run over the production code path.
"""

import io
import logging
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from receivers.tools.tool_manager import ToolManager, _sha256_file

TEQC_URL = "https://example.invalid/teqc.zip"
RNXCMP_URL = "https://example.invalid/rnxcmp.tar.gz"


@pytest.fixture
def manager(tmp_path):
    """ToolManager rooted in tmp_path so nothing touches ~/.local/share."""
    mgr = ToolManager(tools_dir=tmp_path / "tools")
    # _init_tool_definitions() rebuilds self.TOOLS per instance, so mutating
    # these entries cannot leak into another test.
    mgr.TOOLS["teqc"].download_url = TEQC_URL
    mgr.TOOLS["teqc"].auto_install = True
    mgr.TOOLS["rnx2crx"].download_url = RNXCMP_URL
    mgr.TOOLS["rnx2crx"].auto_install = True
    return mgr


def _serve(monkeypatch, archive: Path):
    """Make urlretrieve() deliver ``archive`` instead of hitting the network."""

    def fake_urlretrieve(url, dest):
        Path(dest).write_bytes(archive.read_bytes())
        return str(dest), None

    monkeypatch.setattr("receivers.tools.tool_manager.urlretrieve", fake_urlretrieve)


def _make_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            # ZipInfo, not write(): zf.write() would normalise the crafted
            # traversal names we are testing against.
            zf.writestr(zipfile.ZipInfo(name), data)
    return path


def _make_targz(path: Path, members: dict) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return path


# --- zip / teqc ------------------------------------------------------------


def test_teqc_install_benign_zip(manager, tmp_path, monkeypatch):
    """Regression guard: a normal archive still installs an executable binary."""
    archive = _make_zip(tmp_path / "teqc.zip", {"teqc": b"#!/bin/sh\nexit 0\n"})
    _serve(monkeypatch, archive)

    result = manager.install("teqc", force=True)

    assert result.success, result.message
    dest = manager.bin_dir / "teqc"
    assert dest.exists()
    assert os.access(dest, os.X_OK)


def test_teqc_nested_zip_extracts_but_binary_search_is_top_level(
    manager, tmp_path, monkeypatch
):
    """A nested member must pass validation — the member check is not the limit.

    Documents a PRE-EXISTING limitation, not a regression: _install_teqc scans
    only the top level of the extraction dir, so a binary one directory down is
    not found. The point of this test is that the safety check does not reject
    the nested layout; if the real UNAVCO zip turns out to nest the binary, the
    fix belongs in the search loop, and this test will flip with it.
    """
    archive = _make_zip(
        tmp_path / "teqc.zip",
        {"teqc_CentOSLx86_64d/": b"", "teqc_CentOSLx86_64d/teqc": b"binary"},
    )
    _serve(monkeypatch, archive)

    result = manager.install("teqc", force=True)

    assert not result.success
    assert "Could not find teqc binary" in result.message


def test_teqc_zip_slip_rejected(manager, tmp_path, monkeypatch):
    """A '../' member must abort the install, not escape the temp dir."""
    archive = _make_zip(tmp_path / "evil.zip", {"../evil": b"pwn", "teqc": b"binary"})
    _serve(monkeypatch, archive)

    result = manager.install("teqc", force=True)

    assert not result.success
    assert "escapes" in result.message
    assert not (manager.bin_dir / "teqc").exists()
    # Nothing may have been written next to any extraction directory either.
    assert not list(tmp_path.rglob("evil"))


def test_teqc_absolute_zip_member_rejected(manager, tmp_path, monkeypatch):
    """Absolute member names take a different code path than '../' traversal."""
    archive = _make_zip(tmp_path / "abs.zip", {"/tmp/evil": b"pwn"})
    _serve(monkeypatch, archive)

    result = manager.install("teqc", force=True)

    assert not result.success
    assert "absolute path" in result.message


def test_teqc_windows_drive_member_rejected(manager, tmp_path, monkeypatch):
    """A drive-qualified name is absolute too, and startswith('/') misses it."""
    archive = _make_zip(tmp_path / "drive.zip", {"C:\\evil": b"pwn"})
    _serve(monkeypatch, archive)

    result = manager.install("teqc", force=True)

    assert not result.success
    assert "absolute path" in result.message


# --- checksum pinning ------------------------------------------------------


def test_checksum_mismatch_leaves_nothing_installed(manager, tmp_path, monkeypatch):
    """Fail closed: a wrong digest must not leave a binary on the tool path."""
    archive = _make_zip(tmp_path / "teqc.zip", {"teqc": b"binary"})
    _serve(monkeypatch, archive)
    manager.TOOLS["teqc"].sha256 = "00" * 32

    result = manager.install("teqc", force=True)

    assert not result.success
    assert "Checksum mismatch" in result.message
    assert not (manager.bin_dir / "teqc").exists()


def test_checksum_match_installs(manager, tmp_path, monkeypatch):
    archive = _make_zip(tmp_path / "teqc.zip", {"teqc": b"binary"})
    _serve(monkeypatch, archive)
    manager.TOOLS["teqc"].sha256 = _sha256_file(archive).upper()  # case-insensitive

    result = manager.install("teqc", force=True)

    assert result.success, result.message
    assert (manager.bin_dir / "teqc").exists()


def test_unpinned_download_warns_with_digest(manager, tmp_path, monkeypatch, caplog):
    """Unpinned installs proceed but must log an actionable digest to pin."""
    archive = _make_zip(tmp_path / "teqc.zip", {"teqc": b"binary"})
    _serve(monkeypatch, archive)
    manager.TOOLS["teqc"].sha256 = None

    with caplog.at_level(logging.WARNING, logger="receivers.tools.tool_manager"):
        result = manager.install("teqc", force=True)

    assert result.success, result.message
    assert _sha256_file(archive) in caplog.text


def test_insecure_url_refused(manager, tmp_path, monkeypatch):
    """http:// gives an on-path attacker the binary; refuse before downloading."""
    archive = _make_zip(tmp_path / "teqc.zip", {"teqc": b"binary"})
    _serve(monkeypatch, archive)
    manager.TOOLS["teqc"].download_url = "http://example.invalid/teqc.zip"

    result = manager.install("teqc", force=True)

    assert not result.success
    assert "insecure URL" in result.message


# --- tar / hatanaka --------------------------------------------------------


def test_hatanaka_install_benign_tar(manager, tmp_path, monkeypatch):
    """Mirrors the real RNXCMP layout: binaries live in a subdirectory."""
    archive = _make_targz(
        tmp_path / "rnxcmp.tar.gz",
        {
            "RNXCMP_4.1.0/bin/RNX2CRX": b"rnx2crx",
            "RNXCMP_4.1.0/bin/CRX2RNX": b"crx2rnx",
        },
    )
    _serve(monkeypatch, archive)

    result = manager.install("rnx2crx", force=True)

    assert result.success, result.message
    for name in ("RNX2CRX", "CRX2RNX"):
        dest = manager.bin_dir / name
        assert dest.exists()
        assert os.access(dest, os.X_OK)


def test_hatanaka_tar_slip_rejected(manager, tmp_path, monkeypatch):
    archive = _make_targz(
        tmp_path / "evil.tar.gz",
        {"../../evil": b"pwn", "RNXCMP/bin/RNX2CRX": b"rnx2crx"},
    )
    _serve(monkeypatch, archive)

    result = manager.install("rnx2crx", force=True)

    assert not result.success
    assert "escapes" in result.message
    assert not (manager.bin_dir / "RNX2CRX").exists()
    assert not list(tmp_path.rglob("evil"))


def test_hatanaka_absolute_tar_member_rejected(manager, tmp_path, monkeypatch):
    """tarfile only *strips* a leading '/'; we abort instead, and say why."""
    archive = _make_targz(tmp_path / "abs.tar.gz", {"/tmp/evil": b"pwn"})
    _serve(monkeypatch, archive)

    result = manager.install("rnx2crx", force=True)

    assert not result.success
    assert "absolute path" in result.message
    assert not list(tmp_path.rglob("evil"))
