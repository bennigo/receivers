"""Tests for the BNC stream supervisor (mocked process listing/spawning)."""

from receivers.streaming.supervisor import StreamSupervisor


def _touch_configs(config_dir, *station_ids):
    for sid in station_ids:
        (config_dir / f"rtcm2rinex-{sid}.bnc").write_text("[General]\n")


class TestDiscovery:
    def test_configured_stations_from_files(self, tmp_path):
        _touch_configs(tmp_path, "GONH", "MOHA", "HVER")
        sup = StreamSupervisor("/usr/bin/bnc", tmp_path)
        assert sup.configured_stations() == ["GONH", "HVER", "MOHA"]

    def test_configured_stations_empty_dir(self, tmp_path):
        sup = StreamSupervisor("/usr/bin/bnc", tmp_path / "missing")
        assert sup.configured_stations() == []

    def test_running_stations_parsed_from_cmdlines(self, tmp_path):
        cmdlines = [
            "/usr/bin/bnc --conf /cfg/rtcm2rinex-GONH.bnc -nw",
            "/usr/bin/bnc --conf /cfg/rtcm2rinex-MOHA.bnc -nw",
            "sshd: gpsops@pts/0",
        ]
        sup = StreamSupervisor(
            "/usr/bin/bnc", tmp_path, process_lister=lambda: cmdlines
        )
        assert sup.running_stations() == ["GONH", "MOHA"]


class TestStart:
    def test_start_missing_config_returns_false(self, tmp_path):
        spawned = []
        sup = StreamSupervisor("/usr/bin/bnc", tmp_path, spawner=spawned.append)
        assert sup.start_station("GONH") is False
        assert spawned == []

    def test_start_spawns_correct_command(self, tmp_path):
        _touch_configs(tmp_path, "GONH")
        spawned = []
        sup = StreamSupervisor("/usr/bin/bnc", tmp_path, spawner=spawned.append)
        assert sup.start_station("GONH") is True
        assert spawned == [
            ["/usr/bin/bnc", "--conf", str(tmp_path / "rtcm2rinex-GONH.bnc"), "-nw"]
        ]


class TestSupervise:
    def test_starts_only_missing(self, tmp_path):
        _touch_configs(tmp_path, "GONH", "MOHA", "HVER")
        spawned = []
        sup = StreamSupervisor(
            "/usr/bin/bnc",
            tmp_path,
            process_lister=lambda: ["bnc --conf /x/rtcm2rinex-MOHA.bnc -nw"],
            spawner=spawned.append,
        )
        result = sup.supervise()
        assert result.configured == ["GONH", "HVER", "MOHA"]
        assert result.running == ["MOHA"]
        assert result.started == ["GONH", "HVER"]
        assert result.failed == []
        assert len(spawned) == 2

    def test_all_running_no_starts(self, tmp_path):
        _touch_configs(tmp_path, "GONH")
        spawned = []
        sup = StreamSupervisor(
            "/usr/bin/bnc",
            tmp_path,
            process_lister=lambda: ["bnc --conf /x/rtcm2rinex-GONH.bnc -nw"],
            spawner=spawned.append,
        )
        result = sup.supervise()
        assert result.started == []
        assert result.all_running is True
        assert spawned == []

    def test_spawn_failure_recorded(self, tmp_path):
        _touch_configs(tmp_path, "GONH")

        def boom(_cmd):
            raise OSError("no bnc binary")

        sup = StreamSupervisor(
            "/usr/bin/bnc", tmp_path, process_lister=list, spawner=boom
        )
        result = sup.supervise()
        assert result.started == []
        assert result.failed == ["GONH"]
        assert result.all_running is False
