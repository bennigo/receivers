"""Tests for the targeted archive-sync selection mode (station/session/time window).

Covers the new ``ArchiveSync.enumerate_selection`` / ``ArchiveSync.select`` path
and the push_explicit dry-run guard added for side-effect-free previews.
"""

import os
from datetime import datetime
from pathlib import Path

import pytest

from receivers.archive.config import SyncTarget
from receivers.archive.engine import ArchiveSync
from receivers.archive.path_parse import parse_archive_path

CUTOVER = datetime(2026, 6, 22, 0, 0, 0)


def _target(tmp_root, **over):
    base = dict(
        name="imo_archive",
        active=True,
        tier="archive",
        host="",
        user="",
        dest=str(tmp_root / "dest"),
        source_root=str(tmp_root / "src"),
        sessions=("15s_24hr", "1Hz_1hr", "status_1hr"),
        file_categories=("raw", "rinex"),
        exclude_stations=frozenset({"DYNA"}),
        cutover=CUTOVER,
        overlap_minutes=5,
    )
    base.update(over)
    return SyncTarget(**base)


def _mkfile(src: Path, rel: str) -> str:
    p = src / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return str(p)


def _tree(tmp_path) -> Path:
    """A small archive tree with two stations, two sessions, and two months."""
    src = tmp_path / "src"
    _mkfile(src, "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz")
    _mkfile(src, "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z")
    _mkfile(src, "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz")
    _mkfile(src, "2026/aug/OTHER/15s_24hr/raw/OTHER202608120000a.sbf.gz")
    _mkfile(src, "2026/aug/DYNA/15s_24hr/raw/DYNA202608120000a.sbf.gz")
    _mkfile(src, "2026/jul/MYVA/15s_24hr/raw/MYVA202607120000a.sbf.gz")
    return src


# ---------------------------------------------------------------------- file window
class TestFileWindow:
    def test_daily_covers_whole_day(self):
        parsed = parse_archive_path(
            "/mnt/data/gpsdata/2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "/mnt/data/gpsdata",
        )
        lo, hi = ArchiveSync._file_window(parsed)
        assert lo == datetime(2026, 8, 12, 0, 0, 0)
        assert hi == datetime(2026, 8, 12, 23, 59, 59)

    def test_hourly_covers_single_hour(self):
        parsed = parse_archive_path(
            "/mnt/data/gpsdata/2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
            "/mnt/data/gpsdata",
        )
        lo, hi = ArchiveSync._file_window(parsed)
        assert lo == datetime(2026, 8, 12, 3, 0, 0)
        assert hi == datetime(2026, 8, 12, 3, 59, 59)


# ---------------------------------------------------------------------- enumeration
class TestEnumerateSelection:
    def _eng(self, tmp_path):
        t = _target(tmp_path)
        return ArchiveSync(t, conn=None, catalog_conns=[])

    def test_station_filter(self, tmp_path):
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src) for p in eng.enumerate_selection(station="MYVA")
        )
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
            "2026/jul/MYVA/15s_24hr/raw/MYVA202607120000a.sbf.gz",
        ]

    def test_session_filter(self, tmp_path):
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src) for p in eng.enumerate_selection(session="1Hz_1hr")
        )
        assert got == ["2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz"]

    def test_whole_day_window(self, tmp_path):
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src)
            for p in eng.enumerate_selection(
                station="MYVA",
                start=datetime(2026, 8, 12),
                end=datetime(2026, 8, 12, 23, 59, 59),
            )
        )
        # the jul file is excluded; the three aug-12 MYVA files remain
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
        ]

    def test_hour_window_overlaps_daily_files(self, tmp_path):
        """A 03:00 window overlaps daily files (whole day) AND the hourly hour-3 file."""
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src)
            for p in eng.enumerate_selection(
                station="MYVA",
                start=datetime(2026, 8, 12, 3),
                end=datetime(2026, 8, 12, 3),
            )
        )
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
        ]

    def test_date_window_without_station(self, tmp_path):
        """A date window (no station) walks only the in-range months."""
        src = _tree(tmp_path)
        eng = self._eng(tmp_path)
        got = sorted(
            os.path.relpath(p, src)
            for p in eng.enumerate_selection(
                start=datetime(2026, 8, 12), end=datetime(2026, 8, 12, 23, 59, 59)
            )
        )
        # aug-12 files from both real stations, excluding DYNA (exclude_stations)
        assert got == [
            "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz",
            "2026/aug/MYVA/15s_24hr/rinex/MYVA2240.26D.Z",
            "2026/aug/MYVA/1Hz_1hr/raw/MYVA202608120300b.sbf.gz",
            "2026/aug/OTHER/15s_24hr/raw/OTHER202608120000a.sbf.gz",
        ]

    def test_excluded_station_is_never_selected(self, tmp_path):
        _tree(tmp_path)
        eng = self._eng(tmp_path)
        assert eng.enumerate_selection(station="DYNA") == []

    def test_unknown_station_returns_empty(self, tmp_path):
        _tree(tmp_path)
        eng = self._eng(tmp_path)
        assert eng.enumerate_selection(station="ZZZZ") == []


