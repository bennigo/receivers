"""Tests for receivers.cfg.operations — install/move/visit orchestration.

These tests mock :class:`tostools.api.tos_writer.TOSWriter` so no network
calls happen, and use a tmp ``stations.cfg`` for the file-write paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import (
    DEFAULT_WAREHOUSE,
    CfgOperationError,
    OperationResult,
    _canonical_receiver_type,
    _default_rinex_valid_from,
    _find_open_gnss_receiver_child,
    _find_recently_left_receiver,
    _resolve_station,
    _validate_marker_match,
    _visit_default_time,
    add_visit,
    archive_receiver_install_date,
    delete_join,
    delete_visit,
    list_visits,
    move_device,
    replace_modem,
    replace_receiver,
    replace_sim,
    set_device_attribute,
    show_visit,
    update_visit,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _writer_mock(**spec) -> MagicMock:
    """Return a TOSWriter-shaped MagicMock with sensible defaults.

    ``spec`` lets each test override return values per attribute.
    """
    w = MagicMock()
    w.dry_run = True
    # Sensible defaults — tests override per case
    w.find_station_by_marker.return_value = None
    w.find_device_by_serial.return_value = None
    w.find_location_by_name.return_value = None
    w.get_entity_history.return_value = None
    w.get_open_parent_join.return_value = None
    w.move_device.return_value = {"closed": "ok", "opened": "ok"}
    w.add_maintenance_visit.return_value = {
        "id_maintenance": 9999,
        "created": {"id": 9999},
        "updated": {"ok": True},
    }
    for k, v in spec.items():
        getattr(w, k).return_value = v
    return w


def _device_with_attrs(
    id_entity: int = 21501,
    model: str = "SEPT POLARX5",
    firmware: str = "5.6.0",
) -> dict:
    """Realistic find_device_by_serial / get_entity_history payload shape.

    TOS returns ``attributes`` as a flat list — each item IS an
    attribute_value row with ``code`` / ``value`` / ``date_from`` /
    ``date_to`` directly on it (no nested ``attribute_values`` wrapper).
    """
    return {
        "id_entity": id_entity,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {
                "code": "model",
                "value": model,
                "date_from": "2026-05-21T00:00:00",
                "date_to": None,
            },
            {
                "code": "firmware_version",
                "value": firmware,
                "date_from": "2026-05-21T00:00:00",
                "date_to": None,
            },
            {
                "code": "serial_number",
                "value": "4101524",
                "date_from": "2026-05-21T00:00:00",
                "date_to": None,
            },
        ],
    }


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    """A minimal stations.cfg with HRAC, SAVI sections."""
    p = tmp_path / "stations.cfg"
    p.write_text(
        """# top-of-file comment

[HRAC]
station_id = HRAC
receiver_type = NetR9
receiver_serial = 5545R50370
receiver_firmware_version = 5.2.2
rinex_config_valid_from = 2025-09-23
# a trailing comment

[SAVI]
station_id = SAVI
receiver_type = NetR9
receiver_serial = 5039K70763
receiver_firmware_version = 4.1.7
rinex_config_valid_from = 2007-09-07
"""
    )
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_canonical_receiver_type_polarx5():
    assert _canonical_receiver_type("SEPT POLARX5") == "PolaRX5"


def test_canonical_receiver_type_netr9():
    assert _canonical_receiver_type("TRIMBLE NETR9") == "NetR9"


def test_canonical_receiver_type_unknown_passthrough():
    """Unknown models pass through unchanged — don't silently corrupt."""
    assert _canonical_receiver_type("FUTUREVENDOR FOO9000") == ("FUTUREVENDOR FOO9000")


def test_canonical_receiver_type_none():
    assert _canonical_receiver_type(None) is None


def test_resolve_station_raises_on_unknown():
    w = _writer_mock()
    with pytest.raises(CfgOperationError, match="No TOS station matches"):
        _resolve_station(w, "NONEXIST")


def test_resolve_station_returns_eid():
    w = _writer_mock(find_station_by_marker=16096)
    assert _resolve_station(w, "HRAC") == 16096


# ---------------------------------------------------------------------------
# _find_open_gnss_receiver_child
# ---------------------------------------------------------------------------


def test_find_open_receiver_returns_id_when_present():
    w = MagicMock()
    w.get_entity_history.side_effect = [
        # First call: station history with one open child
        {
            "children_connections": [
                {"id_entity_child": 21197, "time_to": None},
                {"id_entity_child": 99999, "time_to": "2020-01-01"},  # closed
            ]
        },
        # Second call: that child's own history
        {"code_entity_subtype": "gnss_receiver"},
    ]
    assert _find_open_gnss_receiver_child(w, 16096) == 21197


def test_find_open_receiver_returns_none_when_none_open():
    w = MagicMock()
    w.get_entity_history.return_value = {
        "children_connections": [
            {"id_entity_child": 99999, "time_to": "2020-01-01"},
        ]
    }
    assert _find_open_gnss_receiver_child(w, 16096) is None


def test_find_open_receiver_skips_non_receiver_children():
    """Antenna / radome / other open joins must not be confused for receivers."""
    w = MagicMock()
    w.get_entity_history.side_effect = [
        {
            "children_connections": [
                {"id_entity_child": 50001, "time_to": None},  # antenna
            ]
        },
        {"code_entity_subtype": "antenna"},
    ]
    assert _find_open_gnss_receiver_child(w, 16096) is None


# ---------------------------------------------------------------------------
# move_device — auto-detect target (station marker vs location name)
# ---------------------------------------------------------------------------


def _station_writer(station_eid: int = 16096):
    """Writer mock pre-configured for a station-destination move."""
    w = MagicMock()
    w.find_station_by_marker.return_value = station_eid  # `to` is a station
    w.find_location_by_name.return_value = None
    w.get_entity_history.return_value = {"children_connections": []}
    w.find_device_by_serial.return_value = _device_with_attrs()
    w.move_device.return_value = {"closed": "ok", "opened": "ok"}
    w.add_maintenance_visit.return_value = {
        "id_maintenance": 12345,
        "created": {},
        "updated": {},
    }
    return w


def _location_writer(location_eid: int = 4):
    """Writer mock pre-configured for a warehouse-destination move."""
    w = MagicMock()
    w.find_station_by_marker.return_value = None  # `to` is not a station
    w.find_location_by_name.return_value = location_eid
    w.find_device_by_serial.return_value = _device_with_attrs()
    w.get_open_parent_join.return_value = {
        "id_entity_parent": 4440,
        "time_to": None,
    }
    w.move_device.return_value = {"closed": "ok", "opened": "ok"}
    w.add_maintenance_visit.return_value = {"id_maintenance": 99}
    return w


def test_move_device_raises_when_target_resolves_to_neither():
    w = MagicMock()
    w.find_station_by_marker.return_value = None
    w.find_location_by_name.return_value = None
    with pytest.raises(CfgOperationError, match="resolves to neither"):
        move_device("4101524", to="Bogus Target", writer=w, dry_run=True)


# --- Station-destination path -----------------------------------------------


def test_move_device_to_station_refuses_when_destination_has_open_receiver():
    """Displacement constraint — move the old receiver out first."""
    w = _station_writer()
    w.get_entity_history.side_effect = [
        {"children_connections": [{"id_entity_child": 21197, "time_to": None}]},
        {"code_entity_subtype": "gnss_receiver"},
    ]
    with pytest.raises(CfgOperationError, match="already has an open"):
        move_device("4101524", to="HRAC", date="2026-05-23", writer=w, dry_run=True)
    w.move_device.assert_not_called()
    w.add_maintenance_visit.assert_not_called()


def test_move_device_to_station_raises_on_unknown_serial():
    w = _station_writer()
    w.find_device_by_serial.return_value = None
    with pytest.raises(CfgOperationError, match="No gnss_receiver"):
        move_device("NOSUCH", to="HRAC", date="2026-05-23", writer=w, dry_run=True)


def test_move_device_to_station_dry_run_writes_no_cfg(cfg_file):
    w = _station_writer()
    w.add_maintenance_visit.return_value = {
        "id_maintenance": "<dry-run>",
        "created": "dr",
        "updated": None,
    }
    original = cfg_file.read_text()
    result = move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        writer=w,
        dry_run=True,
        cfg_path=cfg_file,
    )
    assert cfg_file.read_text() == original
    assert result.dry_run is True
    assert result.cfg_changes == {}
    w.move_device.assert_called_once()
    w.add_maintenance_visit.assert_called_once()


def test_move_device_to_station_live_updates_cfg(cfg_file):
    w = _station_writer()
    w.find_device_by_serial.return_value = _device_with_attrs(
        id_entity=21501, model="SEPT POLARX5", firmware="5.6.0"
    )
    result = move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert result.vitjun_id == 12345
    text = cfg_file.read_text()
    assert "receiver_serial = 4101524" in text
    assert "receiver_type = PolaRX5" in text
    assert "receiver_firmware_version = 5.6.0" in text
    # Bare date 2026-05-23 → noon-promoted (12:00) → first full day = next day
    assert "rinex_config_valid_from = 2026-05-24" in text
    assert "# top-of-file comment" in text
    assert "# a trailing comment" in text


def test_move_device_from_station_is_metadata_only():
    """--from-station does NOT pass from_id_entity to TOSWriter.move_device.

    TOSWriter's strict check would error in the in-transit case (receiver
    is at B9, not at the source station anymore). At the high-level we
    trust auto-detect — --from-station is for serial inference + vitjun
    text only.
    """
    w = _station_writer(station_eid=4440)  # SAVI
    move_device(
        "5545R50370",
        to="SAVI",
        date="2026-05-23",
        from_station="HRAC",  # plain metadata
        writer=w,
        dry_run=True,
    )
    call = w.move_device.call_args
    # Three positional args, no from_id_entity kwarg.
    # Bare date is noon-promoted at the operations layer.
    assert call.kwargs.get("from_id_entity") is None
    assert call.args == (21501, 4440, "2026-05-23T12:00:00")


