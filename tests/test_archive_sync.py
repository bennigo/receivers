"""Tests for the archive-sync engine (receivers.archive)."""

import os
from datetime import datetime, timedelta

import pytest

from receivers.archive.config import SyncTarget, load_sync_config
from receivers.archive.engine import ArchiveSync
from receivers.archive.path_parse import parse_archive_path
from receivers.archive.state import compute_floor

CUTOVER = datetime(2026, 6, 22, 0, 0, 0)


def _target(tmp_root, **over):
    base = dict(
        name="imo_archive",
        active=True,
        tier="archive",
        host="rawdata.vedur.is",
        user="gpsops",
        dest="~/gpsdata",
        source_root=str(tmp_root),
        sessions=("15s_24hr", "1Hz_1hr", "status_1hr"),
        file_categories=("raw",),
        exclude_stations=frozenset({"DYNA", "HRNC", "HAUR"}),
        cutover=CUTOVER,
        overlap_minutes=5,
    )
    base.update(over)
    return SyncTarget(**base)


# --------------------------------------------------------------------------- config
class TestConfig:
    def test_load(self, tmp_path):
        cfg = tmp_path / "sync.yaml"
        cfg.write_text("""
overlap_minutes: 7
targets:
  - name: imo_archive
    active: true
    tier: archive
    host: rawdata.vedur.is
    user: gpsops
    dest: ~/gpsdata
    source_root: /mnt/data/gpsdata
    sessions: [15s_24hr, 1Hz_1hr]
    file_categories: [raw, rinex]
    exclude_stations: [DYNA, HRNC, HAUR]
    cutover: "2026-06-22T00:00:00"
""")
        targets = load_sync_config(cfg)
        assert len(targets) == 1
        t = targets[0]
        assert t.name == "imo_archive"
        assert t.active is True
        assert t.overlap_minutes == 7  # inherited file default
        assert t.file_categories == ("raw", "rinex")
        assert t.exclude_stations == frozenset({"DYNA", "HRNC", "HAUR"})
        assert t.cutover == CUTOVER
        assert t.remote == "gpsops@rawdata.vedur.is:~/gpsdata"

    def test_legacy_singular_file_category(self, tmp_path):
        cfg = tmp_path / "sync.yaml"
        cfg.write_text("""
targets:
  - name: imo_archive
    active: true
    host: rawdata.vedur.is
    user: gpsops
    dest: ~/gpsdata
    source_root: /mnt/data/gpsdata
    sessions: [15s_24hr]
    file_category: raw
    cutover: "2026-06-22T00:00:00"
""")
        t = load_sync_config(cfg)[0]
        assert t.file_categories == ("raw",)

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_sync_config(tmp_path / "nope.yaml") == []


# ------------------------------------------------------------------------ path parse
class TestPathParse:
    def test_full_path(self):
        p = parse_archive_path(
            "/mnt/data/gpsdata/2026/apr/AKUR/15s_24hr/raw/AKUR202604070000a.T02.gz",
            "/mnt/data/gpsdata",
        )
        assert p is not None
        assert (p.station, p.session_type, p.file_category) == (
            "AKUR",
            "15s_24hr",
            "raw",
        )
        assert p.relative_path == "2026/apr/AKUR/15s_24hr/raw/AKUR202604070000a.T02.gz"

    def test_outside_root_is_none(self):
        assert parse_archive_path("/etc/passwd", "/mnt/data/gpsdata") is None

    def test_too_shallow_is_none(self):
        assert (
            parse_archive_path("/mnt/data/gpsdata/2026/x.T02", "/mnt/data/gpsdata")
            is None
        )


# ----------------------------------------------------------------------------- floor
class TestFloor:
    def test_first_run_is_cutover(self):
        assert compute_floor(None, CUTOVER, 5) == CUTOVER

    def test_backs_off_by_overlap(self):
        last = datetime(2026, 6, 23, 12, 0)
        assert compute_floor(last, CUTOVER, 5) == last - timedelta(minutes=5)

    def test_never_below_cutover(self):
        last = CUTOVER + timedelta(minutes=2)
        assert compute_floor(last, CUTOVER, 5) == CUTOVER


