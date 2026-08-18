"""Tests for session-aware lookback windows (days_back vs files_back)."""

from datetime import datetime, timedelta

import pytest

from receivers.scheduling.lookback import (
    Lookback,
    LookbackConfigError,
    is_hourly_session,
)

END = datetime(2026, 8, 17, 15, 0, 0)


class TestIsHourlySession:
    @pytest.mark.parametrize(
        "session,expected",
        [
            ("15s_24hr", False),
            ("15s_24hr_rinex", False),
            ("1Hz_1hr", True),
            ("1Hz_1hr_rinex", True),
            ("status_1hr", True),
            ("20Hz_1hr", True),
            ("50Hz_1hr", True),
        ],
    )
    def test_classification(self, session, expected):
        assert is_hourly_session(session) is expected

    def test_unparseable_session_defaults_to_daily(self):
        # parse_session_parameters falls back to 15s/24hr for a name it cannot
        # split. Daily is the conservative default: a wider window costs time,
        # a wrongly-narrow one silently skips files.
        assert is_hourly_session("weird") is False


class TestFromConfig:
    def test_files_back_wins_when_only_one_present(self):
        lb = Lookback.from_config({"files_back": 7}, default_days=30)
        assert (lb.count, lb.unit) == (7, "files")

    def test_days_back_preserved(self):
        lb = Lookback.from_config({"days_back": 7}, default_days=30)
        assert (lb.count, lb.unit) == (7, "days")

    def test_neither_key_uses_default_days(self):
        # An untouched deployed config must keep behaving exactly as before.
        lb = Lookback.from_config({}, default_days=30)
        assert (lb.count, lb.unit) == (30, "days")

    def test_both_keys_is_a_hard_error(self):
        # Silent precedence is the same class of bug this module removes.
        with pytest.raises(LookbackConfigError) as exc:
            Lookback.from_config(
                {"days_back": 7, "files_back": 7},
                default_days=30,
                section_name="gap_detection",
            )
        assert "gap_detection" in str(exc.value)
        assert "not both" in str(exc.value)

    def test_explicit_zero_is_not_treated_as_absent(self):
        # `section.get(k) is not None`, not truthiness — files_back: 0 is a
        # meaningful "this run looks at nothing", not "fall back to default".
        lb = Lookback.from_config({"files_back": 0}, default_days=30)
        assert (lb.count, lb.unit) == (0, "files")

    def test_negative_count_rejected(self):
        with pytest.raises(LookbackConfigError):
            Lookback.from_config({"days_back": -1}, default_days=30)


class TestSpan:
    def test_files_back_is_hours_for_hourly(self):
        lb = Lookback(count=7, unit="files")
        assert lb.span("1Hz_1hr") == timedelta(hours=7)

    def test_files_back_is_days_for_daily(self):
        lb = Lookback(count=7, unit="files")
        assert lb.span("15s_24hr") == timedelta(days=7)

    def test_days_back_is_days_regardless_of_session(self):
        # The whole point of keeping the old key: it does NOT become
        # session-aware. epos_disseminate depends on this.
        lb = Lookback(count=7, unit="days")
        assert lb.span("15s_24hr") == timedelta(days=7)
        assert lb.span("1Hz_1hr") == timedelta(days=7)

    def test_the_reconciler_case(self):
        # scheduler.yaml's archive_reconciler comment complains that days_back:30
        # over 1Hz is ~239,000 files. files_back:30 makes it 30 hours.
        assert Lookback(30, "files").span("1Hz_1hr") == timedelta(hours=30)
        assert Lookback(30, "days").span("1Hz_1hr") == timedelta(days=30)


class TestWindow:
    def test_hourly_window(self):
        start, end = Lookback(7, "files").window("1Hz_1hr", END)
        assert end == END
        assert start == END - timedelta(hours=7)

    def test_daily_window(self):
        start, _ = Lookback(7, "files").window("15s_24hr", END)
        assert start == END - timedelta(days=7)


class TestDaysFor:
    def test_fractional_for_hourly_files_back(self):
        # Must NOT round up to 1 — that would restore a 24-file window.
        assert Lookback(7, "files").days_for("1Hz_1hr") == pytest.approx(7 / 24)

    def test_whole_for_daily(self):
        assert Lookback(7, "files").days_for("15s_24hr") == pytest.approx(7.0)


