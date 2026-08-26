"""Install-scoped geometry is per-subtype, mirroring the catalog's applies_to.

Regression origin: the first version applied the ANTENNA set to every subtype
and wrote antenna_height + azimuth onto NPSK's monument (both
`applies_to: [antenna]`). TOS stored them silently and nothing downstream
complained — caught only by eyeballing the web UI against NYLA.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import (
    INSTALL_SCOPED_BY_SUBTYPE,
    CfgOperationError,
    add_monument,
    open_install_scoped_attrs,
)


@pytest.fixture
def w():
    m = MagicMock()
    m.find_location_by_name.return_value = 900
    m.find_station_by_marker.return_value = 21721
    m.get_entity_history.return_value = {"attributes": []}
    m.create_device.return_value = {"id_entity": 77}
    return m


def test_sets_match_the_catalog_applies_to():
    """The map must not drift from tostools' attribute catalog."""
    from pathlib import Path

    import tostools
    import yaml

    cat = Path(tostools.__file__).resolve().parents[2] / "data" / "attribute_codes.yaml"
    if not cat.exists():  # wheel install — data dir not alongside the package
        pytest.skip("attribute_codes.yaml not resolvable in this install layout")
    devices = yaml.safe_load(cat.read_text())["devices"]
    for subtype, codes in INSTALL_SCOPED_BY_SUBTYPE.items():
        for code in codes:
            assert subtype in devices[code]["applies_to"], (
                f"{code} is not applies_to {subtype} in the catalog"
            )


def test_monument_gets_monument_codes(w):
    out = open_install_scoped_attrs(
        w,
        21724,
        "monument",
        "2026-08-05",
        values={"monument_height": "0.0", "foundation_depth": "1.0"},
    )
    assert set(out["written"]) == {
        "monument_height",
        "foundation_depth",
        "antenna_offset_north",
        "antenna_offset_east",
    }
    assert "antenna_height" not in out["written"]
    assert "azimuth" not in out["written"]


def test_antenna_gets_antenna_codes(w):
    out = open_install_scoped_attrs(
        w, 21723, "antenna", "2026-08-05", values={"antenna_height": "0.0"}
    )
    assert set(out["written"]) == {
        "antenna_height",
        "azimuth",
        "antenna_offset_north",
        "antenna_offset_east",
    }
    assert "monument_height" not in out["written"]
    assert "foundation_depth" not in out["written"]


def test_antenna_codes_on_a_monument_are_refused(w):
    """The exact bug: TOS accepts these, so the guard must be ours."""
    with pytest.raises(CfgOperationError, match="does not apply to a monument"):
        open_install_scoped_attrs(
            w,
            21724,
            "monument",
            "2026-08-05",
            values={"antenna_height": "0.0", "azimuth": "0.0"},
        )
    w.upsert_attribute_value.assert_not_called()


def test_monument_codes_on_an_antenna_are_refused(w):
    with pytest.raises(CfgOperationError, match="does not apply to a antenna"):
        open_install_scoped_attrs(
            w, 21723, "antenna", "2026-08-05", values={"foundation_depth": "1.0"}
        )


def test_receiver_rejects_any_geometry(w):
    with pytest.raises(CfgOperationError, match="no install-scoped geometry"):
        open_install_scoped_attrs(
            w, 21722, "gnss_receiver", "2026-08-05", values={"antenna_height": "0.0"}
        )
    assert open_install_scoped_attrs(w, 21722, "gnss_receiver", "2026-08-05") == {
        "applicable": False,
        "subtype": "gnss_receiver",
    }


def test_undefaultable_codes_are_reported_not_guessed(w):
    """foundation_depth and antenna_height are measurements, never invented."""
    mon = open_install_scoped_attrs(w, 1, "monument", "2026-08-05")
    assert mon["missing"] == ["foundation_depth"]
    assert mon["written"]["monument_height"] == "0.0"

    ant = open_install_scoped_attrs(w, 2, "antenna", "2026-08-05")
    assert ant["missing"] == ["antenna_height"]


# --- add-monument now records the mark type and status ---------------------


def _codes(attrs):
    return {a["code"]: a["value"] for a in attrs}


def test_monument_mark_type_uses_the_model_code(w):
    """TOS renders `model` as "Tegund innviða" on a monument — one code, two labels.

    `infrastructure_type` is in tostools' harvested catalog but is NOT a real
    TOS attribute code; add_attribute_value rejects it as "not in
    /admin_attribute_rows". Writing it made every new monument fail at runtime.
    """
    r = add_monument(w, station_id="NPSK", date_start="2026-08-05", dry_run=False)
    got = _codes(r.tos_changes["monument_attributes"])
    assert got["model"] == "GPS stál-fjórfótur"
    assert "infrastructure_type" not in got


def test_explicit_mark_type_wins(w):
    r = add_monument(
        w,
        station_id="NPSK",
        model="GPS steinsteypustöpull",
        date_start="2026-08-05",
        dry_run=False,
    )
    assert _codes(r.tos_changes["monument_attributes"])["model"] == (
        "GPS steinsteypustöpull"
    )


def test_status_defaults_to_virkt_not_virk(w):
    """'virk' appears on legacy records (NYLA) but is not in TOS's vocabulary."""
    r = add_monument(w, station_id="NPSK", date_start="2026-08-05", dry_run=False)
    assert _codes(r.tos_changes["monument_attributes"])["status"] == "virkt"


def test_attrs_are_dated_to_the_join(w):
    """Every install-scoped period opens at the join instant, not 'now'."""
    open_install_scoped_attrs(
        w, 21723, "antenna", "2026-08-05T12:00:00", values={"antenna_height": "0.0"}
    )
    assert w.upsert_attribute_value.call_args_list
    for call in w.upsert_attribute_value.call_args_list:
        assert call[0][0] == 21723
        assert call[0][3] == "2026-08-05T12:00:00"


def test_supplied_values_win_over_defaults(w):
    out = open_install_scoped_attrs(
        w,
        21723,
        "antenna",
        "2026-08-05",
        values={
            "antenna_height": "1.234",
            "azimuth": "90",
            "antenna_offset_north": "0.01",
            "antenna_offset_east": "-0.02",
        },
    )
    assert out["written"] == {
        "antenna_height": "1.234",
        "azimuth": "90",
        "antenna_offset_north": "0.01",
        "antenna_offset_east": "-0.02",
    }
    assert out["defaulted"] == []
