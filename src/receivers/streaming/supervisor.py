"""Supervise per-station BNC daemons (RTCM3 → RINEX stream capture).

Ports the legacy ``rtcm2rinex.sh`` watchdog: BNC runs as one persistent process
per stream station; this supervisor compares the set of configured stations
(``rtcm2rinex-<SID>.bnc`` files) against the BNC processes currently running and
(re)starts any that are missing. Intended to be driven periodically by the
scheduler.

Process listing and spawning are injectable so the supervision logic is fully
unit-testable without a live BNC binary.

Liveness is necessary but **not sufficient**: a BNC daemon can sit running and
connected-but-refused for days, producing nothing. HRIC did exactly that from
2026-08-30 07:02 — process alive the whole time, 175-260 "Wrong caster response"
per day in its own log, zero RINEX written, and every liveness check green
(todo #167 — the underlying cause was a flat battery at the station). So this
supervisor also reports **output freshness**: a running station whose newest
``.rnx`` is older than ``stale_after`` is flagged.

Flagged, deliberately **not** auto-restarted. HRIC's stream was dead because the
station had no power; bouncing BNC would have changed nothing and would have
thrown away the one signal that something was wrong. Staleness means "a human
should look", not "retry".
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .bnc_config import bnc_config_filename

logger = logging.getLogger(__name__)

#: A stream station writes one RINEX per hour, so two hours without output means
#: at least one whole file was missed — long enough not to trip on the boundary
#: of the current hour's still-open file, short enough to catch an outage the
#: same morning rather than a day later.
DEFAULT_STALE_AFTER = timedelta(hours=2)

#: Station id embedded in a BNC config path/cmdline, e.g. ``rtcm2rinex-GONH.bnc``.
_STATION_RE = re.compile(r"rtcm2rinex-([0-9A-Za-z]+)\.bnc")

ProcessLister = Callable[[], Sequence[str]]
Spawner = Callable[[Sequence[str]], None]


def _default_process_lister() -> List[str]:
    """Return command-line strings of running processes (best-effort)."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover - env
        logger.warning("Could not list processes: %s", e)
        return []


def _default_spawner(cmd: Sequence[str]) -> None:
    """Launch ``cmd`` as a detached daemon (nohup-equivalent)."""
    subprocess.Popen(  # noqa: S603 - cmd is built from trusted config paths
        list(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


@dataclass
class SuperviseResult:
    """Outcome of a single supervision pass."""

    configured: List[str] = field(default_factory=list)
    running: List[str] = field(default_factory=list)
    started: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    #: Running stations producing no fresh output — alive but mute. Never
    #: auto-restarted; see the module docstring.
    stale: List[str] = field(default_factory=list)

    @property
    def all_running(self) -> bool:
        return not self.started and not self.failed

    @property
    def all_healthy(self) -> bool:
        """Every station running *and* producing fresh output."""
        return self.all_running and not self.stale


class StreamSupervisor:
    """Keep one BNC daemon alive per configured stream station."""

    def __init__(
        self,
        bnc_path: str | Path,
        config_dir: str | Path,
        *,
        process_lister: Optional[ProcessLister] = None,
        spawner: Optional[Spawner] = None,
        rt_base: Optional[str | Path] = None,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.bnc_path = Path(bnc_path)
        self.config_dir = Path(config_dir)
        self._list_cmdlines: ProcessLister = process_lister or _default_process_lister
        self._spawn: Spawner = spawner or _default_spawner
        #: Where BNC writes per-station RINEX. Without it, freshness is skipped
        #: (liveness-only) rather than reported as false-healthy.
        self.rt_base = Path(rt_base).expanduser() if rt_base else None
        self.stale_after = stale_after
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))

    def last_output_at(self, station_id: str) -> Optional[datetime]:
        """Mtime of the newest RINEX BNC wrote for *station_id*, or ``None``.

        ``None`` means "cannot tell" — no ``rt_base`` configured, no station
        directory, or no files yet — and is never treated as stale, so a
        misconfigured path degrades to today's liveness-only behaviour instead
        of alarming on every station.
        """
        if not self.rt_base:
            return None
        station_dir = self.rt_base / station_id
        if not station_dir.is_dir():
            return None
        newest: Optional[float] = None
        for path in station_dir.glob("*.rnx"):
            try:
                mtime = path.stat().st_mtime
            except OSError:  # vanished mid-scan
                continue
            if newest is None or mtime > newest:
                newest = mtime
        return datetime.fromtimestamp(newest, UTC) if newest is not None else None

    def stale_stations(self, running: Sequence[str]) -> List[str]:
        """Running stations whose newest output is older than ``stale_after``."""
        cutoff = self._now() - self.stale_after
        stale = []
        for station_id in running:
            last = self.last_output_at(station_id)
            if last is not None and last < cutoff:
                stale.append(station_id)
        return sorted(stale)

    def config_path(self, station_id: str) -> Path:
        """Path to the BNC config file for a station."""
        return self.config_dir / bnc_config_filename(station_id)

    def configured_stations(self) -> List[str]:
        """Stations with a ``rtcm2rinex-<SID>.bnc`` config present, sorted."""
        if not self.config_dir.is_dir():
            return []
        ids = set()
        for path in self.config_dir.glob("rtcm2rinex-*.bnc"):
            m = _STATION_RE.search(path.name)
            if m:
                ids.add(m.group(1))
        return sorted(ids)

    def running_stations(self) -> List[str]:
        """Stations whose BNC daemon is currently running, sorted."""
        ids = set()
        for cmdline in self._list_cmdlines():
            m = _STATION_RE.search(cmdline)
            if m:
                ids.add(m.group(1))
        return sorted(ids)

    def start_station(self, station_id: str) -> bool:
        """Start the BNC daemon for one station. Returns True on launch."""
        cfg = self.config_path(station_id)
        if not cfg.exists():
            logger.warning(
                "No BNC config for %s at %s — cannot start stream", station_id, cfg
            )
            return False
        cmd = [str(self.bnc_path), "--conf", str(cfg), "-nw"]
        try:
            self._spawn(cmd)
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("Failed to start BNC for %s: %s", station_id, e)
            return False
        logger.info("Started BNC stream capture for %s", station_id)
        return True

    def supervise(self) -> SuperviseResult:
        """Start any configured station whose BNC daemon is not running."""
        configured = self.configured_stations()
        running = set(self.running_stations())
        result = SuperviseResult(configured=configured, running=sorted(running))
        for station_id in configured:
            if station_id in running:
                continue
            if self.start_station(station_id):
                result.started.append(station_id)
            else:
                result.failed.append(station_id)
        # Freshness is checked on stations that were already running: one just
        # started has no output yet and would be a guaranteed false positive.
        result.stale = self.stale_stations(sorted(running))
        for station_id in result.stale:
            last = self.last_output_at(station_id)
            logger.warning(
                "Stream %s is RUNNING BUT MUTE — newest RINEX is %s (older than %s). "
                "BNC is alive, so this is not a supervisor failure: check the "
                "station is powered and reaching the caster (%s/RinexObs.log_*)",
                station_id,
                last.isoformat() if last else "unknown",
                self.stale_after,
                (self.rt_base / station_id) if self.rt_base else "?",
            )
        if result.started or result.failed:
            logger.info(
                "Stream supervise: %d configured, %d running, started %s%s",
                len(configured),
                len(running),
                result.started or "[]",
                f", FAILED {result.failed}" if result.failed else "",
            )
        return result
