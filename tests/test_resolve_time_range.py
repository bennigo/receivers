"""`resolve_time_range` must reproduce each verb's existing window exactly.

The "explicit start/end, else --days lookback, else fill in the missing bound"
ritual was written out longhand in both `cmd_download` and `cmd_rinex`, and the
two copies had drifted apart in THREE ways. Consolidating them into one helper
is only safe if the helper can still express both behaviours, so these tests
pin each verb's flag combination — and, deliberately, pin the divergence too.

If someone later decides the two verbs should agree, the tests marked
DIVERGENCE below are the ones that must change, and changing them is the
signal that user-visible behaviour moved.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from receivers.utils.time_utils import resolve_time_range

# The flag sets the two call sites pass. Keep in sync with cli/main.py.
DOWNLOAD = dict(
    case_insensitive_session=False,
    infer_start_from_end=True,
    inclusive_same_day=False,
)
RINEX = dict(
    case_insensitive_session=True,
    infer_start_from_end=False,
    inclusive_same_day=True,
)

T0 = datetime(2026, 8, 26, 12, 0, 0)


class TestSharedBehaviour:
    """Things both verbs have always done identically."""

    @pytest.mark.parametrize("flags", [DOWNLOAD, RINEX], ids=["download", "rinex"])
    def test_explicit_start_derives_a_daily_end(self, flags):
        start, end, rev = resolve_time_range("15s_24hr", T0, None, None, **flags)
        assert (start, end) == (T0, T0 + timedelta(days=1))
        assert rev is False

    @pytest.mark.parametrize("flags", [DOWNLOAD, RINEX], ids=["download", "rinex"])
    def test_explicit_start_derives_an_hourly_end(self, flags):
        start, end, _ = resolve_time_range("1Hz_1hr", T0, None, None, **flags)
        assert end == T0 + timedelta(hours=1)

    @pytest.mark.parametrize("flags", [DOWNLOAD, RINEX], ids=["download", "rinex"])
    def test_both_bounds_given_are_left_alone(self, flags):
        later = T0 + timedelta(days=3)
        start, end, rev = resolve_time_range("15s_24hr", T0, later, None, **flags)
        assert (start, end, rev) == (T0, later, False)

    @pytest.mark.parametrize("flags", [DOWNLOAD, RINEX], ids=["download", "rinex"])
    def test_days_lookback_is_reverse_chronological(self, flags):
        start, end, rev = resolve_time_range("15s_24hr", None, None, 3, **flags)
        assert rev is True, "--days prioritises the most recent data"
        assert start is not None and end is not None and start < end

    @pytest.mark.parametrize("flags", [DOWNLOAD, RINEX], ids=["download", "rinex"])
    def test_explicit_start_wins_over_days(self, flags):
        start, _, rev = resolve_time_range("15s_24hr", T0, None, 7, **flags)
        assert start == T0
        assert rev is False, "an explicit start is not a lookback"


class TestDivergence:
    """DIVERGENCE — the two verbs genuinely differ. Pre-existing, preserved.

    Changing any assertion here changes what files a given invocation selects.
    """

    def test_end_only_download_infers_a_start_but_rinex_does_not(self):
        d_start, d_end, _ = resolve_time_range("15s_24hr", None, T0, None, **DOWNLOAD)
        r_start, r_end, _ = resolve_time_range("15s_24hr", None, T0, None, **RINEX)

        assert d_start == T0 - timedelta(days=1), "download fills in the start"
        assert d_end == T0
        assert r_start is None, "rinex has no such branch — start stays None"
        assert r_end == T0

    def test_equal_bounds_rinex_widens_download_leaves_an_empty_window(self):
        d_start, d_end, _ = resolve_time_range("15s_24hr", T0, T0, None, **DOWNLOAD)
        r_start, r_end, _ = resolve_time_range("15s_24hr", T0, T0, None, **RINEX)

        assert d_start == d_end == T0, "download leaves start == end (empty range)"
        assert r_end == T0 + timedelta(days=1), "rinex widens to an inclusive day"

    def test_session_case_changes_the_period_between_verbs(self):
        """`1Hz_1HR` is hourly to rinex and DAILY to download."""
        _, d_end, _ = resolve_time_range("1Hz_1HR", T0, None, None, **DOWNLOAD)
        _, r_end, _ = resolve_time_range("1Hz_1HR", T0, None, None, **RINEX)

        assert d_end == T0 + timedelta(days=1), (
            "download matches '1hr' case-sensitively"
        )
        assert r_end == T0 + timedelta(hours=1), "rinex lowercases first"
        assert d_end != r_end, "same input, different window — this is the bug"


def test_no_bounds_and_no_days_returns_nothing():
    assert resolve_time_range("15s_24hr", None, None, None, **DOWNLOAD) == (
        None,
        None,
        False,
    )
