"""Tests for ``cfg replace-antenna``, ``cfg replace-radome`` and ``cfg close-join``.

The antenna half of the RINEX header has the same swap semantics as the
receiver half, plus two constraints of its own:

* the **radome** is screwed onto the antenna, so ~95% of field swaps take both
  down and put both up — the default must therefore retire the old radome and
  create a new one carrying the same model, with ``keep_radome`` for the rarer
  re-fit of the same physical unit. The two remain independent TOS entities,
  and :func:`replace_radome` covers the radome-only swap;
* ``stations.cfg antenna_height`` is a **composite** (antenna ARP + monument
  offset) that TOS splits across two entities — deriving it from an absent
  monument would silently write a height short by the monument offset, which
  biases every downstream position without erroring anywhere.

All exercised below with a mocked :class:`TOSWriter` (no network).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import (
    CfgOperationError,
    close_join,
    replace_antenna,
    replace_radome,
)

ISAK_EID = 19000
OLD_ANT_ID = 21000
OLD_RADOME_ID = 21001
MONUMENT_ID = 21002


def _writer(
    *,
    children=(("antenna", OLD_ANT_ID), ("monument", MONUMENT_ID)),
    old_serial="262509",
    old_model="TRM29659.00",
    old_radome_model="SCIS",
    monument_height="0.9964",
):
    """TOSWriter-shaped mock for a station with the given open children.

    ``children`` is a sequence of ``(subtype, id_entity)`` pairs, each joined
    to the station with an OPEN join (``time_to=None``).
    """
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = ISAK_EID

    station = {
        "children_connections": [
            {"id_entity_child": eid, "time_to": None} for _sub, eid in children
        ],
        "attributes": [],
    }
    by_id = {eid: sub for sub, eid in children}

    def _hist(eid):
        eid = int(eid)
        if eid == ISAK_EID:
            return station
        subtype = by_id.get(eid)
        attrs = []
        if subtype == "antenna":
            attrs = [
                {"code": "serial_number", "value": old_serial, "date_to": None},
                {"code": "model", "value": old_model, "date_to": None},
            ]
        elif subtype == "radome" and old_radome_model is not None:
            attrs = [{"code": "model", "value": old_radome_model, "date_to": None}]
        elif subtype == "monument" and monument_height is not None:
            attrs = [
                {"code": "monument_height", "value": monument_height, "date_to": None}
            ]
        return {"code_entity_subtype": subtype, "attributes": attrs}

    w.get_entity_history.side_effect = _hist
    w.find_device_by_serial.return_value = None
    w.create_device.side_effect = lambda *a, **k: {"id_entity": 50001}
    w.create_entity_connection.return_value = {"id_connection": 7}
    w.get_open_parent_join.return_value = {"id": 27836}
    w.add_maintenance_visit.return_value = {"id_maintenance": 5150}
    return w


# ---------------------------------------------------------------------------
# replace_antenna — guards
# ---------------------------------------------------------------------------


def test_missing_antenna_height_raises_before_any_write():
    w = _writer()
    with pytest.raises(CfgOperationError, match="--antenna-height is required"):
        replace_antenna("ISAK", new_model="TRM115000.10", new_serial="144", writer=w)
    w.create_device.assert_not_called()
    w.patch_entity_connection.assert_not_called()


def test_no_open_antenna_points_at_add_antenna():
    w = _writer(children=(("monument", MONUMENT_ID),))
    with pytest.raises(CfgOperationError, match="add-antenna"):
        replace_antenna(
            "ISAK", new_model="TRM115000.10", antenna_height="0.0083", writer=w
        )
    w.create_device.assert_not_called()


def test_two_open_antennas_refuses_and_points_at_close_join():
    w = _writer(children=(("antenna", OLD_ANT_ID), ("antenna", 21099)))
    with pytest.raises(CfgOperationError, match="close-join"):
        replace_antenna(
            "ISAK", new_model="TRM115000.10", antenna_height="0.0083", writer=w
        )
    w.create_device.assert_not_called()


def test_same_serial_as_open_antenna_rejected():
    w = _writer()
    with pytest.raises(CfgOperationError, match="Did the swap actually happen"):
        replace_antenna(
            "ISAK",
            new_model="TRM115000.10",
            new_serial="262509",  # == the open antenna's serial
            antenna_height="0.0083",
            writer=w,
        )
    w.create_device.assert_not_called()


def test_bad_radome_code_rejected_before_the_old_join_is_closed():
    """Both models validate up front — a late ValueError would leave the
    station half-swapped (old antenna retired, new one never created)."""
    w = _writer()
    with pytest.raises(ValueError, match="Unknown radome model"):
        replace_antenna(
            "ISAK",
            new_model="TRM115000.10",
            new_serial="144",
            radome="BOGUS",
            antenna_height="0.0083",
            writer=w,
        )
    w.patch_entity_connection.assert_not_called()
    w.move_device.assert_not_called()
    w.create_device.assert_not_called()


def test_no_monument_height_raises_rather_than_writing_bare_arp():
    """The composite guard: cfg antenna_height = ARP + monument offset."""
    w = _writer(children=(("antenna", OLD_ANT_ID),))  # no monument child
    with pytest.raises(CfgOperationError, match="composite"):
        replace_antenna(
            "ISAK",
            new_model="TRM115000.10",
            new_serial="144",
            antenna_height="0.0083",
            dry_run=False,
            writer=w,
        )
    # Aborts with TOS untouched — not half-swapped.
    w.create_device.assert_not_called()
    w.patch_entity_connection.assert_not_called()


def test_cfg_antenna_height_override_bypasses_monument_lookup():
    w = _writer(children=(("antenna", OLD_ANT_ID),))
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        cfg_antenna_height="1.0047",
        date="2026-07-30",
        writer=w,
    )
    assert res.operation == "replace-antenna"


def test_no_cfg_skips_the_composite_requirement():
    w = _writer(children=(("antenna", OLD_ANT_ID),))
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        skip_cfg=True,
        writer=w,
    )
    assert res.serial == "144"


# ---------------------------------------------------------------------------
# replace_antenna — behaviour
# ---------------------------------------------------------------------------


def test_retires_old_antenna_before_creating_the_new_one():
    """Never two open antennas at once — that state is what breaks
    current_session(), station.info and every RINEX header."""
    w = _writer()
    calls = []
    w.patch_entity_connection.side_effect = lambda *a, **k: calls.append("close")
    w.create_device.side_effect = lambda *a, **k: (
        calls.append("create") or {"id_entity": 50001}
    )
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        date="2026-07-30",
        writer=w,
    )
    assert calls == ["close", "create"], calls
    # Closed at the swap instant (bare date → noon, the field-work convention).
    w.patch_entity_connection.assert_called_once_with(
        27836, time_to="2026-07-30T12:00:00"
    )


def test_radome_omitted_carries_the_model_forward_as_a_new_unit():
    """The 95% case: antenna and radome come down and go up together, the new
    radome being the same type. Old join closed, fresh device created."""
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        date="2026-07-30",
        writer=w,
    )
    assert "retire_old_radome" in res.tos_changes
    assert res.tos_changes["new_radome_serial"] == "radome-ISAK-20260730"
    assert [c.kwargs.get("entity_subtype") for c in w.create_device.call_args_list] == [
        "radome",
        "antenna",
    ]
    # The inferred model is named in the plan so a dry-run shows the decision.
    assert res.tos_changes["plan"]["old_radome_model"] == "SCIS"
    assert "SCIS" in res.tos_changes["plan"]["radome"]
    assert "carried forward" in res.tos_changes["plan"]["radome"]


def test_keep_radome_leaves_the_entity_and_join_untouched():
    """The 5% case: the same physical radome unscrewed and re-fitted."""
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        keep_radome=True,
        writer=w,
    )
    assert "retire_old_radome" not in res.tos_changes
    assert "new_radome_create" not in res.tos_changes
    assert [c.kwargs.get("entity_subtype") for c in w.create_device.call_args_list] == [
        "antenna"
    ]
    assert res.tos_changes["plan"]["radome"].startswith("kept")


def test_radome_and_keep_radome_are_mutually_exclusive():
    w = _writer()
    with pytest.raises(CfgOperationError, match="mutually"):
        replace_antenna(
            "ISAK",
            new_model="TRM115000.10",
            new_serial="144",
            antenna_height="0.0083",
            radome="SNOW",
            keep_radome=True,
            writer=w,
        )
    w.create_device.assert_not_called()


def test_no_open_radome_and_flag_omitted_creates_nothing():
    """A station that never had a radome keeps not having one."""
    w = _writer(children=(("antenna", OLD_ANT_ID), ("monument", MONUMENT_ID)))
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        writer=w,
    )
    assert "retire_old_radome" not in res.tos_changes
    assert "new_radome_create" not in res.tos_changes
    assert (
        res.tos_changes["plan"]["radome"]
        == "none in TOS and no --radome — cfg left as-is"
    )


def test_explicit_radome_retires_the_old_and_creates_a_new_one():
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        radome="SNOW",
        antenna_height="0.0083",
        date="2026-07-30",
        writer=w,
    )
    assert "retire_old_radome" in res.tos_changes
    assert res.tos_changes["new_radome_serial"] == "radome-ISAK-20260730"
    assert [c.kwargs.get("entity_subtype") for c in w.create_device.call_args_list] == [
        "radome",
        "antenna",
    ]


def test_radome_none_retires_the_old_and_creates_nothing():
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        radome="NONE",
        antenna_height="0.0083",
        writer=w,
    )
    assert "retire_old_radome" in res.tos_changes
    assert "new_radome_create" not in res.tos_changes


def test_vitjun_text_names_both_units():
    w = _writer()
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        participants="bgo@vedur.is",
        writer=w,
    )
    work = w.add_maintenance_visit.call_args.kwargs["work"]
    assert "Skipt um loftnet" in work
    assert "TRM29659.00 262509" in work
    assert "TRM115000.10 144" in work
    assert res.vitjun_id == 5150


def test_synthetic_serial_when_new_serial_unknown():
    w = _writer()
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        antenna_height="0.0083",
        date="2026-07-30",
        writer=w,
    )
    assert res.serial == "antenna-ISAK-20260730"
    assert res.tos_changes["plan"]["synthetic_serial"] is True


def test_old_antenna_left_parentless_unless_warehouse_given():
    """A retired antenna is as often scrapped as returned — no reparent by
    default, because a wrong location claim is indistinguishable from a real
    one after the fact."""
    w = _writer()
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        writer=w,
    )
    # move_device is the reparent call; only the join close should have run.
    w.move_device.assert_not_called()
    w.patch_entity_connection.assert_called_once()


def test_warehouse_reparents_the_old_antenna():
    w = _writer()
    w.find_location_by_name.return_value = 9001
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        date="2026-07-30",
        warehouse="B9 - Kjallari - Jörð",
        writer=w,
    )
    w.move_device.assert_any_call(OLD_ANT_ID, 9001, "2026-07-30T12:00:00")


def test_cfg_updates_composite_height_and_rinex_valid_from(tmp_path, monkeypatch):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text(
        "[ISAK]\n"
        "antenna_serial = 262509\n"
        "antenna_type = TRM29659.00\n"
        "antenna_radome = SCIS\n"
        "antenna_height = 1.0047\n"
        "rinex_config_valid_from = 2014-02-22\n",
        encoding="utf-8",
    )
    w = _writer()
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.1000",
        date="2026-07-30",
        dry_run=False,
        writer=w,
        cfg_path=cfg,
    )
    # 0.1000 (new ARP) + 0.9964 (monument offset) = 1.0964 — the composite,
    # not the bare ARP. Differs from the old 1.0047 so the write is observable.
    assert res.cfg_changes["antenna_height"] == "1.0964"
    assert res.cfg_changes["antenna_type"] == "TRM115000.10"
    assert res.cfg_changes["antenna_serial"] == "144"
    # Swap at noon splits the day → first full day on the new antenna is the next.
    assert res.cfg_changes["rinex_config_valid_from"] == "2026-07-31"
    # This fixture has no radome in TOS and no --radome flag → the run made no
    # radome decision, so cfg's SCIS must survive rather than be clobbered to
    # NONE on the strength of a TOS gap.
    assert "antenna_radome" not in res.cfg_changes
    assert "antenna_radome = SCIS" in cfg.read_text(encoding="utf-8")


def test_cfg_antenna_radome_untouched_under_keep_radome(tmp_path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text(
        "[ISAK]\nantenna_radome = SCIS\nantenna_height = 1.0047\n", encoding="utf-8"
    )
    w = _writer()
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.1000",
        keep_radome=True,
        dry_run=False,
        writer=w,
        cfg_path=cfg,
    )
    assert "antenna_radome" not in res.cfg_changes
    assert "antenna_radome = SCIS" in cfg.read_text(encoding="utf-8")


def test_cfg_antenna_radome_written_none_when_radome_removed(tmp_path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text(
        "[ISAK]\nantenna_radome = SCIS\nantenna_height = 1.0047\n", encoding="utf-8"
    )
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.1000",
        radome="NONE",
        dry_run=False,
        writer=w,
        cfg_path=cfg,
    )
    assert res.cfg_changes["antenna_radome"] == "NONE"


# ---------------------------------------------------------------------------
# close_join
# ---------------------------------------------------------------------------


def test_close_join_by_station_and_subtype():
    w = _writer()
    res = close_join(station_id="ISAK", subtype="antenna", date="2026-07-30", writer=w)
    assert res.operation == "close-join"
    assert res.serial == "262509"
    assert res.tos_changes["id_connection"] == 27836
    w.patch_entity_connection.assert_called_once_with(
        27836, time_to="2026-07-30T12:00:00"
    )


def test_close_join_by_id_patches_directly():
    w = _writer()
    res = close_join(id_connection=27836, date="2026-07-30", writer=w)
    assert res.tos_changes["id_connection"] == 27836
    w.patch_entity_connection.assert_called_once_with(
        27836, time_to="2026-07-30T12:00:00"
    )
    # Raw-id mode identifies a join, not a station — no station lookup at all.
    w.find_station_by_marker.assert_not_called()


def test_close_join_requires_exactly_one_selector():
    w = _writer()
    with pytest.raises(CfgOperationError, match="not both, not neither"):
        close_join(id_connection=1, station_id="ISAK", subtype="antenna", writer=w)
    with pytest.raises(CfgOperationError, match="not both, not neither"):
        close_join(writer=w)


def test_close_join_station_requires_subtype():
    w = _writer()
    with pytest.raises(CfgOperationError, match="requires subtype"):
        close_join(station_id="ISAK", writer=w)


def test_close_join_id_rejects_warehouse():
    w = _writer()
    with pytest.raises(CfgOperationError, match="needs a device to reparent"):
        close_join(id_connection=1, warehouse="B9 - Kjallari - Jörð", writer=w)


def test_close_join_no_open_child_raises():
    w = _writer(children=(("monument", MONUMENT_ID),))
    with pytest.raises(CfgOperationError, match="nothing to close"):
        close_join(station_id="ISAK", subtype="antenna", writer=w)


def test_close_join_multiple_open_children_refuses():
    w = _writer(children=(("antenna", OLD_ANT_ID), ("antenna", 21099)))
    with pytest.raises(CfgOperationError, match="2 open antenna children"):
        close_join(station_id="ISAK", subtype="antenna", writer=w)


def test_close_join_warehouse_reparents():
    w = _writer()
    w.find_location_by_name.return_value = 9001
    close_join(
        station_id="ISAK",
        subtype="antenna",
        date="2026-07-30",
        warehouse="B9 - Kjallari - Jörð",
        writer=w,
    )
    w.move_device.assert_called_once_with(OLD_ANT_ID, 9001, "2026-07-30T12:00:00")
    w.patch_entity_connection.assert_not_called()


# ---------------------------------------------------------------------------
# replace_radome — the radome-only swap (antenna stays)
# ---------------------------------------------------------------------------

_RADOME_ONLY = (("antenna", OLD_ANT_ID), ("radome", OLD_RADOME_ID))


def test_replace_radome_retires_old_then_creates_new():
    w = _writer(children=_RADOME_ONLY)
    calls = []
    w.patch_entity_connection.side_effect = lambda *a, **k: calls.append("close")
    w.create_device.side_effect = lambda *a, **k: (
        calls.append("create") or {"id_entity": 50002}
    )
    res = replace_radome("ISAK", new_model="SNOW", date="2026-07-30", writer=w)
    assert res.operation == "replace-radome"
    assert calls == ["close", "create"], calls
    assert res.tos_changes["plan"] == {
        "old_model": "SCIS",
        "new_model": "SNOW",
        "new_serial": "radome-ISAK-20260730",
    }
    # The antenna is not touched at all.
    assert [c.kwargs.get("entity_subtype") for c in w.create_device.call_args_list] == [
        "radome"
    ]


def test_replace_radome_bad_model_rejected_before_any_write():
    w = _writer(children=_RADOME_ONLY)
    with pytest.raises(ValueError, match="Unknown radome model"):
        replace_radome("ISAK", new_model="BOGUS", writer=w)
    w.patch_entity_connection.assert_not_called()
    w.create_device.assert_not_called()


def test_replace_radome_with_no_open_radome_still_fits_one():
    """'A radome was fitted where there wasn't one' — mirrors replace_modem."""
    w = _writer(children=(("antenna", OLD_ANT_ID),))
    res = replace_radome("ISAK", new_model="SCIS", date="2026-07-30", writer=w)
    assert "retire_old_radome" not in res.tos_changes
    assert "new_radome_create" in res.tos_changes
    assert res.tos_changes["plan"]["old_model"] is None
    work = w.add_maintenance_visit.call_args.kwargs["work"]
    assert work == "Sett upp raðhlíf: SCIS"


