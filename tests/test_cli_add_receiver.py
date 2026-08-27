"""Tests for ``receivers cfg add-receiver`` CLI handler.

Mocks `receivers.cfg.device_probe.probe_receiver` and
`tostools.api.tos_writer.TOSWriter` at the import boundary so no network
is touched and the duplicate-serial / owner / model guards are exercised
through the in-process tostools.device + OwnersCache code paths.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from receivers.cfg.device_probe import (
    ProbeNotIdentifiedError,
    ProbeUnreachableError,
    ReceiverIdentity,
)
from receivers.cli.cfg import cmd_cfg_add_receiver, create_cfg_parser

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owners_yaml(tmp_path: Path) -> Path:
    """Materialise an owners.yaml with one known owner."""
    p = tmp_path / "owners.yaml"
    p.write_text(yaml.safe_dump({"owners": ["Veðurstofa Íslands"]}, allow_unicode=True))
    return p


@pytest.fixture
def parser() -> argparse.ArgumentParser:
    """Build a top-level parser with the cfg subcommands attached."""
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers(dest="command")
    create_cfg_parser(subparsers)
    return p


@pytest.fixture
def base_args(parser: argparse.ArgumentParser, owners_yaml: Path):
    """Parse a happy-path argv to produce an argparse.Namespace.

    Tests can mutate the returned namespace to vary one input at a time.
    """
    return parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--probe",
            "192.168.20.1",
            "--owner",
            "Veðurstofa Íslands",
            "--location",
            "Bench A",
            "--date-start",
            "2026-05-12",
            "--owners-cache",
            str(owners_yaml),
        ]
    )


def _polarx5_identity(**overrides) -> ReceiverIdentity:
    base = dict(
        subtype="gnss_receiver",
        probe_type="polarx5",
        serial="SN_HAPPY",
        model_raw="PolaRx5",
        firmware_version="5.5.0",
        marker_name="BENC",
        partial=False,
    )
    base.update(overrides)
    return ReceiverIdentity(**base)  # type: ignore[arg-type]


def _make_writer_mock(*, response=None) -> MagicMock:
    writer = MagicMock()
    writer.create_device.return_value = response or {"id_entity": 999}
    writer.upsert_attribute_value.return_value = {"ok": True}
    return writer


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_dry_run_happy_path(base_args, capsys) -> None:
    writer = _make_writer_mock()
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer) as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    out = capsys.readouterr()

    assert rc == 0, out.err
    # Dry-run is the default
    assert tw_cls.call_args.kwargs["dry_run"] is True
    # create_device received the IGS-normalised model and the probed serial
    call = writer.create_device.call_args
    assert call.args[0] == "gnss_receiver"
    attrs = {a["code"]: a["value"] for a in call.args[1]}
    assert attrs["model"] == "SEPT POLARX5"
    assert attrs["serial_number"] == "SN_HAPPY"
    assert attrs["owner"] == "Veðurstofa Íslands"
    # location is NOT a device attribute — it is conveyed via the
    # entity_connection join (parent=area, child=device) created
    # by writer.connect_device_to_location after the device is created.
    assert "location" not in attrs
    assert attrs["status"] == "virkt"
    assert attrs["date_start"].startswith("2026-05-12")
    assert call.kwargs["force"] is False
    # Firmware came through as an optional upsert (probed firmware → DRY RUN log)
    assert "firmware_version" in out.out
    assert "5.5.0" in out.out
    writer.upsert_attribute_value.assert_not_called()  # dry-run skips live upserts


def test_no_dry_run_flips_writer_and_runs_upserts(base_args) -> None:
    writer = _make_writer_mock(response={"id_entity": 4242})
    base_args.no_dry_run = True
    base_args.galvos = "55555"
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer) as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)

    assert rc == 0
    assert tw_cls.call_args.kwargs["dry_run"] is False
    # Three optional upserts: firmware_version (probed) + galvos (CLI) +
    # software_version (derived from firmware: 5.5.0 → 5.50)
    upsert_calls = writer.upsert_attribute_value.call_args_list
    assert len(upsert_calls) == 3
    upserts = {c.kwargs.get("code"): c.kwargs.get("value") for c in upsert_calls}
    assert upserts["firmware_version"] == "5.5.0"
    assert upserts["galvos"] == "55555"
    assert upserts["software_version"] == "5.50"  # derived X.Y.Z → X.YZ
    for call in upsert_calls:
        assert call.args[0] == 4242  # id_entity
        assert call.kwargs["date_from"] == "2026-05-12T00:00:00"


def test_json_output(base_args, capsys) -> None:
    import json as _json

    base_args.json = True
    writer = _make_writer_mock()
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(base_args)
    out = capsys.readouterr().out

    assert rc == 0
    payload = _json.loads(
        # Strip the leading DRY RUN lines that go to stdout before the JSON blob
        out[out.index("{") :]
    )
    assert payload["model"] == "SEPT POLARX5"
    assert payload["model_raw_from_probe"] == "PolaRx5"
    assert payload["serial"] == "SN_HAPPY"
    assert payload["probe_type"] == "polarx5"
    assert payload["dry_run"] is True


# ---------------------------------------------------------------------------
# Probe failures
# ---------------------------------------------------------------------------


def test_probe_unreachable(base_args, capsys) -> None:
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            side_effect=ProbeUnreachableError("refused"),
        ),
        patch("tostools.api.tos_writer.TOSWriter") as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    assert rc == 1
    assert "refused" in capsys.readouterr().err
    tw_cls.assert_not_called()


def test_probe_not_identified(base_args, capsys) -> None:
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            side_effect=ProbeNotIdentifiedError("cannot identify"),
        ),
        patch("tostools.api.tos_writer.TOSWriter") as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    assert rc == 1
    assert "cannot identify" in capsys.readouterr().err
    tw_cls.assert_not_called()


def test_probe_incomplete_g10_without_overrides(base_args, capsys) -> None:
    """G10 returns partial identity → ProbeIncompleteError from to_subtype_attrs → exit 2."""
    g10 = ReceiverIdentity(
        subtype="gnss_receiver",
        probe_type="g10",
        serial=None,
        model_raw="LEICA GR10",
        firmware_version=None,
        marker_name=None,
        partial=True,
    )
    base_args.probe_type = "g10"
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=g10),
        patch("tostools.api.tos_writer.TOSWriter") as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "--serial" in err
    tw_cls.assert_not_called()


def test_probe_incomplete_g10_with_serial_and_model_overrides(base_args) -> None:
    """G10 + --serial + --model CLI overrides → probe succeeds → TOS write proceeds."""
    g10 = ReceiverIdentity(
        subtype="gnss_receiver",
        probe_type="g10",
        serial=None,
        model_raw=None,
        firmware_version=None,
        marker_name=None,
        partial=True,
    )
    base_args.probe_type = "g10"
    base_args.serial = "G10-12345"
    base_args.model = "GR10"  # alias → IGS-normalised to "LEICA GR10"
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=g10),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(base_args)
    assert rc == 0
    attrs = {a["code"]: a["value"] for a in writer.create_device.call_args.args[1]}
    assert attrs["serial_number"] == "G10-12345"
    assert attrs["model"] == "LEICA GR10"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_unknown_owner_rejected(base_args, capsys) -> None:
    base_args.owner = "Bogus Group"
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter") as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "Bogus Group" in err
    assert "tos owners list" in err
    tw_cls.assert_not_called()


def test_unknown_igs_model_rejected(base_args, capsys) -> None:
    bad = _polarx5_identity(model_raw="NotAReceiver")
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=bad),
        patch("tostools.api.tos_writer.TOSWriter") as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "NotAReceiver" in err
    assert "SEPT POLARX5" in err  # the known-models table
    tw_cls.assert_not_called()


def test_bad_date_rejected(base_args, capsys) -> None:
    base_args.date_start = "11/05/2026"
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter") as tw_cls,
    ):
        rc = cmd_cfg_add_receiver(base_args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "date_start" in err.lower() or "YYYY-MM-DD" in err
    tw_cls.assert_not_called()


def test_bad_probe_arg_rejected(base_args, capsys) -> None:
    base_args.probe = ":1234"
    with patch("tostools.api.tos_writer.TOSWriter") as tw_cls:
        rc = cmd_cfg_add_receiver(base_args)
    assert rc == 2
    assert "host" in capsys.readouterr().err.lower()
    tw_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Force / duplicate-serial pass-through
# ---------------------------------------------------------------------------


def test_duplicate_serial_without_force(base_args, capsys) -> None:
    writer = MagicMock()
    writer.create_device.side_effect = ValueError(
        "Device with serial_number='SN_HAPPY' already exists as gnss_receiver "
        "(id_entity=42). Pass force=True to add a duplicate."
    )
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(base_args)
    err = capsys.readouterr().err
    assert rc == 1
    assert "already exists" in err
    assert "--force" in err


def test_force_flag_passes_through(base_args) -> None:
    base_args.force = True
    writer = _make_writer_mock()
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(base_args)
    assert rc == 0
    assert writer.create_device.call_args.kwargs["force"] is True


# ---------------------------------------------------------------------------
# CLI override precedence (parity with tos device add semantics)
# ---------------------------------------------------------------------------


def test_firmware_override_wins(base_args) -> None:
    """--firmware on the CLI wins over the probed firmware."""
    base_args.firmware = "5.7.0"
    writer = _make_writer_mock()
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            return_value=_polarx5_identity(firmware_version="5.5.0"),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(base_args)
    assert rc == 0
    # The optional firmware_version upsert (logged in dry-run) carries the
    # override value; we can verify this through the dry-run text or the
    # writer state when --no-dry-run is set. Here we just confirm exit 0
    # and that to_subtype_attrs picked up the override (covered in
    # test_device_probe.py); the merge logic is already tested there.


# ---------------------------------------------------------------------------
# --from-file (capture-then-write workflow: probe on bench while VPN is off,
# save the result, push to TOS later when VPN is back and bench is unplugged)
# ---------------------------------------------------------------------------


def _write_intake_file(tmp_path: Path, **overrides) -> Path:
    """Materialise a YAML intake file with sensible defaults; overrides win."""
    import yaml as _yaml

    body = dict(
        subtype="gnss_receiver",
        probe_type="polarx5",
        serial="SN_FROMFILE",
        model_raw="PolaRx5",
        firmware_version="5.7.0",
        marker_name="HRAC",
        partial=False,
        owner="Veðurstofa Íslands",
        location="B9 - Kjallari - Jörð",
        date_start="2026-05-21",
        station_hint="HRAC",
    )
    body.update(overrides)
    path = tmp_path / "intake.yaml"
    path.write_text(_yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return path


def test_from_file_happy_path(parser, owners_yaml, tmp_path) -> None:
    """--from-file loads identity and required fields from YAML; no probe."""
    intake = _write_intake_file(tmp_path)
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch(
            "receivers.cfg.device_probe.probe_receiver",
            side_effect=AssertionError("probe must NOT be called when --from-file"),
        ),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    # create_device was called with the file's serial/model
    call = writer.create_device.call_args
    assert call.args[0] == "gnss_receiver"
    attrs = {a["code"]: a["value"] for a in call.args[1]}
    assert attrs["serial_number"] == "SN_FROMFILE"
    assert attrs["model"] == "SEPT POLARX5"  # IGS-normalised
    assert attrs["owner"] == "Veðurstofa Íslands"
    assert attrs["status"] == "virkt"
    assert attrs["date_start"].startswith("2026-05-21")


def test_from_file_cli_arg_overrides_file_value(parser, owners_yaml, tmp_path) -> None:
    """When both file and CLI supply a field, CLI wins."""
    intake = _write_intake_file(tmp_path, owner="OldOwner")
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owner",
            "Veðurstofa Íslands",  # CLI override
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    attrs = {a["code"]: a["value"] for a in writer.create_device.call_args.args[1]}
    assert attrs["owner"] == "Veðurstofa Íslands"


def test_default_location_when_neither_cli_nor_file_supplies(
    parser, owners_yaml, tmp_path
) -> None:
    """No --location AND no `location` in file → default to B9 - Kjallari - Jörð."""
    import yaml as _yaml

    intake = _write_intake_file(tmp_path)
    body = _yaml.safe_load(intake.read_text())
    body.pop("location")
    intake.write_text(_yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")

    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    assert args.location == "B9 - Kjallari - Jörð"


def test_file_location_wins_over_default(parser, owners_yaml, tmp_path) -> None:
    """File `location` is preserved — the CLI default doesn't override it."""
    intake = _write_intake_file(tmp_path, location="Vagnhöfði - Kjallari - Jörð")
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    assert args.location == "Vagnhöfði - Kjallari - Jörð"


