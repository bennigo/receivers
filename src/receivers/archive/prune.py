"""Local ring-buffer prune — age out rek-d01's local gpsdata copies safely.

The local tree (``data_prepath``, /mnt/data/gpsdata on rek-d01) is a BUFFER:
the authoritative copy lives in the long-term archive (rawdata) and is
recorded in ``archive_catalog``. This module deletes local files older than a
per-session retention, with the safety spine:

  * **Catalog-gated**: a file is only deleted when its ``canonical_key`` is
    confirmed in ``archive_catalog`` for the ARCHIVE storage location — i.e.
    the long-term copy demonstrably exists. Uncataloged files are kept and
    counted loudly (they need archive-sync attention, not deletion).
  * **Disk guardrails**: every run reports free space on the data volume.
    Below ``warn_free_gb`` it WARNs (early, log-visible); below
    ``min_free_gb`` it switches to the (shorter) ``emergency_retention_days``
    and ERRORs — the ring tightens itself before the disk can hit 100%.
  * **Bounded**: ``max_delete_per_run`` caps a single pass; dry-run default
    in the CLI; every deletion goes to the audit log.

Retention lives in ``scheduler.yaml [local_prune]`` (synced from
gps-config-data) so it can be raised as the disk grows — config, not code.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from ..db.tx import read_only_cursor

logger = logging.getLogger("receivers.archive.prune")
audit = logging.getLogger("receivers.audit")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Short RINEX name: STATION + DOY + session char + .YY[dDoOnNmg]* — day precision
# comes from DOY+YY; the session char (0=daily, a-x=hour) is irrelevant here.
_RINEX_SHORT_RE = re.compile(r"^[A-Z0-9]{4}(?P<doy>\d{3})[0a-x]\.(?P<yy>\d{2})[a-zA-Z]")


@dataclass
class PruneConfig:
    """Ring-buffer policy — everything an operator may want to tune."""

    retention_days: dict = field(default_factory=dict)  # session -> days
    emergency_retention_days: dict = field(default_factory=dict)
    warn_free_gb: float = 150.0
    min_free_gb: float = 100.0
    require_catalog: bool = True
    max_delete_per_run: int = 20000
    file_categories: tuple = ("raw", "rinex")

    @classmethod
    def from_dict(cls, d: dict) -> PruneConfig:
        return cls(
            retention_days=dict(d.get("retention_days", {})),
            emergency_retention_days=dict(d.get("emergency_retention_days", {})),
            warn_free_gb=float(d.get("warn_free_gb", 150.0)),
            min_free_gb=float(d.get("min_free_gb", 100.0)),
            require_catalog=bool(d.get("require_catalog", True)),
            max_delete_per_run=int(d.get("max_delete_per_run", 20000)),
            file_categories=tuple(d.get("file_categories", ("raw", "rinex"))),
        )


@dataclass
class PruneStats:
    mode: str = "normal"  # normal | warn | emergency
    free_gb_before: float = 0.0
    free_gb_after: float = 0.0
    examined: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    kept_uncataloged: int = 0
    unparseable: int = 0
    capped: bool = False
    catalog_retracted: int = 0  # local_* archive_catalog rows removed on prune
    per_session: dict = field(default_factory=dict)  # session -> deleted count


def disk_free_gb(root: Path) -> tuple[float, float]:
    """(free_gb, total_gb) for the filesystem holding ``root``."""
    st = os.statvfs(root)
    return (st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9)


def disk_mode(root: Path, cfg: PruneConfig) -> tuple[str, float]:
    """Guardrail check: log the data-volume state and pick the prune mode.

    INFO always (trend is visible in the logs), WARNING under
    ``warn_free_gb`` — the operator's early signal — and ERROR + emergency
    mode under ``min_free_gb``.
    """
    free, total = disk_free_gb(root)
    used_pct = 100.0 * (1 - free / total) if total else 0.0
    if free < cfg.min_free_gb:
        logger.error(
            "DISK EMERGENCY: %.0f GB free (< min_free_gb=%.0f) on %s (%.0f%% used) "
            "— applying emergency retention",
            free,
            cfg.min_free_gb,
            root,
            used_pct,
        )
        return "emergency", free
    if free < cfg.warn_free_gb:
        logger.warning(
            "disk low: %.0f GB free (< warn_free_gb=%.0f) on %s (%.0f%% used) — "
            "consider expanding the volume or tightening [local_prune] retention",
            free,
            cfg.warn_free_gb,
            root,
            used_pct,
        )
        return "warn", free
    logger.info("disk ok: %.0f GB free of %.0f GB on %s", free, total, root)
    return "normal", free


def file_observation_date(name: str) -> Optional[date]:
    """Observation date claimed by an archive filename (raw or short RINEX)."""
    from .raw_format import parse_raw_name

    parsed = parse_raw_name(name)
    if parsed is not None:
        return parsed.claimed.date()
    m = _RINEX_SHORT_RE.match(name)
    if m:
        yy = int(m["yy"])
        year = 2000 + yy if yy < 80 else 1900 + yy
        try:
            return date(year, 1, 1) + timedelta(days=int(m["doy"]) - 1)
        except (ValueError, OverflowError):
            return None
    return None


def _month_dirs_upto(root: Path, cutoff: date) -> Iterator[tuple[Path, int, int]]:
    """Yield (month_dir, year, month) for months that can contain files older
    than ``cutoff`` — later months are skipped without touching their trees."""
    if not root.is_dir():
        return
    for ydir in sorted(root.iterdir()):
        if not (ydir.is_dir() and ydir.name.isdigit() and len(ydir.name) == 4):
            continue
        year = int(ydir.name)
        if year > cutoff.year:
            continue
        for mdir in sorted(ydir.iterdir()):
            month = _MONTHS.get(mdir.name)
            if month is None or not mdir.is_dir():
                continue
            if (year, month) > (cutoff.year, cutoff.month):
                continue
            yield mdir, year, month


def _archived_keys(conn, storage_location: str, session: str, category: str) -> set:
    """canonical_keys confirmed in archive_catalog for the long-term archive."""
    with read_only_cursor(conn) as cur:
        cur.execute(
            """SELECT canonical_key FROM archive_catalog
               WHERE storage_location = %s AND session_type = %s
                 AND file_category = %s""",
            (storage_location, session, category),
        )
        return {r[0] for r in cur.fetchall()}


def run_prune(
    root: Path,
    cfg: PruneConfig,
    *,
    archive_location: str,
    conn: Any = None,
    dry_run: bool = True,
    today: Optional[date] = None,
    sessions: Optional[list[str]] = None,
) -> PruneStats:
    """One ring-buffer pass over the local tree.

    ``conn`` is the gps_health connection holding ``archive_catalog``; with
    ``cfg.require_catalog`` (default) and no connection, NOTHING is deleted —
    fail-safe, never fail-destructive.
    """
    from ..utils.canonical_key import canonical_key

    root = Path(root)
    today = today or date.today()
    stats = PruneStats()
    stats.mode, stats.free_gb_before = disk_mode(root, cfg)

    retention = dict(cfg.retention_days)
    if stats.mode == "emergency":
        retention.update(cfg.emergency_retention_days)

    keys_cache: dict[tuple[str, str], Optional[set]] = {}

    def _keys(session: str, category: str) -> Optional[set]:
        k = (session, category)
        if k not in keys_cache:
            if conn is None:
                keys_cache[k] = None
            else:
                try:
                    keys_cache[k] = _archived_keys(
                        conn, archive_location, session, category
                    )
                except Exception as exc:  # noqa: BLE001 - fail-safe: keep files
                    logger.error("archive_catalog read failed (%s) — keeping all", exc)
                    keys_cache[k] = None
        return keys_cache[k]

    for session, days in sorted(retention.items()):
        if sessions and session not in sessions:
            continue
        try:
            days = int(days)
        except (TypeError, ValueError):
            logger.error("invalid retention for %s: %r — skipped", session, days)
            continue
        if days <= 0:
            logger.error("retention %s <= 0 for %s — refusing (safety)", days, session)
            continue
        cutoff = today - timedelta(days=days)
        deleted_session = 0
        for mdir, _y, _m in _month_dirs_upto(root, cutoff):
            for sta_dir in sorted(p for p in mdir.iterdir() if p.is_dir()):
                sess_dir = sta_dir / session
                if not sess_dir.is_dir():
                    continue
                for category in cfg.file_categories:
                    cat_dir = sess_dir / category
                    if not cat_dir.is_dir():
                        continue
                    archived = _keys(session, category)
                    if cfg.require_catalog and archived is None:
                        stats.kept_uncataloged += sum(1 for _ in cat_dir.iterdir())
                        continue
                    # Rows to retract from the LOCAL catalog for files pruned here.
                    # NB: the deletion gate above reads the ARCHIVE location
                    # (archive_location) — proof the permanent copy exists. This
                    # retraction targets the LOCAL ring row (local_raw/local_rinex)
                    # for the file that is going away — a separate location, never
                    # the gate's (else the gate would become circular).
                    retract_location = (
                        "local_rinex" if category == "rinex" else "local_raw"
                    )
                    retract_rels: list[str] = []
                    for f in sorted(cat_dir.iterdir()):
                        if not f.is_file():
                            continue
                        stats.examined += 1
                        obs = file_observation_date(f.name)
                        if obs is None:
                            stats.unparseable += 1
                            continue
                        if obs >= cutoff:
                            continue
                        if cfg.require_catalog and canonical_key(f.name) not in (
                            archived or set()
                        ):
                            stats.kept_uncataloged += 1
                            continue
                        if stats.deleted >= cfg.max_delete_per_run:
                            stats.capped = True
                            break
                        size = f.stat().st_size
                        if dry_run:
                            logger.debug("[DRY] would prune %s", f)
                        else:
                            try:
                                f.unlink()
                            except OSError as exc:
                                logger.error("prune failed for %s: %s", f, exc)
                                continue
                            audit.info(
                                "local-prune deleted %s (%d bytes, obs %s, "
                                "retention %sd, mode=%s)",
                                f,
                                size,
                                obs,
                                days,
                                stats.mode,
                            )
                            # file first, then catalog row (remove.py contract):
                            # a missing-file flag is a louder, recoverable failure
                            # than a silently-uncataloged file.
                            try:
                                retract_rels.append(str(f.relative_to(root)))
                            except ValueError:
                                pass  # not under root — leave the row, log nothing
                        stats.deleted += 1
                        deleted_session += 1
                        stats.freed_bytes += size
                    # Retract the local-ring catalog rows for the files just
                    # unlinked, so "present@local" stops over-reporting a pruned
                    # file (else missing_at_location under-fetches / missing_rinex
                    # re-rinexes a gone file). Best-effort: a retraction failure
                    # never resurrects the file, so it must not abort the prune.
                    if not dry_run and conn is not None and retract_rels:
                        from .remove import remove_catalog_rows

                        try:
                            stats.catalog_retracted += remove_catalog_rows(
                                conn, retract_location, retract_rels
                            )
                        except Exception as exc:  # noqa: BLE001 - never abort prune
                            logger.error(
                                "catalog retraction failed for %s (%d rows): %s",
                                cat_dir,
                                len(retract_rels),
                                exc,
                            )
                    if not dry_run:
                        _rmdir_if_empty(cat_dir)
                if not dry_run:
                    _rmdir_if_empty(sess_dir)
                    _rmdir_if_empty(sta_dir)
            if not dry_run:
                _rmdir_if_empty(mdir)
                _rmdir_if_empty(mdir.parent)
            if stats.capped:
                break
        stats.per_session[session] = deleted_session
        if stats.capped:
            logger.warning(
                "prune capped at max_delete_per_run=%d — remainder next pass",
                cfg.max_delete_per_run,
            )
            break

    stats.free_gb_after = disk_free_gb(root)[0] if root.is_dir() else 0.0
    logger.info(
        "local-prune %s: examined=%d deleted=%d freed=%.1f GB "
        "kept_uncataloged=%d unparseable=%d catalog_retracted=%d mode=%s "
        "per_session=%s",
        "[DRY-RUN]" if dry_run else "done",
        stats.examined,
        stats.deleted,
        stats.freed_bytes / 1e9,
        stats.kept_uncataloged,
        stats.unparseable,
        stats.catalog_retracted,
        stats.mode,
        stats.per_session,
    )
    return stats


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()  # only succeeds when empty
    except OSError:
        pass


def record_and_forecast(
    volume: Path,
    state_path: Path,
    *,
    warn_days_to_full: int = 21,
    today: Optional[date] = None,
    window_days: int = 14,
    keep_samples: int = 90,
) -> Optional[float]:
    """Track free space over time and forecast days-to-full for ``volume``.

    Appends today's free-GB sample to a small JSON history and estimates the
    fill rate over the last ``window_days``. When the volume is on course to
    fill within ``warn_days_to_full`` days it logs an ERROR — the aggressive,
    log-visible signal ops asked for so storage can be expanded incrementally
    BEFORE the disk runs out (IT lead time ~3 weeks). Returns the estimated
    days-to-full (None when no trend yet or the volume is not filling).
    """
    import json

    today = today or date.today()
    try:
        free, total = disk_free_gb(Path(volume))
    except OSError as exc:
        logger.warning("forecast: cannot stat %s: %s", volume, exc)
        return None

    key = str(volume)
    history: dict = {}
    try:
        history = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        history = {}
    samples = [s for s in history.get(key, []) if s[0] != today.isoformat()]
    samples.append([today.isoformat(), round(free, 2)])
    samples = samples[-keep_samples:]
    history[key] = samples
    try:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps(history))
    except OSError as exc:
        logger.warning("forecast: cannot persist history %s: %s", state_path, exc)

    window_floor = today - timedelta(days=window_days)
    window = [s for s in samples if s[0] >= window_floor.isoformat()]
    if len(window) < 2:
        return None
    d0, f0 = window[0]
    span_days = (today - date.fromisoformat(d0)).days
    if span_days < 2:
        return None  # need a real baseline before trusting a rate
    rate_gb_per_day = (f0 - free) / span_days
    if rate_gb_per_day <= 0:
        logger.info(
            "forecast %s: %.0f GB free, not filling (%.1f GB/day)",
            volume,
            free,
            rate_gb_per_day,
        )
        return None
    days_to_full = free / rate_gb_per_day
    if days_to_full <= warn_days_to_full:
        logger.error(
            "DISK FILL FORECAST: %s full in ~%.0f days at %.1f GB/day "
            "(%.0f GB free of %.0f) — request expansion NOW (lead time!)",
            volume,
            days_to_full,
            rate_gb_per_day,
            free,
            total,
        )
    elif days_to_full <= 2 * warn_days_to_full:
        logger.warning(
            "disk fill forecast: %s full in ~%.0f days at %.1f GB/day (%.0f GB free)",
            volume,
            days_to_full,
            rate_gb_per_day,
            free,
        )
    else:
        logger.info(
            "forecast %s: ~%.0f days to full at %.1f GB/day (%.0f GB free)",
            volume,
            days_to_full,
            rate_gb_per_day,
            free,
        )
    return days_to_full


def scratch_report(tmp_dir: Path, warn_free_gb: float = 5.0) -> None:
    """Guardrail for the download scratch volume (best-effort, log-only)."""
    try:
        free, total = disk_free_gb(Path(tmp_dir))
    except OSError:
        return
    if free < warn_free_gb:
        logger.warning(
            "download scratch low: %.1f GB free of %.0f GB at %s",
            free,
            total,
            tmp_dir,
        )
