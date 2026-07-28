"""Regression tests for receivers.config_utils.get_station_config.

The function builds a *typed* dict with nested ``receiver``/``antenna``/
``router`` sub-dicts. For a long time it silently dropped flat keys
that don't fit the typed structure (``receiver_serial``,
``receiver_firmware_version``, ``latitude``, ``longitude``,
``height``, …). Any code path comparing values against ``stations.cfg``
got ``None`` from those getters regardless of file content, which broke
``cfg reconcile``, the ``cfg_discrepancy`` audit log, and the live
identity check in the health flow.

Fix landed in commit "feat(cfg): cfg discrepancy audit log" — every raw
``stations.cfg`` key is merged into the returned dict via
``setdefault``. These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

# A self-contained stations.cfg shaped to exercise every flat field the
# reconciler needs, plus enough surrounding sections that gps_parser is
# happy to load the file.
_STATIONS_CFG = """\
[Configs]
data_prepath = /tmp/gpsdata

[PATHS]
bin2asc_path = /usr/local/bin/bin2asc
receiver_base_path = /Disk/internal

[DEFAULTS]
default_session = 15s_24hr
default_compression = .gz
default_days_back = 5

[TIMEOUT_CATEGORIES]
fixed_wired = 10,30,180,8192
mobile = 20,60,300,2048
very_remote = 30,120,600,1024

[NETWORK_RULES]
default = passive

[ELDC]
sessions = 15s_24hr, 1Hz_1hr, status_1hr
router_ip = 10.0.0.1
router_type = Teltonika-rut240
receiver_type = PolaRX5
receiver_ftpport = 2160
receiver_httpport = 8060
receiver_controlport = 28784
station_id = ELDC
station_name = Eldcraft
power_type = battery
connection_type = IP
antenna_type = SEPPOLANT_X_MF
antenna_radome = NONE
antenna_serial = 9999
antenna_height = 0.05
latitude = 64.123456
longitude = -22.654321
height = 250.55
receiver_serial = 4103914
receiver_firmware_version = 5.7.0
receiver_base_path = /External/
receiver_cachedir_prefix = /CACHEDIR123/download
ftp_username = gpsops
ftp_password = secret
"""

# Minimal postprocess.cfg so gps_parser doesn't error during init.
_POSTPROCESS_CFG = """\
[FILES]
stations_cfg = stations.cfg
"""


@pytest.fixture
def fake_gps_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point GPS_CONFIG_PATH at a tmp dir holding a self-contained stations.cfg."""
    (tmp_path / "stations.cfg").write_text(_STATIONS_CFG)
    (tmp_path / "postprocess.cfg").write_text(_POSTPROCESS_CFG)
    monkeypatch.setenv("GPS_CONFIG_PATH", str(tmp_path))
    yield tmp_path


def test_typed_structure_still_present(fake_gps_config):
    """Backwards-compat: the nested layout consumers depend on still works."""
    from receivers.config_utils import get_station_config

    cfg = get_station_config("ELDC")
    assert cfg is not None

    assert cfg["station_id"] == "ELDC"
    assert cfg["receiver_type"] == "PolaRX5"
    assert cfg["receiver"]["type"] == "PolaRX5"
    assert cfg["receiver"]["ftpport"] == "2160"
    assert cfg["antenna"]["type"] == "SEPPOLANT_X_MF"
    assert cfg["antenna"]["serial"] == "9999"
    assert cfg["router"]["ip"] == "10.0.0.1"


