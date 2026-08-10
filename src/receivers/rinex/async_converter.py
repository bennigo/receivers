"""Fire-and-forget RINEX conversion via a shared ProcessPoolExecutor.

After a successful download, call ``submit_rinex_conversion()`` to queue
RINEX conversion in a separate process.  The caller returns immediately —
the conversion runs independently without blocking further downloads.

Concurrency is bounded by ``max_workers`` (default 4).  Excess submissions
queue inside the executor.  Each worker process creates its own converter
instance, finds raw files by globbing the archive, and converts them.

Usage::

    from receivers.rinex.async_converter import submit_rinex_conversion

    # Fire-and-forget — returns immediately
    submit_rinex_conversion("ELDC", "15s_24hr", start_time, end_time)

    # At shutdown — wait for pending conversions
    from receivers.rinex.async_converter import shutdown_rinex_pool
    shutdown_rinex_pool(wait=True)
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("receivers.rinex.async")

# Module-level executor, lazily initialized on first submit
_executor: ProcessPoolExecutor | None = None
_executor_lock = threading.Lock()
_DEFAULT_MAX_WORKERS = 4


def _get_executor() -> ProcessPoolExecutor:
    """Lazy-initialize the shared ProcessPoolExecutor."""
    global _executor
    if _executor is not None:
        return _executor

    with _executor_lock:
        if _executor is not None:
            return _executor

        max_workers = _DEFAULT_MAX_WORKERS
        try:
            from ..config.receivers_config import get_receivers_config

            config = get_receivers_config()
            rinex_cfg = config.get_rinex_config()
            max_workers = int(rinex_cfg.get("max_workers", _DEFAULT_MAX_WORKERS))
        except Exception:
            pass

        _executor = ProcessPoolExecutor(max_workers=max_workers)
        logger.info(f"RINEX process pool started (max_workers={max_workers})")
        return _executor


def submit_rinex_conversion(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime,
) -> Future | None:
    """Submit a fire-and-forget RINEX conversion job.

    The job runs in a separate process and finds raw files by globbing
    the archive directory.  The caller is not blocked.

    Args:
        station_id: Station ID (e.g. "ELDC")
        session_type: Session type (e.g. "15s_24hr", "1Hz_1hr")
        start_time: Start of the download period
        end_time: End of the download period

    Returns:
        Future for optional monitoring, or None if submission failed.
    """
    try:
        executor = _get_executor()
        future = executor.submit(
            _rinex_worker,
            station_id,
            session_type,
            start_time,
            end_time,
        )

        # Attach done callback for logging (runs in main process thread)
        future.add_done_callback(
            lambda f: _on_conversion_done(f, station_id, session_type)
        )

        logger.info(
            f"🔄 RINEX queued: {station_id} ({session_type}) "
            f"{start_time:%Y-%m-%d} → {end_time:%Y-%m-%d}"
        )
        return future

    except BrokenProcessPool as e:
        # A worker died (OOM-killed, etc.). The singleton stays broken until
        # replaced, so every later submit would fail too. Reset once and retry;
        # if the fresh pool also dies immediately we give up rather than loop.
        logger.warning(
            f"RINEX pool broken for {station_id} ({session_type}); "
            f"resetting and retrying: {e}"
        )
        _reset_executor()
        try:
            executor = _get_executor()
            future = executor.submit(
                _rinex_worker, station_id, session_type, start_time, end_time
            )
            future.add_done_callback(
                lambda f: _on_conversion_done(f, station_id, session_type)
            )
            logger.info(
                f"🔄 RINEX queued (after pool reset): {station_id} ({session_type}) "
                f"{start_time:%Y-%m-%d} → {end_time:%Y-%m-%d}"
            )
            return future
        except Exception as e2:  # noqa: BLE001
            logger.error(
                f"RINEX pool could not be recovered for {station_id} "
                f"({session_type}): {e2}"
            )
            return None

    except Exception as e:
        logger.warning(f"Failed to submit RINEX job for {station_id}: {e}")
        return None


def submit_file_rinex(
    station_id: str,
    session_type: str,
    archive_path: str,
) -> Future | None:
    """Submit RINEX conversion for a single archived file.

    Called per-file from PolaRX5's _handle_successful_download() callback,
    so each file is converted as soon as it's archived — without waiting
    for the entire station download to finish.

    Args:
        station_id: Station ID (e.g. "ELDC")
        session_type: Session type (e.g. "15s_24hr", "1Hz_1hr")
        archive_path: Absolute path to the archived raw file

    Returns:
        Future for optional monitoring, or None if submission failed.
    """
    try:
        executor = _get_executor()
        future = executor.submit(
            _single_file_worker,
            station_id,
            session_type,
            archive_path,
        )

        future.add_done_callback(
            lambda f: _on_conversion_done(f, station_id, session_type)
        )

        logger.info(
            f"🔄 RINEX queued: {station_id} ({session_type}) {Path(archive_path).name}"
        )
        return future

    except BrokenProcessPool as e:
        logger.warning(
            f"RINEX pool broken for {station_id} ({session_type}); "
            f"resetting and retrying: {e}"
        )
        _reset_executor()
        try:
            executor = _get_executor()
            future = executor.submit(
                _single_file_worker, station_id, session_type, archive_path
            )
            future.add_done_callback(
                lambda f: _on_conversion_done(f, station_id, session_type)
            )
            logger.info(
                f"🔄 RINEX queued (after pool reset): {station_id} ({session_type}) "
                f"{Path(archive_path).name}"
            )
            return future
        except Exception as e2:  # noqa: BLE001
            logger.error(
                f"RINEX pool could not be recovered for {station_id} "
                f"({session_type}): {e2}"
            )
            return None

    except Exception as e:
        logger.warning(f"Failed to submit RINEX file job for {station_id}: {e}")
        return None


def _reset_executor() -> None:
    """Shut down and discard the current executor.

    Called when the pool is broken — a worker was killed (e.g. by the memory
    cgroup) and ``ProcessPoolExecutor`` has no self-heal: once broken, every
    later ``submit()`` raises ``BrokenProcessPool``, so the dead singleton must
    be replaced or all inline RINEX silently stops until a full scheduler
    restart. Idempotent and lock-protected; safe to call from multiple
    submit/callback threads concurrently.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            return
        try:
            _executor.shutdown(wait=False)
        except Exception:  # noqa: BLE001 — best-effort teardown of a dead pool
            pass
        _executor = None
        logger.warning("RINEX process pool was broken and has been reset")


