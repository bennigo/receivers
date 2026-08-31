"""Output-freshness detection in the BNC stream supervisor (#168).

Liveness is not health. HRIC's BNC daemon ran continuously from 2026-08-30 07:02
while writing zero RINEX and logging 175-260 "Wrong caster response" per day —
the station itself had gone flat (#167) — and every supervision pass reported it
running and healthy. Nothing escalated for over 27 hours.

So a running station whose newest ``.rnx`` is older than ``stale_after`` is now
flagged. Flagged, not restarted: bouncing BNC would not have revived a station
with a dead battery, and would have destroyed the only evidence that anything
was wrong.
"""

import logging
from datetime import UTC, datetime, timedelta

from receivers.streaming.supervisor import (
    DEFAULT_STALE_AFTER,
    StreamSupervisor,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _mk(tmp_path, stations, *, running=(), ages=None):
    """Build a supervisor over a fake config dir + rt_base."""
    cfg_dir = tmp_path / "bnc"
    cfg_dir.mkdir(exist_ok=True)
    rt_base = tmp_path / "rt"
    rt_base.mkdir(exist_ok=True)

    for sid in stations:
        (cfg_dir / f"rtcm2rinex-{sid}.bnc").write_text("[General]\n")

    for sid, age in (ages or {}).items():
        d = rt_base / sid
        d.mkdir(parents=True, exist_ok=True)
        if age is None:  # directory exists but no output at all
            continue
        f = d / f"{sid}00ISL_S_20262431000_01H_MO.rnx"
        f.write_text("data")
        mtime = (NOW - age).timestamp()
        import os

        os.utime(f, (mtime, mtime))

    cmdlines = [f"bnc --conf {cfg_dir}/rtcm2rinex-{s}.bnc -nw" for s in running]
    return StreamSupervisor(
        "bnc",
        cfg_dir,
        process_lister=lambda: cmdlines,
        spawner=lambda cmd: None,
        rt_base=rt_base,
        now=lambda: NOW,
    )


def test_fresh_output_is_not_stale(tmp_path):
    sup = _mk(tmp_path, ["GONH"], running=["GONH"], ages={"GONH": timedelta(minutes=5)})
    assert sup.supervise().stale == []


def test_mute_but_running_station_is_flagged(tmp_path):
    """The HRIC case: process alive, no output for a day."""
    sup = _mk(tmp_path, ["HRIC"], running=["HRIC"], ages={"HRIC": timedelta(hours=29)})
    result = sup.supervise()
    assert result.stale == ["HRIC"]
    # liveness alone still looks fine — which is exactly the trap
    assert result.all_running is True
    assert result.all_healthy is False


def test_stale_station_is_not_restarted(tmp_path):
    """Staleness must not trigger a respawn — HRIC had no power to come back."""
    started = []
    sup = _mk(tmp_path, ["HRIC"], running=["HRIC"], ages={"HRIC": timedelta(hours=29)})
    sup._spawn = lambda cmd: started.append(cmd)
    result = sup.supervise()
    assert result.stale == ["HRIC"]
    assert result.started == []
    assert started == []


def test_just_started_station_is_not_flagged(tmp_path):
    """A station started this pass has no output yet — never a false positive."""
    sup = _mk(tmp_path, ["GONH"], running=[], ages={"GONH": None})
    result = sup.supervise()
    assert result.started == ["GONH"]
    assert result.stale == []


def test_boundary_just_inside_threshold(tmp_path):
    sup = _mk(
        tmp_path, ["GONH"], running=["GONH"], ages={"GONH": DEFAULT_STALE_AFTER / 2}
    )
    assert sup.supervise().stale == []


def test_boundary_just_past_threshold(tmp_path):
    sup = _mk(
        tmp_path,
        ["GONH"],
        running=["GONH"],
        ages={"GONH": DEFAULT_STALE_AFTER + timedelta(minutes=1)},
    )
    assert sup.supervise().stale == ["GONH"]


def test_no_rt_base_degrades_to_liveness_only(tmp_path):
    """Unconfigured rt_base must not mark every station stale."""
    cfg_dir = tmp_path / "bnc"
    cfg_dir.mkdir()
    (cfg_dir / "rtcm2rinex-GONH.bnc").write_text("[General]\n")
    sup = StreamSupervisor(
        "bnc",
        cfg_dir,
        process_lister=lambda: [f"bnc --conf {cfg_dir}/rtcm2rinex-GONH.bnc -nw"],
        spawner=lambda cmd: None,
        now=lambda: NOW,
    )
    result = sup.supervise()
    assert result.stale == []
    assert sup.last_output_at("GONH") is None


def test_missing_station_dir_is_not_stale(tmp_path):
    """ "Cannot tell" is not "stale"."""
    sup = _mk(tmp_path, ["GONH"], running=["GONH"], ages={})
    assert sup.supervise().stale == []


def test_warning_names_the_station_and_points_at_the_log(tmp_path, caplog):
    sup = _mk(tmp_path, ["HRIC"], running=["HRIC"], ages={"HRIC": timedelta(hours=29)})
    with caplog.at_level(logging.WARNING):
        sup.supervise()
    assert "HRIC" in caplog.text
    assert "RUNNING BUT MUTE" in caplog.text
    assert "RinexObs.log" in caplog.text


def test_last_output_at_picks_the_newest(tmp_path):
    sup = _mk(tmp_path, ["GONH"], running=["GONH"], ages={"GONH": timedelta(hours=29)})
    import os

    d = tmp_path / "rt" / "GONH"
    newer = d / "GONH00ISL_S_20262431100_01H_MO.rnx"
    newer.write_text("x")
    ts = (NOW - timedelta(minutes=1)).timestamp()
    os.utime(newer, (ts, ts))
    assert sup.supervise().stale == []


# ── warning rate-limit ───────────────────────────────────────────────────────
#
# The mute condition persists until someone visits the site, so warning every
# pass is 144 lines/day at a 10-minute interval and 720 at 2 minutes. Only the
# LOG is throttled: `stale` stays fully populated so nothing downstream sees a
# suppressed condition.


def _mute(tmp_path, state, *, now=NOW, warn_every=timedelta(hours=1)):
    sup = _mk(tmp_path, ["HRIC"], running=["HRIC"], ages={"HRIC": timedelta(hours=29)})
    sup._warn_state = state
    sup.warn_every = warn_every
    sup._now = lambda: now
    return sup


def _warn_lines(caplog):
    return [r for r in caplog.records if "RUNNING BUT MUTE" in r.getMessage()]


def test_first_occurrence_warns(tmp_path, caplog):
    state = {}
    with caplog.at_level(logging.DEBUG):
        _mute(tmp_path, state).supervise()
    assert len(_warn_lines(caplog)) == 1
    assert "HRIC" in state


def test_repeat_within_the_window_is_suppressed(tmp_path, caplog):
    state = {"HRIC": NOW - timedelta(minutes=10)}
    with caplog.at_level(logging.DEBUG):
        _mute(tmp_path, state).supervise()
    assert _warn_lines(caplog) == []


def test_repeat_after_the_window_warns_again(tmp_path, caplog):
    state = {"HRIC": NOW - timedelta(hours=2)}
    with caplog.at_level(logging.DEBUG):
        _mute(tmp_path, state).supervise()
    assert len(_warn_lines(caplog)) == 1
    assert state["HRIC"] == NOW


def test_suppression_does_not_hide_the_condition(tmp_path, caplog):
    """`stale` must stay populated even when the log line is throttled."""
    state = {"HRIC": NOW - timedelta(minutes=10)}
    with caplog.at_level(logging.DEBUG):
        result = _mute(tmp_path, state).supervise()
    assert result.stale == ["HRIC"]
    assert result.all_healthy is False


def test_recovery_clears_the_stamp_so_a_new_outage_warns_at_once(tmp_path, caplog):
    """A station that recovers must not be silenced by a stale stamp later."""
    state = {"GONH": NOW - timedelta(minutes=1)}
    sup = _mk(tmp_path, ["GONH"], running=["GONH"], ages={"GONH": timedelta(minutes=5)})
    sup._warn_state = state
    sup._now = lambda: NOW
    sup.supervise()
    assert "GONH" not in state  # forgotten on recovery

    sup2 = _mute(tmp_path, state)  # HRIC dir, but reuse the shared state
    with caplog.at_level(logging.DEBUG):
        sup2.supervise()
    assert len(_warn_lines(caplog)) == 1


def test_rate_limit_is_per_station(tmp_path, caplog):
    """One station being throttled must not silence another."""
    sup = _mk(
        tmp_path,
        ["GONH", "HRIC"],
        running=["GONH", "HRIC"],
        ages={"GONH": timedelta(hours=29), "HRIC": timedelta(hours=29)},
    )
    sup._warn_state = {"GONH": NOW - timedelta(minutes=5)}
    sup._now = lambda: NOW
    with caplog.at_level(logging.DEBUG):
        result = sup.supervise()
    warned = {r.getMessage().split()[1] for r in _warn_lines(caplog)}
    assert warned == {"HRIC"}
    assert result.stale == ["GONH", "HRIC"]


def test_state_is_shared_across_supervisor_instances_by_default():
    """The scheduler builds a new supervisor per pass — the limit must survive."""
    from receivers.streaming import supervisor as mod

    a = StreamSupervisor("bnc", "/nonexistent")
    b = StreamSupervisor("bnc", "/nonexistent")
    assert a._warn_state is b._warn_state is mod._LAST_STALE_WARNING
