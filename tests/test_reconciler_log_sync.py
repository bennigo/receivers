"""What `compare_station` writes to the discrepancy log, and how many
connections it takes to do it.

Two separate concerns, pinned together because the fix for the second must not
change the first:

* **Semantics** — which verdicts record a row, which auto-close one, and the
  `fully_observed` rule that stops an un-probed source's drift being closed
  behind your back.
* **Cost** — architecture review §4.7: the sync runs *inside* the per-field
  loop, so each field opens its own connection and takes its own advisory
  lock. `reconcile --all` is ~173 stations x ~15 fields, on a host with
  documented `max_connections=100` exhaustion.

**These tests must patch the writers.** `compare_station` swallows a
DB-unavailable failure into `logger.debug` and continues, so in an environment
with no database it already writes nothing — absence of writes proves nothing
at all. Every assertion here is on recorded CALLS, never on the absence of an
effect.
"""

from __future__ import annotations

import pytest

from receivers.cfg.reconciler import Verdict, compare_station


class _Recorded:
    """The one batch `compare_station` hands to the log, split by kind.

    `detect` = entries carrying a verdict; `resolve` = entries whose verdict is
    None, meaning "close the open row".
    """

    def __init__(self):
        self.batches: list = []

    def __call__(self, station_id, entries, *, detected_by, **kw):
        self.batches.append((station_id, list(entries), detected_by))
        return len(entries)

    @property
    def detect(self):
        return [
            (sid, e, by)
            for sid, entries, by in self.batches
            for e in entries
            if e.verdict is not None
        ]

    @property
    def resolve(self):
        return [
            (sid, e.cfg_key)
            for sid, entries, _ in self.batches
            for e in entries
            if e.verdict is None
        ]


@pytest.fixture
def log_calls(monkeypatch):
    """Record the batch instead of writing it.

    Patched on `sync_station`, the seam the whole station now goes through.
    Recording the BATCH rather than individual calls is strictly more
    informative: it also shows the grouping, which is the §4.7 fix.
    """
    from receivers.cfg import discrepancy_log as dlog

    rec = _Recorded()
    monkeypatch.setattr(dlog, "sync_station", rec)
    return rec


@pytest.fixture
def connection_count(monkeypatch):
    """Count how many DB connections one compare_station call opens.

    This is the §4.7 finding itself. A `sync_station` that still opens one
    connection per field would satisfy every call-count assertion above while
    changing nothing that matters, so the count is asserted separately.
    """
    import contextlib

    from receivers.health import database_factory as dbf

    opened = []

    @contextlib.contextmanager
    def _fake_connection(*a, **kw):
        opened.append(1)
        raise RuntimeError("no database in tests")
        yield  # pragma: no cover

    monkeypatch.setattr(
        dbf.DatabaseConnectionFactory, "connection", staticmethod(_fake_connection)
    )
    return opened


CFG_CONFLICT = {"receiver_serial": "OLD123"}
IDENT_CONFLICT = {"serial_number": "NEW456"}


# ---------------------------------------------------------------------------
# Which verdicts write what
# ---------------------------------------------------------------------------


def test_a_conflict_records_a_detection(log_calls):
    diffs = compare_station(
        "ELDC", CFG_CONFLICT, IDENT_CONFLICT, None, fields=["receiver_serial"]
    )
    assert diffs[0].verdict == Verdict.CONFLICT
    assert len(log_calls.detect) == 1
    sid, entry, detected_by = log_calls.detect[0]
    assert (sid, entry.cfg_key) == ("ELDC", "receiver_serial")
    assert entry.verdict == "conflict"
    assert entry.cfg_value == "OLD123"
    assert entry.receiver_value == "NEW456"
    assert detected_by == "cfg_reconcile"
    assert log_calls.resolve == []


def test_a_missing_value_records_a_detection(log_calls):
    compare_station(
        "ELDC", {}, {"serial_number": "12345"}, None, fields=["receiver_serial"]
    )
    assert len(log_calls.detect) == 1
    assert log_calls.detect[0][1].verdict == "missing"


def test_no_data_writes_nothing(log_calls):
    """Nothing observed is not a discrepancy — leave existing rows alone."""
    compare_station("ELDC", {}, None, None, fields=["receiver_serial"])
    assert log_calls.detect == []
    assert log_calls.resolve == []


# ---------------------------------------------------------------------------
# The fully_observed rule
# ---------------------------------------------------------------------------


def test_ok_auto_closes_when_every_source_was_queried(log_calls):
    compare_station(
        "ELDC",
        {"receiver_serial": "12345"},
        {"serial_number": "12345"},
        {
            "device_history": [
                {"time_to": None, "gnss_receiver": {"serial_number": "12345"}}
            ]
        },
        fields=["receiver_serial"],
        queried_sources={"cfg", "receiver", "tos"},
    )
    assert log_calls.resolve == [("ELDC", "receiver_serial")]
    assert log_calls.detect == []


def test_ok_does_NOT_auto_close_when_a_source_went_unprobed(log_calls):
    """cfg and TOS agree — but the receiver was never asked.

    A previously-flagged receiver drift is still real; closing the row here
    would silently discard a genuine open discrepancy because we simply did
    not look. Both halves of the `fully_observed` conjunction matter.
    """
    compare_station(
        "ELDC",
        {"receiver_serial": "12345"},
        None,
        {
            "device_history": [
                {"time_to": None, "gnss_receiver": {"serial_number": "12345"}}
            ]
        },
        fields=["receiver_serial"],
        queried_sources={"cfg", "tos"},
    )
    assert log_calls.resolve == [], "closed a row for a source we never probed"


