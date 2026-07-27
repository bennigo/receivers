"""archive_catalog verify — detect divergence and confirm files copied intact.

Two integrity checks against the IMO archive catalog (migration 050), both keyed
on the compression-invariant content_sha256 (over DECOMPRESSED content, so a .Z
on the archive compares by content to its local .gz/.d twin):

  1. **local↔archive cross-check** (cheap, DB-only): compare
     ``file_tracking.content_sha256`` (the local copy, filled lazily by the
     integrity checker, mig 052) to ``archive_catalog.content_sha256`` for the
     same logical file. A mismatch means the local and archived copies diverged
     — a re-download, an edit, or corruption on one side.

  2. **read-back verify** (re-hash the archive copy): when a ``read_root`` is
     given (rek-d01 mounts the archive read-only at /mnt/rawgpsdata), re-hash the
     ACTUAL file on the archive and compare to the stored hash. Match → stamp
     ``last_verified_at``. Mismatch → the file did NOT copy intact (archive-side
     bit-rot or a truncated transfer). This is the guarantee behind "edit a
     RINEX locally, push to rawdata, verify nothing got corrupted".

Read-back is the load-bearing check; the cross-check runs for free alongside it
and also works on a host without the mount.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from ..utils.content_hash import CorruptArchiveFileError, content_sha256

logger = logging.getLogger("receivers.archive.verify")


@dataclass
class VerifyStats:
    """Outcome counts for a verify run."""

    checked: int = 0
    verified: int = 0  # read-back matched, last_verified_at stamped
    mismatched: int = 0  # read-back hash != catalog hash (archive corruption)
    missing: int = 0  # archive file absent/unreadable at read_root
    local_divergent: int = 0  # file_tracking hash != catalog hash
    read_back: bool = False  # whether read-back ran (read_root provided)
    findings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "verified": self.verified,
            "mismatched": self.mismatched,
            "missing": self.missing,
            "local_divergent": self.local_divergent,
            "read_back": self.read_back,
            "findings": self.findings,
        }


def _local_session(session_type: str, file_category: str) -> str:
    """Map a catalog (session_type, category) to the file_tracking session_type.

    file_tracking distinguishes RINEX with a ``_rinex`` suffix; archive_catalog
    splits the same into session_type + file_category.
    """
    return f"{session_type}_rinex" if file_category == "rinex" else session_type


def _local_archive_path(
    file_path: str, dest_prefix: Optional[str], read_root: str
) -> str:
    """Translate a stored archive ``file_path`` to a readable path under read_root.

    archive_catalog.file_path is the dest path on the rawdata host (e.g.
    ``~/gpsdata/2026/jun/.../FILE``). On rek-d01 the same storage is mounted
    read-only at ``read_root`` (/mnt/rawgpsdata), so we swap the dest prefix for
    the mount. Falls back to the tail after the last ``gpsdata/`` segment when the
    prefix does not match (defensive — keeps a config drift from silently
    skipping every file).
    """
    if dest_prefix and file_path.startswith(dest_prefix):
        rel = file_path[len(dest_prefix) :].lstrip("/")
    else:
        marker = "gpsdata/"
        idx = file_path.rfind(marker)
        rel = (
            file_path[idx + len(marker) :] if idx >= 0 else os.path.basename(file_path)
        )
    return os.path.join(read_root, rel)


def verify_archive_catalog(
    conn,
    *,
    storage_location: str = "imo_archive",
    read_root: Optional[str] = None,
    dest_prefix: Optional[str] = None,
    limit: int = 500,
    reverify_after_days: Optional[int] = None,
    priority_sessions: tuple[str, ...] = ("15s_24hr", "1Hz_1hr"),
    workers: int = 1,
    log: logging.Logger = logger,
) -> VerifyStats:
    """Verify catalog rows: local↔archive cross-check + optional read-back.

    Args:
        conn: gps_health DB connection.
        storage_location: archive_catalog.storage_location to verify ('imo_archive').
        read_root: local mount of the archive (e.g. /mnt/rawgpsdata). If None,
            only the DB-only cross-check runs (no read-back, no last_verified_at).
        dest_prefix: the target.dest stored in file_path (e.g. '~/gpsdata'),
            swapped for read_root to locate the archive file.
        limit: max catalog rows per run.
        reverify_after_days: if set, re-verify rows whose last_verified_at is
            older than this (else only never-verified rows when read-back is on).
        workers: >1 pre-hashes the archive files on a thread pool (the
            expensive step — decompress + sha256 over NFS). All DB access
            stays on the calling thread; connections aren't shared.
        log: logger.

    Returns:
        VerifyStats with per-outcome counts and human-readable findings.
    """
    stats = VerifyStats(read_back=read_root is not None)
    if conn is None:
        return stats

    # Priority sessions (15s_24hr, 1Hz_1hr) cold-re-hashed FIRST — the daily-
    # processing inputs whose durability matters most; this is the 1Hz half of
    # "15s immediate + 1Hz cold-priority". Then never-verified first, oldest
    # verification, newest data.
    select_sql = """
        SELECT id, station, session_type, file_category, file_date, file_path,
               content_sha256, canonical_key
        FROM archive_catalog
        WHERE storage_location = %s
          AND content_sha256 IS NOT NULL
          AND (
                %s IS NULL
                OR last_verified_at IS NULL
                OR last_verified_at < now() - (%s * interval '1 day')
              )
        ORDER BY (session_type = ANY(%s)) DESC,
                 last_verified_at NULLS FIRST, file_date DESC NULLS LAST
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(
            select_sql,
            (
                storage_location,
                reverify_after_days,
                reverify_after_days,
                list(priority_sessions),
                limit,
            ),
        )
        rows = cur.fetchall()

    # Parallel pre-hash: compute the read-back hashes up front on a thread
    # pool; the row loop below then consumes the results without touching
    # the filesystem. status ∈ {"ok", "missing", "error"}.
    pre_hash: dict = {}
    if read_root is not None and workers > 1 and rows:
        from ..utils.batch_parallel import run_chunks

        def _hash_row(row):
            row_id, file_path = row[0], row[5]
            lp = _local_archive_path(file_path, dest_prefix, read_root)
            if not os.path.isfile(lp):
                return (row_id, "missing", None)
            try:
                return (row_id, "ok", content_sha256(lp))
            except (CorruptArchiveFileError, OSError) as exc:
                return (row_id, "error", exc)

        for oc in run_chunks(
            rows, _hash_row, workers=workers, logger=log, load_gate=False
        ):
            if oc.ok and oc.value is not None:
                rid, status, payload = oc.value
                pre_hash[rid] = (status, payload)

    for (
        row_id,
        station,
        session_type,
        file_category,
        file_date,
        file_path,
        cat_hash,
        canonical_key,
    ) in rows:
        stats.checked += 1

        # (1) local↔archive cross-check — DB only.
        local_session = _local_session(session_type, file_category)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT content_sha256 FROM file_tracking
                   WHERE sid = %s AND session_type = %s AND file_date = %s
                     AND file_hour IS NULL AND content_sha256 IS NOT NULL""",
                (station, local_session, file_date),
            )
            local_row = cur.fetchone()
        if local_row and local_row[0] != cat_hash:
            stats.local_divergent += 1
            stats.findings.append(
                f"local≠archive {station}/{local_session}/{file_date}: "
                f"local={local_row[0][:12]} archive={cat_hash[:12]}"
            )

        # (2) read-back verify — re-hash the actual archive file.
        if read_root is None:
            continue
        local_path = _local_archive_path(file_path, dest_prefix, read_root)
        _pre = pre_hash.get(row_id)
        if _pre is not None:
            _status, _payload = _pre
            if _status == "missing":
                stats.missing += 1
                log.debug(f"verify: archive file not found at {local_path}")
                continue
            if _status == "error":
                stats.mismatched += 1
                stats.findings.append(
                    f"unreadable archive file {local_path}: {_payload}"
                )
                log.warning(
                    f"verify: cannot hash archive file {local_path}: {_payload}"
                )
                continue
            actual = _payload
        else:
            if not os.path.isfile(local_path):
                stats.missing += 1
                log.debug(f"verify: archive file not found at {local_path}")
                continue
            try:
                actual = content_sha256(local_path)
            except (CorruptArchiveFileError, OSError) as exc:
                stats.mismatched += 1
                stats.findings.append(f"unreadable archive file {local_path}: {exc}")
                log.warning(f"verify: cannot hash archive file {local_path}: {exc}")
                continue

        if actual == cat_hash:
            with conn.cursor() as cur:
                # Keyed on (storage_location, file_path), NOT the surrogate id:
                # this connection may fan out to the pgdev mirror, whose ids
                # are assigned independently — an id-keyed UPDATE would stamp
                # last_verified_at on an unrelated row there.
                # NULL session_type needs `IS NULL`, but `IS NOT DISTINCT FROM`
                # is not btree-indexable — it demotes session_type from the
                # archive_catalog_logical_key Index Cond to a Filter, which on
                # 8.5M rows (all sharing storage_location='imo_archive') costs
                # a near-full index scan per verified file. Pick the predicate.
                if session_type is None:
                    cur.execute(
                        """UPDATE archive_catalog SET last_verified_at = now()
                           WHERE storage_location = %s AND session_type IS NULL
                             AND file_category = %s AND canonical_key = %s""",
                        (storage_location, file_category, canonical_key),
                    )
                else:
                    cur.execute(
                        """UPDATE archive_catalog SET last_verified_at = now()
                           WHERE storage_location = %s AND session_type = %s
                             AND file_category = %s AND canonical_key = %s""",
                        (
                            storage_location,
                            session_type,
                            file_category,
                            canonical_key,
                        ),
                    )
            conn.commit()
            stats.verified += 1
        else:
            stats.mismatched += 1
            stats.findings.append(
                f"ARCHIVE CORRUPT {station}/{session_type}/{file_category}/{file_date}: "
                f"on-disk={actual[:12]} catalog={cat_hash[:12]} ({local_path})"
            )
            log.error(
                f"verify: archive file hash mismatch — {local_path} "
                f"on-disk={actual} catalog={cat_hash}"
            )

    if stats.mismatched or stats.local_divergent:
        log.warning(
            f"Archive verify: {stats.checked} checked, {stats.verified} verified, "
            f"{stats.mismatched} CORRUPT, {stats.local_divergent} local-divergent, "
            f"{stats.missing} missing"
        )
    else:
        log.info(
            f"Archive verify: {stats.checked} checked, {stats.verified} verified, "
            f"{stats.missing} missing (no corruption)"
        )
    return stats
