"""Tests for the attribute-period boundary-alignment advisory.

Guards the near-miss from 2026-08-10: ELDC's ``software_version`` was being
repaired to mirror ``firmware_version``'s three periods, whose boundaries sit at
``T00:00:00``. A bare ``--date 2025-01-06`` resolves to **noon**, which would
have written the boundary 12 hours off — leaving a window where the two chains
disagreed about which version was in force, with nothing downstream erroring and
a date-only view of TOS looking perfectly correct.

The advisory is deliberately non-blocking: same-day-different-time is a strong
hint of an un-copied boundary, but two genuine events in one afternoon are
legitimate.
"""

from receivers.cfg.operations import _sibling_boundary_warnings


class _FakeWriter:
    """Minimal stand-in exposing only get_entity_history."""

    def __init__(self, attributes, raise_exc=None):
        self._attributes = attributes
        self._raise = raise_exc

    def get_entity_history(self, id_entity):  # noqa: ARG002
        if self._raise:
            raise self._raise
        return {"attributes": self._attributes}


FIRMWARE_CHAIN = [
    {
        "code": "firmware_version",
        "date_from": "2021-01-05T00:00:00",
        "date_to": "2025-01-06T00:00:00",
    },
    {
        "code": "firmware_version",
        "date_from": "2025-01-06T00:00:00",
        "date_to": "2026-01-14T00:00:00",
    },
    {"code": "firmware_version", "date_from": "2026-01-14T00:00:00", "date_to": None},
]


def test_warns_when_bare_date_lands_on_a_sibling_day_at_noon():
    """The exact 2026-08-10 near-miss: noon vs a midnight sibling boundary."""
    w = _FakeWriter(FIRMWARE_CHAIN)

    out = _sibling_boundary_warnings(
        w, 21123, "software_version", "2025-01-06T12:00:00"
    )

    assert len(out) == 1, f"expected one deduped warning, got {out}"
    assert "firmware_version" in out[0]
    # It must suggest the exact timestamp to copy, not just complain.
    assert "--date 2025-01-06T00:00:00" in out[0]


def test_silent_when_the_boundary_matches_the_sibling_exactly():
    """A correctly mirrored boundary must not nag."""
    w = _FakeWriter(FIRMWARE_CHAIN)
    assert (
        _sibling_boundary_warnings(w, 21123, "software_version", "2025-01-06T00:00:00")
        == []
    )


def test_silent_on_a_day_no_sibling_shares():
    w = _FakeWriter(FIRMWARE_CHAIN)
    assert (
        _sibling_boundary_warnings(w, 21123, "software_version", "2025-06-30T12:00:00")
        == []
    )


def test_ignores_its_own_code():
    """Its own chain's boundaries are not evidence of a mirroring mistake."""
    w = _FakeWriter(
        [
            {
                "code": "software_version",
                "date_from": "2025-01-06T00:00:00",
                "date_to": None,
            },
        ]
    )
    assert (
        _sibling_boundary_warnings(w, 21123, "software_version", "2025-01-06T12:00:00")
        == []
    )


def test_deduplicates_adjacent_periods_sharing_an_instant():
    """date_to of one period and date_from of the next are the same moment."""
    w = _FakeWriter(FIRMWARE_CHAIN)
    out = _sibling_boundary_warnings(
        w, 21123, "software_version", "2026-01-14T12:00:00"
    )
    # 2026-01-14T00:00:00 appears as both a date_to and a date_from.
    assert len(out) == 1, f"adjacent-period duplicates not collapsed: {out}"


def test_advisory_never_breaks_the_write_on_api_failure():
    """A read failure must degrade to silence, not raise into the caller."""
    w = _FakeWriter(FIRMWARE_CHAIN, raise_exc=RuntimeError("TOS unreachable"))
    assert (
        _sibling_boundary_warnings(w, 21123, "software_version", "2025-01-06T12:00:00")
        == []
    )


def test_bare_date_without_time_component_is_skipped():
    w = _FakeWriter(FIRMWARE_CHAIN)
    assert _sibling_boundary_warnings(w, 21123, "software_version", "2025-01-06") == []