def test_ok_does_NOT_auto_close_when_TOS_went_unqueried(log_calls):
    """The mirror of the case above — and it needs its own test.

    cfg and the receiver agree, but TOS was never asked. Verified by mutation:
    the receiver-only version above stays green when the TOS half of the
    `fully_observed` conjunction is deleted, because it never exercises it.
    Both halves need their own case or one of them is decorative.
    """
    compare_station(
        "ELDC",
        {"receiver_serial": "12345"},
        {"serial_number": "12345"},
        None,
        fields=["receiver_serial"],
        queried_sources={"cfg", "receiver"},
    )
    assert log_calls.resolve == [], "closed a row without ever asking TOS"


# ---------------------------------------------------------------------------
# §4.7 — the connection cost
# ---------------------------------------------------------------------------


def test_the_log_sync_opens_ONE_connection_per_station(connection_count):
    """Not one per field. This is the whole point of §4.7.

    `reconcile --all` is ~173 stations x ~15 fields. At one connection per
    field that is ~2,600 sequential checkouts against a host with documented
    `max_connections=100` exhaustion.

    Uses several fields with a real conflict so each would, in the old shape,
    have triggered its own connection.
    """
    cfg = {"receiver_serial": "OLD", "receiver_firmware_version": "1.0"}
    identity = {"serial_number": "NEW", "firmware_version": "2.0"}
    compare_station(
        "ELDC",
        cfg,
        identity,
        None,
        fields=["receiver_serial", "receiver_firmware_version"],
    )
    assert len(connection_count) <= 1, (
        f"opened {len(connection_count)} connections for one station; "
        f"the log sync must be batched, not per-field"
    )


def test_a_field_whose_log_write_fails_does_not_lose_the_others(monkeypatch):
    """Per-field error isolation, preserved from the loop this replaced.

    The original wrapped each field's sync in its own try/except, so one bad
    field lost only its own row. Batching into one transaction must not let a
    single failure abort the station — tested against `sync_station` directly,
    because that is where the isolation now lives.
    """
    import contextlib

    from receivers.cfg import discrepancy_log as dlog
    from receivers.health import database_factory as dbf

    executed: list[str] = []

    class _Cur:
        rowcount = 1

        def __init__(self):
            self.last = ""

        def execute(self, sql, params=None):
            # The failing key must sort FIRST — entries are processed in sorted
            # order, so failing on a late key would let an aborting
            # implementation pass anyway. (It did: the first version of this
            # test failed on `receiver_serial`, which sorts AFTER
            # `receiver_firmware_version`, and stayed green when the per-field
            # isolation was deleted.)
            if params and "aaa_bad_field" in params:
                raise RuntimeError("one bad field")
            self.last = sql
            if params and len(params) >= 2:
                executed.append(str(params[1]))

        def fetchone(self):
            # No pre-existing open row; the INSERT ... RETURNING id gives one.
            return (1,) if "INSERT" in self.last else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    @contextlib.contextmanager
    def _conn(*a, **kw):
        yield _Conn()

    monkeypatch.setattr(
        dbf.DatabaseConnectionFactory, "connection", staticmethod(_conn)
    )
    written = dlog.sync_station(
        "ELDC",
        [
            dlog.LogSyncEntry(cfg_key="aaa_bad_field", verdict="conflict"),
            dlog.LogSyncEntry(cfg_key="zzz_good_field", verdict="conflict"),
        ],
        detected_by=dlog.DETECTED_BY_RECONCILE,
    )
    assert "zzz_good_field" in executed, (
        "a failure on the FIRST field stopped the rest of the station being logged"
    )
    assert written == 1, "the failed field must not be counted as written"


def test_sync_station_takes_field_locks_in_sorted_order(monkeypatch):
    """Deterministic lock order, so two concurrent batches cannot deadlock.

    Field locks are kept per (station, cfg_key) rather than replaced by one
    station-wide lock, because a station lock uses a different key and would
    stop serialising against a concurrent per-field `record_detection` from the
    health probe — the exact race that function's docstring promises to handle.
    Holding several of them in ONE transaction is only safe if the order is
    fixed.
    """
    import contextlib

    from receivers.cfg import discrepancy_log as dlog
    from receivers.health import database_factory as dbf

    locks: list[str] = []

    class _Cur:
        rowcount = 1

        def execute(self, sql, params=None):
            if "pg_advisory_xact_lock" in sql:
                locks.append(params[1])

        def fetchone(self):
            return (1,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    @contextlib.contextmanager
    def _conn(*a, **kw):
        yield _Conn()

    monkeypatch.setattr(
        dbf.DatabaseConnectionFactory, "connection", staticmethod(_conn)
    )
    dlog.sync_station(
        "ELDC",
        [
            dlog.LogSyncEntry(cfg_key="zzz_last", verdict="conflict"),
            dlog.LogSyncEntry(cfg_key="aaa_first", verdict="conflict"),
            dlog.LogSyncEntry(cfg_key="mmm_mid", verdict="conflict"),
        ],
        detected_by=dlog.DETECTED_BY_RECONCILE,
    )
    assert locks == sorted(locks), f"locks taken out of order: {locks}"
    # Assert they were TAKEN at all — `sorted([]) == []`, so an implementation
    # that dropped the advisory lock entirely passed the ordering check alone.
    assert locks == ["aaa_first", "mmm_mid", "zzz_last"], (
        f"the per-field advisory lock is not being taken: {locks}"
    )
