"""fix-headers rewrites MARKER NUMBER (DOMES) in the same single read/fix pass.

A domes-only discrepancy must no longer be dropped as "formatting noise": it is
now a real, fixable field. These tests drive ``fix_headers_in_file`` fully
offline — the header read, TOS session, validator, corrector and regenerability
gate are all monkeypatched — and assert the DOMES label flows through to the
corrector's ``only_fields`` in one pass (no separate DOMES sweep).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import tostools.rinex as tr
import tostools.rinex.validator as tv

from receivers.rinex import header_fix as hf
from receivers.rinex import raw_presence as rp


def _drive(
    monkeypatch,
    tmp_path,
    *,
    comparison,
    session=None,
    correct_hardware=frozenset(),
    resolved=None,
):
    """Run fix_headers_in_file on one real (empty) file with everything mocked.

    Returns (result, captured) where captured["only_fields"] is the set handed
    to the corrector (None if the corrector was never called).

    ``resolved`` is what the CORRECTOR's builder yields — deliberately a separate
    mock from ``comparison``'s ``corrections``. The validator decides WHICH
    fields differ, the corrector decides WHAT is written, and the two disagreed
    in production (a TOS placeholder antenna serial previewed blank but was
    written ``0000``). Keeping them separate here is what makes a future
    divergence visible instead of silently agreeing by construction. Defaults to
    the comparison's corrections, i.e. "the two agree".
    """
    if session is None:
        session = {"marker": "RHOF", "domes": "10216M001"}
    f = tmp_path / "RHOF0910.10D.Z"
    f.write_bytes(b"stub")  # must exist; content unused (read is mocked)

    monkeypatch.setattr(
        hf, "_read_header_info", lambda *a, **k: {"MARKER NAME": "RHOF"}
    )
    monkeypatch.setattr(tv, "compare_rinex_to_tos", lambda *a, **k: comparison)
    # Regenerable ⇒ no rinex_org preservation branch.
    monkeypatch.setattr(
        rp,
        "check_regenerable",
        lambda *a, **k: SimpleNamespace(regenerable=True, reason=""),
    )

    captured: dict = {"only_fields": None}

    # The corrector's own builder, which the preview now resolves against so a
    # dry-run reports the value that will actually be written.
    import tostools.rinex.corrector as tc

    _resolved = dict(comparison.get("corrections", {}) if resolved is None else resolved)

    def fake_resolve(rinex_file, station, observation_date=None, **kw):
        only = kw.get("only_fields")
        out = {**_resolved, **(kw.get("extra_corrections") or {})}
        if only is not None:
            out = {k: v for k, v in out.items() if k in only}
        captured["resolved"] = out
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
        captured["extra_corrections"] = extra_corrections
        return output_file  # non-None ⇒ fixed

    monkeypatch.setattr(tr, "correct_rinex_from_tos", fake_correct)

    tos_cache = SimpleNamespace(get_session=lambda sid, dt: session)
    result = hf.fix_headers_in_file(
        f,
        "RHOF",
        observation_date=datetime(2010, 4, 1),
        tos_cache=tos_cache,
        session_type="15s_24hr",
        correct_hardware=correct_hardware,
    )
    return result, captured


def _domes_comparison():
    return {
        "discrepancies": {"domes": {"rinex": "RHOF", "tos": "10216M001"}},
        "corrections": {"MARKER NUMBER": "10216M001"},
    }


def test_domes_only_discrepancy_is_fixed(monkeypatch, tmp_path):
    result, captured = _drive(monkeypatch, tmp_path, comparison=_domes_comparison())
    assert result["fixed"] is True
    assert result["changed_labels"] == ["MARKER NUMBER"]
    assert captured["only_fields"] == {"MARKER NUMBER"}
    # the old→new transition is recorded for the run summary
    assert result["changes"]["MARKER NUMBER"] == ("RHOF", "10216M001")


def test_no_domes_strip_routes_marker_number_to_corrector(monkeypatch, tmp_path):
    # New policy path: a no-DOMES station whose archived header still carries a
    # legacy 4-char MARKER NUMBER. The validator flags it with an empty "tos"
    # (display-only strip flag); header_fix must still route MARKER NUMBER into
    # the corrector's only_fields, where the corrector recomputes the real strip.
    comparison = {
        "discrepancies": {"domes": {"rinex": "RHOF", "tos": ""}},
        "corrections": {"MARKER NUMBER": ""},
    }
    result, captured = _drive(
        monkeypatch,
        tmp_path,
        comparison=comparison,
        session={"marker": "RHOF", "domes": ""},  # no DOMES
    )
    assert result["fixed"] is True
    assert captured["only_fields"] == {"MARKER NUMBER"}
    # the strip is recorded as a legacy-id → (empty) transition for the summary
    assert result["changes"]["MARKER NUMBER"] == ("RHOF", "")


def test_domes_and_height_fixed_in_one_pass(monkeypatch, tmp_path):
    comparison = {
        "discrepancies": {
            "domes": {"rinex": "", "tos": "10216M001"},
            "antenna_height": {"rinex": 1.0070, "tos": 1.0140},
        },
        "corrections": {
            "MARKER NUMBER": "10216M001",
            # The corrector's builder yields the H/E/N triplet, not a preformatted
            # string — the preview renders these values, so the fixture has to be
            # the real shape or it is not testing the real path.
            "ANTENNA: DELTA H/E/N": [1.0140, 0.0, 0.0],
        },
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is True
    # one read → both fields fixed in a single corrector call
    assert captured["only_fields"] == {"MARKER NUMBER", "ANTENNA: DELTA H/E/N"}


def test_receiver_only_discrepancy_is_flagged_not_written(monkeypatch, tmp_path):
    # receiver/antenna are FLAG-only: reported for review, never auto-written.
    comparison = {
        "discrepancies": {"receiver": {"rinex": "x sn=1", "tos": "y sn=2"}},
        "corrections": {"REC # / TYPE / VERS": ["2", "y", ""]},
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is False
    assert captured["only_fields"] is None  # corrector never called
    # but the mismatch IS recorded so the run summary can surface it
    assert result["flagged"]["receiver"] == ("x sn=1", "y sn=2")


def test_correct_receiver_optin_writes_rec_type(monkeypatch, tmp_path):
    # With correct_hardware={"receiver"} (the --correct-receiver opt-in) the
    # normally flag-only receiver is promoted to correctable and routed to the
    # corrector's only_fields as REC # / TYPE / VERS.
    comparison = {
        "discrepancies": {"receiver": {"rinex": "TRIMBLE NETRS", "tos": "SEPT POLARX5"}},
        "corrections": {"REC # / TYPE / VERS": ["3001", "SEPT POLARX5", "5.5.0"]},
    }
    result, captured = _drive(
        monkeypatch, tmp_path, comparison=comparison,
        correct_hardware=frozenset({"receiver"}),
    )
    assert result["fixed"] is True
    assert captured["only_fields"] == {"REC # / TYPE / VERS"}
    # The new side is the exact header field the corrector will emit — serial,
    # type and firmware in their A20 columns — not the validator's one-line
    # summary. The whole point of the change is that the preview is the write.
    old, new = result["changes"]["REC # / TYPE / VERS"]
    assert old == "TRIMBLE NETRS"
    assert new == "3001                SEPT POLARX5        5.5.0"
    # not left in the flag-only bucket
    assert "receiver" not in result["flagged"]


def test_correct_receiver_optin_leaves_antenna_flag_only(monkeypatch, tmp_path):
    # Opting in ONLY receiver must not promote antenna — it stays flag-only.
    comparison = {
        "discrepancies": {
            "receiver": {"rinex": "TRIMBLE NETRS", "tos": "SEPT POLARX5"},
            "antenna": {"rinex": "OLD", "tos": "NEW"},
        },
        "corrections": {
            "REC # / TYPE / VERS": ["3001", "SEPT POLARX5", "5.5.0"],
            "ANT # / TYPE": ["999", "NEW"],
        },
    }
    result, captured = _drive(
        monkeypatch, tmp_path, comparison=comparison,
        correct_hardware=frozenset({"receiver"}),
    )
    assert captured["only_fields"] == {"REC # / TYPE / VERS"}  # antenna NOT written
    assert result["flagged"]["antenna"] == ("OLD", "NEW")


def test_observer_agency_fixed_with_injected_value(monkeypatch, tmp_path):
    # observer_agency is correctable; the resolved value is injected into the
    # corrector via extra_corrections (the corrector can't reach agencies.yaml).
    comparison = {
        "discrepancies": {
            "observer_agency": {
                "rinex": "SFS/BGO/SJ / ETH/IMO",
                "tos": "GNSSatIMO / Vedurstofa Islands",
            }
        },
        "corrections": {"OBSERVER / AGENCY": ["GNSSatIMO", "Vedurstofa Islands"]},
    }
    result, captured = _drive(
        monkeypatch,
        tmp_path,
        comparison=comparison,
        session={
            "marker": "RHOF",
            "domes": "10216M001",
            "observer": "GNSSatIMO",
            "agency": "Vedurstofa Islands",
        },
    )
    assert result["fixed"] is True
    assert captured["only_fields"] == {"OBSERVER / AGENCY"}
    assert captured["extra_corrections"] == {
        "OBSERVER / AGENCY": ["GNSSatIMO", "Vedurstofa Islands"]
    }


def test_flagged_receiver_alongside_fixed_domes(monkeypatch, tmp_path):
    # A file can be BOTH fixed (domes) and flagged (receiver) in one pass.
    comparison = {
        "discrepancies": {
            "domes": {"rinex": "RHOF", "tos": "10216M001"},
            "receiver": {"rinex": "a", "tos": "b"},
        },
        "corrections": {
            "MARKER NUMBER": "10216M001",
            "REC # / TYPE / VERS": ["b"],
        },
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is True
    assert captured["only_fields"] == {"MARKER NUMBER"}  # receiver NOT written
    assert result["flagged"]["receiver"] == ("a", "b")


# _nominal_interval_seconds — session_type → expected sampling rate
def test_nominal_interval_seconds():
    assert hf._nominal_interval_seconds("15s_24hr") == 15.0
    assert hf._nominal_interval_seconds("1Hz_1hr") == 1.0
    assert hf._nominal_interval_seconds("30s_24hr") == 30.0
    assert hf._nominal_interval_seconds("status_1hr") is None
    assert hf._nominal_interval_seconds(None) is None
    assert hf._nominal_interval_seconds("") is None


class TestPreviewReportsWhatTheWriteWillDo:
    """The preview must come from the corrector's builder, not the validator's.

    These two disagree in production. TOS assigns a placeholder antenna serial
    ``antenna-<STID>-<YYYYMMDD>``; the validator proposes suppressing it (blank)
    while the corrector writes ``0000``. The dry-run used to show the former and
    the run wrote the latter — measured on 66 VMEY files, 2026-08-19. A dry-run
    that does not describe the action is worthless on the one run it exists for:
    the review before rewriting archived files.
    """

    def test_preview_shows_the_correctors_value_not_the_validators(
        self, monkeypatch, tmp_path
    ):
        comparison = {
            "discrepancies": {
                "antenna": {
                    "rinex": "SEPCHOKE_B3E6/SPKE sn=antenna-VMEY-2023011",
                    "tos": "SEPCHOKE_B3E6/SPKE sn=?",  # validator: suppress it
                }
            },
            "corrections": {"ANT # / TYPE": ["", "SEPCHOKE_B3E6   SPKE"]},
        }
        result, _ = _drive(
            monkeypatch,
            tmp_path,
            comparison=comparison,
            correct_hardware=frozenset({"antenna"}),
            # corrector: placeholder serial becomes 0000
            resolved={"ANT # / TYPE": ["0000", "SEPCHOKE_B3E6   SPKE"]},
        )
        _, new = result["changes"]["ANT # / TYPE"]
        assert new.startswith("0000"), "preview must report the written value"
        assert "sn=?" not in new, "the validator's rendering must not leak in"
        # A20,A20 — the field the truncation bug used to mangle.
        assert new[20:].strip() == "SEPCHOKE_B3E6   SPKE".strip()

    def test_a_field_the_corrector_will_not_write_is_not_reported_as_changed(
        self, monkeypatch, tmp_path
    ):
        # The position guard drops a km-scale APPROX POSITION rewrite this way.
        # Claiming a fix that never happens is the same defect in the other
        # direction, so the label is dropped from changed_labels too.
        comparison = {
            "discrepancies": {"domes": {"rinex": "RHOF", "tos": "10216M001"}},
            "corrections": {"MARKER NUMBER": "10216M001"},
        }
        result, captured = _drive(
            monkeypatch, tmp_path, comparison=comparison, resolved={}
        )
        assert result["changed_labels"] == []
        assert result["changes"] == {}
        assert result["fixed"] is False
        assert captured["only_fields"] is None, "corrector must not be called"

    def test_a_stripped_line_is_previewed_as_a_removal(self, monkeypatch, tmp_path):
        # STRIP_LINE (no real DOMES) removes the line; showing a blank field
        # would misdescribe it as "written empty".
        comparison = {
            "discrepancies": {"domes": {"rinex": "RHOF", "tos": ""}},
            "corrections": {"MARKER NUMBER": "10216M001"},
        }
        result, _ = _drive(
            monkeypatch, tmp_path, comparison=comparison, resolved={"MARKER NUMBER": None}
        )
        assert result["changes"]["MARKER NUMBER"] == ("RHOF", "<line removed>")
