"""Convert-cache integrity: atomic writes + poison-heal on cache hit.

A killed/crashed convert used to leave a truncated obs (or partial packaged
artifact) in the shared cache; cache-hits then served it forever, causing false
QC blocks and RNX2CRX "truncated" package failures until a manual clear. These
tests lock in the fix:

- writes into the cache are atomic (staged on the same fs, os.replace last), so a
  crash mid-header-rewrite never leaves a partial in the cache dir;
- a pre-existing incomplete obs on the hit path is detected, evicted, and
  re-converted rather than served.

The conversion tool steps (_to_plain_obs / _canonical_obs) are stubbed so the
tests are deterministic and don't need gps-tools binaries — they exercise exactly
the placement/eviction logic, not the external converters.
"""

import gzip
from datetime import datetime
from pathlib import Path

import pytest

from receivers.dissemination import convert as dconv
from receivers.dissemination.config import DisseminationFormat
from receivers.dissemination.convert import (
    _obs_complete,
    _packaged_valid,
    convert_for_dissemination,
)

_RINEX_HEADER = (
    "     3.04           OBSERVATION DATA    M                   "
    "RINEX VERSION / TYPE\n"
)
_END = "                                                            " "END OF HEADER\n"


def _complete_obs_text() -> str:
    body = "".join(f"> 2026 05 08 00 00 {i:02d}.0000000  0  0\n" for i in range(20))
    return (
        _RINEX_HEADER + ("G    R    E                          SYS\n" * 8) + _END + body
    )


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- #
# Pure validators
# --------------------------------------------------------------------------- #
def test_obs_complete_true_for_full_header(tmp_path):
    f = _write(tmp_path / "ok.rnx", _complete_obs_text())
    assert _obs_complete(f) is True


def test_obs_complete_false_without_end_of_header(tmp_path):
    # header write cut short before END OF HEADER (the poison shape)
    f = _write(tmp_path / "part.rnx", _RINEX_HEADER + "X" * 400)
    assert _obs_complete(f) is False


def test_obs_complete_false_when_tiny(tmp_path):
    f = _write(tmp_path / "tiny.rnx", "junk")
    assert _obs_complete(f) is False


def test_obs_complete_false_when_missing(tmp_path):
    assert _obs_complete(tmp_path / "nope.rnx") is False


def test_packaged_valid_true_for_good_gzip(tmp_path):
    import os as _os

    p = tmp_path / "x.crx.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(_os.urandom(4096))  # incompressible → gzipped stays well over floor
    assert _packaged_valid(p) is True


def test_packaged_valid_false_for_corrupt_gzip(tmp_path):
    p = tmp_path / "bad.crx.gz"
    p.write_bytes(b"\x1f\x8b" + b"\x00" * 400)  # gzip magic, garbage body
    assert _packaged_valid(p) is False


def test_packaged_valid_false_when_tiny(tmp_path):
    p = tmp_path / "t.crx.gz"
    p.write_bytes(b"\x1f\x8b")
    assert _packaged_valid(p) is False


def test_packaged_valid_z_size_floor(tmp_path):
    p = tmp_path / "x.24d.Z"
    p.write_bytes(b"\x1f\x9d" + b"\x00" * 500)  # .Z magic, no portable test → floor
    assert _packaged_valid(p) is True


# --------------------------------------------------------------------------- #
# Cache placement / eviction (conversion internals stubbed)
# --------------------------------------------------------------------------- #
@pytest.fixture
def stub_convert(monkeypatch):
    """Make convert_for_dissemination produce a deterministic complete obs without
    gps-tools: _to_plain_obs returns a copy in tmpdir, _canonical_obs writes the
    canonical obs into tmpdir. detect_rinex_version reads the (valid) header."""

    def fake_to_plain(src, tmpdir):
        dst = Path(tmpdir) / "plain.rnx"
        dst.write_text(_complete_obs_text())
        return dst

    def fake_canonical(plain, station, dt, version, naming, cc, tmpdir, **kw):
        obs_name = f"{station}00ISL_R_20261280000_01D_15S_MO.rnx"
        canon = Path(tmpdir) / obs_name
        canon.write_text(_complete_obs_text())
        return canon, obs_name

    monkeypatch.setattr(dconv, "_to_plain_obs", fake_to_plain)
    monkeypatch.setattr(dconv, "_canonical_obs", fake_canonical)


