"""`--serial default-<STID>` mints the fleet synthetic serial at warehouse intake.

A radome always, a steel fjórfótur monument likewise, and an antenna often has
no factory serial. At a station the verbs synthesise <subtype>-<STID>-<date>
from the station being installed at; warehouse intake has no station to build
that from, yet TOS still requires a non-empty serial_number and `move-device
--serial` matches the unit by it later.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import (
    CfgOperationError,
    add_monument,
    resolve_intake_serial,
)

WAREHOUSE = "B9 - Kjallari - Jörð"


@pytest.mark.parametrize(
    "subtype,expected",
    [
        ("monument", "monument-NPSK-20260805"),
        ("antenna", "antenna-NPSK-20260805"),
        ("radome", "radome-NPSK-20260805"),
    ],
)
def test_sentinel_mints_the_conventional_serial(subtype, expected):
    out, synthesised = resolve_intake_serial("default-NPSK", subtype, "2026-08-05")
    assert out == expected
    assert synthesised is True


def test_sentinel_is_case_insensitive_and_upcases_the_marker():
    out, _ = resolve_intake_serial("DEFAULT-npsk", "monument", "2026-08-05")
    assert out == "monument-NPSK-20260805"


def test_real_serial_passes_through_untouched():
    out, synthesised = resolve_intake_serial("1441052912", "antenna", "2026-08-05")
    assert out == "1441052912"
    assert synthesised is False


def test_none_and_empty_pass_through():
    assert resolve_intake_serial(None, "antenna", "2026-08-05") == (None, False)
    assert resolve_intake_serial("", "antenna", "2026-08-05") == ("", False)


def test_bare_prefix_is_an_error():
    with pytest.raises(CfgOperationError, match="name the destination station"):
        resolve_intake_serial("default-", "monument", "2026-08-05")


def test_full_iso_date_uses_only_the_date_part():
    out, _ = resolve_intake_serial("default-NPSK", "monument", "2026-08-05T12:00:00")
    assert out == "monument-NPSK-20260805"


# --- end to end through add_monument --------------------------------------


def test_warehouse_intake_accepts_the_sentinel():
    w = MagicMock()
    w.find_location_by_name.return_value = 900
    w.create_device.return_value = {"id_entity": 77}
    r = add_monument(
        w,
        warehouse=WAREHOUSE,
        serial="default-NPSK",
        date_start="2026-08-05",
        dry_run=False,
    )
    assert r.serial == "monument-NPSK-20260805"
    assert r.tos_changes["synthetic_serial"] is True
    # Joined to the warehouse, not the station it is named after.
    assert r.tos_changes["monument_join"]["parent"] == 900


def test_warehouse_still_refuses_a_bare_omitted_serial():
    w = MagicMock()
    with pytest.raises(CfgOperationError, match="--serial is required"):
        add_monument(w, warehouse=WAREHOUSE, dry_run=False)
    w.create_device.assert_not_called()


def test_the_minted_serial_is_what_move_device_would_match():
    """Intake and a later install must agree on the serial, or the move fails."""
    from tostools.device import synthetic_serial

    minted, _ = resolve_intake_serial("default-NPSK", "monument", "2026-08-05")
    assert minted == synthetic_serial("monument", "NPSK", "2026-08-05")
