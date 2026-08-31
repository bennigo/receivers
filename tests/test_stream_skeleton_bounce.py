"""BNC must be bounced when its stored ``.SKL`` changes (#166).

BNC caches the skeleton at process start — it does NOT re-read it per file
write, despite what this module's docstring used to claim. Measured on rek-d01
2026-08-31: the 06:00 refresh stripped a bad ``MARKER NUMBER`` row from all
three SKLs, yet the 09:00 hourly file — created three hours later — still
carried the old value. Bouncing the daemons produced a correct header on the
very next file.

So every skeleton refresh was silently inert: an antenna swap, receiver change
or firmware update written to the SKL never reached published headers until BNC
happened to restart for unrelated reasons.

The stop half is the risky part. It kills live capture, so matching is exact on
the station id and requires the BNC config flag — never a bare substring. A
`pkill -f`-style pattern match killed unrelated processes twice during the
session that found this bug.
"""

from types import SimpleNamespace

import pytest

from receivers.streaming.supervisor import StreamSupervisor


def _mk(tmp_path, stations, *, procs=()):
    """procs: list of (pid, cmdline)."""
    cfg_dir = tmp_path / "bnc"
    cfg_dir.mkdir(exist_ok=True)
    for sid in stations:
        (cfg_dir / f"rtcm2rinex-{sid}.bnc").write_text("[General]\n")
    killed, spawned = [], []
    sup = StreamSupervisor(
        "bnc",
        cfg_dir,
        spawner=lambda cmd: spawned.append(cmd),
        pid_lister=lambda: list(procs),
        killer=lambda pid: killed.append(pid),
    )
    return sup, killed, spawned


def _cmd(tmp_path, sid):
    return f"bnc --conf {tmp_path}/bnc/rtcm2rinex-{sid}.bnc -nw"


# ── pid matching: the dangerous part ─────────────────────────────────────────


def test_matches_only_the_named_station(tmp_path):
    procs = [(101, _cmd(tmp_path, "GONH")), (102, _cmd(tmp_path, "HRIC"))]
    sup, _, _ = _mk(tmp_path, ["GONH", "HRIC"], procs=procs)
    assert sup.pids_for("GONH") == [101]
    assert sup.pids_for("HRIC") == [102]


def test_station_id_is_not_matched_as_a_substring(tmp_path):
    """A station whose id contains another's must not take its neighbour down."""
    procs = [(201, _cmd(tmp_path, "GON")), (202, _cmd(tmp_path, "GONH"))]
    sup, _, _ = _mk(tmp_path, ["GON", "GONH"], procs=procs)
    assert sup.pids_for("GON") == [201]
    assert sup.pids_for("GONH") == [202]


def test_ignores_processes_that_merely_mention_the_station(tmp_path):
    """The self-match hazard: a grep/ps command quoting the pattern is not BNC."""
    procs = [
        (301, "grep rtcm2rinex-GONH.bnc /var/log/syslog"),
        (302, f"tail -f {tmp_path}/rt/GONH/RinexObs.log"),
    ]
    sup, _, _ = _mk(tmp_path, ["GONH"], procs=procs)
    assert sup.pids_for("GONH") == []


def test_never_returns_our_own_pid(tmp_path):
    import os

    procs = [(os.getpid(), _cmd(tmp_path, "GONH"))]
    sup, _, _ = _mk(tmp_path, ["GONH"], procs=procs)
    assert sup.pids_for("GONH") == []


# ── stop / bounce ────────────────────────────────────────────────────────────


def test_stop_signals_the_right_pid(tmp_path):
    procs = [(101, _cmd(tmp_path, "GONH")), (102, _cmd(tmp_path, "HRIC"))]
    sup, killed, _ = _mk(tmp_path, ["GONH", "HRIC"], procs=procs)
    assert sup.stop_station("GONH") == 1
    assert killed == [101]


def test_stop_when_nothing_running_is_a_noop(tmp_path):
    sup, killed, _ = _mk(tmp_path, ["GONH"], procs=[])
    assert sup.stop_station("GONH") == 0
    assert killed == []


def test_bounce_stops_then_starts(tmp_path):
    procs = [(101, _cmd(tmp_path, "GONH"))]
    sup, killed, spawned = _mk(tmp_path, ["GONH"], procs=procs)
    assert sup.bounce_station("GONH") is True
    assert killed == [101]
    assert len(spawned) == 1
    assert "rtcm2rinex-GONH.bnc" in " ".join(spawned[0])