def test_move_device_infers_serial_from_from_station():
    """Without --serial, infer the most recently closed receiver at --from-station."""
    w = _station_writer(station_eid=4440)  # SAVI as target
    # First find_station_by_marker call: destination (SAVI) → returns 4440.
    # But we then need the inference path: find_station_by_marker(from_station)
    # is called via _resolve_station. So set side_effect to return HRAC eid
    # for the second call.
    # _find_recently_left_receiver(HRAC) needs get_entity_history(HRAC) →
    # children with time_to set; then get_entity_history(child_id) → subtype.
    # Then get_entity_history(child_id) AGAIN to read serial_number.
    # Then displacement check on SAVI: get_entity_history(SAVI) → empty children.
    # Then auto-vitjun on SAVI: get_entity_history(SAVI) → no closed children,
    # so it falls to "Settur upp" path. (Sufficient for this test.)
    closed_child = {"id_entity_child": 21197, "time_to": "2026-05-21T23:00:00"}
    receiver_hist = {
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {
                "code": "serial_number",
                "value": "5545R50370",
                "date_from": "2025-09-23T00:00:00",
                "date_to": None,
            },
            {
                "code": "model",
                "value": "TRIMBLE NETR9",
                "date_from": "2025-09-23T00:00:00",
                "date_to": None,
            },
        ],
    }
    w.get_entity_history.side_effect = [
        # 1. _find_recently_left_receiver: HRAC children
        {"children_connections": [closed_child]},
        # 2. child subtype check
        receiver_hist,
        # 3. read serial_number from inferred device
        receiver_hist,
        # 4. SAVI displacement check (empty)
        {"children_connections": []},
        # 5. auto-vitjun lookup on SAVI (no recent closures)
        {"children_connections": []},
    ]
    # find_station_by_marker: destination (SAVI), then from_station (HRAC)
    w.find_station_by_marker.side_effect = [4440, 16096]
    # find_device_by_serial(gnss_receiver, '5545R50370') after inference:
    w.find_device_by_serial.return_value = _device_with_attrs(
        id_entity=21197, model="TRIMBLE NETR9", firmware="5.22"
    )
    move_device(
        # Note: no `serial` positional arg
        to="SAVI",
        from_station="HRAC",
        date="2026-05-23",
        writer=w,
        dry_run=True,
    )
    # find_device_by_serial was called with the inferred serial
    w.find_device_by_serial.assert_called_once_with("gnss_receiver", "5545R50370")


def test_move_device_raises_when_no_selector_given():
    """--id joined --serial/--from-station as a selector (it addresses a device
    exactly, bypassing TOS's fuzzy serial search)."""
    w = MagicMock()
    with pytest.raises(CfgOperationError, match=r"--serial, --id or --from-station"):
        move_device(to="HRAC", writer=w, dry_run=True)


def test_move_device_infers_serial_prefers_open_over_closed():
    """Currently-open receiver wins over old historical closed ones.

    Real-world bug: SAVI's NetR9 5039K70763 is OPEN (never closed since
    2007); a 2007-era device serial '320' is the only CLOSED gnss_receiver.
    Old logic picked '320'; new logic must pick the open one.
    """
    w = MagicMock()
    # First call: from_station "SAVI" → 4440. Second: target "B9..." → None
    # (not a station marker; falls through to location lookup).
    w.find_station_by_marker.side_effect = [4440, None]
    closed_2007 = {"id_entity_child": 4840, "time_to": "2007-09-07T00:00:00"}
    open_now = {"id_entity_child": 4830, "time_to": None}
    open_receiver_hist = {
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {
                "code": "serial_number",
                "value": "5039K70763",
                "date_from": "2014-10-17T00:00:00",
                "date_to": None,
            },
        ],
    }
    # _find_receiver_at_station: SAVI's children, then open child's subtype,
    # then a final get_entity_history(child_id) reads the serial.
    w.get_entity_history.side_effect = [
        {"children_connections": [open_now, closed_2007]},  # SAVI children
        open_receiver_hist,  # open child subtype
        open_receiver_hist,  # read serial
    ]
    w.find_location_by_name.return_value = 4  # B9
    w.get_open_parent_join.return_value = {
        "id_entity_parent": 4440,
        "time_to": None,
    }
    w.find_device_by_serial.return_value = _device_with_attrs(
        id_entity=4830, model="TRIMBLE NETR9", firmware="4.1.7"
    )
    w.move_device.return_value = {"closed": "ok", "opened": "ok"}
    move_device(
        from_station="SAVI",
        date="2026-05-22",
        writer=w,
        dry_run=True,
    )
    w.find_device_by_serial.assert_called_once_with(
        "gnss_receiver", "5039K70763"
    )  # NOT '320'!


def test_move_device_infers_serial_raises_if_no_history():
    """--from-station with no open and no closed receivers → error."""
    w = MagicMock()
    w.find_station_by_marker.return_value = 16096
    w.get_entity_history.return_value = {"children_connections": []}
    with pytest.raises(CfgOperationError, match="no gnss_receiver is currently"):
        move_device(
            to="B9 - Kjallari - Jörð",
            from_station="HRAC",
            writer=w,
            dry_run=True,
        )


def test_default_rinex_valid_from_midnight_install_same_day():
    """Install at exact midnight → same date counts as first full day."""
    assert _default_rinex_valid_from("2026-05-22T00:00:00") == "2026-05-22"


def test_default_rinex_valid_from_non_midnight_rolls_to_next_day():
    """Install at 23:00 → that day is split, next day is first full day."""
    assert _default_rinex_valid_from("2026-05-21T23:00:00") == "2026-05-22"


def test_default_rinex_valid_from_bare_date_treated_as_midnight():
    """YYYY-MM-DD (no time) → treated as midnight → same date."""
    assert _default_rinex_valid_from("2026-05-22") == "2026-05-22"


def test_visit_default_time_promotes_bare_date_to_noon():
    """Bare YYYY-MM-DD → noon (12:00) on that date."""
    assert _visit_default_time("2026-05-22") == "2026-05-22T12:00:00"


def test_visit_default_time_preserves_explicit_iso_datetime():
    """When user provides time, leave it alone."""
    assert _visit_default_time("2026-05-22T09:30:00") == "2026-05-22T09:30:00"
    assert _visit_default_time("2026-05-22T23:45:00") == "2026-05-22T23:45:00"


def test_visit_default_time_none_returns_current_timestamp():
    """None → now (current timestamp, seconds precision) — operator did
    not type --date at all, so "right now" is the most literal default.
    """
    from datetime import datetime as _dt

    before = _dt.now().replace(microsecond=0)
    result = _visit_default_time(None)
    after = _dt.now().replace(microsecond=0)
    parsed = _dt.fromisoformat(result)
    # Within the call window, and definitely not noon (unless we
    # happen to call at noon — accept that)
    assert before <= parsed <= after
    # microseconds stripped
    assert parsed.microsecond == 0


def test_add_visit_bare_date_starts_at_noon():
    """add_visit with bare YYYY-MM-DD → start_time at noon."""
    w = MagicMock()
    w.find_station_by_marker.return_value = 4316  # HEDI
    w.add_maintenance_visit.return_value = {"id_maintenance": 1}
    add_visit(
        "HEDI",
        work="Lagaði loftnetskapal",
        date="2026-05-22",
        writer=w,
        dry_run=True,
    )
    assert w.add_maintenance_visit.call_args.kwargs["start_time"] == (
        "2026-05-22T12:00:00"
    )


def test_add_visit_respects_explicit_time_in_date():
    w = MagicMock()
    w.find_station_by_marker.return_value = 4316
    w.add_maintenance_visit.return_value = {"id_maintenance": 1}
    add_visit(
        "HEDI",
        work="x",
        date="2026-05-22T08:30:00",
        writer=w,
        dry_run=True,
    )
    assert w.add_maintenance_visit.call_args.kwargs["start_time"] == (
        "2026-05-22T08:30:00"
    )


def test_move_device_to_station_default_rinex_rolls_forward(cfg_file):
    """Non-midnight install date → rinex_config_valid_from = next day."""
    w = _station_writer()
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-21T23:00:00",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert "rinex_config_valid_from = 2026-05-22" in cfg_file.read_text()


def test_move_device_to_station_explicit_rinex_wins(cfg_file):
    """--rinex-valid-from overrides the auto-rolled default."""
    w = _station_writer()
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-21T23:00:00",
        rinex_valid_from="2026-06-01",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert "rinex_config_valid_from = 2026-06-01" in cfg_file.read_text()


def test_move_device_to_station_uses_explicit_firmware(cfg_file):
    """--firmware overrides the value read from TOS."""
    w = _station_writer()
    w.find_device_by_serial.return_value = _device_with_attrs(firmware="5.6.0")
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        firmware="5.6.1",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert "receiver_firmware_version = 5.6.1" in cfg_file.read_text()


def test_move_device_to_station_skip_flags(cfg_file):
    """skip_vitjun / skip_cfg short-circuit those steps."""
    w = _station_writer()
    original = cfg_file.read_text()
    result = move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
        skip_vitjun=True,
        skip_cfg=True,
    )
    w.add_maintenance_visit.assert_not_called()
    assert cfg_file.read_text() == original
    assert result.cfg_changes == {}


# --- Location-destination path ----------------------------------------------


def test_move_device_to_location_raises_on_unknown_serial():
    w = _location_writer()
    w.find_device_by_serial.return_value = None
    with pytest.raises(CfgOperationError, match="No gnss_receiver"):
        move_device("NOSUCH", to="B9 - Kjallari - Jörð", writer=w, dry_run=True)


def test_move_device_default_warehouse_is_b9():
    """No `to` arg → defaults to the B9 warehouse string."""
    w = _location_writer()
    move_device("4101524", date="2026-05-23", writer=w, dry_run=True)
    # Auto-detect tries station first (returns None), then location:
    w.find_location_by_name.assert_called_once_with(
        "B9 - Kjallari - Jörð", type_filter="vöruhús"
    )


def test_move_device_to_location_opts_in_to_vitjun_via_text():
    """Location destinations only write vitjun when --vitjun is given."""
    w = _location_writer()
    result = move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        vitjun="Móttakari fjarlægður — sendi til verkstæðis",
    )
    w.add_maintenance_visit.assert_called_once()
    # Vitjun on the source station (4440)
    assert w.add_maintenance_visit.call_args.args[0] == 4440
    assert result.vitjun_id == 99


def test_move_device_to_location_no_vitjun_by_default():
    """Without --vitjun text, the location-path does NOT write a vitjun."""
    w = _location_writer()
    move_device("5039K70763", date="2026-05-23", writer=w, dry_run=True)
    w.add_maintenance_visit.assert_not_called()


def test_move_device_to_location_skip_vitjun_overrides_text():
    w = _location_writer()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=True,
        skip_vitjun=True,
        vitjun="text here",
    )
    w.add_maintenance_visit.assert_not_called()


# --- Auto-clear stations.cfg when device leaves a station for a warehouse