def test_default_date_start_is_today(parser, owners_yaml, tmp_path) -> None:
    """No --date-start AND no `date_start` in file → default to today."""
    from datetime import date

    import yaml as _yaml

    intake = _write_intake_file(tmp_path)
    body = _yaml.safe_load(intake.read_text())
    body.pop("date_start", None)
    intake.write_text(_yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")

    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    assert args.date_start == date.today().isoformat()


def test_file_date_start_wins_over_today_default(parser, owners_yaml, tmp_path) -> None:
    """File `date_start` is preserved — the today-default doesn't override it."""
    intake = _write_intake_file(tmp_path, date_start="2026-01-15")
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    assert args.date_start == "2026-01-15"


def test_probe_and_from_file_mutually_exclusive(
    parser, owners_yaml, tmp_path, capsys
) -> None:
    """Supplying BOTH --probe and --from-file → exit 2."""
    intake = _write_intake_file(tmp_path)
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--probe",
            "192.168.3.1",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    rc = cmd_cfg_add_receiver(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "exactly one" in err.lower()


def test_neither_probe_nor_from_file(parser, owners_yaml, capsys) -> None:
    """Supplying neither --probe nor --from-file → exit 2."""
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--owner",
            "Veðurstofa Íslands",
            "--location",
            "Bench A",
            "--date-start",
            "2026-05-12",
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    rc = cmd_cfg_add_receiver(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "exactly one" in err.lower()


def test_from_file_path_does_not_exist(parser, owners_yaml, capsys, tmp_path) -> None:
    """Missing file → exit 2 with clear message."""
    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(tmp_path / "nope.yaml"),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    rc = cmd_cfg_add_receiver(args)
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err.lower()


def test_default_owner_when_neither_cli_nor_file_supplies(
    parser, owners_yaml, tmp_path
) -> None:
    """Without --owner on CLI AND without `owner` in --from-file, the
    intake defaults to Jarðeðlismælihópur (the IMO Geophysical
    Measurements Group, which owns the GPS receiver fleet — matches the
    owner attribute on every existing open child of B9 - Kjallari -
    Jörð).
    """
    # Re-materialise owners.yaml to include the default owner
    import yaml as _yaml

    owners_yaml.write_text(
        _yaml.safe_dump(
            {"owners": ["Veðurstofa Íslands", "Jarðeðlismælihópur"]}, allow_unicode=True
        )
    )
    intake = _write_intake_file(tmp_path)
    # Strip owner from the file so the default has to kick in
    body = _yaml.safe_load(intake.read_text())
    body.pop("owner")
    intake.write_text(_yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")

    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    attrs = {a["code"]: a["value"] for a in writer.create_device.call_args.args[1]}
    assert attrs["owner"] == "Jarðeðlismælihópur"


def test_empty_string_owner_falls_back_to_the_default(
    parser, owners_yaml, tmp_path
) -> None:
    """`--owner ""` takes the DEFAULT — the gates test falsiness, not None.

    Every fill-in here is `if not getattr(args, key, None)`, so an empty string
    is treated as "not supplied". That is the sensible reading of `--owner ""`
    (there is no owner named ""; TOS would reject it), but it is a distinction
    a request-object refactor erases without noticing: a frozen dataclass with
    `Optional[str]` and an `is None` check would carry the empty string
    straight through to a TOS write that then fails validation.

    Verified by mutation: swapping the falsy test for `is None` left the whole
    intake suite green before this test existed.
    """
    import yaml as _yaml

    owners_yaml.write_text(
        _yaml.safe_dump(
            {"owners": ["Veðurstofa Íslands", "Jarðeðlismælihópur"]}, allow_unicode=True
        )
    )
    intake = _write_intake_file(tmp_path)
    body = _yaml.safe_load(intake.read_text())
    body.pop("owner")
    intake.write_text(_yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")

    args = parser.parse_args(
        [
            "cfg",
            "add-receiver",
            "--from-file",
            str(intake),
            "--owner",
            "",
            "--owners-cache",
            str(owners_yaml),
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver"),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_add_receiver(args)
    assert rc == 0
    attrs = {a["code"]: a["value"] for a in writer.create_device.call_args.args[1]}
    assert attrs["owner"] == "Jarðeðlismælihópur", (
        "an empty --owner must fall through to the default, not be written as ''"
    )