def test_replace_radome_none_removes_without_creating():
    w = _writer(children=_RADOME_ONLY)
    res = replace_radome("ISAK", new_model="NONE", date="2026-07-30", writer=w)
    assert "retire_old_radome" in res.tos_changes
    assert "new_radome_create" not in res.tos_changes
    assert res.serial is None
    assert w.add_maintenance_visit.call_args.kwargs["work"] == "Raðhlíf fjarlægð: SCIS"


def test_replace_radome_vitjun_names_both_models():
    w = _writer(children=_RADOME_ONLY)
    replace_radome("ISAK", new_model="SNOW", participants="bgo@vedur.is", writer=w)
    assert (
        w.add_maintenance_visit.call_args.kwargs["work"]
        == "Skipt um raðhlíf: SCIS → SNOW"
    )


def test_replace_radome_refuses_two_open_radomes():
    w = _writer(children=(("radome", OLD_RADOME_ID), ("radome", 21098)))
    with pytest.raises(CfgOperationError, match="2 open radome children"):
        replace_radome("ISAK", new_model="SCIS", writer=w)
    w.create_device.assert_not_called()


def test_replace_radome_writes_only_antenna_radome_to_cfg(tmp_path):
    """A radome carries no ARP offset — antenna_height must not be recomputed."""
    cfg = tmp_path / "stations.cfg"
    cfg.write_text(
        "[ISAK]\nantenna_radome = SCIS\nantenna_height = 1.0047\n", encoding="utf-8"
    )
    w = _writer(children=_RADOME_ONLY)
    res = replace_radome(
        "ISAK", new_model="SNOW", dry_run=False, writer=w, cfg_path=cfg
    )
    assert res.cfg_changes == {"antenna_radome": "SNOW"}
    assert "antenna_height = 1.0047" in cfg.read_text(encoding="utf-8")


