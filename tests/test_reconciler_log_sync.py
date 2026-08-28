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


@pytest.fixture
def log_calls(monkeypatch):
    """Record every discrepancy-log write instead of performing it."""
    from receivers.cfg import discrepancy_log as dlog

    calls: dict[str, list] = {"detect": [], "resolve": []}
    monkeypatch.setattr(
        dlog,
        "record_detection",
        lambda sid, key, **kw: calls["detect"].append((sid, key, kw)) or 1,
    )
    monkeypatch.setattr(
        dlog,
        "auto_resolve_if_open",
        lambda sid, key, **kw: calls["resolve"].append((sid, key)) or True,
    )
    return calls


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
    assert len(log_calls["detect"]) == 1
    sid, key, kw = log_calls["detect"][0]
    assert (sid, key) == ("ELDC", "receiver_serial")
    assert kw["verdict"] == "conflict"
    assert kw["cfg_value"] == "OLD123"
    assert kw["receiver_value"] == "NEW456"
    assert kw["detected_by"] == "cfg_reconcile"
    assert log_calls["resolve"] == []


def test_a_missing_value_records_a_detection(log_calls):
    compare_station(
        "ELDC", {}, {"serial_number": "12345"}, None, fields=["receiver_serial"]
    )
    assert len(log_calls["detect"]) == 1
    assert log_calls["detect"][0][2]["verdict"] == "missing"


def test_no_data_writes_nothing(log_calls):
    """Nothing observed is not a discrepancy — leave existing rows alone."""
    compare_station("ELDC", {}, None, None, fields=["receiver_serial"])
    assert log_calls["detect"] == []
    assert log_calls["resolve"] == []


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
    assert log_calls["resolve"] == [("ELDC", "receiver_serial")]
    assert log_calls["detect"] == []


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
    assert log_calls["resolve"] == [], "closed a row for a source we never probed"


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
    assert log_calls["resolve"] == [], "closed a row without ever asking TOS"


# ---------------------------------------------------------------------------
# §4.7 — the connection cost
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "architecture review §4.7 — the log sync still runs inside the per-field "
        "loop, so this opens one connection per FIELD. strict=True so that fixing "
        "it fails the suite until this marker is removed: the bug is pinned, not "
        "tolerated."
    ),
)
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


def test_a_field_whose_log_write_fails_does_not_lose_the_others(log_calls, monkeypatch):
    """Per-field error isolation, preserved from the in-loop version.

    The original wrapped each field's sync in its own try/except, so one bad
    field lost only its own row. Batching must not let one failure abort the
    station.
    """
    from receivers.cfg import discrepancy_log as dlog

    seen: list[str] = []

    def _flaky(sid, key, **kw):
        seen.append(key)
        if key == "receiver_serial":
            raise RuntimeError("one bad field")
        return 1

    monkeypatch.setattr(dlog, "record_detection", _flaky)
    compare_station(
        "ELDC",
        {"receiver_serial": "OLD", "receiver_firmware_version": "1.0"},
        {"serial_number": "NEW", "firmware_version": "2.0"},
        None,
        fields=["receiver_serial", "receiver_firmware_version"],
    )
    assert "receiver_firmware_version" in seen, (
        "a failure on one field stopped the rest of the station being logged"
    )
