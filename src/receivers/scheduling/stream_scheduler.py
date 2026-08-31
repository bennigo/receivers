"""Composition root + scheduler job functions for stream capture.

Wires the dependency-injected ``receivers.streaming`` modules to the real seams
(config-driven paths/tools, the file_tracking recorder, and the file downloader)
and exposes module-level job functions for APScheduler (must be importable /
picklable for the SQLite jobstore).

Two jobs:
  * ``_run_stream_supervise_job`` — keep BNC daemons alive (frequent).
  * ``_run_stream_pipeline_job``  — ingest → downsample → gap-fill (hourly).

Registration lives in ``bulk_scheduler._schedule_stream_capture`` and is gated
behind ``stream_capture.enabled`` (default off) so it cannot disturb a running
scheduler until explicitly enabled and BNC is deployed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..streaming import (
    GapFiller,
    RinexDownsampler,
    StreamIngestor,
    StreamPipeline,
    StreamSupervisor,
    get_acquisition_mode,
    make_archive_slot_checker,
)
from ..streaming.config import AcquisitionMode
from ..streaming.gap import GapPolicy

logger = logging.getLogger("receivers.scheduler.stream")


def enumerate_stream_stations(station_configs: Dict[str, Dict[str, Any]]) -> List[str]:
    """Return sorted station ids whose acquisition_mode is ``stream``."""
    return sorted(
        sid
        for sid, cfg in station_configs.items()
        if get_acquisition_mode(cfg) == AcquisitionMode.STREAM
    )


@dataclass
class StreamSettings:
    """Resolved paths/tools for the stream pipeline (from receivers.cfg)."""

    archive_base: str
    rt_base: str
    workdir: str
    bnc_path: str = "bnc"
    bnc_config_dir: str = "~/.config/BKG"
    crx2rnx: str = "CRX2RNX"
    rnx2crx: str = "RNX2CRX"
    gfzrnx: str = "gfzrnx"
    interval: int = 15
    min_missing_to_fill: int = 2
    recent_grace_hours: int = 2
    caster_host: str = "ntrcaster.vedur.is"
    caster_port: int = 2101
    caster_user: Optional[str] = None
    caster_password: Optional[str] = None
    mountpoint_suffix: str = "0"

    @staticmethod
    def _expand(p: str) -> str:
        return str(Path(p).expanduser())


def load_stream_settings() -> StreamSettings:
    """Read stream settings from receivers.cfg.

    Stream-specific settings (paths, tools, intervals, gap policy) come from
    ``[streaming]``. The NTRIP caster connection (host/port/credentials/mountpoint
    suffix) is shared with the RTK stream-status checks, so it is read from
    ``[ntrip_defaults]`` — with optional ``[streaming]`` overrides — rather than
    duplicated.
    """
    from ..config.receivers_config import get_receivers_config

    rc = get_receivers_config()
    archive_base = rc.get_data_prepath()

    def _get(section: str, key: str, default: str) -> str:
        try:
            return rc.config.get(section, key, fallback=default)  # type: ignore[attr-defined]
        except Exception:
            return default

    def _s(key: str, default: str) -> str:
        return _get("streaming", key, default)

    # Caster: prefer a [streaming] override, else fall back to [ntrip_defaults].
    caster_host = _s("caster_host", "") or _get(
        "ntrip_defaults", "host", "ntrcaster.vedur.is"
    )
    caster_port = int(_s("caster_port", "") or _get("ntrip_defaults", "port", "2101"))
    caster_user = (
        _s("caster_user", "") or _get("ntrip_defaults", "username", "") or None
    )
    caster_password = (
        _s("caster_password", "") or _get("ntrip_defaults", "password", "") or None
    )
    mountpoint_suffix = _s("mountpoint_suffix", "") or _get(
        "ntrip_defaults", "mountpoint_suffix", "0"
    )

    # All stream working files live under a single tmp base, consistent with the
    # scheduler's cache root (~/.cache/gps_receivers/): rt_base holds BNC's RINEX
    # output + the stored .SKL headers; the downsample workdir holds intermediates.
    stream_tmp = StreamSettings._expand(_s("stream_tmp", "~/.cache/gps_receivers/tmp"))
    rt_base = StreamSettings._expand(_s("rt_base", str(Path(stream_tmp) / "RT-rinex")))
    workdir = str(Path(stream_tmp) / "stream_downsample")

    return StreamSettings(
        archive_base=archive_base,
        rt_base=rt_base,
        workdir=workdir,
        bnc_path=StreamSettings._expand(_s("bnc_path", "bnc")),
        bnc_config_dir=StreamSettings._expand(_s("bnc_config_dir", "~/.config/BKG")),
        crx2rnx=_s("crx2rnx", "CRX2RNX"),
        rnx2crx=_s("rnx2crx", "RNX2CRX"),
        gfzrnx=_s("gfzrnx", "gfzrnx"),
        interval=int(_s("interval", "15")),
        min_missing_to_fill=int(_s("min_missing_to_fill", "2")),
        recent_grace_hours=int(_s("recent_grace_hours", "2")),
        caster_host=caster_host,
        caster_port=caster_port,
        caster_user=caster_user,
        caster_password=caster_password,
        mountpoint_suffix=mountpoint_suffix.split(",")[0].strip() or "0",
    )


def _make_file_tracking_recorder(tracker):
    """Adapt a FileTracker into the StreamIngestor tracker seam."""

    def record(
        station_id: str, archive_path: Path, file_date: datetime, session_type: str
    ):
        size = archive_path.stat().st_size if archive_path.exists() else None
        tracker.mark_file_archived(
            station_id,
            session_type,
            file_date.date(),
            file_hour=file_date.hour,
            filename=archive_path.name,
            file_size=size,
        )

    return record


def _download_gap(station_id: str, start: datetime, end: datetime):
    """Real downloader seam: fetch 1Hz files for [start, end] from the receiver."""
    from ..base.receiver_factory import create_receiver
    from ..config_utils import get_station_config

    cfg = get_station_config(station_id)
    if cfg is None:
        raise RuntimeError(f"no station config for {station_id}")
    receiver = create_receiver(station_id, cfg)
    return receiver.download_data(
        start=start, end=end, session="1Hz_1hr", sync=True, archive=True
    )


def build_stream_pipeline(
    settings: StreamSettings, *, tracker_recorder, downloader
) -> StreamPipeline:
    """Construct a StreamPipeline from settings + real seams."""
    supervisor = StreamSupervisor(
        settings.bnc_path, settings.bnc_config_dir, rt_base=settings.rt_base
    )
    ingestor = StreamIngestor(
        archive_base=settings.archive_base,
        rnx2crx=settings.rnx2crx,
        tracker=tracker_recorder,
    )
    downsampler = RinexDownsampler(
        interval=settings.interval,
        crx2rnx=settings.crx2rnx,
        rnx2crx=settings.rnx2crx,
        gfzrnx=settings.gfzrnx,
    )
    gap_filler = GapFiller(
        make_archive_slot_checker(settings.archive_base),
        policy=GapPolicy(
            min_missing_to_fill=settings.min_missing_to_fill,
            recent_grace_hours=settings.recent_grace_hours,
        ),
    )
    return StreamPipeline(
        supervisor=supervisor,
        ingestor=ingestor,
        downsampler=downsampler,
        gap_filler=gap_filler,
        downloader=downloader,
        rt_base=settings.rt_base,
        archive_base=settings.archive_base,
        workdir=settings.workdir,
    )


# -- module-level job functions (APScheduler) -------------------------------


def generate_bnc_config_file(
    station_id: str, station_config: Dict[str, Any], settings: StreamSettings
) -> Path:
    """Render and write the BNC config for one stream station.

    Output: ``<bnc_config_dir>/rtcm2rinex-<SID>.bnc`` (0600 — contains caster creds).
    """
    from ..streaming.bnc_config import bnc_config_filename, write_bnc_config
    from ..streaming.config import StreamConfig

    rnx_path = str(Path(settings.rt_base) / station_id)
    sc = StreamConfig.from_station_config(
        station_id,
        station_config,
        rnx_path=rnx_path,
        caster_user=settings.caster_user,
        caster_password=settings.caster_password,
        mountpoint_suffix=settings.mountpoint_suffix,
    )
    sc.caster_host = settings.caster_host
    sc.caster_port = settings.caster_port
    out = Path(settings.bnc_config_dir) / bnc_config_filename(station_id)
    return write_bnc_config(sc, out)


def _config_position(station_config: Optional[Dict[str, Any]]):
    """Extract (lat, lon, height) floats from a station config, or None."""
    if not station_config:
        return None
    from ..streaming.config import _lookup

    try:
        lat = _lookup(station_config, "latitude")
        lon = _lookup(station_config, "longitude")
        hgt = _lookup(station_config, "height")
        if lat and lon and hgt:
            return (float(lat), float(lon), float(hgt))
    except (TypeError, ValueError):
        pass
    return None


def refresh_station_skeleton(
    station_id,
    settings: StreamSettings,
    get_tos_metadata,
    *,
    station_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Create or refresh a station's stored ``.SKL`` from TOS, writing only on change.

    ``get_tos_metadata(station_id)`` returns a TOS ``get_complete_station_metadata``
    dict (injected for testability). When no skeleton exists yet, a base one is built
    from the station's surveyed position (``station_config`` lat/lon/height) + TOS
    equipment. Returns: ``created`` | ``updated`` | ``unchanged`` | ``no_position`` |
    ``no_tos``.
    """
    from ..streaming.skeleton import (
        build_skeleton,
        metadata_from_tos,
        refresh_skeleton,
        upgrade_skeleton,
    )

    skl = Path(settings.rt_base) / station_id / f"{station_id}.SKL"
    station = get_tos_metadata(station_id)
    if not station:
        if skl.exists():
            logger.warning(
                "No TOS metadata for %s — skeleton left unchanged", station_id
            )
            return "no_tos"
        logger.warning(
            "No TOS metadata and no skeleton for %s — cannot create", station_id
        )
        return "no_tos"
    meta = metadata_from_tos(
        station, station_id=station_id, station_config=station_config
    )

    if skl.exists():
        # Structural upgrade (legacy RINEX-2 → 3.04) first, then equipment refill.
        # Either can mark the skeleton dirty; write once if so.
        upgraded, struct_changed = upgrade_skeleton(skl.read_text())
        refilled, equip_changed = refresh_skeleton(upgraded, meta)
        if struct_changed or equip_changed:
            skl.write_text(refilled)
            logger.info(
                "Refreshed RINEX skeleton for %s from TOS%s",
                station_id,
                " (upgraded to 3.04)" if struct_changed else "",
            )
            return "updated"
        return "unchanged"

    # No skeleton yet — build a base header from the surveyed position.
    pos = _config_position(station_config)
    if pos is None:
        logger.warning(
            "No skeleton and no position for %s — cannot create base header", station_id
        )
        return "no_position"
    skl.parent.mkdir(parents=True, exist_ok=True)
    skl.write_text(
        build_skeleton(meta, latitude=pos[0], longitude=pos[1], height=pos[2])
    )
    logger.info(
        "Created base RINEX skeleton for %s from TOS + surveyed position", station_id
    )
    return "created"


