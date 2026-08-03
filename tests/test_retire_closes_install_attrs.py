"""Retiring a device must close the attributes that described its install.

Closing the join alone is what left ISAK antenna 4527 in a state where TOS
still reported Ísakot's ``antenna_height`` as its CURRENT value while the unit
sat in warehouse B9 — found on 2026-08-03, four days after the swap, by
``tos audit missing-attributes``.

``_retire_old_child`` is the single choke point every swap verb funnels
through (``replace_antenna``, ``replace_radome``, ``replace_modem``,
``replace_sim``, ``close_join``), so the fix belongs there rather than in each
verb.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tostools.audit_missing_attributes import INSTALL_SCOPED_CODES

from receivers.cfg.operations import (
    _close_install_scoped_attributes,
    _retire_old_child,
)

OLD_ANT_ID = 4527
EFF_DATE = "2026-07-30T12:00:00"


def _writer():
    w = MagicMock()
    w.dry_run = True
    w.close_attribute_period.return_value = {"ok": True}
    w.get_open_parent_join.return_value = {"id": 5473}
    return w


def _closed_codes(w):
    return sorted(call.args[1] for call in w.close_attribute_period.call_args_list)


class TestRetireClosesInstallScopedAttributes:
    def test_every_install_scoped_code_is_closed(self):
        w = _writer()
        _retire_old_child(w, OLD_ANT_ID, EFF_DATE)
        assert _closed_codes(w) == sorted(INSTALL_SCOPED_CODES)

    def test_closed_at_the_removal_date(self):
        w = _writer()
        _retire_old_child(w, OLD_ANT_ID, EFF_DATE)
        for call in w.close_attribute_period.call_args_list:
            assert call.args[0] == OLD_ANT_ID
            assert call.args[2] == EFF_DATE

    def test_device_state_codes_are_not_touched(self):
        """status/comment/owner describe the device wherever it now is, and the
        calling verbs already transition them via old_status/old_comment."""
        w = _writer()
        _retire_old_child(w, OLD_ANT_ID, EFF_DATE)
        closed = _closed_codes(w)
        assert "status" not in closed
        assert "comment" not in closed
        assert "owner" not in closed

    def test_join_is_still_closed(self):
        """The tidy-up must not displace the operation's actual payload."""
        w = _writer()
        _retire_old_child(w, OLD_ANT_ID, EFF_DATE)
        w.patch_entity_connection.assert_called_once_with(5473, time_to=EFF_DATE)

    def test_warehouse_move_also_closes_them(self):
        """The antenna path reparents to B9 rather than leaving it parentless —
        that is the branch ISAK 4527 actually took."""
        w = _writer()
        _retire_old_child(w, OLD_ANT_ID, EFF_DATE, to_warehouse_eid=4)
        assert _closed_codes(w) == sorted(INSTALL_SCOPED_CODES)
        w.move_device.assert_called_once_with(OLD_ANT_ID, 4, EFF_DATE)

    def test_no_device_means_no_writes(self):
        w = _writer()
        assert _retire_old_child(w, None, EFF_DATE) is None
        w.close_attribute_period.assert_not_called()


class TestFailureIsNotFatal:
    def test_a_failing_close_does_not_sink_the_swap(self):
        """The join close and the new device are the real payload; a tidy-up
        that raises must not roll the operation back to a worse state than it
        started in."""
        w = _writer()
        w.close_attribute_period.side_effect = RuntimeError("TOS 500")
        _retire_old_child(w, OLD_ANT_ID, EFF_DATE)
        w.patch_entity_connection.assert_called_once_with(5473, time_to=EFF_DATE)

    def test_partial_failure_still_closes_the_rest(self):
        w = _writer()
        w.close_attribute_period.side_effect = [
            RuntimeError("TOS 500"),
            {"ok": True},
            {"ok": True},
            {"ok": True},
        ]
        closed = _close_install_scoped_attributes(w, OLD_ANT_ID, EFF_DATE)
        assert len(closed) == len(INSTALL_SCOPED_CODES) - 1

    def test_noop_closes_are_not_reported(self):
        """close_attribute_period returns None when there was nothing open."""
        w = _writer()
        w.close_attribute_period.return_value = None
        assert _close_install_scoped_attributes(w, OLD_ANT_ID, EFF_DATE) == {}


class TestSharedDefinition:
    def test_code_set_comes_from_tostools(self):
        """One definition, so the write path closes exactly what the audit
        reports — the same reasoning as the enforcement primitive."""
        assert INSTALL_SCOPED_CODES == {
            "antenna_height",
            "antenna_offset_north",
            "antenna_offset_east",
            "azimuth",
        }
