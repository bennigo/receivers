"""Tests for archive_catalog reindex (receivers.archive.reindex).

Reindex re-hashes a local staging-mirror file and upserts its catalog row so the
stored content_sha256 matches archive bytes edited out-of-band (the
``rinex --fix-headers --push`` case). These tests seed rows in a local
gps_health and assert updated / inserted / unchanged / only_existing behaviour.
"""

import gzip

import pytest

from receivers.archive.reindex import reindex_files
from receivers.utils.content_hash import content_sha256

REL = "2026/jun/ELDC/15s_24hr/raw/ELDC_a.sbf.gz"
LOC = "test_reindex"


def _local_conn():
    try:
        from receivers.db.connection import get_connection

        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.archive_catalog')")
            if cur.fetchone()[0] is None:
                conn.close()
                return None
        return conn
    except Exception:
        return None


@pytest.fixture
def db_conn():
    conn = _local_conn()
    if conn is None:
        pytest.skip("local gps_health with archive_catalog not available")
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM archive_catalog WHERE storage_location = %s", (LOC,))
    conn.commit()
    conn.close()


def _seed_gz(root, rel, payload):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb") as fh:
        fh.write(payload)
    return path, content_sha256(path)


def _seed_row(conn, rel, digest):
    from receivers.archive.catalog import upsert_catalog_row

    upsert_catalog_row(
        conn,
        storage_location=LOC,
        station="ELDC",
        session_type="15s_24hr",
        file_category="raw",
        file_date=None,
        archive_path=f"~/gpsdata/{rel}",
        filename=rel.split("/")[-1],
        file_size=1,
        content_sha256=digest,
    )
    conn.commit()


def _catalog_sha(conn, key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_sha256 FROM archive_catalog "
            "WHERE storage_location=%s AND canonical_key=%s",
            (LOC, key),
        )
        row = cur.fetchone()
    return row[0] if row else None