# --------------------------------------------------------------------------- select
class TestSelect:
    def _eng(self, tmp_path, **kw):
        t = _target(tmp_path)
        return ArchiveSync(t, conn=None, catalog_conns=[], **kw)

    def test_select_pushes_without_advancing_watermark(self, tmp_path, monkeypatch):
        import receivers.archive.engine as eng_mod

        _tree(tmp_path)
        eng = self._eng(tmp_path)
        monkeypatch.setattr(eng, "push_explicit", lambda paths: (len(paths), [], {}))
        monkeypatch.setattr(
            eng_mod,
            "record_run",
            lambda *a, **k: pytest.fail("watermark must not advance"),
        )

        res = eng.select(
            station="MYVA",
            start=datetime(2026, 8, 12),
            end=datetime(2026, 8, 12, 23, 59, 59),
        )
        assert res.ok is True
        assert res.delta_count == 3
        assert res.transferred == 3
        assert res.cataloged == 3
        assert "watermark NOT advanced" in res.message

    def test_select_no_match_is_ok(self, tmp_path):
        _tree(tmp_path)
        eng = self._eng(tmp_path)
        res = eng.select(station="ZZZZ")
        assert res.ok is True
        assert res.delta_count == 0
        assert res.message == "0 files matched the selection"

    def test_select_dry_run_reports_zero_cataloged(self, tmp_path, monkeypatch):
        _tree(tmp_path)
        eng = self._eng(tmp_path, dry_run=True)
        monkeypatch.setattr(eng, "push_explicit", lambda paths: (len(paths), [], {}))
        res = eng.select(
            station="MYVA",
            start=datetime(2026, 8, 12),
            end=datetime(2026, 8, 12, 23, 59, 59),
        )
        assert res.transferred == 3
        assert res.cataloged == 0
        assert "would transfer" in res.message

    def test_select_inactive_target_skipped(self, tmp_path):
        _tree(tmp_path)
        t = _target(tmp_path, active=False)
        eng = ArchiveSync(t, conn=None, catalog_conns=[])
        res = eng.select(station="MYVA")
        assert res.ok is True
        assert res.message == "target inactive — skipped"


# ---------------------------------------------------------------- push dry-run guard
class TestPushExplicitDryRun:
    def test_dry_run_does_not_catalog(self, tmp_path, monkeypatch):
        src = _tree(tmp_path)
        eng = ArchiveSync(_target(tmp_path), conn=None, catalog_conns=[], dry_run=True)
        monkeypatch.setattr(
            eng, "_rsync", lambda rels, imm, **kw: (True, list(rels), "")
        )
        monkeypatch.setattr(
            eng,
            "_catalog_transferred",
            lambda *a, **k: pytest.fail("dry-run must not write the catalog"),
        )

        pushed, errors, _ = eng.push_explicit(
            [
                os.path.join(
                    str(src), "2026/aug/MYVA/15s_24hr/raw/MYVA202608120000a.sbf.gz"
                )
            ]
        )
        assert pushed == 1
        assert errors == []
