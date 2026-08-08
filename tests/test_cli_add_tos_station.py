"""Tests for ``receivers cfg add-tos-station`` — the greenfield orchestrator.

Mocks the probe (`receivers.cli.cfg._probe_cfg_fields`) and the tostools CLI
entry point (`tostools.tos.main`) so no receiver is dialled and no TOS write is
attempted. What's under test is the decision logic between those two calls:
argv construction, coordinate precedence, and the refusal paths.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from receivers.cli.cfg import cmd_cfg_add_tos_station, create_cfg_parser

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: What a successful PolaRX5 probe yields (trimmed to what the verb reads).
PROBED_FIELDS = {
    "station_id": "NPSK",
    "router_ip": "10.6.1.71",
    "receiver_type": "PolaRX5",
    "receiver_ftpport": "2160",
    "receiver_httpport": "8060",
    "receiver_controlport": "28784",
    "rinex_marker_name": "NPSK",
    "receiver_serial": "4103913",
    "receiver_firmware_version": "5.7.0",
    "latitude": "66.48574808",
    "longitude": "-16.49981136",
    "height": "103.143",
}


@pytest.fixture
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers(dest="command")
    create_cfg_parser(subparsers)
    return p


@pytest.fixture
def stations_cfg(tmp_path: Path) -> Path:
    """An empty-but-real stations.cfg the verb can inspect and append to."""
    p = tmp_path / "stations.cfg"
    p.write_text("[ELEY]\nstation_id = ELEY\n")
    return p


def _args(parser: argparse.ArgumentParser, *argv: str):
    return parser.parse_args(["cfg", "add-tos-station", *argv])


class _FakeConfigParser:
    """Stands in for gps_parser.ConfigParser so cfg_path points at tmp_path."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_stations_config_path(self) -> str:
        return str(self._path)


@pytest.fixture
def patched(stations_cfg: Path):
    """Patch the probe, the tostools entry point, and the cfg-path lookup."""
    import gps_parser

    tos_calls: list[list[str]] = []

    def fake_tos_main(argv: list[str]) -> int:
        tos_calls.append(list(argv))
        if "--json" in argv:
            print(json.dumps({"marker": "NPSK", "station_id": 99, "site_id": 98}))
        return 0

    with (
        patch(
            "receivers.cli.cfg._probe_cfg_fields",
            return_value=({}, dict(PROBED_FIELDS)),
        ) as probe,
        patch.object(
            gps_parser, "ConfigParser", lambda: _FakeConfigParser(stations_cfg)
        ),
        patch("tostools.tos.main", side_effect=fake_tos_main) as tos_main,
    ):
        yield {"probe": probe, "tos_main": tos_main, "tos_calls": tos_calls}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


BASE = (
    "NPSK",
    "--probe",
    "10.6.1.71",
    "--name",
    "Núpskatla",
    "--date-start",
    "2026-08-08",
)


def test_dry_run_writes_nothing(parser, patched, stations_cfg: Path):
    """Default dry-run: tostools invoked without --no-dry-run, cfg untouched."""
    before = stations_cfg.read_text()
    rc = cmd_cfg_add_tos_station(_args(parser, *BASE))

    assert rc == 0
    assert stations_cfg.read_text() == before, "dry-run must not touch stations.cfg"
    (argv,) = patched["tos_calls"]
    assert "--no-dry-run" not in argv


def test_commit_writes_both_halves(parser, patched, stations_cfg: Path):
    rc = cmd_cfg_add_tos_station(_args(parser, *BASE, "--no-dry-run"))

    assert rc == 0
    (argv,) = patched["tos_calls"]
    assert "--no-dry-run" in argv

    text = stations_cfg.read_text()
    assert "[NPSK]" in text
    assert "receiver_serial = 4103913" in text
    # The provenance warning must survive into the file, not just the terminal.
    assert "PVT fix" in text


def test_probed_position_flows_into_tos_argv(parser, patched):
    cmd_cfg_add_tos_station(_args(parser, *BASE))

    (argv,) = patched["tos_calls"]
    assert argv[:2] == ["station", "add"]
    assert argv[argv.index("--lat") + 1] == "66.48574808"
    assert argv[argv.index("--lon") + 1] == "-16.49981136"
    assert argv[argv.index("--altitude") + 1] == "103.143"
    # Site name defaults to the station name.
    assert argv[argv.index("--location-name") + 1] == "Núpskatla"


def test_site_name_override(parser, patched):
    cmd_cfg_add_tos_station(_args(parser, *BASE, "--site-name", "Núpskatla SIL stöð"))
    (argv,) = patched["tos_calls"]
    assert argv[argv.index("--location-name") + 1] == "Núpskatla SIL stöð"


# ---------------------------------------------------------------------------
# Coordinate precedence — a survey must never lose to a PVT fix
# ---------------------------------------------------------------------------


def test_surveyed_override_beats_probe(parser, patched, stations_cfg: Path):
    rc = cmd_cfg_add_tos_station(
        _args(
            parser,
            *BASE,
            "--lat",
            "66.4857100",
            "--lon",
            "-16.4998200",
            "--height",
            "103.500",
            "--no-dry-run",
        )
    )

    assert rc == 0
    (argv,) = patched["tos_calls"]
    assert argv[argv.index("--lat") + 1] == "66.4857100"
    assert argv[argv.index("--altitude") + 1] == "103.500"

    text = stations_cfg.read_text()
    assert "latitude = 66.4857100" in text
    # Fully-surveyed position must NOT carry the provisional warning.
    assert "PVT fix" not in text
    assert "surveyed" in text