def _src(tmp_path) -> Path:
    return _write(tmp_path / "FIM21280.26d.gz.src", _complete_obs_text())


def test_convert_places_complete_obs(stub_convert, tmp_path):
    cache = tmp_path / "cache"
    res = convert_for_dissemination(
        _src(tmp_path),
        "FIM2",
        datetime(2026, 5, 8),
        fmt=DisseminationFormat(),
        cache_dir=cache,
    )
    assert res.cached is False
    assert res.output_path.is_file()
    assert _obs_complete(res.output_path)


def test_crash_mid_finalize_leaves_no_partial_in_cache(
    stub_convert, tmp_path, monkeypatch
):
    """A kill during header finalize must NOT leave a partial obs in the cache —
    the mutation happens on the same-fs temp before the atomic placement."""

    def half_write_then_die(path, *a, **k):
        Path(path).write_text(_RINEX_HEADER + "TRUNCATED")  # no END OF HEADER
        raise RuntimeError("killed mid-finalize")

    monkeypatch.setattr(dconv, "set_header_from_tos", lambda *a, **k: None)
    monkeypatch.setattr(dconv, "finalize_epos_header", half_write_then_die)

    cache = tmp_path / "cache"
    with pytest.raises(RuntimeError):
        convert_for_dissemination(
            _src(tmp_path),
            "FIM2",
            datetime(2026, 5, 8),
            fmt=DisseminationFormat(),
            cache_dir=cache,
            set_header=True,
        )

    # The invariant is "no INCOMPLETE obs in the cache", not "no obs at all".
    #
    # This used to assert `cache.rglob("*.rnx") == []`. That was right when the
    # cache had one layer, and became wrong with the decode/head split: the
    # decode layer legitimately keeps the fully decoded .rnx so a later --force
    # can re-head without paying for another raw decode. Its presence is the
    # point of that design, not a leak.
    #
    # Verified rather than assumed: at this point the decode entry is complete
    # (END OF HEADER present, no "TRUNCATED" marker) — the half-written file
    # died in the same-fs temp dir before atomic placement, which is exactly
    # what the test is here to prove.
    partial = [p for p in cache.rglob("*.rnx") if p.is_file() and not _obs_complete(p)]
    assert partial == [], f"partial obs leaked into cache: {partial}"

    # And the truncation the stub wrote must not be anywhere in the cache at all.
    leaked = [
        p
        for p in cache.rglob("*")
        if p.is_file() and "TRUNCATED" in p.read_text(errors="ignore")
    ]
    assert leaked == [], f"truncated content leaked into cache: {leaked}"


def test_poisoned_cache_hit_is_evicted_and_reconverted(stub_convert, tmp_path):
    """A truncated obs pre-planted in the cache (pre-fix poison) is not served —
    it's evicted and re-converted to a complete obs."""
    cache = tmp_path / "cache"
    kw = dict(fmt=DisseminationFormat(), cache_dir=cache)

    first = convert_for_dissemination(
        _src(tmp_path), "FIM2", datetime(2026, 5, 8), **kw
    )
    assert first.cached is False

    # Poison the cached obs in place (simulate an interrupted in-place rewrite).
    first.output_path.write_text(_RINEX_HEADER + "TRUNCATED")  # no END OF HEADER
    assert not _obs_complete(first.output_path)

    second = convert_for_dissemination(
        _src(tmp_path), "FIM2", datetime(2026, 5, 8), **kw
    )
    assert second.cached is False, "poisoned hit should evict + re-convert, not serve"
    assert _obs_complete(second.output_path)


def test_clean_cache_hit_still_served(stub_convert, tmp_path):
    """A complete cached obs is served as a hit (no needless re-convert)."""
    cache = tmp_path / "cache"
    kw = dict(fmt=DisseminationFormat(), cache_dir=cache)
    convert_for_dissemination(_src(tmp_path), "FIM2", datetime(2026, 5, 8), **kw)
    second = convert_for_dissemination(
        _src(tmp_path), "FIM2", datetime(2026, 5, 8), **kw
    )
    assert second.cached is True