def test_replace_radome_warehouse_reparents_the_old_unit():
    w = _writer(children=_RADOME_ONLY)
    w.find_location_by_name.return_value = 9001
    replace_radome(
        "ISAK",
        new_model="SNOW",
        date="2026-07-30",
        warehouse="B9 - Kjallari - Jörð",
        writer=w,
    )
    w.move_device.assert_called_once_with(OLD_RADOME_ID, 9001, "2026-07-30T12:00:00")


def test_vitjun_records_the_radome_swap_too():
    """The radome went up in the same visit — the vitjun is the record of what
    happened on the mast, so it must say so."""
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        writer=w,
    )
    work = w.add_maintenance_visit.call_args.kwargs["work"]
    assert work == (
        "Skipt um loftnet: TRM29659.00 262509 → TRM115000.10 144 "
        "(skipt um raðhlíf: SCIS → SCIS)"
    )


def test_vitjun_notes_a_re_fitted_radome():
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        keep_radome=True,
        writer=w,
    )
    assert w.add_maintenance_visit.call_args.kwargs["work"].endswith(
        "(sama raðhlíf sett aftur á)"
    )


def test_explicit_vitjun_text_is_never_decorated():
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        antenna_height="0.0083",
        vitjun="Mín eigin lýsing",
        writer=w,
    )
    assert w.add_maintenance_visit.call_args.kwargs["work"] == "Mín eigin lýsing"


