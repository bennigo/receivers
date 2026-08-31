"""Orphaned convert-cache entries must be reaped, and nothing else touched.

``prune_cache`` deletes an entry when its push goes durable -- the success path
and only the success path. A dry run (converts for real, records no
cache_entries, never pushes), a killed run, and a failed push all leave their
intermediates behind permanently. Measured on rek-d01 2026-08-31: 209 GB across
18,097 entries going back three weeks, on a volume an unreaped cache has filled
before and taken Postgres down with it.
"""

from __future__ import annotations

import os
import time

import pytest

from receivers.dissemination.convert import _CACHE_ENTRY_RE, reap_stale_cache

_OLD = time.time() - (30 * 86400)
_HASH = "a" * 64


def _aged(path, when=_OLD):
    os.utime(path, (when, when))
    return path


def _entry(root, name, *, aged=True, size=1024):
    d = root / name
    d.mkdir()
    (d / "product.crx.gz").write_bytes(b"x" * size)
    if aged:
        _aged(d / "product.crx.gz")
        _aged(d)
    return d


class TestItReapsWhatNoRunWillClaim:
    def test_an_aged_entry_is_removed(self, tmp_path):
        e = _entry(tmp_path, _HASH)
        removed, freed = reap_stale_cache(tmp_path, max_age_days=7)
        assert not e.exists()
        assert removed == 1 and freed >= 1024

    def test_a_fresh_entry_is_kept(self, tmp_path):
        """A live run's entries must survive a concurrent reap."""
        e = _entry(tmp_path, _HASH, aged=False)
        removed, _ = reap_stale_cache(tmp_path, max_age_days=7)
        assert e.exists() and removed == 0

    @pytest.mark.parametrize(
        "name",
        [_HASH, f"{_HASH}-s30", "decode", ".epos_decode_ab12_x", ".epos_hdr_q1w2"],
    )
    def test_every_shape_the_cache_actually_creates_is_reapable(self, tmp_path, name):
        _entry(tmp_path, name)
        removed, _ = reap_stale_cache(tmp_path, max_age_days=7)
        assert removed == 1, f"{name} was left behind"


class TestItRefusesToEatAnythingElse:
    """The reaper runs unattended against a CONFIGURABLE path."""

    @pytest.mark.parametrize(
        "name", ["gpsdata", "important-data", "2026", "ISAK", "rinex", _HASH[:63]]
    )
    def test_a_non_cache_name_is_never_deleted(self, tmp_path, name):
        e = _entry(tmp_path, name)
        removed, _ = reap_stale_cache(tmp_path, max_age_days=7)
        assert e.exists(), f"reaper deleted {name!r} — a mis-pointed cache dir"
        assert removed == 0

    def test_a_mispointed_cache_dir_reaps_nothing(self, tmp_path):
        """Point it at a real archive tree: it must be a no-op, not a disaster."""
        for name in ("2024", "2025", "ISAK", "raw", "rinex"):
            _entry(tmp_path, name)
        removed, freed = reap_stale_cache(tmp_path, max_age_days=7)
        assert (removed, freed) == (0, 0)
        assert len(list(tmp_path.iterdir())) == 5

    def test_a_missing_root_is_not_an_error(self, tmp_path):
        assert reap_stale_cache(tmp_path / "nope", max_age_days=7) == (0, 0)

    @pytest.mark.parametrize("max_age", [0, -1])
    def test_the_disable_value_does_not_reap_everything(self, tmp_path, max_age):
        """0 must mean OFF, not "older than zero days" — i.e. not the whole cache."""
        e = _entry(tmp_path, _HASH)
        assert reap_stale_cache(tmp_path, max_age_days=max_age) == (0, 0)
        assert e.exists(), (
            f"max_age_days={max_age} is the DISABLE value and it deleted the "
            "cache — the most damaging possible reading of the knob"
        )


class TestTheNameGuard:
    @pytest.mark.parametrize(
        "name,ok",
        [
            (_HASH, True),
            (f"{_HASH}-s30", True),
            ("decode", True),
            (".epos_decode_1pvkzk1l", True),
            ("gpsdata", False),
            ("..", False),
            (_HASH.upper(), False),
            (f"{_HASH}-s", False),
        ],
    )
    def test_it_matches_only_cache_shapes(self, name, ok):
        assert bool(_CACHE_ENTRY_RE.match(name)) is ok
