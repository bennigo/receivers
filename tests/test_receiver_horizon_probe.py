"""Unit tests for the per-receiver horizon probe (unified file index slice-2b.3).

Covers the parse/walk logic and the silent-under-report guards with mocked
FTP/HTTP listings — no network. Live end-to-end validation against real
receivers was done separately (AFST/ALFD/... on the laptop).
"""

from datetime import date

from receivers.scheduling import receiver_horizon_probe as hp
from receivers.utils.download_tracker import (
    record_receiver_horizon,
    upsert_receiver_horizon,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeFTP:
    """Minimal FTP whose ``nlst`` returns canned entries per path (full paths,
    to also exercise basename extraction). Supports both ``nlst(path)`` and
    ``cwd(path)`` + bare ``nlst()`` (the Leica space-path pattern)."""

    def __init__(self, tree):
        self.tree = tree
        self._cwd = None

    def cwd(self, path):
        if path not in self.tree:
            raise Exception(f"550 No such directory: {path}")
        self._cwd = path

    def nlst(self, path=None):
        path = path if path is not None else self._cwd
        if path not in self.tree:
            raise Exception(f"550 No such directory: {path}")
        return [path.rstrip("/") + "/" + n for n in self.tree[path]]


class FakeHTTP:
    """Minimal Trimble HTTP client: ``get_url`` returns a canned
    ``/prog/show?directory`` body per requested path."""

    def __init__(self, responses):
        self.responses = responses

    def get_url(self, endpoint, params=None):
        from urllib.parse import unquote

        path = unquote(endpoint.split("path=", 1)[1])
        text = self.responses.get(path, "ERROR: no such directory")
        return True, text, None


class FakeCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.store.append(params)


class FakeConn:
    def __init__(self):
        self.executed = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self.executed)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


# --------------------------------------------------------------------------- #
# %y%j day-directory parsing
# --------------------------------------------------------------------------- #
class TestParseYyjjj:
    def test_valid(self):
        assert hp._parse_yyjjj("26119") == date(2026, 4, 29)
        assert hp._parse_yyjjj("25188") == date(2025, 7, 7)
        assert hp._parse_yyjjj("26001") == date(2026, 1, 1)

    def test_int_sort_is_chronological_across_year(self):
        names = ["26001", "25188", "25365", "26119"]
        assert hp._sorted_numeric_subdirs(names) == [
            "25188",
            "25365",
            "26001",
            "26119",
        ]

    def test_rejects_gps_week_like_and_garbage(self):
        # GPS-week-style 5-digit name → doy 377 out of range → None (station keeps
        # its static floor rather than a wrong date).
        assert hp._parse_yyjjj("02377") is None
        assert hp._parse_yyjjj("26400") is None  # doy 400 invalid
        assert hp._parse_yyjjj("2611") is None  # wrong length
        assert hp._parse_yyjjj("abcde") is None


# --------------------------------------------------------------------------- #
# Septentrio: date from dir name, walk past empty oldest dir, hourly hour
# --------------------------------------------------------------------------- #
class TestSeptentrioOldest:
    def _parse(self):
        from receivers.utils.download_tracker import parse_date_from_filename

        return parse_date_from_filename

    def test_walks_past_empty_oldest_and_takes_dir_date_daily(self):
        index = "/DSK1/SSN/LOG1_15s_24hr/"
        tree = {
            index: [".", "..", "26120", "26119", "26200"],
            index + "26119/": [],  # stale empty shell — must be skipped
            # daily SBF/RINEX names that DON'T carry a full timestamp
            index + "26120/": ["AFST1200.26D.Z", "AFST120.26_.gz"],
            index + "26200/": ["AFST2000.26D.Z"],
        }
        ftp = FakeFTP(tree)
        day_dirs = hp._sorted_numeric_subdirs(
            [hp._basename(e) for e in ftp.nlst(index)]
        )
        horizon = hp._septentrio_oldest(
            "AFST", ftp, index, day_dirs, is_hourly=False, parse_fn=self._parse()
        )
        # date comes from the dir name 26120 = 2026 doy 120, hour None (daily)
        assert horizon == (date(2026, 4, 30), None)

    def test_hourly_refines_hour_from_filenames(self):
        index = "/DSK1/SSN/LOG2_1Hz_1hr/"
        tree = {
            index: ["26120"],
            index + "26120/": [
                "AFST202604300500b.sbf.gz",
                "AFST202604300100b.sbf.gz",
                "AFST202604300900b.sbf.gz",
            ],
        }
        ftp = FakeFTP(tree)
        day_dirs = hp._sorted_numeric_subdirs(
            [hp._basename(e) for e in ftp.nlst(index)]
        )
        horizon = hp._septentrio_oldest(
            "AFST", ftp, index, day_dirs, is_hourly=True, parse_fn=self._parse()
        )
        assert horizon == (date(2026, 4, 30), 1)  # min hour on the oldest date

    def test_all_empty_returns_none(self):
        index = "/DSK1/SSN/LOG1_15s_24hr/"
        tree = {index: ["26119", "26120"], index + "26119/": [], index + "26120/": []}
        ftp = FakeFTP(tree)
        day_dirs = hp._sorted_numeric_subdirs(
            [hp._basename(e) for e in ftp.nlst(index)]
        )
        assert (
            hp._septentrio_oldest(
                "AFST", ftp, index, day_dirs, is_hourly=False, parse_fn=self._parse()
            )
            is None
        )


