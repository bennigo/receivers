"""Tests for ``receivers cfg add-antenna`` — the antenna/radome TOS-intake verb.

Covers the tostools helpers (synthetic serial, attribute builder, IGS
validation) and the :func:`receivers.cfg.operations.add_antenna` orchestration
with a mocked :class:`TOSWriter` (no network). The driving real-world constraint
is that antenna serials are frequently unknown — exercised by the synthetic-
serial path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tostools.device import (
    build_antenna_attributes,
    synthetic_serial,
    validate_model,
)
from tostools.standards.igs_equipment import to_igs_antenna

from receivers.cfg.operations import CfgOperationError, add_antenna

SEY9_EID = 19085


# ---------------------------------------------------------------------------
# tostools helpers
# ---------------------------------------------------------------------------


def test_seppolant_x_mf_is_igs_valid():
    """SEPPOLANT_X_MF must resolve (regression: it was missing from ANTENNA_IGS)."""
    assert to_igs_antenna("SEPPOLANT_X_MF") == "SEPPOLANT_X_MF"
    assert validate_model("antenna", "SEPPOLANT_X_MF") == "SEPPOLANT_X_MF"


def test_synthetic_serial_matches_radome_convention():
    """`<subtype>-<STID>-<YYYYMMDD>`, mirroring radome-REYK-20130502."""
    assert synthetic_serial("antenna", "SEY9", "2021-03-25") == "antenna-SEY9-20210325"
    # Accepts a full ISO datetime; only the date part is used.
    assert (
        synthetic_serial("radome", "SEY9", "2021-03-25T00:00:00")
        == "radome-SEY9-20210325"
    )


def test_build_antenna_attributes_height_optional():
    no_h = build_antenna_attributes("s1", "SEPPOLANT_X_MF", "owner", "2021-03-25")
    assert not any(a["code"] == "antenna_height" for a in no_h)
    assert {a["code"] for a in no_h} == {
        "serial_number",
        "model",
        "owner",
        "status",
        "date_start",
    }
    with_h = build_antenna_attributes(
        "s1", "SEPPOLANT_X_MF", "owner", "2021-03-25", antenna_height="0.0083"
    )
    h = [a for a in with_h if a["code"] == "antenna_height"]
    assert h and h[0]["value"] == "0.0083"


# ---------------------------------------------------------------------------
# add_antenna operation
# ---------------------------------------------------------------------------


def _writer(station_dict=None, child_subtype=None):
    """TOSWriter-shaped mock. ``station_dict`` is returned for the station's
    get_entity_history; ``child_subtype`` (if set) is returned for any other id."""
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = SEY9_EID
    station_dict = (
        station_dict
        if station_dict is not None
        else {
            "children_connections": [],
            "attributes": [
                {"code": "date_start", "value": "2021-03-25T00:00:00", "date_to": None}
            ],
        }
    )

    def _hist(eid):
        if int(eid) == SEY9_EID:
            return station_dict
        return {"code_entity_subtype": child_subtype}

    w.get_entity_history.side_effect = _hist
    # create_device returns a fresh id per call (antenna then radome)
    w.create_device.side_effect = [{"id_entity": 50001}, {"id_entity": 50002}]
    w.create_entity_connection.return_value = {"id_connection": 7}
    return w


def test_add_antenna_synthetic_serial_when_unknown():
    w = _writer()
    res = add_antenna(
        w,
        station_id="SEY9",
        model="SEPPOLANT_X_MF",
        date_start="2021-03-25",
        dry_run=True,
    )
    assert res.operation == "add-antenna"
    assert res.serial == "antenna-SEY9-20210325"
    assert res.tos_changes["synthetic_serial"] is True
    # One device (antenna) created and joined; no radome on NONE.
    subtype = w.create_device.call_args_list[0].args[0]
    assert subtype == "antenna"
    assert w.create_device.call_count == 1
    # Bare date promoted to noon (move-device convention) so co-installs align.
    w.create_entity_connection.assert_called_once_with(
        SEY9_EID, 50001, "2021-03-25T12:00:00"
    )
    # synthetic-serial provenance comment auto-added
    attrs = res.tos_changes["antenna_attributes"]
    assert any(a["code"] == "comment" for a in attrs)
    assert any(
        a["code"] == "serial_number" and a["value"] == "antenna-SEY9-20210325"
        for a in attrs
    )


def test_add_antenna_explicit_serial_not_synthetic():
    w = _writer()
    res = add_antenna(
        w,
        station_id="SEY9",
        model="SEPPOLANT_X_MF",
        serial="ABC123",
        date_start="2021-03-25",
    )
    assert res.serial == "ABC123"
    assert res.tos_changes["synthetic_serial"] is False


def test_add_antenna_with_radome_creates_second_device():
    w = _writer()
    res = add_antenna(
        w,
        station_id="SEY9",
        model="LEIAR25.R4",
        radome="LEIT",
        date_start="2021-03-25",
    )
    assert w.create_device.call_count == 2
    assert [c.args[0] for c in w.create_device.call_args_list] == ["antenna", "radome"]
    assert res.tos_changes["radome_serial"] == "radome-SEY9-20210325"
    assert w.create_entity_connection.call_count == 2


def test_add_antenna_height_attribute_included():
    w = _writer()
    res = add_antenna(
        w,
        station_id="SEY9",
        model="SEPPOLANT_X_MF",
        antenna_height="0.0083",
        date_start="2021-03-25",
    )
    attrs = res.tos_changes["antenna_attributes"]
    h = [a for a in attrs if a["code"] == "antenna_height"]
    assert h and h[0]["value"] == "0.0083"


def test_add_antenna_unknown_model_raises():
    w = _writer()
    with pytest.raises(ValueError, match="Unknown antenna model"):
        add_antenna(w, station_id="SEY9", model="BOGUS_ANT", date_start="2021-03-25")
    w.create_device.assert_not_called()


def test_add_antenna_guards_existing_open_antenna():
    w = _writer(
        station_dict={
            "children_connections": [{"id_entity_child": 777, "time_to": None}],
            "attributes": [],
        },
        child_subtype="antenna",
    )
    with pytest.raises(CfgOperationError, match="already has an open antenna"):
        add_antenna(
            w, station_id="SEY9", model="SEPPOLANT_X_MF", date_start="2021-03-25"
        )
    w.create_device.assert_not_called()


def test_add_antenna_force_bypasses_open_guard():
    w = _writer(
        station_dict={
            "children_connections": [{"id_entity_child": 777, "time_to": None}],
            "attributes": [],
        },
        child_subtype="antenna",
    )
    res = add_antenna(
        w,
        station_id="SEY9",
        model="SEPPOLANT_X_MF",
        date_start="2021-03-25",
        force=True,
    )
    assert res.serial.startswith("antenna-SEY9-")
    w.create_device.assert_called_once()


def test_add_antenna_defaults_date_to_station_date_start():
    w = _writer()  # station dict carries date_start 2021-03-25
    res = add_antenna(w, station_id="SEY9", model="SEPPOLANT_X_MF")
    assert res.date == "2021-03-25T00:00:00"
    assert res.serial == "antenna-SEY9-20210325"


def test_add_antenna_unknown_station_raises():
    w = _writer()
    w.find_station_by_marker.return_value = None
    with pytest.raises(CfgOperationError, match="No TOS station matches marker"):
        add_antenna(
            w, station_id="ZZZZ", model="SEPPOLANT_X_MF", date_start="2021-03-25"
        )


def test_add_antenna_bare_date_promoted_to_noon():
    """Bare YYYY-MM-DD → noon (move-device convention) so a same-date co-install
    of antenna + receiver shares one TOS session (else the SKL drops one)."""
    w = _writer()
    res = add_antenna(
        w, station_id="SEY9", model="SEPPOLANT_X_MF", date_start="2021-03-25"
    )
    assert res.date == "2021-03-25T12:00:00"
    w.create_entity_connection.assert_called_once_with(
        SEY9_EID, 50001, "2021-03-25T12:00:00"
    )


def test_add_antenna_full_datetime_preserved():
    """A full ISO datetime is used verbatim — lets an operator pin the antenna to
    the receiver's exact join instant (how SEY9 was aligned)."""
    w = _writer()
    res = add_antenna(
        w,
        station_id="SEY9",
        model="SEPPOLANT_X_MF",
        date_start="2021-03-25T00:00:00",
    )
    assert res.date == "2021-03-25T00:00:00"
    w.create_entity_connection.assert_called_once_with(
        SEY9_EID, 50001, "2021-03-25T00:00:00"
    )


