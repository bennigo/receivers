"""Backfill must use the same Trimble converter as the live path.

Live downloads went through TrimbleNativeConverter (Docker/Wine) while the
backfill hardcoded TrimbleConverter (runpkr00 + teqc). runpkr00 exits 30 on
.T02 files whose payload is bzip2-compressed — it writes a 26-byte stub .dat —
so every Trimble backfill conversion failed and the gaps never filled. Measured
2026-08-16 on rek-d01: 566 distinct files, 1,132 failed spawns per 3 h.

Worse, failure_class advised `archive-rm` on those files. They are not bad —
the native converter reads them fine.
"""

from unittest.mock import MagicMock, patch

import pytest

from receivers.rinex.failure_class import *  # noqa: F401,F403
from receivers.scheduling.task_interface import TaskConfig, TaskFrequency, TaskType
from receivers.scheduling.tasks.rinex_task import RINEXTask


def _task():
    cfg = TaskConfig(
        task_type=TaskType.RINEX,
        session_type="1Hz_1hr",
        frequency=TaskFrequency.HOURLY,
        schedule_minute=0,
        distribution_window=5,
    )
    return RINEXTask(station_id="VARG", config=cfg)


def _rinex_cfg(use_native):
    cfg = MagicMock()
    cfg.get_rinex_config.return_value = {"use_native_trimble": use_native}
    return cfg


# --- converter selection ----------------------------------------------------


def test_prefers_native_converter_when_configured_and_available():
    from receivers.rinex.trimble_native_converter import TrimbleNativeConverter

    t = _task()
    with (
        patch(
            "receivers.config.receivers_config.get_receivers_config",
            return_value=_rinex_cfg(True),
        ),
        patch.object(TrimbleNativeConverter, "is_available", return_value=True),
    ):
        assert t._resolve_trimble_converter(object) is TrimbleNativeConverter


def test_falls_back_when_native_not_configured():
    """Config off = old behaviour, unchanged."""
    sentinel = object()
    t = _task()
    with patch(
        "receivers.config.receivers_config.get_receivers_config",
        return_value=_rinex_cfg(False),
    ):
        assert t._resolve_trimble_converter(sentinel) is sentinel


def test_falls_back_and_warns_when_docker_is_unavailable():
    """A Docker outage must degrade, not fail the task — but say so."""
    from receivers.rinex.trimble_native_converter import TrimbleNativeConverter

    sentinel = object()
    t = _task()
    t.logger = MagicMock()
    with (
        patch(
            "receivers.config.receivers_config.get_receivers_config",
            return_value=_rinex_cfg(True),
        ),
        patch.object(TrimbleNativeConverter, "is_available", return_value=False),
    ):
        assert t._resolve_trimble_converter(sentinel) is sentinel
    t.logger.warning.assert_called_once()
    assert "runpkr00" in t.logger.warning.call_args[0][0]


def test_falls_back_when_config_lookup_raises():
    sentinel = object()
    t = _task()
    t.logger = MagicMock()
    with patch(
        "receivers.config.receivers_config.get_receivers_config",
        side_effect=Exception("no config"),
    ):
        assert t._resolve_trimble_converter(sentinel) is sentinel


@pytest.mark.parametrize("rx", ["netr9", "netrs", "netr5"])
def test_all_trimble_types_route_through_the_resolver(rx):
    """netr5 was in the same hardcoded branch and must not be left behind."""
    from receivers.rinex.trimble_native_converter import TrimbleNativeConverter

    t = _task()
    with (
        patch(
            "receivers.config.receivers_config.get_receivers_config",
            return_value=_rinex_cfg(True),
        ),
        patch.object(TrimbleNativeConverter, "is_available", return_value=True),
    ):
        conv = t._get_converter({"receiver_type": rx.upper()})
    assert isinstance(conv, TrimbleNativeConverter)


def test_septentrio_selection_is_untouched():
    from receivers.rinex.sbf_converter import SBFConverter

    t = _task()
    assert isinstance(t._get_converter({"receiver_type": "PolaRX5"}), SBFConverter)


# --- the classification that could have cost data ---------------------------


def test_runpkr00_exit_30_no_longer_advises_deleting_the_raw():
    """It said 'archive-rm'. The raw is fine; the converter was wrong."""
    from receivers.rinex import failure_class as fc

    entries = [e for e in fc.__dict__.values() if isinstance(e, tuple)]
    text = " ".join(str(e) for e in entries)
    assert "runpkr00 failed with exit code 30" in text
    hit = [e for e in _pairs(fc) if "runpkr00 failed with exit code 30" in e[0]]
    assert hit, "the exit-30 classification disappeared"
    advice = hit[0][1]
    assert "archive-rm" not in advice.replace("DO NOT archive-rm", "")
    assert "DO NOT archive-rm" in advice
    assert "native" in advice.lower() or "docker" in advice.lower()


def _pairs(mod):
    out = []
    for v in mod.__dict__.values():
        if isinstance(v, tuple):
            for e in v:
                if isinstance(e, tuple) and len(e) == 2 and isinstance(e[0], str):
                    out.append(e)
    return out