# ---------------------------------------------------------------------------
# --list-models — the offline "what may I type?" lookup
# ---------------------------------------------------------------------------


def _run_cli(argv):
    """Run `receivers cfg ...` through the real parser; return (rc, stdout)."""
    import argparse
    import contextlib
    import io

    from receivers.cli.cfg import create_cfg_parser

    parser = argparse.ArgumentParser(prog="receivers")
    create_cfg_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return rc, buf.getvalue()


def test_list_models_needs_no_other_flags_and_makes_no_tos_call(monkeypatch):
    """The model tables are the one thing you must get EXACTLY right, and the
    verbs resolve the station against TOS before validating the model — so the
    lookup has to work with no station, no credentials, no network."""
    import receivers.cfg.operations as ops

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("--list-models must not construct a TOSWriter")

    monkeypatch.setattr(ops, "TOSWriter", _boom)
    rc, out = _run_cli(["cfg", "replace-antenna", "--list-models"])
    assert rc == 0
    assert "TRM29659.00" in out
    assert "SEPPOLANT_X_MF" in out
    assert "SCIS" in out


def test_list_models_on_replace_radome_shows_radomes_only():
    rc, out = _run_cli(["cfg", "replace-radome", "--list-models"])
    assert rc == 0
    assert "SCIS" in out and "LEIT" in out
    # An antenna model would be noise on a radome-only verb.
    assert "TRM29659.00" not in out