# ------------------------------------------------------------------------- itemize
class TestParseTransferred:
    def test_push_transfers(self):
        out = (
            "<f+++++++++ 2026/apr/AKUR/15s_24hr/raw/AKUR0.T02.gz\n"
            "<f+++++++++ 2026/apr/BLEI/15s_24hr/raw/BLEI0.T00\n"
            "cd+++++++++ 2026/apr/AKUR/15s_24hr/raw/\n"  # dir create, not a file
            ".d          2026/\n"  # unchanged dir
        )
        got = ArchiveSync._parse_transferred(out)
        assert got == [
            "2026/apr/AKUR/15s_24hr/raw/AKUR0.T02.gz",
            "2026/apr/BLEI/15s_24hr/raw/BLEI0.T00",
        ]

    def test_empty(self):
        assert ArchiveSync._parse_transferred("") == []


# ------------------------------------------------------------------- find_delta
def _make_file(root, rel, mtime: datetime):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # .Z fixtures need the LZW magic or the format guard (correctly) refuses
    # to sync them — .Z means real compress(1) output.
    payload = b"\x1f\x9ddata" if rel.endswith(".Z") else b"data"
    p.write_bytes(payload)
    ts = mtime.timestamp()
    os.utime(p, (ts, ts))
    return p


class TestFindDelta:
    def test_filters_mtime_session_and_excludes(self, tmp_path):
        new = CUTOVER + timedelta(days=1)
        old = CUTOVER - timedelta(days=1)
        # in delta:
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_new.T02.gz", new)
        _make_file(tmp_path, "2026/jun/THOB/1Hz_1hr/raw/THOB_new.sbf.gz", new)
        # excluded: too old
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_old.T02.gz", old)
        # excluded: alias station
        _make_file(tmp_path, "2026/jun/DYNA/15s_24hr/raw/DYNA_new.T02.gz", new)
        # excluded: session not configured
        _make_file(tmp_path, "2026/jun/AKUR/6h_custom/raw/AKUR_new.T02.gz", new)
        # excluded: not raw category
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/rinex/AKUR_new.d.Z", new)

        target = _target(tmp_path)
        eng = ArchiveSync(target, conn=None, dry_run=True)
        got = {
            os.path.relpath(p, str(tmp_path)) for p in eng.find_delta(CUTOVER, "raw")
        }
        assert got == {
            "2026/jun/AKUR/15s_24hr/raw/AKUR_new.T02.gz",
            "2026/jun/THOB/1Hz_1hr/raw/THOB_new.sbf.gz",
        }

    def test_rinex_category_finds_rinex_only(self, tmp_path):
        new = CUTOVER + timedelta(days=1)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_new.T02.gz", new)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/rinex/AKUR_new.d.Z", new)
        eng = ArchiveSync(_target(tmp_path), conn=None, dry_run=True)
        got = {
            os.path.relpath(p, str(tmp_path)) for p in eng.find_delta(CUTOVER, "rinex")
        }
        assert got == {"2026/jun/AKUR/15s_24hr/rinex/AKUR_new.d.Z"}

    def test_missing_source_root(self, tmp_path):
        target = _target(tmp_path / "nope")
        eng = ArchiveSync(target, conn=None, dry_run=True)
        assert eng.find_delta(CUTOVER, "raw") == []


