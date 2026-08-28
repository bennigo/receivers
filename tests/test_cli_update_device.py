"""Tests for `receivers cfg update-device`.

Companion to cmd_cfg_update_device in src/receivers/cli/cfg.py — exercises
the probe → TOS-lookup → patch flow with mocked TOSWriter and probe_receiver.

Intent is mandatory: every invocation must pass exactly one of --change
(real-world change → transition, records history) or --correct (fix a wrong
record → in-place, no history).
"""

from __future__ import annotations

from datetime import date
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.device_probe import (
    ProbeUnreachableError,
    ReceiverIdentity,
)
from receivers.cli.arguments import create_argument_parser
from receivers.cli.cfg import cmd_cfg_update_device, firmware_to_software

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def parser():
    return create_argument_parser()


def _identity(**overrides) -> ReceiverIdentity:
    base: dict = dict(
        subtype="gnss_receiver",
        probe_type="polarx5",
        serial="SN_4101524",
        model_raw="PolaRx5",
        firmware_version="5.7.0",
        marker_name="HRAC",
        partial=False,
    )
    base.update(overrides)
    return ReceiverIdentity(**base)


def _make_writer_mock(
    *, device=None, current_value: Optional[str] = "5.6.0"
) -> MagicMock:
    """Build a TOSWriter mock.

    ``current_value`` is what get_attribute_values reports as the open period
    value — default '5.6.0' so an update to '5.7.0' is a real change (not a
    no-op). Pass current_value='5.7.0' to exercise the no-op guard, or None
    to simulate no existing open period.
    """
    writer = MagicMock()
    writer.find_device_by_serial.return_value = device or {"id_entity": 12345}
    writer.upsert_attribute_value.return_value = {"ok": True}
    writer.transition_attribute_value.return_value = {"ok": True}
    if current_value is None:
        writer.get_attribute_values.return_value = []
    else:
        writer.get_attribute_values.return_value = [
            {
                "id_attribute_value": 1,
                "code": "firmware_version",
                "value": current_value,
                "date_from": "2026-05-21",
                "date_to": None,
            }
        ]
    return writer


# ── intent is required + exclusive ──────────────────────────────────────────


def test_no_intent_flag_is_rejected(parser):
    """Neither --change nor --correct → argparse error (required mutex)."""
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "cfg",
                "update-device",
                "--probe",
                "192.168.3.1",
                "--field",
                "firmware_version",
            ]
        )


def test_change_and_correct_are_mutually_exclusive(parser):
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "cfg",
                "update-device",
                "--probe",
                "192.168.3.1",
                "--field",
                "firmware_version",
                "--change",
                "--correct",
            ]
        )


# ── --change (transition, history) ──────────────────────────────────────────


def test_change_dry_run_uses_transition(parser, capsys):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
        ]
    )
    writer = _make_writer_mock()  # current open value 5.6.0; probe reports 5.7.0
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.find_device_by_serial.assert_called_once_with("gnss_receiver", "SN_4101524")
    writer.transition_attribute_value.assert_called_once()
    writer.upsert_attribute_value.assert_not_called()
    call = writer.transition_attribute_value.call_args
    assert call.args[0] == 12345  # id_entity
    assert call.args[1] == "firmware_version"  # code
    assert call.args[2] == "5.7.0"  # new value
    assert call.args[3] == date.today().isoformat()  # transition date
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "--change" in out and "Pattern 2" in out
    assert "5.7.0" in out


def test_change_commit_uses_transition(parser, capsys):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" not in out
    assert "Pattern 2" in out
    writer.transition_attribute_value.assert_called_once()
    writer.upsert_attribute_value.assert_not_called()


def test_change_back_dated(parser):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--date",
            "2026-05-28",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.transition_attribute_value.assert_called_once_with(
        12345, "firmware_version", "5.7.0", "2026-05-28"
    )


# ── --correct (in-place, no history) ────────────────────────────────────────


def test_correct_uses_in_place_upsert(parser, capsys):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--correct",
            "--date",
            "2026-05-30",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.upsert_attribute_value.assert_called_once_with(
        12345, "firmware_version", "5.7.0", "2026-05-30"
    )
    writer.transition_attribute_value.assert_not_called()
    assert "Pattern 1" in capsys.readouterr().out