def test_move_device_to_warehouse_clears_source_station_cfg(cfg_file):
    """station → B9 (no replacement) auto-writes NONE to the cfg."""
    w = _location_writer()
    # Source is SAVI (4440); look up the station's marker via get_entity_history
    w.get_entity_history.return_value = {
        "code_entity_subtype": "stöð",
        "attributes": [
            {
                "code": "marker",
                "value": "savi",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
            }
        ],
    }
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    text = cfg_file.read_text()
    # Look at SAVI section specifically — find [SAVI] and check just its body
    savi_section = text.split("[SAVI]")[1].split("\n[")[0]
    assert "receiver_type = NONE" in savi_section
    assert "receiver_serial = NONE" in savi_section
    assert "receiver_firmware_version = NONE" in savi_section
    # rinex_config_valid_from now follows _default_rinex_valid_from:
    # noon-promoted 2026-05-23 → next full day = 2026-05-24
    assert "rinex_config_valid_from = 2026-05-24" in savi_section


def test_move_device_to_warehouse_dry_run_no_clear(cfg_file):
    """Dry-run never touches cfg, including the auto-clear path."""
    w = _location_writer()
    w.get_entity_history.return_value = {
        "code_entity_subtype": "stöð",
        "attributes": [
            {
                "code": "marker",
                "value": "savi",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
            }
        ],
    }
    original = cfg_file.read_text()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=True,
        cfg_path=cfg_file,
    )
    assert cfg_file.read_text() == original


def test_move_device_to_warehouse_no_cfg_flag_suppresses_clear(cfg_file):
    """--no-cfg (skip_cfg=True) suppresses the auto-clear too."""
    w = _location_writer()
    w.get_entity_history.return_value = {
        "code_entity_subtype": "stöð",
        "attributes": [
            {
                "code": "marker",
                "value": "savi",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
            }
        ],
    }
    original = cfg_file.read_text()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
        skip_cfg=True,
    )
    assert cfg_file.read_text() == original


def test_move_device_chained_orchestration_skips_clear(cfg_file):
    """_assume_cleared_device_id signal suppresses cfg-clear.

    Models replace_receiver's first step: move OLD to warehouse, then
    install NEW immediately after. The install-new step writes the new
    cfg values, so the move-old step shouldn't temporarily NONE them.
    """
    w = _location_writer()
    w.get_entity_history.return_value = {
        "code_entity_subtype": "stöð",
        "attributes": [{"code": "marker", "value": "savi"}],
    }
    original = cfg_file.read_text()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
        _assume_cleared_device_id=99,  # any non-None signal
    )
    assert cfg_file.read_text() == original


def test_move_device_warehouse_to_warehouse_skips_clear(cfg_file):
    """Source == location → no cfg-clear (it's not a station)."""
    w = _location_writer()
    # The current parent is the SAME location (warehouse → warehouse)
    w.get_open_parent_join.return_value = {
        "id_entity_parent": 4,  # same as B9 location_eid
        "time_to": None,
    }
    original = cfg_file.read_text()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert cfg_file.read_text() == original


def test_move_device_device_status_triggers_pattern2_transition():
    """--device-status calls transition_attribute_value with code='status'."""
    w = _location_writer()
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    result = move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        device_status="bilað",
    )
    w.transition_attribute_value.assert_called_once_with(
        21501,
        "status",
        "bilað",
        "2026-05-23T12:00:00",  # bare date → noon
    )
    assert "device_status" in result.tos_changes


def test_move_device_device_comment_triggers_pattern2_transition():
    w = _location_writer()
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    result = move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        device_comment="Sendi til verkstæðis: GPS lock óstöðugt",
    )
    w.transition_attribute_value.assert_called_once_with(
        21501,
        "comment",
        "Sendi til verkstæðis: GPS lock óstöðugt",
        "2026-05-23T12:00:00",
    )
    assert "device_comment" in result.tos_changes


def test_move_device_status_and_comment_both_run():
    """Both flags trigger two separate transition calls."""
    w = _location_writer()
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        device_status="bilað",
        device_comment="comment",
    )
    assert w.transition_attribute_value.call_count == 2
    codes = [c.args[1] for c in w.transition_attribute_value.call_args_list]
    assert codes == ["status", "comment"]


def test_move_device_to_station_also_supports_device_attrs():
    """Status/comment transitions also work when target is a station."""
    w = _station_writer()
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        device_status="virkt",  # restoring active after warehouse repair
        cfg_path=None,
        skip_cfg=True,
    )
    w.transition_attribute_value.assert_called_once_with(
        21501, "status", "virkt", "2026-05-23T12:00:00"
    )


def test_move_device_empty_string_status_skips_transition():
    """``device_status=""`` is treated as 'skip', matching the CLI
    --old-status "" "pass empty string to skip" contract."""
    w = _location_writer()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        device_status="",
        skip_cfg=True,
    )
    w.transition_attribute_value.assert_not_called()


def test_move_device_empty_string_comment_skips_transition():
    """``device_comment=""`` is treated as 'skip' — same as status."""
    w = _location_writer()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        device_comment="",
        skip_cfg=True,
    )
    w.transition_attribute_value.assert_not_called()


