"""Tests for the MARKER NUMBER / IERS DOMES receiver push.

Policy under test (bgo, 2026-07-13): MARKER NUMBER carries the IERS DOMES and
nothing else. Absent a real DOMES the receiver field is left ALONE — never
filled with the 4-char station id. cfg had the wrong 4-char marker for most of
the fleet, so "skip" has to be the default outcome, not an edge case.
"""

from __future__ import annotations

import pytest

from receivers.septentrio.marker import (
    NoDomesError,
    build_domes_commands,
    build_domes_commands_from_station_config,
    normalize_domes,
)

VALID = "10214M001"  # ISAK


class TestNormalize:
    def test_accepts_a_real_domes(self):
        assert normalize_domes(VALID) == VALID

    def test_uppercases_and_strips(self):
        assert normalize_domes("  10214m001  ") == VALID

    @pytest.mark.parametrize(
        "bad",
        [
            "ISAK",  # the 4-char id — the exact value that must never be pushed
            "",
            None,
            "1021M001",  # too few leading digits
            "10214M01",  # too few trailing digits
            "10214M0011",  # too many trailing digits
            "10214MM01",  # two letters
        ],
    )
    def test_rejects_non_domes(self, bad):
        assert normalize_domes(bad) == ""

    def test_letter_code_is_not_restricted_to_M(self):
        """The guard is ``^\\d{5}[A-Z]\\d{3}$`` — the DOMES point-type letter is
        not always M (S marks a satellite-tracking point), so restricting it
        would reject legitimate numbers."""
        assert normalize_domes("10214S001") == "10214S001"


class TestBuildCommands:
    def test_writes_only_the_second_argument(self):
        """Blank args = 'leave unchanged', so marker NAME and station code survive."""
        cmds = build_domes_commands(VALID)
        assert cmds[0] == f'setMarkerParameters, , "{VALID}"'
        # exactly two leading commas => arg 2 is the one being set
        head = cmds[0].split('"')[0]
        assert head.count(",") == 2

    def test_saves_to_boot(self):
        assert build_domes_commands(VALID)[-1] == "eccf, Current, Boot"

    def test_emits_nothing_else(self):
        cmds = build_domes_commands(VALID)
        assert len(cmds) == 2
        joined = " ".join(cmds)
        for forbidden in (
            "setAntennaOffset",
            "setSignalTracking",
            "setDataInOut",
            "setSBFOutput",
            "setObserverParameters",
        ):
            assert forbidden not in joined

    def test_does_not_touch_the_station_code(self):
        """arg 4 is StationCode (the 4-char RINEX designator) — must stay empty.

        The guide's own example is `setMarkerParameters, , , , LEUV`; writing
        the DOMES into that slot would rename the station's files.
        """
        cmds = build_domes_commands(VALID)
        args = cmds[0][len("setMarkerParameters,") :].split(",")
        assert args[3].strip() == "" if len(args) > 3 else True

    def test_no_domes_raises_the_skip_signal(self):
        with pytest.raises(NoDomesError):
            build_domes_commands("")

    def test_four_char_id_raises_rather_than_being_written(self):
        with pytest.raises(NoDomesError) as exc:
            build_domes_commands("ISAK")
        assert "ISAK" in str(exc.value)

    def test_nodomes_is_a_valueerror_subclass(self):
        """Callers catching ValueError must still catch the skip."""
        assert issubclass(NoDomesError, ValueError)


class TestFromStationConfig:
    def test_flat_key(self):
        cfg = {"rinex_marker_number": VALID}
        assert build_domes_commands_from_station_config(cfg)[0].endswith(f'"{VALID}"')

    def test_nested_rinex_section(self):
        cfg = {"rinex": {"marker_number": VALID}}
        assert build_domes_commands_from_station_config(cfg)[0].endswith(f'"{VALID}"')

    def test_flat_key_wins_over_nested(self):
        cfg = {"rinex_marker_number": VALID, "rinex": {"marker_number": "99999M999"}}
        assert VALID in build_domes_commands_from_station_config(cfg)[0]

    def test_station_without_domes_is_skipped(self):
        """162 of 196 fleet stations have no DOMES — this is the common path."""
        with pytest.raises(NoDomesError):
            build_domes_commands_from_station_config({"station_id": "OLKE"})

    def test_blank_value_is_skipped_not_written(self):
        with pytest.raises(NoDomesError):
            build_domes_commands_from_station_config({"rinex_marker_number": "   "})
