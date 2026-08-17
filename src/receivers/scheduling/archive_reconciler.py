"""Archive reconciler: find raw files missing their RINEX counterpart.

Scans archive directories for all receiver types with RINEX converters
and triggers raw→RINEX conversion for any raw files that lack a
corresponding RINEX file.

Runs on the 'backfill' executor at a configurable interval (default every 6h).

When FormatResolver is available (archive_format table populated), uses
format templates for RINEX directory and file path construction. Falls back
to ArchiveFileChecker + filesystem glob when format data is unavailable.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .lookback import Lookback

if TYPE_CHECKING:
    from ..health.file_tracker import ArchiveFileChecker, ArchiveFormat, FormatResolver

logger = logging.getLogger("receivers.scheduler.reconciler")

# Raw-validation refusals collected during ONE sweep — summarized (with the
# suggested operator actions) at sweep end instead of cluttering the per-file
# stream. Reset at each sweep start; single-threaded within a sweep.
_SWEEP_REFUSALS: list = []
_SWEEP_LOCK: threading.Lock = threading.Lock()


def _yield_to_load_gate(caller: str = "reconciler") -> None:
    """Pause the reconciler if the load gate is blocking STANDARD jobs.

    The reconciler runs on the backfill executor (low priority). If the load
    gate is blocking STANDARD jobs (network/CPU/thread saturation), we pause
    so real-time downloads and conversions can proceed. Checks every 5 s, up
    to 120 s total.
    """
    try:
        from .bulk_scheduler import _get_load_monitor

        lm = _get_load_monitor()
        if lm is None or not lm.enabled:
            return
        waited = 0
        while waited < 120 and not lm.can_start_job():
            time.sleep(5)
            waited += 5
        if waited:
            logger.debug("%s: yielded %ds for load gate", caller, waited)
    except Exception:
        pass


def _get_format_resolver():
    """Try to create a FormatResolver. Returns None if unavailable."""
    try:
        from ..health.file_tracker import FormatResolver

        resolver = FormatResolver()
        if resolver.connect():
            # Check if format data is actually loaded
            try:
                formats = resolver.list_formats(file_category="rinex")
            except Exception:
                resolver.close()
                return None
            if formats:
                return resolver
        resolver.close()
    except Exception:
        pass
    return None


def _run_archive_reconciler_job(
    session_types: List[str],
    days_back: int = 30,
    lookback: Optional[Lookback] = None,
) -> None:
    """APScheduler job: reconcile raw archives with RINEX output.

    For each active station with a RINEX converter, scans archive directories
    for raw files that have no corresponding RINEX file and triggers conversion.

    Args:
        session_types: Session types to reconcile (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday. Kept for callers
            that still pass a plain int (the CLI); ignored when ``lookback`` is
            given.
        lookback: Session-aware window. With ``files_back`` this narrows hourly
            sessions to N files instead of N*24 — the case scheduler.yaml's own
            comment complains about (days_back:30 over 1Hz is ~239,000 files).
            Daily sessions are unaffected either way.
    """
    try:
        from ..cli.main import get_all_station_configs
        from ..config.receiver_registry import has_rinex_converter
        from ..health.file_tracker import ArchiveFileChecker
    except ImportError as e:
        logger.debug(f"Archive reconciler dependencies not available: {e}")
        return

    resolver = None
    try:
        start_time = time.time()

        # Get active stations with RINEX converters (all receiver types)
        all_stations = get_all_station_configs()
        convertible_stations: Dict[str, str] = {
            sid: cfg.get("receiver_type", "").lower()
            for sid, cfg in all_stations.items()
            if cfg.get("enabled", True)
            and has_rinex_converter(cfg.get("receiver_type", ""))
            and cfg.get("station_status") not in ("discontinued", "inactive")
            and cfg.get("health_check") != "passive"
        }

        if not convertible_stations:
            logger.info("Archive reconciler: no active stations with RINEX converters")
            return

        lb = lookback or Lookback(days_back, "days")
        logger.info(
            f"Archive reconciler: scanning {len(convertible_stations)} stations, "
            f"{len(session_types)} sessions, {lb.describe(session_types)}"
        )
        _SWEEP_REFUSALS.clear()

        total_missing = 0
        total_converted = 0
        total_errors = 0

        checker = ArchiveFileChecker()
        resolver = _get_format_resolver()
        if resolver:
            logger.debug("Using FormatResolver for RINEX path construction")

        end_date = date.today() - timedelta(days=1)
        # The tiers below span the WIDEST session window; each session is then
        # clamped to its own inside the loop. Doing it that way keeps the tier
        # ordering intact (all stations at yesterday before any deep history) —
        # inverting the tier/session loops to get per-session ranges would have
        # broken exactly the priority this design exists for.
        widest_days = max(
            (lb.date_span_days(st) for st in session_types), default=days_back
        )
        start_date = end_date - timedelta(days=widest_days - 1)
        station_ids = sorted(convertible_stations)

        # --- Tiered priority: yesterday → last 7 days → deep history ---
        # Each tier processes ALL stations before moving to the next tier,
        # so yesterday's data for every station is done before any historical
        # backfill consumes the reconciler window.

        yesterday = end_date
        day7 = end_date - timedelta(days=6)

        tiers = [
            ("yesterday", yesterday, yesterday),
            ("last-7-days", day7, yesterday - timedelta(days=1)),
            ("deep-history", start_date, day7 - timedelta(days=1)),
        ]

        for tier_name, tier_start, tier_end in tiers:
            if tier_start > tier_end:
                continue  # empty tier
            _yield_to_load_gate(f"reconciler-{tier_name}")
            logger.info(
                f"Reconciler tier {tier_name}: {tier_start} → {tier_end} "
                f"({len(station_ids)} stations)"
            )

            # Process sessions in two phases: 15s (fast, 1 file/day) first,
            # then 1Hz (slow, 24 files/day).  All 15s jobs complete before
            # any 1Hz job starts, so yesterday data for all stations is done
            # in one fast batch.
            for session_type in session_types:
                # Clamp this tier to the session's own window. With files_back
                # an hourly session covers only the last day or two, so it
                # simply drops out of the deeper tiers while the daily session
                # still walks them.
                session_start = max(
                    tier_start,
                    end_date - timedelta(days=lb.date_span_days(session_type) - 1),
                )
                if session_start > tier_end:
                    continue
                _yield_to_load_gate(f"reconciler-{tier_name}-{session_type}")
                max_pool = min(len(station_ids), 8)
                with ThreadPoolExecutor(
                    max_workers=max_pool, thread_name_prefix="recon"
                ) as pool:
                    futures = {}
                    for station_id in station_ids:
                        receiver_type = convertible_stations[station_id]
                        fut = pool.submit(
                            _reconcile_station_session,
                            station_id,
                            session_type,
                            session_start,
                            tier_end,
                            checker,
                            resolver,
                            receiver_type=receiver_type,
                        )
                        futures[fut] = (station_id, session_type)

                    for fut in as_completed(futures):
                        station_id, session_type = futures[fut]
                        try:
                            missing, converted, errors = fut.result()
                        except Exception as e:
                            logger.warning(
                                f"Reconciler {station_id}/{session_type}: {e}"
                            )
                            errors = 1
                            missing = converted = 0
                        total_missing += missing
                        total_converted += converted
                        total_errors += errors

        duration = time.time() - start_time
        logger.info(
            f"Archive reconciler complete: "
            f"{total_missing} missing RINEX, {total_converted} converted, "
            f"{total_errors} errors ({duration:.1f}s)"
        )
        try:
            from ..rinex.converter_base import validation_epilog

            epilog = validation_epilog(list(_SWEEP_REFUSALS))
            if epilog:
                logger.warning(epilog)
        except Exception:  # noqa: BLE001 - epilog must never fail the sweep
            pass

    except Exception as e:
        logger.error(f"Archive reconciler failed: {type(e).__name__}: {e}")
    finally:
        if resolver:
            resolver.close()


def _find_obs_rinex_file(raw_path: Path) -> Optional[Path]:
    """Find a legacy .o.Z / .o.gz RINEX observation file for the given raw file.

    These are produced by the old rek.vedur.is pipeline (non-Hatanaka).
    The reconciler uses this to clean them up after converting to .d.Z.
    """
    stem = raw_path.name
    for ext in (
        ".sbf.gz",
        ".sbf",
        ".T02.gz",
        ".T02",
        ".t02",
        ".T00.gz",
        ".T00",
        ".t00",
        ".m00.gz",
        ".m00",
        ".M00",
    ):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break

    station = stem[:4]
    search_dirs = [raw_path.parent, raw_path.parent.parent / "rinex"]

    try:
        date_str = stem[4:12]
        dt = datetime.strptime(date_str, "%Y%m%d")
        doy = dt.strftime("%j")
        doy_prefix = f"{station}{doy}0"
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for suffix in ("o.Z", "o.gz"):
                matches = list(search_dir.glob(f"{doy_prefix}*{suffix}"))
                if matches:
                    return matches[0]
    except (ValueError, IndexError):
        pass
    return None


def _delete_obs_sibling(rinex_path: Path) -> None:
    """Delete the legacy .o.Z / .o.gz counterpart of a preferred .d.Z file.

    E.g. LFEL1160.26d.Z → deletes LFEL1160.26o.Z in the same directory.
    """
    name = rinex_path.name
    # Match the Hatanaka type letter case-insensitively: the archive convention
    # is uppercase .D.Z (converter_base / stream), but tolerate legacy .d.Z too.
    for preferred, legacy in (("d.z", "o.Z"), ("d.gz", "o.gz")):
        if name.lower().endswith(preferred):
            obs_path = rinex_path.parent / (name[: -len(preferred)] + legacy)
            if obs_path.exists():
                try:
                    obs_path.unlink()
                    logger.debug(f"Removed legacy RINEX: {obs_path.name}")
                except Exception as e:
                    logger.warning(f"Could not remove legacy {obs_path.name}: {e}")
            return


def _reconcile_station_session(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    resolver: Optional["FormatResolver"] = None,
    receiver_type: str = "polarx5",
) -> Tuple[int, int, int]:
    """Check one station/session for raw files missing RINEX.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_date: Start of date range
        end_date: End of date range
        checker: ArchiveFileChecker instance
        resolver: Optional FormatResolver for format-aware path building
        receiver_type: Receiver type key (e.g., 'polarx5', 'netr9')

    Returns:
        Tuple of (missing_count, converted_count, error_count)
    """
    missing = 0
    converted = 0
    errors = 0

    # Resolve RINEX format for this session (if FormatResolver available)
    rinex_format = None
    if resolver:
        rinex_format = resolver.find_format(
            session_type=session_type,
            file_category="rinex",
            receiver_type=receiver_type,
        )

    # One indexed range query for the whole station/session window, instead of
    # a per-file point lookup. The old per-hour check filtered on `filename`
    # (unindexed) and opened its own connection: 22.6M calls at 24 ms each on
    # pgdev, 151 h of server CPU, and the bulk of file_tracking's 46M seq
    # scans. Slot-keyed prefetch makes the sweep O(1) queries per station.
    tracked_rinex = _load_tracked_rinex(
        station_id, f"{session_type}_rinex", start_date, end_date
    )

    try:
        # Iterate newest-first so recent files (1-3 days old) get converted first
        current = end_date
        while current >= start_date:
            dt = datetime.combine(current, datetime.min.time()).replace(tzinfo=UTC)

            if session_type == "15s_24hr":
                # Daily file: check one file per day
                hours = [0]
            else:
                # Hourly file: check 24 hours
                hours = list(range(24))

            for hour in hours:
                file_dt = dt.replace(hour=hour)
                raw_path = _find_raw_file(
                    station_id,
                    session_type,
                    file_dt,
                    checker,
                    receiver_type=receiver_type,
                )
                if raw_path is None:
                    continue

                # Check for preferred RINEX (.d.Z / .obs / etc.) — not legacy .o.Z
                rinex_path = None
                if rinex_format and resolver:
                    rinex_path = _find_rinex_file_by_format(
                        station_id,
                        file_dt,
                        rinex_format,
                        resolver,
                        checker,
                    )
                else:
                    rinex_path = _find_rinex_file(raw_path)

                if rinex_path is not None:
                    # Preferred format found — delete any legacy .o.Z sibling
                    _delete_obs_sibling(rinex_path)
                    _ensure_rinex_tracked(
                        station_id,
                        session_type,
                        rinex_path,
                        tracked=tracked_rinex,
                        tracked_window=(start_date, end_date),
                    )
                    continue

                # No preferred RINEX — check for legacy .o.Z to clean up after conversion
                old_obs_path = _find_obs_rinex_file(raw_path)

                # Validate raw file before attempting conversion
                if _is_corrupt_gz(raw_path):
                    _handle_corrupt_file(
                        station_id,
                        session_type,
                        raw_path,
                        file_dt,
                    )
                    errors += 1
                    continue

                # Convert raw → preferred format (.d.Z)
                missing += 1
                success = _convert_raw_to_rinex(
                    station_id,
                    raw_path,
                    receiver_type=receiver_type,
                    session_type=session_type,
                )
                if success:
                    converted += 1
                    if old_obs_path and old_obs_path.exists():
                        try:
                            old_obs_path.unlink()
                            logger.debug(f"Removed legacy RINEX: {old_obs_path.name}")
                        except Exception as e:
                            logger.warning(
                                f"Could not remove legacy {old_obs_path.name}: {e}"
                            )
                else:
                    errors += 1

            current -= timedelta(days=1)

    except Exception as e:
        logger.warning(f"Reconciler error {station_id}/{session_type}: {e}")
        errors += 1

    if missing > 0:
        logger.info(
            f"Reconciler {station_id}/{session_type}: "
            f"{missing} missing, {converted} converted, {errors} errors"
        )

    return missing, converted, errors


def _find_rinex_file_by_format(
    station_id: str,
    dt: datetime,
    rinex_format: "ArchiveFormat",
    resolver: "FormatResolver",
    checker: "ArchiveFileChecker",
) -> Optional[Path]:
    """Check if a RINEX file exists using format template path construction.

    Uses FormatResolver to build the expected RINEX path from the archive_format
    template, then checks if that file exists on disk.

    Args:
        station_id: Station identifier
        dt: File datetime
        rinex_format: ArchiveFormat for the RINEX output
        resolver: FormatResolver instance
        checker: ArchiveFileChecker for base path fallback

    Returns:
        Path to existing RINEX file, or None
    """
    try:
        # Build expected RINEX path using format template
        base_path = checker.data_prepath or "/mnt/gpsdata"
        expected_path = resolver._build_path_from_format(
            rinex_format, station_id, dt, base_path
        )
        if expected_path and Path(expected_path).exists():
            return Path(expected_path)
    except Exception:
        pass
    return None


def _find_raw_file(
    station_id: str,
    session_type: str,
    dt: datetime,
    checker: "ArchiveFileChecker",
    receiver_type: str = "polarx5",
) -> Optional[Path]:
    """Find a raw archive file for the given station/session/datetime.

    Works for all receiver types by using the registry to determine
    valid file extensions.
    """
    from ..config.receiver_registry import get_capability

    cap = get_capability(receiver_type)
    if cap is None:
        return None

    try:
        archive_dir = checker.get_archive_directory(
            station_id,
            session_type,
            year=dt.year,
            month=dt.strftime("%b").lower(),
        )
        archive_path = Path(archive_dir)

        if not archive_path.exists():
            return None

        # Primary pattern: SSSSYYYYMMDDHHMMX.ext (used by all receiver types)
        pattern = f"{station_id}{dt.strftime('%Y%m%d')}{dt.hour:02d}*"
        matches = list(archive_path.glob(pattern))

        # Filter matches against known extensions for this receiver type
        for match in matches:
            name = match.name
            if any(name.endswith(ext) for ext in cap.raw_extensions):
                return match

        # Fallback: DOY-based naming pattern (some older archives)
        doy = dt.strftime("%j")
        hour_letter = chr(ord("a") + dt.hour) if session_type != "15s_24hr" else "0"
        year2 = dt.strftime("%y")
        fallback_pattern = f"{station_id.lower()}{doy}{hour_letter}.{year2}_*"
        fallback_matches = list(archive_path.glob(fallback_pattern))
        for match in fallback_matches:
            name = match.name
            if any(name.endswith(ext) for ext in cap.raw_extensions):
                return match

        return None

    except Exception:
        return None


def _find_rinex_file(raw_path: Path) -> Optional[Path]:
    """Check if a RINEX file exists for the given raw file.

    Looks in parent directory and sibling 'rinex' directory for
    corresponding observation files (.obs, .rnx, .crx, .gz variants).

    Uses DOY-based pattern matching to avoid false positives where a RINEX
    file for a different date (same station) is incorrectly matched.

    Raw filename formats:
      SBF:     SSSSYYYYMMDDHHMMX.sbf.gz  (X = session letter)
      Trimble: SSSSYYYYMMDDHHMMX.T02     (same pattern)
      Leica:   SSSSYYYYMMDDHHMMX.m00.gz  (same pattern)

    RINEX 2 short name: SSSSdddS.YYd.Z (ddd = DOY, S = RINEX session char)

    Note: Raw session letter (a/b/...) does NOT correspond to RINEX session
    character. RINEX uses '0' for daily and 'a'-'x' for hourly (hour-mapped).
    Current converters produce '0' for both daily and hourly sessions.
    """
    # Strip all known raw extensions to get the stem
    stem = raw_path.name
    for ext in (
        ".sbf.gz",
        ".sbf",
        ".T02.gz",
        ".T02",
        ".t02",
        ".T00.gz",
        ".T00",
        ".t00",
        ".m00.gz",
        ".m00",
        ".M00",
    ):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break

    station = stem[:4]

    # Extract date + hour from filename (SSSSYYYYMMDDHHMMX)
    doy_patterns: List[str] = []
    try:
        date_str = stem[4:12]  # YYYYMMDD
        hour_str = stem[12:14]  # HH
        dt = datetime.strptime(date_str, "%Y%m%d")
        doy = dt.strftime("%j")  # e.g., "042"
        hour = int(hour_str)

        # Try '0' first — current converter convention for both daily and hourly
        doy_patterns.append(f"{station}{doy}0")
        # Also try hour-mapped letter (a-x) for standard hourly RINEX naming
        hour_letter = chr(ord("a") + hour)
        if hour_letter != "0":
            doy_patterns.append(f"{station}{doy}{hour_letter}")
    except (ValueError, IndexError):
        pass

    # Check in same directory and rinex subdirectory
    search_dirs = [raw_path.parent]
    rinex_dir = raw_path.parent.parent / "rinex"
    if rinex_dir.exists():
        search_dirs.append(rinex_dir)

    rinex_extensions = [".obs", ".rnx", ".crx", ".obs.gz", ".rnx.gz", ".crx.gz"]

    for search_dir in search_dirs:
        if doy_patterns:
            # Date-specific search: try each RINEX session pattern
            for doy_pattern in doy_patterns:
                for ext in rinex_extensions:
                    candidates = list(search_dir.glob(f"{doy_pattern}*{ext}"))
                    if candidates:
                        return candidates[0]

                # Hatanaka compressed RINEX (.D.Z / .D.gz, legacy .d.Z / .d.gz).
                # The type letter MUST be matched case-insensitively: producers
                # write uppercase .D.Z (f0fe505) and Path.glob is case-sensitive
                # on Linux, so a lowercase-only glob never finds what was just
                # written and every file is re-converted on every sweep.
                hatanaka = list(search_dir.glob(f"{doy_pattern}*[dD].Z"))
                hatanaka += list(search_dir.glob(f"{doy_pattern}*[dD].gz"))
                if hatanaka:
                    return hatanaka[0]
        else:
            # Fallback: no date extracted, use station-only (legacy behavior)
            for ext in rinex_extensions:
                candidates = list(search_dir.glob(f"{station}*{ext}"))
                if candidates:
                    return candidates[0]

            hatanaka = list(search_dir.glob(f"{station}*[dD].Z"))
            hatanaka += list(search_dir.glob(f"{station}*[dD].gz"))
            if hatanaka:
                return hatanaka[0]

    return None


def _convert_raw_to_rinex(
    station_id: str,
    raw_path: Path,
    receiver_type: str = "polarx5",
    session_type: str = "15s_24hr",
) -> bool:
    """Convert a single raw file to RINEX using the appropriate converter.

    Dynamically loads the converter class from the receiver registry.
    On success, records the output in file_tracking so Grafana can see it.

    Args:
        station_id: Station identifier
        raw_path: Path to raw file
        receiver_type: Receiver type key
        session_type: Session type (for file_tracking)

    Returns:
        True if conversion succeeded
    """
    try:
        from ..config.receiver_registry import get_converter_class

        converter_class = get_converter_class(receiver_type)
        if converter_class is None:
            logger.debug(f"No converter available for {receiver_type}")
            return False

        # Output to rinex/ sibling directory instead of raw/
        rinex_dir = raw_path.parent.parent / "rinex"
        rinex_dir.mkdir(parents=True, exist_ok=True)

        # Plumb session_type so the converter picks the right gtimes lfrequency
        # ('1H' vs '1D') for the filename — without this the reconciler writes
        # daily-form names (e.g. HEDI1450.26d.Z) for hourly raw files and each
        # hour overwrites the previous (the bug PR #75 fixed in the live path,
        # silently re-introduced here).
        converter = converter_class(station_id=station_id, session_type=session_type)
        result = converter.convert_file(raw_path, output_dir=rinex_dir)

        if result.success:
            logger.debug(f"Converted {raw_path.name} to RINEX ({receiver_type})")
            # Track in file_tracking so Grafana dashboards see it
            if result.rinex_file:
                try:
                    from .bulk_scheduler import _track_rinex_output_files

                    _track_rinex_output_files(
                        station_id,
                        session_type,
                        [str(result.rinex_file)],
                        logger,
                    )
                except Exception as e:
                    logger.warning(f"Could not track RINEX file: {e}")
            return True
        else:
            if getattr(result, "validation_category", None):
                # Gate refusal: compact line already logged by the converter —
                # collect for the sweep-end epilog, don't repeat the detail.
                with _SWEEP_LOCK:
                    _SWEEP_REFUSALS.append(result)
            else:
                logger.warning(
                    f"RINEX conversion failed for {raw_path.name}: {result.message}"
                )
            return False

    except ImportError:
        logger.debug(f"Converter not available for {receiver_type}")
        return False
    except Exception as e:
        logger.warning(f"RINEX conversion error for {raw_path.name}: {e}")
        return False


def _load_tracked_rinex(
    station_id: str,
    rinex_session: str,
    start_date: date,
    end_date: date,
) -> Optional[Dict[Tuple[date, Optional[int]], str]]:
    """Load the tracked RINEX filenames for one station/session window.

    Keyed by the file_tracking uniqueness slot — (file_date, file_hour) —
    so a caller can answer "is this file already tracked?" without a query
    per file. Ranges over (sid, session_type, file_date), which is exactly
    what the composite index serves.

    Returns:
        {(file_date, file_hour): filename}, or None if the lookup failed —
        callers must treat None as "unknown" and fall back to a point query,
        never as "nothing is tracked" (that would re-upsert the whole window).
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection(single_host=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT file_date, file_hour, filename FROM file_tracking
                       WHERE sid = %s AND session_type = %s
                         AND file_date BETWEEN %s AND %s""",
                    (station_id, rinex_session, start_date, end_date),
                )
                return {(row[0], row[1]): row[2] for row in cur.fetchall()}
    except Exception as e:
        logger.debug(f"Could not prefetch tracked RINEX for {station_id}: {e}")
        return None


def _ensure_rinex_tracked(
    station_id: str,
    session_type: str,
    rinex_path: Path,
    tracked: Optional[Dict[Tuple[date, Optional[int]], str]] = None,
    tracked_window: Optional[Tuple[date, date]] = None,
) -> None:
    """Ensure an existing RINEX file is recorded in file_tracking.

    Checks whether the file is already tracked under '{session_type}_rinex'.
    If not, inserts a tracking record so Grafana dashboards can see it.

    The check is keyed on (sid, session_type, file_date, file_hour) — the
    table's uniqueness slot — not on `filename`, which carries no index. The
    filename is still compared so a renamed file (.o.Z → .d.Z, R2 → R3) still
    re-upserts and corrects the recorded name.

    Args:
        tracked: Optional prefetched {(file_date, file_hour): filename} map
            from :func:`_load_tracked_rinex`. Avoids a query per file; None
            falls back to a single indexed point lookup.
        tracked_window: (start_date, end_date) the map covers. A file whose
            name parses OUTSIDE it — a stray, a DOY/year glob artifact — is
            absent from the map for the wrong reason, and trusting that miss
            would re-upsert it on every sweep. Such files take the point
            query instead.
    """
    try:
        from .bulk_scheduler import _parse_rinex_filename, _track_rinex_output_files

        rinex_session = f"{session_type}_rinex"
        file_date, file_hour = _parse_rinex_filename(rinex_path.name, station_id)

        if file_date is None:
            # Unparseable name — let the tracker's own parser log and skip it.
            _track_rinex_output_files(
                station_id, session_type, [str(rinex_path)], logger
            )
            return

        in_window = tracked_window is None or (
            tracked_window[0] <= file_date <= tracked_window[1]
        )
        if tracked is not None and in_window:
            if tracked.get((file_date, file_hour)) == rinex_path.name:
                return  # Already tracked under this exact name
        else:
            from ..health.database_factory import DatabaseConnectionFactory

            with DatabaseConnectionFactory.connection(single_host=True) as conn:
                with conn.cursor() as cur:
                    if file_hour is None:
                        cur.execute(
                            """SELECT filename FROM file_tracking
                               WHERE sid = %s AND session_type = %s
                                 AND file_date = %s AND file_hour IS NULL""",
                            (station_id, rinex_session, file_date),
                        )
                    else:
                        cur.execute(
                            """SELECT filename FROM file_tracking
                               WHERE sid = %s AND session_type = %s
                                 AND file_date = %s AND file_hour = %s""",
                            (station_id, rinex_session, file_date, file_hour),
                        )
                    row = cur.fetchone()
                    if row and row[0] == rinex_path.name:
                        return  # Already tracked

        # Not tracked — register it
        _track_rinex_output_files(
            station_id,
            session_type,
            [str(rinex_path)],
            logger,
        )
        if tracked is not None and in_window:
            tracked[(file_date, file_hour)] = rinex_path.name
        logger.debug(f"Tracked existing RINEX: {station_id}/{rinex_path.name}")

    except Exception as e:
        logger.warning(f"Could not track RINEX {rinex_path.name}: {e}")


def _is_corrupt_gz(raw_path: Path) -> bool:
    """Check if a .gz file has invalid gzip content.

    Detects files saved with .gz extension but containing uncompressed data,
    bzip2, or other non-gzip content.  Only checks files ending in .gz.

    Returns:
        True if file claims to be .gz but isn't valid gzip.
    """
    if not raw_path.name.endswith(".gz"):
        return False
    try:
        with open(raw_path, "rb") as f:
            magic = f.read(2)
        return magic != b"\x1f\x8b"
    except Exception:
        return False


def _handle_corrupt_file(
    station_id: str,
    session_type: str,
    raw_path: Path,
    file_dt: datetime,
) -> None:
    """Delete a corrupt raw file and reset file_tracking for re-download.

    Args:
        station_id: Station identifier
        session_type: Session type (e.g., '15s_24hr')
        raw_path: Path to the corrupt file
        file_dt: Observation datetime
    """
    file_size = raw_path.stat().st_size if raw_path.exists() else 0
    logger.warning(
        f"Corrupt .gz file: {raw_path.name} ({file_size} bytes) — "
        f"deleting for re-download"
    )

    # Delete the corrupt file
    try:
        raw_path.unlink()
    except Exception as e:
        logger.error(f"Could not delete {raw_path}: {e}")
        return

    # Reset file_tracking so the next download re-fetches it
    # file_tracking may store filename with or without .gz extension
    raw_name = raw_path.name
    name_without_gz = raw_name[:-3] if raw_name.endswith(".gz") else raw_name
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM file_tracking
                       WHERE sid = %s
                         AND session_type = %s
                         AND file_date = %s
                         AND filename IN (%s, %s)""",
                    (
                        station_id,
                        session_type,
                        file_dt.date(),
                        raw_name,
                        name_without_gz,
                    ),
                )
                deleted = cur.rowcount
            conn.commit()
        if deleted:
            logger.info(
                f"Reset file_tracking for {station_id}/{raw_path.name} "
                f"— will be re-downloaded"
            )
    except Exception as e:
        logger.warning(f"Could not reset file_tracking for {raw_path.name}: {e}")


# Backward-compatible aliases for imports in cli/scheduler.py
_find_sbf_file = _find_raw_file
_convert_sbf_to_rinex = _convert_raw_to_rinex