def test_updated_refreshes_stale_hash(db_conn, tmp_path):
    # Seed a row with a wrong (stale) hash, then reindex the real file.
    _seed_row(db_conn, REL, "0" * 64)
    _, real = _seed_gz(tmp_path, REL, b"corrected header payload" * 50)

    stats = reindex_files(
        db_conn,
        [str(tmp_path / REL)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
    )
    assert (stats.updated, stats.inserted, stats.unchanged) == (1, 0, 0)
    assert _catalog_sha(db_conn, "eldc_a.sbf") == real


def test_inserted_when_no_prior_row(db_conn, tmp_path):
    _, real = _seed_gz(tmp_path, REL, b"brand new file" * 50)
    stats = reindex_files(
        db_conn,
        [str(tmp_path / REL)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
    )
    assert (stats.updated, stats.inserted, stats.unchanged) == (0, 1, 0)
    assert _catalog_sha(db_conn, "eldc_a.sbf") == real


def test_only_existing_skips_insert(db_conn, tmp_path):
    _seed_gz(tmp_path, REL, b"unseen file" * 50)
    stats = reindex_files(
        db_conn,
        [str(tmp_path / REL)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
        only_existing=True,
    )
    assert (stats.updated, stats.inserted, stats.skipped_new) == (0, 0, 1)
    assert _catalog_sha(db_conn, "eldc_a.sbf") is None  # nothing written


def test_unchanged_when_hash_matches(db_conn, tmp_path):
    _, real = _seed_gz(tmp_path, REL, b"same content" * 50)
    _seed_row(db_conn, REL, real)
    stats = reindex_files(
        db_conn,
        [str(tmp_path / REL)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
    )
    assert (stats.updated, stats.inserted, stats.unchanged) == (0, 0, 1)


def test_dry_run_writes_nothing(db_conn, tmp_path):
    _seed_row(db_conn, REL, "0" * 64)
    _seed_gz(tmp_path, REL, b"would-fix payload" * 50)
    stats = reindex_files(
        db_conn,
        [str(tmp_path / REL)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
        dry_run=True,
    )
    assert stats.updated == 1
    assert _catalog_sha(db_conn, "eldc_a.sbf") == "0" * 64  # unchanged on disk


class TestCatalogHostResolution:
    def test_override_single(self):
        from receivers.archive.reindex import resolve_catalog_hosts

        assert resolve_catalog_hosts("pgdev.vedur.is") == ["pgdev.vedur.is"]

    def test_override_comma_list(self):
        from receivers.archive.reindex import resolve_catalog_hosts

        assert resolve_catalog_hosts("a.is, b.is , c.is") == ["a.is", "b.is", "c.is"]

    def test_default_is_local_not_config(self, monkeypatch):
        """No flag → [None] (database.cfg default), NEVER the config prod set —
        so a dev run can't silently write production."""
        import receivers.archive.reindex as rx

        class _Cfg:
            def get_catalog_hosts(self):
                return ["pgdev.vedur.is", "rek-d01.vedur.is"]

        monkeypatch.setattr(
            "receivers.config.receivers_config.get_receivers_config", lambda: _Cfg()
        )
        assert rx.resolve_catalog_hosts(None) == [None]

    def test_prod_uses_config_hosts(self, monkeypatch):
        import receivers.archive.reindex as rx

        class _Cfg:
            def get_catalog_hosts(self):
                return ["rek-d01.vedur.is", "pgdev.vedur.is"]

        monkeypatch.setattr(
            "receivers.config.receivers_config.get_receivers_config", lambda: _Cfg()
        )
        assert rx.resolve_catalog_hosts(None, prod=True) == [
            "rek-d01.vedur.is",
            "pgdev.vedur.is",
        ]

    def test_prod_empty_config_returns_empty_not_localhost(self, monkeypatch):
        """--catalog-prod with unset catalog_hosts → [] so the caller errors,
        rather than silently falling back to localhost (dev)."""
        import receivers.archive.reindex as rx

        class _Cfg:
            def get_catalog_hosts(self):
                return []

        monkeypatch.setattr(
            "receivers.config.receivers_config.get_receivers_config", lambda: _Cfg()
        )
        assert rx.resolve_catalog_hosts(None, prod=True) == []


def test_reindex_files_multi_fans_out(monkeypatch, tmp_path):
    """reindex_files_multi opens a connection per host and reindexes each."""
    import receivers.archive.reindex as rx

    seen_hosts = []
    monkeypatch.setattr(
        "receivers.db.connection.get_connection",
        lambda host_override=None: seen_hosts.append(host_override) or object(),
    )
    monkeypatch.setattr(
        rx,
        "reindex_files",
        lambda conn, files, **kw: rx.ReindexStats(updated=len(files)),
    )
    results = rx.reindex_files_multi(
        ["pgdev.vedur.is", "rek-d01.vedur.is"],
        ["a", "b"],
        root="/w",
        storage_location="imo_archive",
        dest_prefix="~/gpsdata",
    )
    assert seen_hosts == ["pgdev.vedur.is", "rek-d01.vedur.is"]
    assert set(results) == {"pgdev.vedur.is", "rek-d01.vedur.is"}
    assert all(s.updated == 2 for s in results.values())


def test_reindex_files_multi_records_per_host_failure(monkeypatch):
    import receivers.archive.reindex as rx

    def _conn(host_override=None):
        if host_override == "bad.host":
            raise OSError("refused")
        return object()

    monkeypatch.setattr("receivers.db.connection.get_connection", _conn)
    monkeypatch.setattr(
        rx, "reindex_files", lambda conn, files, **kw: rx.ReindexStats()
    )
    results = rx.reindex_files_multi(
        ["good.host", "bad.host"],
        ["a"],
        root="/w",
        storage_location="imo_archive",
        dest_prefix="~/gpsdata",
    )
    assert results["good.host"] is not None
    assert results["bad.host"] is None  # failure surfaced, not swallowed


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        return None

    def fetchone(self):
        return (1,)


class _FakeConn:
    closed = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        self.closed = True


def test_preflight_all_reachable(monkeypatch):
    """Every host SELECT 1s cleanly → all-None (no errors), None host → localhost."""
    import receivers.archive.reindex as rx

    monkeypatch.setattr(
        "receivers.db.connection.get_connection",
        lambda host_override=None: _FakeConn(),
    )
    res = rx.preflight_catalog_hosts(["pgdev.vedur.is", None])
    assert res == {"pgdev.vedur.is": None, "localhost": None}


def test_preflight_records_unreachable(monkeypatch):
    """An unreachable host is reported (error string), a good one stays None."""
    import receivers.archive.reindex as rx

    def _conn(host_override=None):
        if host_override == "bad.host":
            raise OSError("connection refused")
        return _FakeConn()

    monkeypatch.setattr("receivers.db.connection.get_connection", _conn)
    res = rx.preflight_catalog_hosts(["good.host", "bad.host"])
    assert res["good.host"] is None
    assert res["bad.host"] is not None
    assert "refused" in res["bad.host"]


def test_rinex_org_catalogs_as_distinct_row(db_conn, tmp_path):
    # A rinex_org preservation shares the rinex file's canonical_key but parses
    # to file_category='rinex_org' → a SEPARATE catalog row, no collision.
    rnx_rel = "2026/jun/RHOF/15s_24hr/rinex/RHOF1720.26D.Z"
    org_rel = "2026/jun/RHOF/15s_24hr/rinex_org/RHOF1720.26D.Z"
    _, rnx_sha = _seed_gz(tmp_path, rnx_rel, b"fixed content")
    _, org_sha = _seed_gz(tmp_path, org_rel, b"original content")
    stats = reindex_files(
        db_conn,
        [str(tmp_path / rnx_rel), str(tmp_path / org_rel)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
    )
    assert stats.inserted == 2  # two distinct (file_category) rows
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT file_category, content_sha256 FROM archive_catalog "
            "WHERE storage_location=%s AND canonical_key='rhof1720.26d' "
            "ORDER BY file_category",
            (LOC,),
        )
        rows = {c: s for c, s in cur.fetchall()}
    assert rows["rinex"] == rnx_sha
    assert rows["rinex_org"] == org_sha  # preserved original, distinct + monitored


def test_unparsable_path_skipped(db_conn, tmp_path):
    # A file not in archive layout can't be parsed to an identity.
    stray = tmp_path / "stray.txt"
    stray.write_text("nope")
    stats = reindex_files(
        db_conn,
        [str(stray)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
    )
    assert stats.skipped == 1
    assert stats.touched == 0


# ---- _push_reindex glue (the `rinex --fix-headers --push --reindex` entry) ---
class TestPushReindexGlue:
    """The flag entry point wires args + staged paths into reindex_files."""

    def test_no_flag_prints_hint_only(self, capsys):
        import types

        from receivers.cli.main import _push_reindex

        args = types.SimpleNamespace(reindex=False, catalog_host=None)
        # No DB touched; must not raise and must point at the reindex verb.
        _push_reindex(
            args,
            ["/whatever"],
            root="/tmp/x",
            storage_location="imo_archive",
            dest_prefix="~/gpsdata",
        )
        out = capsys.readouterr().out
        assert "archive-reindex" in out
        assert "archive-sync --status" not in out

    def test_flag_reindexes_staged_file(self, db_conn, tmp_path, capsys):
        import types

        from receivers.cli.main import _push_reindex

        _seed_row(db_conn, REL, "0" * 64)  # stale row
        _seed_gz(tmp_path, REL, b"pushed corrected payload" * 50)
        args = types.SimpleNamespace(reindex=True, catalog_host=None)  # localhost
        _push_reindex(
            args,
            [str(tmp_path / REL)],
            root=str(tmp_path),
            storage_location=LOC,
            dest_prefix="~/gpsdata",
        )
        out = capsys.readouterr().out
        assert "reindexed archive_catalog" in out
        assert "1 updated" in out
        # Row now holds the real hash, not the stale zeros.
        assert _catalog_sha(db_conn, "eldc_a.sbf") != "0" * 64

    def test_flag_missing_storage_location_skips(self, capsys):
        import types

        from receivers.cli.main import _push_reindex

        args = types.SimpleNamespace(reindex=True, catalog_host=None)
        _push_reindex(
            args,
            ["/whatever"],
            root="/tmp/x",
            storage_location=None,
            dest_prefix=None,
        )
        assert "no archive storage_location" in capsys.readouterr().out
