"""Install-scoped attributes are opened when a device joins a station.

`tostools.audit_missing_attributes.INSTALL_SCOPED_CODES` — antenna_height,
antenna_offset_north, antenna_offset_east, azimuth — describe a device AT A
MARK, not the device. The audit already flags them when they outlive a join;
nothing opened them when the join was made, which is why a warehouse-first
antenna reached its station with no height at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import (
    CfgOperationError,
    add_monument,
    open_install_scoped_attrs,
)


@pytest.fixture
def w():
    return MagicMock()


def test_codes_match_the_tostools_definition():
    """Guard against drifting from the canonical set."""
    from tostools.audit_missing_attributes import INSTALL_SCOPED_CODES

    from receivers.cfg import operations as ops

    handled = {
        "antenna_height",
        "antenna_offset_north",
        "antenna_offset_east",
        "azimuth",
    }
    assert handled == set(INSTALL_SCOPED_CODES)
    assert set(ops._OFFSET_DEFAULT_CODES) < handled


def test_receiver_subtype_is_not_applicable(w):
    out = open_install_scoped_attrs(w, 1, "gnss_receiver", "2026-08-05")
    assert out["applicable"] is False
    w.upsert_attribute_value.assert_not_called()


def test_offsets_and_azimuth_default_to_zero(w):
    out = open_install_scoped_attrs(
        w, 21723, "antenna", "2026-08-05", antenna_height="0.0"
    )
    assert out["written"]["antenna_height"] == "0.0"
    assert out["written"]["antenna_offset_north"] == "0.0"
    assert out["written"]["antenna_offset_east"] == "0.0"
    assert out["written"]["azimuth"] == "0.0"
    assert set(out["defaulted"]) == {
        "antenna_offset_north",
        "antenna_offset_east",
        "azimuth",
    }
    assert w.upsert_attribute_value.call_count == 4


def test_height_is_never_defaulted(w):
    """A silent 0.0 here becomes a wrong ANTENNA: DELTA H in every RINEX header."""
    out = open_install_scoped_attrs(w, 21723, "antenna", "2026-08-05")
    assert "antenna_height" not in out["written"]
    assert out["height_missing"] is True
    assert "antenna_height" not in out["defaulted"]


def test_supplied_values_win(w):
    out = open_install_scoped_attrs(
        w,
        21723,
        "antenna",
        "2026-08-05",
        antenna_height="1.234",
        azimuth="90",
        offset_north="0.01",
        offset_east="-0.02",
    )
    assert out["written"] == {
        "antenna_height": "1.234",
        "antenna_offset_north": "0.01",
        "antenna_offset_east": "-0.02",
        "azimuth": "90",
    }
    assert out["defaulted"] == []


def test_monument_is_in_scope(w):
    out = open_install_scoped_attrs(
        w, 9, "monument", "2026-08-05", antenna_height="0.0"
    )
    assert out["applicable"] is True


def test_attrs_are_dated_to_the_join(w):
    open_install_scoped_attrs(w, 21723, "antenna", "2026-08-05", antenna_height="0.0")
    for call in w.upsert_attribute_value.call_args_list:
        assert call[0][0] == 21723
        assert call[0][3] == "2026-08-05"


# --- warehouse intake must refuse install geometry -------------------------


def test_monument_warehouse_refuses_a_height():
    writer = MagicMock()
    with pytest.raises(
        CfgOperationError,
        match="meaningless for warehouse intake|--height is meaningless",
    ):
        add_monument(
            writer,
            warehouse="B9 - Kjallari - Jörð",
            serial="M-9",
            height="1.5",
            dry_run=False,
        )
    writer.create_device.assert_not_called()


def test_monument_warehouse_accepts_the_zero_sentinel():
    """0.0 is the parser default, not an operator assertion — don't block on it."""
    writer = MagicMock()
    writer.find_location_by_name.return_value = 900
    writer.create_device.return_value = {"id_entity": 77}
    r = add_monument(
        writer, warehouse="B9 - Kjallari - Jörð", serial="M-9", dry_run=False
    )
    assert r.tos_changes["monument_join"]["parent"] == 900