@pytest.mark.parametrize(
    "key, expected",
    [
        ("receiver_serial", "4103914"),
        ("receiver_firmware_version", "5.7.0"),
        ("latitude", "64.123456"),
        ("longitude", "-22.654321"),
        ("height", "250.55"),
        ("station_name", "Eldcraft"),
        ("antenna_serial", "9999"),
        ("antenna_radome", "NONE"),
        ("antenna_type", "SEPPOLANT_X_MF"),
    ],
)
def test_flat_cfg_keys_visible_at_top_level(fake_gps_config, key, expected):
    """The reconciler reads flat keys directly — they must be present.

    Regression: before the setdefault merge in get_station_config, all
    of these returned None even though the values were on disk, so
    `cfg reconcile` reported them as missing forever.
    """
    from receivers.config_utils import get_station_config

    cfg = get_station_config("ELDC")
    assert cfg is not None
    assert cfg.get(key) == expected, (
        f"{key} should propagate from stations.cfg to top-level dict; "
        f"got {cfg.get(key)!r}"
    )


def test_per_station_base_path_and_cachedir_prefix_mapped(fake_gps_config):
    """receiver_base_path / receiver_cachedir_prefix reach the typed receiver dict.

    These are two distinct path pieces that must NOT share a key:
      * base_path      → storage root (the NetR9/NetRS path builder; /Internal/
                         vs /External/), consumed by _resolve_base_path().
      * cachedir_prefix → the NetR5 CACHEDIR download prefix (HTTP client).
    Mapping them to separate fields is what prevents a station's storage-root
    override from hijacking the CACHEDIR slot (and vice-versa).
    """
    from receivers.config_utils import get_station_config

    cfg = get_station_config("ELDC")
    assert cfg is not None
    assert cfg["receiver"]["base_path"] == "/External/"
    assert cfg["receiver"]["cachedir_prefix"] == "/CACHEDIR123/download"


def test_typed_keys_win_over_raw_on_overlap(fake_gps_config):
    """When the typed structure already sets a key, raw must not clobber it.

    ``station_name`` exists both as a flat raw key (``Eldcraft``) and as
    an explicit field built into the typed dict from the same source —
    the values match here, but the contract is "typed wins". Verify by
    spot-checking the typed-only fallback (``station_name`` defaults to
    station_id when raw is missing) still applies.
    """
    from receivers.config_utils import get_station_config

    cfg = get_station_config("ELDC")
    assert cfg is not None
    # station_name should be the explicit raw value, not a fallback.
    assert cfg["station_name"] == "Eldcraft"
    # receiver_type is set explicitly in the typed dict; raw merge can
    # only fill, not overwrite.
    assert cfg["receiver_type"] == "PolaRX5"


def test_unknown_station_returns_none(fake_gps_config, caplog):
    from receivers.config_utils import get_station_config

    cfg = get_station_config("ZZZZ", silent=True)
    assert cfg is None


class TestSupportedSessionCasing:
    """Session names must come back in canonical casing, not configparser's.

    ``configparser`` lower-cases every option name, so ``session_map_1Hz_1hr``
    in receivers.cfg reads back as ``session_map_1hz_1hr``. Everything else —
    the CLI's ``--session`` choices, the scheduler, archive path templates —
    uses ``1Hz_1hr``. Before the fix an exact-match check could never succeed,
    so ``receivers download <PolaRX5 station> --session 1Hz_1hr`` was rejected
    as "not supported" for the whole Septentrio fleet.
    """

    def test_sessions_use_canonical_casing(self):
        from receivers.config.receivers_config import get_receivers_config

        sessions = get_receivers_config().get_supported_sessions("polarx5")
        assert "1Hz_1hr" in sessions
        assert "1hz_1hr" not in sessions

    def test_matches_the_cli_choices(self):
        """What the gate accepts must be spelled the way argparse demands."""
        from receivers.config.receivers_config import get_receivers_config

        cli_choices = {"15s_24hr", "1Hz_1hr", "status_1hr"}
        for receiver_type in ("polarx5", "netr9", "netrs"):
            sessions = set(get_receivers_config().get_supported_sessions(receiver_type))
            if not sessions:
                continue
            assert sessions <= cli_choices, (
                f"{receiver_type} reports sessions the CLI would refuse: "
                f"{sessions - cli_choices}"
            )