def test_marker_for_entity_rejects_non_station_subtype(cfg_file):
    """The marker-for-entity helper guards against clearing an
    unrelated station's cfg when the source entity carries a
    ``marker`` attribute but is not subtype ``stöð``."""
    w = _location_writer()
    # Source has a 'marker' attribute but is NOT a station — e.g.
    # an admin-tagged container, a future TOS subtype.
    w.get_entity_history.return_value = {
        "code_entity_subtype": "annað",  # something other than 'stöð'
        "attributes": [{"code": "marker", "value": "savi"}],
    }
    original = cfg_file.read_text()
    move_device(
        "5039K70763",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    # SAVI section must not be touched.
    assert cfg_file.read_text() == original


# --- Auto-vitjun text generation --------------------------------------------


def test_find_recently_left_receiver_returns_most_recent():
    """The latest closed gnss_receiver child wins."""
    w = MagicMock()
    w.get_entity_history.side_effect = [
        {
            "children_connections": [
                {"id_entity_child": 100, "time_to": "2024-01-01T00:00:00"},
                {"id_entity_child": 200, "time_to": "2026-05-21T23:00:00"},
                {"id_entity_child": 300, "time_to": None},  # still open
            ]
        },
        {"code_entity_subtype": "gnss_receiver"},
    ]
    found = _find_recently_left_receiver(w, 16096, "2026-05-21T23:00:00")
    assert found == 200


def test_find_recently_left_receiver_skips_non_receiver_children():
    w = MagicMock()
    w.get_entity_history.side_effect = [
        {
            "children_connections": [
                {"id_entity_child": 555, "time_to": "2026-05-21T23:00:00"},
            ]
        },
        {"code_entity_subtype": "antenna"},
    ]
    assert _find_recently_left_receiver(w, 16096, "2026-05-21T23:00:00") is None


def test_move_device_to_station_auto_vitjun_for_swap(cfg_file):
    """When --vitjun absent, auto-derives text from old + new device context."""
    w = _station_writer()
    # First get_entity_history call: empty children (passes displacement check).
    # Subsequent calls: for auto-vitjun, station's history with closed 21197,
    # then 21197's own history (to confirm gnss_receiver subtype + read attrs).
    w.get_entity_history.side_effect = [
        # 1. displacement-check on dest
        {"children_connections": []},
        # 2. _find_recently_left_receiver: station children
        {
            "children_connections": [
                {"id_entity_child": 21197, "time_to": "2026-05-21T23:00:00"},
            ]
        },
        # 3. _find_recently_left_receiver: child subtype check
        {"code_entity_subtype": "gnss_receiver"},
        # 4. fetch the old device's attrs
        _device_with_attrs(id_entity=21197, model="TRIMBLE NETR9", firmware="5.22")
        | {
            "attributes": [
                {
                    "code": "model",
                    "value": "TRIMBLE NETR9",
                    "date_from": "2025-09-23T00:00:00",
                    "date_to": None,
                },
                {
                    "code": "serial_number",
                    "value": "5545R50370",
                    "date_from": "2025-09-23T00:00:00",
                    "date_to": None,
                },
            ]
        },
    ]
    w.find_device_by_serial.return_value = _device_with_attrs(
        id_entity=21501, model="SEPT POLARX5", firmware="5.7.0"
    )
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-21T23:00:00",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    work_arg = w.add_maintenance_visit.call_args.kwargs.get("work")
    assert "Skipt um móttakara" in work_arg
    assert "NetR9 5545R50370" in work_arg
    assert "PolaRX5 4101524" in work_arg


def test_move_device_to_station_auto_vitjun_for_new_deploy(cfg_file):
    """Empty station with no prior closed receiver → 'Settur upp' default."""
    w = _station_writer()
    # First call: empty children (displacement passes); subsequent calls
    # for auto-vitjun: no closed children at all.
    w.get_entity_history.side_effect = [
        {"children_connections": []},
        {"children_connections": []},
    ]
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    work = w.add_maintenance_visit.call_args.kwargs.get("work")
    assert work.startswith("Settur upp móttakari")


def test_move_device_to_station_explicit_vitjun_wins(cfg_file):
    """--vitjun TEXT overrides the auto-derived default."""
    w = _station_writer()
    move_device(
        "4101524",
        to="HRAC",
        date="2026-05-23",
        vitjun="custom text — overrides auto",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert w.add_maintenance_visit.call_args.kwargs["work"] == (
        "custom text — overrides auto"
    )


# ---------------------------------------------------------------------------
# add_visit
# ---------------------------------------------------------------------------


def test_add_visit_resolves_station_and_calls_writer():
    w = MagicMock()
    w.find_station_by_marker.return_value = 4316  # HEDI
    w.add_maintenance_visit.return_value = {"id_maintenance": 9999}
    result = add_visit(
        "HEDI",
        work="Lagaði loftnetskapal",
        reasons=["repairs"],
        date="2026-05-23",
        participants="bgo@vedur.is",
        writer=w,
        dry_run=True,
    )
    assert result.station_id == "HEDI"
    assert result.vitjun_id == 9999
    call = w.add_maintenance_visit.call_args
    assert call.args[0] == 4316
    assert call.kwargs["work"] == "Lagaði loftnetskapal"
    assert call.kwargs["reasons"] == ["repairs"]
    assert call.kwargs["participants"] == "bgo@vedur.is"


def test_add_visit_default_reason_is_repairs():
    w = MagicMock()
    w.find_station_by_marker.return_value = 4316
    w.add_maintenance_visit.return_value = {"id_maintenance": 1}
    add_visit("HEDI", work="Lagaði loftnetskapal", writer=w, dry_run=True)
    assert w.add_maintenance_visit.call_args.kwargs["reasons"] == ["repairs"]


def test_add_visit_raises_when_station_unknown():
    w = MagicMock()
    w.find_station_by_marker.return_value = None
    with pytest.raises(CfgOperationError, match="No TOS station matches"):
        add_visit("XXXX", work="anything", writer=w, dry_run=True)


# ---------------------------------------------------------------------------
# delete_join
# ---------------------------------------------------------------------------


def test_delete_join_passes_id_through_to_writer():
    w = MagicMock()
    w.delete_entity_connection.return_value = None
    result = delete_join(27836, writer=w, dry_run=False)
    w.delete_entity_connection.assert_called_once_with(27836)
    assert result.operation == "delete-join"
    assert result.tos_changes["id_connection"] == 27836
    assert result.dry_run is False


def test_delete_join_dry_run_default():
    w = MagicMock()
    w.delete_entity_connection.return_value = "fake-dry-run"
    result = delete_join(27836, writer=w)  # dry_run defaults to True
    assert result.dry_run is True
    # The writer is still called (the writer's own dry_run flag handles
    # the no-send semantics — operations just forwards).
    w.delete_entity_connection.assert_called_once_with(27836)


# ---------------------------------------------------------------------------
# show_visit / list_visits / update_visit
# ---------------------------------------------------------------------------


def test_show_visit_returns_writer_payload():
    w = MagicMock()
    detail = {"id_maintenance": 5147, "maintenance_attribute_values": []}
    w.get_maintenance_visit.return_value = detail
    assert show_visit(5147, writer=w) is detail


def test_show_visit_raises_when_not_found():
    w = MagicMock()
    w.get_maintenance_visit.return_value = None
    with pytest.raises(CfgOperationError, match="No vitjun"):
        show_visit(99999, writer=w)


def test_list_visits_resolves_station_then_calls_writer():
    w = MagicMock()
    w.find_station_by_marker.return_value = 4316  # HEDI
    sample = [{"id": 5146}, {"id": 5147}]
    w.list_maintenance_visits.return_value = sample
    assert list_visits("HEDI", writer=w) == sample
    w.list_maintenance_visits.assert_called_once_with(4316)


def test_list_visits_raises_on_unknown_station():
    w = MagicMock()
    w.find_station_by_marker.return_value = None
    with pytest.raises(CfgOperationError, match="No TOS station"):
        list_visits("XXXX", writer=w)


def test_update_visit_forwards_kwargs_to_writer():
    w = MagicMock()
    w.update_maintenance_visit.return_value = {
        "id_maintenance": 5147,
        "updated": {"ok": True},
        "before": {},
        "after": {},
    }
    result = update_visit(
        5147,
        remaining="Þarf að mála",
        reasons=["repairs"],
        writer=w,
        dry_run=False,
    )
    call = w.update_maintenance_visit.call_args
    assert call.args[0] == 5147
    assert call.kwargs["remaining"] == "Þarf að mála"
    assert call.kwargs["reasons"] == ["repairs"]
    # Unspecified fields are forwarded as None (preserve semantics)
    assert call.kwargs["work"] is None
    assert call.kwargs["comment"] is None
    assert call.kwargs["participants"] is None
    assert result.vitjun_id == 5147
    assert result.operation == "visit-edit"


# ---------------------------------------------------------------------------
# replace_receiver — orchestration
# ---------------------------------------------------------------------------


def test_validate_marker_match_accepts_none():
    """Probe couldn't read marker → no check fires."""
    _validate_marker_match(None, "ARHO")  # no exception


def test_validate_marker_match_accepts_test():
    """Bench-default 'TEST' is acceptable (will be auto-corrected later)."""
    _validate_marker_match("TEST", "ARHO")
    _validate_marker_match("test", "ARHO")


def test_validate_marker_match_accepts_station_marker():
    _validate_marker_match("ARHO", "ARHO")
    _validate_marker_match("arho", "ARHO")  # case-insensitive


def test_validate_marker_match_rejects_other():
    with pytest.raises(CfgOperationError, match="marker_name is 'HRAC'"):
        _validate_marker_match("HRAC", "ARHO")


def _replace_writer(*, with_b9_eid=4, station_eid=4233, station_marker="ARHO"):
    """Pre-rigged writer for replace_receiver tests.

    ``find_station_by_marker`` returns the station eid only for the
    target marker; any other input (including "B9 - Kjallari - Jörð"
    used by the move helpers) returns None so the auto-detect falls
    through to location lookup.
    """
    w = MagicMock()
    w.dry_run = False
    w.find_station_by_marker.side_effect = lambda marker, **_kw: (
        station_eid if marker.upper() == station_marker.upper() else None
    )
    w.find_location_by_name.return_value = with_b9_eid
    # Default: new serial NOT in TOS (warehouse intake required)
    w.find_device_by_serial.return_value = None
    w.move_device.return_value = {"closed": "ok", "opened": "ok"}
    w.add_maintenance_visit.return_value = {
        "id_maintenance": 9999,
        "created": {},
        "updated": {},
    }
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    w.create_device.return_value = {"id_entity": 21999}
    w.connect_device_to_location.return_value = "connect_ok"
    w.get_open_parent_join.return_value = None
    return w


@pytest.fixture
def stub_move_device(monkeypatch):
    """Replace operations.move_device with a recording stub.

    Lets replace_receiver tests focus on its orchestration logic without
    re-exercising the move_device displacement check / cfg-write path
    (those are covered by their own tests). The stub returns an
    OperationResult that mimics a successful move and records each call.
    """
    move_calls = []

    def fake_move(serial, **kwargs):
        move_calls.append((serial, kwargs))
        return OperationResult(
            operation="move",
            serial=serial,
            date=kwargs.get("date"),
            tos_changes={"move": "stub", "to_location": kwargs.get("to")},
            cfg_changes={},
            vitjun_id=9999 if kwargs.get("to") != DEFAULT_WAREHOUSE else None,
            dry_run=kwargs.get("dry_run", True),
        )

    from receivers.cfg import operations as _ops

    monkeypatch.setattr(_ops, "move_device", fake_move)
    return move_calls


def _replace_station_with_old_receiver(w, old_id=21197, old_serial="5039K70766"):
    """Wire up: station has open old gnss_receiver. Both serial lookup and
    history lookup return consistent data."""
    receiver_hist = {
        "id_entity": old_id,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {
                "code": "serial_number",
                "value": old_serial,
                "date_from": "2012-08-28T00:00:00",
                "date_to": None,
            },
            {
                "code": "model",
                "value": "TRIMBLE NETR9",
                "date_from": "2012-08-28T00:00:00",
                "date_to": None,
            },
        ],
    }

    def history_for(eid):
        if eid == 4233:  # the station
            return {
                "children_connections": [
                    {"id_entity_child": old_id, "time_to": None},
                ]
            }
        return receiver_hist

    w.get_entity_history.side_effect = history_for

    # find_device_by_serial: new serial → None (fresh); old serial → device
    def find_by_serial(subtype, serial):
        if serial == old_serial:
            return receiver_hist
        return None

    w.find_device_by_serial.side_effect = find_by_serial


def test_replace_receiver_manual_mode_skips_probe(
    tmp_path, monkeypatch, stub_move_device
):
    """All three identity fields supplied → no probe is attempted."""
    w = _replace_writer()
    _replace_station_with_old_receiver(w)

    def boom(*a, **kw):
        raise AssertionError("probe_receiver should not be called in manual mode")

    monkeypatch.setattr("receivers.cfg.device_probe.probe_receiver", boom)

    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[ARHO]\nreceiver_serial = 5039K70766\n")

    result = replace_receiver(
        "ARHO",
        "polarx5",
        date="2026-05-23",
        new_serial="4101525",
        new_model="SEPT POLARX5",
        new_firmware="5.7.0",
        new_marker="ARHO",
        writer=w,
        dry_run=False,
        cfg_path=cfg,
    )
    assert result.operation == "replace"
    assert result.serial == "4101525"
    assert "warehouse_create" in result.tos_changes
    assert "move_old" in result.tos_changes
    assert "install_new" in result.tos_changes
    # Two move_device calls — one to B9 for the old, one to ARHO for the new
    assert len(stub_move_device) == 2
    assert stub_move_device[0][0] == "5039K70766"  # old → B9
    assert stub_move_device[0][1]["to"] == DEFAULT_WAREHOUSE
    assert stub_move_device[1][0] == "4101525"  # new → ARHO
    assert stub_move_device[1][1]["to"] == "ARHO"


def test_replace_receiver_refuses_when_new_serial_equals_old():
    """The same physical unit can't be its own replacement."""
    w = _replace_writer()
    _replace_station_with_old_receiver(w, old_serial="4101524")
    with pytest.raises(CfgOperationError, match="matches the old receiver"):
        replace_receiver(
            "ARHO",
            "polarx5",
            new_serial="4101524",  # same as old
            new_model="SEPT POLARX5",
            new_firmware="5.7.0",
            new_marker="ARHO",
            writer=w,
            dry_run=True,
        )


def test_replace_receiver_refuses_on_marker_mismatch():
    w = _replace_writer()
    _replace_station_with_old_receiver(w)
    with pytest.raises(CfgOperationError, match="marker_name is 'HRAC'"):
        replace_receiver(
            "ARHO",
            "polarx5",
            new_serial="9999999",
            new_model="SEPT POLARX5",
            new_firmware="5.7.0",
            new_marker="HRAC",  # configured for another station!
            writer=w,
            dry_run=True,
        )


def test_replace_receiver_skip_marker_check_overrides_rejection(stub_move_device):
    w = _replace_writer()
    _replace_station_with_old_receiver(w)
    # Doesn't raise:
    replace_receiver(
        "ARHO",
        "polarx5",
        new_serial="9999999",
        new_model="SEPT POLARX5",
        new_firmware="5.7.0",
        new_marker="HRAC",
        skip_marker_check=True,
        writer=w,
        dry_run=True,
    )


def test_replace_receiver_refuses_existing_device_at_other_station():
    """New serial already in TOS but joined to a station != B9 → red flag."""
    w = _replace_writer()
    _replace_station_with_old_receiver(w)
    # Override the per-serial side_effect from the helper:
    w.find_device_by_serial.side_effect = None
    w.find_device_by_serial.return_value = {"id_entity": 22000}
    # That device is currently joined to some OTHER station (not B9):
    w.get_open_parent_join.return_value = {
        "id_entity_parent": 16096,  # HRAC, not B9 (4)
        "time_to": None,
    }
    with pytest.raises(CfgOperationError, match="already joined to TOS entity 16096"):
        replace_receiver(
            "ARHO",
            "polarx5",
            new_serial="4101525",
            new_model="SEPT POLARX5",
            new_firmware="5.7.0",
            new_marker="ARHO",
            writer=w,
            dry_run=True,
        )


def test_replace_receiver_reuses_existing_device_at_b9(stub_move_device):
    """New serial already in TOS at B9 → skip warehouse intake, reuse."""
    w = _replace_writer()
    _replace_station_with_old_receiver(w)
    # Override find_device_by_serial to always return the existing device
    w.find_device_by_serial.side_effect = None
    w.find_device_by_serial.return_value = {"id_entity": 22000}
    w.get_open_parent_join.return_value = {
        "id_entity_parent": 4,  # B9
        "time_to": None,
    }
    result = replace_receiver(
        "ARHO",
        "polarx5",
        new_serial="4101525",
        new_model="SEPT POLARX5",
        new_firmware="5.7.0",
        new_marker="ARHO",
        writer=w,
        dry_run=True,
    )
    assert result.tos_changes["warehouse_create"] == "skipped (already in TOS)"
    # create_device should NOT have been called
    w.create_device.assert_not_called()


def test_replace_receiver_continue_from_install_new(stub_move_device):
    """--continue-from install-new skips warehouse+move-old."""
    w = _replace_writer()
    _replace_station_with_old_receiver(w)
    result = replace_receiver(
        "ARHO",
        "polarx5",
        new_serial="4101525",
        new_model="SEPT POLARX5",
        new_firmware="5.7.0",
        new_marker="ARHO",
        continue_from="install-new",
        writer=w,
        dry_run=True,
    )
    assert "warehouse_create" not in result.tos_changes
    assert "move_old" not in result.tos_changes
    assert "install_new" in result.tos_changes


def test_replace_receiver_refuses_when_station_has_no_open_receiver():
    w = _replace_writer()
    # Station has no open children:
    w.get_entity_history.return_value = {"children_connections": []}
    with pytest.raises(CfgOperationError, match="nothing to replace"):
        replace_receiver(
            "ARHO",
            "polarx5",
            new_serial="4101525",
            new_model="SEPT POLARX5",
            new_firmware="5.7.0",
            new_marker="ARHO",
            writer=w,
            dry_run=True,
        )


def test_replace_receiver_continue_from_validates_step_name():
    w = _replace_writer()
    with pytest.raises(CfgOperationError, match="--continue-from must be one of"):
        replace_receiver(
            "ARHO",
            "polarx5",
            new_serial="X",
            new_model="Y",
            new_firmware="Z",
            new_marker="ARHO",
            continue_from="bogus",
            writer=w,
            dry_run=True,
        )


# ---------------------------------------------------------------------------
# fill_install_attributes — position install-attr fill (todo #28)
# ---------------------------------------------------------------------------


class _RecordingWriter:
    """Minimal TOSWriter stand-in that records attribute-write calls.

    ``fill_install_attributes`` reaches the writer only through
    ``tos_push.push_field_to_tos`` (→ ``upsert_attribute_value``) and
    ``push_field_transition_to_tos`` (→ ``transition_attribute_value``); for
    station-entity fields ``resolve_entity_id`` short-circuits to the station
    id without touching the writer, so those two methods are all we need.
    """

    def __init__(self):
        self.dry_run = True
        self.calls: list[dict] = []

    def upsert_attribute_value(self, id_entity, code, value, date_from, **kw):
        self.calls.append(
            {
                "method": "upsert_attribute_value",
                "id_entity": id_entity,
                "code": code,
                "value": value,
                "date_from": date_from,
            }
        )
        return {"ok": True}

    def transition_attribute_value(
        self, id_entity, code, new_value, transition_date, **kw
    ):
        self.calls.append(
            {
                "method": "transition_attribute_value",
                "id_entity": id_entity,
                "code": code,
                "new_value": new_value,
                "transition_date": transition_date,
            }
        )
        return {"closed": {"ok": True}, "opened": {"ok": True}}


def _install_writer() -> Any:
    """Recording writer usable by the install-attr push helpers.

    Annotated ``Any`` so the duck-typed fake satisfies the ``writer:
    TOSWriter`` parameter of ``fill_install_attributes`` without a cast at
    every call site (mirrors how the MagicMock-based ``_writer_mock`` is
    accepted elsewhere in this file).
    """
    return _RecordingWriter()


def _station_cfg(lat=None, lon=None, height=None):
    cfg = {}
    if lat is not None:
        cfg["latitude"] = lat
    if lon is not None:
        cfg["longitude"] = lon
    if height is not None:
        cfg["height"] = height
    return cfg


def _tos_record(id_entity=4242, lat=None, lon=None, altitude=None):
    """Minimal TOS station dict for the position extractors + push helpers."""
    rec = {"id_entity": id_entity}
    if lat is not None:
        rec["lat"] = lat
    if lon is not None:
        rec["lon"] = lon
    if altitude is not None:
        rec["altitude"] = altitude
    return rec


def test_fill_install_attrs_adds_missing_position():
    """cfg has position, TOS has none → upsert each field (Pattern 1)."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    changes = fill_install_attributes(
        w,
        "HRAC",
        _station_cfg(lat="64.130000", lon="-21.900000", height="100.0"),
        _tos_record(),  # no lat/lon/altitude in TOS yet
        "2026-05-31T12:00:00",
        confirm=lambda p: "add",
    )
    upserts = {
        c["code"]: c["value"]
        for c in w.calls
        if c["method"] == "upsert_attribute_value"
    }
    assert upserts == {
        "lat": "64.130000",
        "lon": "-21.900000",
        "altitude": "100.0",
    }
    # all writes target the station entity id from tos_data
    assert all(
        c["id_entity"] == 4242
        for c in w.calls
        if c["method"] == "upsert_attribute_value"
    )
    assert changes["latitude"].startswith("upsert→")
    assert "transition_attribute_value" not in {c["method"] for c in w.calls}


def test_fill_install_attrs_noop_when_equal():
    """TOS already matches cfg within tolerance → no writes, no confirm."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    confirm_calls = []

    changes = fill_install_attributes(
        w,
        "HRAC",
        _station_cfg(lat="64.130000", lon="-21.900000", height="100.0"),
        _tos_record(lat="64.130000", lon="-21.900000", altitude="100.4"),
        "2026-05-31T12:00:00",
        confirm=lambda p: confirm_calls.append(p) or "add",
        position_tolerance_m=2.0,
    )
    # height 100.0 vs 100.4 is within 2 m; lat/lon identical → all unchanged
    assert confirm_calls == []
    assert not [
        c
        for c in w.calls
        if c["method"] in ("upsert_attribute_value", "transition_attribute_value")
    ]
    assert set(changes.values()) == {"unchanged"}


def test_fill_install_attrs_differ_change_uses_transition():
    """cfg differs from TOS + confirm 'change' → Pattern 2 transition."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    changes = fill_install_attributes(
        w,
        "HRAC",
        _station_cfg(height="250.0"),
        _tos_record(altitude="100.0"),  # differs by 150 m
        "2026-05-31T12:00:00",
        confirm=lambda p: "change",
    )
    transitions = [c for c in w.calls if c["method"] == "transition_attribute_value"]
    assert len(transitions) == 1
    assert transitions[0]["code"] == "altitude"
    assert transitions[0]["new_value"] == "250.0"
    assert transitions[0]["transition_date"] == "2026-05-31T12:00:00"
    assert changes["height"].startswith("transition→")


def test_fill_install_attrs_differ_correct_uses_upsert():
    """cfg differs from TOS + confirm 'correct' → Pattern 1 in-place upsert."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    changes = fill_install_attributes(
        w,
        "HRAC",
        _station_cfg(height="250.0"),
        _tos_record(altitude="100.0"),
        "2026-05-31T12:00:00",
        confirm=lambda p: "correct",
    )
    upserts = [c for c in w.calls if c["method"] == "upsert_attribute_value"]
    assert len(upserts) == 1
    assert upserts[0]["code"] == "altitude" and upserts[0]["value"] == "250.0"
    assert "transition_attribute_value" not in {c["method"] for c in w.calls}
    assert changes["height"].startswith("upsert→")


