"""Binary raw-format identification + misfiled-file sorter (.atc findings).

Covers the three checks from vault todo #56: magic-byte format dispatch,
decoded-date vs filename-date validation, and the guarded relocation plan
for misfiled batches (RHOF 2000/2001 holding 2010/2011 data).
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.archive import raw_format, sort
from receivers.archive.raw_format import (
    ASHTECH_R,
    ASHTECH_U,
    SBF,
    TRIMBLE,
    UNKNOWN,
    build_raw_name,
    classify_raw,
    parse_raw_name,
)
from receivers.archive.relocate import relocate_archive_files
from receivers.archive.sort import plan_relocations

# ── magic-byte classification ────────────────────────────────────────────────


class TestClassifyRaw:
    def test_sbf_magic(self):
        assert classify_raw(head=b"$@Sic\x00\x00\x00" + b"\x00" * 56) == SBF

    def test_ashtech_u_bhdr_at_offset_4(self):
        head = b"\x00\x00\x00\x30BHDRVersion: UZ-12" + b"\x00" * 40
        assert classify_raw(head=head) == ASHTECH_U

    def test_ashtech_r_z12_prefix(self):
        assert classify_raw(head=b"Z-12\x00 receiver dump" + b"\x00" * 44) == ASHTECH_R

    def test_trimble_by_extension_only(self, tmp_path):
        f = tmp_path / "RHOF201804010000a.T02"
        f.write_bytes(b"\x00\x00\x00\x0dtry" + b"\x00" * 500)
        assert classify_raw(f) == TRIMBLE

    def test_unknown(self):
        assert classify_raw(head=b"\x00" * 64) == UNKNOWN

    def test_gzip_transparent(self, tmp_path):
        import gzip as _gzip

        f = tmp_path / "HUSM202606270000a.sbf.gz"
        f.write_bytes(_gzip.compress(b"$@Sic" + b"\x00" * 100))
        assert classify_raw(f) == SBF

    def test_mislabeled_atc_with_sbf_content(self, tmp_path):
        # KOSK case: .atc extension, SBF bytes — content wins.
        f = tmp_path / "KOSK201301010000a.atc"
        f.write_bytes(b"$@Sic" + b"\x00" * 100)
        assert classify_raw(f) == SBF


# ── filename parse / rebuild ─────────────────────────────────────────────────


class TestRawName:
    def test_parse(self):
        p = parse_raw_name("RHOF200004010000a.atc")
        assert p is not None
        assert p.station == "RHOF"
        assert p.claimed == datetime(2000, 4, 1)
        assert p.session_letter == "a"
        assert p.ext == "atc"

    def test_parse_gz(self):
        p = parse_raw_name("HUSM202606270000a.sbf.gz")
        assert p is not None and p.ext == "sbf.gz"

    def test_parse_rejects_garbage(self):
        assert parse_raw_name("RHOF0970.18D.Z") is None
        assert parse_raw_name("RHOF200013990000a.atc") is None  # month 13

    def test_build_corrected_name(self):
        p = parse_raw_name("RHOF200004010000a.atc")
        assert p is not None
        # the misfiled batch: claims 2000-04-01, decodes to 2010-04-02
        assert build_raw_name(p, datetime(2010, 4, 2)) == "RHOF201004020000a.atc"


# ── relocation planning ──────────────────────────────────────────────────────


def _mk(root: Path, rel: str, head: bytes, size: int = 8192) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(head + b"\x00" * max(0, size - len(head)))
    return rel


class TestPlanRelocations:
    def test_misfiled_planned_correct_and_stub_skipped(self, tmp_path, monkeypatch):
        misfiled = _mk(
            tmp_path,
            "2000/apr/RHOF/15s_24hr/raw/RHOF200004010000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        correct = _mk(
            tmp_path,
            "2010/sep/ARHO/15s_24hr/raw/ARHO201009010000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        stub = _mk(
            tmp_path,
            "2001/oct/RHOF/15s_24hr/raw/RHOF200110010000a.atc",
            b"\x00\x00\x00\x30BHDR",
            size=100,
        )

        from receivers.archive.raw_format import RawMeta

        spans = {
            "RHOF200004010000a.atc": RawMeta(
                start=datetime(2010, 4, 2), end=datetime(2010, 4, 2, 23)
            ),
            "ARHO201009010000a.atc": RawMeta(
                start=datetime(2010, 9, 1), end=datetime(2010, 9, 1, 23)
            ),
        }

        def fake_meta(path, fmt, **kw):
            return spans.get(Path(path).name)

        monkeypatch.setattr(sort, "teqc_meta", fake_meta)
        plans, skips = plan_relocations(tmp_path, [misfiled, correct, stub])

        assert len(plans) == 1
        p = plans[0]
        assert p.src_rel == misfiled
        assert p.dst_rel == "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.atc"
        assert p.fmt == ASHTECH_U
        reasons = {s.rel: s.reason for s in skips}
        assert reasons[correct] == "verified-correct"
        assert reasons[stub] == "stub"

    def test_no_date_decoder_skips_trimble(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2018/apr/RHOF/15s_24hr/raw/RHOF201804010000a.T02",
            b"\x00\x00\x00\x0d",
        )
        plans, skips = plan_relocations(tmp_path, [rel])
        assert not plans
        assert skips[0].reason == "no-date-decoder"

    def test_decode_failure_never_plans_a_move(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2000/apr/RHOF/15s_24hr/raw/RHOF200004010000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        monkeypatch.setattr(sort, "teqc_meta", lambda *a, **k: None)
        plans, skips = plan_relocations(tmp_path, [rel])
        assert not plans
        assert skips[0].reason == "decode-failed"


# ── guarded relocation (gateway) ─────────────────────────────────────────────


class _Proc:
    def __init__(self, stdout, rc=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = rc


class TestRelocateArchiveFiles:
    SRC = "2000/apr/RHOF/15s_24hr/raw/RHOF200004010000a.atc"
    DST = "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.atc"

    def test_invalid_paths_refused_no_ssh(self):
        with patch("subprocess.run") as m:
            res = relocate_archive_files(
                [("../etc/passwd", self.DST)],
                ssh_target="gpsops@rawdata",
                dest_root="~/gpsdata",
            )
        m.assert_not_called()
        assert res.invalid and not res.ok

    def test_dry_run_parses_would_move(self):
        out = f"WOULD_MOVE|{self.SRC}|{self.DST}\n"
        with patch("subprocess.run", return_value=_Proc(out)) as m:
            res = relocate_archive_files(
                [(self.SRC, self.DST)],
                ssh_target="gpsops@rawdata",
                dest_root="~/gpsdata",
            )
        assert res.would_move == [(self.SRC, self.DST)]
        assert res.ok
        # dry-run flag ("0") on the argv boundary, pairs as argv
        cmd = m.call_args.args[0]
        assert "0" in cmd and self.SRC in cmd and self.DST in cmd

    def test_existing_destination_never_replaced(self):
        out = f"SKIP_EXISTS|{self.SRC}|{self.DST}\n"
        with patch("subprocess.run", return_value=_Proc(out)):
            res = relocate_archive_files(
                [(self.SRC, self.DST)],
                ssh_target="gpsops@rawdata",
                dest_root="~/gpsdata",
                execute=True,
            )
        assert res.dst_exists == [(self.SRC, self.DST)]
        assert not res.moved and res.ok

    def test_moved_and_failed_classified(self):
        out = f"MOVED|{self.SRC}|{self.DST}\nFAIL|{self.DST}|{self.SRC}\n"
        with patch("subprocess.run", return_value=_Proc(out)):
            res = relocate_archive_files(
                [(self.SRC, self.DST), (self.DST, self.SRC)],
                ssh_target="gpsops@rawdata",
                dest_root="~/gpsdata",
                execute=True,
            )
        assert res.moved == [(self.SRC, self.DST)]
        assert res.failed and not res.ok


# ── dissemination decoder dispatch by magic ──────────────────────────────────


class TestDecodeRawMagicDispatch:
    def test_mislabeled_sbf_atc_routes_to_sbf(self, tmp_path):
        from receivers.dissemination import convert

        f = tmp_path / "KOSK201301010000a.atc"
        f.write_bytes(b"$@Sic" + b"\x00" * 200)
        with patch.object(convert, "_decode_sbf_raw", return_value=Path("x")) as sbf:
            convert._decode_raw(f, "KOSK", datetime(2013, 1, 1), tmp_path)
        sbf.assert_called_once()

    def test_ashtech_raises_instead_of_wrong_decoder(self, tmp_path):
        from receivers.dissemination import convert
        from receivers.dissemination.convert import ConversionError

        f = tmp_path / "RHOF201004020000a.atc"
        f.write_bytes(b"\x00\x00\x00\x30BHDR" + b"\x00" * 200)
        with pytest.raises(ConversionError, match="ashtech_u"):
            convert._decode_raw(f, "RHOF", datetime(2010, 4, 2), tmp_path)

    def test_t02_still_routes_to_trimble(self, tmp_path):
        from receivers.dissemination import convert

        f = tmp_path / "RHOF201804010000a.T02"
        f.write_bytes(b"\x00\x00\x00\x0d" + b"\x00" * 200)
        with patch.object(
            convert, "_decode_trimble_raw", return_value=Path("x")
        ) as trm:
            convert._decode_raw(f, "RHOF", datetime(2018, 4, 1), tmp_path)
        trm.assert_called_once()

    def test_sbf_gz_by_extension_still_works(self, tmp_path):
        import gzip as _gzip

        from receivers.dissemination import convert

        f = tmp_path / "HUSM202606270000a.sbf.gz"
        f.write_bytes(_gzip.compress(b"$@Sic" + b"\x00" * 100))
        with patch.object(convert, "_decode_sbf_raw", return_value=Path("x")) as sbf:
            convert._decode_raw(f, "HUSM", datetime(2026, 6, 27), tmp_path)
        sbf.assert_called_once()


# ── decoded_span parsing (teqc output, no binary needed) ─────────────────────


class TestDecodedSpan:
    def test_parses_teqc_meta_output(self, tmp_path, monkeypatch):
        meta = (
            "start date & time:       2010-04-02 00:00:00.000\n"
            "final date & time:       2010-04-02 23:59:45.000\n"
        )
        monkeypatch.setattr(raw_format, "subprocess", _SubprocessStub(_Proc(meta)))
        with patch("receivers.dissemination.convert.resolve_tool", return_value="teqc"):
            span = raw_format.decoded_span(tmp_path / "f.atc", ASHTECH_U)
        assert span == (datetime(2010, 4, 2), datetime(2010, 4, 2, 23, 59, 45))

    def test_trimble_has_no_decoder(self, tmp_path):
        assert raw_format.decoded_span(tmp_path / "f.T02", TRIMBLE) is None


class _SubprocessStub:
    def __init__(self, proc):
        self._proc = proc

    def run(self, *a, **k):
        return self._proc


class TestRelocateGatewayReset:
    SRC1 = "2000/apr/RHOF/15s_24hr/raw/RHOF200004010000a.atc"
    DST1 = "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.atc"
    SRC2 = "2001/oct/RHOF/15s_24hr/raw/RHOF200110190000a.atc"
    DST2 = "2011/oct/RHOF/15s_24hr/raw/RHOF201110190334a.atc"

    def test_partial_output_marks_unreported_and_not_ok(self):
        """A mid-stream ssh reset (some lines + rc=255) must NEVER read as
        success — the silent-partial that bit the live 2026-07-06 run."""
        out = f"WOULD_MOVE|{self.SRC1}|{self.DST1}\n"  # second pair: no status
        with patch("subprocess.run", return_value=_Proc(out, rc=255)):
            res = relocate_archive_files(
                [(self.SRC1, self.DST1), (self.SRC2, self.DST2)],
                ssh_target="gpsops@rawdata",
                dest_root="~/gpsdata",
            )
        assert res.would_move == [(self.SRC1, self.DST1)]
        assert res.unreported == [(self.SRC2, self.DST2)]
        assert not res.ok

    def test_full_output_ok(self):
        out = (
            f"WOULD_MOVE|{self.SRC1}|{self.DST1}\n"
            f"WOULD_MOVE|{self.SRC2}|{self.DST2}\n"
        )
        with patch("subprocess.run", return_value=_Proc(out)):
            res = relocate_archive_files(
                [(self.SRC1, self.DST1), (self.SRC2, self.DST2)],
                ssh_target="gpsops@rawdata",
                dest_root="~/gpsdata",
            )
        assert not res.unreported and res.ok


class TestStationAndExtRemediation:
    """The full remediation dimensions: wrong station (position decides) and
    wrong extension (content decides)."""

    FLEET = {"RHOF": (66.461123, -15.946707), "REYK": (64.1388, -21.9555)}

    def _meta(self, lat=None, lon=None, start=None):
        from receivers.archive.raw_format import RawMeta

        return RawMeta(
            start=start or datetime(2010, 4, 2),
            end=datetime(2010, 4, 2, 23),
            lat=lat,
            lon=lon,
        )

    def test_wrong_station_relocates_by_position(self, tmp_path, monkeypatch):
        # filed under REYK, but the antenna position is RHOF's mark
        rel = _mk(
            tmp_path,
            "2010/apr/REYK/15s_24hr/raw/REYK201004020000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(
            sort, "teqc_meta", lambda *a, **k: self._meta(66.46113, -15.94671)
        )
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert len(plans) == 1
        p = plans[0]
        assert p.reasons == ("wrong-station",)
        assert p.true_station == "RHOF"
        assert p.dst_rel == "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.atc"
        assert p.station_dist_m is not None and p.station_dist_m < 50

    def test_unknown_position_reported_never_moved(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(
            sort, "teqc_meta", lambda *a, **k: self._meta(51.0, -1.0)  # not Iceland
        )
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert not plans
        assert skips[0].reason == "unknown-station"

    def test_matching_station_and_date_verified(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(
            sort, "teqc_meta", lambda *a, **k: self._meta(66.46113, -15.94671)
        )
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert not plans and skips[0].reason == "verified-correct"

    def test_wrong_ext_renamed_to_content(self, tmp_path, monkeypatch):
        # KOSK case: SBF bytes under .atc → rename to .sbf (same date/station)
        rel = _mk(
            tmp_path,
            "2013/jan/KOSK/15s_24hr/raw/KOSK201301010000a.atc",
            b"$@Sic",
        )
        monkeypatch.setattr(
            sort,
            "teqc_meta",
            lambda *a, **k: self._meta(start=datetime(2013, 1, 1)),
        )
        plans, skips = plan_relocations(tmp_path, [rel])
        assert len(plans) == 1
        assert plans[0].reasons == ("wrong-ext",)
        assert plans[0].dst_rel == "2013/jan/KOSK/15s_24hr/raw/KOSK201301010000a.sbf"

    def test_combined_wrong_everything(self, tmp_path, monkeypatch):
        # SBF bytes, wrong date, filed under wrong station
        rel = _mk(
            tmp_path,
            "2000/apr/REYK/15s_24hr/raw/REYK200004010000a.atc",
            b"$@Sic",
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(
            sort, "teqc_meta", lambda *a, **k: self._meta(66.46113, -15.94671)
        )
        plans, _ = plan_relocations(tmp_path, [rel], verify_station=True)
        assert len(plans) == 1
        p = plans[0]
        assert set(p.reasons) == {"wrong-station", "wrong-date", "wrong-ext"}
        assert p.dst_rel == "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.sbf"


class TestStationFirstScan:
    def test_scan_station_raw_by_years(self, tmp_path):
        from receivers.archive.sort import scan_station_raw

        _mk(tmp_path, "2008/apr/RHOF/15s_24hr/raw/RHOF200804010000a.atc", b"x")
        _mk(tmp_path, "2009/jul/RHOF/15s_24hr/raw/RHOF200907130000a.atc", b"x")
        _mk(tmp_path, "2008/apr/REYK/15s_24hr/raw/REYK200804010000a.sbf", b"x")
        _mk(tmp_path, "2008/apr/RHOF/1Hz_1hr/raw/RHOF200804010600b.sbf", b"x")

        all_rhof = scan_station_raw(tmp_path, "rhof")
        assert len(all_rhof) == 2  # both years, only RHOF 15s_24hr
        only_2008 = scan_station_raw(tmp_path, "RHOF", years=[2008])
        assert only_2008 == ["2008/apr/RHOF/15s_24hr/raw/RHOF200804010000a.atc"]
        hz = scan_station_raw(tmp_path, "RHOF", "1Hz_1hr")
        assert len(hz) == 1

    def test_noisy_same_station_is_informational(self, tmp_path, monkeypatch):
        """~100 m from the CLAIMED station's own mark = degraded solution,
        classified position-noisy — not unknown-station, never moved."""
        from receivers.archive.raw_format import RawMeta

        rel = _mk(
            tmp_path,
            "2009/nov/RHOF/15s_24hr/raw/RHOF200911250000a.atc",
            b"\x00\x00\x00\x30BHDR",
        )
        fleet = {"RHOF": (66.461123, -15.946707), "REYK": (64.1388, -21.9555)}
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: fleet)
        monkeypatch.setattr(
            sort,
            "teqc_meta",
            lambda *a, **k: RawMeta(
                start=datetime(2009, 11, 25),
                end=datetime(2009, 11, 25, 23),
                lat=66.46059,
                lon=-15.94597,
            ),
        )
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert not plans
        assert skips[0].reason == "position-noisy"
        assert "as filed" in skips[0].detail

    def test_t02_path_name_mismatch_flagged_without_decode(self, tmp_path):
        """A T02 (no cheap decoder) whose NAME claims a different year/month
        than its directory is flagged path-name-mismatch — the 324 MB
        'RHOF202101031833a.T02 in 2017/dec' class."""
        rel = _mk(
            tmp_path,
            "2017/dec/RHOF/15s_24hr/raw/RHOF202101031833a.T02",
            b"\x00\x00\x00\x0d",
        )
        plans, skips = plan_relocations(tmp_path, [rel])
        assert not plans
        assert skips[0].reason == "path-name-mismatch"
        assert "2021-01-03" in skips[0].detail


# ── RINEX APPROX POSITION fallback (todo #151) ──────────────────────────────


def _ecef(lat: float, lon: float) -> tuple:
    """ECEF metres for a lat/lon — the inverse of sort._ecef_to_latlon."""
    import pyproj

    tr = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    x, y, z = tr.transform(lon, lat, 0.0)
    return x, y, z


def _rinex_bytes(station: str, lat: float, lon: float) -> bytes:
    x, y, z = _ecef(lat, lon)
    return (
        f"     2.11           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE\n"
        f"{station}                                                        MARKER NAME\n"
        f"{x:14.4f}{y:14.4f}{z:14.4f}                  APPROX POSITION XYZ\n"
        "                                                            END OF HEADER\n"
    ).encode()


def _mk_rinex(root: Path, rel: str, content: bytes) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return rel


class TestRinexPositionFallback:
    """Septentrio SBF raw decodes to the teqc (90,0) placeholder — the station
    identity must fall back to the sibling RINEX header's APPROX POSITION."""

    FLEET = {"RHOF": (66.461123, -15.946707), "REYK": (64.1388, -21.9555)}

    def _sbf_meta(self, lat=90.0, lon=0.0):
        from receivers.archive.raw_format import RawMeta

        return RawMeta(
            start=datetime(2010, 4, 2),
            end=datetime(2010, 4, 2, 23),
            lat=lat,
            lon=lon,
        )

    def test_rinex_name_matching(self):
        claimed = datetime(2010, 4, 2)  # doy 92
        # R2 short Hatanaka
        assert sort._rinex_name_matches_date("RHOF0920.10D.Z", "RHOF", claimed)
        assert sort._rinex_name_matches_date("RHOF0920.10D", "RHOF", claimed)
        # R3 long IGS name
        assert sort._rinex_name_matches_date(
            "RHOF00ISL_R_20100920000_01D_15S_MO.crx.Z", "RHOF", claimed
        )
        # wrong day / wrong station / day-digit run-in
        assert not sort._rinex_name_matches_date("RHOF0930.10D.Z", "RHOF", claimed)
        assert not sort._rinex_name_matches_date("REYK0920.10D.Z", "RHOF", claimed)
        assert not sort._rinex_name_matches_date(
            "RHOF00ISL_R_20100930000_01D_15S_MO.crx.Z", "RHOF", claimed
        )

    def test_sbf_placeholder_confirmed_via_rinex(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.sbf.gz",
            _gzip_compress(b"$@Sic" + b"\x00" * 32),
        )
        _mk_rinex(
            tmp_path,
            "2010/apr/RHOF/15s_24hr/rinex/RHOF0920.10D",
            _rinex_bytes("RHOF", 66.461123, -15.946707),
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(sort, "teqc_meta", lambda *a, **k: self._sbf_meta())
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert not plans
        assert skips[0].reason == "verified-correct"
        assert "RINEX" in skips[0].detail

    def test_sbf_placeholder_rinex_reveals_stray_and_co_moves(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2010/apr/REYK/15s_24hr/raw/REYK201004020000a.sbf.gz",
            _gzip_compress(b"$@Sic" + b"\x00" * 32),
        )
        _mk_rinex(
            tmp_path,
            "2010/apr/REYK/15s_24hr/rinex/REYK0920.10D",
            _rinex_bytes("REYK", 66.461123, -15.946707),  # RHOF's mark, filed REYK
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(sort, "teqc_meta", lambda *a, **k: self._sbf_meta())
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert len(plans) == 2  # the raw + the stray RINEX that proved it
        raw, rx = plans
        assert raw.reasons == ("wrong-station",)
        assert raw.true_station == "RHOF"
        assert raw.dst_rel == "2010/apr/RHOF/15s_24hr/raw/RHOF201004020000a.sbf.gz"
        assert "RINEX header" in raw.evidence
        assert rx.fmt == "rinex"
        assert rx.dst_rel == "2010/apr/RHOF/15s_24hr/rinex/RHOF0920.10D"
        assert rx.src_rel == "2010/apr/REYK/15s_24hr/rinex/REYK0920.10D"

    def test_no_sibling_rinex_still_unknown_station(self, tmp_path, monkeypatch):
        rel = _mk(
            tmp_path,
            "2010/apr/REYK/15s_24hr/raw/REYK201004020000a.sbf.gz",
            _gzip_compress(b"$@Sic" + b"\x00" * 32),
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        monkeypatch.setattr(sort, "teqc_meta", lambda *a, **k: self._sbf_meta())
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert not plans
        assert skips[0].reason == "unknown-station"

    def test_date_mismatch_suppresses_rinex_co_move(self, tmp_path, monkeypatch):
        # wrong-station (via RINEX) AND wrong-date: the RINEX name's date would
        # also be wrong, so it is left for eyes — only the raw moves.
        rel = _mk(
            tmp_path,
            "2010/apr/REYK/15s_24hr/raw/REYK201004020000a.sbf.gz",
            _gzip_compress(b"$@Sic" + b"\x00" * 32),
        )
        _mk_rinex(
            tmp_path,
            "2010/apr/REYK/15s_24hr/rinex/REYK0920.10D",
            _rinex_bytes("REYK", 66.461123, -15.946707),
        )
        monkeypatch.setattr(sort, "fleet_coordinates", lambda: self.FLEET)
        from receivers.archive.raw_format import RawMeta

        meta = RawMeta(
            start=datetime(2010, 4, 3),  # decodes one day later than claimed
            end=datetime(2010, 4, 3, 23),
            lat=90.0,
            lon=0.0,
        )
        monkeypatch.setattr(sort, "teqc_meta", lambda *a, **k: meta)
        plans, skips = plan_relocations(tmp_path, [rel], verify_station=True)
        assert len(plans) == 1
        assert set(plans[0].reasons) == {"wrong-station", "wrong-date"}
        assert plans[0].fmt != "rinex"


def _gzip_compress(data: bytes) -> bytes:
    import gzip as _gzip

    return _gzip.compress(data)