def _run_stream_config_refresh_job() -> None:
    """(Re)generate .bnc configs + refresh .SKL headers from TOS for stream stations.

    Low-frequency job: BNC + the pipeline read the stored configs/skeletons; this only
    keeps them in sync with stations.cfg + TOS (equipment swaps, firmware updates).
    """
    from ..cli.main import get_all_station_configs

    configs = get_all_station_configs()
    stations = enumerate_stream_stations(configs)
    if not stations:
        return
    settings = load_stream_settings()
    tos_provider = _make_tos_metadata_provider()
    supervisor = StreamSupervisor(
        settings.bnc_path, settings.bnc_config_dir, rt_base=settings.rt_base
    )
    for sid in stations:
        try:
            generate_bnc_config_file(sid, configs[sid], settings)
            outcome = refresh_station_skeleton(
                sid, settings, tos_provider, station_config=configs[sid]
            )
            # BNC caches the .SKL at process start, so a rewritten skeleton does
            # NOT reach published headers until the daemon restarts. Without this
            # bounce every equipment refresh (antenna/receiver swap, firmware
            # update) is silently inert — measured 2026-08-31, where a corrected
            # skeleton was still missing from a file created 3 h later (#166).
            # Only changed stations are bounced, so a no-op refresh costs nothing.
            if outcome in ("created", "updated"):
                if supervisor.bounce_station(sid):
                    logger.info(
                        "Bounced BNC for %s so it picks up the %s skeleton",
                        sid,
                        outcome,
                    )
                else:
                    logger.error(
                        "Skeleton %s for %s but BNC failed to restart — headers will "
                        "keep the OLD metadata until the next supervise pass",
                        outcome,
                        sid,
                    )
        except Exception as e:  # noqa: BLE001 - isolate per station
            logger.error("Stream config refresh failed for %s: %s", sid, e)