def test_fill_install_attrs_skip_writes_nothing():
    """confirm 'skip' → field recorded as skipped, no TOS write."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    changes = fill_install_attributes(
        w,
        "HRAC",
        _station_cfg(lat="64.130000"),
        _tos_record(),
        "2026-05-31T12:00:00",
        confirm=lambda p: "skip",
    )
    assert not [
        c
        for c in w.calls
        if c["method"] in ("upsert_attribute_value", "transition_attribute_value")
    ]
    assert changes["latitude"] == "skipped"


def test_fill_install_attrs_skips_fields_absent_from_cfg():
    """Fields with no cfg value are silently ignored (confirm never called)."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    seen = []
    changes = fill_install_attributes(
        w,
        "HRAC",
        _station_cfg(lat="64.130000"),  # lon/height absent
        _tos_record(),
        "2026-05-31T12:00:00",
        confirm=lambda p: seen.append(p.cfg_key) or "add",
    )
    assert seen == ["latitude"]
    assert "longitude" not in changes and "height" not in changes


def test_fill_install_attrs_requires_id_entity():
    """Missing id_entity in tos_data → CfgOperationError (no silent no-op)."""
    from receivers.cfg.operations import fill_install_attributes

    w = _install_writer()
    with pytest.raises(CfgOperationError):
        fill_install_attributes(
            w,
            "HRAC",
            _station_cfg(lat="64.13"),
            {"lat": "64.13"},  # no id_entity
            "2026-05-31T12:00:00",
            confirm=lambda p: "add",
        )


# ---------------------------------------------------------------------------
# delete_visit
# ---------------------------------------------------------------------------


def test_delete_visit_passes_id_through_to_writer():
    w = MagicMock()
    w.delete_maintenance.return_value = None
    result = delete_visit(5147, writer=w, dry_run=False)
    w.delete_maintenance.assert_called_once_with(5147)
    assert result.operation == "delete-visit"
    assert result.tos_changes["id_maintenance"] == 5147
    assert result.vitjun_id == 5147
    assert result.dry_run is False