# ------------------------------------------------------------------------- run()
class TestRun:
    def test_inactive_target_skipped(self, tmp_path):
        target = _target(tmp_path, active=False)
        res = ArchiveSync(target, conn=None, dry_run=True).run()
        assert res.ok and "inactive" in res.message

    def test_cutover_override_changes_floor(self, tmp_path, monkeypatch):
        # Target cutover is in the future (CUTOVER); a file dated before it would
        # be excluded. An earlier --cutover override pulls it into the delta.
        before_cutover = CUTOVER - timedelta(days=2)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz", before_cutover)
        target = _target(tmp_path)  # cutover = CUTOVER (future relative to the file)

        # without override: floor = CUTOVER, file is older -> empty delta
        eng = ArchiveSync(target, conn=None, dry_run=True)
        assert eng.run().delta_count == 0

        # with an earlier override: file is newer than the override -> in delta
        eng2 = ArchiveSync(
            target,
            conn=None,
            dry_run=True,
            cutover_override=CUTOVER - timedelta(days=3),
        )
        monkeypatch.setattr(eng2, "_rsync", lambda rel, imm: (True, list(rel), ""))
        res = eng2.run()
        assert res.delta_count == 1
        assert res.floor == CUTOVER - timedelta(days=3)

    def test_force_runs_inactive_target(self, tmp_path, monkeypatch):
        # Pre-stage verify: an inactive target must still run under --force.
        new = CUTOVER + timedelta(days=1)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz", new)
        target = _target(tmp_path, active=False)
        eng = ArchiveSync(target, conn=None, dry_run=True, force=True)
        monkeypatch.setattr(eng, "_rsync", lambda rel, imm: (True, list(rel), ""))
        res = eng.run()
        assert res.ok
        assert "inactive" not in res.message
        assert res.delta_count == 1
        assert res.transferred == 1

    def test_dry_run_no_db(self, tmp_path, monkeypatch):
        new = CUTOVER + timedelta(days=1)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz", new)
        target = _target(tmp_path)
        eng = ArchiveSync(target, conn=None, dry_run=True)
        # stub rsync: pretend it would transfer the one file (rel_paths, immutability)
        monkeypatch.setattr(
            eng,
            "_rsync",
            lambda rel, imm: (True, list(rel), ""),
        )
        res = eng.run()
        assert res.ok
        assert res.delta_count == 1
        assert res.transferred == 1
        assert res.cataloged == 0  # dry-run never catalogs

    def test_empty_delta_is_ok(self, tmp_path):
        target = _target(tmp_path)  # empty tree
        res = ArchiveSync(target, conn=None, dry_run=True).run()
        assert res.ok
        assert res.delta_count == 0
        assert res.transferred == 0
        assert "raw 0/0" in res.message

    def test_both_tiers_use_correct_immutability(self, tmp_path, monkeypatch):
        new = CUTOVER + timedelta(days=1)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz", new)
        _make_file(tmp_path, "2026/jun/AKUR/15s_24hr/rinex/AKUR_a.d.Z", new)
        target = _target(tmp_path, file_categories=("raw", "rinex"))
        eng = ArchiveSync(target, conn=None, dry_run=True)
        # capture the immutability flag rsync is invoked with per tier
        seen: dict[str, str] = {}

        def fake_rsync(rel, imm):
            cat = "rinex" if any("/rinex/" in r for r in rel) else "raw"
            seen[cat] = imm
            return True, list(rel), ""

        monkeypatch.setattr(eng, "_rsync", fake_rsync)
        res = eng.run()
        assert res.ok
        assert res.delta_count == 2
        assert res.transferred == 2
        assert seen == {"raw": "--ignore-existing", "rinex": "--update"}
        assert "raw 1/1" in res.message and "rinex 1/1" in res.message

    def test_archive_path_uses_dest(self, tmp_path):
        target = _target(tmp_path)
        eng = ArchiveSync(target, conn=None)
        assert eng._archive_path("2026/jun/AKUR/15s_24hr/raw/x.T02.gz") == (
            "~/gpsdata/2026/jun/AKUR/15s_24hr/raw/x.T02.gz"
        )

    def test_dest_override(self, tmp_path):
        target = _target(tmp_path)
        eng = ArchiveSync(target, conn=None, dest_override="~/gpsdata_staging")
        assert eng.remote_dest == "gpsops@rawdata.vedur.is:~/gpsdata_staging"
        assert eng._archive_path("a/b.T02.gz") == "~/gpsdata_staging/a/b.T02.gz"


# --------------------------------------------------------- DB-backed round-trip
def _local_conn():
    try:
        from receivers.db.connection import get_connection

        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.archive_catalog'), "
                "to_regclass('public.sync_state')"
            )
            cat, st = cur.fetchone()
        if cat is None or st is None:
            conn.close()
            return None
        return conn
    except Exception:
        return None


@pytest.fixture
def db_conn():
    conn = _local_conn()
    if conn is None:
        pytest.skip("local gps_health with archive_catalog/sync_state not available")
    yield conn
    # cleanup test rows
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM archive_catalog WHERE storage_location = 'test_target'"
        )
        cur.execute("DELETE FROM sync_state WHERE target = 'test_target'")
    conn.commit()
    conn.close()


