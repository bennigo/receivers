"""SBF-sourced health samples must carry the sample's own epoch, not "now" (#169).

When the TCP probe fails, PolaRX5 health falls back to decoding the newest
*available* status SBF. That reading was then stamped with the current time, so
a dead station kept reporting a fresh-looking value forever.

Measured on rek-d01 2026-08-31: HRIC lost power around 2026-08-30 05:00, and
`block_power_status` showed **exactly 11.38 V for every hour since** — the same
Aug-30 05:00 sample re-decoded and re-written hourly. The flat line was the only
hint that the station was off; "last reading age" was useless because every row
claimed to be current.

Two properties matter:

* the row `ts` becomes the sample's epoch, so staleness is visible and the
  `ON CONFLICT (sid, ts)` upsert collapses repeats instead of manufacturing a
  new hourly row from one stale file;
* the epoch is applied to the WHOLE record, not just the power block. The
  pre-built health views join `block_*` tables ``USING (sid, ts)`` (see
  CLAUDE.md's cartesian-join rule), so a split epoch would make those joins
  silently return nothing for SBF-sourced samples.

The live TCP path is untouched: there "now" IS the sample epoch.
"""

from receivers.septentrio.polarx5 import PolaRX5

SAMPLE_TS = "2026-08-30T05:00:00+00:00"


def _metrics(power_ts=SAMPLE_TS):
    power = {"voltage": 11.38, "unit": "V", "status": "warning"}
    if power_ts is not None:
        power["timestamp"] = power_ts
    return {
        "power": power,
        "cpu_load": {"value": 37.0, "status": "ok"},
        "temperature": {"value": 24.0, "status": "ok"},
    }


# ── the epoch helper ─────────────────────────────────────────────────────────


def test_epoch_read_from_the_power_block():
    assert PolaRX5._sbf_sample_epoch(_metrics()) == SAMPLE_TS


def test_no_epoch_when_power_block_absent():
    assert PolaRX5._sbf_sample_epoch({"cpu_load": {"value": 1}}) is None


def test_no_epoch_when_power_has_no_timestamp():
    assert PolaRX5._sbf_sample_epoch(_metrics(power_ts=None)) is None


def test_no_epoch_for_empty_or_missing_metrics():
    assert PolaRX5._sbf_sample_epoch(None) is None
    assert PolaRX5._sbf_sample_epoch({}) is None


def test_blank_timestamp_is_not_an_epoch():
    """An empty string must not become the row ts (it would fail to parse)."""
    assert PolaRX5._sbf_sample_epoch(_metrics(power_ts="")) is None


def test_non_string_timestamp_rejected():
    assert PolaRX5._sbf_sample_epoch(_metrics(power_ts=12345)) is None


# ── the record-level contract ────────────────────────────────────────────────
#
# These call the PRODUCTION function. An earlier draft re-implemented the branch
# inside the test file, which would have passed even with the real one broken —
# the failure mode this repo keeps hitting (see the mutation-harness notes in
# CLAUDE.md). `_apply_extraction_metadata` was extracted so the tests exercise
# shipped code rather than a copy of it.

_retimestamp = PolaRX5._apply_extraction_metadata


def test_sbf_record_is_stamped_with_the_sample_epoch():
    hs = _retimestamp(
        {"timestamp": "2026-08-31T11:53:26+00:00"}, _metrics(), "sbf_file"
    )
    assert hs["timestamp"] == SAMPLE_TS


def test_read_time_is_preserved_separately():
    """We must still be able to say when we looked, not only when the data is from."""
    read_at = "2026-08-31T11:53:26+00:00"
    hs = _retimestamp({"timestamp": read_at}, _metrics(), "sbf_file")
    assert hs["extraction_metadata"]["extraction_time"] == read_at
    assert hs["timestamp"] != read_at


def test_live_tcp_path_keeps_now():
    """On the live path 'now' IS the sample epoch — must not be rewritten."""
    read_at = "2026-08-31T11:53:26+00:00"
    hs = _retimestamp({"timestamp": read_at}, _metrics(), "tcp_live")
    assert hs["timestamp"] == read_at


def test_port_check_only_keeps_now():
    read_at = "2026-08-31T11:53:26+00:00"
    hs = _retimestamp({"timestamp": read_at}, None, "port_check_only")
    assert hs["timestamp"] == read_at


def test_sbf_without_epoch_falls_back_to_now():
    """No epoch available ⇒ leave the existing stamp; no worse than before."""
    read_at = "2026-08-31T11:53:26+00:00"
    hs = _retimestamp({"timestamp": read_at}, _metrics(power_ts=None), "sbf_file")
    assert hs["timestamp"] == read_at


def test_repeated_reads_of_one_stale_file_share_an_epoch():
    """The HRIC case: 24 hourly reads of the same SBF must collapse, not multiply.

    Identical `ts` means the ON CONFLICT (sid, ts) upsert overwrites one row
    instead of inventing 24 hourly readings for a station that was off.
    """
    stamps = {
        _retimestamp(
            {"timestamp": f"2026-08-31T{h:02d}:00:00+00:00"}, _metrics(), "sbf_file"
        )["timestamp"]
        for h in range(24)
    }
    assert stamps == {SAMPLE_TS}
