"""archive_catalog reindex — refresh content_sha256 for files changed out-of-band.

The forward catalog (``catalog.upsert_catalog_row``) is written by the sync
engine for every file it *transfers*. But a file can be modified on the archive
by a path that does NOT go through the engine — notably
``receivers rinex --fix-headers --push``, which rsyncs corrected RINEX straight
to the archive. After such a write the archive bytes change but the catalog's
``content_sha256`` still reflects the pre-edit content, so the scheduled
integrity verify would flag the row as corrupt (a false positive).

Reindex closes that gap: re-hash the authoritative bytes and upsert the row.
The bytes are taken from a local *staging mirror* (the ``--work-dir`` tree that
``--fix-headers`` pushed from) — byte-identical to what rsync placed on the
archive — so no archive mount or ssh read-back is needed. This makes it usable
from a laptop, where this kind of maintenance work actually happens (the
production server is busy with the daily runs).

``content_sha256`` here matches the verify pass exactly: it is taken over the
DECOMPRESSED content (see :mod:`receivers.utils.content_hash`), so a ``.d.Z``
Hatanaka file hashes identically to its decompressed twin, and a header rewrite
changes the hash (which is the whole point).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..db.tx import read_only_cursor
from ..utils.canonical_key import canonical_key
from ..utils.content_hash import (
    CorruptArchiveFileError,
    compressed_sha256,
    content_sha256,
)
from .catalog import upsert_catalog_row
from .path_parse import parse_archive_path

logger = logging.getLogger(__name__)


@dataclass
class ReindexStats:
    """Outcome of a reindex run."""

    updated: int = 0  # existing row, content_sha256 changed
    inserted: int = 0  # no prior row for this file
    unchanged: int = 0  # row already held the correct hash
    errors: list[str] = field(default_factory=list)
    skipped: int = 0  # file could not be parsed to an archive identity
    skipped_new: int = 0  # only_existing: no prior row, insert suppressed

    @property
    def touched(self) -> int:
        return self.updated + self.inserted

    def to_dict(self) -> dict:
        return {
            "updated": self.updated,
            "inserted": self.inserted,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "skipped_new": self.skipped_new,
            "errors": self.errors,
        }


def reindex_files(
    conn,
    files: list[str],
    *,
    root: str,
    storage_location: str,
    dest_prefix: str,
    dry_run: bool = False,
    only_existing: bool = False,
    log: logging.Logger = logger,
) -> ReindexStats:
    """Re-hash each local file and upsert its ``archive_catalog`` row.

    Args:
        conn: gps_health DB connection (the catalog host — pass a pgdev
            connection to update production).
        files: local file paths, each under ``root`` in the archive mirror
            layout (``YYYY/mon/STA/session/category/FILE``).
        root: the mirror root the ``files`` are relative to (e.g. the
            ``--fix-headers`` work-dir). Used only to derive the archive-relative
            path; the bytes hashed are the local file's.
        storage_location: ``archive_catalog.storage_location`` to write
            (e.g. ``imo_archive``).
        dest_prefix: the archive dest the files live at (e.g. ``~/gpsdata``);
            combined with the relative path to form ``file_path``.
        dry_run: classify + log but do not write.
        only_existing: only repair rows that already exist (skip inserts) — for
            surgically fixing sha256 the caller knows went stale, without
            expanding catalog coverage to previously-uncataloged files. Skipped
            inserts are counted in ``stats.skipped``.

    Returns:
        :class:`ReindexStats`.
    """
    stats = ReindexStats()
    if conn is None:
        stats.errors.append("no DB connection")
        return stats
    dest_prefix = dest_prefix.rstrip("/")

    for f in files:
        parsed = parse_archive_path(f, root)
        if parsed is None:
            stats.skipped += 1
            log.warning("reindex: cannot parse archive identity from %s", f)
            continue
        try:
            digest = content_sha256(f)
        except CorruptArchiveFileError as exc:
            # DEBUG, not ERROR: a re-rinex --push run reindexes the staging tree
            # AFTER _push_cleanup has removed what it pushed, so every cleaned
            # file lands here. One VMEY run emitted 238 of these — two lines per
            # file — burying the actual conversion result. The aggregate below
            # is the operator-facing signal; the per-file detail stays in
            # stats.errors and at -v.
            stats.errors.append(f"corrupt, not reindexed: {f}: {exc}")
            log.debug("reindex: corrupt local file %s: %s", f, exc)
            continue
        except OSError as exc:
            stats.errors.append(f"could not read {f}: {exc}")
            log.debug("reindex: could not read %s: %s", f, exc)
            continue

        key = canonical_key(os.path.basename(f))
        archive_path = f"{dest_prefix}/{parsed.relative_path}"

        # Classify against the existing row so the report distinguishes a genuine
        # correction (updated) from a no-op (unchanged) or a first index (inserted).
        prior = _existing_sha(
            conn, storage_location, parsed.session_type, parsed.file_category, key
        )
        if prior is None:
            outcome = "inserted"
        elif prior == digest:
            outcome = "unchanged"
        else:
            outcome = "updated"

        if outcome == "unchanged":
            stats.unchanged += 1
            continue
        if outcome == "inserted" and only_existing:
            stats.skipped_new += 1  # no prior row and caller asked to skip inserts
            continue

        if dry_run:
            log.info(
                "reindex[DRY]: %s %s %s → %s (%s)",
                storage_location,
                parsed.station,
                key,
                digest[:12],
                outcome,
            )
        else:
            upsert_catalog_row(
                conn,
                storage_location=storage_location,
                station=parsed.station,
                session_type=parsed.session_type,
                file_category=parsed.file_category,
                file_date=parsed.file_date,
                file_hour=parsed.file_hour,
                archive_path=archive_path,
                filename=os.path.basename(f),
                file_size=os.path.getsize(f),
                content_sha256=digest,
            )
            conn.commit()  # per-file: a crash loses one row, not the run
        if outcome == "updated":
            stats.updated += 1
        else:
            stats.inserted += 1
    # One aggregate line instead of two per unreadable file. On a re-rinex
    # --push run these are almost always files _push_cleanup already removed
    # after a successful push — expected, not a fault — so the count plus a
    # couple of examples is the useful signal. Full detail is in stats.errors.
    if stats.errors:
        sample = ", ".join(
            e.split(": ", 1)[-1].split(":")[0].strip() for e in stats.errors[:3]
        )
        log.warning(
            "reindex: %d file(s) unreadable or already removed, not indexed "
            "(e.g. %s%s) — run with -v for the full list",
            len(stats.errors),
            sample,
            ", …" if len(stats.errors) > 3 else "",
        )
    return stats


def resolve_catalog_hosts(
    override: Optional[str] = None, *, prod: bool = False
) -> list:
    """Resolve which gps_health host(s) an archive-catalog write targets.

    Safe-by-default: production is an EXPLICIT opt-in, never a silent config
    default, so a dev test on a laptop can't accidentally write production.

    * ``override`` (``--catalog-host``, comma-separated allowed) → exactly those
      hosts (one-off, e.g. ``localhost`` or ``a.vedur.is,b.vedur.is``);
    * ``prod=True`` (``--catalog-prod``) → the ``[archive] catalog_hosts`` set
      from receivers.cfg (the identical-DB production set). Returns ``[]`` when
      that is unset — the caller MUST treat empty as an error (do not fall back
      to localhost, which would silently write dev instead of prod);
    * otherwise → ``[None]`` — the single default connection (database.cfg host,
      i.e. localhost on a laptop / localhost+mirror=pgdev on rek-d01).

    Returns a list of host strings (``None`` = the default connection).
    """
    if override:
        return [h.strip() for h in override.split(",") if h.strip()]
    if prod:
        try:
            from ..config.receivers_config import get_receivers_config

            return get_receivers_config().get_catalog_hosts()
        except Exception:  # noqa: BLE001
            return []
    return [None]


@contextmanager
def open_catalog_conns(
    *,
    prod: bool = True,
    override: Optional[str] = None,
    required: bool = True,
    log: logging.Logger = logger,
):
    """Open one gps_health connection per catalog host for a ROUTINE catalog write.

    The routine write paths (sync engine push-on-download and the scheduled
    archive-sync sweep) historically wrote a single default connection
    (localhost). That silently diverged the ``catalog_hosts`` set: only explicit
    ``--catalog-prod`` reindex operations ever fanned out, so the mirror
    (``pgdev``) fell ~10x behind the operational host (``rek-d01``). This helper
    resolves the identical-catalog set the same way :func:`resolve_catalog_hosts`
    does and opens a connection to every host, so the caller can upsert to all.

    Yields the list of open connections (host order == ``catalog_hosts`` order,
    so element 0 is the local/operational host). Semantics:

    * **Best-effort secondaries, mandatory primary.** If the FIRST host cannot be
      reached the error propagates (the operational catalog write must not be
      silently skipped). A later host that is unreachable is logged loudly (this
      is exactly the divergence the fan-out prevents) and dropped from the list,
      so a ``pgdev`` outage degrades to "rek-d01 only + a warning" instead of
      blocking every download.
    * **Dev fallback.** When no ``catalog_hosts`` are configured (a laptop),
      ``resolve_catalog_hosts`` returns ``[]``; we fall back to ``[None]`` — the
      single default connection — preserving the previous single-host behaviour.

    All connections are closed on exit.
    """
    from ..db.connection import get_connection

    hosts = resolve_catalog_hosts(override, prod=prod) or [None]
    explicit = override is not None
    conns: list = []
    try:
        for idx, host in enumerate(hosts):
            # For the resolved prod set, element 0 is the operational/local host:
            # reach it via the DEFAULT connection (host_override=None) so it uses
            # clean primary credentials and we do not open a second connection to
            # the same DB — connecting it by its own FQDN would trip the "neither
            # primary nor mirror_host" credential-fallback warning on every routine
            # write. The remaining hosts are mirrors, reached by explicit
            # host_override. An explicit ``override`` is always honoured as given.
            conn_host = host if explicit else (None if idx == 0 else host)
            label = host or "localhost"
            # Element 0 goes through the DEFAULT connection, which fans writes
            # out to ``mirror_host`` when one is configured. With a multi-host
            # catalog set that mirror IS one of the later elements, so the row
            # would be written to it TWICE (once implicitly, once explicitly) —
            # and a mirror leg that silently succeeds would mask the loud
            # "secondary unreachable" warning below. Pin element 0 to the
            # primary and let the explicit fan-out own every other host.
            # With a single-element set (a laptop, no ``catalog_hosts``) the
            # implicit mirror is the ONLY fan-out there is — leave it alone.
            single = len(hosts) > 1 and conn_host is None
            try:
                conns.append(
                    get_connection(host_override=conn_host, single_host=single)
                )
            except Exception as exc:  # noqa: BLE001
                if idx == 0:
                    # Primary (operational) host: a caller that can proceed
                    # without a DB (a pure dry-run) passes required=False and
                    # gets an empty set; otherwise the failure propagates.
                    if required:
                        raise
                    log.warning(
                        "no gps_health connection (%s) — proceeding without "
                        "catalog indexing",
                        exc,
                    )
                    break
                # Deliberately does NOT say "unreachable": this fires for ANY
                # connection failure, and an auth failure is by far the more
                # common one (an expired LDAP/domain password reads as a dead
                # host otherwise, sending the reader to check the network while
                # the port is wide open). Lead with the driver's own error.
                log.error(
                    "catalog fan-out: could not connect to secondary host %s "
                    "(auth or network — read the error below before assuming "
                    "the host is down) — routine writes will SKIP it and the "
                    "catalogs may DIVERGE: %s",
                    label,
                    exc,
                )
        yield conns
    finally:
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def reindex_files_multi(
    hosts: list,
    files: list[str],
    *,
    root: str,
    storage_location: str,
    dest_prefix: str,
    dry_run: bool = False,
    only_existing: bool = False,
    log: logging.Logger = logger,
) -> dict:
    """Reindex ``files`` into EVERY host in ``hosts`` (the identical-catalog set).

    Returns ``{host_label: ReindexStats | None}`` (None = that host errored).
    Idempotent, so a partial failure is safe to re-run. Callers should surface a
    per-host failure loudly — a catalog that wrote to one DB but not the other is
    exactly the divergence this fan-out exists to prevent.
    """
    from ..db.connection import get_connection

    results: dict = {}
    for host in hosts:
        label = host or "localhost"
        conn = None
        try:
            conn = get_connection(host_override=host)
            results[label] = reindex_files(
                conn,
                files,
                root=root,
                storage_location=storage_location,
                dest_prefix=dest_prefix,
                dry_run=dry_run,
                only_existing=only_existing,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("reindex to %s failed: %s", label, exc)
            results[label] = None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    return results


def preflight_catalog_hosts(
    hosts: list,
    *,
    log: logging.Logger = logger,
) -> dict:
    """Test-connect to every catalog host BEFORE a long reindex run.

    A ``--catalog-prod`` run reindexes EVERY ``[archive] catalog_hosts`` entry
    per batch. When one host is unreachable, :func:`reindex_files_multi` only
    surfaces it as a per-batch "catalogs may DIVERGE" warning — so a hours-long
    fix-headers run completes "successfully" while the catalogs silently drift
    (rek-d01 ↔ pgdev diverged ~12 days unnoticed). This preflight opens a real
    connection and runs ``SELECT 1`` against each host so the caller can refuse
    to start the run when any target is down, instead of discovering it batch by
    batch. Idempotent and read-only.

    Returns ``{host_label: None if reachable else error_string}`` in ``hosts``
    order (``None`` host → the ``"localhost"`` default connection).
    """
    from ..db.connection import get_connection

    results: dict = {}
    for host in hosts:
        label = host or "localhost"
        conn = None
        try:
            conn = get_connection(host_override=host)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            results[label] = None
        except Exception as exc:  # noqa: BLE001
            log.error("catalog preflight: %s UNREACHABLE — %s", label, exc)
            results[label] = str(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    return results


def _existing_sha(
    conn, storage_location: str, session_type: str, file_category: str, key: str
) -> Optional[str]:
    """Return the current content_sha256 for the catalog row, or None.

    Deliberately a plain cursor, NOT :func:`~receivers.db.tx.read_only_cursor`
    — the one read in this module that must not end its transaction. It is
    called from inside the per-file loop (see the ``_existing_sha`` call around
    line 133), which upserts catalog rows on the same connection and commits in
    batches. Rolling back here would discard the caller's pending upserts.

    The rule for the helper is "safe iff the read is the first statement on the
    connection", and here it is not. ``tests/test_transaction_audit.py`` carries
    this file in its allowlist so the audit stays green without hiding it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT content_sha256 FROM archive_catalog
               WHERE storage_location = %s AND session_type = %s
                 AND file_category = %s AND canonical_key = %s""",
            (storage_location, session_type, file_category, key),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# One-time archive-index backfill (unified file index Track B).
#
# Indexes files ALREADY on disk in the permanent archive into archive_catalog on
# every catalog host, computing BOTH hashes. Unlike ``reindex_files`` (a targeted
# fix for out-of-band edits), this is a bulk, resumable, pausable sweep of the
# archive tree for the ~39k-file historical backfill — run gradually from a host
# that has the archive mounted (e.g. the laptop's ananas NFS), writing to prod
# via the ``catalog_hosts`` fan-out.
# ---------------------------------------------------------------------------


@dataclass
class BackfillStats:
    """Outcome of an archive-index backfill run."""

    hashed: int = 0  # files newly hashed + upserted to >=1 host this run
    skipped_done: int = 0  # every target host already held both hashes
    skipped_parse: int = 0  # path is not a catalogable archive file
    writes: dict = field(default_factory=dict)  # {label: {"ok": int, "fail": int}}
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hashed": self.hashed,
            "skipped_done": self.skipped_done,
            "skipped_parse": self.skipped_parse,
            "writes": self.writes,
            "errors": self.errors,
        }


def _load_done_keys(
    conn, storage_location: str, *, require_compressed: bool = False
) -> set:
    """Return the set of ``(session_type, file_category, canonical_key)`` rows on
    this host that count as already-indexed — the resume skip-set.

    Default "done" = ``content_sha256`` present (the primary index hash). Most of
    a mature archive is already content-hashed from prior reindex/sync work, so
    this makes a full-archive sweep touch ONLY the genuinely-uncataloged files
    (read once, not re-read the whole 16 TB). ``require_compressed=True`` also
    demands ``compressed_sha256`` — for a deliberate later pass that fills the
    EPOS-md5 counterpart on already-content-hashed rows (re-reads the archive).

    Loaded once per run so the per-file "already indexed?" check is an in-memory
    lookup, not a network round-trip per file (one query vs tens of thousands
    against a remote prod DB).
    """
    done: set = set()
    predicate = "content_sha256 IS NOT NULL"
    if require_compressed:
        predicate += " AND compressed_sha256 IS NOT NULL"
    with read_only_cursor(conn) as cur:
        cur.execute(
            f"""SELECT session_type, file_category, canonical_key
               FROM archive_catalog
               WHERE storage_location = %s AND {predicate}""",
            (storage_location,),
        )
        for st, fc, k in cur.fetchall():
            done.add((st, fc, k))
    return done


def iter_archive_files(
    walk_dir: str,
    *,
    root: str,
    stations: Optional[set] = None,
    sessions: Optional[set] = None,
) -> Iterable[str]:
    """Yield archive file paths under ``walk_dir`` recursively (sorted).

    Prunes the walk to ``stations`` (upper-cased 4-char IDs) and/or ``sessions``
    at the STA / session directory level — so "index just these stations" does
    not stat the whole tree. Skips ``rinex_archive`` backup dirs. Identities are
    parsed relative to ``root`` (``walk_dir`` may be a subtree of it), so the
    prune depths are measured from ``root``: ``YYYY(1)/mon(2)/STA(3)/session(4)``.
    """
    for dirpath, dirs, names in os.walk(walk_dir):
        dirs.sort()
        # Prune hidden directories — most importantly NFS/NetApp ``.snapshot``
        # trees, which mirror the whole archive under two extra prefix dirs and
        # would otherwise be walked (and mis-catalogued) once per retained snap.
        # The archive layout has no legitimate dotdirs.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
        if stations is not None and depth == 2:
            dirs[:] = [d for d in dirs if d.upper() in stations]
        if sessions is not None and depth == 3:
            dirs[:] = [d for d in dirs if d in sessions]
        if f"{os.sep}rinex_archive{os.sep}" in dirpath + os.sep:
            continue
        for n in sorted(names):
            yield os.path.join(dirpath, n)


def backfill_archive_catalog(
    hosts: list,
    files: Iterable[str],
    *,
    root: str,
    storage_location: str,
    dest_prefix: str,
    limit: Optional[int] = None,
    dry_run: bool = False,
    require_compressed: bool = False,
    sleep_between: float = 0.0,
    progress_every: int = 500,
    progress_callback: Optional[Callable[[str], None]] = None,
    unparsable_callback: Optional[Callable[[str], None]] = None,
    log: logging.Logger = logger,
) -> BackfillStats:
    """Index already-on-disk archive files into ``archive_catalog`` on every host.

    Resumable + pausable: a file already counted as indexed on a host is skipped
    there — the catalog's own hash-completeness IS the cursor, no separate state
    file. "Indexed" = ``content_sha256`` present by default (the primary index
    hash), so a full-archive sweep touches ONLY the genuinely-uncataloged files
    instead of re-reading the whole archive for rows that were content-hashed by
    prior work. ``require_compressed=True`` also demands ``compressed_sha256`` —
    the deliberate later pass that fills the EPOS-md5 counterpart everywhere.
    ``limit`` caps the number of files newly hashed this run (the heavy decompress
    + sha256 work), so the archive is indexed in bounded batches with pauses.
    ``sleep_between`` throttles the read rate — a small pause after each file
    keeps a long-running sweep gentle on the NFS mount (pair with ``ionice``).

    Each file is hashed ONCE and fanned out to every host that needs it (rather
    than re-hashing per host) — ``content_sha256`` decompresses the file and is
    the expensive step. ``dest_prefix`` maps the local read ``root`` onto the
    canonical archive path, so laptop-written rows collide with the server's
    forward-catalog rows on the logical key (COALESCE upsert → idempotent).

    Note: ``file_date`` is written from the path parse (as ``reindex_files``
    does). It activates the ``verify.py`` local↔archive cross-check, which is
    inert here because the ``archive_verify`` scheduler job stays disabled for
    this rollout.

    Args:
        hosts: catalog hosts from :func:`resolve_catalog_hosts` (``None`` in the
            list = the default connection).
        files: iterable of local file paths under ``root`` (archive layout).
        root: the mount root the files sit under (e.g. ``/mnt_data/rawgpsdata``).
        storage_location: ``archive_catalog.storage_location`` (e.g.
            ``imo_archive``).
        dest_prefix: the canonical archive dest for ``file_path``.
        limit: stop after this many files are newly hashed (``None`` = no cap).
        dry_run: classify + count, do not hash or write.

    Returns:
        :class:`BackfillStats`.
    """
    from ..db.connection import get_connection

    stats = BackfillStats()
    dest_prefix = dest_prefix.rstrip("/")

    conns: dict = {}
    for host in hosts:
        label = host or "localhost"
        try:
            conns[label] = get_connection(host_override=host)
            stats.writes[label] = {"ok": 0, "fail": 0}
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"connect {label}: {exc}")
            log.error("backfill: cannot connect to %s: %s", label, exc)
    if not conns:
        stats.errors.append("no catalog hosts reachable")
        return stats

    try:
        done_keys = {
            label: _load_done_keys(
                conn, storage_location, require_compressed=require_compressed
            )
            for label, conn in conns.items()
        }

        scanned = 0
        verb = "would-index" if dry_run else "hashed"
        for f in files:
            if limit is not None and stats.hashed >= limit:
                break
            scanned += 1
            if scanned % progress_every == 0:
                msg = (
                    f"progress: {scanned} scanned — {stats.hashed} {verb}, "
                    f"{stats.skipped_done} already-indexed, "
                    f"{stats.skipped_parse} unparsable"
                )
                log.info("backfill %s", msg)
                if progress_callback is not None:
                    progress_callback(msg)
            parsed = parse_archive_path(f, root)
            if parsed is None:
                stats.skipped_parse += 1
                if unparsable_callback is not None:
                    unparsable_callback(f)
                continue
            key = canonical_key(os.path.basename(f))
            ident = (parsed.session_type, parsed.file_category, key)

            needing = [label for label in conns if ident not in done_keys[label]]
            if not needing:
                stats.skipped_done += 1
                continue

            if dry_run:
                stats.hashed += 1
                log.debug("backfill[DRY]: would index %s → %s", key, needing)
                continue

            try:
                csha = content_sha256(f)
                zsha = compressed_sha256(f)
            except CorruptArchiveFileError as exc:
                stats.errors.append(f"corrupt, not indexed: {f}: {exc}")
                log.error("backfill: corrupt file %s: %s", f, exc)
                continue
            except OSError as exc:
                stats.errors.append(f"could not read {f}: {exc}")
                continue

            archive_path = f"{dest_prefix}/{parsed.relative_path}"
            fsize = os.path.getsize(f)
            for label in needing:
                conn = conns[label]
                try:
                    upsert_catalog_row(
                        conn,
                        storage_location=storage_location,
                        station=parsed.station,
                        session_type=parsed.session_type,
                        file_category=parsed.file_category,
                        file_date=parsed.file_date,
                        file_hour=parsed.file_hour,
                        archive_path=archive_path,
                        filename=os.path.basename(f),
                        file_size=fsize,
                        content_sha256=csha,
                        compressed_sha256=zsha,
                    )
                    conn.commit()
                    stats.writes[label]["ok"] += 1
                    done_keys[label].add(ident)
                except Exception as exc:  # noqa: BLE001
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    stats.writes[label]["fail"] += 1
                    stats.errors.append(f"upsert {label} {key}: {exc}")
                    log.error(
                        "backfill: upsert to %s failed for %s: %s", label, key, exc
                    )

            stats.hashed += 1
            if sleep_between > 0:
                time.sleep(sleep_between)
    finally:
        for conn in conns.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return stats