def test_list_models_on_add_antenna_shows_both_tables():
    rc, out = _run_cli(["cfg", "add-antenna", "--list-models"])
    assert rc == 0
    assert "TRM29659.00" in out
    assert "SCIS" in out


def test_list_models_output_names_every_accepted_antenna():
    """Guards against the list and the validator drifting apart."""
    from tostools.standards.igs_equipment import ANTENNA_IGS

    _rc, out = _run_cli(["cfg", "replace-antenna", "--list-models"])
    for igs in set(ANTENNA_IGS.values()):
        assert igs in out, f"{igs} missing from --list-models output"


def test_missing_required_flags_report_real_flag_spellings(capsys):
    """`new_model` is the dest but `--model` is what the operator types."""
    rc, _out = _run_cli(["cfg", "replace-antenna", "--station", "ISAK"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--model" in err and "--antenna-height" in err
    assert "new_model" not in err


def test_radome_serial_overrides_the_synthetic_placeholder():
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        radome="SCIS",
        radome_serial="RAD-9911",
        antenna_height="0.0083",
        date="2026-07-30",
        writer=w,
    )
    assert res.tos_changes["new_radome_serial"] == "RAD-9911"


def test_radome_serial_still_applies_to_a_carried_forward_radome():
    w = _writer(
        children=(
            ("antenna", OLD_ANT_ID),
            ("radome", OLD_RADOME_ID),
            ("monument", MONUMENT_ID),
        )
    )
    res = replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="144",
        radome_serial="RAD-9911",
        antenna_height="0.0083",
        writer=w,
    )
    assert res.tos_changes["new_radome_serial"] == "RAD-9911"


