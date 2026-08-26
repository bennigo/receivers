"""Septentrio mosaic-X5 receiver support.

The mosaic-X5 is a Septentrio OEM GNSS module. For data acquisition and health
monitoring it is protocol-compatible with the PolaRX5 (same SBF format, same
FTP 2160 / HTTP 8060 / control 28784 ports), so :class:`MosaicX5` subclasses
:class:`~receivers.septentrio.polarx5.PolaRX5` and reuses all of its download,
SBF→RINEX conversion and health machinery.

It differs from a full PolaRx5 in two ways that this class handles:

1. **Identity.** It must report ``mosaic-X5`` (not ``PolaRX5``) so RINEX headers
   and TOS metadata are correct. :meth:`get_receiver_type` is overridden and the
   PolaRX5 download paths report identity via ``self.get_receiver_type()``.

2. **Non-standard on-disk layout.** mosaic-X5 units in the IMO fleet are often
   provisioned with a *single* logging session under a non-fleet directory name
   (e.g. ``GRB0051``) and default Septentrio file naming (``gonh1620.26_.A``),
   rather than the fleet's ``LOG1_15s_24hr/…/<STATION>#Rin2`` 3-session layout.
   This is the general "non-standard remote layout" mechanism: a station may
   declare the layout in ``stations.cfg`` and :meth:`_build_remote_template`
   honours it. The same keys work for any receiver whose disk layout deviates
   from the fleet default — mosaic is just the first consumer.

``stations.cfg`` override keys (all optional; absent ⇒ standard PolaRX5 layout)::

    remote_session_dir      = GRB0051
        # on-receiver directory that replaces the LOG* session directory
    remote_filename_pattern = {marker_lc}%j0.%y_.A
        # gtimes template for the filename. {station}/{marker_lc}/{compression}
        # are substituted first, then gtimes resolves %y %j %m %d etc.
    remote_sessions         = 15s_24hr
        # comma-separated sessions this receiver actually logs (informational;
        # the scheduler's capability gate is fail-open, so this documents intent)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .polarx5 import PolaRX5

logger = logging.getLogger(__name__)

#: Canonical receiver-type string as it appears in stations.cfg / TOS / RINEX.
RECEIVER_TYPE = "mosaic-X5"


class MosaicX5(PolaRX5):
    """Septentrio mosaic-X5 — PolaRX5-compatible with identity + layout overrides."""

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        super().__init__(station_id, station_info)
        # Read optional non-standard-layout overrides from station config. The
        # adapted config can carry them under "receiver", "station", or top level.
        self._remote_session_dir = self._cfg_override(
            station_info, "remote_session_dir"
        )
        self._remote_filename_pattern = self._cfg_override(
            station_info, "remote_filename_pattern"
        )
        remote_sessions = self._cfg_override(station_info, "remote_sessions")
        self._remote_sessions = (
            [s.strip() for s in remote_sessions.split(",") if s.strip()]
            if remote_sessions
            else None
        )
        if self._remote_session_dir or self._remote_filename_pattern:
            self.logger.info(
                "mosaic-X5 %s using non-standard layout: dir=%s pattern=%s",
                station_id,
                self._remote_session_dir or "(default)",
                self._remote_filename_pattern or "(default)",
            )

    @staticmethod
    def _cfg_override(station_info: Dict[str, Any], key: str) -> str | None:
        """Look up an override key across the adapted-config sections.

        Returns the first non-empty value found under ``receiver``, ``station``,
        or the top-level dict, else ``None``.
        """
        for section in (
            station_info.get("receiver"),
            station_info.get("station"),
            station_info,
        ):
            if isinstance(section, dict):
                val = section.get(key)
                if val:
                    return str(val)
        return None

    def get_receiver_type(self) -> str:
        """Report the canonical mosaic-X5 identity (not the class name)."""
        return RECEIVER_TYPE

    def _build_remote_template(self, session: str, compression: str) -> str:
        """Build the remote path template, honouring non-standard layout overrides.

        Falls back to the standard PolaRX5 fleet layout when no overrides are set,
        so a mosaic provisioned like the rest of the fleet needs no special config.
        """
        if not (self._remote_session_dir or self._remote_filename_pattern):
            return super()._build_remote_template(session, compression)

        session_dir = self._remote_session_dir or self.session_map[session][1]
        if self._remote_filename_pattern:
            filename = self._remote_filename_pattern.format(
                station=self.station_id,
                marker_lc=self.station_id.lower(),
                compression=compression,
            )
        else:
            filename = f"{self.station_id}#Rin2_{compression}"
        return f"{self.base_path}{session_dir}/%y%j/{filename}"