class TestDbRoundTrip:
    def test_catalog_upsert_idempotent(self, db_conn):
        from receivers.archive.catalog import upsert_catalog_row

        kw = dict(
            storage_location="test_target",
            station="AKUR",
            session_type="15s_24hr",
            file_category="raw",
            file_date=None,
            archive_path="~/gpsdata/2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz",
            filename="AKUR202606220000a.T02.gz",
            file_size=123,
            content_sha256="a" * 64,
        )
        upsert_catalog_row(db_conn, **kw)
        kw["file_size"] = 456  # same logical key -> update, not duplicate
        upsert_catalog_row(db_conn, **kw)
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), max(file_size), max(compression) "
                "FROM archive_catalog WHERE storage_location='test_target'"
            )
            n, size, comp = cur.fetchone()
        assert n == 1
        assert size == 456
        assert comp == ".gz"

    def test_watermark_advance_only_on_success(self, db_conn):
        from receivers.archive.state import get_last_success, record_run

        assert get_last_success(db_conn, "test_target") is None
        t1 = datetime(2026, 6, 22, 1, 0)
        record_run(db_conn, "test_target", ran_at=t1, files=3, ok=True, advance_to=t1)
        assert get_last_success(db_conn, "test_target") == t1
        # a failed run must NOT move the watermark back/forward
        t2 = datetime(2026, 6, 22, 2, 0)
        record_run(
            db_conn, "test_target", ran_at=t2, files=0, ok=False, advance_to=None
        )
        assert get_last_success(db_conn, "test_target") == t1


class TestEndToEndLocal:
    """Exercise the REAL rsync command + parser + catalog against a local dest.

    No SSH, no stub: a local destination (empty host) drives the actual
    ``rsync -a --ignore-existing --itemize-changes`` invocation, so the real
    itemize output feeds the real ``_parse_transferred`` and the real catalog
    upsert. This is the gap unit stubs cannot cover.
    """

    def test_real_rsync_lands_file_and_catalogs(self, tmp_path, db_conn):
        import hashlib
        import shutil

        if not shutil.which("rsync"):
            pytest.skip("rsync not available")

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        dst.mkdir()
        rel = "2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz"
        payload = b"raw payload bytes"
        # write content FIRST, then set mtime above the cutover floor (writing
        # resets mtime, so order matters).
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(payload)
        ts = (CUTOVER + timedelta(days=1)).timestamp()
        os.utime(f, (ts, ts))

        target = _target(src, name="test_target", host="", dest=str(dst))
        eng = ArchiveSync(target, conn=db_conn, dry_run=False)
        res = eng.run()

        # real rsync actually moved the file to the relative tree under dest
        assert (dst / rel).is_file()
        assert res.ok
        assert res.transferred == 1
        assert res.cataloged == 1

        # catalog row written with the real content hash (magic-byte: plain here)
        expected_hash = hashlib.sha256(payload).hexdigest()
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT station, session_type, file_category, content_sha256, "
                "compression, file_path FROM archive_catalog "
                "WHERE storage_location='test_target'"
            )
            row = cur.fetchone()
        assert row[0:3] == ("AKUR", "15s_24hr", "raw")
        assert row[3] == expected_hash
        assert row[4] == ".gz"
        assert row[5] == f"{dst}/{rel}"

        # second run is idempotent: --ignore-existing => nothing transfers
        res2 = eng.run()
        assert res2.ok
        assert res2.transferred == 0

    def test_raw_immutable_rinex_updates(self, tmp_path, db_conn):
        """Real rsync: raw is never overwritten; a newer rinex replaces the old."""
        import shutil

        if not shutil.which("rsync"):
            pytest.skip("rsync not available")

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        dst.mkdir()
        raw_rel = "2026/jun/AKUR/15s_24hr/raw/AKUR_a.T02.gz"
        rnx_rel = "2026/jun/AKUR/15s_24hr/rinex/AKUR_a.d.Z"

        def write(rel, data, when):
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            ts = when.timestamp()
            os.utime(f, (ts, ts))

        write(raw_rel, b"raw v1", CUTOVER + timedelta(days=1))
        write(
            rnx_rel,
            b"\x1f\x9d\x90r\xd2\xb8)\x83\x07\x84\x9d\x18",
            CUTOVER + timedelta(days=1),
        )  # compress(1) of b"rinex v1"

        target = _target(
            src,
            name="test_target",
            host="",
            dest=str(dst),
            file_categories=("raw", "rinex"),
        )
        eng = ArchiveSync(target, conn=db_conn, dry_run=False)
        res = eng.run()
        assert res.ok
        assert res.transferred == 2  # both tiers landed
        assert (dst / raw_rel).read_bytes() == b"raw v1"
        assert (
            dst / rnx_rel
        ).read_bytes() == b"\x1f\x9d\x90r\xd2\xb8)\x83\x07\x84\x9d\x18"

        # Regenerate BOTH, with a mtime of NOW.
        #
        # Not CUTOVER+2d, which is what this test used to do: run 1 advances
        # the `sync_state` watermark to wall-clock now, and the next delta
        # scan floors at `max(last_success_ts - overlap, cutover)`. A file
        # stamped June 2026 is below that floor, so it is never scanned and
        # the archive keeps v1 — which is correct engine behaviour, not the
        # immutability rule this test is about. A real regeneration (a
        # `--fix-headers` rewrite) stamps the file NOW, which is above the
        # floor, so this is also the faithful fixture.
        regenerated_at = datetime.now()
        write(raw_rel, b"raw v2 MUST NOT WIN", regenerated_at)
        write(
            rnx_rel,
            b"\x1f\x9d\x90r\xd2\xb8)\x83\x07\x84\x1d\x19",
            regenerated_at,
        )  # compress(1) of b"rinex v2"
        eng.run()

        # raw: --ignore-existing => archive keeps v1 (immutable record)
        assert (dst / raw_rel).read_bytes() == b"raw v1"
        # rinex: --update => archive now holds the newer v2 (regenerable)
        assert (
            dst / rnx_rel
        ).read_bytes() == b"\x1f\x9d\x90r\xd2\xb8)\x83\x07\x84\x1d\x19"


