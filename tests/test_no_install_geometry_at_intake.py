"""Warehouse intake must not record install geometry.

A mark on a shelf has no mark→ARP offset and an antenna in a box has no ARP
height — those describe the device AT a station. Writing 0.0 at intake is worse
than writing nothing: it looks like a measurement.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import CfgOperationError, add_antenna, add_monument

WAREHOUSE = "B9 - Kjallari - Jörð"


def _codes(attrs):
    return {a.get("code") for a in attrs}


@pytest.fixture
def w():
    m = MagicMock()
    m.find_location_by_name.return_value = 900
    m.find_station_by_marker.return_value = 21721
    m.get_entity_history.return_value = {"attributes": []}
    m.create_device.return_value = {"id_entity": 77}
    return m


def test_monument_intake_writes_no_height_attribute(w):
    r = add_monument(
        w,
        warehouse=WAREHOUSE,
        serial="default-NPSK",
        date_start="2026-08-05",
        dry_run=False,
    )
    assert "monument_height" not in _codes(r.tos_changes["monument_attributes"])


def test_monument_station_install_still_defaults_to_zero(w):
    r = add_monument(w, station_id="NPSK", date_start="2026-08-05", dry_run=False)
    attrs = {a["code"]: a["value"] for a in r.tos_changes["monument_attributes"]}
    assert attrs["monument_height"] == "0.0"


def test_monument_station_install_honours_an_explicit_height(w):
    r = add_monument(
        w, station_id="NPSK", height="1.25", date_start="2026-08-05", dry_run=False
    )
    attrs = {a["code"]: a["value"] for a in r.tos_changes["monument_attributes"]}
    assert attrs["monument_height"] == "1.25"


@pytest.mark.parametrize("height", ["0.0", "1.5", "0"])
def test_monument_intake_refuses_any_supplied_height(w, height):
    """Including 0.0 — an explicit zero is still an install claim."""
    with pytest.raises(CfgOperationError, match="meaningless"):
        add_monument(
            w, warehouse=WAREHOUSE, serial="default-NPSK", height=height, dry_run=False
        )
    w.create_device.assert_not_called()


def test_antenna_intake_writes_no_height_attribute(w):
    r = add_antenna(
        w,
        warehouse=WAREHOUSE,
        model="TRM115000.00",
        serial="1441052912",
        radome="NONE",
        date_start="2026-08-05",
        dry_run=False,
    )
    assert "antenna_height" not in _codes(r.tos_changes["antenna_attributes"])


def test_antenna_intake_does_not_warn_about_the_missing_height(w, caplog):
    """The absence is correct at intake — nagging here is what prompted this."""
    with caplog.at_level("WARNING"):
        add_antenna(
            w,
            warehouse=WAREHOUSE,
            model="TRM115000.00",
            serial="1441052912",
            radome="NONE",
            date_start="2026-08-05",
            dry_run=False,
        )
    assert "no antenna height supplied" not in caplog.text


def test_antenna_station_install_still_warns(w, caplog):
    with caplog.at_level("WARNING"):
        add_antenna(
            w,
            station_id="NPSK",
            model="TRM115000.00",
            radome="NONE",
            date_start="2026-08-05",
            dry_run=False,
        )
    assert "no antenna height supplied" in caplog.text