def test_radome_serial_rejected_under_keep_radome():
    w = _writer()
    with pytest.raises(CfgOperationError, match="nothing to name"):
        replace_antenna(
            "ISAK",
            new_model="TRM115000.10",
            new_serial="144",
            antenna_height="0.0083",
            keep_radome=True,
            radome_serial="RAD-9911",
            writer=w,
        )
    w.create_device.assert_not_called()


def test_arp_height_pinned_even_when_the_antenna_was_pre_warehoused():
    """Warehouse intake (`tos device add --subtype antenna`) writes no
    antenna_height, and _create_and_join_device leaves a reused device's
    attributes alone — so the height must be pinned explicitly or --antenna-height
    is silently dropped for exactly the antennas that went through intake."""
    w = _writer()
    # Antenna already exists in TOS (warehoused) → reuse + reparent path.
    w.find_device_by_serial.side_effect = lambda sub, ser: (
        {"id_entity": 60001} if sub == "antenna" else None
    )
    replace_antenna(
        "ISAK",
        new_model="TRM115000.10",
        new_serial="2505010005",
        antenna_height="0.0",
        date="2026-07-30",
        writer=w,
    )
    # Reparented from the warehouse rather than recreated...
    w.move_device.assert_any_call(60001, ISAK_EID, "2026-07-30T12:00:00")
    # ...and the ARP height still landed on it.
    w.upsert_attribute_value.assert_called_once_with(
        60001, "antenna_height", "0.0", "2026-07-30T12:00:00"
    )


