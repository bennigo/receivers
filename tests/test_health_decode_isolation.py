"""Health checks must not be starved by their own SBF decoding.

The scheduler already gives health monitoring a dedicated APScheduler executor,
so it is isolated from downloads and backfill. It is NOT isolated from
*conversion*: the PolaRX5 health check falls back to decoding a status_1hr SBF
with ``bin2asc`` on the health worker thread. On 2026-08-11 that fallback
wedged fleet-wide (one firmware revision bin2asc choked on), 77 processes
accumulated, and fleet monitoring went blind.

Three properties keep that bounded, and each is pinned here:

1. the fallback decodes all blocks in ONE bin2asc pass, not one per block —
   so a wedge costs one timeout ceiling, not four stacked on one thread;
2. that pass uses a health-sized ceiling, not the daily-SBF one;
3. aggregate decode concurrency is capped, and an unavailable slot SKIPS the
   decode rather than queueing for it — queueing would move the wait onto the
   very thread the cap protects.
"""

import copy
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from receivers.health import decode_gate
from receivers.health.rxtools_extractor import RxToolsExtractor
from receivers.utils import rxtools_extractor as rx


@pytest.fixture(autouse=True)
def _fresh_gate():
    """Each test gets an unclaimed semaphore and a zeroed skip counter."""
    decode_gate._reset_for_tests()
    yield
    decode_gate._reset_for_tests()


def _write_sbf(tmp_path: Path) -> Path:
    sbf = tmp_path / "STAT202608251200c.sbf"
    sbf.write_bytes(b"\x24\x40" + b"\x00" * 50)
    return sbf


# ---------------------------------------------------------------------------
# 1. One pass, not one per block
# ---------------------------------------------------------------------------