# ---------------------------------------------------------------------------
# Warehouse intake — the antenna arrives with its radome screwed on
# ---------------------------------------------------------------------------


def _warehouse_writer():
    w = _writer()
    w.find_location_by_name.return_value = 9001
    return w


def test_warehouse_intake_joins_antenna_and_radome_to_the_warehouse():
    """Both come in as one unit — one command, one location, one date."""
    w = _warehouse_writer()
    res = add_antenna(
        w,
        warehouse="B9 - Kjallari - Jörð",
        model="SEPPOLANT_X_MF",
        serial="2505010005",
        radome="SCIS",
        radome_serial="RAD-77",
        date_start="2026-07-30",
    )
    assert res.serial == "2505010005"
    assert res.tos_changes["radome_serial"] == "RAD-77"
    # Both joined to the warehouse entity, not a station.
    parents = {c.args[0] for c in w.create_entity_connection.call_args_list}
    assert parents == {9001}
    assert w.create_device.call_count == 2


def test_warehouse_intake_requires_a_real_antenna_serial():
    """A synthetic serial can't be matched at install → the unit would be
    duplicated instead of reparented."""
    w = _warehouse_writer()
    with pytest.raises(CfgOperationError, match="--serial is required"):
        add_antenna(w, warehouse="B9 - Kjallari - Jörð", model="SEPPOLANT_X_MF")
    w.create_device.assert_not_called()