# ── no-op guard + edge cases ────────────────────────────────────────────────


def test_noop_when_value_unchanged(parser, capsys):
    """Probed value already matches the open TOS value → no write (either mode)."""
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock(current_value="5.7.0")  # already 5.7.0
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.transition_attribute_value.assert_not_called()
    writer.upsert_attribute_value.assert_not_called()
    assert "no change" in capsys.readouterr().out


def test_change_when_no_open_period_exists(parser):
    """No existing open period → still transitions (tostools falls back to add)."""
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock(current_value=None)  # no open period
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.transition_attribute_value.assert_called_once()


def test_multiple_fields_in_one_call(parser):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--field",
            "model",
            "--change",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    assert writer.transition_attribute_value.call_count == 2


# ── Error paths (all need a valid intent flag to reach the handler) ──────────


def test_no_field_supplied(parser, capsys):
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.3.1", "--change"]
    )
    rc = cmd_cfg_update_device(args)
    assert rc == 2
    assert "--field is required" in capsys.readouterr().err


def test_unsupported_field(parser, capsys):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "voltage",
            "--change",
        ]
    )
    with patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()):
        rc = cmd_cfg_update_device(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "voltage" in err and "not supported" in err


def test_probe_unreachable(parser, capsys):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.99.99",
            "--field",
            "firmware_version",
            "--change",
        ]
    )
    with patch(
        "receivers.cfg.device_probe.probe_receiver",
        side_effect=ProbeUnreachableError("no route to 192.168.99.99"),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 1
    assert "no route" in capsys.readouterr().err


def test_device_not_found_in_tos(parser, capsys):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
        ]
    )
    writer = _make_writer_mock()
    writer.find_device_by_serial.return_value = None  # missing
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no TOS device" in err
    assert "SN_4101524" in err
    assert "cfg add-receiver" in err  # helpful hint


def test_probe_returns_no_firmware(parser, capsys):
    """Probe succeeded but receiver reported no firmware_version — clear error."""
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
        ]
    )
    no_fw = _identity(firmware_version=None)
    with patch("receivers.cfg.device_probe.probe_receiver", return_value=no_fw):
        rc = cmd_cfg_update_device(args)
    assert rc == 1
    assert "firmware_version" in capsys.readouterr().err


# ── credential overrides ────────────────────────────────────────────────────


