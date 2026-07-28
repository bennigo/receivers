"""A chain that stops early must not strand its in-archive working file."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from receivers.rinex.converter_base import ConversionError, ConversionResult


def _run(tmp_path, fail_at):
    from receivers.rinex.sbf_converter import SBFConverter

    raw = tmp_path / "TEST202607260000a.sbf.gz"
    raw.write_bytes(b"x" * 64)
    out = tmp_path / "rinex"
    out.mkdir()
    work = out / "TEST202607260000a.26o"

    def fake_run_conversion(raw_path, output_dir, obs_date):
        work.write_bytes(b"y" * 4096)  # the in-archive intermediate
        return work

    conv = SBFConverter(station_id="TEST", apply_header_corrections=False)
    with (
        patch.object(SBFConverter, "_run_conversion", side_effect=fake_run_conversion),
        patch.object(SBFConverter, "_validate_raw_content", return_value=None),
        patch.object(SBFConverter, fail_at, side_effect=ConversionError("boom", raw)),
    ):
        res = conv.convert_file(
            raw,
            output_dir=out,
            observation_date=datetime(2026, 7, 26),
        )
    return res, work


def test_stranded_intermediate_is_removed_on_failure(tmp_path):
    res, work = _run(tmp_path, "_verify_conversion_identity")
    assert res.success is False
    assert not work.exists(), "intermediate was left in the archive rinex/ dir"


def test_failure_later_in_the_chain_also_cleans_up(tmp_path):
    res, work = _run(tmp_path, "_canonicalize_rinex")
    assert res.success is False
    assert not work.exists()
