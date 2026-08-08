"""The dissemination QC gate must actually receive the station coordinates.

`coordinates` has always been in DEFAULT_BLOCKING_FIELDS, and
`compare_rinex_to_tos` does compare APPROX POSITION XYZ against TOS — but only
when lat AND lon AND altitude are all present in the session. The session is
built by merging DEVICE history (receiver/antenna/radome/monument); the surveyed
position is station-level and was never carried across. So the comparison was
skipped silently: no match, no discrepancy, nothing to block on.

Consequence: ISAK's receiver was removed from the mark for a campaign survey in
August 2016 (5 marks, up to 220 km away). All 14 days were converted with ISAK's
MARKER NAME and DOMES and published on the EPOS portal. The raw validator DID
refuse them at conversion time — this gate is the one that should have stopped
them being disseminated.
"""

from __future__ import annotations

from receivers.dissemination.tos_access import (
    _STATION_COORD_KEYS,
    _carry_station_coords,
)

# ISAK, and the campaign mark its receiver actually sat on for days 214-217.
ISAK_META = {
    "marker": "isak",
    "lat": 64.119329,
    "lon": -19.747178,
    "altitude": 319.479303,
    "iers_domes_number": "10214M001",
}


class TestCoordsReachTheSession:
    def test_all_three_are_carried(self):
        s: dict = {}
        _carry_station_coords(s, ISAK_META)
        assert (s["lat"], s["lon"], s["altitude"]) == (
            64.119329,
            -19.747178,
            319.479303,
        )

    def test_the_comparator_precondition_is_met(self):
        """compare_rinex_to_tos needs all three, or it skips the check."""
        s: dict = {}
        _carry_station_coords(s, ISAK_META)
        assert all(s.get(k) is not None for k in ("lat", "lon", "altitude"))

    def test_a_device_only_session_would_have_failed_the_precondition(self):
        """What the session looked like before the fix — the regression."""
        device_only = {"gnss_receiver": {}, "antenna": {}, "marker": "ISAK"}
        assert not all(
            device_only.get(k) is not None for k in ("lat", "lon", "altitude")
        )

    def test_existing_values_are_not_overwritten(self):
        """setdefault semantics — a caller-supplied position wins."""
        s = {"lat": 1.0}
        _carry_station_coords(s, ISAK_META)
        assert s["lat"] == 1.0
        assert s["lon"] == -19.747178

    def test_absent_coords_are_not_invented(self):
        """A station with no surveyed position must not gain a bogus one."""
        s: dict = {}
        _carry_station_coords(s, {"marker": "x", "lat": None, "lon": None})
        assert s == {}

    def test_partial_metadata_carries_only_what_exists(self):
        s: dict = {}
        _carry_station_coords(s, {"lat": 64.0, "lon": None, "altitude": 300.0})
        assert set(s) == {"lat", "altitude"}

    def test_key_set_matches_what_the_comparator_reads(self):
        assert _STATION_COORD_KEYS == ("lat", "lon", "altitude")


class TestCoordinatesIsABlockingField:
    def test_gate_would_block_on_a_coordinate_mismatch(self):
        from receivers.dissemination.qc_gate import DEFAULT_BLOCKING_FIELDS

        assert "coordinates" in DEFAULT_BLOCKING_FIELDS