def test_cli_username_password_passed_to_probe(parser):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--username",
            "bench_user",
            "--password",
            "bench_pw",
        ]
    )
    writer = _make_writer_mock()
    mock_probe = MagicMock(return_value=_identity())
    with (
        patch("receivers.cfg.device_probe.probe_receiver", mock_probe),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    call_kwargs = mock_probe.call_args.kwargs
    assert call_kwargs.get("tcp_username") == "bench_user"
    assert call_kwargs.get("tcp_password") == "bench_pw"


def test_no_cli_creds_passes_none_to_probe(parser):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
        ]
    )
    writer = _make_writer_mock()
    mock_probe = MagicMock(return_value=_identity())
    with (
        patch("receivers.cfg.device_probe.probe_receiver", mock_probe),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    call_kwargs = mock_probe.call_args.kwargs
    assert call_kwargs.get("tcp_username") is None
    assert call_kwargs.get("tcp_password") is None


def test_probe_polarx5_applies_credential_overrides_to_extractor():
    """_probe_polarx5 must set tcp_username/tcp_password on the extractor."""
    from receivers.cfg.device_probe import _probe_polarx5

    fake_extractor = MagicMock()
    fake_extractor._query_receiver_setup.return_value = {
        "serial_number": "SN_X",
        "receiver_model": "PolaRx5",
        "firmware_version": "5.7.0",
        "marker_name": "BENCH",
    }
    with patch(
        "receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor",
        return_value=fake_extractor,
    ):
        ident = _probe_polarx5(
            "192.168.3.1",
            port=None,
            station_id_hint="BENCH",
            tcp_username="alt_user",
            tcp_password="alt_pw",
        )
    assert fake_extractor.tcp_username == "alt_user"
    assert fake_extractor.tcp_password == "alt_pw"
    assert ident.serial == "SN_X"


def test_probe_polarx5_without_overrides_leaves_extractor_creds_alone():
    """When no override is passed, _probe_polarx5 must not touch tcp_username/password
    on the extractor (so it keeps whatever it loaded from receivers.cfg)."""
    from receivers.cfg.device_probe import _probe_polarx5

    fake_extractor = MagicMock()
    fake_extractor.tcp_username = "fleet_user"
    fake_extractor.tcp_password = "fleet_pw"
    fake_extractor._query_receiver_setup.return_value = {
        "serial_number": "SN_X",
        "receiver_model": "PolaRx5",
        "firmware_version": "5.7.0",
        "marker_name": "BENCH",
    }
    with patch(
        "receivers.health.polarx5_tcp_extractor.PolaRX5TCPExtractor",
        return_value=fake_extractor,
    ):
        _probe_polarx5("192.168.3.1", port=None, station_id_hint="BENCH")
    assert fake_extractor.tcp_username == "fleet_user"
    assert fake_extractor.tcp_password == "fleet_pw"


# ── firmware → software_version conversion ──────────────────────────────────


def test_firmware_to_software_clean_three_part():
    assert firmware_to_software("5.7.0") == ("5.70", None)
    assert firmware_to_software("5.5.0") == ("5.50", None)
    assert firmware_to_software("4.10.2") == ("4.102", None)


def test_firmware_to_software_extra_parts():
    sw, warn = firmware_to_software("5.7.0.1")
    assert sw == "5.701"
    assert warn is None


def test_firmware_to_software_non_standard_passes_through_with_warning():
    for bad in ("5.7", "5", "5.7.0-rc1", ""):
        sw, warn = firmware_to_software(bad)
        assert sw == bad
        assert warn is not None


def test_software_version_field_derives_from_firmware(parser):
    """--field software_version writes the X.YZ form derived from probed fw."""
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "software_version",
            "--change",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock(current_value="5.50")  # old sw; probe fw=5.7.0 → 5.70
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.transition_attribute_value.assert_called_once()
    call = writer.transition_attribute_value.call_args
    assert call.args[1] == "software_version"
    assert call.args[2] == "5.70"  # derived, not the raw firmware


def test_firmware_and_software_in_one_call(parser):
    """Both fields update together when named explicitly."""
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--field",
            "software_version",
            "--change",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    codes = {c.args[1] for c in writer.transition_attribute_value.call_args_list}
    assert codes == {"firmware_version", "software_version"}


# ── the vitjun gate: --correct is NOT a field event ─────────────────────────


def test_correct_does_not_file_a_vitjun(parser):
    """Fixing a wrong RECORD must not log a site visit that never happened.

    `--change` is a real-world maintenance event and files a Fjarvitjun.
    `--correct` says the world did not change — only the recorded value was
    wrong — so a vitjun would fabricate a visit in TOS, the same class of
    damage as picking the wrong write pattern.

    Verified by mutation: dropping `not in_place` from the `want_vitjun`
    conjunction left all 23 existing tests green.
    """
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--correct",
            "--date",
            "2026-05-30",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
        patch("receivers.cli.cfg._create_update_vitjun") as vitjun,
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    vitjun.assert_not_called()


def test_change_does_file_a_vitjun(parser):
    """The other half of the same rule — otherwise the guard above is vacuous."""
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--date",
            "2026-05-30",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
        patch("receivers.cli.cfg._create_update_vitjun") as vitjun,
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    vitjun.assert_called_once()


def test_no_vitjun_flag_suppresses_it_even_for_a_change(parser):
    args = parser.parse_args(
        [
            "cfg",
            "update-device",
            "--probe",
            "192.168.3.1",
            "--field",
            "firmware_version",
            "--change",
            "--date",
            "2026-05-30",
            "--no-dry-run",
            "--no-vitjun",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
        patch("receivers.cli.cfg._create_update_vitjun") as vitjun,
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    vitjun.assert_not_called()