def test_partial_override_still_marked_provisional(parser, patched, stations_cfg: Path):
    """One surveyed value among three does not make the position surveyed."""
    cmd_cfg_add_tos_station(_args(parser, *BASE, "--lat", "66.4", "--no-dry-run"))

    text = stations_cfg.read_text()
    assert "latitude = 66.4" in text
    assert "longitude = -16.49981136" in text
    assert "PVT fix" in text


# ---------------------------------------------------------------------------
# Refusals — ordering matters: nothing may be written after a guard trips
# ---------------------------------------------------------------------------


def test_existing_cfg_section_refused_before_tos_write(
    parser, patched, stations_cfg: Path
):
    """The collision guard must fire BEFORE minting an undeletable land site."""
    stations_cfg.write_text("[NPSK]\nstation_id = NPSK\n")

    rc = cmd_cfg_add_tos_station(_args(parser, *BASE, "--no-dry-run"))

    assert rc == 1
    assert patched["tos_calls"] == [], "TOS must not be touched on a cfg collision"
    patched["probe"].assert_not_called()


def test_no_cfg_and_no_tos_is_a_no_op(parser, patched):
    rc = cmd_cfg_add_tos_station(_args(parser, *BASE, "--no-cfg", "--no-tos"))
    assert rc == 2
    assert patched["tos_calls"] == []


def test_no_probe_and_no_coords_refused(parser, patched):
    rc = cmd_cfg_add_tos_station(
        _args(parser, "NPSK", "--name", "X", "--date-start", "2026-08-08", "--no-cfg")
    )
    assert rc == 2
    assert patched["tos_calls"] == []


def test_cfg_section_requires_a_probe(parser, patched):
    """Coordinates alone cannot produce a cfg section — identity is missing."""
    rc = cmd_cfg_add_tos_station(
        _args(
            parser,
            "NPSK",
            "--name",
            "X",
            "--date-start",
            "2026-08-08",
            "--lat",
            "66.4",
            "--lon",
            "-16.4",
            "--height",
            "100",
        )
    )
    assert rc == 2
    assert patched["tos_calls"] == []


def test_probeless_tos_only_path(parser, patched, stations_cfg: Path):
    """No probe + full coordinates + --no-cfg registers TOS and leaves cfg alone."""
    before = stations_cfg.read_text()
    rc = cmd_cfg_add_tos_station(
        _args(
            parser,
            "NPSK",
            "--name",
            "Núpskatla",
            "--date-start",
            "2026-08-08",
            "--lat",
            "66.4857",
            "--lon",
            "-16.4998",
            "--height",
            "103.5",
            "--no-cfg",
            "--no-dry-run",
        )
    )

    assert rc == 0
    assert stations_cfg.read_text() == before
    (argv,) = patched["tos_calls"]
    assert argv[argv.index("--lat") + 1] == "66.4857"


def test_unreachable_probe_without_coords_refused(parser, patched):
    with patch("receivers.cli.cfg._probe_cfg_fields", return_value=None):
        rc = cmd_cfg_add_tos_station(_args(parser, *BASE))
    assert rc == 1
    assert patched["tos_calls"] == []


def test_tos_failure_leaves_cfg_untouched(parser, patched, stations_cfg: Path):
    """A non-zero tostools exit must abort before the cfg section is written."""
    before = stations_cfg.read_text()
    with patch("tostools.tos.main", return_value=1):
        rc = cmd_cfg_add_tos_station(_args(parser, *BASE, "--no-dry-run"))

    assert rc == 1
    assert stations_cfg.read_text() == before


# ---------------------------------------------------------------------------
# --json contract
# ---------------------------------------------------------------------------


def test_json_stdout_is_pure_json(parser, patched, capsys):
    """stdout must parse as JSON — human lines and the probe banner go elsewhere."""
    rc = cmd_cfg_add_tos_station(_args(parser, *BASE, "--json"))
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["station"] == "NPSK"
    assert payload["dry_run"] is True
    assert payload["position_surveyed"] == {
        "latitude": False,
        "longitude": False,
        "height": False,
    }
    # The nested tostools summary is parsed, not passed through as a blob.
    assert payload["tos"]["marker"] == "NPSK"


def test_json_mode_suppresses_probe_banner(parser, patched, capsys):
    """The shared probe helper prints a progress line — it must not reach stdout.

    Regression guard: this is exactly what made an earlier --json run unparseable.
    """

    def noisy_probe(sid, station_config, host):
        print(f"↳ {sid}: probing receiver…")
        return ({}, dict(PROBED_FIELDS))

    with patch("receivers.cli.cfg._probe_cfg_fields", side_effect=noisy_probe):
        rc = cmd_cfg_add_tos_station(_args(parser, *BASE, "--json"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "probing receiver" not in out
    json.loads(out)  # raises if the banner leaked


def test_json_mode_passes_json_to_tostools(parser, patched):
    cmd_cfg_add_tos_station(_args(parser, *BASE, "--json"))
    (argv,) = patched["tos_calls"]
    assert "--json" in argv


def test_control_port_is_not_the_tos_port(parser, patched):
    """--port is the TOS API port; it must never reach the receiver probe."""
    captured: dict[str, Any] = {}

    def capture(sid, station_config, host):
        captured["config"] = station_config
        return ({}, dict(PROBED_FIELDS))

    with patch("receivers.cli.cfg._probe_cfg_fields", side_effect=capture):
        cmd_cfg_add_tos_station(_args(parser, *BASE, "--port", "443"))

    assert captured["config"]["receiver"]["controlport"] == "28784"
    (argv,) = patched["tos_calls"]
    assert argv[argv.index("--port") + 1] == "443"
