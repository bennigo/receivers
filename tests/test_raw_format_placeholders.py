"""teqc's undecodable-input placeholders must never be read as measurements.

On Septentrio SBF — most of the fleet — `teqc +meta` reports the SAME
placeholder triple for every single file:

    start date & time:  1980-01-01
    final date & time:  1980-01-01
    antenna latitude:   90
    antenna longitude:  0

The position half was known and guarded downstream. The DATE half was not,
so `archive-sort` read 1980-01-01 as the file's true observation date,
classified every correctly-filed Septentrio file `wrong-date`, and planned
to relocate it to `1980/jan/` — where every file of a station also collides
on one filename.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from receivers.archive.raw_format import RawMeta, _drop_placeholders


def _vals(**kw):
    base = dict(
        start=datetime(2025, 7, 1), end=datetime(2025, 7, 1, 23), lat=64.1, lon=-21.9
    )
    base.update(kw)
    return base


class TestDatePlaceholder:
    @pytest.mark.parametrize(
        "stamp",
        [datetime(1980, 1, 1), datetime(1979, 12, 31), datetime(1980, 1, 5)],
    )
    def test_pre_gps_start_becomes_none(self, stamp):
        """Nothing was observed before GPS time zero (1980-01-06)."""
        out = _drop_placeholders(_vals(start=stamp), "f", "sbf")
        assert out["start"] is None

    def test_pre_gps_end_becomes_none(self):
        out = _drop_placeholders(_vals(end=datetime(1980, 1, 1)), "f", "sbf")
        assert out["end"] is None

    def test_a_real_date_survives(self):
        out = _drop_placeholders(_vals(), "f", "ashtech")
        assert out["start"] == datetime(2025, 7, 1)

    def test_gps_time_zero_itself_is_kept(self):
        """The boundary is exclusive — 1980-01-06 is a real (if absurd) epoch."""
        out = _drop_placeholders(_vals(start=datetime(1980, 1, 6)), "f", "sbf")
        assert out["start"] == datetime(1980, 1, 6)

    def test_none_start_stays_none(self):
        assert _drop_placeholders(_vals(start=None), "f", "sbf")["start"] is None


class TestPositionPlaceholder:
    def test_north_pole_becomes_none(self):
        out = _drop_placeholders(_vals(lat=90.0, lon=0.0), "f", "sbf")
        assert out["lat"] is None and out["lon"] is None

    def test_a_real_position_survives(self):
        out = _drop_placeholders(_vals(), "f", "ashtech")
        assert (out["lat"], out["lon"]) == (64.1, -21.9)

    def test_only_the_exact_pair_is_a_placeholder(self):
        """A real station at lat 90 with a non-zero lon is not the sentinel.

        (There is no such station, but the check must key on the PAIR — a
        latitude test alone would blank real high-latitude positions.)
        """
        out = _drop_placeholders(_vals(lat=90.0, lon=-21.9), "f", "sbf")
        assert out["lat"] == 90.0


class TestTheSeptentrioTriple:
    def test_the_real_sbf_response_yields_nothing_usable(self):
        """Exactly what teqc reports for every .sbf.gz — measured on VMOS."""
        out = _drop_placeholders(
            dict(
                start=datetime(1980, 1, 1), end=datetime(1980, 1, 1), lat=90.0, lon=0.0
            ),
            "VMOS202507010000a.sbf.gz",
            "sbf",
        )
        meta = RawMeta(**out)
        assert meta.start is None
        assert meta.lat is None and meta.lon is None
        assert meta.span is None