class TestFreshness:
    def test_inactive_needs_no_db(self, tmp_path):
        from receivers.archive.freshness import evaluate_freshness

        target = _target(tmp_path, active=False)
        s = evaluate_freshness(None, target, now=datetime(2026, 6, 23, 12, 0))
        assert s.state == "inactive"
        assert not s.is_alerting

    def test_never_is_alerting(self, db_conn):
        from receivers.archive.freshness import evaluate_freshness

        target = _target("/x", name="test_target")
        s = evaluate_freshness(db_conn, target, now=datetime(2026, 6, 23, 12, 0))
        assert s.state == "never"
        assert s.is_alerting

    def test_ok_within_threshold(self, db_conn):
        from receivers.archive.freshness import evaluate_freshness
        from receivers.archive.state import record_run

        now = datetime(2026, 6, 23, 12, 0)
        last = now - timedelta(minutes=10)
        record_run(
            db_conn, "test_target", ran_at=last, files=1, ok=True, advance_to=last
        )
        target = _target("/x", name="test_target")
        s = evaluate_freshness(db_conn, target, now=now, max_age_minutes=120)
        assert s.state == "ok"
        assert not s.is_alerting

    def test_stale_past_threshold(self, db_conn):
        from receivers.archive.freshness import evaluate_freshness
        from receivers.archive.state import record_run

        now = datetime(2026, 6, 23, 12, 0)
        last = now - timedelta(minutes=200)
        record_run(
            db_conn, "test_target", ran_at=last, files=1, ok=True, advance_to=last
        )
        target = _target("/x", name="test_target")
        s = evaluate_freshness(db_conn, target, now=now, max_age_minutes=120)
        assert s.state == "stale"
        assert s.is_alerting


