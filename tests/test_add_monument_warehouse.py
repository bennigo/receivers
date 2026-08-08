"""Tests for `cfg add-monument --warehouse` — warehouse-first intake for monuments.

Mirrors `add-antenna --warehouse`. Policy context (bgo, 2026-08-08): all
equipment is logged into the warehouse before it is joined to a station, so a
monument needs an intake path too — not every mark is cast in place.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import CfgOperationError, add_monument

WAREHOUSE = "B9 - Kjallari - Jörð"


@pytest.fixture
def writer():
    w = MagicMock()
    w.find_location_by_name.return_value = 900
    w.find_station_by_marker.return_value = 21721
    w.get_entity_history.return_value = {"attributes": []}
    w.create_device.return_value = {"id_entity": 5555}
    w.create_entity_connection.return_value = {"ok": True}
    return w


def test_warehouse_intake_joins_the_warehouse_not_a_station(writer):
    r = add_monument(
        writer,
        warehouse=WAREHOUSE,
        serial="M-001",
        date_start="2026-08-05",
        dry_run=False,
    )
    assert r.operation == "add-monument"
    writer.find_location_by_name.assert_called_once()
    # Joined under the warehouse entity, never a station.
    writer.create_entity_connection.assert_called_once()
    assert writer.create_entity_connection.call_args[0][0] == 900
    assert r.tos_changes["monument_join"]["parent"] == 900


def test_warehouse_requires_a_real_serial(writer):
    """Synthetic serials need a station marker, and the later move matches by serial."""
    with pytest.raises(CfgOperationError, match="--serial is required"):
        add_monument(writer, warehouse=WAREHOUSE, dry_run=False)
    with pytest.raises(CfgOperationError, match="--serial is required"):
        add_monument(writer, warehouse=WAREHOUSE, serial="   ", dry_run=False)
    writer.create_device.assert_not_called()


def test_station_and_warehouse_are_mutually_exclusive(writer):
    with pytest.raises(CfgOperationError, match="not both and not neither"):
        add_monument(writer, station_id="NPSK", warehouse=WAREHOUSE, dry_run=False)


def test_neither_destination_is_refused(writer):
    with pytest.raises(CfgOperationError, match="not both and not neither"):
        add_monument(writer, dry_run=False)


def test_one_open_monument_guard_does_not_apply_to_the_warehouse(writer, monkeypatch):
    """A warehouse may hold many spare marks; a station may have one open."""
    import receivers.cfg.operations as ops

    monkeypatch.setattr(ops, "_find_open_child", lambda *a, **k: 4242)
    # Station install refuses...
    with pytest.raises(CfgOperationError, match="already has an open monument"):
        add_monument(writer, station_id="NPSK", dry_run=False)
    # ...warehouse intake does not consult the guard at all.
    r = add_monument(writer, warehouse=WAREHOUSE, serial="M-002", dry_run=False)
    assert r.tos_changes["monument_join"]["parent"] == 900


def test_station_path_still_works(writer):
    r = add_monument(
        writer,
        station_id="NPSK",
        height="0.0",
        date_start="2026-08-05",
        dry_run=False,
    )
    assert writer.create_entity_connection.call_args[0][0] == 21721
    assert r.station_id == "NPSK"
    # Serial omitted at a station → synthetic placeholder, as before.
    assert r.tos_changes["synthetic_serial"] is True
    assert r.serial.startswith("monument-NPSK-")


def test_warehouse_serial_is_not_synthetic(writer):
    r = add_monument(writer, warehouse=WAREHOUSE, serial="M-003", dry_run=False)
    assert r.tos_changes["synthetic_serial"] is False
    assert r.serial == "M-003"
