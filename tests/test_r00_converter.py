"""Trimble R00 raw (4000SSi/4000Si era) is convertible — runpkr00 + teqc.

The pre-2013 archive holds ``.r00`` containers that nothing in the pipeline
could read, so ``raw_presence`` classified those days as UN-regenerable and the
fix-headers gate routed them to ``rinex_org`` preservation instead of a
re-conversion. That was a gap, not a fact: the chain is identical to ``.T02``
(runpkr00 unpacks to a binary ``.dat``, teqc decodes it), which
``TrimbleConverter`` already implements.

Verified by hand against VMEY201006012359a.r00 (2010-06-02 data): runpkr00
returned 8,495 records, teqc produced 5,760 epochs at 15 s — a complete 24-hour
day — with header receiver ``26093 TRIMBLE 4000SSI`` matching TOS's join for
that era.

The one thing that must NOT be left to the decoder is the GPS week. R00 predates
the week-number rollover and the stream carries a 10-bit week, so teqc guesses::

    ? Error ? translation ... may have started with GPS week 2432
              rather than 1586  (try using '-week 1586' option)

It guessed right on that file. A wrong guess is silent and lands the data ~19.6
years away, so the week is derived from the known observation date instead.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from receivers.archive.raw_format import TRIMBLE_R00, classify_raw
from receivers.rinex import R00Converter
from receivers.rinex.raw_presence import KNOWN_RAW_EXTENSIONS

# The real first 32 bytes of VMEY201006012359a.r00.
R00_HEAD = b"n\x00TNL RAW DATA IMAGE\x00\x00\x05\x00\xd9\x02\xd9\x02\x03\x00\x01\x00"


class TestContentIdentification:
    def test_r00_is_identified_by_magic_not_extension(self):
        # .T02/.T00 have no printable magic and fall back to the extension;
        # R00 carries "TNL RAW DATA IMAGE", so a mislabelled file still routes.
        assert classify_raw(head=R00_HEAD) == TRIMBLE_R00

    def test_a_t02_container_is_not_mistaken_for_r00(self):
        # Real .T02 head: a length prefix then a bzip2 payload — no TNL magic.
        t02 = b"\x00\x00\x00\rt\xbayK\xcfk\x08<\x81\x03\x08\x0b\x01\x05\x00P\xdbBZh"
        assert classify_raw(head=t02) != TRIMBLE_R00

    def test_sbf_still_wins(self):
        assert classify_raw(head=b"$@" + b"\x00" * 30) == "sbf"


class TestRegenerability:
    def test_r00_is_deliberately_NOT_treated_as_regenerable_yet(self):
        # Reverted after review. check_regenerable matches `date_tag in p.name`,
        # and the archive names R00 by SESSION START (…YYYYMMDD2359a.r00, data
        # belonging to the NEXT day). So for day D the matcher picks the file
        # stamped D, which holds D+1 — listing .r00 here would report
        # regenerable=True off the WRONG DAY's raw and make fix-headers skip
        # rinex_org preservation for ~977 irreplaceable VMEY days.
        # Un-recognised is the safe direction: those days get preserved.
        assert ".r00" not in KNOWN_RAW_EXTENSIONS

    def test_the_other_formats_are_untouched(self):
        assert {".sbf", ".t02", ".t00", ".m00"} <= KNOWN_RAW_EXTENSIONS

    def test_the_converter_still_exists_for_explicit_use(self):
        # The gate and the converter are separate concerns: --r00 can still
        # convert on purpose; what is withheld is the automatic claim that such
        # a day needs no preservation.
        assert R00Converter("VMEY").supported_extensions


class TestGpsWeekIsDerivedNotGuessed:
    @pytest.mark.parametrize(
        "obs,expected",
        [
            (date(2010, 6, 2), 1586),  # the hand-verified file — teqc agreed
            (date(1980, 1, 6), 0),  # GPS epoch itself
            (date(1980, 1, 13), 1),
            (date(2008, 1, 1), 1460),
            (date(2012, 5, 15), 1688),
        ],
    )
    def test_week_matches_the_gps_epoch_arithmetic(self, obs, expected):
        args = R00Converter("VMEY")._teqc_extra_args(
            datetime(obs.year, obs.month, obs.day)
        )
        assert args == ["-week", str(expected)]

    def test_the_flag_is_actually_emitted(self):
        args = R00Converter("VMEY")._teqc_extra_args(datetime(2010, 6, 2))
        assert args[0] == "-week"
        assert args[1].isdigit()

    def test_a_plain_date_works_too(self):
        # observation_date reaches converters as datetime, but be liberal.
        assert R00Converter("VMEY")._teqc_extra_args(date(2010, 6, 2)) == [
            "-week",
            "1586",
        ]

    def test_the_t02_path_is_not_given_a_week(self):
        # The .T02/.T00 command was already correct; adding a derived flag there
        # would be an unrequested behaviour change on a working path.
        from receivers.rinex import TrimbleConverter

        assert TrimbleConverter("VMEY")._teqc_extra_args(datetime(2015, 6, 1)) == []


class TestConverterShape:
    def test_it_accepts_r00_in_both_cases_and_gzipped(self):
        exts = R00Converter("VMEY").supported_extensions
        assert {".r00", ".R00", ".r00.gz", ".R00.gz"} == set(exts)

    def test_it_reuses_the_trimble_toolchain(self):
        # Same chain as .T02 — this is why the feature is small.
        tools = R00Converter("VMEY")._get_required_tools()
        assert "runpkr00" in tools and "teqc" in tools

    def test_it_gates_on_r00_content(self):
        assert R00Converter.accepted_raw_formats == frozenset({TRIMBLE_R00})


class TestSessionStartNaming:
    """The archive stamps some R00 files with the moment the session OPENED.

    ``…YYYYMMDD2359a.r00`` holds the data of the NEXT day. Measured fleet-wide
    over 2008/2010/2012: 2,150 files named ``0000`` (same day) against 1,188
    named ``2359`` (next day), plus ~270 at assorted hours. So it is a real
    minority convention, not universal — VMEY happens to be a ``2359`` station
    (951 of its 977 files), which is why it looked universal at first.

    Confirmed by decoding both ways:
        HVER201004010000a.r00 -> first obs 2010-04-01   (same day)
        VMEY201006012359a.r00 -> first obs 2010-06-02   (next day)
    """

    @pytest.mark.parametrize(
        "name,expect",
        [
            ("VMEY201006012359a.r00", "2010-06-02"),  # session start -> next day
            ("HVER201004010000a.r00", "2010-04-01"),  # midnight -> same day
            ("VMEY201212312359a.r00", "2013-01-01"),  # rolls the year
        ],
    )
    def test_the_observation_date_follows_the_session(self, name, expect):
        from pathlib import Path

        got = R00Converter("VMEY")._extract_date_from_filename(Path(name))
        assert got.date().isoformat() == expect

    def test_an_odd_hour_is_left_alone_for_the_identity_gate(self):
        # ~270 files carry assorted times (1900, 2200, …). Guessing for those
        # would be worse than letting the base class's identity gate compare the
        # decoded first-obs date against the claim and refuse a mismatch.
        from pathlib import Path

        got = R00Converter("VMEY")._extract_date_from_filename(
            Path("XXXX201004011900a.r00")
        )
        assert got.date().isoformat() == "2010-04-01"

    def test_the_week_is_derived_from_the_OBSERVATION_date(self):
        # The whole point of the shift: a 2359 file must be decoded with the
        # week of the day its data belongs to, not of its filename stamp.
        from pathlib import Path

        c = R00Converter("VMEY")
        obs = c._extract_date_from_filename(Path("VMEY201006012359a.r00"))
        assert c._teqc_extra_args(obs) == ["-week", "1586"]