def test_delete_visit_dry_run_default():
    w = MagicMock()
    w.delete_maintenance.return_value = "fake-dry-run"
    result = delete_visit(5147, writer=w)  # dry_run defaults to True
    assert result.dry_run is True
    w.delete_maintenance.assert_called_once_with(5147)


# ---------------------------------------------------------------------------
# replace_modem / replace_sim — telemetry swaps
# ---------------------------------------------------------------------------


def _telemetry_writer(
    *,
    station_eid=4312,
    old_child_id=None,
    old_subtype=None,
    old_attrs=None,
):
    """Writer mock for replace_modem / replace_sim.

    Drives ``_find_open_child``'s two-step history walk: the station history
    lists one open child (``old_child_id``), and that child's history reports
    ``old_subtype`` + ``old_attrs``. When ``old_child_id`` is None the station
    has no open telemetry child (fresh install).
    """
    w = MagicMock()
    w.dry_run = False
    w.find_station_by_marker.return_value = station_eid
    w.find_location_by_name.return_value = 4  # B9 warehouse eid

    station_hist = {
        "children_connections": (
            [{"id_entity_child": old_child_id, "time_to": None}]
            if old_child_id is not None
            else []
        )
    }
    child_hist = {
        "code_entity_subtype": old_subtype,
        "attributes": old_attrs or [],
    }

    def _hist(eid):
        if eid == station_eid:
            return station_hist
        if old_child_id is not None and eid == old_child_id:
            return child_hist
        return None

    w.get_entity_history.side_effect = _hist
    # By default the NEW device is not yet in TOS (fresh install → create path).
    # Pre-registered-device tests override this with the existing entity.
    w.find_device_by_serial.return_value = None
    w.create_device.return_value = {"id_entity": 22000}
    w.create_entity_connection.return_value = {"opened": "ok"}
    w.move_device.return_value = {"closed": "ok", "opened": "ok"}
    w.get_open_parent_join.return_value = {"id": 555, "id_entity_parent": station_eid}
    w.patch_entity_connection.return_value = {"closed": "ok"}
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    w.add_maintenance_visit.return_value = {"id_maintenance": 9999}
    return w


def _attr(code, value):
    return {"code": code, "value": value, "date_from": "2020-01-01", "date_to": None}


def test_replace_modem_fresh_install_no_old():
    """No existing modem → create new modem_gsm + join + vitjun; no retire."""
    w = _telemetry_writer(old_child_id=None)
    result = replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        date="2026-06-06",
        writer=w,
        dry_run=False,
    )
    # New device created with modem_gsm subtype
    w.create_device.assert_called_once()
    assert w.create_device.call_args.kwargs["entity_subtype"] == "modem_gsm"
    # Joined to the station (4312)
    w.create_entity_connection.assert_called_once()
    assert w.create_entity_connection.call_args.kwargs["id_parent"] == 4312
    # Vitjun written, no old retire
    w.add_maintenance_visit.assert_called_once()
    w.move_device.assert_not_called()
    assert result.operation == "replace-modem"
    assert result.serial == "6001312345"
    assert result.vitjun_id == 9999


def test_replace_modem_swaps_old_to_warehouse():
    """Existing modem → new created + old moved to B9 with status transition."""
    w = _telemetry_writer(
        old_child_id=20771,
        old_subtype="modem_gsm",
        old_attrs=[_attr("serial_number", "6001254079")],
    )
    result = replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        old_status="bilað",
        date="2026-06-06",
        writer=w,
        dry_run=False,
    )
    # Old modem moved to warehouse (eid 4) via move_device
    w.move_device.assert_called_once_with(20771, 4, "2026-06-06T12:00:00")
    # Old status transitioned to bilað
    w.transition_attribute_value.assert_any_call(
        20771, "status", "bilað", "2026-06-06T12:00:00"
    )
    assert result.tos_changes["plan"]["old_serial"] == "6001254079"


def test_replace_modem_reuses_preregistered_device():
    """New modem already in TOS (bench intake) → reparent, don't duplicate.

    The bench process often registers/warehouses the new router in TOS before
    the field install. The new device's serial already resolves via
    find_device_by_serial, so replace_modem must reparent it to the station
    with move_device instead of calling create_device (which refuses a
    duplicate). Regression guard for the RIFC swap.
    """
    w = _telemetry_writer(
        old_child_id=20771,
        old_subtype="modem_gsm",
        old_attrs=[_attr("serial_number", "6001254079")],
    )
    # New modem 6001312345 is already registered as id_entity 20421 (warehoused).
    w.find_device_by_serial.return_value = {"id_entity": 20421}
    result = replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        date="2026-06-06",
        writer=w,
        dry_run=False,
    )
    # No duplicate created for the pre-registered device.
    w.create_device.assert_not_called()
    # Old modem (20771) retired to B9 (eid 4) AND new modem (20421) reparented
    # to the station (eid 4312) — both via move_device.
    w.move_device.assert_any_call(20771, 4, "2026-06-06T12:00:00")
    w.move_device.assert_any_call(20421, 4312, "2026-06-06T12:00:00")
    w.add_maintenance_visit.assert_called_once()
    assert result.serial == "6001312345"


def test_replace_modem_rejects_same_serial():
    """New serial == open modem's serial → refuse (no-op swap)."""
    w = _telemetry_writer(
        old_child_id=20771,
        old_subtype="modem_gsm",
        old_attrs=[_attr("serial_number", "6001254079")],
    )
    with pytest.raises(CfgOperationError, match="matches the modem already"):
        replace_modem(
            "GSIG",
            new_serial="6001254079",
            new_model="Teltonika RUT200",
            writer=w,
            dry_run=False,
        )


def test_replace_modem_writes_router_type_to_cfg(cfg_file):
    """--router-type updates stations.cfg router_type on a live run."""
    # GSIG section needed in the cfg fixture
    cfg_file.write_text(cfg_file.read_text() + "\n[GSIG]\nrouter_type = Conel\n")
    w = _telemetry_writer(old_child_id=None)
    result = replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        new_router_type="Teltonika",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
    )
    assert result.cfg_changes.get("router_type") == "Teltonika"
    assert "router_type = Teltonika" in cfg_file.read_text()


def test_replace_modem_dry_run_no_cfg_write(cfg_file):
    """Dry-run: cfg untouched even with --router-type."""
    w = _telemetry_writer(old_child_id=None)
    before = cfg_file.read_text()
    result = replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        new_router_type="Teltonika",
        writer=w,
        dry_run=True,
        cfg_path=cfg_file,
    )
    assert result.cfg_changes == {}
    assert cfg_file.read_text() == before


def test_replace_sim_creates_new_entity_and_closes_old():
    """SIM swap: new sim_card created + joined; old join closed (not warehoused)."""
    w = _telemetry_writer(
        old_child_id=5785,
        old_subtype="sim_card",
        old_attrs=[_attr("ip_address", "10.4.1.225")],
    )
    result = replace_sim(
        "GSIG",
        ip_address="10.4.1.240",
        phone_number="8400754",
        date="2026-06-06",
        writer=w,
        dry_run=False,
    )
    # New sim_card created + joined to station
    assert w.create_device.call_args.kwargs["entity_subtype"] == "sim_card"
    w.create_entity_connection.assert_called_once()
    # Old SIM join CLOSED via patch (not moved to a warehouse)
    w.patch_entity_connection.assert_called_once_with(
        555, time_to="2026-06-06T12:00:00"
    )
    w.move_device.assert_not_called()
    assert result.operation == "replace-sim"
    assert result.tos_changes["plan"]["old_ip"] == "10.4.1.225"
    assert result.tos_changes["plan"]["new_ip"] == "10.4.1.240"


def test_replace_sim_cfg_ip_off_by_default(cfg_file):
    """router_ip in cfg is NOT written unless --update-cfg-ip given."""
    cfg_file.write_text(
        cfg_file.read_text() + "\n[GSIG]\nrouter_ip = GSIG.gps.vedur.is\n"
    )
    w = _telemetry_writer(old_child_id=None)
    result = replace_sim(
        "GSIG",
        ip_address="10.4.1.240",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
        update_cfg_ip=False,
    )
    assert result.cfg_changes == {}
    assert "GSIG.gps.vedur.is" in cfg_file.read_text()  # hostname preserved


def test_replace_sim_cfg_ip_written_when_requested(cfg_file):
    """--update-cfg-ip writes the literal IP to router_ip."""
    cfg_file.write_text(
        cfg_file.read_text() + "\n[GSIG]\nrouter_ip = GSIG.gps.vedur.is\n"
    )
    w = _telemetry_writer(old_child_id=None)
    result = replace_sim(
        "GSIG",
        ip_address="10.4.1.240",
        writer=w,
        dry_run=False,
        cfg_path=cfg_file,
        update_cfg_ip=True,
    )
    assert result.cfg_changes.get("router_ip") == "10.4.1.240"
    assert "router_ip = 10.4.1.240" in cfg_file.read_text()


def test_replace_sim_ip_only_omits_phone():
    """No --phone → sim_card built with ip_address (+ default status), no phone."""
    w = _telemetry_writer(old_child_id=None)
    replace_sim("GSIG", ip_address="10.4.1.240", writer=w, dry_run=False)
    attrs = w.create_device.call_args.kwargs["attributes"]
    codes = [a["code"] for a in attrs]
    assert "ip_address" in codes
    assert "phone_number" not in codes  # omitted when not given
    assert "status" in codes  # default "virkt"


def test_replace_sim_passes_optional_attrs():
    """Named optionals + extra_attrs flow into the sim_card builder."""
    w = _telemetry_writer(old_child_id=None)
    replace_sim(
        "GSIG",
        ip_address="10.4.1.240",
        phone_number="8400754",
        serial_number="89354010120801048520",
        provider="Síminn",
        model="sim kort",
        owner="Jarðeðlismælihópur",
        extra_attrs={"date_end": "2027-01-01"},
        writer=w,
        dry_run=False,
    )
    attrs = {
        a["code"]: a["value"] for a in w.create_device.call_args.kwargs["attributes"]
    }
    assert attrs["provider"] == "Síminn"
    assert attrs["serial_number"] == "89354010120801048520"
    assert attrs["date_end"] == "2027-01-01"