class TestDescribe:
    def test_mixed_sessions_expand_visibly(self):
        text = Lookback(7, "files").describe(["15s_24hr", "1Hz_1hr"])
        assert text == "files_back=7 -> 15s_24hr: 7d, 1Hz_1hr: 7h"

    def test_days_back_expansion(self):
        text = Lookback(7, "days").describe(["15s_24hr", "1Hz_1hr"])
        assert text == "days_back=7 -> 15s_24hr: 7d, 1Hz_1hr: 7d"


class TestFileCount:
    def test_files_back_is_literally_the_file_count(self):
        assert Lookback(7, "files").file_count("1Hz_1hr") == 7
        assert Lookback(7, "files").file_count("15s_24hr") == 7

    def test_days_back_expands_to_24_per_day_for_hourly(self):
        # The number the scheduler.yaml comment complains about: 30 days of 1Hz
        # is 720 files per station.
        assert Lookback(30, "days").file_count("1Hz_1hr") == 720
        assert Lookback(30, "days").file_count("15s_24hr") == 30

    def test_the_narrowing_is_24x(self):
        wide = Lookback(7, "days").file_count("1Hz_1hr")
        narrow = Lookback(7, "files").file_count("1Hz_1hr")
        assert wide == 168
        assert narrow == 7
        assert wide / narrow == 24


class TestDateSpanDays:
    def test_hourly_files_back_rounds_up_and_adds_straddle_day(self):
        # 7 hourly files fit in one day, but they straddle midnight for most of
        # the day, so enumerate 2.
        assert Lookback(7, "files").date_span_days("1Hz_1hr") == 2
        # 30 files = 1.25 days -> ceil 2, +1 straddle = 3
        assert Lookback(30, "files").date_span_days("1Hz_1hr") == 3
        assert Lookback(48, "files").date_span_days("1Hz_1hr") == 3

    def test_daily_is_unchanged(self):
        assert Lookback(7, "files").date_span_days("15s_24hr") == 7

    def test_days_back_is_always_its_own_count(self):
        assert Lookback(30, "days").date_span_days("1Hz_1hr") == 30
        assert Lookback(30, "days").date_span_days("15s_24hr") == 30


class TestWiringContract:
    """The consumers must keep accepting a Lookback.

    These are signature guards: the wiring is easy to undo by accident in a
    later refactor, and the failure mode is silent — the job falls back to
    days_back and quietly re-widens the window by 24x on hourly sessions.
    """

    def test_jobs_accept_lookback_kwarg(self):
        import inspect

        from receivers.scheduling.archive_reconciler import (
            _run_archive_reconciler_job,
        )
        from receivers.scheduling.gap_scheduler import _run_gap_detection_job
        from receivers.scheduling.integrity_checker import _run_integrity_check_job

        for fn in (
            _run_gap_detection_job,
            _run_archive_reconciler_job,
            _run_integrity_check_job,
        ):
            params = inspect.signature(fn).parameters
            assert "lookback" in params, f"{fn.__name__} lost its lookback kwarg"
            assert params["lookback"].default is None, (
                f"{fn.__name__}'s lookback must default to None so callers "
                f"passing a plain days_back int keep working"
            )

    def test_gap_summary_accepts_max_files(self):
        import inspect

        from receivers.health.file_tracker import GapDetector

        params = inspect.signature(GapDetector.get_gap_summary).parameters
        assert "max_files" in params
        assert params["max_files"].default is None

    def test_gap_detection_values_for_a_realistic_config(self):
        # What the wiring actually hands get_gap_summary for files_back: 7.
        lb = Lookback.from_config({"files_back": 7}, default_days=7)
        assert lb.date_span_days("1Hz_1hr") == 2  # enumerate 2 days...
        assert lb.file_count("1Hz_1hr") == 7  # ...then keep the newest 7
        assert lb.date_span_days("15s_24hr") == 7
        assert lb.file_count("15s_24hr") == 7

    def test_days_back_makes_max_files_a_noop(self):
        # Under days_back, max_files equals the full enumerated range, so the
        # trim cannot change anything and both units share one call path.
        lb = Lookback.from_config({"days_back": 7}, default_days=7)
        enumerated_hourly = lb.date_span_days("1Hz_1hr") * 24
        assert lb.file_count("1Hz_1hr") == enumerated_hourly == 168