def _make_tos_metadata_provider():
    """Build a TOSClient-backed metadata provider (lazy — needs [tos] config)."""
    from tostools.api.tos_client import TOSClient

    client = TOSClient()

    def provider(station_id: str):
        try:
            return client.get_complete_station_metadata(station_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("TOS query failed for %s: %s", station_id, e)
            return None

    return provider


def _run_stream_supervise_job() -> None:
    """Ensure BNC daemons are alive for all configured stream stations."""
    settings = load_stream_settings()
    supervisor = StreamSupervisor(
        settings.bnc_path,
        settings.bnc_config_dir,
        # Lets the supervisor also catch alive-but-mute streams (#168); without
        # it the check is skipped rather than reporting false health.
        rt_base=settings.rt_base,
    )
    result = supervisor.supervise()
    if result.started or result.failed:
        logger.info(
            "Stream supervise: started %s, failed %s", result.started, result.failed
        )
    if result.stale:
        logger.warning(
            "Stream supervise: running but producing no data %s", result.stale
        )


def _run_stream_pipeline_job(days_back: int = 1) -> None:
    """Ingest → downsample → gap-fill for all stream stations (today + days_back)."""
    from ..cli.main import get_all_station_configs
    from ..health.file_tracker import FileTracker

    configs = get_all_station_configs()
    stations = enumerate_stream_stations(configs)
    if not stations:
        logger.debug("No stream stations configured")
        return

    def _logs_1hz_on_disk(sid: str) -> bool:
        """Gap-fill (1 Hz from disk) is only valid if the receiver logs 1 Hz.
        Declared per-station via remote_sessions; absent ⇒ no 1 Hz on disk."""
        raw = str((configs.get(sid) or {}).get("remote_sessions") or "")
        return "1Hz_1hr" in [s.strip() for s in raw.split(",")]

    settings = load_stream_settings()
    now = datetime.now(UTC)
    days = [(now - timedelta(days=d)).date() for d in range(days_back + 1)]

    tracker = FileTracker()
    try:
        pipeline = build_stream_pipeline(
            settings,
            tracker_recorder=_make_file_tracking_recorder(tracker),
            downloader=_download_gap,
        )
        pipeline.supervise()
        for day in days:
            for station_id in stations:
                try:
                    pipeline.process_station(
                        station_id,
                        day,
                        now=now,
                        gap_fill=_logs_1hz_on_disk(station_id),
                    )
                except Exception as e:  # noqa: BLE001 - isolate per-station/day
                    logger.error("Stream pipeline %s %s: %s", station_id, day, e)
    finally:
        close = getattr(tracker, "close", None)
        if callable(close):
            close()
