"""A TOS synthetic serial must never reach a RINEX header.

`<subtype>-<STID>-<YYYYMMDD>` exists because TOS requires a non-empty
serial_number; radomes never have a factory serial and antennas frequently do
not. It is a lookup key, not equipment data — and at 21 characters it does not
even fit the A20 field.

VMEY shows the consequence: 65 headers from 2026-08-01 carry
`antenna-VMEY-2023011` truncated flush against the antenna type, and
`reconstruct-from-archive` proposed creating a duplicate antenna because the
truncated string no longer matched TOS.

**A suppressed serial is published as "0000", NOT as blanks.** This file
originally asserted 20 spaces, which was the behaviour before the M3G bisect of
2026-08-20: M3G rejects an empty antenna Serial Number with a 422, while the
identical log carrying "0000" is accepted. The converter moved to the constant
in 9ecd6b9; this test kept asserting the superseded rule and had been failing
ever since — a red test demanding the behaviour that 422s is an invitation to
"fix" the code back to broken.

The single source of truth is
`tostools.device.PUBLISHED_UNKNOWN_ANTENNA_SERIAL`, asserted against here rather
than hardcoded so the two cannot drift apart again. tostools pins the same rule
across its own writers in `test_unknown_antenna_serial_agrees.py`.
"""

import pytest
from tostools.device import PUBLISHED_UNKNOWN_ANTENNA_SERIAL

from receivers.rinex.metadata_provider import EquipmentMetadata


def _ant(serial: str, model: str = "SEPCHOKE_B3E6", radome: str = "SPKE"):
    md = EquipmentMetadata(
        antenna_serial=serial, antenna_model=model, radome_model=radome
    )
    return md.to_rinex_corrections().get("ANT # / TYPE")


class TestSyntheticSuppressed:
    def test_synthetic_serial_is_published_as_the_constant(self):
        out = _ant("antenna-VMEY-20230111")
        assert out is not None
        assert "antenna-VMEY" not in out
        assert out.startswith(PUBLISHED_UNKNOWN_ANTENNA_SERIAL)

    def test_the_suppressed_field_is_not_blank(self):
        # The whole point of the constant: M3G 422s on an empty Serial Number.
        # Stated separately from the assertion above so that a future change of
        # the constant's VALUE cannot quietly re-admit a blank.
        out = _ant("antenna-VMEY-20230111")
        assert out[:20].strip() != ""

    def test_the_antenna_type_survives_suppression(self):
        # Gating the line on the serial alone would drop type AND radome —
        # a worse header than a suppressed serial field.
        out = _ant("antenna-VMEY-20230111")
        assert "SEPCHOKE_B3E6" in out
        assert "SPKE" in out

    def test_other_subtypes_are_caught_too(self):
        for sn in ("radome-VMEY-20230111", "monument-ISAK-20010530"):
            out = _ant(sn)
            assert sn.split("-")[0] not in out


class TestRealSerialsUnaffected:
    def test_real_serial_is_written(self):
        assert _ant("1441045161").startswith("1441045161")

    def test_short_numeric_serial_is_written(self):
        assert _ant("0000").startswith("0000")

    def test_hyphenated_real_serial_survives(self):
        # The match is anchored on the synthetic pattern, not on "has a hyphen".
        assert "ANT-99-X" in _ant("ANT-99-X")


class TestMissingSerial:
    def test_empty_serial_still_emits_the_type(self):
        out = _ant("")
        assert out is not None
        assert "SEPCHOKE_B3E6" in out

    def test_no_model_and_no_serial_emits_nothing(self):
        md = EquipmentMetadata(antenna_serial="", antenna_model="", radome_model="")
        assert md.to_rinex_corrections().get("ANT # / TYPE") is None


class TestTheDeltaIsACompositeOnAllThreeAxes:
    """EquipmentMetadata must not hardcode a zeroed eccentricity either.

    TOS splits the mark->ARP vector across a monument entity (mark -> monument
    top) and an antenna entity (monument top -> ARP), on all three axes. This
    builder summed the height and wrote ``0.0, 0.0`` for east/north -- the same
    defect the tostools corrector's config branch carried, which published a
    zeroed eccentricity for ISAK against a site log holding 0.0002.

    This builder currently has no production caller (see the note in
    metadata_provider.py); the point of the test is that whoever adopts it does
    not inherit the bug.
    """

    _DELTA = "ANTENNA: DELTA H/E/N"

    def _session(
        self, ant_e=0.0, ant_n=0.0, ant_h=0.0, mon_e=0.0, mon_n=0.0, mon_h=1.0
    ):
        return {
            "antenna": {
                "model": "SEPVC6150L",
                "serial_number": "2505010005",
                "antenna_height": ant_h,
                "antenna_offset_east": ant_e,
                "antenna_offset_north": ant_n,
            },
            "monument": {
                "monument_height": mon_h,
                "monument_offset_east": mon_e,
                "monument_offset_north": mon_n,
            },
            "radome": {"model": "SCIS"},
        }

    def _delta(self, session):
        """(h, e, n) parsed back out of the rendered 3F14.4 field.

        to_rinex_corrections returns the FORMATTED header value, so the parse
        also pins that the composite survives rendering at the field's 0.1 mm
        precision -- a 0.0002 that formatted to 0.0000 would be just as wrong.
        """
        from receivers.rinex.metadata_provider import EquipmentMetadata

        md = EquipmentMetadata.from_tos_session(session)
        body = md.to_rinex_corrections()[self._DELTA]
        return tuple(float(body[i : i + 14]) for i in (0, 14, 28))

    def test_a_monument_stored_eccentricity_reaches_the_header(self):
        """ISAK's shape: the whole offset lives on the monument record."""
        h, e, n = self._delta(self._session(mon_e=0.0001, mon_n=0.0002, mon_h=1.0047))
        assert (h, e, n) == pytest.approx((1.0047, 0.0001, 0.0002))

    def test_offsets_on_both_entities_are_summed(self):
        h, e, n = self._delta(
            self._session(
                ant_e=0.02,
                ant_n=-0.01,
                ant_h=0.15,
                mon_e=0.0001,
                mon_n=0.0002,
                mon_h=1.0,
            )
        )
        assert (h, e, n) == pytest.approx((1.15, 0.0201, -0.0098))

    def test_a_centered_antenna_still_publishes_zeros(self):
        h, e, n = self._delta(self._session(mon_h=0.107))
        assert (h, e, n) == pytest.approx((0.107, 0.0, 0.0))
