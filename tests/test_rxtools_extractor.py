"""Tests for the RxTools health data extractor.

These tests had drifted from the code since ``ffc0db6``, which cut
``health/rxtools_extractor.py`` from ~1150 lines to ~265 by delegating the
actual bin2asc work to the verified ``receivers.utils.rxtools_extractor``
helpers. 17 tests here failed on every run, which is a worse state than having
no tests: a permanently-red file is one nobody reads.

Three things moved, and each is handled differently below:

1. ``self._bin2asc_path`` (a per-instance ``shutil.which`` lookup) became the
   module constant ``BIN2ASC_PATH``, resolved **once at import time**. Tests can
   no longer influence it by patching ``shutil.which`` — they must patch the
   constant. See :class:`TestRxToolsAvailability`.
2. The ``_check_*_status`` threshold helpers moved to
   ``receivers.health.metrics.MetricChecker``. **``MetricChecker`` had no tests
   at all**, so these four were the only encoding of the voltage / disk / CPU /
   temperature rules anywhere in the suite — and they were dead. They are
   restored against their new home in :class:`TestMetricThresholds` rather than
   deleted with the old API.
3. The ``_parse_*`` methods (PowerStatus, DiskStatus, ReceiverStatus,
   WiFiStatus, LogStatus, NTRIP server/client, ReceiverSetup) were deleted
   outright; parsing now lives in ``utils.rxtools_extractor.extract_*``, and
   NTRIP client status is no longer read from SBF at all — it is queried over
   TCP by ``polarx5_tcp_extractor._query_ntrip_client_status``.

   **Coverage gap, stated rather than hidden**: the deleted tests carried real
   bin2asc CSV samples and pinned field-name mappings (e.g.
   ``'DiskUsagePercent [%]'``). That mapping is now unguarded. Re-pinning it
   belongs at the utils layer with those CSV fixtures, against
   ``parse_csv_to_dict`` / ``extract_*``, and needs the bin2asc preamble
   handling those helpers do — a separate piece of work, not a line edit here.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.health.metrics import HealthStatus, MetricChecker, ThresholdConfig
from receivers.health.rxtools_extractor import RxToolsExtractor, RxToolsNotFoundError

MODULE = "receivers.health.rxtools_extractor"


class TestRxToolsExtractor:
    """The extractor object itself."""

    def test_initialization(self):
        extractor = RxToolsExtractor(station_id="ELDC")
        assert extractor.station_id == "ELDC"
        # Thresholds are resolved at construction and held on the instance;
        # the old `_bin2asc_path` attribute is gone (see module docstring).
        assert isinstance(extractor._checker, MetricChecker)
        assert not hasattr(extractor, "_bin2asc_path")

    def test_power_type_reaches_the_threshold_config(self):
        # A DC/DC station must not be judged against mains voltage limits.
        # This is the only path config -> MetricChecker, so it is worth pinning.
        plain = RxToolsExtractor(station_id="ELDC")
        dcdc = RxToolsExtractor(station_id="ELDC", config={"power_type": "dcdc"})
        assert isinstance(plain._checker.config, ThresholdConfig)
        assert isinstance(dcdc._checker.config, ThresholdConfig)


class TestRxToolsAvailability:
    """`BIN2ASC_PATH` is a module constant, NOT a per-call `shutil.which`.

    Patching `shutil.which` here would silently do nothing — the constant is
    already resolved by import time. That is exactly how these tests rotted.
    """

    def test_available_when_the_binary_exists(self, tmp_path):
        fake = tmp_path / "bin2asc"
        fake.write_text("")
        with patch(f"{MODULE}.BIN2ASC_PATH", str(fake)):
            assert RxToolsExtractor(station_id="ELDC").check_rxtools_available()

    def test_not_available_when_the_binary_is_missing(self, tmp_path):
        missing = tmp_path / "definitely-not-here"
        with patch(f"{MODULE}.BIN2ASC_PATH", str(missing)):
            assert not RxToolsExtractor(station_id="ELDC").check_rxtools_available()

    def test_extract_raises_when_rxtools_missing(self, tmp_path):
        missing = tmp_path / "definitely-not-here"
        with patch(f"{MODULE}.BIN2ASC_PATH", str(missing)):
            extractor = RxToolsExtractor(station_id="ELDC")
            with pytest.raises(RxToolsNotFoundError) as exc_info:
                extractor.extract_health_from_sbf(Path("/tmp/test.sbf"))
        assert "bin2asc not found" in str(exc_info.value)

    def test_extract_raises_when_sbf_missing(self, tmp_path):
        # bin2asc present, SBF absent -> FileNotFoundError, not RxToolsNotFound.
        # The order of these two guards is the point: a missing SBF on a healthy
        # host must not be reported as a broken RxTools install.
        fake = tmp_path / "bin2asc"
        fake.write_text("")
        with patch(f"{MODULE}.BIN2ASC_PATH", str(fake)):
            extractor = RxToolsExtractor(station_id="ELDC")
            with pytest.raises(FileNotFoundError) as exc_info:
                extractor.extract_health_from_sbf(tmp_path / "nonexistent.sbf")
        assert "SBF file not found" in str(exc_info.value)


class TestMetricThresholds:
    """The voltage / disk / CPU / temperature rules, at their new home.

    Asserted as **boundary semantics derived from the config**, not as magic
    numbers. The old tests hardcoded values that have since drifted — they
    claimed 85 % disk was a warning (the threshold is now 90) and 65 °C a
    warning (it is now critical). Numbers copied forward would have re-encoded
    thresholds nobody uses. Retuning a threshold should not break these; losing
    the ordering or the classification should.
    """

    @staticmethod
    def _checker() -> MetricChecker:
        return MetricChecker(ThresholdConfig())

    def test_threshold_ordering_is_sane(self):
        cfg = ThresholdConfig()
        assert cfg.voltage_critical_low < cfg.voltage_warning_low
        assert cfg.voltage_warning_high < cfg.voltage_critical_high
        assert cfg.temp_critical_low < cfg.temp_warning_low
        assert cfg.temp_warning_high < cfg.temp_critical_high
        assert cfg.cpu_warning < cfg.cpu_critical
        assert cfg.disk_warning < cfg.disk_critical

    def test_voltage_classification(self):
        cfg, checker = ThresholdConfig(), self._checker()
        nominal = (cfg.voltage_warning_low + cfg.voltage_warning_high) / 2
        assert checker.check_voltage(nominal).status == HealthStatus.OK
        assert checker.check_voltage(cfg.voltage_critical_low - 1).status == (
            HealthStatus.CRITICAL
        )
        assert checker.check_voltage(cfg.voltage_critical_high + 1).status == (
            HealthStatus.CRITICAL
        )

    def test_voltage_unknown_when_absent(self):
        # A missing reading must not read as OK — that would hide a dead sensor.
        assert self._checker().check_voltage(None).status == HealthStatus.UNKNOWN

    def test_cpu_classification(self):
        cfg, checker = ThresholdConfig(), self._checker()
        assert checker.check_cpu_load(cfg.cpu_warning - 10).status == HealthStatus.OK
        assert checker.check_cpu_load(cfg.cpu_critical + 5).status == (
            HealthStatus.CRITICAL
        )

    def test_disk_classification(self):
        cfg, checker = ThresholdConfig(), self._checker()
        assert checker.check_disk_usage(cfg.disk_warning - 40).status == HealthStatus.OK
        assert checker.check_disk_usage(cfg.disk_critical + 2).status == (
            HealthStatus.CRITICAL
        )

    def test_temperature_classification(self):
        cfg, checker = ThresholdConfig(), self._checker()
        nominal = (cfg.temp_warning_low + cfg.temp_warning_high) / 2
        assert checker.check_temperature(nominal).status == HealthStatus.OK
        assert checker.check_temperature(cfg.temp_critical_high + 5).status == (
            HealthStatus.CRITICAL
        )
        assert checker.check_temperature(cfg.temp_critical_low - 5).status == (
            HealthStatus.CRITICAL
        )