def test_replace_modem_passes_optional_attrs():
    """Named optionals + extra_attrs flow into the modem_gsm builder."""
    w = _telemetry_writer(old_child_id=None)
    replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        ip_address="157.157.24.132",
        mac_address="00:0A:14:85:54:A0",
        manufacturer="Teltonika",
        io_type="Ethernet+RS232",
        modem_subtype="4G",
        provider="Nova",
        extra_attrs={"comment": "site visit"},
        writer=w,
        dry_run=False,
    )
    attrs = {
        a["code"]: a["value"] for a in w.create_device.call_args.kwargs["attributes"]
    }
    assert attrs["mac_address"] == "00:0A:14:85:54:A0"
    assert attrs["manufacturer"] == "Teltonika"
    assert attrs["io_type"] == "Ethernet+RS232"
    assert attrs["subtype"] == "4G"  # modem_subtype → TOS `subtype`
    assert attrs["comment"] == "site visit"


def test_replace_sim_rejects_same_ip():
    """New IP == open SIM's IP → refuse (no duplicate sim_card churn)."""
    w = _telemetry_writer(
        old_child_id=5785,
        old_subtype="sim_card",
        old_attrs=[_attr("ip_address", "10.4.1.225")],
    )
    with pytest.raises(CfgOperationError, match="matches the SIM already"):
        replace_sim("GSIG", ip_address="10.4.1.225", writer=w, dry_run=False)
    w.create_device.assert_not_called()


def test_replace_modem_retires_old_before_creating_new():
    """Retire-first ordering: old modem moved out before the new one is joined."""
    w = _telemetry_writer(
        old_child_id=20771,
        old_subtype="modem_gsm",
        old_attrs=[_attr("serial_number", "6001254079")],
    )
    call_order = []
    w.move_device.side_effect = lambda *a, **k: (
        call_order.append("retire") or {"closed": "ok"}
    )
    w.create_device.side_effect = lambda *a, **k: (
        call_order.append("create") or {"id_entity": 22000}
    )
    replace_modem(
        "GSIG",
        new_serial="6001312345",
        new_model="Teltonika RUT241",
        writer=w,
        dry_run=False,
    )
    assert call_order == ["retire", "create"]


def test_replace_sim_retires_old_before_creating_new():
    """Retire-first ordering for SIM: old join closed before new SIM created."""
    w = _telemetry_writer(
        old_child_id=5785,
        old_subtype="sim_card",
        old_attrs=[_attr("ip_address", "10.4.1.225")],
    )
    call_order = []
    w.patch_entity_connection.side_effect = lambda *a, **k: (
        call_order.append("retire") or {"closed": "ok"}
    )
    w.create_device.side_effect = lambda *a, **k: (
        call_order.append("create") or {"id_entity": 22001}
    )
    replace_sim("GSIG", ip_address="10.4.1.240", writer=w, dry_run=False)
    assert call_order == ["retire", "create"]


# ---------------------------------------------------------------------------
# _parse_attr_pairs — generic --attr code=value escape hatch (CLI helper)
# ---------------------------------------------------------------------------


def test_parse_attr_pairs_basic():
    from receivers.cli.cfg import _parse_attr_pairs

    assert _parse_attr_pairs(["a=1", "b=2"]) == {"a": "1", "b": "2"}


def test_parse_attr_pairs_value_with_equals():
    from receivers.cli.cfg import _parse_attr_pairs

    # Only the first '=' is the separator (values may contain '=').
    assert _parse_attr_pairs(["io_type=A=B"]) == {"io_type": "A=B"}


def test_parse_attr_pairs_none_and_empty():
    from receivers.cli.cfg import _parse_attr_pairs

    assert _parse_attr_pairs(None) == {}
    assert _parse_attr_pairs([]) == {}


def test_parse_attr_pairs_rejects_missing_equals():
    import pytest as _pytest

    from receivers.cli.cfg import _parse_attr_pairs

    with _pytest.raises(ValueError, match="code=value"):
        _parse_attr_pairs(["noequals"])


def test_parse_attr_pairs_rejects_empty_code():
    import pytest as _pytest

    from receivers.cli.cfg import _parse_attr_pairs

    with _pytest.raises(ValueError, match="empty attribute code"):
        _parse_attr_pairs(["=value"])


# ---------------------------------------------------------------------------
# set_device_attribute — single attribute on an existing device, no swap
# ---------------------------------------------------------------------------


def _sim_writer(*, open_values=None, child_id=21582):
    """Telemetry writer mock with an open sim_card and configurable attr values."""
    w = _telemetry_writer(
        old_child_id=child_id,
        old_subtype="sim_card",
        old_attrs=[_attr("ip_address", "10.6.1.227")],
    )
    w.get_attribute_values.return_value = open_values if open_values is not None else []
    w.get_open_parent_join.return_value = {"time_from": "2026-06-24T12:00:00"}
    w.add_attribute_value.return_value = {"id_attribute_value": 5}
    w.upsert_attribute_value.return_value = {"id_attribute_value": 5}
    return w


def test_set_attr_adds_missing_attribute():
    """No open value → POST a new one dated from the device install."""
    w = _sim_writer(open_values=[])
    r = set_device_attribute(
        "RIFC",
        subtype="sim_card",
        code="phone_number",
        value="+3548372739",
        writer=w,
        dry_run=False,
    )
    w.add_attribute_value.assert_called_once_with(
        21582, "phone_number", "+3548372739", "2026-06-24T12:00:00"
    )
    w.upsert_attribute_value.assert_not_called()
    w.transition_attribute_value.assert_not_called()
    assert r.tos_changes["mode"] == "add"
    assert r.tos_changes["plan"]["old_value"] is None


def test_set_attr_explicit_date_overrides_install():
    """--date sets the new period's date_from instead of the install date."""
    w = _sim_writer(open_values=[])
    set_device_attribute(
        "RIFC",
        subtype="sim_card",
        code="phone_number",
        value="+3548372739",
        date="2026-06-20T12:00:00",
        writer=w,
        dry_run=False,
    )
    w.add_attribute_value.assert_called_once_with(
        21582, "phone_number", "+3548372739", "2026-06-20T12:00:00"
    )


def test_set_attr_noop_when_value_matches():
    """Open value already equals → no write."""
    w = _sim_writer(
        open_values=[
            {
                "value": "+3548372739",
                "date_to": None,
                "date_from": "2026-06-24T12:00:00",
                "id_attribute_value": 5,
            },
        ]
    )
    r = set_device_attribute(
        "RIFC",
        subtype="sim_card",
        code="phone_number",
        value="+3548372739",
        writer=w,
        dry_run=False,
    )
    assert r.tos_changes["mode"] == "noop"
    w.add_attribute_value.assert_not_called()
    w.upsert_attribute_value.assert_not_called()
    w.transition_attribute_value.assert_not_called()


def test_set_attr_differs_requires_intent():
    """Open value differs and no --change/--correct → refuse."""
    w = _sim_writer(
        open_values=[
            {
                "value": "+3540000000",
                "date_to": None,
                "date_from": "2026-06-24T12:00:00",
                "id_attribute_value": 5,
            },
        ]
    )
    with pytest.raises(CfgOperationError, match="--correct"):
        set_device_attribute(
            "RIFC",
            subtype="sim_card",
            code="phone_number",
            value="+3548372739",
            writer=w,
            dry_run=False,
        )


def test_set_attr_correct_patches_in_place():
    """--correct on a differing value → Pattern 1 upsert (no transition)."""
    w = _sim_writer(
        open_values=[
            {
                "value": "+3540000000",
                "date_to": None,
                "date_from": "2026-06-24T12:00:00",
                "id_attribute_value": 5,
            },
        ]
    )
    r = set_device_attribute(
        "RIFC",
        subtype="sim_card",
        code="phone_number",
        value="+3548372739",
        correct=True,
        writer=w,
        dry_run=False,
    )
    w.upsert_attribute_value.assert_called_once_with(
        21582, "phone_number", "+3548372739", "2026-06-24T12:00:00"
    )
    w.transition_attribute_value.assert_not_called()
    assert r.tos_changes["mode"] == "correct"


def test_set_attr_change_transitions():
    """--change on a differing value → Pattern 2 transition (records history)."""
    w = _sim_writer(
        open_values=[
            {
                "value": "+3540000000",
                "date_to": None,
                "date_from": "2026-06-24T12:00:00",
                "id_attribute_value": 5,
            },
        ]
    )
    r = set_device_attribute(
        "RIFC",
        subtype="sim_card",
        code="phone_number",
        value="+3548372739",
        change=True,
        date="2026-06-25",
        writer=w,
        dry_run=False,
    )
    assert w.transition_attribute_value.call_args.args[:3] == (
        21582,
        "phone_number",
        "+3548372739",
    )
    w.upsert_attribute_value.assert_not_called()
    assert r.tos_changes["mode"] == "change"


