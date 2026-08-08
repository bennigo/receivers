"""Tests for probe-derived GNSS constellation extraction.

Covers the mapping layer (`constellations_from_satellites`) and the
`cfg add-receiver` wiring that turns it into TOS `true` attributes.

The central invariant under test: **only positive evidence is ever written**.
A system missing from the PVT solution may simply have had nothing in view,
and QZSS/IRN are regional systems invisible from Iceland even when enabled —
so `false` must never be emitted.
"""

from __future__ import annotations

import pytest

from receivers.cfg.device_probe import (
    CONSTELLATION_LABEL_TO_TOS,
    ReceiverIdentity,
    constellations_from_satellites,
)

# ---------------------------------------------------------------------------
# Mapping layer
# ---------------------------------------------------------------------------

#: Exactly what the live NPSK probe returned (PolaRX5, 2026-08-08).
NPSK_SATELLITES = {
    "total": 41,
    "by_constellation": {"GPS": 12, "Galileo": 11, "GLONASS": 8, "BeiDou": 10},
    "status": "ok",
}


def test_npsk_live_payload_maps_to_four_codes():
    assert constellations_from_satellites(NPSK_SATELLITES) == {
        "GPS",
        "GAL",
        "GLO",
        "BDS",
    }


@pytest.mark.parametrize(
    "label,expected",
    [
        ("GPS", "GPS"),
        ("GLONASS", "GLO"),
        ("Galileo", "GAL"),
        ("BeiDou", "BDS"),
        ("QZSS", "QZSS"),
        ("SBAS", "SBAS"),
        ("IRNSS", "IRN"),
        ("NavIC", "IRN"),
        ("Compass", "BDS"),
        # Case and whitespace must not matter — vendor strings vary.
        ("  galileo ", "GAL"),
        ("BEIDOU", "BDS"),
    ],
)
def test_label_variants(label, expected):
    assert constellations_from_satellites({"by_constellation": {label: 1}}) == {
        expected
    }


def test_zero_count_is_not_evidence():
    """A system present in the dict with zero satellites is NOT in use."""
    out = constellations_from_satellites(
        {"by_constellation": {"GPS": 9, "Galileo": 0, "BeiDou": 0}}
    )
    assert out == {"GPS"}


def test_unmapped_label_is_skipped_and_warned(caplog):
    with caplog.at_level("WARNING"):
        out = constellations_from_satellites({"by_constellation": {"Wibble": 4}})
    assert out == frozenset()
    assert "unmapped constellation label" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"by_constellation": None}, {"by_constellation": []}, "nonsense", 42],
)
def test_malformed_payloads_yield_empty(payload):
    assert constellations_from_satellites(payload) == frozenset()


def test_non_numeric_count_is_skipped():
    out = constellations_from_satellites(
        {"by_constellation": {"GPS": "twelve", "GLONASS": 8}}
    )
    assert out == {"GLO"}


def test_every_mapping_target_is_a_valid_tos_code():
    """Guard against a typo'd code silently creating a bogus TOS attribute."""
    from tostools.constellation import TOS_CONSTELLATION_CODES

    assert set(CONSTELLATION_LABEL_TO_TOS.values()) <= set(TOS_CONSTELLATION_CODES)


def test_identity_defaults_to_empty():
    """An identity built without a probe (e.g. --from-file) has no claims."""
    assert (
        ReceiverIdentity(subtype="gnss_receiver", probe_type="polarx5").constellations
        == frozenset()
    )


# ---------------------------------------------------------------------------
# add-receiver wiring
# ---------------------------------------------------------------------------


def _codes(constellations, *, skip=False):
    """Call the real handler helper and return just the (code, value) pairs."""
    from receivers.cli.cfg import build_constellation_attrs

    attrs, _note, _warn = build_constellation_attrs(constellations, skip=skip)
    return attrs


def test_wiring_emits_true_for_each_probed_system():
    out = _codes({"GPS", "GLO", "GAL", "BDS"})
    assert out == [("BDS", "true"), ("GAL", "true"), ("GLO", "true"), ("GPS", "true")]
    # The invariant: nothing is ever written as false.
    assert all(v == "true" for _, v in out)


def test_wiring_writes_nothing_when_probe_empty():
    assert _codes(frozenset()) == []
    assert _codes(None) == []


def test_empty_probe_emits_a_warning():
    from receivers.cli.cfg import build_constellation_attrs

    attrs, note, warn = build_constellation_attrs(frozenset())
    assert attrs == []
    assert warn is True
    assert "verify against the receiver" in note


def test_no_constellations_flag_suppresses():
    assert _codes({"GPS", "GAL"}, skip=True) == []


def test_skip_note_names_what_was_dropped():
    from receivers.cli.cfg import build_constellation_attrs

    _attrs, note, warn = build_constellation_attrs({"GPS", "GAL"}, skip=True)
    assert "GAL" in note and "GPS" in note
    assert warn is False


def test_skip_with_nothing_probed_says_nothing():
    from receivers.cli.cfg import build_constellation_attrs

    assert build_constellation_attrs(frozenset(), skip=True) == ([], None, False)


def test_success_note_lists_the_systems():
    from receivers.cli.cfg import build_constellation_attrs

    _attrs, note, warn = build_constellation_attrs({"GPS", "BDS"})
    assert "BDS, GPS" in note
    assert warn is False


def test_end_to_end_live_payload_to_attrs():
    """The NPSK payload, all the way from SBF counts to TOS attribute pairs."""
    assert _codes(constellations_from_satellites(NPSK_SATELLITES)) == [
        ("BDS", "true"),
        ("GAL", "true"),
        ("GLO", "true"),
        ("GPS", "true"),
    ]


def test_flag_is_registered_on_the_parser():
    import argparse

    from receivers.cli.cfg import create_cfg_parser

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command")
    create_cfg_parser(sub)
    args = p.parse_args(
        ["cfg", "add-receiver", "--probe", "10.6.1.71", "--no-constellations"]
    )
    assert args.no_constellations is True

    default = p.parse_args(["cfg", "add-receiver", "--probe", "10.6.1.71"])
    assert default.no_constellations is False


# ---------------------------------------------------------------------------
# Truncation guard
# ---------------------------------------------------------------------------


def test_short_read_still_returns_what_was_seen_but_warns(caplog):
    """A truncated SBF block loses the tail; the systems seen are still real."""
    truncated = {
        "total": 41,
        "by_constellation": {"GPS": 11, "Galileo": 10, "GLONASS": 6},
    }
    with caplog.at_level("WARNING"):
        out = constellations_from_satellites(truncated)
    assert out == {"GPS", "GAL", "GLO"}  # positive evidence survives
    assert "truncated" in caplog.text
    assert "41" in caplog.text


def test_complete_read_does_not_warn(caplog):
    """Counts summing to the declared total is a complete block — no warning."""
    complete = {
        "total": 40,
        "by_constellation": {"GPS": 10, "Galileo": 10, "GLONASS": 9, "BeiDou": 11},
    }
    with caplog.at_level("WARNING"):
        out = constellations_from_satellites(complete)
    assert out == {"GPS", "GAL", "GLO", "BDS"}
    assert "truncated" not in caplog.text


def test_missing_total_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        constellations_from_satellites({"by_constellation": {"GPS": 5}})
    assert "truncated" not in caplog.text