# --------------------------------------------------------------------------- #
# Trimble: /prog/show?directory parsing, default + per-day layouts, NetRS no-op
# --------------------------------------------------------------------------- #
class TestTrimbleNames:
    def test_parses_directory_and_file_entries(self):
        idx = (
            "<show directory path=/Internal/>\n"
            "size      4294967296\n"
            "available 427841320\n"
            "directory name=202601\n"
            "directory name=202602\n"
            "directory name=lost+found\n"
            "<end of show directory>\n"
        )
        http = FakeHTTP({"/Internal/": idx})
        names = hp._trimble_names(http, "/Internal/")
        assert "202601" in names and "202602" in names and "lost+found" in names
        assert "." not in names and ".." not in names

    def test_netrs_error_response_yields_no_names(self):
        http = FakeHTTP(
            {"/download/": "ERROR: Invalid verb/object combination 'show directory'"}
        )
        assert hp._trimble_names(http, "/download/") == []


class TestTrimbleOldest:
    def _files(self, yyyymm, *days):
        return "".join(
            f"file name=SITE{yyyymm}{d:02d}0000a.T02 size=1 ctime=1 attr=1\n"
            for d in days
        )

    def test_default_monthly_layout(self):
        http = FakeHTTP(
            {
                "/Internal/": "directory name=202601\ndirectory name=202602\n",
                "/Internal/202601/": "directory name=15s_24hr\n",
                "/Internal/202601/15s_24hr/": self._files("202601", 18, 19, 20),
            }
        )
        month_dirs = ["202601", "202602"]
        h = hp._trimble_oldest("SITE", http, "/Internal", month_dirs, "15s_24hr")
        assert h == (date(2026, 1, 18), None)

    def test_per_day_varg_layout(self):
        # %Y%m/%d: DD dirs between month and session subdir
        http = FakeHTTP(
            {
                "/Internal/": "directory name=202601\n",
                "/Internal/202601/": "directory name=07\ndirectory name=05\n",
                "/Internal/202601/05/15s_24hr/": self._files("202601", 5),
                "/Internal/202601/07/15s_24hr/": self._files("202601", 7),
            }
        )
        h = hp._trimble_oldest("SITE", http, "/Internal", ["202601"], "15s_24hr")
        assert h == (date(2026, 1, 5), None)  # oldest DD walked first

    def test_walks_forward_when_oldest_month_leaf_empty(self):
        http = FakeHTTP(
            {
                "/Internal/": "directory name=202601\ndirectory name=202602\n",
                "/Internal/202601/": "directory name=15s_24hr\n",
                "/Internal/202601/15s_24hr/": "",  # empty
                "/Internal/202602/": "directory name=15s_24hr\n",
                "/Internal/202602/15s_24hr/": self._files("202602", 3),
            }
        )
        h = hp._trimble_oldest(
            "SITE", http, "/Internal", ["202601", "202602"], "15s_24hr"
        )
        assert h == (date(2026, 2, 3), None)


class TestFtpCwdNames:
    def test_cwd_then_bare_nlst_for_space_path(self):
        # The Leica G10 path has a space; cwd+bare-nlst must reach it.
        leaf = "/SD Card/Data/15s_24hr/"
        ftp = FakeFTP({leaf: ["SKFC188a.zip", "SKFC189a.zip"]})
        assert hp._ftp_cwd_names(ftp, leaf) == ["SKFC188a.zip", "SKFC189a.zip"]

    def test_missing_dir_returns_empty(self):
        assert hp._ftp_cwd_names(FakeFTP({}), "/SD Card/Data/nope/") == []


class TestLooksLikeFile:
    def test(self):
        assert hp._looks_like_file("SITE202601180000a.T02")
        assert not hp._looks_like_file("15s_24hr")
        assert not hp._looks_like_file(".")


# --------------------------------------------------------------------------- #
# Shared upsert: future-date guard, daily-hour handling, listing delegation
# --------------------------------------------------------------------------- #
class TestUpsertGuards:
    def test_past_date_written_daily_forces_null_hour(self):
        conn = FakeConn()
        ok = upsert_receiver_horizon(
            conn,
            "AFST",
            "15s_24hr",
            is_hourly=False,
            oldest_date=date(2025, 1, 1),
            oldest_hour=5,
        )
        assert ok is True and conn.committed
        (params,) = conn.executed
        # params = (sid, session, oldest_date, hour); daily → hour None
        assert params[2] == date(2025, 1, 1) and params[3] is None

    def test_hourly_keeps_hour(self):
        conn = FakeConn()
        upsert_receiver_horizon(
            conn,
            "AFST",
            "1Hz_1hr",
            is_hourly=True,
            oldest_date=date(2026, 6, 25),
            oldest_hour=1,
        )
        assert conn.executed[0][3] == 1

    def test_future_date_rejected(self):
        conn = FakeConn()
        ok = upsert_receiver_horizon(
            conn,
            "G10X",
            "15s_24hr",
            is_hourly=False,
            oldest_date=date(2999, 1, 1),
            oldest_hour=None,
        )
        assert ok is False
        assert conn.executed == [] and not conn.committed

    def test_none_date_rejected(self):
        conn = FakeConn()
        assert (
            upsert_receiver_horizon(conn, "X", "15s_24hr", False, None, None) is False
        )

    def test_record_from_listing_parses_then_upserts(self):
        conn = FakeConn()
        ok = record_receiver_horizon(
            conn,
            "ALFD",
            "15s_24hr",
            False,
            ["ALFD202601190000a.T02", "ALFD202601180000a.T02"],
        )
        assert ok is True
        assert conn.executed[0][2] == date(2026, 1, 18)  # min across the listing
