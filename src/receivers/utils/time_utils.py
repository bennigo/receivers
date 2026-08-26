"""Time range calculation utilities for download operations.

Thin adapters over ``gtimes.timefunc`` that translate receivers-specific
session vocabulary (``'15s_24hr'`` → daily, everything else → hourly) into
generic ``period`` timedeltas. The actual time math (previous-complete-period
alignment, datetime list generation, period iteration) lives in gtimes where
other projects in the ecosystem can reuse it.

See ``gtimes.timefunc.generate_time_range`` for the canonical implementation.
"""

from datetime import datetime, timedelta
from typing import List, Tuple

from gtimes.timefunc import (
    generate_datetime_list as _gt_generate_datetime_list,
)
from gtimes.timefunc import (
    generate_period_ranges as _gt_generate_period_ranges,
)
from gtimes.timefunc import (
    generate_time_range as _gt_generate_time_range,
)


def _session_period(session_type: str) -> timedelta:
    """Map a receivers session_type to a generic period timedelta."""
    if session_type == "15s_24hr":
        return timedelta(days=1)
    return timedelta(hours=1)


def calculate_download_time_range(
    session_type: str,
    lookback_periods: int,
) -> Tuple[datetime, datetime]:
    """Calculate download time range for a session type.

    Ends at the start of the most recently completed period (so an in-progress
    file on the receiver is not pulled mid-write). See
    ``gtimes.timefunc.generate_time_range`` for the generic version.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr).
        lookback_periods: Number of complete periods to include.

    Returns:
        ``(start, end)`` — ``end`` is exclusive.
    """
    return _gt_generate_time_range(_session_period(session_type), lookback_periods)


def generate_download_datetimes(
    session_type: str,
    lookback_periods: int,
    reverse_chronological: bool = False,
) -> List[datetime]:
    """Generate list of datetimes to download.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr).
        lookback_periods: Number of periods to include.
        reverse_chronological: If True, newest-first (CLI -D behaviour).
    """
    period = _session_period(session_type)
    start, end = _gt_generate_time_range(period, lookback_periods)
    return _gt_generate_datetime_list(start, end, period, reverse=reverse_chronological)


def get_session_frequency(session_type: str) -> str:
    """Get pandas/gtimes-style frequency string for a session type."""
    if session_type == "15s_24hr":
        return "1D"
    return "1H"


def generate_period_ranges(
    start: datetime,
    end: datetime,
    session_type: str,
    reverse: bool = False,
) -> List[Tuple[datetime, datetime]]:
    """Generate ``(period_start, period_end)`` pairs for iteration.

    Used by network-first download ordering: iterate over periods and process
    all stations for each period before moving on.
    """
    return _gt_generate_period_ranges(
        start, end, _session_period(session_type), reverse=reverse
    )


def parse_dates_arg(value: str) -> set:
    """Parse a ``--dates`` value — comma-separated YYYYMMDD, or ``@file``
    (one per line; ``#`` comments and blanks ignored). Returns a set of
    :class:`datetime.date`. Shared by ``rinex --dates`` and
    ``epos-disseminate --dates``."""
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    if value.startswith("@"):
        tokens = []
        for line in _Path(value[1:]).expanduser().read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                tokens.append(line)
    else:
        tokens = [t.strip() for t in value.split(",") if t.strip()]
    return {_dt.strptime(t, "%Y%m%d").date() for t in tokens}


def resolve_time_range(
    session: str,
    start: "datetime | None",
    end: "datetime | None",
    days: "int | None",
    *,
    case_insensitive_session: bool = False,
    infer_start_from_end: bool = False,
    inclusive_same_day: bool = False,
) -> "Tuple[datetime | None, datetime | None, bool]":
    """Resolve a ``(start, end, reverse_chronological)`` download window.

    This ritual — "explicit start/end, else ``--days`` lookback; fill in a
    missing bound from the session period" — was written out longhand in both
    ``cmd_download`` and ``cmd_rinex``, and the two copies had already diverged
    in three separate ways. The keyword flags exist to reproduce each caller's
    CURRENT behaviour exactly rather than to quietly unify them; converging the
    semantics is a separate, reviewable decision.

    Args:
        session: Session type, e.g. ``15s_24hr`` / ``1Hz_1hr``.
        start: Parsed explicit start, or ``None``.
        end: Parsed explicit end, or ``None``.
        days: ``--days`` lookback in complete periods, or ``None``.
        case_insensitive_session: Match ``"1hr"`` against ``session.lower()``.
            ``cmd_rinex`` does; ``cmd_download`` does not, so a session spelled
            ``1Hz_1HR`` is treated as daily by one verb and hourly by the other.
        infer_start_from_end: When only ``end`` is given, derive ``start`` one
            period earlier. ``cmd_download`` does this; ``cmd_rinex`` has no
            such branch and leaves ``start`` as ``None``.
        inclusive_same_day: When ``start == end``, widen ``end`` by one period
            so the range covers that day/hour instead of being empty. Only
            ``cmd_rinex`` does this.

    Returns:
        ``(start, end, reverse_chronological)``. ``reverse_chronological`` is
        True only when the window came from ``days`` (latest data first).
    """
    reverse_chronological = False

    if start is None and days:
        # -D/--days: prioritise the most recent complete periods.
        reverse_chronological = True
        start, end = calculate_download_time_range(
            session_type=session, lookback_periods=days
        )

    haystack = (session or "").lower() if case_insensitive_session else (session or "")
    period = timedelta(hours=1) if "1hr" in haystack else timedelta(days=1)

    if start is not None and end is None:
        end = start + period

    if infer_start_from_end and end is not None and start is None:
        start = end - period

    if inclusive_same_day and start is not None and end is not None and start == end:
        end = start + period

    return start, end, reverse_chronological