def test_set_attr_no_open_device_raises():
    """No open child of the subtype → clear error."""
    w = _telemetry_writer(old_child_id=None)
    with pytest.raises(CfgOperationError, match="No open sim_card"):
        set_device_attribute(
            "RIFC",
            subtype="sim_card",
            code="phone_number",
            value="+3548372739",
            writer=w,
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# archive_receiver_install_date — date-from-archive + multi-segment guard
# ---------------------------------------------------------------------------


def _seg(start: str, end: str, rtype: str, serial: str, fw=None):
    """Build a tostools ReceiverSegment for timeline fixtures."""
    from datetime import date as _date

    from tostools.receiver_timeline import ReceiverHeader, ReceiverSegment

    return ReceiverSegment(
        _date.fromisoformat(start),
        _date.fromisoformat(end),
        ReceiverHeader(serial=serial, rtype=rtype, firmware=fw),
    )


def _patch_timeline(monkeypatch, timeline):
    import tostools.receiver_timeline as rt

    monkeypatch.setattr(rt, "build_receiver_timeline", lambda *a, **k: timeline)


def _archive_writer(monkeypatch, *, open_receiver_id, cutoff):
    """Writer mock + patched _find_open_gnss_receiver_child for the cutoff."""
    import receivers.cfg.operations as ops

    w = _writer_mock(find_station_by_marker=4378)
    monkeypatch.setattr(
        ops, "_find_open_gnss_receiver_child", lambda writer, eid: open_receiver_id
    )
    if open_receiver_id is not None:
        w.get_open_parent_join.return_value = {"time_from": cutoff} if cutoff else None
    return w


def test_archive_install_date_refuses_multi_segment(monkeypatch):
    """KRIV's exact case: Trimble → interim PolaRX5 → current PolaRX5, none in TOS."""
    _patch_timeline(
        monkeypatch,
        [
            _seg("2009-10-21", "2021-03-02", "TRIMBLE NETRS", "4539258382"),
            _seg("2021-03-03", "2021-05-24", "SEPT POLARX5", "3235697", "5.4.0"),
            _seg("2021-05-25", "2026-06-25", "SEPT POLARX5", "3070340", "5.4.0"),
        ],
    )
    w = _archive_writer(
        monkeypatch, open_receiver_id=4976, cutoff="2009-10-21T00:00:00"
    )
    with pytest.raises(CfgOperationError, match="2 receiver changes not yet in TOS"):
        archive_receiver_install_date("KRIV", writer=w)


def test_archive_install_date_single_unrecorded_returns_date(monkeypatch):
    _patch_timeline(
        monkeypatch,
        [
            _seg("2009-10-21", "2021-05-24", "TRIMBLE NETRS", "4539258382"),
            _seg("2021-05-25", "2026-06-25", "SEPT POLARX5", "3070340", "5.4.0"),
        ],
    )
    w = _archive_writer(
        monkeypatch, open_receiver_id=4976, cutoff="2009-10-21T00:00:00"
    )
    r = archive_receiver_install_date("KRIV", writer=w)
    assert r.install_date == "2021-05-25T00:00:00"
    assert r.current_serial == "3070340"
    assert len(r.tail_units) == 1


def test_archive_install_date_coalesces_firmware_bump(monkeypatch):
    """A firmware bump on the SAME unit is not a receiver change."""
    _patch_timeline(
        monkeypatch,
        [
            _seg("2009-10-21", "2021-05-24", "TRIMBLE NETRS", "4539258382"),
            _seg("2021-05-25", "2023-01-01", "SEPT POLARX5", "3070340", "5.3.0"),
            _seg("2023-01-02", "2026-06-25", "SEPT POLARX5", "3070340", "5.4.0"),
        ],
    )
    w = _archive_writer(
        monkeypatch, open_receiver_id=4976, cutoff="2009-10-21T00:00:00"
    )
    r = archive_receiver_install_date("KRIV", writer=w)
    # install date back-coalesces the fw bump to the unit's first appearance
    assert r.install_date == "2021-05-25T00:00:00"
    assert len(r.tail_units) == 1


def test_archive_install_date_cutoff_excludes_recorded_units(monkeypatch):
    """If the interim unit is already in TOS (cutoff after it), only the current
    one is unrecorded → single swap is fine."""
    _patch_timeline(
        monkeypatch,
        [
            _seg("2009-10-21", "2021-03-02", "TRIMBLE NETRS", "4539258382"),
            _seg("2021-03-03", "2021-05-24", "SEPT POLARX5", "3235697", "5.4.0"),
            _seg("2021-05-25", "2026-06-25", "SEPT POLARX5", "3070340", "5.4.0"),
        ],
    )
    w = _archive_writer(
        monkeypatch, open_receiver_id=5001, cutoff="2021-03-03T00:00:00"
    )
    r = archive_receiver_install_date("KRIV", writer=w)
    assert r.install_date == "2021-05-25T00:00:00"
    assert len(r.tail_units) == 1


def test_archive_install_date_raises_on_empty_timeline(monkeypatch):
    _patch_timeline(monkeypatch, [])
    w = _archive_writer(monkeypatch, open_receiver_id=None, cutoff=None)
    with pytest.raises(CfgOperationError, match="No archived RINEX"):
        archive_receiver_install_date("KRIV", writer=w)


# ---------------------------------------------------------------------------
# move-device generalised beyond gnss_receiver
# ---------------------------------------------------------------------------


def _generic_move_writer(subtype="antenna", eid=4527):
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = None  # destination is a location
    w.find_location_by_name.return_value = 4  # B9
    w._request.return_value = {
        "id_entity": eid,
        "code_entity_subtype": subtype,
        "attributes": [
            {"code": "serial_number", "value": "262509", "date_to": None},
            {"code": "model", "value": "TRM29659.00", "date_to": None},
        ],
    }
    w.get_open_parent_join.return_value = None  # retired: no open parent
    w.move_device.return_value = {"closed": None, "opened": {}}
    return w


def test_move_device_by_id_moves_a_retired_antenna_to_the_warehouse():
    """The ISAK case: replace-antenna closed the join, so the old antenna is
    parentless — and TOS's fuzzy serial search can't find it. --id + --subtype
    is the exact route, and no verb should be hardcoded to receivers."""
    w = _generic_move_writer()
    res = move_device(
        subtype="antenna",
        id_entity=4527,
        to="B9 - Kjallari - Jörð",
        date="2026-07-30",
        writer=w,
        dry_run=True,
    )
    assert res.operation == "move"
    w.move_device.assert_called_once_with(4527, 4, "2026-07-30T12:00:00")
    # Serial back-filled from the entity for the summary line.
    assert res.serial == "262509"


def test_move_device_by_id_rejects_a_subtype_mismatch():
    w = _generic_move_writer(subtype="gnss_receiver")
    with pytest.raises(
        CfgOperationError, match="is a 'gnss_receiver', not a 'antenna'"
    ):
        move_device(subtype="antenna", id_entity=4527, to="B9", writer=w, dry_run=True)
    w.move_device.assert_not_called()


def test_move_device_rejects_both_selectors():
    w = _generic_move_writer()
    with pytest.raises(CfgOperationError, match="exactly one of --serial or --id"):
        move_device(
            "262509", subtype="antenna", id_entity=4527, to="B9", writer=w, dry_run=True
        )


def test_missing_serial_error_points_at_the_id_escape_hatch():
    """TOS's serial search can miss an existing device, so 'not found' must not
    dead-end the operator."""
    w = _generic_move_writer()
    w.find_device_by_serial.return_value = None
    with pytest.raises(CfgOperationError, match=r"--id <ID_ENTITY>"):
        move_device("262509", subtype="antenna", to="B9", writer=w, dry_run=True)


def test_non_receiver_move_to_location_does_not_clear_receiver_cfg(tmp_path):
    """An antenna leaving a station must not NONE the station's receiver_* keys."""
    w = _generic_move_writer()
    w.get_open_parent_join.return_value = {"id": 99, "id_entity_parent": 4346}
    res = move_device(
        subtype="antenna",
        id_entity=4527,
        to="B9 - Kjallari - Jörð",
        date="2026-07-30",
        writer=w,
        dry_run=False,
        cfg_path=tmp_path / "stations.cfg",
    )
    assert res.cfg_changes == {}


# ---------------------------------------------------------------------------
# The radome follows the antenna
# ---------------------------------------------------------------------------


def _paired_writer(radome_time_to="2026-07-30T12:00:00", extra_radomes=0):
    """Antenna 4527 + radome 5310 that shared an ISAK join closed at the same
    instant — the shape replace-antenna leaves behind."""
    RAD, STATION, B9 = 5310, 4346, 4
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = None
    w.find_location_by_name.return_value = B9
    w.get_open_parent_join.return_value = None
    w.move_device.return_value = {"closed": None, "opened": {}}

    children = [
        {"id_entity_child": RAD, "time_to": radome_time_to},
        {"id_entity_child": 9999, "time_to": "1999-01-01T00:00:00"},  # old radome
    ]
    for i in range(extra_radomes):
        children.append({"id_entity_child": 7000 + i, "time_to": radome_time_to})

    def _hist(eid):
        eid = int(eid)
        if eid == STATION:
            return {"children_connections": children}
        subtype = "radome" if eid in (RAD, 9999) or eid >= 7000 else "antenna"
        return {"code_entity_subtype": subtype, "attributes": []}

    w.get_entity_history.side_effect = _hist

    def _req(method, path, **kw):
        if "parent_history" in path:
            return [
                {
                    "id_entity_parent": STATION,
                    "time_from": "2002-01-09T00:00:00",
                    "time_to": "2026-07-30T12:00:00",
                }
            ]
        eid = int(path.rstrip("/").rsplit("/", 1)[-1])
        return {
            "id_entity": eid,
            "code_entity_subtype": _hist(eid)["code_entity_subtype"],
            "attributes": [
                {"code": "serial_number", "value": "262509", "date_to": None}
            ],
        }

    w._request.side_effect = _req
    return w


def test_moving_an_antenna_takes_its_radome_along():
    """They are screwed together — leaving the radome at the station it came
    off is a false record."""
    w = _paired_writer()
    res = move_device(
        subtype="antenna",
        id_entity=4527,
        to="B9 - Kjallari - Jörð",
        date="2026-07-30",
        writer=w,
        dry_run=True,
    )
    assert res.tos_changes["companion_radome"]["id_entity"] == 5310
    moved = {c.args[0] for c in w.move_device.call_args_list}
    assert moved == {4527, 5310}


def test_no_radome_moves_the_antenna_alone():
    w = _paired_writer()
    res = move_device(
        subtype="antenna",
        id_entity=4527,
        to="B9 - Kjallari - Jörð",
        date="2026-07-30",
        with_radome=False,
        writer=w,
        dry_run=True,
    )
    assert "companion_radome" not in res.tos_changes
    assert {c.args[0] for c in w.move_device.call_args_list} == {4527}


def test_ambiguous_radome_pairing_is_left_alone():
    """Two radomes closed at the same instant — guessing which travelled with
    the antenna would write an unfalsifiable location claim."""
    w = _paired_writer(extra_radomes=1)
    res = move_device(
        subtype="antenna", id_entity=4527, to="B9", date="2026-07-30", writer=w
    )
    assert "companion_radome" not in res.tos_changes
    assert {c.args[0] for c in w.move_device.call_args_list} == {4527}


def test_radome_with_a_different_join_period_is_not_a_companion():
    w = _paired_writer(radome_time_to="2019-01-01T00:00:00")
    res = move_device(
        subtype="antenna", id_entity=4527, to="B9", date="2026-07-30", writer=w
    )
    assert "companion_radome" not in res.tos_changes


def test_moving_a_receiver_never_looks_for_a_radome():
    """The companion lookup is antenna-only — a receiver move must not drag a
    radome that merely shares a close date."""
    w = _paired_writer()
    w.get_entity_history.side_effect = lambda eid: {
        "code_entity_subtype": "gnss_receiver",
        "attributes": [],
        "children_connections": [],
    }
    w._request.side_effect = lambda method, path, **kw: (
        []
        if "parent_history" in path
        else {
            "id_entity": 4527,
            "code_entity_subtype": "gnss_receiver",
            "attributes": [],
        }
    )
    move_device(
        subtype="gnss_receiver", id_entity=4527, to="B9", date="2026-07-30", writer=w
    )
    assert {c.args[0] for c in w.move_device.call_args_list} == {4527}