def test_bounce_starts_even_if_not_currently_running(tmp_path):
    """A dead daemon must still come back — bounce is stop-then-start."""
    sup, killed, spawned = _mk(tmp_path, ["GONH"], procs=[])
    assert sup.bounce_station("GONH") is True
    assert killed == []
    assert len(spawned) == 1


def test_bounce_reports_failure_when_config_missing(tmp_path):
    procs = [(101, _cmd(tmp_path, "GONH"))]
    sup, killed, spawned = _mk(tmp_path, ["GONH"], procs=procs)
    (tmp_path / "bnc" / "rtcm2rinex-GONH.bnc").unlink()
    assert sup.bounce_station("GONH") is False
    assert spawned == []


def test_kill_failure_is_survived(tmp_path):
    """A pid that vanished between listing and kill must not abort the bounce."""

    def boom(pid):
        raise ProcessLookupError(pid)

    cfg_dir = tmp_path / "bnc"
    cfg_dir.mkdir()
    (cfg_dir / "rtcm2rinex-GONH.bnc").write_text("[General]\n")
    spawned = []
    sup = StreamSupervisor(
        "bnc",
        cfg_dir,
        spawner=lambda cmd: spawned.append(cmd),
        pid_lister=lambda: [(101, _cmd(tmp_path, "GONH"))],
        killer=boom,
    )
    assert sup.stop_station("GONH") == 0
    assert sup.bounce_station("GONH") is True
    assert len(spawned) == 1


# ── the refresh job wiring ───────────────────────────────────────────────────
#
# The supervisor tests above prove bounce_station works; these prove the refresh
# job actually CALLS it, and only for stations whose skeleton really changed.
# Without them the fix could be deleted from the job and every test still pass.


@pytest.fixture
def refresh_job(monkeypatch):
    """Run _run_stream_config_refresh_job with every outbound dep stubbed."""
    from receivers.scheduling import stream_scheduler as ss

    bounced = []

    class FakeSupervisor:
        def __init__(self, *a, **k):
            pass

        def bounce_station(self, sid):
            bounced.append(sid)
            return True

    def run(outcomes):
        cfgs = {sid: {"station_id": sid} for sid in outcomes}
        monkeypatch.setattr(ss, "enumerate_stream_stations", lambda c: sorted(outcomes))
        settings = SimpleNamespace(
            bnc_path="bnc", bnc_config_dir="/nonexistent/bnc", rt_base="/nonexistent/rt"
        )
        monkeypatch.setattr(ss, "load_stream_settings", lambda: settings)
        monkeypatch.setattr(ss, "_make_tos_metadata_provider", lambda: lambda s: {})
        monkeypatch.setattr(ss, "generate_bnc_config_file", lambda *a, **k: None)
        monkeypatch.setattr(
            ss, "refresh_station_skeleton", lambda sid, *a, **k: outcomes[sid]
        )
        monkeypatch.setattr(ss, "StreamSupervisor", FakeSupervisor)
        monkeypatch.setattr(
            "receivers.cli.main.get_all_station_configs", lambda: cfgs, raising=False
        )
        bounced.clear()
        ss._run_stream_config_refresh_job()
        return bounced

    return run


def test_job_bounces_a_station_whose_skeleton_was_updated(refresh_job):
    assert refresh_job({"GONH": "updated"}) == ["GONH"]


def test_job_bounces_a_newly_created_skeleton(refresh_job):
    assert refresh_job({"SEY9": "created"}) == ["SEY9"]


def test_job_does_not_bounce_an_unchanged_skeleton(refresh_job):
    """A no-op refresh must not interrupt live capture."""
    assert refresh_job({"GONH": "unchanged"}) == []


def test_job_does_not_bounce_on_failed_refresh(refresh_job):
    """no_tos / no_position mean nothing was written — nothing to pick up."""
    assert refresh_job({"GONH": "no_tos", "HRIC": "no_position"}) == []


def test_job_bounces_only_the_changed_stations(refresh_job):
    assert refresh_job({"GONH": "updated", "HRIC": "unchanged", "SEY9": "created"}) == [
        "GONH",
        "SEY9",
    ]
