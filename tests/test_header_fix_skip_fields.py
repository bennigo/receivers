"""``--skip-fields`` narrows a fix-headers run to the fields actually intended.

The case this exists for is APPROX POSITION XYZ. A header's approximate
position is the receiver's own autonomous solution and legitimately sits tens
of metres from the surveyed value in TOS, so it differs on essentially every
historical file — RHOF 2005 flagged 347/347. Repairing MARKER NUMBER on that
station would therefore have rewritten the coordinates of its entire archive as
a side effect.

The filter drops the key from ``correctable_map`` rather than post-filtering the
result, so a skipped field is never previewed, never written, and never counted
as a fix — the dry-run keeps describing exactly what the write will do.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import tostools.rinex as tr
import tostools.rinex.corrector as tc
import tostools.rinex.validator as tv

from receivers.rinex import header_fix as hf
from receivers.rinex import raw_presence as rp


def _drive(monkeypatch, tmp_path, *, comparison, skip_fields=frozenset()):
    """Run fix_headers_in_file fully offline; return (result, only_fields)."""
    f = tmp_path / "RHOF0910.05D.Z"
    f.write_bytes(b"stub")

    monkeypatch.setattr(
        hf, "_read_header_info", lambda *a, **k: {"MARKER NAME": "RHOF"}
    )
    monkeypatch.setattr(tv, "compare_rinex_to_tos", lambda *a, **k: comparison)
    monkeypatch.setattr(
        rp,
        "check_regenerable",
        lambda *a, **k: SimpleNamespace(regenerable=True, reason=""),
    )

    captured: dict = {"only_fields": None}
    _resolved = dict(comparison.get("corrections", {}))

    def fake_resolve(rinex_file, station, observation_date=None, **kw):
        only = kw.get("only_fields")
        out = {**_resolved, **(kw.get("extra_corrections") or {})}
        if only is not None:
            out = {k: v for k, v in out.items() if k in only}
        return out

    monkeypatch.setattr(tc, "resolve_corrections", fake_resolve)

    def fake_correct(
        target,
        station,
        *,
        observation_date,
        output_file,
        loglevel,
        only_fields,
        extra_corrections=None,
        tos_metadata_cache=None,
    ):
        captured["only_fields"] = set(only_fields)
        return output_file

    monkeypatch.setattr(tr, "correct_rinex_from_tos", fake_correct)

    tos_cache = SimpleNamespace(
        get_session=lambda sid, dt: {
            "marker": "RHOF",
            "domes": "10216M001",
            "observer": "GNSSatIMO",
            "agency": "Icelandic Meteorological Office",
        }
    )
    result = hf.fix_headers_in_file(
        f,
        "RHOF",
        observation_date=datetime(2005, 4, 1),
        tos_cache=tos_cache,
        session_type="15s_24hr",
        skip_fields=skip_fields,
    )
    return result, captured["only_fields"]


def _rhof_2005_comparison():
    """What RHOF's 2005 files actually produce: three correctable differences."""
    return {
        "discrepancies": {
            "domes": {"rinex": "", "tos": "10216M001"},
            "observer_agency": {"rinex": "Halldor Geirsson", "tos": "GNSSatIMO"},
            "coordinates": {"rinex": "2456177.05", "tos": "2456170.4959"},
        },
        "corrections": {
            "MARKER NUMBER": "10216M001",
            "OBSERVER / AGENCY": ["GNSSatIMO", "Icelandic Meteorological Office"],
            "APPROX POSITION XYZ": [2456170.4959, -701823.8383, 5824744.9433],
        },
    }


class TestSkipFields:
    def test_all_three_are_written_by_default(self, monkeypatch, tmp_path):
        _, only = _drive(monkeypatch, tmp_path, comparison=_rhof_2005_comparison())
        assert only == {
            "MARKER NUMBER",
            "OBSERVER / AGENCY",
            "APPROX POSITION XYZ",
        }

    def test_coordinates_can_be_skipped(self, monkeypatch, tmp_path):
        _, only = _drive(
            monkeypatch,
            tmp_path,
            comparison=_rhof_2005_comparison(),
            skip_fields=frozenset({"coordinates"}),
        )
        assert only == {"MARKER NUMBER", "OBSERVER / AGENCY"}
        assert "APPROX POSITION XYZ" not in only

    def test_a_skipped_field_is_not_reported_as_a_fix(self, monkeypatch, tmp_path):
        # The dry-run must describe the write. A field we refuse to write must
        # not appear in changed_labels either.
        result, _ = _drive(
            monkeypatch,
            tmp_path,
            comparison=_rhof_2005_comparison(),
            skip_fields=frozenset({"coordinates"}),
        )
        assert "APPROX POSITION XYZ" not in set(result.get("changed_labels") or [])

    def test_skipping_every_discrepant_field_is_a_no_op(self, monkeypatch, tmp_path):
        result, only = _drive(
            monkeypatch,
            tmp_path,
            comparison=_rhof_2005_comparison(),
            skip_fields=frozenset({"coordinates", "domes", "observer_agency"}),
        )
        assert only is None  # corrector never invoked
        assert not result.get("fixed")

    def test_skipping_an_unrelated_field_changes_nothing(self, monkeypatch, tmp_path):
        _, only = _drive(
            monkeypatch,
            tmp_path,
            comparison=_rhof_2005_comparison(),
            skip_fields=frozenset({"antenna_height"}),
        )
        assert only == {
            "MARKER NUMBER",
            "OBSERVER / AGENCY",
            "APPROX POSITION XYZ",
        }