class TestSchedulerWiring:
    def test_archive_sync_in_default_config(self):
        from receivers.scheduling.config_loader import get_default_config

        cfg = get_default_config()
        assert "archive_sync" in cfg
        assert cfg["archive_sync"]["enabled"] is False  # opt-in only
        assert cfg["archive_sync"]["schedule"] == ":45"

    def test_job_no_op_without_config(self):
        # Missing sync.yaml -> no targets -> no-op, must not raise (no DB touched).
        from receivers.archive.job import run_archive_sync_job

        run_archive_sync_job(config_path="/nonexistent/sync.yaml")

    def test_job_no_op_with_only_inactive(self, tmp_path):
        from receivers.archive.job import run_archive_sync_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text("""
targets:
  - name: imo_archive
    active: false
    host: rawdata.vedur.is
    user: gpsops
    dest: ~/gpsdata
    source_root: /mnt/data/gpsdata
    sessions: [15s_24hr]
    cutover: "2026-06-22T00:00:00"
""")
        # all inactive -> returns before opening any DB connection
        run_archive_sync_job(config_path=str(cfg))


class TestPushExplicit:
    """push_explicit() — the low-latency write-through used by the download hooks.

    Real rsync into a local dest (host-less target, no SSH): assert files land,
    catalog rows are written for raw+rinex, scope/exclude filtering holds, and a
    re-push is an idempotent no-op (download-retry safe).
    """

    def test_pushes_raw_and_rinex_and_catalogs(self, tmp_path, db_conn):
        import hashlib
        import shutil

        if not shutil.which("rsync"):
            pytest.skip("rsync not available")

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        dst.mkdir()
        raw_rel = "2026/jun/AKUR/15s_24hr/raw/AKUR202606220000a.T02.gz"
        rnx_rel = "2026/jun/AKUR/15s_24hr/rinex/AKUR1730.26d.Z"
        raw_payload = b"raw sbf payload"
        rnx_plain = b"hatanaka rinex payload"
        rnx_payload = b"\x1f\x9d\x90h\xc2\xd0\t\xe3&\xcc\x9a0 \xe4\xa4qS\x06\x0f\x088a\xf2\xb0y\x13\x86\x0c"  # compress(1) of rnx_plain
        for rel, data in ((raw_rel, raw_payload), (rnx_rel, rnx_payload)):
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)

        target = _target(
            src,
            name="test_target",
            host="",
            dest=str(dst),
            file_categories=("raw", "rinex"),
        )
        eng = ArchiveSync(target, conn=db_conn)

        pushed, errors, digests = eng.push_explicit(
            [str(src / raw_rel), str(src / rnx_rel)]
        )
        assert errors == []
        assert pushed == 2
        assert (dst / raw_rel).is_file() and (dst / rnx_rel).is_file()
        assert set(digests) == {raw_rel, rnx_rel}
        assert digests[raw_rel] == hashlib.sha256(raw_payload).hexdigest()

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT file_category, content_sha256 FROM archive_catalog "
                "WHERE storage_location='test_target' ORDER BY file_category"
            )
            rows = dict(cur.fetchall())
        assert rows["raw"] == hashlib.sha256(raw_payload).hexdigest()
        assert rows["rinex"] == hashlib.sha256(rnx_plain).hexdigest()

        # re-push is a clean no-op (raw --ignore-existing, rinex --update unchanged)
        pushed2, errors2, _ = eng.push_explicit(
            [str(src / raw_rel), str(src / rnx_rel)]
        )
        assert errors2 == []
        assert pushed2 == 0

    def test_filters_scope_and_excludes(self, tmp_path, db_conn):
        import shutil

        if not shutil.which("rsync"):
            pytest.skip("rsync not available")

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        dst.mkdir()
        # excluded alias station, an out-of-scope session, and one good file
        excluded = "2026/jun/DYNA/15s_24hr/raw/DYNA202606220000a.T02.gz"
        offscope = "2026/jun/AKUR/30s_1hr/raw/AKUR202606220000a.T02.gz"
        good = "2026/jun/AKUR/15s_24hr/raw/AKUR202606220000a.T02.gz"
        for rel in (excluded, offscope, good):
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")

        target = _target(src, name="test_target", host="", dest=str(dst))
        eng = ArchiveSync(target, conn=db_conn)
        pushed, errors, _ = eng.push_explicit(
            [str(src / excluded), str(src / offscope), str(src / good)]
        )
        assert errors == []
        assert pushed == 1  # only the good one
        assert (dst / good).is_file()
        assert not (dst / excluded).exists()
        assert not (dst / offscope).exists()

    def test_nonexistent_and_nonarchive_paths_skipped(self, tmp_path, db_conn):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        dst.mkdir()
        target = _target(src, name="test_target", host="", dest=str(dst))
        eng = ArchiveSync(target, conn=db_conn)
        # a path that doesn't exist + a path outside the archive tree → no-op, no raise
        pushed, errors, digests = eng.push_explicit(
            [str(src / "nope/missing.gz"), "/etc/hostname"]
        )
        assert pushed == 0
        assert errors == []
        assert digests == {}