class TestSinglePassExtraction:
    def test_all_blocks_come_from_one_subprocess(self, tmp_path: Path):
        sbf = _write_sbf(tmp_path)
        wanted = ("PowerStatus", "ReceiverStatus2", "DiskStatus", "QualityInd")

        def fake_run(cmd, **kwargs):
            out = Path(cmd[cmd.index("-p") + 1])
            for block in wanted:
                (out / f"{sbf.name}_SBF_{block}.txt").write_text("A,B\n1,2\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run) as mock_run:
            blocks = rx.extract_sbf_blocks(sbf, wanted)

        assert mock_run.call_count == 1, "one bin2asc pass must cover every block"
        assert set(blocks) == set(wanted)
        assert all(rows for rows in blocks.values())

    def test_pass_is_unfiltered_so_every_block_lands(self, tmp_path: Path):
        """No -m flag: filtering per block is what forced N invocations."""
        sbf = _write_sbf(tmp_path)

        def fake_run(cmd, **kwargs):
            out = Path(cmd[cmd.index("-p") + 1])
            (out / f"{sbf.name}_SBF_PowerStatus.txt").write_text("A\n1\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run) as mock_run:
            rx.extract_sbf_blocks(sbf, ["PowerStatus"])

        assert "-m" not in mock_run.call_args.args[0]

    def test_absent_block_is_empty_not_an_error(self, tmp_path: Path):
        """A file lacking a block must not lose the blocks it does carry."""
        sbf = _write_sbf(tmp_path)

        def fake_run(cmd, **kwargs):
            out = Path(cmd[cmd.index("-p") + 1])
            (out / f"{sbf.name}_SBF_PowerStatus.txt").write_text("A\n1\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run):
            blocks = rx.extract_sbf_blocks(sbf, ["PowerStatus", "DiskStatus"])

        assert blocks["PowerStatus"]
        assert blocks["DiskStatus"] == []

    def test_timeout_degrades_to_runtimeerror(self, tmp_path: Path):
        """Callers must see the same failure shape as a bin2asc error."""
        sbf = _write_sbf(tmp_path)

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["bin2asc"], timeout=30),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                rx.extract_sbf_blocks(sbf, ["PowerStatus"])


# ---------------------------------------------------------------------------
# 2. A health-sized ceiling
# ---------------------------------------------------------------------------


class TestHealthSizedTimeout:
    def test_health_ceiling_is_far_below_the_daily_one(self):
        """The fallback reads a ~70 kB hourly file, not a 4 MB daily SBF."""
        assert rx.RXTOOLS_HEALTH_TIMEOUT_S < rx.RXTOOLS_TIMEOUT_S

    def test_health_extraction_passes_the_health_ceiling(self, tmp_path: Path):
        sbf = _write_sbf(tmp_path)
        extractor = RxToolsExtractor(station_id="TEST")

        with patch(
            "receivers.health.rxtools_extractor.extract_sbf_blocks",
            return_value={},
        ) as mock_extract:
            extractor._extract_all_health_blocks(sbf)

        assert mock_extract.call_args.kwargs["timeout"] == rx.RXTOOLS_HEALTH_TIMEOUT_S

    def test_failed_pass_returns_empty_metrics_not_an_exception(self, tmp_path: Path):
        """A wedge must cost the enrichment, never the whole health row."""
        sbf = _write_sbf(tmp_path)
        extractor = RxToolsExtractor(station_id="TEST")

        with patch(
            "receivers.health.rxtools_extractor.extract_sbf_blocks",
            side_effect=RuntimeError("bin2asc timed out after 30s"),
        ):
            result = extractor._extract_all_health_blocks(sbf)

        assert result["metrics"] == {}
        assert result["data_quality"] == {}


# ---------------------------------------------------------------------------
# 3. Aggregate cap that skips rather than queues
# ---------------------------------------------------------------------------


class TestDecodeGate:
    def test_slot_is_granted_when_free(self):
        with decode_gate.decode_slot("TEST") as claimed:
            assert claimed is True

    def test_slot_is_released_on_exit(self):
        for _ in range(decode_gate.HEALTH_DECODE_SLOTS + 2):
            with decode_gate.decode_slot("TEST") as claimed:
                assert claimed is True

    def test_slot_is_released_even_when_the_decode_raises(self):
        with pytest.raises(RuntimeError):
            with decode_gate.decode_slot("TEST"):
                raise RuntimeError("bin2asc wedged")

        with decode_gate.decode_slot("TEST") as claimed:
            assert claimed is True, "a failed decode must not leak its permit"

    def test_exhausted_gate_refuses_instead_of_blocking(self):
        """The whole point: no waiting on the thread the cap protects."""
        held = []
        for _ in range(decode_gate.HEALTH_DECODE_SLOTS):
            ctx = decode_gate.decode_slot("BUSY")
            assert ctx.__enter__() is True
            held.append(ctx)

        finished = threading.Event()
        result = {}

        def try_claim():
            with decode_gate.decode_slot("LATE") as claimed:
                result["claimed"] = claimed
            finished.set()

        threading.Thread(target=try_claim, daemon=True).start()

        assert finished.wait(timeout=5), "decode_slot must never block"
        assert result["claimed"] is False
        assert decode_gate.skipped_count() == 1

        for ctx in held:
            ctx.__exit__(None, None, None)

    def test_bad_slot_env_defaults_instead_of_crashing_at_import(self, monkeypatch):
        """A typo in the systemd unit must not take the scheduler down."""
        monkeypatch.setenv("RECEIVERS_HEALTH_DECODE_SLOTS", "four")
        assert decode_gate._configured_slots() == (
            decode_gate.DEFAULT_HEALTH_DECODE_SLOTS
        )

    def test_slot_env_is_honoured_when_valid(self, monkeypatch):
        monkeypatch.setenv("RECEIVERS_HEALTH_DECODE_SLOTS", "9")
        assert decode_gate._configured_slots() == 9

    def test_slot_count_is_never_zero(self, monkeypatch):
        """Zero slots would silently disable the SBF fallback fleet-wide."""
        monkeypatch.setenv("RECEIVERS_HEALTH_DECODE_SLOTS", "0")
        assert decode_gate._configured_slots() == 1

    def test_capacity_recovers_once_holders_release(self):
        ctxs = [
            decode_gate.decode_slot("BUSY")
            for _ in range(decode_gate.HEALTH_DECODE_SLOTS)
        ]
        for ctx in ctxs:
            ctx.__enter__()
        for ctx in ctxs:
            ctx.__exit__(None, None, None)

        with decode_gate.decode_slot("TEST") as claimed:
            assert claimed is True


class TestPolarx5UsesTheGate:
    """The fallback is the call site that runs conversion on a health thread."""

    def _receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5

        rec = object.__new__(PolaRX5)
        rec.station_id = "TEST"
        rec.logger = MagicMock()
        return rec

    def test_skips_extraction_when_no_slot_is_free(self, tmp_path: Path):
        rec = self._receiver()
        sbf = _write_sbf(tmp_path)

        held = []
        for _ in range(decode_gate.HEALTH_DECODE_SLOTS):
            ctx = decode_gate.decode_slot("BUSY")
            ctx.__enter__()
            held.append(ctx)

        fake_extractor = MagicMock()
        fake_extractor.check_rxtools_available.return_value = True

        with (
            patch.object(
                type(rec), "_find_latest_status_file", return_value=sbf, create=True
            ),
            patch("receivers.health.RxToolsExtractor", return_value=fake_extractor),
        ):
            result = rec._get_health_from_sbf_files()

        assert result is None
        fake_extractor.extract_health_from_sbf.assert_not_called()

        for ctx in held:
            ctx.__exit__(None, None, None)

    def test_extracts_normally_when_a_slot_is_free(self, tmp_path: Path):
        rec = self._receiver()
        sbf = _write_sbf(tmp_path)

        fake_extractor = MagicMock()
        fake_extractor.check_rxtools_available.return_value = True
        fake_extractor.extract_health_from_sbf.return_value = {"metrics": {"x": 1}}

        with (
            patch.object(
                type(rec), "_find_latest_status_file", return_value=sbf, create=True
            ),
            patch("receivers.health.RxToolsExtractor", return_value=fake_extractor),
        ):
            result = rec._get_health_from_sbf_files()

        assert result == {"metrics": {"x": 1}}
        fake_extractor.extract_health_from_sbf.assert_called_once_with(sbf)


# ---------------------------------------------------------------------------
# 4. Health executor capacity is configurable, not derived from max_workers
# ---------------------------------------------------------------------------


class TestHealthWorkersKnob:
    """max_workers sizes downloads; fleet monitoring should not ride on it."""

    def _scheduler(
        self,
        tmp_path: Path,
        monkeypatch,
        status_monitoring: dict,
        max_workers: int = 30,
    ):
        from receivers.scheduling import bulk_scheduler as bs
        from receivers.scheduling.config_loader import get_default_config

        # Built from the packaged defaults, not load_scheduler_config(): the latter
        # reads the developer's own ~/.config/gpsconfig/scheduler.yaml, which
        # is deliberately divergent from production and could quietly supply a
        # status_monitoring.workers of its own.
        config = copy.deepcopy(get_default_config())
        config["status_monitoring"] = {
            **config["status_monitoring"],
            **status_monitoring,
        }
        monkeypatch.setattr(
            "receivers.scheduling.config_loader.load_scheduler_config",
            lambda *_a, **_k: config,
        )

        with patch("receivers.cli.main.get_all_station_configs", return_value={}):
            return bs.BulkDownloadScheduler(
                production_mode=False,
                max_workers=max_workers,
                database_url=f"sqlite:///{tmp_path / 'sched.db'}",
                log_dir=tmp_path,
            )

    def _health_workers(self, scheduler) -> int:
        return scheduler.scheduler._lookup_executor("health")._pool._max_workers

    def test_defaults_to_the_derived_share(self, tmp_path: Path, monkeypatch):
        sched = self._scheduler(tmp_path, monkeypatch, {}, max_workers=30)
        assert self._health_workers(sched) == 10  # min(30 // 3, 30)

    def test_derived_share_is_capped(self, tmp_path: Path, monkeypatch):
        """Production runs max_workers=200, so the cap is the branch it takes."""
        sched = self._scheduler(tmp_path, monkeypatch, {}, max_workers=200)
        assert self._health_workers(sched) == 30  # min(200 // 3, 30)

    def test_explicit_workers_wins(self, tmp_path: Path, monkeypatch):
        sched = self._scheduler(tmp_path, monkeypatch, {"workers": 24})
        assert self._health_workers(sched) == 24

    def test_explicit_workers_may_exceed_the_derived_cap(
        self, tmp_path: Path, monkeypatch
    ):
        """Setting it outright means outright — the 30 cap is only a default."""
        sched = self._scheduler(tmp_path, monkeypatch, {"workers": 48}, max_workers=200)
        assert self._health_workers(sched) == 48

    def test_garbage_value_falls_back_to_derived(self, tmp_path: Path, monkeypatch):
        """A typo must not silently leave health with one thread."""
        sched = self._scheduler(tmp_path, monkeypatch, {"workers": "many"})
        assert self._health_workers(sched) == 10
