"""Data-presence check, and its authority over the size heuristic.

The size rule alone is fragile: disabling GLONASS/Galileo/BeiDou legitimately
shrinks a file several-fold, and a size-only guard would quarantine that station
until its baseline caught up. So an undersized file is only rejected when a
content decode CONFIRMS it holds no observation epochs.
"""

import gzip
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

from receivers.utils.data_presence import (
    SBF_MEAS_EPOCH,
    has_observations,
    sbf_epoch_count,
)
from receivers.utils.file_archiver import ArchiveMode, FileArchiver
from receivers.utils.yield_guard import YieldGuardConfig

HEALTHY_MEDIAN = 461_902
RFEL_SIZE = 8_029


def _sbf_block(block_id: int, payload: bytes = b"\0" * 8) -> bytes:
    """One well-formed SBF block: sync, crc, id, length, payload."""
    length = 8 + len(payload)
    pad = (-length) % 4
    payload += b"\0" * pad
    length += pad
    return b"$@" + struct.pack("<HHH", 0, block_id, length) + payload


def _sbf_file(tmp_path: Path, n_epochs: int, name="X.sbf", gz=False) -> Path:
    body = b"".join(_sbf_block(SBF_MEAS_EPOCH) for _ in range(n_epochs))
    body += _sbf_block(4007)  # a non-measurement block, always present
    p = tmp_path / (name + (".gz" if gz else ""))
    if gz:
        with gzip.open(p, "wb") as fh:
            fh.write(body)
    else:
        p.write_bytes(body)
    return p


# --- SBF decoding -----------------------------------------------------------


def test_counts_measurement_epochs(tmp_path):
    assert sbf_epoch_count(_sbf_file(tmp_path, 3600)) == 3600


def test_counts_zero_when_only_non_measurement_blocks(tmp_path):
    """The RFEL shape: the file decodes, it just holds no observations."""
    assert sbf_epoch_count(_sbf_file(tmp_path, 0)) == 0


def test_reads_gzipped_files(tmp_path):
    assert sbf_epoch_count(_sbf_file(tmp_path, 5, gz=True)) == 5


def test_sync_bytes_inside_a_payload_do_not_inflate_the_count(tmp_path):
    """Walking the block chain by length must beat naive '$@' scanning."""
    p = tmp_path / "Y.sbf"
    p.write_bytes(_sbf_block(SBF_MEAS_EPOCH, b"$@" + b"\0" * 6))
    assert sbf_epoch_count(p) == 1


def test_has_observations_maps_counts_to_a_verdict(tmp_path):
    assert has_observations(_sbf_file(tmp_path, 10, name="A.sbf")) is True
    assert has_observations(_sbf_file(tmp_path, 0, name="B.sbf")) is False


def test_unknown_for_formats_we_cannot_decode(tmp_path):
    p = tmp_path / "Z.m00"
    p.write_bytes(b"leica")
    assert has_observations(p) is None


def test_unreadable_file_is_unknown_not_empty(tmp_path):
    """Failing to read says nothing about the data — must not read as 'no data'."""
    assert has_observations(tmp_path / "does-not-exist.sbf") is None


# --- Trimble ----------------------------------------------------------------


def _teqc_meta(start, final):
    proc = MagicMock()
    proc.stdout = (
        f"start date & time:       {start}\nfinal date & time:       {final}\n"
    )
    return proc


def test_trimble_degenerate_span_means_no_observations(tmp_path):
    p = tmp_path / "RFEL202608142300b.T00"
    p.write_bytes(b"\0" * 100)
    dat = tmp_path / "RFEL202608142300b.dat"
    dat.write_bytes(b"\0")
    with (
        patch(
            "receivers.dissemination.convert.resolve_tool",
            side_effect=["runpkr", "teqc"],
        ),
        patch("subprocess.run") as run,
        patch("pathlib.Path.glob", return_value=iter([dat])),
    ):
        run.side_effect = [
            MagicMock(),
            _teqc_meta("1980-01-01 00:00:00.000", "1980-01-01 00:00:00.000"),
        ]
        assert has_observations(p) is False


def test_trimble_real_span_means_observations(tmp_path):
    p = tmp_path / "SAUR202608142300b.T00"
    p.write_bytes(b"\0" * 100)
    dat = tmp_path / "SAUR202608142300b.dat"
    dat.write_bytes(b"\0")
    with (
        patch(
            "receivers.dissemination.convert.resolve_tool",
            side_effect=["runpkr", "teqc"],
        ),
        patch("subprocess.run") as run,
        patch("pathlib.Path.glob", return_value=iter([dat])),
    ):
        run.side_effect = [
            MagicMock(),
            _teqc_meta("2026-08-14 23:00:00.000", "2026-08-14 23:59:59.000"),
        ]
        assert has_observations(p) is True


def test_missing_trimble_tools_is_unknown(tmp_path):
    p = tmp_path / "RFEL.T00"
    p.write_bytes(b"\0" * 10)
    with patch(
        "receivers.dissemination.convert.resolve_tool", side_effect=Exception("no tool")
    ):
        assert has_observations(p) is None


# --- content overrides size -------------------------------------------------


def _conn(median):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        median,
        500,
    )
    return conn


def _archiver(tmp_path):
    return FileArchiver(
        mode=ArchiveMode.IMMEDIATE,
        yield_guard=YieldGuardConfig(
            connection=_conn(HEALTHY_MEDIAN), quarantine_root=tmp_path / "q"
        ),
    )


def _paths(tmp_path, name="RFEL202608142100a.sbf"):
    src = tmp_path / "tmp" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    dest = tmp_path / "arch" / "2026" / "aug" / "RFEL" / "1Hz_1hr" / "raw" / name
    return src, dest


def test_undersized_but_has_data_is_archived(tmp_path):
    """THE constellation-change case: small file, real observations — keep it."""
    src, dest = _paths(tmp_path)
    src.write_bytes(b"x" * RFEL_SIZE)
    with patch("receivers.utils.data_presence.has_observations", return_value=True):
        assert _archiver(tmp_path).archive_file(src, dest, compress=False) is True
    assert dest.exists()


def test_undersized_and_provably_empty_is_quarantined(tmp_path):
    src, dest = _paths(tmp_path)
    src.write_bytes(b"x" * RFEL_SIZE)
    with patch("receivers.utils.data_presence.has_observations", return_value=False):
        assert _archiver(tmp_path).archive_file(src, dest, compress=False) is False
    assert not dest.exists()
    assert (tmp_path / "q" / "RFEL" / src.name).exists()


def test_undersized_and_undeterminable_is_archived(tmp_path):
    """UNKNOWN must never be treated as 'no data'."""
    src, dest = _paths(tmp_path)
    src.write_bytes(b"x" * RFEL_SIZE)
    with patch("receivers.utils.data_presence.has_observations", return_value=None):
        assert _archiver(tmp_path).archive_file(src, dest, compress=False) is True
    assert dest.exists()


def test_normal_sized_file_never_triggers_a_content_decode(tmp_path):
    """The decode is a subprocess for Trimble — it must not run on every file."""
    src, dest = _paths(tmp_path, name="SAUR202608142100a.sbf")
    src.write_bytes(b"x" * HEALTHY_MEDIAN)
    with patch("receivers.utils.data_presence.has_observations") as ho:
        assert _archiver(tmp_path).archive_file(src, dest, compress=False) is True
    ho.assert_not_called()
