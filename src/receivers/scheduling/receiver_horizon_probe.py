"""Scheduled per-receiver horizon probe (unified file index slice-2b.3).

Lists each receiver's date-directory INDEX to find the OLDEST file the station
still holds, and records it (``receiver_horizon``) via the shared upsert. That
oldest date becomes the real fetch floor for ``missing_on_receiver`` — and, later,
the retention floor for prune — replacing the conservative static
``receiver_buffer_depth`` seed.

Why a dedicated pass and not a hot-path hook: a normal download run lists only the
recently requested date directories, so it never sees the receiver's oldest file.
This job lists the top-level index (one extra listing per station/session), walks
to the oldest non-empty directory, and records the minimum parseable ``(date,
hour)`` there.

Design constraints (advisor-reviewed):

* **Reuse the solved listing, no hardcoded paths.** Each receiver object already
  resolves its own ``base_path`` / ``session_map`` / ports / credentials / passive
  mode from ``stations.cfg`` over the per-type ``receivers.cfg`` defaults (e.g. a
  Trimble station on ``/External/`` or VARG's ``%Y%m/%d`` layout). We read those
  resolved values off the receiver object rather than reconstructing paths.
* **Silent under-report is the failure mode to guard.** A too-recent horizon pushes
  the fetch floor past fetchable slots and drops them with no error. So we record
  ONLY when we truly reached the oldest non-empty directory with a parseable file;
  on any listing failure we record nothing and the safe static floor stays in
  force. Future-dated parses are rejected in the shared upsert.
* **Connection hygiene.** One connection per station, sessions looped on it,
  sequential across the fleet (lossy 3G/4G links trip per-source penalties on
  bursts). Daily cadence is ample — the horizon advances ~1 day/day.

Never raises: a probe hiccup must not disturb the scheduler.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from ftplib import FTP
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ..db.tx import read_only_cursor

logger = logging.getLogger("receivers.scheduler.horizon")

# A probe result per session: the oldest (date, hour) still on the receiver.
# hour is None for daily sessions. Septentrio derives the date from the %y%j
# day-directory name; Trimble/Leica from the filenames in the oldest leaf.
Horizon = Tuple[date, Optional[int]]

# Fallback receiver_type -> sessions map, matching the receiver_buffer_depth seed
# (migration 054). Used only when the DB is unreachable; normally the live
# receiver_buffer_depth table drives the scope so it tracks missing_on_receiver.
_FALLBACK_SESSIONS: Dict[str, List[str]] = {
    "polarx5": ["15s_24hr", "1Hz_1hr", "status_1hr"],
    "mosaic-x5": ["15s_24hr", "1Hz_1hr", "status_1hr"],
    "netr9": ["15s_24hr", "1Hz_1hr"],
    "netr5": ["15s_24hr", "1Hz_1hr"],
    "netrs": ["15s_24hr"],
    "g10": ["15s_24hr"],
}

_SEPTENTRIO_TYPES = {"polarx5", "mosaic-x5"}
_TRIMBLE_TYPES = {"netr9", "netr5", "netrs"}
_LEICA_TYPES = {"g10"}


def _run_horizon_probe_job(
    probe_cfg: Optional[Dict[str, Any]] = None,
    station_filter: Optional[List[str]] = None,
) -> None:
    """One horizon-probe pass over the active fleet.

    Args:
        probe_cfg: scheduler.yaml ``[receiver_horizon_probe]`` section. Honoured
            keys: ``sessions`` (restrict which sessions to probe; default = the
            receiver's seeded sessions).
        station_filter: optional explicit station list (CLI / manual trigger).
    """
    probe_cfg = probe_cfg or {}
    try:
        from ..cli.main import get_all_station_configs
        from ..health import FileTracker
    except Exception:  # noqa: BLE001 - job must never kill the scheduler
        logger.exception("horizon-probe: import failed — skipped")
        return

    all_stations = get_all_station_configs()
    active = {
        sid: cfg
        for sid, cfg in all_stations.items()
        if cfg.get("enabled", True)
        and cfg.get("station_status") not in ("discontinued", "inactive")
        and (cfg.get("receiver_type") or "").strip()
        and (cfg.get("health_check") != "passive")
    }
    if station_filter:
        wanted = {s.upper() for s in station_filter}
        active = {sid: cfg for sid, cfg in active.items() if sid.upper() in wanted}
    if not active:
        logger.info("horizon-probe: no active stations to probe")
        return

    from ..utils.download_tracker import upsert_receiver_horizon

    tracker = FileTracker()
    if not tracker.connect():
        logger.warning("horizon-probe: cannot connect to gps_health — skipped")
        return

    session_override = probe_cfg.get("sessions")
    sessions_by_type = _sessions_by_receiver_type(tracker._conn)

    probed = recorded = failed = 0
    try:
        for sid in sorted(active):
            cfg = active[sid]
            rtype = (cfg.get("receiver_type") or "").strip().lower()
            sessions = session_override or sessions_by_type.get(
                rtype, _FALLBACK_SESSIONS.get(rtype, [])
            )
            if not sessions:
                continue
            try:
                # {session: (oldest_date, oldest_hour)} for the sessions reached.
                # cfg IS the full station_config (get_all_station_configs returns
                # get_station_config per station) — no second gps_parser load.
                horizons = _probe_station(sid, cfg, rtype, sessions)
            except Exception as e:  # noqa: BLE001 - per-station isolation
                logger.debug("horizon-probe: %s probe failed: %s", sid, e)
                failed += 1
                continue

            for session in sessions:
                probed += 1
                horizon = horizons.get(session)
                if horizon is None:
                    continue
                is_hourly = "1hr" in session.lower()
                if upsert_receiver_horizon(
                    tracker._conn, sid, session, is_hourly, horizon[0], horizon[1]
                ):
                    recorded += 1
    finally:
        tracker.close()

    logger.info(
        "horizon-probe complete: %d (station,session) probed, %d horizons "
        "recorded, %d stations failed to list",
        probed,
        recorded,
        failed,
    )


def _sessions_by_receiver_type(conn) -> Dict[str, List[str]]:
    """receiver_type -> [session_type] from receiver_buffer_depth (the same source
    ``missing_on_receiver`` uses), so the probe scope matches the differential.
    Falls back to the static seed map on any error."""
    try:
        with read_only_cursor(conn) as cur:
            cur.execute(
                "SELECT lower(receiver_type), session_type FROM receiver_buffer_depth"
            )
            out: Dict[str, List[str]] = {}
            for rtype, session in cur.fetchall():
                out.setdefault(rtype, []).append(session)
        return out or dict(_FALLBACK_SESSIONS)
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: receiver_buffer_depth query failed: %s", e)
        return dict(_FALLBACK_SESSIONS)


def _probe_station(
    sid: str, station_config: Dict[str, Any], rtype: str, sessions: List[str]
) -> Dict[str, Horizon]:
    """Dispatch to the receiver-specific oldest-directory prober.

    Returns ``{session: (oldest_date, oldest_hour)}`` only for sessions whose
    oldest file was located; sessions we could not reach are absent (they keep
    the safe static floor). One connection per station, opened by the per-type
    helper.
    """
    if rtype in _SEPTENTRIO_TYPES:
        return _probe_septentrio(sid, station_config, sessions)
    if rtype in _TRIMBLE_TYPES:
        return _probe_trimble(sid, station_config, sessions)
    if rtype in _LEICA_TYPES:
        return _probe_leica(sid, station_config, sessions)
    logger.debug("horizon-probe: %s unsupported receiver type %r", sid, rtype)
    return {}


# --------------------------------------------------------------------------- #
# Septentrio (PolaRX5 / mosaic-X5) — FTP, {base}{session_dir}/{%y%j}/{files}
# --------------------------------------------------------------------------- #
def _probe_septentrio(
    sid: str, station_config: Dict[str, Any], sessions: List[str]
) -> Dict[str, Horizon]:
    """Oldest (date, hour) per session for a Septentrio receiver.

    The receiver stores each session under ``{base}{session_dir}/`` in ``%y%j``
    (year + day-of-year) day directories — e.g. ``/DSK1/SSN/LOG1_15s_24hr/26119/``
    for 2026 doy 119. The DAY-DIR NAME is the canonical date: daily SBF/RINEX
    filenames inside do not carry a full timestamp, so we take the date from the
    oldest non-empty directory (not the filenames). For hourly sessions the hour
    is refined from the (timestamped) filenames in that directory.
    """
    from ..cli.main import create_receiver
    from ..utils.download_tracker import parse_date_from_filename

    # create_receiver returns a BaseReceiver; the concrete PolaRX5/MosaicX5
    # subclass carries the resolved connection attrs — read via getattr (also
    # keeps the probe a safe no-op if a subclass ever lacks one).
    receiver = create_receiver(sid, station_config)
    base = getattr(receiver, "base_path", None)  # config-resolved, e.g. /DSK1/SSN/
    session_map = getattr(receiver, "session_map", {}) or {}
    ip = getattr(receiver, "ip_number", None)
    port = getattr(receiver, "ip_port", None)
    out: Dict[str, Horizon] = {}
    if not (base and ip and port and session_map):
        return out
    ftp: Optional[FTP] = None
    try:
        ftp = FTP()
        ftp.connect(ip, port, timeout=getattr(receiver, "connection_timeout", 20))
        if getattr(receiver, "ftp_anonymous", True):
            ftp.login("anonymous")
        else:
            ftp.login(
                getattr(receiver, "ftp_username", None) or "anonymous",
                getattr(receiver, "ftp_password", None) or "",
            )
        ftp.set_pasv(getattr(receiver, "pasv", True))

        for session in sessions:
            mapping = session_map.get(session)
            if not mapping:
                continue
            index = f"{base}{mapping[1]}/"
            is_hourly = "1hr" in session.lower()
            day_dirs = _sorted_numeric_subdirs(_ftp_names(ftp, index))
            horizon = _septentrio_oldest(
                sid, ftp, index, day_dirs, is_hourly, parse_date_from_filename
            )
            if horizon is not None:
                out[session] = horizon
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: %s septentrio FTP failed: %s", sid, e)
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                try:
                    ftp.close()
                except Exception:  # noqa: BLE001
                    pass
    return out


def _septentrio_oldest(
    sid: str,
    ftp: FTP,
    index: str,
    day_dirs: List[str],
    is_hourly: bool,
    parse_fn,
) -> Optional[Horizon]:
    """Walk ``%y%j`` day dirs oldest→newest; first non-empty one is the horizon.

    Empty old day directories (stale shells the receiver keeps) are skipped so
    the recorded date is the TRUE oldest. Date comes from the directory name;
    for hourly sessions the hour is the minimum timestamped-filename hour in that
    directory (None if none parse)."""
    for name in day_dirs:
        d = _parse_yyjjj(name)
        if d is None:
            continue
        files = [f for f in _ftp_names(ftp, f"{index}{name}/") if f not in (".", "..")]
        if not files:
            continue
        hour: Optional[int] = None
        if is_hourly:
            hours = [
                p[1]
                for f in files
                if (p := parse_fn(f, sid)) and p[0] == d and p[1] is not None
            ]
            hour = min(hours) if hours else None
        return d, hour
    return None


def _ftp_names(ftp: FTP, path: str) -> List[str]:
    """``nlst <path>`` a directory, returning basenames; [] on any error (e.g.
    550). Used for Septentrio, whose paths have no spaces."""
    try:
        return [_basename(e) for e in ftp.nlst(path)]
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: nlst %s failed: %s", path, e)
        return []


def _ftp_cwd_names(ftp: FTP, path: str) -> List[str]:
    """``cwd`` into a directory then bare ``nlst`` — for paths a server may not
    accept as an NLST argument (e.g. the Leica G10's "/SD Card/Data/…" with a
    space). Returns basenames; [] on any error."""
    try:
        ftp.cwd(path)
        return [_basename(e) for e in ftp.nlst()]
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: cwd+nlst %s failed: %s", path, e)
        return []


# --------------------------------------------------------------------------- #
# Trimble (NetR9 / NetR5 / NetRS) — HTTP, {base}/{YYYYMM}[/{DD}]/{subdir}/{files}
# --------------------------------------------------------------------------- #
def _probe_trimble(
    sid: str, station_config: Dict[str, Any], sessions: List[str]
) -> Dict[str, Horizon]:
    """Oldest (date, hour) per session for a NetR9/NetR5 receiver.

    Trimble files carry a full ``YYYYMMDDHHMM`` timestamp, so the oldest is read
    from the filenames in the oldest leaf directory. NetRS is intentionally
    unreached here: its HTTP API has no ``directory`` verb (returns "Invalid
    verb/object combination"), so ``_trimble_names`` finds no month dirs and the
    station safely keeps its static floor.
    """
    from ..cli.main import create_receiver

    receiver = create_receiver(sid, station_config)
    http = getattr(receiver, "http_client", None)
    resolve_base = getattr(receiver, "_resolve_base_path", None)
    if http is None or resolve_base is None:
        return {}
    base = resolve_base().rstrip("/") or "/Internal"  # /Internal, /External
    out: Dict[str, Horizon] = {}
    try:
        month_dirs = sorted(
            n for n in _trimble_names(http, base + "/") if re.fullmatch(r"\d{6}", n)
        )
        if not month_dirs:
            return {}
        for session in sessions:
            subdir = _trimble_session_subdir(receiver, session)
            if not subdir:
                continue
            horizon = _trimble_oldest(sid, http, base, month_dirs, subdir)
            if horizon is not None:
                out[session] = horizon
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: %s trimble HTTP failed: %s", sid, e)
    return out


def _trimble_oldest(
    sid: str,
    http,
    base: str,
    month_dirs: List[str],
    subdir: str,
) -> Optional[Horizon]:
    """Oldest (date, hour) from the oldest Trimble leaf that has parseable files.

    Handles both the default ``%Y%m`` layout (files under
    ``{base}/{YYYYMM}/{subdir}/``) and the per-day ``%Y%m/%d`` layout (VARG:
    ``{base}/{YYYYMM}/{DD}/{subdir}/``) by inspecting each month directory's
    entries rather than assuming a fixed depth. Walks oldest→newest until a leaf
    yields a parseable filename.
    """
    from ..utils.download_tracker import _oldest_from_listing

    for ym in month_dirs:
        month_entries = _trimble_names(http, f"{base}/{ym}/")
        if subdir in month_entries:
            leaves = [f"{base}/{ym}/{subdir}/"]  # default layout
        else:
            # per-day layout: DD dirs between month and session subdir
            leaves = [
                f"{base}/{ym}/{dd}/{subdir}/"
                for dd in sorted(d for d in month_entries if re.fullmatch(r"\d{2}", d))
            ]
        for leaf in leaves:
            files = [f for f in _trimble_names(http, leaf) if _looks_like_file(f)]
            horizon = _oldest_from_listing(files, sid) if files else None
            if horizon is not None:
                return horizon
    return None


def _looks_like_file(name: str) -> bool:
    """A leaf entry that is a data file (has an extension), not a subdirectory."""
    return "." in name and name not in (".", "..")


def _trimble_names(http, path: str) -> List[str]:
    """All ``name=`` entries (files AND subdirs) from a ``/prog/show?directory``
    response. Unlike the download client's ``get_directory_listing`` (which keeps
    only files matching ``size=`` + station id), this keeps the ``YYYYMM`` month
    directories that make up the index. [] on any error."""
    endpoint = f"/prog/show?directory&path={quote(path)}"
    try:
        success, response, _err = http.get_url(endpoint)
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: trimble listing %s failed: %s", path, e)
        return []
    if not success or not response:
        return []
    names: List[str] = []
    for line in response.split("\n"):
        idx = line.find("name=")
        if idx == -1:
            continue
        start = idx + 5
        end = line.find(" ", start)
        name = (line[start:] if end == -1 else line[start:end]).strip()
        if name and name not in (".", ".."):
            names.append(_basename(name))
    return names


def _trimble_session_subdir(receiver, session: str) -> Optional[str]:
    """The session subdirectory name, resolved the same way the NetR9/NetRS
    download path does: per-station ``session_map_<session>`` override in
    stations.cfg, else the ``[netr9]``/``[netrs]`` receivers.cfg default. The
    mapping is ``"<letter>,<subdir>"``."""
    key = f"session_map_{session.lower()}"
    per_station = receiver.station_info.get("receiver", {}).get(key)
    default = getattr(receiver, "netr9_config", None) or getattr(
        receiver, "netrs_config", {}
    )
    mapping = per_station or default.get(key)
    if not mapping or "," not in mapping:
        return None
    return mapping.split(",", 1)[1].strip() or None


# --------------------------------------------------------------------------- #
# Leica G10 — FTP, flat {base}{subdir}/{files} (day-of-year filenames)
# --------------------------------------------------------------------------- #
def _probe_leica(
    sid: str, station_config: Dict[str, Any], sessions: List[str]
) -> Dict[str, Horizon]:
    """Oldest (date, hour) per session for a Leica G10 receiver.

    Daily G10 files sit flat in ``{base}{subdir}/`` with day-of-year names
    (``SSSS<doy><letter>.zip``). The year is not in the filename, so a listing
    that spans a year boundary can misparse the oldest to a wrong/future date —
    the shared upsert's future-date guard rejects the future case.
    """
    from ..cli.main import create_receiver
    from ..utils.download_tracker import _oldest_from_listing

    receiver = create_receiver(sid, station_config)
    leica_cfg = getattr(receiver, "leica_config", {}) or {}
    base = leica_cfg.get("base_path", "/SD Card/Data/")
    receiver_cfg = station_config.get("receiver", {})
    ftp_port = int(receiver_cfg.get("ftpport") or leica_cfg.get("ftp_port", 2160))
    out: Dict[str, Horizon] = {}
    ftp: Optional[FTP] = None
    try:
        ftp = FTP()
        ftp.connect(station_config["router"]["ip"], ftp_port, timeout=20)
        ftp.login()  # anonymous
        ftp.set_pasv(True)
        for session in sessions:
            mapping = leica_cfg.get(f"session_map_{session.lower()}")
            if not mapping or "," not in mapping:
                continue
            subdir = mapping.split(",", 1)[1].strip()
            leaf = f"{base}{subdir}/"
            # The G10 storage path contains a space ("/SD Card/Data/...") — a bare
            # NLST <path> arg may not parse on its embedded FTP server, so mirror
            # the working leica client: cwd into the dir, then NLST with no arg.
            names = _ftp_cwd_names(ftp, leaf)
            files = [n for n in names if _looks_like_file(n)]
            horizon = _oldest_from_listing(files, sid) if files else None
            if horizon is not None:
                out[session] = horizon
    except Exception as e:  # noqa: BLE001
        logger.debug("horizon-probe: %s leica FTP failed: %s", sid, e)
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                try:
                    ftp.close()
                except Exception:  # noqa: BLE001
                    pass
    return out


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _parse_yyjjj(name: str) -> Optional[date]:
    """Parse a Septentrio ``%y%j`` day-directory name (e.g. ``26119`` → 2026 doy
    119 → 2026-04-29). Returns None if it is not a valid year+day-of-year — which
    also rejects GPS-week-style dir names, so a receiver using a different layout
    simply keeps its static floor rather than recording a wrong date."""
    if not name.isdigit() or len(name) != 5:
        return None
    year = 2000 + int(name[:2])
    doy = int(name[2:])
    if not 1 <= doy <= 366:
        return None
    try:
        return date(year, 1, 1) + timedelta(days=doy - 1)
    except ValueError:
        return None


def _sorted_numeric_subdirs(names: List[str]) -> List[str]:
    """Numeric subdirectory names (``%y%j`` day dirs) sorted chronologically.
    ``%y%j`` sorts correctly as an integer because the 2-digit year leads."""
    nums = [n for n in names if n.isdigit()]
    return sorted(nums, key=int)


def _basename(entry: str) -> str:
    """Last path segment — ``nlst`` and ``/prog/show`` may return full paths."""
    if not entry:
        return entry
    return entry.rstrip("/").rsplit("/", 1)[-1]
