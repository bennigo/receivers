"""``receivers archive-sync`` — host-level batch delta sweep to the archive.

Reads ``sync.yaml``, computes each active target's watermark-bounded delta,
rsyncs raw files to the archive gateway (``gpsops@rawdata:~/gpsdata``), and
forward-indexes them into ``archive_catalog``. Dry-run-safe; the live push and
the legacy-cutover are operator actions. See design 1781867391.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.cli.archive_sync")


def _get_conn(host: Optional[str], required: bool):
    """Open a gps_health connection; tolerate absence only when not required."""
    from ..db.connection import optional_connection

    return optional_connection(host, required=required, log=logger)


def _parse_bound(value: str, *, is_start: bool) -> datetime:
    """Parse a --start/--end bound into a naive datetime.

    ``YYYY-MM-DD`` expands to the whole day (start=00:00, end=23:59:59) so a
    bare date selects a full calendar day; ``YYYY-MM-DD HH`` (and HH:MM[:SS])
    pins the hour for the 1Hz_1hr session.
    """
    value = value.strip()
    if len(value) == 10:  # YYYY-MM-DD
        d = datetime.strptime(value, "%Y-%m-%d")
        if is_start:
            return d
        return d.replace(hour=23, minute=59, second=59)
    for fmt in ("%Y-%m-%d %H", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"invalid --start/--end bound {value!r} — use 'YYYY-MM-DD' or 'YYYY-MM-DD HH'"
    )


def cmd_archive_sync(args: argparse.Namespace) -> int:
    from ..archive import ArchiveSync, load_sync_config

    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    if args.target:
        targets = [t for t in targets if t.name == args.target]
        if not targets:
            print(f"No sync target named {args.target!r} in config")
            return 1

    if not targets:
        print("No sync targets configured (sync.yaml absent or empty).")
        return 0

    if args.status:
        return _cmd_status(args, targets)

    # Selection mode: any of --station/--session/--start/--end switches from the
    # watermark sweep to a targeted explicit push (no watermark advance).
    selection_mode = bool(args.station or args.session or args.start or args.end)
    start_dt = _parse_bound(args.start, is_start=True) if args.start else None
    end_dt = _parse_bound(args.end, is_start=False) if args.end else None

    cutover_override = None
    if args.cutover:
        from ..archive.config import _parse_cutover

        try:
            cutover_override = _parse_cutover(args.cutover, "cli")
        except ValueError as exc:
            print(f"Invalid --cutover: {exc}")
            return 1

    # The catalog + watermark need a DB; a pure dry-run can run without one.
    # Fan the catalog out to every catalog host (rek-d01 + pgdev) so a manual
    # sweep keeps the mirror in sync too — writing only localhost is what let the
    # catalogs diverge. conns[0] is the operational host (watermark); an explicit
    # --host is honoured as a single target.
    from ..archive import open_catalog_conns

    results = []
    exit_code = 0
    with open_catalog_conns(
        prod=True, override=args.host, required=not args.dry_run
    ) as catalog_conns:
        conn = catalog_conns[0] if catalog_conns else None
        for target in targets:
            engine = ArchiveSync(
                target,
                conn=conn,
                catalog_conns=catalog_conns,
                dry_run=args.dry_run,
                dest_override=args.dest_override,
                force=args.force,
                cutover_override=cutover_override,
            )
            if selection_mode:
                result = engine.select(
                    station=args.station,
                    session=args.session,
                    start=start_dt,
                    end=end_dt,
                )
            else:
                result = engine.run()
            results.append(result)
            if not result.ok:
                exit_code = 1
            if not args.json:
                _print_result(result)

    if args.json:
        print(
            json.dumps(
                [_result_dict(r) for r in results], indent=2, default=_json_default
            )
        )
    return exit_code


def _cmd_status(args: argparse.Namespace, targets) -> int:
    """Print per-target sync freshness; non-zero exit if any target is alerting."""
    from datetime import datetime

    from ..archive.freshness import evaluate_all

    conn = _get_conn(args.host, required=True)
    try:
        statuses = evaluate_all(
            conn, targets, now=datetime.now(), max_age_minutes=args.max_age_minutes
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(
            json.dumps(
                [_status_dict(s) for s in statuses], indent=2, default=_json_default
            )
        )
    else:
        for s in statuses:
            icon = {"ok": "✅", "stale": "🔴", "never": "⚠️", "inactive": "⏸️"}.get(
                s.state, "?"
            )
            age = (
                f"{s.age_seconds / 60:.0f} min ago"
                if s.age_seconds is not None
                else "—"
            )
            last = f"{s.last_success:%Y-%m-%d %H:%M:%S}" if s.last_success else "never"
            print(f"{icon} {s.target}: {s.state}  (last success {last}, {age})")
    return 1 if any(s.is_alerting for s in statuses) else 0


def _status_dict(s) -> dict:
    return {
        "target": s.target,
        "state": s.state,
        "last_success": s.last_success,
        "age_seconds": s.age_seconds,
        "threshold_seconds": s.threshold_seconds,
    }


def _print_result(result) -> None:
    icon = "✅" if result.ok else "❌"
    tag = " [dry-run]" if result.dry_run else ""
    print(f"{icon} {result.target}{tag}: {result.message}")
    print(
        f"   floor={result.floor:%Y-%m-%d %H:%M:%S}  delta={result.delta_count}  "
        f"transferred={result.transferred}  cataloged={result.cataloged}"
    )
    for err in result.errors:
        print(f"   ⚠ {err}")


def _result_dict(result) -> dict:
    return {
        "target": result.target,
        "floor": result.floor,
        "delta_count": result.delta_count,
        "transferred": result.transferred,
        "cataloged": result.cataloged,
        "ok": result.ok,
        "dry_run": result.dry_run,
        "message": result.message,
        "errors": result.errors,
    }


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def cmd_archive_verify(args: argparse.Namespace) -> int:
    """Verify archived files against the catalog (local↔archive + read-back)."""
    from ..archive import load_sync_config, verify_archive_catalog

    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    target = None
    if targets:
        target = next(
            (t for t in targets if t.name == args.storage_location), targets[0]
        )
    # dest_prefix lets us map the stored archive path onto the local read mount.
    dest_prefix = args.dest_prefix or (target.dest if target else None)

    from ..utils.batch_parallel import resolve_workers

    workers = resolve_workers(
        getattr(args, "parallel", None), max(1, args.limit), logging.getLogger(__name__)
    )

    conn = _get_conn(args.host, required=True)
    try:
        stats = verify_archive_catalog(
            conn,
            storage_location=args.storage_location,
            read_root=args.read_root,
            dest_prefix=dest_prefix,
            limit=args.limit,
            reverify_after_days=args.reverify_after_days,
            workers=workers,
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps(stats.to_dict(), indent=2))
    else:
        icon = "❌" if (stats.mismatched or stats.local_divergent) else "✅"
        mode = "read-back" if stats.read_back else "cross-check only"
        print(f"{icon} archive verify ({mode}): {stats.checked} checked")
        print(
            f"   verified={stats.verified}  CORRUPT={stats.mismatched}  "
            f"local-divergent={stats.local_divergent}  missing={stats.missing}"
        )
        for f in stats.findings[:50]:
            print(f"   ⚠ {f}")
    # Non-zero exit on any real integrity problem.
    return 1 if (stats.mismatched or stats.local_divergent) else 0


def cmd_archive_audit(args: argparse.Namespace) -> int:
    """Audit the archive for junk + regen candidates; emit ready-made commands."""
    from ..archive.audit import audit_station_session
    from ..utils.batch_parallel import ProgressBoard, resolve_workers, run_chunks

    # Source root: like fix-headers — the dissemination source_root (the
    # read-only archive mount) unless overridden.
    source = getattr(args, "source_dir", None)
    if not source:
        try:
            from ..dissemination import load_dissemination_config

            for t in load_dissemination_config():
                if getattr(t, "tier", None) == "dissemination" and getattr(
                    t, "source_root", None
                ):
                    source = t.source_root
                    break
        except Exception:  # noqa: BLE001
            pass
    if not source:
        print("❌ no --source-dir and no dissemination source_root in sync.yaml")
        return 2
    root = Path(source).expanduser()
    if not root.is_dir():
        print(f"❌ source dir not found: {root}")
        return 2

    stations = [s.upper() for s in args.stations]
    years = {int(y) for y in args.years.split(",")} if args.years else None
    workers = resolve_workers(getattr(args, "parallel", None), len(stations), logger)

    print(
        f"Archive audit: {' '.join(stations)} {args.session} under {root}"
        + (f" years={sorted(years)}" if years else "")
        + (" [deep]" if args.deep else "")
        + (" [check-version]" if args.check_version else "")
    )

    board = ProgressBoard(interval=30)
    handles = {sta: board.handle(sta) for sta in stations}

    def _audit_one(sta):
        h = handles[sta]
        h.start()
        try:
            rep = audit_station_session(
                root,
                sta,
                args.session,
                years=years,
                deep=args.deep,
                check_version=args.check_version,
                check_missing=not args.no_missing,
                check_identity=getattr(args, "identity", False),
                progress=h,
            )
            h.finish(ok=True)
            return rep
        except BaseException:
            h.finish(ok=False)
            raise

    with board:
        outcomes = run_chunks(
            stations, _audit_one, workers=workers, logger=logger, load_gate=False
        )

    reports = [oc.value for oc in outcomes if oc.ok and oc.value is not None]
    failed = [oc for oc in outcomes if not oc.ok]
    for oc in failed:
        print(f"❌ audit failed for {oc.chunk}: {oc.error}")

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "station": r.station,
                        "session": r.session,
                        "scanned": r.scanned,
                        "clean": r.clean,
                        "counts": r.counts(),
                        "findings": [
                            {
                                "path": f.rel_path,
                                "issue": f.issue,
                                "detail": f.detail,
                                "size": f.size,
                                "date": (
                                    f.file_date.isoformat() if f.file_date else None
                                ),
                                "junk": f.junk,
                                "regen": f.regen,
                            }
                            for f in r.findings
                        ],
                    }
                    for r in reports
                ],
                indent=2,
            )
        )
        return 1 if any(r.findings for r in reports) or failed else 0

    any_findings = False
    for r in reports:
        counts = r.counts()
        icon = "✅" if not r.findings else "⚠️ "
        print(
            f"\n{icon} {r.station}: {r.scanned} scanned, {r.clean} clean"
            + ("" if not counts else f", issues: {counts}")
        )
        for f in r.findings[:40]:
            print(f"    [{f.issue}] {f.rel_path} — {f.detail}")
        if len(r.findings) > 40:
            print(f"    … (+{len(r.findings) - 40} more — use --json for all)")
        any_findings = any_findings or bool(r.findings)

    # Ready-to-run remediation commands.
    junk = [(f.rel_path, f.size) for r in reports for f in r.findings if f.junk]
    if junk:
        cap = max(sz for _, sz in junk) + 1
        print("\n🗑  junk removal (dry-run as shown; add --yes to delete):")
        print(f"  receivers archive-rm --catalog-prod --max-size {cap} \\")
        print("    --file " + " \\\n           ".join(p for p, _ in junk))
    for r in reports:
        dates = r.regen_dates
        if dates:
            dlist = ",".join(d.strftime("%Y%m%d") for d in dates)
            print(f"\n🔁 regenerate {r.station} ({len(dates)} date(s)):")
            print(
                f"  receivers rinex {r.station} --session {r.session} "
                f"--from-archive --backup-old --push --catalog-prod "
                f"--force --dates {dlist}"
            )
    gzipz = [f for r in reports for f in r.findings if f.issue == "bad-magic"]
    if gzipz:
        print(
            f"\n📦 {len(gzipz)} gzip-.Z file(s): content OK, wrong compression — "
            "recompress via deployment/scripts/fix_gzip_z_to_lzw.sh (rek-d01) "
            "or re-push from a recompressed staging tree; do NOT delete."
        )
    strays = [(r.station, f) for r in reports for f in r.findings if f.issue == "stray"]
    if strays:
        stray_stations = sorted({sta for sta, _ in strays})
        print(
            f"\n📍 {len(strays)} stray file(s): position matches a DIFFERENT "
            "station. Confirm the true station (authoritative teqc +meta) and "
            "relocate — never delete or regen-in-place:"
        )
        for sta in stray_stations:
            print(
                f"  receivers archive-sort {sta} --session {args.session} "
                "--verify-station   # dry-run; add --apply to move"
            )
    stacked = [f for r in reports for f in r.findings if f.issue == "stacked"]
    if stacked:
        print(
            f"\n🧱 {len(stacked)} stacked file(s): multiple RINEX documents in one "
            ".Z — included in the regenerate command(s) above (a clean re-rinex "
            "collapses them to a single document)."
        )
    if not any_findings and not failed:
        print("\n✅ archive clean — nothing to do")
    return 1 if any_findings or failed else 0


def cmd_archive_reindex(args: argparse.Namespace) -> int:
    """Re-hash files in a staging mirror and refresh their archive_catalog rows.

    For files modified out-of-band (e.g. ``rinex --fix-headers --push``): the
    archive bytes changed but the catalog still holds the pre-edit
    content_sha256. Point ``--dir`` at the staging mirror that was pushed (its
    bytes are identical to what landed on the archive) and this upserts the
    correct hash. Laptop-friendly — no archive mount needed.
    """
    import os

    from ..archive import load_sync_config, reindex_files_multi, resolve_catalog_hosts

    root = str(Path(args.dir).expanduser())
    if not os.path.isdir(root):
        print(f"❌ --dir not a directory: {root}")
        return 2

    # Resolve the archive target for storage_location + dest prefix.
    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    target = next(
        (t for t in targets if t.name == args.storage_location),
        next((t for t in targets if getattr(t, "tier", None) == "archive"), None),
    )
    if target is None:
        print("❌ no archive target in sync.yaml (need storage_location + dest)")
        return 2
    dest_prefix = args.dest_prefix or target.dest

    # Collect files under the mirror (skip *_archive backup dirs).
    files: list[str] = []
    for dirpath, _dirs, names in os.walk(root):
        if f"{os.sep}rinex_archive{os.sep}" in dirpath + os.sep:
            continue
        for n in names:
            files.append(os.path.join(dirpath, n))
    if not files:
        print(f"⚠️  no files under {root}")
        return 0

    hosts = resolve_catalog_hosts(args.catalog_host, prod=args.catalog_prod)
    if args.catalog_prod and not hosts:
        print(
            "⚠️  --catalog-prod but [archive] catalog_hosts is unset in "
            "receivers.cfg — refusing (would silently hit dev). Set "
            "catalog_hosts = rek-d01.vedur.is, pgdev.vedur.is."
        )
        return 2

    results = reindex_files_multi(
        hosts,
        files,
        root=root,
        storage_location=target.name,
        dest_prefix=dest_prefix,
        dry_run=args.dry_run,
        only_existing=args.only_existing,
    )

    if args.json:
        print(
            json.dumps(
                {h: (s.to_dict() if s else None) for h, s in results.items()}, indent=2
            )
        )
    else:
        verb = "would reindex" if args.dry_run else "reindexed"
        for label, stats in results.items():
            if stats is None:
                print(f"⚠️  reindex FAILED on {label} — catalogs may DIVERGE; re-run.")
                continue
            print(
                f"↻ {verb} archive_catalog ({target.name} on {label}): "
                f"{stats.updated} updated, {stats.inserted} inserted, "
                f"{stats.unchanged} unchanged"
                + (
                    f", {stats.skipped_new} skipped (no prior row)"
                    if stats.skipped_new
                    else ""
                )
                + (f", {stats.skipped} unparsable" if stats.skipped else "")
            )
            for msg in stats.errors[:50]:
                print(f"   ⚠ {msg}")
    _any_err = any(s is None or s.errors for s in results.values())
    return 1 if _any_err else 0


def cmd_archive_index_backfill(args: argparse.Namespace) -> int:
    """One-time index of already-on-disk archive files into archive_catalog.

    Walks ``--dir`` (the archive tree, e.g. the ananas read mount), hashes each
    file (content + compressed sha256), and upserts its catalog row on every
    catalog host. Resumable + pausable: ``--limit`` caps files hashed per run and
    a re-run skips files already fully hashed. Built for the ~39k-file historical
    ``imo_archive`` backfill, run gradually from a host that has the archive
    mounted, writing to prod via ``[archive] catalog_hosts``.
    """
    import os

    from ..archive import (
        backfill_archive_catalog,
        iter_archive_files,
        load_sync_config,
        resolve_catalog_hosts,
    )

    walk_dir = str(Path(args.dir).expanduser())
    if not os.path.isdir(walk_dir):
        print(f"❌ --dir not a directory: {walk_dir}")
        return 2
    # `root` is the archive-layout root that identities are parsed relative to
    # (YYYY/mon/STA/session/cat/FILE). `--dir` is the walk scope, which may be a
    # SUBTREE of root (e.g. --root /mnt_data/rawgpsdata --dir …/2026 to index one
    # year at a time). Defaults to --dir for the whole-mount case.
    root = str(Path(args.root).expanduser()) if args.root else walk_dir
    if os.path.relpath(walk_dir, root).startswith(".."):
        print(f"❌ --dir {walk_dir} must be inside --root {root}")
        return 2

    # Resolve dest_prefix (canonical archive path) from the sync target unless given.
    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    target = next(
        (t for t in targets if t.name == args.storage_location),
        next((t for t in targets if getattr(t, "tier", None) == "archive"), None),
    )
    dest_prefix = args.dest_prefix or (target.dest if target else None)
    if not dest_prefix:
        print(
            "❌ no --dest-prefix and no archive target in sync.yaml — need one to "
            "map local paths onto the canonical archive file_path"
        )
        return 2

    hosts = resolve_catalog_hosts(args.catalog_host, prod=args.catalog_prod)
    if args.catalog_prod and not hosts:
        print(
            "⚠️  --catalog-prod but [archive] catalog_hosts is unset in "
            "receivers.cfg — refusing (would silently hit dev). Set "
            "catalog_hosts = rek-d01.vedur.is, pgdev.vedur.is."
        )
        return 2

    # Optional station/session scoping — prunes the walk at the STA / session
    # directory level so "index just these stations" doesn't stat the whole tree.
    station_set = {s.upper() for s in args.station} if args.station else None
    session_set = set(args.session) if args.session else None
    files = iter_archive_files(
        walk_dir, root=root, stations=station_set, sessions=session_set
    )

    # Periodic progress to stderr (keeps stdout clean for --json).
    def _progress(msg: str) -> None:
        print(f"  … {msg}", file=sys.stderr, flush=True)

    # Optionally record paths that don't fit the archive layout (misfiled / stray
    # files) to a manifest for later manual triage (archive-sort / archive-rm).
    unparsable_fh = (
        open(args.report_unparsable, "w") if args.report_unparsable else None
    )
    unparsable_cb = (
        (lambda p: unparsable_fh.write(p + "\n")) if unparsable_fh is not None else None
    )
    try:
        stats = backfill_archive_catalog(
            hosts,
            files,
            root=root,
            storage_location=args.storage_location,
            dest_prefix=dest_prefix,
            limit=args.limit,
            dry_run=args.dry_run,
            require_compressed=args.refill_compressed,
            sleep_between=args.sleep,
            progress_every=args.progress_every,
            progress_callback=_progress,
            unparsable_callback=unparsable_cb,
        )
    finally:
        if unparsable_fh is not None:
            unparsable_fh.close()

    if args.json:
        print(json.dumps(stats.to_dict(), indent=2))
    else:
        verb = "would index" if args.dry_run else "indexed"
        print(
            f"📇 archive-index-backfill ({args.storage_location}): "
            f"{stats.hashed} {verb}, {stats.skipped_done} already-done, "
            f"{stats.skipped_parse} unparsable"
        )
        for label, w in stats.writes.items():
            note = (
                f" ({w['fail']} FAILED — catalogs may DIVERGE, re-run)"
                if w["fail"]
                else ""
            )
            print(f"   • {label}: {w['ok']} written{note}")
        if args.limit and stats.hashed >= args.limit:
            print(
                f"   ⏸ hit --limit {args.limit}; re-run to continue "
                "(already-done files are skipped)."
            )
        for msg in stats.errors[:50]:
            print(f"   ⚠ {msg}")
    _any_fail = bool(stats.errors) or any(w["fail"] for w in stats.writes.values())
    return 1 if _any_fail else 0


def cmd_archive_rm(args: argparse.Namespace) -> int:
    """Guarded deletion of specific files from the long-term archive.

    Dry-run by default; only 0-byte files are eligible unless --allow-nonempty.
    Built so a wrong invocation is safe (server-side empty re-check, argv-boundary
    paths, strict layout validation) — so nobody hand-runs rm on rawdata.
    """
    from ..archive import (
        load_sync_config,
        remove_archive_files,
        remove_catalog_rows,
    )

    # Resolve the archive gateway (user@host) + root from the sync target.
    config_path = Path(args.config) if args.config else None
    target = next(
        (
            t
            for t in load_sync_config(config_path)
            if getattr(t, "tier", None) == "archive"
        ),
        None,
    )
    if target is None or not target.host:
        print("❌ no remote archive tier target in sync.yaml — nothing to delete")
        return 2
    ssh_target = f"{target.user}@{target.host}"
    dest_root = target.dest

    execute = bool(args.yes)
    max_size = max(0, int(args.max_size))

    # Loud, explicit banner — this is a production deletion path.
    print("⚠️  ARCHIVE DELETION" + ("" if execute else " (DRY-RUN — nothing removed)"))
    print(f"   gateway: {ssh_target}:{dest_root}")
    print(
        f"   guard:   size ≤ {max_size} bytes"
        + (" (empty only)" if max_size == 0 else "")
    )
    if execute and max_size > 0:
        print(
            f"   🛑 --yes with --max-size {max_size}: deleting files up to "
            f"{max_size} bytes. Verify EACH path manually first."
        )
    print(f"   targets ({len(args.file)}):")
    for rel in args.file:
        print(f"      {rel}")

    res = remove_archive_files(
        list(args.file),
        ssh_target=ssh_target,
        dest_root=dest_root,
        max_size=max_size,
        execute=execute,
        prune_empty_dirs=not getattr(args, "keep_empty_dirs", False),
    )

    print()
    for rel in res.invalid:
        print(f"   ❌ REFUSED (invalid/unsafe path): {rel}")
    for rel, sz in res.skipped_toobig:
        print(f"   ⏭️  SKIP over cap ({sz} bytes > {max_size} — not deleted): {rel}")
    for rel in res.missing:
        print(f"   ·  missing (already gone): {rel}")
    for rel in res.not_file:
        print(f"   ⏭️  SKIP not-a-regular-file: {rel}")
    for rel, sz in res.would_delete:
        print(f"   🅳  would delete ({sz} bytes): {rel}")
    for rel, sz in res.deleted:
        print(f"   ✅ DELETED ({sz} bytes): {rel}")
    for rel, sz in res.failed:
        print(f"   ❌ FAILED to delete: {rel}")
    for d in res.dirs_would_remove:
        print(f"   🗀  would remove emptied dir: {d}")
    for d in res.dirs_removed:
        print(f"   🗀  removed emptied dir: {d}")

    # Catalog consistency: after real deletes, drop their catalog rows on EVERY
    # catalog host (the identical-DB set) so no integrity-verify later flags them
    # missing (file first, then rows).
    if execute and res.deleted:
        from ..archive import resolve_catalog_hosts
        from ..db.connection import get_connection

        _prune_hosts = resolve_catalog_hosts(args.catalog_host, prod=args.catalog_prod)
        if args.catalog_prod and not _prune_hosts:
            print(
                "   ⚠️  --catalog-prod but no [archive] catalog_hosts — catalog "
                "rows NOT pruned (would hit dev). Set catalog_hosts."
            )
            _prune_hosts = []
        for host in _prune_hosts:
            label = host or "localhost"
            conn = None
            try:
                conn = get_connection(host_override=host)
                n = remove_catalog_rows(
                    conn, target.name, [rel for rel, _ in res.deleted]
                )
                print(f"   ↻ removed {n} archive_catalog row(s) on {label}")
            except Exception as exc:  # noqa: BLE001
                print(f"   ⚠️  catalog prune FAILED on {label}: {exc}")
            finally:
                if conn is not None:
                    conn.close()

    if not execute and res.would_delete:
        print(
            "\n   → re-run with --yes to actually delete "
            "(catalog rows are pruned on all [archive] catalog_hosts)."
        )
    if res.skipped_toobig and max_size == 0:
        print(
            "\n   → some files are non-empty; if they are known-bad, re-run "
            "with --max-size <bytes> (bounded) after verifying them."
        )
    return 0 if res.ok else 1


def create_archive_rm_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-rm",
        help="Guarded deletion of specific empty/bad files from the archive",
        description=(
            "Delete named files from the long-term archive via the rawdata "
            "gateway — so nobody hand-runs rm on the server. Dry-run by default; "
            "ONLY 0-byte files are eligible unless --allow-nonempty. Paths are "
            "validated to the archive layout and passed to the remote shell as "
            "argv (never interpolated), and emptiness is re-checked server-side "
            "at delete time."
        ),
    )
    parser.add_argument(
        "--file",
        action="extend",
        nargs="+",
        required=True,
        metavar="REL",
        help="Archive-relative path(s) to delete "
        "(e.g. 2023/aug/RHOF/15s_24hr/rinex/RHOF2400.23D.Z). Repeatable and "
        "multi-valued. Explicit only — no globs, directories or recursion.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete (default is dry-run). Even with --yes, only files "
        "within --max-size are removed.",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=0,
        metavar="BYTES",
        help="Delete only files with size ≤ BYTES (server-side re-check). "
        "Default 0 = empty only. Raise it (bounded) to remove known-tiny broken "
        "files, e.g. --max-size 8 for 3-byte truncated RINEX.",
    )
    parser.add_argument(
        "--keep-empty-dirs",
        action="store_true",
        help="Leave behind any directory this run empties. By DEFAULT an "
        "emptied directory is removed with rmdir (which refuses a non-empty "
        "one, so it can never destroy data) — otherwise clearing e.g. a "
        "station's rinex_bak leaves dozens of empty dirs to sweep by hand on "
        "the gateway, which is what this verb exists to avoid. Parent dirs are "
        "never climbed; only the directory the deleted files were in.",
    )
    parser.add_argument(
        "--catalog-host",
        help="Explicit gps_health host(s), comma-separated, for "
        "catalog-row pruning (default: database.cfg). This is the CATALOG DB, not "
        "the delete target (that is the rawdata gateway from sync.yaml).",
    )
    parser.add_argument(
        "--catalog-prod",
        action="store_true",
        help="Prune catalog rows on the PRODUCTION set ([archive] catalog_hosts). "
        "Explicit opt-in; default prunes the database.cfg host only.",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.set_defaults(func=cmd_archive_rm)
    return parser


def create_archive_reindex_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-reindex",
        help="Refresh archive_catalog content_sha256 for out-of-band file edits",
        description=(
            "Re-hash the files in a staging mirror (the --work-dir that "
            "`rinex --fix-headers --push` pushed from) and upsert their "
            "archive_catalog rows, so the catalog matches the edited archive "
            "bytes and the integrity verify stops flagging them. Runs from a "
            "laptop; writes ALL [archive] catalog_hosts (e.g. pgdev + rek-d01) "
            "so the catalogs stay identical (--catalog-host overrides)."
        ),
    )
    parser.add_argument(
        "--dir",
        required=True,
        help="Staging mirror root (archive layout YYYY/mon/STA/session/cat/FILE), "
        "e.g. ~/tmp/rinex_fixes",
    )
    parser.add_argument(
        "--storage-location",
        default="imo_archive",
        help="archive_catalog.storage_location to write (default: imo_archive)",
    )
    parser.add_argument(
        "--dest-prefix",
        help="Archive dest prefix for file_path (default: target.dest from sync.yaml)",
    )
    parser.add_argument(
        "--catalog-host",
        help="Explicit gps_health host(s) to write, comma-separated (e.g. "
        "localhost for a dev test). Default (no flag): database.cfg host.",
    )
    parser.add_argument(
        "--catalog-prod",
        action="store_true",
        help="Write the PRODUCTION catalog set ([archive] catalog_hosts, e.g. "
        "rek-d01 + pgdev). Explicit opt-in so a dev run stays local by default.",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--only-existing",
        action="store_true",
        help="Only repair rows that already exist (skip inserts) — fix known-stale "
        "sha256 without expanding catalog coverage to previously-uncataloged files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify + report without writing catalog rows",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_reindex)
    return parser


def create_archive_index_backfill_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-index-backfill",
        help="One-time index of already-on-disk archive files into archive_catalog",
        description=(
            "Walk an archive tree (e.g. the ananas read mount) and upsert an "
            "archive_catalog row for every file, hashing BOTH content_sha256 and "
            "compressed_sha256. Resumable + pausable: --limit caps files hashed "
            "per run and a re-run skips files already fully hashed, so the "
            "~39k-file historical archive can be indexed in bounded batches with "
            "pauses. Run from a host that has the archive mounted; --catalog-prod "
            "writes the [archive] catalog_hosts set (rek-d01 + pgdev) so laptop "
            "rows coalesce with the server's forward-catalog rows."
        ),
    )
    parser.add_argument(
        "--dir",
        required=True,
        help="Directory to walk. May be the whole archive mount or a SUBTREE of "
        "--root (e.g. --root /mnt_data/rawgpsdata --dir …/2020 to index one year "
        "at a time). Files are identified by their path relative to --root.",
    )
    parser.add_argument(
        "--root",
        help="Archive-layout root that identities are parsed relative to "
        "(YYYY/mon/STA/session/cat/FILE), e.g. /mnt_data/rawgpsdata. Defaults to "
        "--dir (whole-mount case). Set this when --dir is a subtree.",
    )
    parser.add_argument(
        "--station",
        nargs="+",
        metavar="SID",
        help="Only index these station(s) (4-char IDs, case-insensitive) — prunes "
        "the walk to their dirs. e.g. --station RHOF ELEY to index two stations "
        "first. Combine with --session to narrow further.",
    )
    parser.add_argument(
        "--session",
        nargs="+",
        metavar="SESSION",
        help="Only index these session type(s), e.g. --session 15s_24hr. Prunes the "
        "walk to matching session dirs.",
    )
    parser.add_argument(
        "--storage-location",
        default="imo_archive",
        help="archive_catalog.storage_location to write (default: imo_archive)",
    )
    parser.add_argument(
        "--dest-prefix",
        help="Canonical archive dest prefix for file_path (default: target.dest "
        "from sync.yaml). Maps the local read mount onto the archive path so "
        "rows coalesce with server-written rows on the logical key.",
    )
    parser.add_argument(
        "--catalog-host",
        help="Explicit gps_health host(s) to write, comma-separated (e.g. "
        "localhost for a dev test). Default (no flag): database.cfg host.",
    )
    parser.add_argument(
        "--catalog-prod",
        action="store_true",
        help="Write the PRODUCTION catalog set ([archive] catalog_hosts, e.g. "
        "rek-d01 + pgdev). Explicit opt-in so a dev run stays local by default.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N files are newly hashed this run (bounded/pausable). "
        "Re-run to continue — already-indexed files are skipped.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Pause this long after each file, to keep a long sweep gentle on the "
        "NFS mount (default: 0). Pair with `ionice -c3 nice -n19` when running.",
    )
    parser.add_argument(
        "--refill-compressed",
        action="store_true",
        help="Treat a row as done only when BOTH hashes are present, so this pass "
        "RE-READS content-hashed rows to add the compressed_sha256 (EPOS-md5) "
        "counterpart. Heavy (re-reads the archive) — leave off for the primary "
        "index pass, which only touches genuinely-uncataloged files.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500,
        metavar="N",
        help="Log a progress line every N files scanned (default: 500) — so a long "
        "walk reports periodically instead of only at the end.",
    )
    parser.add_argument(
        "--report-unparsable",
        metavar="FILE",
        help="Write each file whose path does not fit the archive layout "
        "(YYYY/mon/STA/session/cat) to FILE — a manifest of misfiled/stray files "
        "for later manual triage (archive-sort to relocate, archive-rm to delete).",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be indexed without hashing or writing",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_index_backfill)
    return parser


def create_archive_verify_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-verify",
        help="Verify archived files against the catalog (re-hash + local compare)",
        description=(
            "Re-hash files on the long-term archive and compare to the stored "
            "content_sha256 (read-back, needs --read-root = the archive mount), and "
            "cross-check local file_tracking hashes vs the catalog. Detects "
            "archive bit-rot / partial transfers and local↔archive divergence."
        ),
    )
    parser.add_argument(
        "--storage-location",
        default="imo_archive",
        help="archive_catalog.storage_location to verify (default: imo_archive); "
        "also selects the sync target whose dest maps to --read-root",
    )
    parser.add_argument(
        "--read-root",
        help="Local mount of the archive for read-back re-hashing "
        "(rek-d01: /mnt/rawgpsdata). Omit for the DB-only cross-check.",
    )
    parser.add_argument(
        "--dest-prefix",
        help="Archive dest prefix stored in file_path (default: target.dest from "
        "sync.yaml), swapped for --read-root to locate each file",
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Max catalog rows per run (default: 500)"
    )
    parser.add_argument(
        "--reverify-after-days",
        type=int,
        help="Re-verify rows whose last_verified_at is older than N days "
        "(default: only never-verified rows)",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--host", help="gps_health host override (default: from config)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.add_argument(
        "--parallel",
        nargs="?",
        const="auto",
        default=None,
        metavar="N",
        help="Pre-hash the archive files (the expensive read-back step) on a "
        "thread pool. --parallel alone sizes from free cores minus current "
        "loadavg; --parallel N forces N workers. DB access stays serial.",
    )
    parser.set_defaults(func=cmd_archive_verify)
    return parser


def create_archive_audit_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-audit",
        help="Audit archive RINEX for junk + regen candidates (emits fix commands)",
        description=(
            "Walk a station/session's rinex dirs on the archive mount and flag "
            "convention-breaking names (.o.Z, bare .d, lowercase .d.Z), wrong "
            ".Z magic (gzip-as-.Z), unreadable files (--deep), RINEX 2 products "
            "(--check-version) and raw-without-rinex dates. Emits ready-to-run "
            "archive-rm and 'rinex --dates --force' commands, so campaign state "
            "is reconstructible from the archive on ANY host — no local "
            "staging-tree knowledge needed. Read-only."
        ),
    )
    parser.add_argument("stations", nargs="+", metavar="STATION")
    parser.add_argument("--session", default="15s_24hr")
    parser.add_argument(
        "--source-dir",
        help="Archive mount to scan (default: dissemination source_root from "
        "sync.yaml, e.g. /mnt_data/rawgpsdata)",
    )
    parser.add_argument("--years", metavar="Y1,Y2", help="Restrict to these year dirs")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also test full decompression of every product (slow over NFS)",
    )
    parser.add_argument(
        "--check-version",
        action="store_true",
        help="Read each product's CRINEX head and flag RINEX 2 products as "
        "regen candidates (streams file heads — slower than name/magic-only)",
    )
    parser.add_argument(
        "--identity",
        action="store_true",
        help="Header-probe every valid product for a stray position (closest to "
        "a DIFFERENT station → issue 'stray', fix via archive-sort) and for "
        "stacking (multiple concatenated RINEX docs → issue 'stacked', fix via a "
        "clean re-rinex). Report-only; decompresses each file (slower over NFS).",
    )
    parser.add_argument(
        "--no-missing",
        action="store_true",
        help="Skip the raw-without-rinex check",
    )
    parser.add_argument(
        "--parallel",
        nargs="?",
        const="auto",
        default=None,
        metavar="N",
        help="Audit stations in parallel (auto = load-aware sizing)",
    )
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_archive_audit)
    return parser


def create_archive_sync_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-sync",
        help="Batch delta sync of raw files to the long-term archive gateway",
        description=(
            "Push raw files newer than each target's watermark to the archive "
            "(rawdata/ananas) and index them in archive_catalog. Reads sync.yaml."
        ),
    )
    parser.add_argument(
        "--target", help="Only run this target (default: all active targets)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview: rsync --dry-run, no catalog writes, no watermark advance",
    )
    parser.add_argument(
        "--dest-override",
        help="Override the remote dest path (e.g. ~/gpsdata_staging for pre-stage)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if the target is inactive (for the pre-stage verify before "
        "the cutover). The scheduled :45 job is unaffected — it only runs active targets.",
    )
    parser.add_argument(
        "--cutover",
        help="Override the target's cutover (ISO ts, e.g. 2026-06-18T00:00:00) for "
        "this run — e.g. a pre-stage verify with a recent cutover so there is a "
        "real delta. Does not change sync.yaml.",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--host", help="gps_health host override (default: from config)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show per-target sync freshness instead of syncing "
        "(non-zero exit if any target is stale/never)",
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=120,
        help="Freshness threshold for --status (default: 120)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.add_argument(
        "--station",
        help="Selection mode: restrict to this station (4-char SID). "
        "Skips the watermark sweep and pushes just the matching files.",
    )
    parser.add_argument(
        "--session",
        help="Selection mode: restrict to this session dir "
        "(e.g. 15s_24hr, 1Hz_1hr, status_1hr).",
    )
    parser.add_argument(
        "--start",
        help="Selection mode: window start — 'YYYY-MM-DD' or 'YYYY-MM-DD HH'.",
    )
    parser.add_argument(
        "--end",
        help="Selection mode: window end — 'YYYY-MM-DD' or 'YYYY-MM-DD HH'.",
    )
    parser.set_defaults(func=cmd_archive_sync)
    return parser


def _sort_progress_renderer(total: int):
    """Inline progress line for the decode pass: count, rate, ETA, hits.

    TTY: a single self-overwriting line. Non-TTY (logs, pipes): one plain
    line every 250 files so long runs still show a heartbeat.
    """
    import sys
    import time

    t0 = time.monotonic()
    is_tty = sys.stdout.isatty()

    def render(done: int, n: int, n_plans: int) -> None:
        elapsed = time.monotonic() - t0
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (n - done) / rate if rate > 0 else 0.0
        msg = (
            f"   ⏳ {done}/{n} ({100 * done // max(n, 1)}%) — "
            f"{rate:.1f} file/s, ETA {int(eta) // 60}:{int(eta) % 60:02d} — "
            f"remediation hits: {n_plans}"
        )
        if is_tty:
            print(f"\r{msg}", end="", flush=True)
            if done == n:
                print()
        elif done % 250 == 0 or done == n:
            print(msg, flush=True)

    return render


def _print_fix_commands(plans, skips, persisted_dirs) -> None:
    """End-of-scan contract: the ready-to-run FIX command per finding class.

    Two-command workflow (bgo): command 1 scans + records in the corrections
    repo and ENDS with exactly what to run; command 2 is that fix.
    """
    lines = []
    if plans and persisted_dirs:
        for d in persisted_dirs:
            plan = Path(d) / "plan.tsv"
            if plan.is_file():
                lines.append(f"receivers archive-sort --apply-plan {plan} --yes")
    stubs = [sk.rel for sk in skips if sk.reason == "stub"]
    if stubs:
        head = " \\\n    --file ".join([""] + stubs[:40]).lstrip()
        lines.append(
            f"receivers archive-rm --max-size 4096 --yes {head}"
            + (f"\n  # ... +{len(stubs) - 40} more stubs" if len(stubs) > 40 else "")
        )
    eyes = [
        sk
        for sk in skips
        if sk.reason in ("unknown-station", "path-name-mismatch", "unparseable-name")
    ]
    if not lines and not eyes:
        return
    print("\n→ FIX (run when reviewed):")
    for cmd in lines:
        print(f"  {cmd}")
    if eyes:
        print(f"  # needs eyes ({len(eyes)}):")
        for sk in eyes[:10]:
            print(f"  #   {sk.rel}  [{sk.reason}] {sk.detail[:70]}")
        if len(eyes) > 10:
            print(f"  #   ... and {len(eyes) - 10} more (see report.tsv)")


def _record_plan_applied(plan_path: Path, res) -> None:
    """When an executed plan lives inside gps-tos-corrections, stamp the batch
    dir with an applied-marker and commit+push — the fix and its history are
    the same object. Best-effort."""
    import subprocess
    from datetime import datetime, timezone

    from ..config.receivers_config import ReceiversConfig

    repo = ReceiversConfig().get_tos_corrections_repo() or str(
        Path.home() / "git" / "gps-tos-corrections"
    )
    repo_path = Path(repo)
    try:
        rel_plan = plan_path.resolve().relative_to(repo_path.resolve())
    except (ValueError, OSError):
        return  # plan lives elsewhere — nothing to track
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker = plan_path.parent / "applied.txt"
    lines = [f"applied: {stamp}", f"moved: {len(res.moved)}"]
    for src, dst in res.moved:
        lines.append(f"MOVED\t{src}\t{dst}")
    for src, dst in res.dst_exists:
        lines.append(f"SKIPPED_EXISTS\t{src}\t{dst}")
    for src, dst in res.failed:
        lines.append(f"FAILED\t{src}\t{dst}")
    marker.write_text("\n".join(lines) + "\n")
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "add", str(rel_plan.parent)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "commit",
                "-q",
                "-m",
                f"raw-remediation applied: {rel_plan.parent} "
                f"({len(res.moved)} moved)",
            ],
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "push", "-q"],
            capture_output=True,
            timeout=60,
        )
        print(f"   📌 applied-marker committed + pushed ({rel_plan.parent})")
    except Exception as exc:  # noqa: BLE001 - tracking is best-effort
        print(f"   ⚠️  applied-marker git tracking failed: {exc}")


def _persist_remediation_records(
    plans, skips, *, gate_m: float, commit: bool = False
) -> None:
    """Write the fix files into the gps-tos-corrections repo (tracked!).

    Structure: ``<repo>/<station>/raw-remediation/<UTC-stamp>/plan.tsv`` +
    ``report.tsv`` — station dirs at the top level (existing repo convention,
    lowercase), one dated batch dir per run. Best-effort: no repo configured ->
    a hint, never a failure.

    Files are ALWAYS written — the review workflow depends on it, since the
    ``--apply-plan`` command this run prints points at the plan.tsv below.

    ``commit`` gates only the git commit+push, and is passed ``--yes``: the
    corrections repo records remediation ACTIONS, not rehearsals. It used to
    commit and push unconditionally, so a pure dry-run published to a shared
    repo with no flag involved — observed 2026-08-25, when a dry-run against a
    *synthetic* staged archive committed and pushed test data (reverted). That
    also made the verb impossible to practise. Nothing is lost by waiting:
    :func:`_record_plan_applied` stages the whole batch dir, so applying a plan
    commits plan, report and applied-marker together — one commit carrying both
    the intent and the outcome, rather than the two split across runs.
    """
    import subprocess
    from datetime import datetime, timezone

    from ..config.receivers_config import ReceiversConfig

    _persist_remediation_records.last_written = []
    issues = [sk for sk in skips if sk.reason != "verified-correct"]
    if not plans and not issues:
        return
    repo = ReceiversConfig().get_tos_corrections_repo()
    if not repo:
        default = Path.home() / "git" / "gps-tos-corrections"
        repo = str(default) if default.is_dir() else None
    if not repo or not Path(repo).is_dir():
        print(
            "   ⚠️  gps-tos-corrections repo not found ([paths] "
            "tos_corrections_repo or ~/git/gps-tos-corrections) — "
            "fix files not archived"
        )
        return
    repo_path = Path(repo)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%MZ")

    by_sta: dict = {}
    for pl in plans:
        sta = pl.src_rel.split("/")[2].lower()
        by_sta.setdefault(sta, ([], []))[0].append(pl)
    for sk in issues:
        parts = sk.rel.split("/")
        sta = parts[2].lower() if len(parts) >= 3 else "unknown"
        by_sta.setdefault(sta, ([], []))[1].append(sk)

    written = []
    for sta, (spl, ssk) in sorted(by_sta.items()):
        out_dir = repo_path / sta / "raw-remediation" / stamp
        out_dir.mkdir(parents=True, exist_ok=True)
        if spl:
            with open(out_dir / "plan.tsv", "w") as fh:
                fh.write(f"# archive-sort move plan — position gate {gate_m:.0f} m\n")
                fh.write("# src\tdst\treasons\tdecoded\tevidence\n")
                for pl in spl:
                    ev = (
                        f"{pl.station_dist_m:.0f}m from {pl.true_station}"
                        if pl.station_dist_m is not None
                        else ""
                    )
                    fh.write(
                        f"{pl.src_rel}\t{pl.dst_rel}\t{','.join(pl.reasons)}\t"
                        f"{pl.decoded_start:%Y-%m-%d}\t{ev}\n"
                    )
        with open(out_dir / "report.tsv", "w") as fh:
            fh.write("# kind\tpath\treason\tdetail\n")
            for pl in spl:
                fh.write(
                    f"MOVE\t{pl.src_rel}\t{','.join(pl.reasons)}\t-> {pl.dst_rel}\n"
                )
            for sk in ssk:
                fh.write(f"ISSUE\t{sk.rel}\t{sk.reason}\t{sk.detail}\n")
        written.append(out_dir)
        print(f"   📁 fix files -> {out_dir}")
    _persist_remediation_records.last_written = written

    if not commit:
        # Say it plainly rather than leaving the tree quietly dirty: someone
        # who wants a rehearsal recorded should be able to see how.
        rels = " ".join(str(d.relative_to(repo_path)) for d in written)
        print(
            "   📝 written but NOT committed (dry-run) — record them with:\n"
            f"      git -C {repo_path} add {rels} && "
            f"git -C {repo_path} commit && git -C {repo_path} push"
        )
        return

    try:
        rels = [str(d.relative_to(repo_path)) for d in written]
        subprocess.run(
            ["git", "-C", str(repo_path), "add", *rels],
            check=True,
            capture_output=True,
            timeout=30,
        )
        msg = (
            f"raw-remediation {stamp}: "
            + ", ".join(
                f"{sta} ({len(spl)} moves, {len(ssk)} issues)"
                for sta, (spl, ssk) in sorted(by_sta.items())
            )
            + "\n\nGenerated by 'receivers archive-sort' (position gate "
            f"{gate_m:.0f} m)."
        )
        done = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-q", "-m", msg],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if done.returncode == 0:
            pushed = subprocess.run(
                ["git", "-C", str(repo_path), "push", "-q"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            # A fresh clone often has no upstream set on its default branch, so a
            # bare `git push` fails ("has no upstream branch"). Fall back to an
            # explicit `origin HEAD` (and set tracking) so the records actually
            # reach the shared repo instead of stranding in a local commit.
            if pushed.returncode != 0 and "upstream" in (pushed.stderr or ""):
                pushed = subprocess.run(
                    ["git", "-C", str(repo_path), "push", "-q", "-u", "origin", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            if pushed.returncode == 0:
                print("   📌 committed + pushed to gps-tos-corrections")
            else:
                print(
                    "   📌 committed to gps-tos-corrections (push failed: "
                    f"{(pushed.stderr or '').strip()[:80]} — push manually)"
                )
        else:
            print("   · nothing new to commit in gps-tos-corrections")
    except Exception as exc:  # noqa: BLE001 - tracking is best-effort
        print(f"   ⚠️  gps-tos-corrections commit failed: {exc}")


def _apply_plan_file(plan_path: Path, args) -> int:
    """Execute a reviewed plan.tsv verbatim (no re-decode) via the gateway.

    Dry-run unless --yes; a real run inside gps-tos-corrections gets an
    applied-marker committed+pushed (_record_plan_applied).
    """
    from ..archive import load_sync_config, relocate_archive_files

    pairs = []
    for ln in plan_path.read_text().splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        cols = ln.split("\t")
        if len(cols) >= 2:
            pairs.append((cols[0].strip(), cols[1].strip()))
    if not pairs:
        print(f"❌ no src/dst pairs in {plan_path}")
        return 2
    target = next(
        (
            t
            for t in load_sync_config(Path(args.config) if args.config else None)
            if getattr(t, "tier", None) == "archive"
        ),
        None,
    )
    if target is None or not target.host:
        print("❌ no remote archive tier target in sync.yaml")
        return 2
    execute = bool(args.yes)
    print(
        f"🌀 APPLYING PLAN {plan_path} ({len(pairs)} move(s))"
        + ("" if execute else " (DRY-RUN)")
    )
    res = relocate_archive_files(
        pairs,
        ssh_target=f"{target.user}@{target.host}",
        dest_root=target.dest,
        execute=execute,
    )
    for src, dst in res.would_move:
        print(f"   🅼  would move: {src} -> {dst}")
    for src, dst in res.moved:
        print(f"   ✅ MOVED: {src} -> {dst}")
    for src, dst in res.dst_exists:
        print(f"   ⏭️  SKIP destination exists: {dst}")
    for src, _d in res.missing:
        print(f"   ·  missing (already moved?): {src}")
    for src, dst in res.failed:
        print(f"   ❌ FAILED: {src} -> {dst}")
    for src, _d in res.unreported:
        print(f"   ⚠️  NO STATUS (gateway dropped — re-run): {src}")
    if not execute and res.would_move:
        print("\n   → re-run with --yes to actually move.")
    if execute and res.moved:
        _record_plan_applied(plan_path, res)
    return 0 if res.ok else 1


def cmd_archive_sort(args: argparse.Namespace) -> int:
    """Find misfiled/misnamed raw files (decoded date ≠ filename date) and
    move them to their correct archive location via the rawdata gateway.

    Planning reads file content locally (read-only mount): magic-byte format
    classification + `teqc +meta` decoded epoch span. Dry-run by default;
    --yes executes the moves (argv-safe, never overwrites an existing file).
    """
    from ..archive import (
        load_sync_config,
        plan_relocations,
        plan_rinex_relocations,
        relocate_archive_files,
    )
    from ..archive.sort import resolve_position_gate_m

    # Apply the LATEST unapplied plan per station from gps-tos-corrections —
    # the no-flag form of --apply-plan: scan wrote it there, review happened
    # in the repo, --apply picks it up.
    if args.apply:
        if not args.stations:
            print("❌ --apply needs STATION(s) (whose latest plan to run)")
            return 2
        from ..config.receivers_config import ReceiversConfig

        repo = ReceiversConfig().get_tos_corrections_repo() or str(
            Path.home() / "git" / "gps-tos-corrections"
        )
        rc = 0
        for sta in args.stations:
            base = Path(repo) / sta.lower() / "raw-remediation"
            candidates = (
                sorted(
                    (d for d in base.iterdir() if (d / "plan.tsv").is_file()),
                    reverse=True,
                )
                if base.is_dir()
                else []
            )
            plan = None
            for d in candidates:
                if (d / "applied.txt").exists():
                    break  # newest applied — nothing newer pending
                plan = d / "plan.tsv"
                break
            if plan is None:
                print(f"   {sta.upper()}: no unapplied plan in {base} — nothing to do")
                continue
            print(f"   {sta.upper()}: applying {plan}")
            rc = max(rc, _apply_plan_file(plan, args))
        return rc

    # Apply a previously reviewed plan verbatim — no re-decode, the reviewed
    # file IS the contract (src<TAB>dst per line; extra columns ignored).
    if args.apply_plan:
        return _apply_plan_file(Path(args.apply_plan), args)

    root = Path(args.root)
    if not root.is_dir():
        print(f"❌ local archive root not found: {root}")
        return 2

    rel_files: list[str] = list(args.file or [])
    if args.list:
        rel_files.extend(
            ln.strip()
            for ln in Path(args.list).read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
    # RINEX candidates travel separately: they need their own parse/classify
    # (parse_raw_name returns None and classify_raw says 'unknown' for a .d.Z)
    # and their own size floor, so they cannot ride the raw list.
    rinex_files: list[str] = []
    if args.stations:
        from ..archive.sort import scan_station_raw, scan_station_rinex

        years = None
        if args.years:
            years = []
            for tok in args.years:
                if "-" in tok:
                    a, b = tok.split("-", 1)
                    years.extend(range(int(a), int(b) + 1))
                else:
                    years.append(int(tok))
        for sta in args.stations:
            found = scan_station_raw(root, sta, args.session, years=years)
            print(f"   {sta.upper()}/{args.session}: {len(found)} raw file(s)")
            rel_files.extend(found)
            if args.scan_rinex:
                rx = scan_station_rinex(root, sta, args.session, years=years)
                print(f"   {sta.upper()}/{args.session}: {len(rx)} rinex file(s)")
                rinex_files.extend(rx)
    else:
        # An explicit --file/--list may name RINEX paths directly; route them
        # by their archive category segment rather than making the caller say.
        from ..archive.sort import split_raw_and_rinex

        explicit_raw, explicit_rinex = split_raw_and_rinex(rel_files)
        rinex_files.extend(explicit_rinex)
        rel_files = explicit_raw
    if not rel_files and not rinex_files:
        print("❌ nothing to check (give STATION(s), --file or --list)")
        return 2

    print(f"🔎 archive-sort: classifying + decoding {len(rel_files)} file(s)")
    print(f"   local root: {root}")
    gate_m = resolve_position_gate_m(args.station_gate_m)
    if args.check_station:
        print(
            f"   station identity check: ON (position gate {gate_m:.0f} m — "
            "same metric as the RINEX-header check; coordinates decide)"
        )
    plans, skips = plan_relocations(
        root,
        rel_files,
        min_bytes=args.min_bytes,
        verify_station=args.check_station,
        station_gate_m=gate_m,
        progress=_sort_progress_renderer(len(rel_files)),
    )

    if rinex_files:
        if not args.check_station:
            # Say so rather than silently reporting only half of what the
            # RINEX pass can see — position is its only identity evidence.
            print(
                f"   rinex pass: {len(rinex_files)} file(s), name/path "
                "consistency only — add --check-station to test identity by "
                "position (header-only, no teqc)"
            )
        else:
            print(
                f"   rinex pass: {len(rinex_files)} file(s), identity from each "
                "file's own APPROX POSITION XYZ"
            )
        rx_plans, rx_skips = plan_rinex_relocations(
            root,
            rinex_files,
            verify_station=args.check_station,
            station_gate_m=gate_m,
            progress=_sort_progress_renderer(len(rinex_files)),
        )
        # A RINEX already co-planned by the raw pass (as a stray raw's
        # identity evidence) must not be planned twice.
        planned = {p.src_rel for p in plans}
        plans.extend(p for p in rx_plans if p.src_rel not in planned)
        skips.extend(rx_skips)

    by_reason: dict[str, int] = {}
    for s in skips:
        by_reason[s.reason] = by_reason.get(s.reason, 0) + 1
    for reason, n in sorted(by_reason.items()):
        print(f"   · {reason}: {n}")
    if args.verbose:
        for s in skips:
            if s.reason != "verified-correct":
                print(f"     {s.reason}: {s.rel} {s.detail}")

    def _write_report():
        if not args.report_out:
            return
        with open(args.report_out, "w") as fh:
            fh.write("# kind\tpath\treason\tdetail\n")
            for p in plans:
                fh.write(f"MOVE\t{p.src_rel}\t{','.join(p.reasons)}\t-> {p.dst_rel}\n")
            for sk in skips:
                if sk.reason == "verified-correct":
                    continue
                fh.write(f"ISSUE\t{sk.rel}\t{sk.reason}\t{sk.detail}\n")
        print(f"   full issue report written to {args.report_out}")

    if not plans:
        _write_report()
        _persist_remediation_records(plans, skips, gate_m=gate_m, commit=bool(args.yes))
        print("✅ no misfiled files — nothing to move")
        _print_fix_commands(
            plans, skips, getattr(_persist_remediation_records, "last_written", [])
        )
        return 0

    print(f"\n📦 {len(plans)} file(s) need remediation:")
    for p in plans:
        why = ",".join(p.reasons) or "misfiled"
        extra = ""
        if "wrong-station" in p.reasons and p.station_dist_m is not None:
            extra = (
                f" — position is {p.station_dist_m:.0f} m from "
                f"{p.true_station}'s mark"
            )
            if p.evidence:
                extra += f" (via {p.evidence})"
        print(
            f"   {p.src_rel}  [{why}]\n"
            f"     claims {p.claimed:%Y-%m-%d}, decodes to "
            f"{p.decoded_start:%Y-%m-%d %H:%M} ({p.fmt}){extra}\n"
            f"     -> {p.dst_rel}"
        )

    if args.plan_out:
        with open(args.plan_out, "w") as fh:
            fh.write("# src\tdst\treasons\tdecoded\tevidence\n")
            for p in plans:
                ev = p.evidence or (
                    f"{p.station_dist_m:.0f}m from {p.true_station}"
                    if p.station_dist_m is not None
                    else ""
                )
                fh.write(
                    f"{p.src_rel}\t{p.dst_rel}\t{','.join(p.reasons)}\t"
                    f"{p.decoded_start:%Y-%m-%d}\t{ev}\n"
                )
        print(
            f"\n   plan written to {args.plan_out} — review, then run:\n"
            f"     receivers archive-sort --apply-plan {args.plan_out} --yes"
        )
    _write_report()
    _persist_remediation_records(plans, skips, gate_m=gate_m, commit=bool(args.yes))
    _print_fix_commands(
        plans, skips, getattr(_persist_remediation_records, "last_written", [])
    )

    # Resolve the archive gateway from the sync target (same as archive-rm).
    config_path = Path(args.config) if args.config else None
    target = next(
        (
            t
            for t in load_sync_config(config_path)
            if getattr(t, "tier", None) == "archive"
        ),
        None,
    )
    if target is None or not target.host:
        print("\n⚠️  no remote archive tier target in sync.yaml — plan only")
        return 0
    ssh_target = f"{target.user}@{target.host}"

    execute = bool(args.yes)
    print(
        "\n⚠️  ARCHIVE RELOCATION"
        + ("" if execute else " (DRY-RUN — nothing moved)")
        + f"\n   gateway: {ssh_target}:{target.dest}"
    )
    res = relocate_archive_files(
        [(p.src_rel, p.dst_rel) for p in plans],
        ssh_target=ssh_target,
        dest_root=target.dest,
        execute=execute,
    )
    for src, dst in res.invalid:
        print(f"   ❌ REFUSED (invalid/unsafe path): {src} -> {dst}")
    for src, dst in res.dst_exists:
        print(f"   ⏭️  SKIP destination exists (NOT replaced): {dst}")
    for src, _dst in res.missing:
        print(f"   ·  missing on archive: {src}")
    for src, dst in res.would_move:
        print(f"   🅼  would move: {src} -> {dst}")
    for src, dst in res.moved:
        print(f"   ✅ MOVED: {src} -> {dst}")
    for src, dst in res.failed:
        print(f"   ❌ FAILED: {src} -> {dst}")
    for src, _dst in res.unreported:
        print(f"   ⚠️  NO STATUS (gateway dropped mid-run — re-run): {src}")

    if not execute and res.would_move:
        print("\n   → re-run with --yes to actually move the files.")
    if res.moved:
        print(
            "\n   → catalog note: archive_catalog rows for the OLD paths are now "
            "stale — run archive-reindex / re-audit for the affected stations."
        )
    return 0 if res.ok else 1


def create_archive_sort_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-sort",
        help="Move misfiled raw files (decoded date ≠ filename) to the right place",
        description=(
            "Classify raw files by magic bytes, decode their TRUE observation "
            "date (teqc +meta — the receiver's embedded GPS week), and relocate "
            "files whose filename/path claims a different date (e.g. the RHOF "
            "2000/2001 batches holding 2010/2011 data). Dry-run by default; "
            "moves go through the rawdata gateway, argv-safe, and an existing "
            "destination is never overwritten."
        ),
    )
    parser.add_argument(
        "stations",
        nargs="*",
        metavar="STATION",
        help="Station(s) to audit — the verb scans YYYY/mon/STA/SESSION/raw "
        "itself (mirrors archive-audit); alternatively use --file/--list",
    )
    parser.add_argument(
        "--session",
        default="15s_24hr",
        help="Session type for the station scan (default: 15s_24hr)",
    )
    parser.add_argument(
        "--years",
        nargs="+",
        metavar="Y|A-B",
        help="Limit the station scan to these years / ranges (e.g. 2000-2011)",
    )
    parser.add_argument(
        "--file",
        action="extend",
        nargs="+",
        metavar="REL",
        help="Archive-relative path(s), raw or RINEX "
        "(YYYY/mon/STA/session/{raw,rinex}/FILE) — routed to the right pass by "
        "the category segment, so the two may be mixed freely",
    )
    parser.add_argument(
        "--list",
        metavar="FILE",
        help="File with one archive-relative path per line (# comments ok)",
    )
    parser.add_argument(
        "--root",
        default="/mnt_data/rawgpsdata",
        help="Local (read-only) archive mount used to classify/decode "
        "(default: /mnt_data/rawgpsdata)",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=4096,
        help="Skip files smaller than this as stubs (default: 4096)",
    )
    parser.add_argument(
        "--check-station",
        action="store_true",
        help="Also verify STATION identity from the decoded antenna position "
        "(coordinates decide) — a file matching another station's mark is "
        "relocated to that station's tree; no match within the gate = "
        "reported, never moved. When the raw decode cannot place the antenna "
        "(Septentrio SBF: teqc +meta reports the (90,0) placeholder), the "
        "sibling RINEX header's APPROX POSITION XYZ is used as fallback "
        "evidence — a stray it proves is moved together with its RINEX.",
    )
    parser.add_argument(
        "--station-gate-m",
        type=float,
        default=None,
        help="Position-match gate in metres for --check-station (default: "
        "receivers.cfg [rinex] position_gate_m, else 10 m — same metric as "
        "the RINEX-header identity check)",
    )
    parser.add_argument(
        "--no-scan-rinex",
        dest="scan_rinex",
        action="store_false",
        default=True,
        help="Do not walk the station's rinex/ tree (raw only). By default a "
        "station scan covers BOTH raw/ and rinex/: a stray RINEX whose raw is "
        "correctly filed is reachable no other way (the raw pass sees a RINEX "
        "only as a stray raw's sibling). The RINEX pass is header-only — no "
        "teqc, no decompress-to-decode — and like the raw pass it needs "
        "--check-station before a position can relocate anything.",
    )
    parser.add_argument(
        "--plan-out",
        metavar="FILE",
        help="Write the runnable move plan (src<TAB>dst<TAB>reasons...) to "
        "FILE — execute it later with --apply-plan FILE --yes",
    )
    parser.add_argument(
        "--report-out",
        metavar="FILE",
        help="Write the FULL issue report (moves + stubs/unknown-station/"
        "unreadable with evidence) to FILE",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the LATEST unapplied plan for the given STATION(s) from "
        "gps-tos-corrections (no path needed); dry-run unless --yes",
    )
    parser.add_argument(
        "--apply-plan",
        metavar="FILE",
        help="Execute a specific reviewed plan file verbatim "
        "(no re-decode); dry-run unless --yes",
    )
    parser.add_argument("--config", help="Path to sync.yaml (default: standard)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Execute the moves (default: dry-run)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.set_defaults(func=cmd_archive_sort)
    return parser


def cmd_archive_prune(args: argparse.Namespace) -> int:
    """Local ring-buffer prune: age out local gpsdata copies whose long-term
    archive copy is catalog-confirmed. Dry-run by default; --yes deletes.
    Retention + guardrails come from scheduler.yaml [local_prune] (overridable
    per-flag for manual passes)."""
    from ..archive import load_sync_config
    from ..archive.prune import PruneConfig, disk_free_gb, run_prune
    from ..config.receivers_config import ReceiversConfig
    from ..scheduling.config_loader import load_scheduler_config

    try:
        ycfg = load_scheduler_config(
            Path(args.scheduler_config) if args.scheduler_config else None
        )
        prune_cfg = ycfg.get("local_prune", {}) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  scheduler.yaml not loadable ({exc}) — using built-in defaults")
        prune_cfg = {}

    cfg = PruneConfig.from_dict(prune_cfg)
    if args.retention:
        for spec in args.retention:
            session, _, days = spec.partition("=")
            cfg.retention_days[session] = int(days)
    if args.max_delete is not None:
        cfg.max_delete_per_run = args.max_delete

    rcfg = ReceiversConfig()
    root = Path(args.root or rcfg.get_data_prepath())
    if not root.is_dir():
        print(f"❌ data root not found: {root}")
        return 2

    target = next(
        (
            t
            for t in load_sync_config(Path(args.config) if args.config else None)
            if getattr(t, "tier", None) == "archive"
        ),
        None,
    )
    archive_location = prune_cfg.get("archive_location") or getattr(
        target, "name", None
    )
    if cfg.require_catalog and not archive_location:
        print("❌ no archive tier target — catalog gate impossible, refusing")
        return 2

    execute = bool(args.yes)
    free, total = disk_free_gb(root)
    print("🌀 LOCAL RING-BUFFER PRUNE" + ("" if execute else " (DRY-RUN)"))
    print(f"   root: {root}  ({free:.0f} GB free of {total:.0f} GB)")
    print(f"   retention: {cfg.retention_days}")
    print(
        f"   guardrails: warn<{cfg.warn_free_gb:.0f} GB, "
        f"emergency<{cfg.min_free_gb:.0f} GB "
        f"(emergency retention: {cfg.emergency_retention_days})"
    )
    print(
        f"   catalog gate: {'ON — only archive-confirmed files' if cfg.require_catalog else 'OFF (--no-catalog-gate)'}"
    )

    conn = None
    if cfg.require_catalog:
        from ..db.connection import get_connection

        try:
            conn = get_connection(host_override=args.catalog_host)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ gps_health connection failed ({exc}) — nothing deleted")
            return 1
    try:
        stats = run_prune(
            root,
            cfg,
            archive_location=archive_location or "",
            conn=conn,
            dry_run=not execute,
            sessions=args.session or None,
        )
    finally:
        if conn is not None:
            conn.close()

    verb = "deleted" if execute else "would delete"
    print(
        f"\n   {verb}: {stats.deleted} file(s), {stats.freed_bytes / 1e9:.1f} GB "
        f"(examined {stats.examined})"
    )
    for session, n in stats.per_session.items():
        print(f"      {session}: {n}")
    if stats.kept_uncataloged:
        print(
            f"   🛡  kept {stats.kept_uncataloged} file(s) NOT confirmed in the "
            "archive catalog — check archive-sync before touching these"
        )
    if stats.unparseable:
        print(f"   · {stats.unparseable} unparseable filename(s) skipped")
    if stats.capped:
        print("   ⏸  capped by max_delete_per_run — run again for the remainder")
    if stats.mode != "normal":
        print(f"   ⚠️  disk mode: {stats.mode}")
    if not execute and stats.deleted:
        print("\n   → re-run with --yes to actually delete.")
    return 0


def create_archive_prune_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-prune",
        help="Ring-buffer prune of local gpsdata (catalog-gated, dry-run default)",
        description=(
            "Delete local gpsdata files older than the per-session retention "
            "(scheduler.yaml [local_prune]) — ONLY files whose long-term archive "
            "copy is confirmed in archive_catalog. Disk guardrails log WARNING "
            "under warn_free_gb and switch to emergency retention under "
            "min_free_gb. Dry-run by default."
        ),
    )
    parser.add_argument(
        "--session",
        action="extend",
        nargs="+",
        metavar="S",
        help="Limit to these session types (default: all configured)",
    )
    parser.add_argument(
        "--retention",
        action="extend",
        nargs="+",
        metavar="SESSION=DAYS",
        help="Override retention for this run (e.g. 1Hz_1hr=14)",
    )
    parser.add_argument(
        "--root", help="Data root (default: receivers.cfg data_prepath)"
    )
    parser.add_argument("--max-delete", type=int, help="Cap deletions this run")
    parser.add_argument("--config", help="Path to sync.yaml (archive target name)")
    parser.add_argument(
        "--scheduler-config", help="Path to scheduler.yaml (default: standard)"
    )
    parser.add_argument(
        "--catalog-host", help="gps_health host override for the catalog gate"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Actually delete (default: dry-run)"
    )
    parser.set_defaults(func=cmd_archive_prune)
    return parser


def cmd_archive_catalog_backfill(args: argparse.Namespace) -> int:
    """Seed local_raw/local_rinex catalog rows from file_tracking (unified index M1)."""
    import json as _json

    from ..archive.catalog import backfill_local_catalog
    from ..config.receivers_config import ReceiversConfig

    conn = _get_conn(args.host, required=True)
    root = args.root or ReceiversConfig().get_data_prepath()
    stats = backfill_local_catalog(
        conn,
        root,
        batch_size=args.batch_size,
        verify_exists=not args.no_verify_exists,
        dry_run=args.dry_run,
    )
    conn.close()
    if args.json:
        print(_json.dumps(stats))
    else:
        verb = "would catalog" if args.dry_run else "cataloged"
        print(
            f"local catalog backfill: scanned={stats['scanned']} "
            f"{verb}={stats['cataloged']} "
            f"skipped_missing={stats['skipped_missing']} "
            f"skipped_unmapped={stats['skipped_unmapped']}"
        )
    return 0


def create_archive_catalog_backfill_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "catalog-backfill-local",
        help="Seed local_raw/local_rinex archive_catalog rows from file_tracking",
        description=(
            "One-time (idempotent) backfill of the unified file index: for every "
            "locally-held file already in file_tracking (archived/downloaded), "
            "upsert a local_raw/local_rinex archive_catalog row. Carries any "
            "file_tracking content_sha256 already computed (no re-hash) and skips "
            "rows whose on-disk file is absent (unless --no-verify-exists). Run "
            "against localhost first; the throttled rek-d01 run is a later phase."
        ),
    )
    parser.add_argument(
        "--host", help="gps_health host override (default: database.cfg host)"
    )
    parser.add_argument(
        "--root", help="Data root (default: receivers.cfg data_prepath)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=1000, help="Rows per page (default: 1000)"
    )
    parser.add_argument(
        "--no-verify-exists",
        action="store_true",
        help="Do not stat each file — trust file_tracking (faster, less accurate)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be cataloged without writing",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_catalog_backfill)
    return parser