# ---------------------------------------------------------------------------
# --warehouse: three distinguishable states
# ---------------------------------------------------------------------------


def _parse_cfg(argv):
    import argparse

    from receivers.cli.cfg import create_cfg_parser

    parser = argparse.ArgumentParser(prog="receivers")
    create_cfg_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


_RA_BASE = [
    "cfg",
    "replace-antenna",
    "--station",
    "ISAK",
    "--model",
    "SEPVC6150L",
    "--antenna-height",
    "0.0",
]


def test_warehouse_absent_means_do_not_reparent():
    from receivers.cli.cfg import _resolve_warehouse_arg

    assert _resolve_warehouse_arg(_parse_cfg(_RA_BASE).warehouse) is None


def test_bare_warehouse_flag_resolves_to_the_fleet_default():
    """Nobody should have to retype "B9 - Kjallari - Jörð" (accents and all)
    to reach the default warehouse."""
    from receivers.cfg.operations import DEFAULT_WAREHOUSE
    from receivers.cli.cfg import _resolve_warehouse_arg

    args = _parse_cfg(_RA_BASE + ["--warehouse"])
    assert args.warehouse == ""  # sentinel, distinct from absent (None)
    assert _resolve_warehouse_arg(args.warehouse) == DEFAULT_WAREHOUSE


def test_explicit_warehouse_name_is_passed_through():
    from receivers.cli.cfg import _resolve_warehouse_arg

    args = _parse_cfg(_RA_BASE + ["--warehouse", "Reykjavík - tæknibílskúr"])
    assert _resolve_warehouse_arg(args.warehouse) == "Reykjavík - tæknibílskúr"


def test_bare_warehouse_works_on_replace_radome_and_close_join():
    from receivers.cfg.operations import DEFAULT_WAREHOUSE
    from receivers.cli.cfg import _resolve_warehouse_arg

    rr = _parse_cfg(
        ["cfg", "replace-radome", "--station", "ISAK", "--model", "SCIS", "--warehouse"]
    )
    cj = _parse_cfg(
        [
            "cfg",
            "close-join",
            "--station",
            "ISAK",
            "--subtype",
            "antenna",
            "--warehouse",
        ]
    )
    assert _resolve_warehouse_arg(rr.warehouse) == DEFAULT_WAREHOUSE
    assert _resolve_warehouse_arg(cj.warehouse) == DEFAULT_WAREHOUSE