def test_warehouse_intake_requires_a_radome_serial_when_a_radome_is_given():
    w = _warehouse_writer()
    with pytest.raises(CfgOperationError, match="--radome-serial is required"):
        add_antenna(
            w,
            warehouse="B9 - Kjallari - Jörð",
            model="SEPPOLANT_X_MF",
            serial="2505010005",
            radome="SCIS",
        )
    w.create_device.assert_not_called()


def test_station_and_warehouse_are_mutually_exclusive():
    w = _warehouse_writer()
    with pytest.raises(CfgOperationError, match="not both and not neither"):
        add_antenna(
            w,
            station_id="SEY9",
            warehouse="B9 - Kjallari - Jörð",
            model="SEPPOLANT_X_MF",
            serial="x",
        )
    with pytest.raises(CfgOperationError, match="not both and not neither"):
        add_antenna(w, model="SEPPOLANT_X_MF", serial="x")


def test_warehouse_intake_skips_the_one_open_antenna_guard():
    """A warehouse holds any number of spare antennas."""
    w = _warehouse_writer()
    res = add_antenna(
        w,
        warehouse="B9 - Kjallari - Jörð",
        model="SEPPOLANT_X_MF",
        serial="SPARE-2",
        date_start="2026-07-30",
    )
    assert res.serial == "SPARE-2"


def test_radome_serial_override_at_a_station():
    w = _writer()
    res = add_antenna(
        w,
        station_id="SEY9",
        model="LEIAR25.R4",
        radome="LEIT",
        radome_serial="RAD-99",
        date_start="2021-03-25",
    )
    assert res.tos_changes["radome_serial"] == "RAD-99"
