"""Session-aware lookback windows for scheduler jobs.

Scheduler jobs (gap detection, backfill, the archive reconciler, the integrity
checker, morning recovery) all need the same thing: "how far back should this
run look?". Historically every one of them read a ``days_back`` key and did
``end - timedelta(days=days_back)``.

That is wrong for hourly sessions, and expensively so. ``1Hz_1hr`` produces 24
files per station per day, so ``days_back: 30`` over ~332 active stations is
~239,000 files — a number the deployed ``scheduler.yaml`` already complains
about in a comment next to ``archive_reconciler``. The manual CLI path had
quietly diverged and got it right (``cli/main.py``: "-d counts hours not days
for status_1hr, consistent with download"); it was the scheduler that was
inconsistent.

Two keys, one mechanism
-----------------------

``days_back`` keeps its original meaning **exactly**: N calendar days, whatever
the session's file frequency. Every existing config and every runbook that says
"days_back 7" stays true.

``files_back`` is the session-aware alternative: N *files* back, which is N days
for a daily session and N hours for an hourly one.

Both resolve to a single :class:`Lookback` at config-load time so the machinery
downstream has one code path, not two. Setting both in the same section is a
hard error rather than a silent precedence rule — a silent precedence is the
same class of bug this module exists to remove.

Why ``days_back`` is kept rather than redefined
-----------------------------------------------

Redefining it would have been a one-line change, and it would have silently
falsified every comment, runbook and note that mentions it without altering a
single character of them. It also protects a real trap: ``epos_disseminate``'s
``days_back`` is load-bearing as *days* — it is the only backfill window EPOS
has (no gap-reconciler yet), so a day that ages out of it is orphaned until
someone runs ``epos-disseminate --force`` by hand. That section deliberately
keeps ``days_back``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Optional, Tuple

from ..utils.session_parser import parse_session_parameters

__all__ = ["Lookback", "LookbackConfigError", "is_hourly_session"]

DAYS_KEY = "days_back"
FILES_KEY = "files_back"


class LookbackConfigError(ValueError):
    """Raised when a config section specifies its lookback ambiguously."""


def is_hourly_session(session_type: str) -> bool:
    """True when ``session_type`` produces one file per hour.

    Delegates to :func:`parse_session_parameters`, which also handles the
    ``_rinex`` suffix (``1Hz_1hr_rinex`` -> ``1H``) and the ``status_1hr``
    shape (no acquisition rate in the name).
    """
    _, _, gtimes_freq = parse_session_parameters(session_type)
    return gtimes_freq == "1H"


@dataclass(frozen=True)
class Lookback:
    """How far back a scheduler job should look, in days or in files.

    ``unit`` is ``"days"`` (calendar days regardless of session) or ``"files"``
    (session-aware: days for 1D sessions, hours for 1H sessions).
    """

    count: int
    unit: str
    #: Per-session overrides as (session_type, count) pairs. A tuple rather
    #: than a dict so the dataclass stays genuinely immutable and hashable —
    #: these instances are pickled into the APScheduler jobstore.
    overrides: Tuple[Tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.unit not in ("days", "files"):
            raise LookbackConfigError(
                f"Lookback unit must be 'days' or 'files', got {self.unit!r}"
            )
        for label, value in (("default", self.count), *self.overrides):
            if value < 0:
                raise LookbackConfigError(
                    f"Lookback count for {label} must be >= 0, got {value!r}"
                )

    def count_for(self, session_type: str) -> int:
        """The count that applies to ``session_type`` (override, else default)."""
        for name, value in self.overrides:
            if name == session_type:
                return value
        return self.count

    @classmethod
    def from_config(
        cls,
        section: Mapping,
        *,
        default_days: int,
        section_name: str = "<section>",
    ) -> Lookback:
        """Build from a scheduler.yaml section.

        Exactly one of ``days_back`` / ``files_back`` may be present. Neither is
        also fine — the caller's historical default applies, so an untouched
        deployed config keeps behaving exactly as before.

        Either key may be a plain number::

            files_back: 36

        or a mapping with a ``default`` and per-session overrides, for when one
        number does not suit both frequencies (``files_back: 36`` means 36 days
        for a daily session but 36 hours for an hourly one, which is often not
        what you want on both at once)::

            files_back:
              default: 36        # status_1hr, 1Hz_1hr -> 36 hours
              15s_24hr: 7        # the daily session -> 7 days

        Raises:
            LookbackConfigError: if both keys are set, or a mapping has no
                ``default``.
        """
        has_days = section.get(DAYS_KEY) is not None
        has_files = section.get(FILES_KEY) is not None

        if has_days and has_files:
            raise LookbackConfigError(
                f"{section_name}: set either '{DAYS_KEY}' or '{FILES_KEY}', not both "
                f"(got {DAYS_KEY}={section[DAYS_KEY]!r}, {FILES_KEY}={section[FILES_KEY]!r}). "
                f"'{DAYS_KEY}' is always calendar days; '{FILES_KEY}' is days for daily "
                f"sessions and hours for hourly ones."
            )

        if has_files:
            key, unit = FILES_KEY, "files"
        elif has_days:
            key, unit = DAYS_KEY, "days"
        else:
            return cls(count=int(default_days), unit="days")

        raw = section[key]
        if isinstance(raw, Mapping):
            if "default" not in raw:
                raise LookbackConfigError(
                    f"{section_name}.{key} is a mapping but has no 'default' — "
                    f"add one so sessions without an explicit entry are covered "
                    f"(got keys {sorted(raw)})."
                )
            overrides = tuple(
                (str(k), int(v)) for k, v in raw.items() if k != "default"
            )
            return cls(count=int(raw["default"]), unit=unit, overrides=overrides)

        return cls(count=int(raw), unit=unit)

    def window(
        self, session_type: str, end: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """Return ``(start, end)`` for this session type.

        ``end`` defaults to now. For ``unit="files"`` on an hourly session the
        span is ``count`` hours; everything else is ``count`` days.
        """
        if end is None:
            end = datetime.now()
        return (end - self.span(session_type), end)

    def span(self, session_type: str) -> timedelta:
        """The lookback duration for ``session_type``."""
        n = self.count_for(session_type)
        if self.unit == "files" and is_hourly_session(session_type):
            return timedelta(hours=n)
        return timedelta(days=n)

    def days_for(self, session_type: str) -> float:
        """The span expressed in days, for callers whose API still takes days.

        Deliberately fractional: 7 files back on an hourly session is 7/24 of a
        day, and rounding that up to 1 would quietly restore a 24-file window.
        Callers that need a whole number should use :meth:`span` or
        :meth:`window` instead of rounding this.
        """
        return self.span(session_type).total_seconds() / 86400.0

    def file_count(self, session_type: str) -> int:
        """How many files this window covers for ``session_type``.

        This is the form the gap machinery needs. ``get_gap_summary`` and
        ``_generate_expected_files`` work in whole ``date`` objects and cannot
        express "7/24 of a day", so instead of narrowing their date range they
        expand as usual and keep the newest ``file_count`` entries. For an
        hourly session that is literally "the last N files", which is what
        ``files_back`` means.
        """
        n = self.count_for(session_type)
        if self.unit == "files":
            return n
        return n * 24 if is_hourly_session(session_type) else n

    def date_span_days(self, session_type: str) -> int:
        """Whole days to enumerate so that :meth:`file_count` files fit inside.

        Always rounds UP and adds a day, because the newest N hourly files
        straddle a midnight for most of the day. Narrowing happens afterwards
        via :meth:`file_count`; enumerating a day too many is cheap, a day too
        few silently drops files.
        """
        n = self.count_for(session_type)
        if self.unit == "days":
            return n
        if is_hourly_session(session_type):
            return -(-n // 24) + 1  # ceil, plus the straddle day
        return n

    def describe(self, session_types: Iterable[str]) -> str:
        """Human-readable expansion, for logging the effective window at startup.

        e.g. ``files_back=7 -> 15s_24hr: 7d, 1Hz_1hr: 7h`` — so the real meaning
        shows up in the startup log instead of having to be derived by hand.
        """
        parts = []
        for st in session_types:
            span = self.span(st)
            if span >= timedelta(days=1) and span.total_seconds() % 86400 == 0:
                parts.append(f"{st}: {int(span.total_seconds() // 86400)}d")
            else:
                parts.append(f"{st}: {int(span.total_seconds() // 3600)}h")
        key = FILES_KEY if self.unit == "files" else DAYS_KEY
        head = f"{key}={self.count}"
        if self.overrides:
            head += " (" + ", ".join(f"{k}={v}" for k, v in self.overrides) + ")"
        return f"{head} -> " + ", ".join(parts)
