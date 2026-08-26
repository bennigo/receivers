"""T4 — index a disseminated RINEX file into the EPOS ``rinex_file`` table.

Records each pushed file with TWO md5s, exactly as EPOS expects (and distinct
from the archive's ``content_sha256``):

* ``md5checksum``     — md5 of the file **as published** (compressed bytes), and
* ``md5uncompressed`` — md5 of the fully-decompressed RINEX observation content
  (gunzip/.Z + un-Hatanaka), so a consumer can verify the data independent of the
  on-disk packaging.

Upserts the supporting ``data_center`` / ``file_type`` / ``data_center_structure``
rows (IMO data centre, hardcoded like the legacy importer) and then the
``rinex_file`` row, keyed on ``(name, relative_path)`` via SELECT-then-write (the
schema has no UNIQUE there, so we don't rely on ON CONFLICT). A re-index updates
the row and stamps ``revision_date`` — the hook the retroactive header-correction
re-push needs.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import epos_db
from .convert import _is_hatanaka, _strip_compression, resolve_tool
from .epos_db import get_or_create, insert_row, update_row

logger = logging.getLogger("receivers.dissemination.index")

# IMO data centre (hardcoded, as in the legacy importer).
_DATA_CENTER = {
    "acronym": "IMO",
    "hostname": "data.epos-iceland.is",
    "root_path": "",
    "name": "IMO",
    "protocol": "https",
}


def _md5_bytes(data: bytes) -> str:
    # usedforsecurity=False: MD5 is not a security control here — it is the
    # interchange format EPOS requires for md5checksum / md5uncompressed (see
    # this module's docstring). Saying so explicitly satisfies bandit B324 and,
    # more usefully, keeps this working on a FIPS-mode Python, where an
    # unqualified hashlib.md5() raises instead of hashing.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def rinex_md5s(path: Path) -> tuple[str, str]:
    """Return ``(md5checksum, md5uncompressed)`` for a RINEX file.

    ``md5checksum`` is over the file bytes as-is; ``md5uncompressed`` is over the
    decompressed, un-Hatanaka RINEX observation content (so a plain ``.rnx`` gives
    equal values, while a ``.crx.gz`` gives the compressed vs the obs md5).
    """
    path = Path(path)
    raw = path.read_bytes()
    md5checksum = _md5_bytes(raw)

    _, was_compressed = _strip_compression(path.name)
    if was_compressed:
        decompressed = subprocess.run(
            ["gzip", "-dc", str(path)], capture_output=True, check=True
        ).stdout
    else:
        decompressed = raw

    # If the decompressed content is Hatanaka (CRINEX), un-Hatanaka it for the
    # "uncompressed" md5 (CRX2RNX reads stdin with '-').
    inner_name, _ = _strip_compression(path.name)
    if _is_hatanaka(inner_name):
        crx2rnx = resolve_tool("CRX2RNX")
        decompressed = subprocess.run(
            [crx2rnx, "-"], input=decompressed, capture_output=True, check=True
        ).stdout

    return md5checksum, _md5_bytes(decompressed)


def _file_type_for(rinex_version: int, session: str) -> dict[str, str]:
    """The ``file_type`` row values for a session (24h/15s assumptions for now)."""
    window = "24hour" if "24hr" in session or "24h" in session else "N/N"
    freq = "15s" if "15s" in session else "N/N"
    return {
        "format": f"RINEX{rinex_version}",
        "sampling_window": window,
        "sampling_frequency": freq,
    }


def index_rinex_file(
    conn,
    file_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    relative_path: str,
    session: str = "15s_24hr",
    rinex_version: int = 3,
    published_dt: Optional[datetime] = None,
) -> Optional[int]:
    """Upsert one ``rinex_file`` row for a disseminated file. Returns its id.

    Returns ``None`` (and logs) if the station isn't in the EPOS DB yet — the
    metadata ETL (T5) must run first (the row FKs to ``station``).
    """
    file_path = Path(file_path)
    marker = station.upper()

    with epos_db.tx_cursor(conn) as cur:
        cur.execute("SELECT id FROM station WHERE upper(marker) = %s", (marker,))
        row = cur.fetchone()
        if row is None:
            logger.warning(
                "station %s not in EPOS DB — run the metadata ETL first; "
                "skipping rinex_file index",
                marker,
            )
            return None
        id_station = row[0]

        id_agency = get_or_create(
            cur, "agency", {"abbreviation": "IMO"}, {"name": "IMO"}
        )
        id_data_center = get_or_create(
            cur,
            "data_center",
            {"acronym": "IMO"},
            {
                **{k: v for k, v in _DATA_CENTER.items() if k != "acronym"},
                "id_agency": id_agency,
            },
        )
        id_file_type = get_or_create(
            cur, "file_type", _file_type_for(rinex_version, session)
        )
        get_or_create(
            cur,
            "data_center_structure",
            {"id_data_center": id_data_center, "id_file_type": id_file_type},
            {"directory_naming": "unknown", "comments": None},
        )

        md5checksum, md5uncompressed = rinex_md5s(file_path)
        values = {
            "name": file_path.name,
            "id_station": id_station,
            "id_data_center": id_data_center,
            "file_size": file_path.stat().st_size,
            "id_file_type": id_file_type,
            "relative_path": relative_path,
            "reference_date": observation_dt,
            "creation_date": None,
            "published_date": published_dt or datetime.now(),
            "md5checksum": md5checksum,
            "md5uncompressed": md5uncompressed,
            "status": 0,
        }

        # Upsert keyed on (name, relative_path) — no UNIQUE in schema, so do it
        # by hand; a re-index stamps revision_date.
        cur.execute(
            "SELECT id FROM rinex_file WHERE name = %s AND relative_path = %s",
            (file_path.name, relative_path),
        )
        hit = cur.fetchone()
        if hit is not None:
            values["revision_date"] = datetime.now()
            update_row(cur, "rinex_file", int(hit[0]), values)
            rid = int(hit[0])
        else:
            rid = insert_row(cur, "rinex_file", values)

    conn.commit()
    logger.info("indexed rinex_file %s (id=%s) for %s", file_path.name, rid, marker)
    return rid


# Supersede-cleanup: a new R3 long-name product replaces the legacy short-name
# file the old epos-gnss container pushed for the same day. After a durable
# push + index of the new file, remove the superseded legacy file (portal + DB).
# Bounded so a runaway can't delete an unexpectedly-large file; a RINEX daily is
# single-digit MB.
_SUPERSEDE_MAX_BYTES = 100 * 1024 * 1024


def deindex_rinex_file(conn, name: str) -> list[int]:
    """Delete the ``rinex_file`` row(s) for the exact ``name`` and return the ids.

    Keyed on ``name`` alone: a short RINEX name (``RHOF1770.26D.Z``) encodes
    station+year+DOY and is unique, so this targets exactly the superseded legacy
    row — NOT a DOY glob. Legacy rows carry a dir-only ``relative_path`` that
    differs from ours, so matching on name (not name+path) is both correct and
    deliberate here.
    """
    with epos_db.tx_cursor(conn) as cur:
        cur.execute("DELETE FROM rinex_file WHERE name = %s RETURNING id", (name,))
        ids = [int(r[0]) for r in cur.fetchall()]
    conn.commit()
    if ids:
        logger.info("de-indexed legacy rinex_file %s (ids=%s)", name, ids)
    return ids


def supersede_legacy(
    conn,
    *,
    superseded_name: str,
    relative_dir: str,
    ssh_target: str,
    dest_root: str,
    dry_run: bool = True,
) -> dict:
    """Remove the legacy short-name file replaced by the new long-name product.

    Portal delete via the argv-safe SSH gateway (``remove_archive_files``), then
    (real runs only) de-index the row. Caller MUST gate on a durable push+index
    of the NEW file and ``superseded_name != new name`` (the R3-long case) — this
    function does not re-check that. ``dry_run`` shows intent without touching the
    portal or DB. Never raises (best-effort cleanup; the product is already live).
    """
    from ..archive.remove import remove_archive_files

    rel = (
        f"{relative_dir.rstrip('/')}/{superseded_name}"
        if relative_dir
        else superseded_name
    )
    out: dict = {
        "legacy_rel": rel,
        "removed": [],
        "would_remove": [],
        "skipped": [],
        "deindexed": [],
    }
    try:
        rm = remove_archive_files(
            [rel],
            ssh_target=ssh_target,
            dest_root=dest_root,
            max_size=_SUPERSEDE_MAX_BYTES,
            execute=not dry_run,
        )
        out["removed"] = [r for r, _ in rm.deleted]
        out["would_remove"] = [r for r, _ in rm.would_delete]
        out["skipped"] = (
            [r for r, _ in rm.skipped_toobig] + list(rm.missing) + list(rm.invalid)
        )
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("supersede portal delete failed for %s: %s", rel, exc)
    if not dry_run:
        out["deindexed"] = _deindex_recovering(conn, superseded_name)
    return out


def supersede_legacy_batch(
    conn,
    items: list[tuple[str, str]],
    *,
    ssh_target: str,
    dest_root: str,
    dry_run: bool = True,
) -> dict:
    """Batch form of :func:`supersede_legacy` for range/backfill sweeps.

    ``items`` = ``(superseded_name, relative_dir)`` pairs, each already gated by
    the caller on a durable push+index of its replacement. ONE argv-safe SSH
    call removes the whole batch (vs one round-trip per date — the difference
    between minutes and hours on a full-history portal refresh), then each
    removed name is de-indexed. Never raises.
    """
    from ..archive.remove import remove_archive_files

    out: dict = {"removed": [], "would_remove": [], "skipped": [], "deindexed": []}
    if not items:
        return out
    rel_by_name = {
        name: (f"{rel_dir.rstrip('/')}/{name}" if rel_dir else name)
        for name, rel_dir in items
    }
    try:
        rm = remove_archive_files(
            sorted(rel_by_name.values()),
            ssh_target=ssh_target,
            dest_root=dest_root,
            max_size=_SUPERSEDE_MAX_BYTES,
            execute=not dry_run,
        )
        out["removed"] = [r for r, _ in rm.deleted]
        out["would_remove"] = [r for r, _ in rm.would_delete]
        out["skipped"] = (
            [r for r, _ in rm.skipped_toobig] + list(rm.missing) + list(rm.invalid)
        )
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("supersede batch portal delete failed: %s", exc)
        return out
    if not dry_run:
        # De-index names removed just now AND names already absent from the
        # portal: a prior partially-failed flush may have removed the file but
        # left its DB row, so gating on this call's removals alone would leave
        # the stale row forever. De-indexing a never-indexed name is a no-op.
        clean_rels = set(out["removed"]) | set(rm.missing)
        for name, rel in rel_by_name.items():
            if rel not in clean_rels:
                continue
            out["deindexed"].extend(_deindex_recovering(conn, name))
    return out


def _deindex_recovering(conn, name: str) -> list[int]:
    """De-index with recover + one retry, never raising.

    A statement error aborts the psycopg2 transaction and would poison every
    later de-index on this connection ('current transaction is aborted') —
    recover (rollback + search_path re-assert) and retry once, mirroring the
    index path in job._index_pushed.
    """
    for attempt in (1, 2):
        try:
            return deindex_rinex_file(conn, name)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            recovered = epos_db.recover(conn)
            if attempt == 2 or not recovered:
                logger.warning("supersede de-index failed for %s: %s", name, exc)
                return []
            logger.info("supersede de-index retry for %s after: %s", name, exc)
    return []


# --- G2: same-slot invariant (one file per station/obs-date/product dir) --------
# The narrow supersede above matched a single precomputed legacy name and only
# for the native product — it missed .d/.D case variants (the source basename it
# derived was the re-rinexed .D, the straggler was a mistaken-run .d) and never
# touched decimated-product residue. The purge below enforces the real invariant
# directly: after a durable push+index of a product, any OTHER indexed file in
# the SAME (station, obs-date, dir) is a stale variant and is removed by its
# ACTUAL stored name/case. Bounded to one slot — never a DOY glob.


def find_stale_siblings(
    conn, *, marker: str, obs_date: Any, relative_dir: str, keep_name: str
) -> list[tuple[str, str, str]]:
    """Indexed files in the same slot as a freshly pushed product but with a
    different name — the stale variants to purge. Returns
    ``(name, stored_path, dest_rel)`` where ``dest_rel`` is the actual
    portal-relative file path (``<dir>/<name>``) to remove.

    Two row shapes both count as in-slot: OUR full-path rows
    (``/files/<dir>/<name>``) and the legacy container's DIR-ONLY rows
    (``/files/<dir>/``, filename carried in ``name``). Both are the legacy day
    file our long-name product replaces. Deeper subdir rows are excluded.

    Pinned to a single day AND a single dir. The dir holds a whole month, so the
    ``reference_date`` predicate is what isolates the one date — without it this
    would sweep the month. The name comparison is CASE-SENSITIVE on purpose: the
    portal FS is case-sensitive, so ``RHOF1790.11d.Z`` and ``RHOF1790.11D.Z`` are
    two different files — when G3 re-publishes the uppercase ``.D``, the old
    lowercase ``.d`` is a stale sibling that must be removed (a case-insensitive
    match would treat it AS the keep and leave it behind).
    """
    rel_dir = relative_dir.strip("/")
    prefix = f"/files/{rel_dir}/"
    with epos_db.tx_cursor(conn) as cur:
        cur.execute(
            """SELECT rf.name, rf.relative_path
               FROM rinex_file rf JOIN station s ON s.id = rf.id_station
               WHERE upper(s.marker) = %s
                 AND rf.reference_date::date = %s
                 AND rf.relative_path LIKE %s
                 AND rf.name <> %s""",
            (marker.upper(), obs_date, prefix + "%", keep_name),
        )
        rows = cur.fetchall()
    out: list[tuple[str, str, str]] = []
    for n, p in rows:
        n, p = str(n), str(p)
        # our full-path row, OR a legacy dir-only row (path == the dir itself).
        # Anything else under the prefix is a deeper subdir → not this slot.
        if p == f"{prefix}{n}" or p == prefix:
            out.append((n, p, f"{rel_dir}/{n}"))
    return out


def _deindex_row(conn, name: str, relative_path: str) -> list[int]:
    """Delete the exact ``(name, relative_path)`` row(s); return ids."""
    with epos_db.tx_cursor(conn) as cur:
        cur.execute(
            "DELETE FROM rinex_file WHERE name = %s AND relative_path = %s "
            "RETURNING id",
            (name, relative_path),
        )
        ids = [int(r[0]) for r in cur.fetchall()]
    conn.commit()
    if ids:
        logger.info("purged stale rinex_file %s (ids=%s)", name, ids)
    return ids


def purge_stale_siblings_batch(
    conn,
    slots: list[tuple[str, Any, str, str]],
    *,
    ssh_target: str,
    dest_root: str,
    dry_run: bool = True,
) -> dict:
    """Enforce one-file-per-slot across a batch. Each ``slot`` is
    ``(marker, obs_date, relative_dir, keep_name)`` for a durably pushed+indexed
    product; remove any OTHER indexed portal file in that slot (portal + DB).
    ONE argv-safe SSH rm for the whole set. Never raises."""
    out: dict = {"removed": [], "would_remove": [], "skipped": [], "deindexed": []}
    if not slots or conn is None:
        return out
    from ..archive.remove import remove_archive_files

    victims: list[tuple[str, str, str]] = []  # (dest_rel, name, stored_path)
    seen: set[str] = set()
    for marker, obs_date, rel_dir, keep in slots:
        try:
            sibs = find_stale_siblings(
                conn,
                marker=marker,
                obs_date=obs_date,
                relative_dir=rel_dir,
                keep_name=keep,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            epos_db.recover(conn)
            logger.warning(
                "stale-sibling scan failed (%s %s): %s", marker, obs_date, exc
            )
            continue
        for name, stored, dest_rel in sibs:
            if dest_rel in seen:
                continue
            seen.add(dest_rel)
            victims.append((dest_rel, name, stored))
    if not victims:
        return out
    try:
        rm = remove_archive_files(
            sorted(v[0] for v in victims),
            ssh_target=ssh_target,
            dest_root=dest_root,
            max_size=_SUPERSEDE_MAX_BYTES,
            execute=not dry_run,
        )
        out["removed"] = [r for r, _ in rm.deleted]
        out["would_remove"] = [r for r, _ in rm.would_delete]
        out["skipped"] = (
            [r for r, _ in rm.skipped_toobig] + list(rm.missing) + list(rm.invalid)
        )
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("stale-sibling portal delete failed: %s", exc)
        return out
    if not dry_run:
        clean = set(out["removed"]) | set(rm.missing)
        for dest_rel, name, stored in victims:
            if dest_rel in clean:
                out["deindexed"].extend(_deindex_row(conn, name, stored))
    return out