class TestPerSessionOverrides:
    """`files_back: N` is N days for daily and N hours for hourly — one number
    rarely suits both, so a mapping form allows per-session counts."""

    CFG = {"files_back": {"default": 36, "15s_24hr": 7}}

    def test_mapping_parses(self):
        lb = Lookback.from_config(self.CFG, default_days=7)
        assert lb.unit == "files"
        assert lb.count == 36
        assert lb.count_for("15s_24hr") == 7
        assert lb.count_for("1Hz_1hr") == 36
        assert lb.count_for("status_1hr") == 36

    def test_spans_differ_per_session(self):
        lb = Lookback.from_config(self.CFG, default_days=7)
        assert lb.span("15s_24hr") == timedelta(days=7)
        assert lb.span("1Hz_1hr") == timedelta(hours=36)

    def test_file_count_and_date_span_follow_the_override(self):
        lb = Lookback.from_config(self.CFG, default_days=7)
        assert lb.file_count("15s_24hr") == 7
        assert lb.date_span_days("15s_24hr") == 7
        assert lb.file_count("1Hz_1hr") == 36
        assert lb.date_span_days("1Hz_1hr") == 3  # ceil(36/24)=2, +1 straddle

    def test_describe_shows_the_overrides(self):
        lb = Lookback.from_config(self.CFG, default_days=7)
        text = lb.describe(["15s_24hr", "1Hz_1hr", "status_1hr"])
        assert text == (
            "files_back=36 (15s_24hr=7) -> "
            "15s_24hr: 7d, 1Hz_1hr: 36h, status_1hr: 36h"
        )

    def test_mapping_without_default_is_rejected(self):
        # Otherwise a session absent from the mapping would silently get
        # whatever the caller's historical default happened to be.
        with pytest.raises(LookbackConfigError) as exc:
            Lookback.from_config(
                {"files_back": {"15s_24hr": 7}},
                default_days=7,
                section_name="gap_detection",
            )
        assert "no 'default'" in str(exc.value)

    def test_days_back_also_accepts_the_mapping_form(self):
        lb = Lookback.from_config(
            {"days_back": {"default": 30, "15s_24hr": 7}}, default_days=7
        )
        assert lb.unit == "days"
        # days_back stays frequency-blind: hourly still gets DAYS.
        assert lb.span("1Hz_1hr") == timedelta(days=30)
        assert lb.span("15s_24hr") == timedelta(days=7)

    def test_negative_override_rejected(self):
        with pytest.raises(LookbackConfigError):
            Lookback.from_config(
                {"files_back": {"default": 36, "15s_24hr": -1}}, default_days=7
            )

    def test_still_hashable_and_picklable(self):
        # Pickled into the APScheduler SQLite jobstore; a dict field would have
        # broken hashing on a frozen dataclass.
        import pickle

        lb = Lookback.from_config(self.CFG, default_days=7)
        assert hash(lb) is not None
        assert pickle.loads(pickle.dumps(lb)) == lb


class TestScalarIntConsumers:
    """Some consumers still need a plain int, and a mapping must not leak into
    them. rinex_days_back is the one that bit: it feeds timedelta(days=...),
    and reading the raw files_back key handed it a dict."""

    RECONCILER = {"files_back": {"default": 36, "15s_24hr": 30}}

    def test_reconciler_daily_reach_is_an_int(self):
        lb = Lookback.from_config(self.RECONCILER, default_days=30)
        v = lb.date_span_days("15s_24hr")
        assert isinstance(v, int)
        assert v == 30  # same value the old hardcoded fallback used

    def test_it_survives_timedelta(self):
        from datetime import date, timedelta

        lb = Lookback.from_config(self.RECONCILER, default_days=30)
        # This is the exact expression that raised on a dict.
        assert date.today() - timedelta(days=lb.date_span_days("15s_24hr"))

    def test_scalar_form_gives_the_same_shape(self):
        lb = Lookback.from_config({"files_back": 36}, default_days=30)
        assert isinstance(lb.date_span_days("15s_24hr"), int)