def shutdown_rinex_pool(wait: bool = True) -> None:
    """Shut down the RINEX process pool.

    Args:
        wait: If True, block until all pending conversions finish.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            logger.info(f"Shutting down RINEX pool (wait={wait})...")
            _executor.shutdown(wait=wait)
            _executor = None


def _on_conversion_done(future: Future, station_id: str, session_type: str) -> None:
    """Callback for completed/failed RINEX conversion."""
    try:
        exc = future.exception()
        if exc:
            logger.error(
                f"❌ RINEX failed: {station_id} ({session_type}) — "
                f"{type(exc).__name__}: {exc}"
            )
            # A dead worker breaks the whole pool for every later submit.
            # Reset proactively so the next download's submit gets a fresh pool
            # instead of waiting for a BrokenProcessPool on submit.
            if isinstance(exc, BrokenProcessPool):
                _reset_executor()
        else:
            result = future.result()
            converted = result.get("converted", 0)
            failed = result.get("failed", 0)
            duration = result.get("duration", 0)
            if failed > 0:
                logger.warning(
                    f"⚠️  RINEX partial: {station_id} ({session_type}) — "
                    f"{converted} converted, {failed} failed [{duration:.1f}s]"
                )
            elif converted > 0:
                logger.info(
                    f"✅ RINEX done: {station_id} ({session_type}) — "
                    f"{converted} file(s) [{duration:.1f}s]"
                )
            else:
                logger.debug(
                    f"RINEX: {station_id} ({session_type}) — no raw files found"
                )
    except Exception as e:
        logger.error(f"RINEX callback error for {station_id}: {e}")


# ---------------------------------------------------------------------------
# Worker function — runs in a separate process
# ---------------------------------------------------------------------------


def _rinex_worker(
    station_id: str,
    session_type: str,
    start_time: datetime,
    end_time: datetime,
) -> dict[str, Any]:
    """Convert raw files to RINEX for a station/period.

    Runs in a worker process.  All imports are local because each process
    starts fresh.  Finds raw files by globbing the archive directory.

    Returns:
        Dict with keys: converted, failed, skipped, duration, output_files.
    """
    import time as _time

    t0 = _time.monotonic()
    worker_logger = logging.getLogger(f"receivers.rinex.{station_id}")

    # If logging isn't set up in this process, add a basic handler
    if not logging.getLogger("receivers").handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    converted = 0
    failed = 0
    skipped = 0
    output_files: list[str] = []

    try:
        from ..config.receivers_config import get_receivers_config

        config = get_receivers_config()
        data_prepath = config.get_data_prepath()
        rinex_config = config.get_rinex_config()

        # Determine receiver type for this station
        from ..config_utils import get_station_config

        station_config = get_station_config(station_id)
        if station_config is None:
            worker_logger.warning(
                f"Station {station_id} not in config — skipping RINEX"
            )
            return {
                "converted": 0,
                "failed": 0,
                "skipped": 1,
                "duration": 0,
                "output_files": [],
            }

        receiver_type = station_config.get("receiver", {}).get("type", "").lower()

        # Create converter and determine raw extension
        converter, raw_extension = _create_converter(
            station_id,
            receiver_type,
            rinex_config,
            worker_logger,
            session_type=session_type,
        )
        if converter is None or raw_extension is None:
            return {
                "converted": 0,
                "failed": 0,
                "skipped": 1,
                "duration": 0,
                "output_files": [],
            }

        # Find raw files in archive
        raw_files = _find_raw_files(
            station_id, session_type, raw_extension, start_time, end_time, data_prepath
        )

        if not raw_files:
            worker_logger.debug(f"No raw files for {station_id} ({session_type})")
            duration = _time.monotonic() - t0
            return {
                "converted": 0,
                "failed": 0,
                "skipped": 0,
                "duration": duration,
                "output_files": [],
            }

        worker_logger.info(
            f"Converting {len(raw_files)} raw file(s) for {station_id} ({session_type})"
        )

        # Convert each file
        for raw_file in raw_files:
            # Output dir: sibling "rinex/" next to "raw/"
            if raw_file.parent.name == "raw":
                output_dir = raw_file.parent.parent / "rinex"
            else:
                output_dir = raw_file.parent / "rinex"
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                result = converter.convert_file(raw_file, output_dir=output_dir)
                if result.success:
                    converted += 1
                    output_files.append(str(result.rinex_file))
                    worker_logger.info(f"✅ {raw_file.name} → {result.rinex_file.name}")
                else:
                    failed += 1
                    worker_logger.warning(f"❌ {raw_file.name}: {result.message}")
            except Exception as e:
                failed += 1
                worker_logger.error(f"❌ {raw_file.name}: {type(e).__name__}: {e}")

        # Track RINEX output files in database (fire-and-forget)
        if output_files:
            try:
                from ..scheduling.bulk_scheduler import _track_rinex_output_files

                _track_rinex_output_files(
                    station_id, session_type, output_files, worker_logger
                )
            except Exception as e:
                worker_logger.warning(f"RINEX file tracking failed: {e}")

    except Exception as e:
        worker_logger.error(
            f"RINEX worker error for {station_id}: {type(e).__name__}: {e}"
        )
        failed += 1

    duration = _time.monotonic() - t0
    return {
        "converted": converted,
        "failed": failed,
        "skipped": skipped,
        "duration": duration,
        "output_files": output_files,
    }


def _single_file_worker(
    station_id: str,
    session_type: str,
    archive_path: str,
) -> dict[str, Any]:
    """Convert a single archived raw file to RINEX.

    Runs in a worker process.  Determines the converter from station config,
    converts the one file, and tracks the output.

    Returns:
        Dict with keys: converted, failed, skipped, duration, output_files.
    """
    import time as _time

    t0 = _time.monotonic()
    worker_logger = logging.getLogger(f"receivers.rinex.{station_id}")

    if not logging.getLogger("receivers").handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    converted = 0
    failed = 0
    output_files: list[str] = []

    try:
        from ..config.receivers_config import get_receivers_config

        config = get_receivers_config()
        rinex_config = config.get_rinex_config()

        from ..config_utils import get_station_config

        station_config = get_station_config(station_id)
        if station_config is None:
            duration = _time.monotonic() - t0
            return {
                "converted": 0,
                "failed": 0,
                "skipped": 1,
                "duration": duration,
                "output_files": [],
            }

        receiver_type = station_config.get("receiver", {}).get("type", "").lower()

        converter, _ext = _create_converter(
            station_id,
            receiver_type,
            rinex_config,
            worker_logger,
            session_type=session_type,
        )
        if converter is None:
            duration = _time.monotonic() - t0
            return {
                "converted": 0,
                "failed": 0,
                "skipped": 1,
                "duration": duration,
                "output_files": [],
            }

        raw_file = Path(archive_path)
        if not raw_file.exists():
            worker_logger.warning(f"Archive file not found: {archive_path}")
            duration = _time.monotonic() - t0
            return {
                "converted": 0,
                "failed": 1,
                "skipped": 0,
                "duration": duration,
                "output_files": [],
            }

        # Output dir: sibling "rinex/" next to "raw/"
        if raw_file.parent.name == "raw":
            output_dir = raw_file.parent.parent / "rinex"
        else:
            output_dir = raw_file.parent / "rinex"
        output_dir.mkdir(parents=True, exist_ok=True)

        result = converter.convert_file(raw_file, output_dir=output_dir)
        if result.success:
            converted = 1
            output_files.append(str(result.rinex_file))
            worker_logger.info(f"✅ {raw_file.name} → {result.rinex_file.name}")
        else:
            failed = 1
            worker_logger.warning(f"❌ {raw_file.name}: {result.message}")

        if output_files:
            try:
                from ..scheduling.bulk_scheduler import _track_rinex_output_files

                _track_rinex_output_files(
                    station_id, session_type, output_files, worker_logger
                )
            except Exception as e:
                worker_logger.debug(f"RINEX file tracking failed: {e}")

    except Exception as e:
        worker_logger.error(
            f"RINEX single-file error for {station_id}: {type(e).__name__}: {e}"
        )
        failed = 1

    duration = _time.monotonic() - t0
    return {
        "converted": converted,
        "failed": failed,
        "skipped": 0,
        "duration": duration,
        "output_files": output_files,
    }


def _create_converter(
    station_id: str,
    receiver_type: str,
    rinex_config: dict[str, Any],
    worker_logger: logging.Logger,
    session_type: str | None = None,
) -> tuple[Any, str | None]:
    """Create the appropriate RINEX converter for a receiver type.

    session_type plumbs through to the converter so hourly sessions
    (1Hz_1hr, status_1hr) produce per-hour RINEX filenames instead of
    overwriting each other under the daily name (e.g. SSSSDDD0.YYd.Z).

    Returns:
        (converter, raw_extension) or (None, None) if unsupported.
    """
    from ..rinex import (
        LeicaConverter,
        NamingConvention,
        RinexVersion,
        SBFConverter,
        TrimbleConverter,
    )

    version_map = {
        2: RinexVersion.RINEX_2,
        3: RinexVersion.RINEX_3,
        4: RinexVersion.RINEX_4,
    }
    rinex_version = version_map.get(
        int(rinex_config.get("default_version", 3)), RinexVersion.RINEX_3
    )
    naming_str = str(rinex_config.get("default_naming", "short")).lower()
    naming = NamingConvention.SHORT if naming_str == "short" else NamingConvention.LONG
    apply_header: bool = rinex_config.get("apply_header_corrections", True)

    # For Trimble receivers, prefer the native Docker converter when configured.
    # NetRS is included: the native converter handles both NetR9 and NetRS. NetRS
    # is pinned to RINEX 2 below (not 3) because its codeless L2 codes as C2D in
    # RINEX 3, which GAMIT cannot map to P2; at RINEX 2 the native converter emits
    # a proper P2. (The teqc TrimbleConverter path does NOT produce output for
    # NetRS .T00 here, so we must use the native converter — same as the manual
    # `receivers rinex` path.)
    use_native = rinex_config.get("use_native_trimble", False)
    trimble_cls = TrimbleConverter
    if use_native and ("netr9" in receiver_type or "netrs" in receiver_type):
        try:
            from ..rinex.trimble_native_converter import TrimbleNativeConverter

            if TrimbleNativeConverter.is_available():
                trimble_cls = TrimbleNativeConverter
            else:
                worker_logger.debug(
                    "Native Trimble configured but Docker not available"
                )
        except ImportError:
            pass

    if (
        "polarx" in receiver_type
        or "septentrio" in receiver_type
        or "mosaic" in receiver_type
    ):
        converter = SBFConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            naming_convention=naming,
            apply_header_corrections=apply_header,
            loglevel=logging.INFO,
            session_type=session_type,
        )
        return converter, ".sbf.gz"
    elif "netr9" in receiver_type:
        converter = trimble_cls(
            station_id=station_id,
            rinex_version=rinex_version,
            naming_convention=naming,
            apply_header_corrections=apply_header,
            loglevel=logging.INFO,
            session_type=session_type,
        )
        return converter, ".T02*"
    elif "netrs" in receiver_type:
        # NetRS L2 is codeless: native RINEX 3 codes the L2 range as C2D, which
        # GAMIT deletes ("no P2 range"). Convert to RINEX 2 (native converter) so
        # the L2 range stays P2. Use trimble_cls (the native converter when Docker
        # is available — the teqc path produces no .T00 output here). Configurable
        # via [rinex] netrs_rinex_version (default 2); RINEX 2 forces SHORT naming.
        netrs_version = version_map.get(
            int(rinex_config.get("netrs_rinex_version", 2)), RinexVersion.RINEX_2
        )
        netrs_naming = (
            NamingConvention.SHORT if netrs_version == RinexVersion.RINEX_2 else naming
        )
        converter = trimble_cls(
            station_id=station_id,
            rinex_version=netrs_version,
            naming_convention=netrs_naming,
            apply_header_corrections=apply_header,
            loglevel=logging.INFO,
            session_type=session_type,
        )
        return converter, ".T00*"
    elif "g10" in receiver_type or "leica" in receiver_type:
        converter = LeicaConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            naming_convention=naming,
            apply_header_corrections=apply_header,
            loglevel=logging.INFO,
            session_type=session_type,
        )
        return converter, ".m00.gz"
    else:
        worker_logger.debug(
            f"No RINEX converter for receiver type '{receiver_type}' ({station_id})"
        )
        return None, None


def _find_raw_files(
    station_id: str,
    session_type: str,
    raw_extension: str,
    start_time: datetime,
    end_time: datetime,
    data_prepath: str,
) -> list[Path]:
    """Find raw files in the archive for a station/period.

    Mirrors the glob logic in ``_rinex_convert_station_period``.
    """
    raw_files: list[Path] = []
    current = start_time

    while current < end_time:
        year = current.strftime("%Y")
        month = current.strftime("%b").lower()
        raw_dir = Path(data_prepath) / year / month / station_id / session_type / "raw"

        if "1hr" in session_type.lower():
            pattern = f"{station_id}{current.strftime('%Y%m%d%H%M')}*.{raw_extension.lstrip('.')}"
            current += timedelta(hours=1)
        else:
            pattern = f"{station_id}{current.strftime('%Y%m%d')}*{raw_extension}"
            current += timedelta(days=1)

        if raw_dir.exists():
            raw_files.extend(raw_dir.glob(pattern))

    return sorted(raw_files)