class TestVerifyPushed:
    """verify_pushed() — immediate per-push read-back via the archive mount.

    Local dest doubles as the 'archive mount' (read_root=dst): an intact copy
    stamps last_verified_at; a tampered archive copy is flagged ARCHIVE CORRUPT;
    an absent file is DEFERRED (never a false corruption); out-of-scope sessions
    are skipped.
    """

    def _push(self, tmp_path, db_conn, rel, payload):
        import shutil

        if not shutil.which("rsync"):
            pytest.skip("rsync not available")
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        dst.mkdir(exist_ok=True)
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(payload)
        target = _target(
            src,
            name="test_target",
            host="",
            dest=str(dst),
            file_categories=("raw", "rinex"),
        )
        eng = ArchiveSync(target, conn=db_conn)
        pushed, errors, digests = eng.push_explicit([str(f)])
        assert pushed == 1 and errors == []
        return eng, dst

    def test_intact_copy_is_verified_and_stamped(self, tmp_path, db_conn):
        rel = "2026/jun/AKUR/15s_24hr/raw/AKUR202606220000a.T02.gz"
        eng, dst = self._push(tmp_path, db_conn, rel, b"intact payload")
        digests = {rel: __import__("hashlib").sha256(b"intact payload").hexdigest()}

        stats = eng.verify_pushed(
            digests, read_root=str(dst), sessions={"15s_24hr"}, retries=0
        )
        assert stats.checked == 1
        assert stats.verified == 1
        assert stats.mismatched == 0
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT last_verified_at FROM archive_catalog "
                "WHERE storage_location='test_target'"
            )
            assert cur.fetchone()[0] is not None

    def test_tampered_archive_is_flagged_corrupt(self, tmp_path, db_conn):
        rel = "2026/jun/AKUR/15s_24hr/raw/AKUR202606220000a.T02.gz"
        eng, dst = self._push(tmp_path, db_conn, rel, b"good payload")
        # corrupt the archive copy AFTER push; local digest is the good one
        (dst / rel).write_bytes(b"different bytes entirely")
        digests = {rel: __import__("hashlib").sha256(b"good payload").hexdigest()}

        stats = eng.verify_pushed(
            digests, read_root=str(dst), sessions={"15s_24hr"}, retries=0
        )
        assert stats.mismatched == 1
        assert stats.verified == 0
        assert any("ARCHIVE CORRUPT" in f for f in stats.findings)

    def test_absent_archive_file_is_deferred(self, tmp_path, db_conn):
        rel = "2026/jun/AKUR/15s_24hr/raw/AKUR202606220000a.T02.gz"
        eng, dst = self._push(tmp_path, db_conn, rel, b"payload")
        (dst / rel).unlink()  # vanished from the mount (cache lag analogue)
        digests = {rel: __import__("hashlib").sha256(b"payload").hexdigest()}

        stats = eng.verify_pushed(
            digests, read_root=str(dst), sessions={"15s_24hr"}, retries=0
        )
        assert stats.deferred == 1
        assert stats.mismatched == 0
        assert stats.verified == 0

    def test_out_of_scope_session_skipped(self, tmp_path, db_conn):
        rel = "2026/jun/AKUR/15s_24hr/raw/AKUR202606220000a.T02.gz"
        eng, dst = self._push(tmp_path, db_conn, rel, b"payload")
        digests = {rel: __import__("hashlib").sha256(b"payload").hexdigest()}

        # only 1Hz_1hr in scope → the 15s file is not checked
        stats = eng.verify_pushed(
            digests, read_root=str(dst), sessions={"1Hz_1hr"}, retries=0
        )
        assert stats.checked == 0
        assert stats.verified == 0
