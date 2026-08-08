"""Identity must be judged on the SOURCE, before any header rewrite.

The dissemination QC gate runs after `set_header`, which normalises the header
towards TOS. Asking that header "do you match this station?" can only answer
yes — the pipeline validates its own rewrite. Three fixes to the correction
bounds each looked right and each still let the file through in production;
only checking the source before conversion closes the shape.

The case: ISAK's receiver was taken off the mark for a campaign survey in August
2016 (5 marks, 210-245 km away). All 14 days were published to EPOS as ISAK,
carrying ISAK's DOMES.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from receivers.dissemination.source_identity import (
    MAX_SITE_DISTANCE_M,
    check_source_identity,
)

ISAK_SESSION = {
    "marker": "ISAK",
    "lat": 64.119329,
    "lon": -19.747178,
    "altitude": 319.479303,
}
ISAK_XYZ = "  2627583.4397  -943252.7548  5715821.3894"
CAMPAIGN_XYZ = "  2475668.5588  -773840.8653  5807475.5828"


def _check(xyz, session=ISAK_SESSION):
    with (
        patch("tostools.rinex.reader.read_rinex_header", return_value={"header": []}),
        patch(
            "tostools.rinex.reader.extract_header_info",
            return_value={"APPROX POSITION XYZ": xyz} if xyz is not None else {},
        ),
    ):
        return check_source_identity(Path("ISAK2140.16D.Z"), session)


class TestBlocksForeignData:
    def test_the_isak_campaign_file_is_refused(self):
        v = _check(CAMPAIGN_XYZ)
        assert not v.ok

    def test_the_distance_is_reported_in_the_message(self):
        v = _check(CAMPAIGN_XYZ)
        assert "km from ISAK" in v.message

    def test_the_measured_distance_is_carried(self):
        v = _check(CAMPAIGN_XYZ)
        assert v.distance_m > 200_000


class TestPassesGenuineData:
    def test_the_station_own_position_passes(self):
        assert _check(ISAK_XYZ).ok

    def test_a_metre_scale_a_priori_error_passes(self):
        x, y, z = (float(v) for v in ISAK_XYZ.split())
        assert _check(f"  {x + 200:.4f}  {y:.4f}  {z:.4f}").ok

    def test_the_bound_separates_the_regimes(self):
        assert 100.0 < MAX_SITE_DISTANCE_M < 10_000.0


class TestFailsOpenOnMissingInformation:
    """A gate that blocked on absent data would halt the fleet the first time
    TOS blinked. It must block only on a measured contradiction."""

    def test_no_session_passes(self):
        assert _check(ISAK_XYZ, session=None).ok

    def test_session_without_coordinates_passes(self):
        assert _check(ISAK_XYZ, session={"marker": "ISAK"}).ok

    def test_source_without_a_position_passes(self):
        assert _check(None).ok

    def test_unreadable_header_passes(self):
        with patch(
            "tostools.rinex.reader.read_rinex_header", side_effect=OSError("boom")
        ):
            assert check_source_identity(Path("x.D.Z"), ISAK_SESSION).ok

    def test_partial_coordinates_pass(self):
        s = {"marker": "ISAK", "lat": 64.1, "lon": -19.7}  # no altitude
        assert _check(ISAK_XYZ, session=s).ok
