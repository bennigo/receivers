"""MARKER NUMBER policy on the BNC stream skeleton (.SKL).

`MARKER NUMBER` carries the IERS DOMES and nothing else. Absent a real DOMES the
line is skipped — never filled with the 4-char station id, which `MARKER NAME`
already carries (policy: bgo 2026-07-13, enforced by
`tostools.rinex.domes.domes_or_skip`).

The stream path was a fourth producer that bypassed that enforcement entirely:
`_skeleton_metadata` set `marker_number=station_id` outright, BNC copied the
skeleton into every hourly RINEX header, and GONH/HRIC/SEY9 published
`MARKER NUMBER = <their own marker name>` every hour. Verified on rek-d01
2026-08-30; GONH's stored SKL literally contained:

    GONH                                                        MARKER NAME
    GONH                                                        MARKER NUMBER

Two halves matter and they fail differently:

* **build** — a fresh skeleton must omit the line when there is no DOMES.
* **refresh** — `fill_skeleton` must *strip* a stored non-DOMES value. The
  original `if label == "MARKER NUMBER" and meta.marker_number` guard fell
  through to "keep the template's data" when the DOMES was absent, so an already
  poisoned skeleton would re-emit the bad value forever. That truthiness
  fallthrough is the same trap CLAUDE.md flags for `--owner ""` intake.
"""

import pytest

from receivers.streaming.skeleton import (
    SkeletonMetadata,
    build_skeleton,
    fill_skeleton,
)

DOMES = "10221M001"

# A stored skeleton poisoned with the 4-char id — what rek-d01 actually had.
POISONED_SKL = (
    "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
    "File configured from IMO rt streams                         COMMENT\n"
    "GONH                                                        MARKER NAME\n"
    "GONH                                                        MARKER NUMBER\n"
    "                                                            MARKER TYPE\n"
    "  2605201.8825 -1066894.4868  5704421.8437                  APPROX POSITION XYZ\n"
    "                                                            END OF HEADER\n"
)


def _labels(text: str) -> list[str]:
    return [ln[60:].rstrip() for ln in text.splitlines()]


def _data_for(text: str, label: str) -> str:
    for ln in text.splitlines():
        if ln[60:].rstrip() == label:
            return ln[:60].strip()
    raise AssertionError(f"{label!r} not present")


# ── refresh path (the poisoned-skeleton case) ────────────────────────────────


def test_refresh_strips_stored_station_id():
    """The station-id MARKER NUMBER must be REMOVED, not preserved."""
    out = fill_skeleton(POISONED_SKL, SkeletonMetadata(marker_name="GONH"))
    assert "MARKER NUMBER" not in _labels(out)
    # and it must not have been silently blanked-but-kept either
    assert "GONH" not in [
        ln[:60].strip() for ln in out.splitlines() if "MARKER NUMBER" in ln
    ]


def test_refresh_keeps_marker_name():
    """Stripping MARKER NUMBER must not disturb MARKER NAME."""
    out = fill_skeleton(POISONED_SKL, SkeletonMetadata(marker_name="GONH"))
    assert _data_for(out, "MARKER NAME") == "GONH"


def test_refresh_preserves_static_lines():
    """Only the offending line goes; position/comment/END OF HEADER remain."""
    out = fill_skeleton(POISONED_SKL, SkeletonMetadata(marker_name="GONH"))
    for label in (
        "RINEX VERSION / TYPE",
        "COMMENT",
        "APPROX POSITION XYZ",
        "END OF HEADER",
    ):
        assert label in _labels(out)
    assert "2605201.8825" in _data_for(out, "APPROX POSITION XYZ")


def test_refresh_writes_a_real_domes():
    """A genuine DOMES is written into the line."""
    out = fill_skeleton(
        POISONED_SKL, SkeletonMetadata(marker_name="GONH", marker_number=DOMES)
    )
    assert _data_for(out, "MARKER NUMBER") == DOMES


# ── build path ───────────────────────────────────────────────────────────────


def _build(marker_number):
    return build_skeleton(
        SkeletonMetadata(marker_name="GONH", marker_number=marker_number),
        latitude=63.89,
        longitude=-22.27,
        height=50.0,
        comment="test",
    )


def test_build_omits_line_without_domes():
    assert "MARKER NUMBER" not in _labels(_build(None))


def test_build_emits_line_with_domes():
    assert _data_for(_build(DOMES), "MARKER NUMBER") == DOMES


def test_build_keeps_marker_type_after_name_when_number_absent():
    """Dropping MARKER NUMBER must not lose the MARKER TYPE line that follows."""
    labels = _labels(_build(None))
    assert "MARKER NAME" in labels
    assert "MARKER TYPE" in labels
    assert labels.index("MARKER TYPE") > labels.index("MARKER NAME")


# ── the policy boundary itself ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "value", ["GONH", "SEY9", "", "   ", "1022M001", "10221M0011", "BJTC"]
)
def test_non_domes_values_never_reach_the_header(value):
    """Anything that is not a DOMES must be stripped, station ids included."""
    from tostools.rinex.domes import domes_or_skip

    resolved = domes_or_skip(value) or None
    out = fill_skeleton(
        POISONED_SKL, SkeletonMetadata(marker_name="GONH", marker_number=resolved)
    )
    assert "MARKER NUMBER" not in _labels(out)
