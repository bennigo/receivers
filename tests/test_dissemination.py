"""Tests for EPOS dissemination (receivers.dissemination) — T1 tracer bullet.

Pure-logic tests run everywhere. The end-to-end convert+push test is guarded:
it needs the gps-tools binaries (CRX2RNX/gfzrnx) and a real archived RINEX sample,
so it skips cleanly in environments that lack either.
"""

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from receivers.dissemination.config import (
    DisseminationFormat,
    DisseminationTarget,
    VersionPolicy,
    load_dissemination_config,
)
from receivers.dissemination.convert import (
    _crinex_to_obs_name,
    _is_hatanaka,
    _obs_to_crinex_name,
    _strip_compression,
    cache_key,
    published_name,
    resolve_tool,
)
from receivers.dissemination.engine import EposDisseminate
from receivers.dissemination.qc_gate import (
    qc_check,
    read_header_info,
    select_session,
)
from receivers.dissemination.tos_access import (
    epos_markers,
    get_attribute_value,
    is_epos_flagged,
    make_session_provider,
    missing_required_attributes,
)


def _target(tmp_root, **over):
    base = dict(
        name="epos",
        active=False,
        host="",
        user="epos",
        dest=str(tmp_root / "stage"),
        source_root=str(tmp_root / "archive"),
        sessions=("15s_24hr",),
        exclude_stations=frozenset({"DYNA", "HRNC", "HAUR"}),
        convert_cache_dir=str(tmp_root / "cache"),
    )
    base.update(over)
    return DisseminationTarget(**base)


# --------------------------------------------------------------------------- config
class TestConfig:
    def test_loads_only_dissemination_tier(self, tmp_path):
        cfg = tmp_path / "sync.yaml"
        cfg.write_text("""
targets:
  - name: imo_archive
    tier: archive
    host: rawdata
    user: gpsops
    dest: ~/gpsdata
    source_root: /mnt/data/gpsdata
    cutover: "2026-06-22T00:00:00"
  - name: epos
    tier: dissemination
    active: true
    host: ""
    user: epos
    dest: /tmp/epos_stage
    source_root: /mnt/data/gpsdata
    sessions: [15s_24hr]
    exclude_stations: [DYNA]
    convert_cache_dir: ~/.cache/epos
    format:
      preserve_source_version: true
      country_code: ISL
      rinex2:
        naming: short
        hatanaka: true
        compression: Z
      rinex3:
        naming: long
        hatanaka: true
        compression: gz
      dir_template: "%Y/#b/{station}/15s_24hr/rinex/"
""")
        targets = load_dissemination_config(cfg)
        assert [t.name for t in targets] == ["epos"]  # archive tier filtered out
        t = targets[0]
        assert t.active is True
        assert t.format.country_code == "ISL"
        assert t.format.preserve_source_version is True
        assert t.format.policy_for(3).naming == "long"
        assert t.format.policy_for(3).compression == "gz"
        assert t.format.policy_for(2).naming == "short"
        assert t.format.policy_for(2).compression == "Z"
        assert t.format.dir_template.startswith("%Y")
        assert "DYNA" in t.exclude_stations

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_dissemination_config(tmp_path / "nope.yaml") == []

    def test_remote_spec_local_vs_remote(self, tmp_path):
        local = _target(tmp_path)
        assert local.remote == local.dest  # empty host => bare local path
        remote = _target(tmp_path, host="epos-portal.vedur.is")
        assert remote.remote == f"epos@epos-portal.vedur.is:{remote.dest}"


# --------------------------------------------------------------------------- convert helpers
class TestConvertHelpers:
    @pytest.mark.parametrize(
        "name,expected_stem,expected_flag",
        [
            ("FIM21280.26d.gz", "FIM21280.26d", True),
            ("FIM21280.26D.Z", "FIM21280.26D", True),
            ("FIM21280.26o", "FIM21280.26o", False),
            ("foo.crx.gz", "foo.crx", True),
        ],
    )
    def test_strip_compression(self, name, expected_stem, expected_flag):
        assert _strip_compression(name) == (expected_stem, expected_flag)

    @pytest.mark.parametrize(
        "crinex,obs",
        [
            ("FIM21280.26d", "FIM21280.26o"),
            ("FIM21280.26D", "FIM21280.26O"),
            (
                "STAT00ISL_R_20261280000_01D_15S_MO.crx",
                "STAT00ISL_R_20261280000_01D_15S_MO.rnx",
            ),
        ],
    )
    def test_crinex_to_obs_name(self, crinex, obs):
        assert _crinex_to_obs_name(crinex) == obs

    @pytest.mark.parametrize(
        "name,is_hat",
        [
            ("FIM21280.26d", True),
            ("FIM21280.26D", True),
            ("foo.crx", True),
            ("FIM21280.26o", False),
            ("STAT_..._MO.rnx", False),
            ("", False),
        ],
    )
    def test_is_hatanaka(self, name, is_hat):
        assert _is_hatanaka(name) is is_hat

    def test_cache_key_depends_on_fingerprint(self, tmp_path):
        # A plain (uncompressed) file is hashed as-is by content_sha256.
        f = tmp_path / "sample.rnx"
        f.write_bytes(b"RINEX CONTENT")
        k_empty = cache_key(f, "")
        k_fp = cache_key(f, "tos-fingerprint-v1")
        assert k_empty != k_fp  # header correction (new fingerprint) ⇒ new cache slot
        assert cache_key(f, "tos-fingerprint-v1") == k_fp  # deterministic

    def test_cache_key_depends_on_header_schema_version(self, tmp_path, monkeypatch):
        # A code-level header-logic fix (bumped HEADER_SCHEMA_VERSION) invalidates
        # the cache even though the source bytes + TOS fingerprint are unchanged —
        # so `epos-disseminate --force` re-converts with the corrected header.
        from receivers.dissemination import convert as dconv

        f = tmp_path / "sample.rnx"
        f.write_bytes(b"RINEX CONTENT")
        k_v2 = cache_key(f, "fp")
        monkeypatch.setattr(dconv, "HEADER_SCHEMA_VERSION", 99)
        assert cache_key(f, "fp") != k_v2

    @pytest.mark.parametrize(
        "obs,crinex",
        [
            ("STAT00ISL_R_..._MO.rnx", "STAT00ISL_R_..._MO.crx"),
            ("FIM21280.26o", "FIM21280.26d"),
            ("FIM21280.26O", "FIM21280.26D"),
        ],
    )
    def test_obs_to_crinex_name(self, obs, crinex):
        assert _obs_to_crinex_name(obs) == crinex

    def test_published_name_per_policy(self):
        # R3 long, Hatanaka + gz
        r3 = VersionPolicy(naming="long", hatanaka=True, compression="gz")
        assert (
            published_name("RHOF00ISL_R_20261280000_01D_15S_MO.rnx", r3)
            == "RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz"
        )
        # R2 short, Hatanaka + .Z (legacy): G3 forces uppercase .D to match the
        # IMO archive (.d->.D) and the legacy EPOS portal (RHOF0010.00D.Z).
        r2 = VersionPolicy(naming="short", hatanaka=True, compression="Z")
        assert published_name("FIM21280.26o", r2) == "FIM21280.26D.Z"
        # plain obs, no hatanaka, gz
        plain = VersionPolicy(naming="long", hatanaka=False, compression="gz")
        assert published_name("X.rnx", plain) == "X.rnx.gz"
        # no compression
        none = VersionPolicy(naming="long", hatanaka=False, compression="none")
        assert published_name("X.rnx", none) == "X.rnx"

    def test_resolve_tool_missing(self):
        with pytest.raises(Exception):
            resolve_tool("definitely-not-a-real-tool-xyz")

    def test_cache_hit_returns_plain_obs_not_packaged(self, tmp_path):
        # The cache dir holds BOTH the plain obs and the packaged .crx.gz (the
        # engine packages into the same dir). A cache hit must return the plain
        # obs — picking the gzipped artifact made detect_rinex_version choke.
        from receivers.dissemination.config import DisseminationFormat
        from receivers.dissemination.convert import (
            cache_key,
            convert_for_dissemination,
        )

        src = tmp_path / "RHOF1790.26D.Z"
        src.write_bytes(b"arbitrary source bytes")
        cache = tmp_path / "cache"
        keydir = cache / cache_key(src, "")
        keydir.mkdir(parents=True)
        plain = keydir / "RHOF00ISL_R_20261790000_01D_15S_MO.rnx"
        plain.write_text(
            "     3.04           OBSERVATION DATA    M (MIXED)"
            "           RINEX VERSION / TYPE\n" + " " * 60 + "END OF HEADER\n"
        )
        (keydir / "RHOF00ISL_R_20261790000_01D_15S_MO.crx.gz").write_bytes(
            b"\x1f\x8b\x08 not a rinex file"
        )
        res = convert_for_dissemination(
            src,
            "RHOF",
            datetime(2026, 6, 28),
            fmt=DisseminationFormat(),
            cache_dir=cache,
        )
        assert res.cached is True
        assert res.obs_name.endswith(".rnx")
        assert res.rinex_version == 3


# ------------------------------------------------------------------- decode-cache split
_FAKE_OBS = (
    "     3.04           OBSERVATION DATA    M (MIXED)           "
    "RINEX VERSION / TYPE\n" + " " * 60 + "END OF HEADER\n"
)


class TestDecodeCacheSplit:
    """The two-layer cache: the expensive header-free decode is keyed on source
    content alone, so a TOS-fingerprint change or a HEADER_SCHEMA_VERSION bump
    re-heads the cached decode obs WITHOUT re-running sbf2rin/gfzrnx."""

    @staticmethod
    def _install_fakes(monkeypatch):
        """Patch the decode primitives with a call counter (no external tools)."""
        import receivers.dissemination.convert as dconv

        counter = {"decode": 0}

        def fake_to_plain(src, tmpdir):
            counter["decode"] += 1
            p = Path(tmpdir) / "plain.rnx"
            p.write_text(_FAKE_OBS)
            return p

        def fake_canonical(
            plain,
            station,
            observation_dt,
            version,
            naming,
            country_code,
            tmpdir,
            *,
            sample=None,
            file_period="01D",
            monument_number="00",
        ):
            name = f"{station}00ISL_R_20261790000_01D_15S_MO.rnx"
            p = Path(tmpdir) / name
            p.write_text(_FAKE_OBS)
            return p, name

        monkeypatch.setattr(dconv, "_to_plain_obs", fake_to_plain)
        monkeypatch.setattr(dconv, "_canonical_obs", fake_canonical)
        monkeypatch.setattr(dconv, "detect_rinex_version", lambda p: 3)
        return dconv, counter

    def _convert(self, dconv, src, cache, **over):
        from receivers.dissemination.config import DisseminationFormat

        kw = dict(fmt=DisseminationFormat(), cache_dir=cache, tos_fingerprint="fp1")
        kw.update(over)
        return dconv.convert_for_dissemination(src, "RHOF", datetime(2026, 6, 28), **kw)

    def test_fingerprint_change_reuses_decode_cache(self, tmp_path, monkeypatch):
        dconv, counter = self._install_fakes(monkeypatch)
        src = tmp_path / "RHOF1790.26D.Z"
        src.write_bytes(b"source-bytes-xyz")
        cache = tmp_path / "cache"

        r1 = self._convert(dconv, src, cache, tos_fingerprint="fp1")
        r2 = self._convert(dconv, src, cache, tos_fingerprint="fp2")

        # Decode ran ONCE though two distinct header-applied outputs were rendered.
        assert counter["decode"] == 1
        assert r1.cached is False and r2.cached is False
        assert r1.output_path.parent != r2.output_path.parent  # distinct out_dirs
        assert r1.output_path.is_file() and r2.output_path.is_file()

    def test_header_schema_bump_reuses_decode_cache(self, tmp_path, monkeypatch):
        dconv, counter = self._install_fakes(monkeypatch)
        src = tmp_path / "RHOF1790.26D.Z"
        src.write_bytes(b"source-bytes-xyz")
        cache = tmp_path / "cache"

        r1 = self._convert(dconv, src, cache, tos_fingerprint="fp")
        monkeypatch.setattr(dconv, "HEADER_SCHEMA_VERSION", 99)
        r2 = self._convert(dconv, src, cache, tos_fingerprint="fp")

        # A code-level header fix (schema bump) re-heads cheaply — no re-decode.
        assert counter["decode"] == 1
        assert r1.output_path.parent != r2.output_path.parent

    def test_repeat_same_key_is_output_cache_hit(self, tmp_path, monkeypatch):
        dconv, counter = self._install_fakes(monkeypatch)
        src = tmp_path / "RHOF1790.26D.Z"
        src.write_bytes(b"source-bytes-xyz")
        cache = tmp_path / "cache"

        r1 = self._convert(dconv, src, cache, tos_fingerprint="fp")
        r2 = self._convert(dconv, src, cache, tos_fingerprint="fp")

        assert counter["decode"] == 1
        assert r1.cached is False and r2.cached is True

    def test_decode_cache_key_ignores_fingerprint_and_schema(
        self, tmp_path, monkeypatch
    ):
        import receivers.dissemination.convert as dconv
        from receivers.dissemination.convert import cache_key, decode_cache_key

        f = tmp_path / "s"
        f.write_bytes(b"abc")
        k = decode_cache_key(f)

        # Stable across a header-schema bump (it is header-free by design).
        monkeypatch.setattr(dconv, "HEADER_SCHEMA_VERSION", 42)
        assert decode_cache_key(f) == k
        # Distinct from the output key (which folds fingerprint + schema).
        assert decode_cache_key(f) != cache_key(f, "")
        # Content-sensitive, and a decimated product gets its own slot.
        g = tmp_path / "g"
        g.write_bytes(b"different")
        assert decode_cache_key(g) != k
        assert decode_cache_key(f, sample=30) != k


# --------------------------------------------------------------------------- engine (no tools)
class TestEngineSourceResolution:
    def test_excluded_station_is_noop(self, tmp_path):
        eng = EposDisseminate(_target(tmp_path))
        r = eng.run_one("DYNA", date(2026, 5, 8))
        assert r.ok is True
        assert "excluded" in r.message

    def test_missing_source_reports_cleanly(self, tmp_path):
        eng = EposDisseminate(_target(tmp_path))
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.ok is False
        assert r.source_path is None
        assert "no archived RINEX" in r.message

    def test_find_source_globs_archive_layout(self, tmp_path):
        # Build the archive layout: <root>/2026/may/FIM2/15s_24hr/rinex/<file>
        d = date(2026, 5, 8)  # doy 128
        rinex_dir = (
            tmp_path / "archive" / "2026" / "may" / "FIM2" / "15s_24hr" / "rinex"
        )
        rinex_dir.mkdir(parents=True)
        sample = rinex_dir / "FIM21280.26d.gz"
        sample.write_bytes(b"x")
        eng = EposDisseminate(_target(tmp_path))
        found = eng.find_source("FIM2", d)
        assert found == sample


# --------------------------------------------------------------------------- QC gate (pure)
def _write_min_header(
    path: Path, marker: str, *, xyz: str = "", receiver: str = "", number: str = ""
) -> Path:
    """Write a minimal RINEX-3 header file (enough for extract_header_info)."""
    lines = [
        "     3.04           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE",
        f"{marker:<60}MARKER NAME",
    ]
    if number:
        lines.append(f"{number:<60}MARKER NUMBER")
    if receiver:
        lines.append(f"{receiver:<60}REC # / TYPE / VERS")
    if xyz:
        lines.append(f"{xyz:<60}APPROX POSITION XYZ")
    lines.append(f"{'':<60}END OF HEADER")
    path.write_text("\n".join(lines) + "\n")
    return path


class TestQCGate:
    def test_select_session_by_date(self):
        history = [
            {
                "time_from": datetime(2020, 1, 1),
                "time_to": datetime(2021, 1, 1),
                "marker": "OLD",
            },
            {"time_from": datetime(2021, 1, 1), "time_to": None, "marker": "CUR"},
        ]
        assert select_session(history, datetime(2020, 6, 1))["marker"] == "OLD"
        assert select_session(history, datetime(2026, 5, 8))["marker"] == "CUR"
        assert select_session(history, datetime(2019, 1, 1)) is None

    def test_select_session_merges_concurrent_device_sessions(self):
        # TOS splits receiver / antenna / monument into separate overlapping
        # sessions; select_session must merge the device complement covering the
        # date so the fingerprint/QC see the full picture (not e.g. monument-only).
        history = [
            {
                "time_from": datetime(2001, 7, 19),
                "time_to": None,
                "monument": {"monument_height": 1.014},
            },
            {
                "time_from": datetime(2012, 8, 28),
                "time_to": None,
                "antenna": {"model": "TRM57971.00"},
                "gnss_receiver": {"model": "TRIMBLE NETR9"},
            },
        ]
        merged = select_session(history, datetime(2026, 6, 27))
        assert merged["monument"]["monument_height"] == 1.014
        assert merged["antenna"]["model"] == "TRM57971.00"
        assert merged["gnss_receiver"]["model"] == "TRIMBLE NETR9"

    def test_no_session_fails(self, tmp_path):
        f = _write_min_header(tmp_path / "h.rnx", "FIM2")
        v = qc_check(f, None)
        assert v.passed is False
        assert "no TOS session" in v.message

    def test_marker_match_passes(self, tmp_path):
        f = _write_min_header(tmp_path / "h.rnx", "FIM2")
        v = qc_check(f, {"marker": "FIM2"})
        assert v.passed is True

    def test_marker_mismatch_is_blocking(self, tmp_path):
        f = _write_min_header(tmp_path / "h.rnx", "FIM2")
        v = qc_check(f, {"marker": "XXXX"})
        assert v.passed is False
        assert "marker" in v.blocking

    def test_receiver_diff_is_not_blocking(self, tmp_path):
        # gnss_receiver is recorded as a (non-blocking) discrepancy; marker matches.
        f = _write_min_header(
            tmp_path / "h.rnx", "FIM2", receiver="3070340 SEPT POLARX5 5.4.0"
        )
        session = {
            "marker": "FIM2",
            "gnss_receiver": {
                "serial_number": "1",
                "model": "X",
                "firmware_version": "1",
            },
        }
        v = qc_check(f, session)
        assert v.passed is True  # receiver discrepancy does not block
        assert "receiver" in v.discrepancies

    def test_unreadable_header_fails(self, tmp_path):
        bad = tmp_path / "bad.rnx"
        bad.write_text("not a rinex file\n")
        v = qc_check(bad, {"marker": "FIM2"})
        assert v.passed is False

    def test_epos_9char_marker_accepted(self, tmp_path):
        # EPOS 4.1.7: 9-char MARKER NAME whose 4-char prefix is the TOS marker
        # must NOT be flagged as a mismatch.
        f = _write_min_header(tmp_path / "h.rnx", "FIM200ISL")
        v = qc_check(f, {"marker": "FIM2"})
        assert v.passed is True
        assert "marker" not in v.blocking
        assert v.matches.get("marker") == "FIM200ISL"

    def test_domes_match_passes_mismatch_blocks(self, tmp_path):
        # DOMES present in TOS → MARKER NUMBER must equal it.
        ok = _write_min_header(tmp_path / "ok.rnx", "FIM200ISL", number="10222M001")
        v = qc_check(ok, {"marker": "FIM2", "domes": "10222M001"})
        assert v.passed is True
        assert v.matches.get("domes") == "10222M001"

        bad = _write_min_header(tmp_path / "bad.rnx", "FIM200ISL", number="FIM2")
        v = qc_check(bad, {"marker": "FIM2", "domes": "10222M001"})
        assert v.passed is False
        assert "domes" in v.blocking


# --------------------------------------------------------------------------- EPOS filter (offline)
def _station(marker, *, epos="true", drop=()):
    """Build a TOS-style station dict with the full required attribute set."""
    attrs = {
        "marker": marker,
        "in_network_epos": epos,
        "lat": "64.1",
        "lon": "-21.9",
        "altitude": "10",
        "bedrock_condition": "good",
        "bedrock_type": "basalt",
        "geological_characteristic": "x",
        "name": f"{marker} station",
        "date_start": "2020-01-01",
    }
    for k in drop:
        attrs.pop(k, None)
    return {"attributes": [{"code": k, "value": v} for k, v in attrs.items()]}


class TestEposFilter:
    def test_get_attribute_value(self):
        st = _station("FIM2")
        assert get_attribute_value(st["attributes"], "marker") == "FIM2"
        assert get_attribute_value(st["attributes"], "nope") is None

    def test_is_epos_flagged(self):
        assert is_epos_flagged(_station("A", epos="true")) is True
        assert is_epos_flagged(_station("A", epos="TRUE")) is True
        assert is_epos_flagged(_station("A", epos="false")) is False

    def test_is_epos_flagged_requires_active_period(self):
        # KRAC-shape: in_network_epos=true but a closed (here zero-duration) period
        # -> NOT currently in EPOS.
        closed = {
            "attributes": [
                {
                    "code": "in_network_epos",
                    "value": "true",
                    "date_from": "2023-10-23T00:00:00",
                    "date_to": "2023-10-23T00:00:00",
                }
            ]
        }
        assert is_epos_flagged(closed, at="2026-06-30T00:00:00") is False

        # Open period (date_to None) is active.
        open_period = {
            "attributes": [
                {
                    "code": "in_network_epos",
                    "value": "true",
                    "date_from": "2023-10-23T00:00:00",
                    "date_to": None,
                }
            ]
        }
        assert is_epos_flagged(open_period, at="2026-06-30T00:00:00") is True

        # date_to in the future is still active; in the past is not.
        future = {
            "attributes": [
                {
                    "code": "in_network_epos",
                    "value": "true",
                    "date_from": "2023-01-01T00:00:00",
                    "date_to": "2030-01-01T00:00:00",
                }
            ]
        }
        assert is_epos_flagged(future, at="2026-06-30T00:00:00") is True
        assert is_epos_flagged(future, at="2031-06-30T00:00:00") is False

    def test_missing_required_attributes(self):
        assert missing_required_attributes(_station("A")) == []
        miss = missing_required_attributes(_station("A", drop=("bedrock_type", "name")))
        assert set(miss) == {"bedrock_type", "name"}

    def test_epos_markers_filters_flag_and_completeness(self):
        stations = [
            _station("FIM2", epos="true"),
            _station("GORE", epos="false"),  # not flagged
            _station("INCM", epos="true", drop=("lat",)),  # incomplete
            _station("REYK", epos="true"),
        ]
        assert epos_markers(stations) == ["FIM2", "REYK"]


class TestSessionFingerprint:
    def test_empty_session_is_empty_fingerprint(self):
        from receivers.dissemination.tos_access import session_fingerprint

        assert session_fingerprint(None) == ""
        assert session_fingerprint({}) == ""

    def test_changes_on_receiver_or_antenna(self):
        from receivers.dissemination.tos_access import session_fingerprint

        base = {
            "marker": "RHOF",
            "gnss_receiver": {
                "model": "NETR9",
                "serial_number": "1",
                "firmware_version": "4.6",
            },
            "antenna": {
                "model": "TRM57971.00",
                "serial_number": "9",
                "antenna_height": "1.0",
            },
            "radome": {"model": "NONE"},
        }
        fp = session_fingerprint(base)
        assert fp == session_fingerprint(dict(base))  # stable
        changed = {
            **base,
            "gnss_receiver": {**base["gnss_receiver"], "model": "MOSAIC"},
        }
        assert session_fingerprint(changed) != fp  # receiver change ⇒ new fingerprint

    def test_ignores_non_header_fields(self):
        from receivers.dissemination.tos_access import session_fingerprint

        base = {"marker": "RHOF", "gnss_receiver": {"model": "NETR9"}}
        noisy = {**base, "description": "irrelevant", "time_to": "2026-01-01"}
        assert session_fingerprint(base) == session_fingerprint(noisy)


class TestSessionProvider:
    class _FakeClient:
        def __init__(self, meta):
            self._meta = meta

        def get_complete_station_metadata(self, station):
            return self._meta

    def test_provider_selects_session_and_injects_marker(self):
        meta = {
            "marker": "fim2",
            "device_history": [
                {
                    "time_from": datetime(2021, 1, 1),
                    "time_to": None,
                    "gnss_receiver": {},
                },
            ],
        }
        provider = make_session_provider(self._FakeClient(meta))
        session = provider("FIM2", datetime(2026, 5, 8))
        assert session is not None
        assert session["marker"] == "FIM2"  # injected, upper-cased

    def test_provider_returns_none_when_no_coverage(self):
        meta = {"marker": "FIM2", "device_history": []}
        provider = make_session_provider(self._FakeClient(meta))
        assert provider("FIM2", datetime(2026, 5, 8)) is None

    def test_provider_failsafe_on_tos_error(self):
        class Boom:
            def get_complete_station_metadata(self, station):
                raise RuntimeError("TOS down")

        provider = make_session_provider(Boom())
        assert provider("FIM2", datetime(2026, 5, 8)) is None


# --------------------------------------------------------------------------- end-to-end (guarded)
def _tools_available() -> bool:
    try:
        resolve_tool("CRX2RNX")
        resolve_tool("gfzrnx")
        return True
    except Exception:
        return False


_SAMPLE = Path.home() / "tmp/gpsdata/2026/may/FIM2/15s_24hr/rinex/FIM21280.26d.gz"

# The local archive's "*.26d.gz" 15s files are RINEX 3.04 content (Hatanaka) with
# legacy short names — detect-from-content yields version 3 → long-name .crx.gz.
_PUBLISHED = "FIM200ISL_R_20261280000_01D_15S_MO.crx.gz"
_LAYOUT = "2026/may/FIM2/15s_24hr/rinex"


@pytest.mark.skipif(
    not (_tools_available() and _SAMPLE.is_file()),
    reason="needs gps-tools binaries and a real archived RINEX sample",
)
class TestEndToEnd:
    def test_convert_produces_canonical_obs(self, tmp_path):
        from receivers.dissemination.convert import convert_for_dissemination

        res = convert_for_dissemination(
            _SAMPLE,
            "FIM2",
            datetime(2026, 5, 8),
            fmt=DisseminationFormat(),
            cache_dir=tmp_path / "cache",
        )
        assert res.obs_name == "FIM200ISL_R_20261280000_01D_15S_MO.rnx"
        assert res.rinex_version == 3
        assert res.output_path.is_file()
        assert res.cached is False
        first = res.output_path.read_text(errors="replace").splitlines()[0]
        assert "3.04" in first and "RINEX VERSION" in first

    def test_second_convert_is_cache_hit(self, tmp_path):
        from receivers.dissemination.convert import convert_for_dissemination

        kw = dict(fmt=DisseminationFormat(), cache_dir=tmp_path / "cache")
        first = convert_for_dissemination(_SAMPLE, "FIM2", datetime(2026, 5, 8), **kw)
        second = convert_for_dissemination(_SAMPLE, "FIM2", datetime(2026, 5, 8), **kw)
        assert first.cached is False
        assert second.cached is True

    def _archive_with_sample(self, tmp_path):
        rinex_dir = tmp_path / "archive/2026/may/FIM2/15s_24hr/rinex"
        rinex_dir.mkdir(parents=True)
        os.symlink(_SAMPLE, rinex_dir / "FIM21280.26d.gz")

    def test_run_one_publishes_crx_gz_in_layout(self, tmp_path):
        self._archive_with_sample(tmp_path)
        eng = EposDisseminate(_target(tmp_path))  # no provider → set-header off
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.ok is True
        assert r.long_name == _PUBLISHED
        assert r.rinex_version == 3
        assert r.relative_path == f"{_LAYOUT}/{_PUBLISHED}"
        assert (tmp_path / "stage" / _LAYOUT / _PUBLISHED).is_file()

    def test_qc_gate_passes_then_pushes(self, tmp_path):
        self._archive_with_sample(tmp_path)
        provider = lambda station, dt: {"marker": "FIM2"}  # noqa: E731
        eng = EposDisseminate(
            _target(tmp_path), session_provider=provider, set_header=False
        )
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.qc_passed is True
        assert r.ok is True
        assert (tmp_path / "stage" / _LAYOUT / r.long_name).is_file()

    def test_qc_gate_blocks_on_marker_mismatch(self, tmp_path):
        self._archive_with_sample(tmp_path)
        provider = lambda station, dt: {"marker": "WRNG"}  # noqa: E731
        eng = EposDisseminate(
            _target(tmp_path), session_provider=provider, set_header=False
        )
        r = eng.run_one("FIM2", date(2026, 5, 8))
        assert r.qc_passed is False
        assert r.ok is False
        assert "QC gate failed" in r.message
        assert not (tmp_path / "stage").exists() or not list(
            (tmp_path / "stage").rglob("*.gz")
        )


# --------------------------------------------------------------------------- ETL + indexer (pure)
class TestEtlPure:
    def test_llh_to_xyz_reyk(self):
        from receivers.dissemination.epos_etl import llh_to_xyz

        x, y, z = llh_to_xyz(64.1388, -21.9555, 93.0)  # ~REYK
        # ECEF magnitude ≈ Earth radius (~6.37e6 m).
        assert abs((x**2 + y**2 + z**2) ** 0.5 - 6.37e6) < 5e4
        assert z > 0  # northern hemisphere

    def test_file_type_for(self):
        from receivers.dissemination.rinex_index import _file_type_for

        assert _file_type_for(3, "15s_24hr") == {
            "format": "RINEX3",
            "sampling_window": "24hour",
            "sampling_frequency": "15s",
        }


def _local_epos_db_ov():
    import getpass

    return {
        "host": "localhost",
        "dbname": "gnss_europe_local",
        "user": getpass.getuser(),
        "schema": "public",
    }


def _local_epos_db_available() -> bool:
    try:
        from receivers.dissemination import epos_db

        conn = epos_db.connect(_local_epos_db_ov())
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _local_epos_db_available(),
    reason="needs the local gnss_europe_local harness DB",
)
class TestEposDbHelpers:
    def test_insert_get_update_roundtrip(self):
        from receivers.dissemination import epos_db

        conn = epos_db.connect(_local_epos_db_ov())
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT t")
                aid = epos_db.get_or_create(
                    cur, "agency", {"abbreviation": "ZZ"}, {"name": "Z"}
                )
                aid2 = epos_db.get_or_create(cur, "agency", {"abbreviation": "ZZ"})
                assert aid == aid2  # get-or-create returns the same row
                epos_db.update_row(cur, "agency", aid, {"name": "Z2"})
                cur.execute("SELECT name FROM agency WHERE id = %s", (aid,))
                assert cur.fetchone()[0] == "Z2"
                cur.execute("ROLLBACK TO SAVEPOINT t")  # leave the DB clean
        finally:
            conn.rollback()
            conn.close()


class TestVocabMapping:
    def test_receiver_subtype_is_gnss_receiver(self):
        from receivers.dissemination.epos_etl import _ATTR_MAP, WHITELISTED_ITEMS

        assert "gnss_receiver" in WHITELISTED_ITEMS
        assert "receiver" not in WHITELISTED_ITEMS  # TOS uses gnss_receiver
        assert _ATTR_MAP["gnss_receiver"]["model"] == "receiver_type"
        assert _ATTR_MAP["antenna"]["model"] == "antenna_type"
        assert _ATTR_MAP["radome"]["model"] == "radome_type"

    def test_type_resolve_covers_trigger_attributes(self):
        from receivers.dissemination.epos_etl import _TYPE_RESOLVE

        # The schema triggers fire on antenna_type/receiver_type/radome_type.
        assert set(_TYPE_RESOLVE) == {"antenna_type", "receiver_type", "radome_type"}


@pytest.mark.skipif(
    not _local_epos_db_available(),
    reason="needs the local gnss_europe_local harness DB (seeded vocab + type tables)",
)
class TestItemVocabETL:
    def test_populate_items_resolves_types_and_fires_triggers(self):
        from datetime import datetime as _dt

        from receivers.dissemination import epos_db
        from receivers.dissemination.epos_etl import _populate_items

        conn = epos_db.connect(_local_epos_db_ov())
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT t")
                sid = epos_db.insert_row(cur, "station", {"name": "T", "marker": "TST"})
                children = [
                    (
                        {"time_from": _dt(2021, 1, 1), "time_to": None},
                        {
                            "code_entity_subtype": "antenna",
                            "id_entity": 1,
                            "attributes": [
                                {"code": "model", "value": "ASH701945C_M"},
                                {"code": "serial_number", "value": "S1"},
                            ],
                        },
                    ),
                    (
                        {"time_from": _dt(2021, 1, 1), "time_to": None},
                        {
                            "code_entity_subtype": "gnss_receiver",
                            "id_entity": 2,
                            "attributes": [
                                {"code": "model", "value": "TRIMBLE NETR9"},
                                {"code": "firmware_version", "value": "4.6"},
                            ],
                        },
                    ),
                ]
                n = _populate_items(cur, sid, children)
                assert n == 2
                # Scope all checks to THIS station's items (the harness DB may hold
                # committed fleet data from other ETL runs).
                scoped = (
                    "SELECT ia.value_varchar, ia.value_numeric FROM item_attribute ia "
                    "JOIN station_item si ON si.id_item = ia.id_item "
                    "WHERE si.id_station = %s AND ia.id_attribute = %s"
                )
                # antenna_type resolved to value_numeric (the trigger requires it)
                cur.execute(scoped, (sid, 1))
                assert cur.fetchone() == ("ASH701945C_M", 52)
                # the trigger wrote a filter_antenna row for this item_attribute
                cur.execute(
                    "SELECT count(*) FROM filter_antenna fa "
                    "JOIN item_attribute ia ON ia.id = fa.id_item_attribute "
                    "JOIN station_item si ON si.id_item = ia.id_item "
                    "WHERE si.id_station = %s",
                    (sid,),
                )
                assert cur.fetchone()[0] == 1
                # serial_number (id 5) carries no value_numeric
                cur.execute(scoped, (sid, 5))
                assert cur.fetchone()[1] is None
                cur.execute("ROLLBACK TO SAVEPOINT t")
        finally:
            conn.rollback()
            conn.close()


class TestRinexMd5s:
    def test_plain_rinex_md5s_equal(self, tmp_path):
        from receivers.dissemination.rinex_index import rinex_md5s

        f = tmp_path / "STAT00ISL_R_20261280000_01D_15S_MO.rnx"
        f.write_bytes(b"     3.04           OBSERVATION DATA\n")
        chk, unc = rinex_md5s(f)
        assert chk == unc  # plain (uncompressed, non-Hatanaka) ⇒ identical

    def test_gz_md5checksum_differs_uncompressed_matches_plain(self, tmp_path):
        import gzip as _gz

        from receivers.dissemination.rinex_index import _md5_bytes, rinex_md5s

        body = b"     3.04           OBSERVATION DATA\nplain rinex body\n"
        gzf = tmp_path / "STAT00ISL_R_20261280000_01D_15S_MO.rnx.gz"
        with _gz.open(gzf, "wb") as fh:
            fh.write(body)
        chk, unc = rinex_md5s(gzf)
        assert chk != unc  # compressed bytes vs content
        assert unc == _md5_bytes(body)  # uncompressed md5 == the obs content md5


# --------------------------------------------------------------------------- raw→rinex (guarded, Docker)
_RAW_SAMPLE = (
    Path.home() / "tmp/gpsdata/2026/may/RHOF/15s_24hr/raw/RHOF202605080000a.T02.gz"
)


def _native_trimble_available() -> bool:
    try:
        from receivers.rinex.trimble_native_converter import TrimbleNativeConverter

        return bool(TrimbleNativeConverter.is_available())
    except Exception:
        return False


@pytest.mark.skipif(
    not (_native_trimble_available() and _RAW_SAMPLE.is_file()),
    reason="needs the trm2rinex Docker image and a real archived Trimble .T02 sample",
)
class TestRawFallback:
    def test_raw_t02_converts_to_r3_obs(self, tmp_path):
        from receivers.dissemination.convert import convert_for_dissemination

        res = convert_for_dissemination(
            _RAW_SAMPLE,
            "RHOF",
            datetime(2026, 5, 8),
            fmt=DisseminationFormat(),
            cache_dir=tmp_path / "cache",
        )
        assert res.obs_name == "RHOF00ISL_R_20261280000_01D_15S_MO.rnx"
        assert res.rinex_version == 3
        assert res.output_path.is_file()
        first = res.output_path.read_text(errors="replace").splitlines()[0]
        assert "3.04" in first and "RINEX VERSION" in first

    def test_engine_prefers_rinex_else_raw(self, tmp_path):
        # Only raw present → engine falls back to the raw path, publishes .crx.gz.
        raw_dir = tmp_path / "archive/2026/may/RHOF/15s_24hr/raw"
        raw_dir.mkdir(parents=True)
        os.symlink(_RAW_SAMPLE, raw_dir / _RAW_SAMPLE.name)
        eng = EposDisseminate(_target(tmp_path))  # no QC provider
        r = eng.run_one("RHOF", date(2026, 5, 8))
        assert r.ok is True
        assert r.long_name == "RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz"
        layout = "2026/may/RHOF/15s_24hr/rinex"
        assert (tmp_path / "stage" / layout / r.long_name).is_file()


# --------------------------------------------------------------------------- dissemination sweep job (T8)
class TestDisseminateJob:
    def _active_cfg(self, tmp_path):
        cfg = tmp_path / "sync.yaml"
        cfg.write_text("""
targets:
  - name: epos
    tier: dissemination
    active: true
    host: ""
    user: epos
    dest: /tmp/epos_stage
    source_root: /mnt/data/gpsdata
    sessions: [15s_24hr]
""")
        return cfg

    def test_no_active_target_is_noop(self, tmp_path):
        from receivers.dissemination.job import run_epos_disseminate_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n  - name: epos\n    tier: dissemination\n"
            "    active: false\n    dest: /tmp/x\n    source_root: /x\n"
        )
        s = run_epos_disseminate_job(config_path=str(cfg), markers=["RHOF"], no_qc=True)
        assert s["stations"] == 0

    def test_sweep_counts_outcomes(self, tmp_path):
        from receivers.dissemination.job import run_epos_disseminate_job

        class _Res:
            def __init__(self, ok, cached):
                self.ok, self.cached = ok, cached
                self.artifact_path = None
                self.relative_path = None
                self.station = "X"
                self.file_date = date(2026, 6, 28)
                self.rinex_version = 3

        class _Engine:
            def __init__(self):
                self.calls = []

            def run_one(self, station, d):
                self.calls.append((station, d))
                # RHOF pushes, FIHO is cached, AKUR has nothing (skip)
                return {
                    "RHOF": _Res(True, False),
                    "FIHO": _Res(True, True),
                    "AKUR": _Res(False, False),
                }[station]

        eng = _Engine()
        s = run_epos_disseminate_job(
            config_path=str(self._active_cfg(tmp_path)),
            days_back=2,
            no_qc=True,
            today=date(2026, 6, 28),
            markers=["RHOF", "FIHO", "AKUR"],
            engine_factory=lambda _t: eng,
        )
        assert s == {
            "stations": 3,
            "pushed": 2,
            "cached": 2,
            "skipped": 2,
            "failed": 0,
            "superseded": 0,
        }
        assert len(eng.calls) == 6  # 3 stations x 2 days


# --------------------------------------------------------------------------- site logs (C6/T7)
class TestSiteLogs:
    def test_generate_returns_none_without_metadata(self, tmp_path):
        from receivers.dissemination.sitelogs import generate_site_log

        class _Empty:
            def get_complete_station_metadata(self, sid):
                return {}

        assert generate_site_log("RHOF", tmp_path, client=_Empty()) is None

    def test_commit_site_log_commits_then_noops(self, tmp_path):
        import subprocess

        from receivers.dissemination.sitelogs import commit_site_log

        repo = tmp_path / "gps-sitelogs"
        repo.mkdir()
        for cmd in (
            ["git", "-C", str(repo), "init", "-q"],
            ["git", "-C", str(repo), "config", "user.email", "t@t"],
            ["git", "-C", str(repo), "config", "user.name", "t"],
        ):
            subprocess.run(cmd, check=True)
        log = repo / "RHOF00ISL.log"
        log.write_text("site log v1\n")
        assert commit_site_log(repo, log, "add RHOF") is True
        # unchanged → nothing to commit
        assert commit_site_log(repo, log, "noop") is False


# --------------------------------------------------------------------------- EPOS header finalization (C1/C2)
class TestEposHeaderFinalize:
    """C1/C2: 9-char MARKER NAME (R3), DOMES in MARKER NUMBER, generic OBSERVER."""

    def _header(self, tmp_path, *, marker="RHOF", number=None, observer="BGO/HMF"):
        lines = [
            "     3.04           OBSERVATION DATA    M (MIXED)"
            "           RINEX VERSION / TYPE\n",
            f"{marker:<60}MARKER NAME\n",
        ]
        if number is not None:
            lines.append(f"{number:<60}MARKER NUMBER\n")
        lines.append(f"{observer:<20}{'Old Agency':<40}OBSERVER / AGENCY\n")
        lines.append(" " * 60 + "END OF HEADER\n")
        p = tmp_path / "x.rnx"
        p.write_text("".join(lines))
        return p

    def _records(self, path):
        recs = {}
        for line in path.read_text().splitlines():
            label = line[60:80].strip()
            if label:
                recs[label] = line[:60]
        return recs

    def test_marker_name_9char_for_r3(self):
        from receivers.dissemination.convert import epos_marker_name

        assert epos_marker_name("RHOF", 3, "ISL") == "RHOF00ISL"
        assert epos_marker_name("rhof", 3, "isl") == "RHOF00ISL"
        assert epos_marker_name("RHOF", 2, "ISL") == "RHOF"  # 4-char for R2

    def test_marker_name_monument_and_country_from_config(self):
        # monument + country are config-driven (not hardcoded 00ISL), and the
        # MARKER NAME must match the long filename (both read the same knobs).
        from datetime import datetime

        from receivers.dissemination.convert import (
            epos_marker_name,
            long_rinex3_name,
        )

        assert epos_marker_name("RHOF", 3, "NOR", "05") == "RHOF05NOR"
        name = long_rinex3_name(
            "RHOF", datetime(2026, 6, 28), country_code="NOR", monument_number="05"
        )
        assert name.startswith("RHOF05NOR_")

    def test_format_policy_reads_monument(self):
        fmt = DisseminationFormat.from_dict({"monument_number": "07"})
        assert fmt.monument_number == "07"
        assert DisseminationFormat.from_dict({}).monument_number == "00"

    def test_finalize_r3_updates_and_inserts(self, tmp_path):
        from receivers.dissemination.convert import finalize_epos_header

        # MARKER NUMBER absent + personal-initials OBSERVER (the FIHO/RHOF case)
        p = self._header(tmp_path, marker="RHOF", number=None, observer="BGO/HMF")
        finalize_epos_header(
            p,
            "RHOF",
            3,
            country_code="ISL",
            domes="10216M001",
            observer="GNSSatIMO",
            agency="Vedurstofa Islands",
        )
        recs = self._records(p)
        assert recs["MARKER NAME"].strip() == "RHOF00ISL"
        assert recs["MARKER NUMBER"].strip() == "10216M001"  # inserted
        assert recs["OBSERVER / AGENCY"][:20].strip() == "GNSSatIMO"
        assert recs["OBSERVER / AGENCY"][20:].strip() == "Vedurstofa Islands"

    def test_finalize_no_domes_strips_marker_number(self, tmp_path):
        from receivers.dissemination.convert import finalize_epos_header

        # No DOMES → MARKER NUMBER line is removed (never the station id), in both
        # RINEX versions. The pre-existing legacy 4-char line is actively stripped.
        p3 = self._header(tmp_path, marker="ABCD", number="ABCD")
        finalize_epos_header(p3, "ABCD", 3, country_code="ISL", domes="")
        assert "MARKER NUMBER" not in self._records(p3)
        assert self._records(p3)["MARKER NAME"].strip() == "ABCD00ISL"

        p2 = self._header(tmp_path, marker="ABCD", number="ABCD")
        finalize_epos_header(p2, "ABCD", 2, country_code="ISL", domes="")
        assert "MARKER NUMBER" not in self._records(p2)

    def test_finalize_is_idempotent(self, tmp_path):
        from receivers.dissemination.convert import finalize_epos_header

        p = self._header(tmp_path, number="10216M001")
        kw = dict(
            country_code="ISL",
            domes="10216M001",
            observer="GNSSatIMO",
            agency="Vedurstofa Islands",
        )
        finalize_epos_header(p, "RHOF", 3, **kw)
        first = p.read_text()
        finalize_epos_header(p, "RHOF", 3, **kw)
        assert p.read_text() == first


# --------------------------------------------------------------------------- sampling / config-driven naming
class TestSamplingConfig:
    """C4/C5: sampling + naming knobs come from the format policy, and the
    frequency token is derived from the file's INTERVAL, not a hardcoded default."""

    def _write_header(self, tmp_path, interval=None):
        lines = [
            "     3.04           OBSERVATION DATA    M (MIXED)"
            "           RINEX VERSION / TYPE\n"
        ]
        if interval is not None:
            lines.append(f"{interval:10.3f}".ljust(60) + "INTERVAL\n")
        lines.append(" " * 60 + "END OF HEADER\n")
        p = tmp_path / "x.rnx"
        p.write_text("".join(lines))
        return p

    def test_detect_interval(self, tmp_path):
        from receivers.dissemination.convert import detect_interval

        assert detect_interval(self._write_header(tmp_path, 15.0)) == 15.0
        assert detect_interval(self._write_header(tmp_path, 30.0)) == 30.0
        assert detect_interval(self._write_header(tmp_path, None)) is None

    def test_frequency_token(self):
        from receivers.dissemination.convert import _frequency_token

        assert _frequency_token(15) == "15S"
        assert _frequency_token(30) == "30S"
        assert _frequency_token(1) == "01S"
        assert _frequency_token(5) == "05S"

    def test_resolve_data_frequency_prefers_config_then_interval(self, tmp_path):
        from receivers.dissemination.convert import _resolve_data_frequency

        f15 = self._write_header(tmp_path, 15.0)
        assert _resolve_data_frequency(f15, sample=30) == "30S"  # config override wins
        assert _resolve_data_frequency(f15, sample=None) == "15S"  # else INTERVAL
        f_none = self._write_header(tmp_path, None)
        assert _resolve_data_frequency(f_none, sample=None) == "15S"  # logged fallback

    def test_format_policy_reads_sample_and_period(self):
        fmt = DisseminationFormat.from_dict(
            {"sample": 30, "file_period": "01D", "country_code": "ISL"}
        )
        assert fmt.sample == 30
        assert fmt.file_period == "01D"
        # default: no decimation, source rate preserved
        assert DisseminationFormat.from_dict({}).sample is None


# --------------------------------------------------------------------------- raw-source date matching
class TestFindRawSource:
    """Regression: the raw dir holds a whole month, so find_raw_source MUST
    match the requested day, not just return the directory's earliest file."""

    def _make_raw(self, tmp_path, *names):
        raw_dir = tmp_path / "archive/2026/jun/AKUR/15s_24hr/raw"
        raw_dir.mkdir(parents=True)
        for n in names:
            (raw_dir / n).write_bytes(b"dummy")
        return raw_dir

    def test_picks_date_matching_file_in_multi_day_dir(self, tmp_path):
        self._make_raw(
            tmp_path,
            "AKUR202606010000a.T02.gz",  # earliest — the old bug returned this
            "AKUR202606270000a.T02.gz",
            "AKUR202606280000a.T02.gz",  # the one we want
        )
        eng = EposDisseminate(_target(tmp_path))
        got = eng.find_raw_source("AKUR", date(2026, 6, 28))
        assert got is not None and got.name == "AKUR202606280000a.T02.gz"

    def test_matches_underscore_padded_netr5_name(self, tmp_path):
        self._make_raw(
            tmp_path,
            "AKUR______202606010000a.T02",
            "AKUR______202606280000a.T02",
        )
        eng = EposDisseminate(_target(tmp_path))
        got = eng.find_raw_source("AKUR", date(2026, 6, 28))
        assert got is not None and got.name == "AKUR______202606280000a.T02"

    def test_returns_none_when_day_absent(self, tmp_path):
        self._make_raw(tmp_path, "AKUR202606010000a.T02.gz")
        eng = EposDisseminate(_target(tmp_path))
        assert eng.find_raw_source("AKUR", date(2026, 6, 28)) is None


# --------------------------------------------------------------------------- packaging (.Z, guarded)
def _compress_available() -> bool:
    import shutil as _sh

    return _sh.which("compress") is not None


@pytest.mark.skipif(
    not _compress_available(), reason="needs the `compress` tool for .Z"
)
class TestPackagingZ:
    def test_package_unix_compress(self, tmp_path):
        # Exercise the legacy .Z compression path (no Hatanaka, so no valid-RINEX
        # requirement — this isolates the `compress` step).
        from receivers.dissemination.convert import package

        policy = VersionPolicy(naming="short", hatanaka=False, compression="Z")
        obs = tmp_path / "FIM21280.26o"
        obs.write_bytes(b"     2.11           OBSERVATION DATA\nbody\n")
        pub = package(obs, policy, tmp_path / "out")
        assert pub.name == "FIM21280.26o.Z"
        assert pub.is_file()


# --------------------------------------------------------------------------- reactive detection (T6)
class TestReactiveDetection:
    def _st(self, fp, in_epos=True):
        from receivers.dissemination.reactive import StationState

        return StationState(fingerprint=fp, in_epos=in_epos)

    def test_classify_state_machine(self):
        from receivers.dissemination.reactive import (
            ACTIVATED,
            CHANGED,
            DEACTIVATED,
            NEW,
            UNCHANGED,
            classify,
        )

        # first sight of an EPOS station → NEW (backfill)
        assert classify("A", None, self._st("f1")).kind == NEW
        # header-affecting change while in EPOS → CHANGED
        assert classify("A", self._st("f1"), self._st("f2")).kind == CHANGED
        # same fingerprint → UNCHANGED
        assert classify("A", self._st("f1"), self._st("f1")).kind == UNCHANGED
        # in_epos off→on (known before) → ACTIVATED
        assert classify("A", self._st("f1", False), self._st("f1")).kind == ACTIVATED
        # in_epos on→off → DEACTIVATED (stop-only)
        assert classify("A", self._st("f1"), self._st("f1", False)).kind == DEACTIVATED
        # never in epos → UNCHANGED
        assert classify("A", None, self._st("f1", False)).kind == UNCHANGED

    def test_store_roundtrip_and_atomic(self, tmp_path):
        from receivers.dissemination.reactive import FingerprintStore

        store = FingerprintStore(tmp_path / "state.json")
        assert store.load() == {}
        store.save({"RHOF": self._st("abc"), "FIHO": self._st("def", False)})
        back = store.load()
        assert back["RHOF"].fingerprint == "abc"
        assert back["FIHO"].in_epos is False

    def test_scan_and_advance_only_succeeded(self, tmp_path):
        from receivers.dissemination.reactive import (
            CHANGED,
            FingerprintStore,
            advance,
            scan,
        )

        store = FingerprintStore(tmp_path / "s.json")
        store.save({"RHOF": self._st("old"), "FIHO": self._st("old")})
        current = {"RHOF": self._st("newR"), "FIHO": self._st("newF")}
        changes = scan(["RHOF", "FIHO"], lambda s: current[s], store)
        kinds = {c.station: c.kind for c in changes}
        assert kinds == {"RHOF": CHANGED, "FIHO": CHANGED}
        # only RHOF's action succeeded → FIHO keeps old fp (retried next scan)
        advance(store, changes, succeeded={"RHOF"})
        after = store.load()
        assert after["RHOF"].fingerprint == "newR"
        assert after["FIHO"].fingerprint == "old"


class TestReactiveOrchestrator:
    def _st(self, fp, in_epos=True):
        from receivers.dissemination.reactive import StationState

        return StationState(fingerprint=fp, in_epos=in_epos)

    def test_dispatches_per_kind_and_advances_on_success(self, tmp_path):
        from receivers.dissemination.reactive import (
            FingerprintStore,
            ReactiveActions,
            run_reactive_sync,
        )

        store = FingerprintStore(tmp_path / "s.json")
        # RHOF: changed (prev fp differs), AKUR: new (no prev), FIHO: deactivated
        store.save({"RHOF": self._st("old"), "FIHO": self._st("f", True)})
        current = {
            "RHOF": self._st("newR"),
            "AKUR": self._st("newA"),
            "FIHO": self._st("f", False),  # in_epos on→off
        }
        calls = {"refresh": [], "disseminate": [], "sitelog": [], "stop": []}
        actions = ReactiveActions(
            refresh_metadata=lambda s: calls["refresh"].append(s),
            disseminate=lambda ch: (
                calls["disseminate"].append((ch.station, ch.kind)) or True
            ),
            regenerate_sitelog=lambda s: calls["sitelog"].append(s),
            stop=lambda s: calls["stop"].append(s),
        )
        summary = run_reactive_sync(
            ["RHOF", "AKUR", "FIHO"], lambda s: current[s], store, actions
        )
        assert summary["changed"] == 1 and summary["new"] == 1
        assert summary["deactivated"] == 1 and summary["failed"] == 0
        assert set(calls["refresh"]) == {"RHOF", "AKUR"}  # not the deactivated one
        assert calls["stop"] == ["FIHO"]
        assert set(calls["sitelog"]) == {"RHOF", "AKUR"}
        # all three actions succeeded → store advanced
        after = store.load()
        assert after["RHOF"].fingerprint == "newR"
        assert after["AKUR"].fingerprint == "newA"
        assert after["FIHO"].in_epos is False

    def test_failed_action_does_not_advance(self, tmp_path):
        from receivers.dissemination.reactive import (
            FingerprintStore,
            ReactiveActions,
            run_reactive_sync,
        )

        store = FingerprintStore(tmp_path / "s.json")
        store.save({"RHOF": self._st("old")})
        actions = ReactiveActions(
            refresh_metadata=lambda s: None,
            disseminate=lambda ch: False,  # report failure
            regenerate_sitelog=lambda s: None,
            stop=lambda s: None,
        )
        summary = run_reactive_sync(
            ["RHOF"], lambda s: self._st("newR"), store, actions
        )
        assert summary["failed"] == 1
        assert store.load()["RHOF"].fingerprint == "old"  # NOT advanced → retried


# --------------------------------------------------------------------------- reactive production wiring (T6)
class TestReactiveJob:
    def _active_cfg(self, tmp_path, extra=""):
        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    tier: dissemination\n"
            "    active: true\n"
            '    host: ""\n'
            "    user: epos\n"
            "    dest: /tmp/epos_stage\n"
            "    source_root: /mnt/data/gpsdata\n"
            "    sessions: [15s_24hr]\n" + extra
        )
        return cfg

    def test_date_range_floored_at_cutover(self):
        from receivers.dissemination.job import _reactive_date_range

        class _T:
            cutover = datetime(2026, 6, 22)

        dates = _reactive_date_range(_T(), date(2026, 6, 30), 365)
        assert dates[0] == date(2026, 6, 30) and dates[-1] == date(2026, 6, 22)
        assert len(dates) == 9

    def test_date_range_bounded_window_no_cutover(self):
        from receivers.dissemination.job import _reactive_date_range

        class _T:
            cutover = None

        dates = _reactive_date_range(_T(), date(2026, 6, 30), 3)
        assert dates == [date(2026, 6, 30 - k) for k in range(4)]

    def test_no_active_target_is_noop(self, tmp_path):
        from receivers.dissemination.job import run_epos_reactive_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n  - name: epos\n    tier: dissemination\n"
            "    active: false\n    dest: /tmp/x\n    source_root: /x\n"
        )
        s = run_epos_reactive_job(config_path=str(cfg), markers=["RHOF"], no_qc=True)
        assert s["new"] == 0 and s["failed"] == 0

    def test_job_scans_store_union_for_deactivation(self, tmp_path):
        """A station that left the EPOS marker set is still detected (DEACTIVATED)."""
        from receivers.dissemination.job import run_epos_reactive_job
        from receivers.dissemination.reactive import (
            FingerprintStore,
            ReactiveActions,
            StationState,
        )

        store_path = tmp_path / "state.json"
        FingerprintStore(store_path).save(
            {"FIHO": StationState("f", True), "RHOF": StationState("old", True)}
        )
        # Current EPOS markers: RHOF still in, AKUR new, FIHO dropped out.
        cur = {
            "RHOF": StationState("newR", True),
            "AKUR": StationState("newA", True),
            "FIHO": StationState("f", False),
        }
        calls = {"refresh": [], "disseminate": [], "sitelog": [], "stop": []}
        actions = ReactiveActions(
            refresh_metadata=lambda s: calls["refresh"].append(s),
            disseminate=lambda ch: (
                calls["disseminate"].append((ch.station, ch.kind)) or True
            ),
            regenerate_sitelog=lambda s: calls["sitelog"].append(s),
            stop=lambda s: calls["stop"].append(s),
        )
        s = run_epos_reactive_job(
            config_path=str(self._active_cfg(tmp_path)),
            no_qc=True,
            markers=["RHOF", "AKUR"],
            state_path=str(store_path),
            fingerprint_fn=lambda sid: cur[sid],
            actions=actions,
        )
        assert s["new"] == 1 and s["changed"] == 1 and s["deactivated"] == 1
        assert calls["stop"] == ["FIHO"]  # detected despite leaving the marker set
        assert set(calls["refresh"]) == {"AKUR", "RHOF"}

    def test_build_actions_disseminate_loops_range(self, tmp_path):
        from receivers.dissemination.job import _build_reactive_actions
        from receivers.dissemination.reactive import NEW, StationChange

        class _Res:
            ok = True
            cached = False
            artifact_path = None
            relative_path = None
            station = "RHOF"
            file_date = date(2026, 6, 30)
            rinex_version = 3

        class _Engine:
            def __init__(self):
                self.calls = []

            def run_one(self, station, d):
                self.calls.append((station, d))
                return _Res()

        eng = _Engine()

        class _T:
            cutover = None
            format = type("F", (), {"country_code": "ISL", "monument_number": "00"})()

        actions = _build_reactive_actions(
            _T(),
            session_provider=None,
            epos_conn=None,
            engine_factory=lambda _t: eng,
            sitelogs_dir=str(tmp_path),
            backfill_days=2,
            today=date(2026, 6, 30),
        )
        ok = actions.disseminate(StationChange("RHOF", NEW, None, None))
        assert ok is True
        assert len(eng.calls) == 3  # today + 2 back

    def test_historical_changed_sweeps_from_cutover(self, tmp_path):
        # A CHANGED with no bound (affected_floor None = historical correction) must
        # reach back to cutover even when backfill_days is smaller than the
        # cutover→today gap — else a deep-historical correction is detected but its
        # date never re-pushed.
        from receivers.dissemination.job import _build_reactive_actions
        from receivers.dissemination.reactive import (
            CHANGED,
            StationChange,
            StationState,
        )

        class _Res:
            ok = True
            cached = True
            artifact_path = None
            relative_path = None
            station = "RHOF"
            file_date = date(2026, 6, 30)
            rinex_version = 3

        class _Engine:
            def __init__(self):
                self.calls = []

            def run_one(self, station, d):
                self.calls.append((station, d))
                return _Res()

        eng = _Engine()

        class _T:
            cutover = date(2026, 6, 20)  # 10 days before today
            format = type("F", (), {"country_code": "ISL", "monument_number": "00"})()

        actions = _build_reactive_actions(
            _T(),
            session_provider=None,
            epos_conn=None,
            engine_factory=lambda _t: eng,
            sitelogs_dir=str(tmp_path),
            backfill_days=2,  # would only reach 2 days back without the extension
            today=date(2026, 6, 30),
        )
        ch = StationChange(
            "RHOF", CHANGED, StationState("o", True, {}), StationState("n", True, {})
        )
        assert actions.disseminate(ch) is True
        assert len(eng.calls) == 11  # cutover (06-20) .. today (06-30) inclusive
        assert eng.calls[-1][1] == date(2026, 6, 20)

    def test_build_actions_refresh_raises_without_conn(self, tmp_path):
        from receivers.dissemination.job import _build_reactive_actions

        class _T:
            cutover = None
            format = type("F", (), {"country_code": "ISL", "monument_number": "00"})()

        actions = _build_reactive_actions(
            _T(),
            session_provider=None,
            epos_conn=None,
            engine_factory=lambda _t: object(),
            sitelogs_dir=str(tmp_path),
            backfill_days=1,
            today=date(2026, 6, 30),
        )
        with pytest.raises(RuntimeError):
            actions.refresh_metadata("RHOF")


class TestReadHeaderInfo:
    def test_streams_header_only_and_parses(self, tmp_path):
        rnx = tmp_path / "X.rnx"
        rnx.write_text(
            "     3.04           OBSERVATION DATA    M                   "
            "RINEX VERSION / TYPE\n"
            "ABCD00ISL                                                   MARKER NAME\n"
            "        1.0760        0.0000        0.0000                  "
            "ANTENNA: DELTA H/E/N\n"
            "                                                            END OF HEADER\n"
            + ("> body line that must NOT be needed\n" * 1000)
        )
        info = read_header_info(rnx)
        assert info["MARKER NAME"] == "ABCD00ISL"
        assert info["ANTENNA: DELTA H/E/N"].split()[0] == "1.0760"

    def test_missing_end_of_header_returns_empty(self, tmp_path):
        rnx = tmp_path / "bad.rnx"
        rnx.write_text("     3.04           OBSERVATION DATA\nno terminator here\n")
        assert read_header_info(rnx) == {}

    def test_tolerant_of_non_utf8_bytes(self, tmp_path):
        rnx = tmp_path / "latin.rnx"
        rnx.write_bytes(
            b"     3.04           OBSERVATION DATA    M                   "
            b"RINEX VERSION / TYPE\n"
            b"Beneditk \xf3feigsson                                         OBSERVER\n"
            b"                                                            END OF HEADER\n"
        )
        # strict UTF-8 would raise; tolerant read must still find the header.
        assert read_header_info(rnx).get("MARKER NAME") == ""  # parsed, no crash

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_header_info(tmp_path / "nope.rnx") == {}


class TestRawDecodeDispatch:
    """The raw fallback routes Septentrio .sbf to sbf2rin, Trimble .T02 to trm2rinex."""

    def test_sbf_routes_to_sbf_decoder(self, tmp_path, monkeypatch):
        from receivers.dissemination import convert as conv_mod

        calls = []
        monkeypatch.setattr(
            conv_mod, "_decode_sbf_raw", lambda *a, **k: calls.append("sbf")
        )
        monkeypatch.setattr(
            conv_mod, "_decode_trimble_raw", lambda *a, **k: calls.append("trimble")
        )
        conv_mod._decode_raw(
            tmp_path / "HUSM202606270000a.sbf.gz",
            "HUSM",
            datetime(2026, 6, 27),
            tmp_path,
        )
        assert calls == ["sbf"]

    def test_t02_routes_to_trimble_decoder(self, tmp_path, monkeypatch):
        from receivers.dissemination import convert as conv_mod

        calls = []
        monkeypatch.setattr(
            conv_mod, "_decode_sbf_raw", lambda *a, **k: calls.append("sbf")
        )
        monkeypatch.setattr(
            conv_mod, "_decode_trimble_raw", lambda *a, **k: calls.append("trimble")
        )
        conv_mod._decode_raw(
            tmp_path / "AKUR202606270000a.T02", "AKUR", datetime(2026, 6, 27), tmp_path
        )
        assert calls == ["trimble"]


# --------------------------------------------------------------------------- exact-affected-range (T6 refinement)
class TestExactAffectedRange:
    def _dh(self):
        # antenna stable since 2012; receiver firmware updated 2026-05-18.
        return [
            {
                "time_from": datetime(2012, 8, 28),
                "time_to": None,
                "antenna": {
                    "model": "TRM57971.00",
                    "serial_number": "a",
                    "antenna_height": -0.007,
                },
            },
            {
                "time_from": datetime(2012, 8, 28),
                "time_to": datetime(2026, 5, 18),
                "gnss_receiver": {
                    "model": "NETR9",
                    "serial_number": "s",
                    "firmware_version": "4.50",
                },
            },
            {
                "time_from": datetime(2026, 5, 18),
                "time_to": None,
                "gnss_receiver": {
                    "model": "NETR9",
                    "serial_number": "s",
                    "firmware_version": "4.60",
                },
            },
        ]

    def test_reactive_components_picks_current_period(self):
        from receivers.dissemination.tos_access import reactive_components

        c = reactive_components(
            self._dh(), datetime(2026, 6, 27), marker="RHOF", domes="D"
        )
        # receiver's CURRENT period starts at the firmware-update date.
        assert c["gnss_receiver"]["since"] == "2026-05-18"
        assert c["antenna"]["since"] == "2012-08-28"
        assert c["marker"]["since"] is None
        assert c["radome"]["since"] is None  # no radome session

    def test_affected_floor_device_change_uses_device_since(self):
        from receivers.dissemination.reactive import (
            CHANGED,
            StationChange,
            StationState,
            affected_floor,
        )
        from receivers.dissemination.tos_access import reactive_components

        old = reactive_components(
            [
                {
                    "time_from": datetime(2012, 8, 28),
                    "time_to": None,
                    "gnss_receiver": {
                        "model": "NETR9",
                        "serial_number": "s",
                        "firmware_version": "4.50",
                    },
                },
                {
                    "time_from": datetime(2012, 8, 28),
                    "time_to": None,
                    "antenna": {
                        "model": "TRM57971.00",
                        "serial_number": "a",
                        "antenna_height": -0.007,
                    },
                },
            ],
            datetime(2026, 5, 1),
            marker="RHOF",
            domes="D",
        )
        new = reactive_components(
            self._dh(), datetime(2026, 6, 27), marker="RHOF", domes="D"
        )
        ch = StationChange(
            "RHOF", CHANGED, StationState("o", True, old), StationState("n", True, new)
        )
        assert affected_floor(ch) == date(2026, 5, 18)

    def test_affected_floor_marker_change_is_whole_history(self):
        from receivers.dissemination.reactive import (
            CHANGED,
            StationChange,
            StationState,
            affected_floor,
        )
        from receivers.dissemination.tos_access import reactive_components

        old = reactive_components(
            self._dh(), datetime(2026, 6, 27), marker="RHOF", domes="D"
        )
        new = reactive_components(
            self._dh(), datetime(2026, 6, 27), marker="XXXX", domes="D"
        )
        ch = StationChange(
            "RHOF", CHANGED, StationState("o", True, old), StationState("n", True, new)
        )
        assert affected_floor(ch) is None  # whole history

    def test_affected_floor_new_and_no_components_are_none(self):
        from receivers.dissemination.reactive import (
            CHANGED,
            NEW,
            StationChange,
            StationState,
            affected_floor,
        )

        assert (
            affected_floor(StationChange("S", NEW, None, StationState("n", True, {})))
            is None
        )
        # CHANGED but no component data -> no bound.
        ch = StationChange(
            "S", CHANGED, StationState("o", True, {}), StationState("n", True, {})
        )
        assert affected_floor(ch) is None

    def test_date_range_honours_floor_from(self):
        from receivers.dissemination.job import _reactive_date_range

        class _T:
            cutover = None

        full = _reactive_date_range(_T(), date(2026, 6, 27), 365)
        tight = _reactive_date_range(
            _T(), date(2026, 6, 27), 365, floor_from=date(2026, 5, 18)
        )
        assert len(full) == 366 and len(tight) == 41
        assert tight[0] == date(2026, 6, 27) and tight[-1] == date(2026, 5, 18)

    def test_components_round_trip_through_store(self, tmp_path):
        from receivers.dissemination.reactive import FingerprintStore, StationState

        store = FingerprintStore(tmp_path / "s.json")
        comps = {
            "gnss_receiver": {"fp": "abc", "since": "2026-05-18"},
            "marker": {"fp": "m", "since": None},
        }
        store.save({"RHOF": StationState("fp", True, comps)})
        assert store.load()["RHOF"].components == comps


# ----------------------------------------------- history-wide reactive detection (T6 interim)
class TestHistoryFingerprint:
    def _dh(self, hist_height=-0.007):
        # A closed historical antenna period (the one a retro-correction touches)
        # followed by the current open period — distinct sessions, so editing the
        # closed one leaves the *current* session (and its fingerprint) untouched.
        return [
            {
                "time_from": datetime(2010, 1, 1),
                "time_to": datetime(2015, 1, 1),
                "antenna": {
                    "model": "TRM1",
                    "serial_number": "a1",
                    "antenna_height": hist_height,
                },
            },
            {
                "time_from": datetime(2015, 1, 1),
                "time_to": None,
                "antenna": {
                    "model": "TRM2",
                    "serial_number": "a2",
                    "antenna_height": 0.05,
                },
            },
        ]

    def test_empty_history_is_empty_fingerprint(self):
        from receivers.dissemination.tos_access import history_fingerprint

        assert history_fingerprint([]) == ""

    def test_stable_16_hex_over_open_period(self):
        # An open (time_to=None) current session must not crash — the digest-then-
        # sort design avoids the (date_from, date_to) tuple comparison (DYNC bug).
        from receivers.dissemination.tos_access import history_fingerprint

        fp = history_fingerprint(self._dh(), marker="RHOF")
        assert fp == history_fingerprint(self._dh(), marker="RHOF")  # stable
        assert len(fp) == 16

    def test_session_order_independent(self):
        from receivers.dissemination.tos_access import history_fingerprint

        dh = self._dh()
        assert history_fingerprint(dh, marker="R") == history_fingerprint(
            list(reversed(dh)), marker="R"
        )

    def test_datetime_and_iso_string_dates_hash_equal(self):
        # device_history carries period dates as datetime OR ISO string; both forms
        # must fingerprint identically (normalized through _to_dt).
        from receivers.dissemination.tos_access import history_fingerprint

        dt = [
            {
                "time_from": datetime(2015, 1, 1),
                "time_to": None,
                "antenna": {"model": "X", "serial_number": "s", "antenna_height": 0.0},
            }
        ]
        iso = [
            {
                "time_from": "2015-01-01T00:00:00",
                "time_to": None,
                "antenna": {"model": "X", "serial_number": "s", "antenna_height": 0.0},
            }
        ]
        assert history_fingerprint(dt) == history_fingerprint(iso)

    def test_period_date_and_marker_changes_reflected(self):
        from receivers.dissemination.tos_access import history_fingerprint

        dh = self._dh()
        # closing the current period (date_to None → a date) changes the fingerprint
        closed = [dict(dh[0]), {**dh[1], "time_to": datetime(2020, 1, 1)}]
        assert history_fingerprint(dh) != history_fingerprint(closed)
        # station-scope marker / domes changes are folded in too
        assert history_fingerprint(dh, marker="RHOF") != history_fingerprint(
            dh, marker="XXXX"
        )
        assert history_fingerprint(dh, domes="A") != history_fingerprint(dh, domes="B")

    def test_historical_closed_session_change_is_detected(self):
        # The actual fix: a retro-correction to a CLOSED historical period is invisible
        # to current-session-only detection, but the history-wide fingerprint catches
        # it → CHANGED; and because the current period is untouched, affected_floor
        # returns None ⇒ the sweep re-iterates the full window (cache gates the dates).
        from receivers.dissemination.qc_gate import select_session
        from receivers.dissemination.reactive import (
            CHANGED,
            StationState,
            affected_floor,
            classify,
        )
        from receivers.dissemination.tos_access import (
            history_fingerprint,
            reactive_components,
            session_fingerprint,
        )

        dh_old = self._dh(-0.007)
        dh_new = self._dh(-0.009)  # closed-period antenna_height corrected
        today = datetime(2026, 6, 27)

        # current-session-only detection MISSES it (the bug we're closing):
        assert session_fingerprint(
            select_session(dh_old, today)
        ) == session_fingerprint(select_session(dh_new, today))
        # history-wide detection CATCHES it:
        fp_old = history_fingerprint(dh_old, marker="RHOF")
        fp_new = history_fingerprint(dh_new, marker="RHOF")
        assert fp_old != fp_new

        comps_old = reactive_components(dh_old, today, marker="RHOF")
        comps_new = reactive_components(dh_new, today, marker="RHOF")
        ch = classify(
            "RHOF",
            StationState(fp_old, True, comps_old),
            StationState(fp_new, True, comps_new),
        )
        assert ch.kind == CHANGED
        assert affected_floor(ch) is None  # purely historical ⇒ full window

    def test_make_fingerprint_fn_uses_history(self):
        # The production reader builds StationState from history_fn (not the current
        # session) + the EPOS marker set + components_fn.
        from receivers.dissemination.reactive import make_fingerprint_fn

        fn = make_fingerprint_fn(
            lambda sid, when: "deadbeef",
            {"RHOF"},
            components_fn=lambda sid, when: {"marker": {"fp": "m", "since": None}},
        )
        st = fn("rhof")
        assert st.fingerprint == "deadbeef"
        assert st.in_epos is True
        assert st.components == {"marker": {"fp": "m", "since": None}}
        # no history reader ⇒ empty fingerprint, still tracked
        assert make_fingerprint_fn(None, set())("ZZZZ").fingerprint == ""


# ----------------------------------------------- agency reference data (agencies.yaml)
class TestAgencyResolver:
    RAW = {
        "defaults": {
            "operator_agency": "Veðurstofa Íslands",
            "data_center_agency": "Veðurstofa Íslands",
            "url": "https://en.vedur.is",
        },
        "agencies": {
            "Veðurstofa Íslands": {
                "english_name": "Icelandic Meteorological Office",
                "abbrev": "IMO",
                "abbrev_is": "VÍ",
                "observer": "GNSSatIMO",
                "agency_label": "Vedurstofa Islands",
                "email": "gnss-epos@vedur.is",
                "address": ["Bústaðarvegur 7-9", "105 Reykjavík", "Iceland"],
                "contact_name": "GNSS Operator",
            },
            "Landmælingar Íslands": {
                "english_name": "Natural Science Institute of Iceland",
                "abbrev": "NSII",
                "abbrev_is": "NATT",
                "observer": "GNSSatNATT",
                "agency_label": "NATT",
                "email": "gnss@natt.is",
            },
        },
    }

    def test_from_dict_resolves_both_languages(self):
        from receivers.dissemination.agencies import AgencyResolver

        r = AgencyResolver.from_dict(self.RAW)
        imo = r.resolve("Veðurstofa Íslands")
        assert imo.abbrev == "IMO" and imo.observer == "GNSSatIMO"
        assert imo.address == ("Bústaðarvegur 7-9", "105 Reykjavík", "Iceland")
        natt = r.resolve("Landmælingar Íslands")
        assert natt.english_name == "Natural Science Institute of Iceland"
        assert natt.abbrev == "NSII" and natt.abbrev_is == "NATT"
        assert natt.observer == "GNSSatNATT" and natt.agency_label == "NATT"

    def test_unknown_and_blank_and_strip(self):
        from receivers.dissemination.agencies import AgencyResolver

        r = AgencyResolver.from_dict(self.RAW)
        assert r.resolve("Nope") is None
        assert r.resolve(None) is None
        assert r.resolve("  Veðurstofa Íslands  ").abbrev == "IMO"  # stripped

    def test_defaults_resolve_to_imo(self):
        from receivers.dissemination.agencies import AgencyResolver

        r = AgencyResolver.from_dict(self.RAW)
        assert r.operator_default().abbrev == "IMO"
        assert r.data_center_default().abbrev == "IMO"
        assert r.url_default() == "https://en.vedur.is"

    def test_missing_file_is_empty_resolver(self, tmp_path):
        from receivers.dissemination.agencies import AgencyResolver

        r = AgencyResolver.load(tmp_path / "nope.yaml")
        assert r.resolve("Veðurstofa Íslands") is None
        assert r.operator_default() is None and r.url_default() == ""

    def test_load_from_file_and_scalar_address(self, tmp_path):
        import textwrap

        from receivers.dissemination.agencies import AgencyResolver

        p = tmp_path / "agencies.yaml"
        p.write_text(
            textwrap.dedent("""
                defaults: {url: "https://x"}
                agencies:
                  "Org A":
                    english_name: "A EN"
                    abbrev: "A"
                    observer: "GNSSatA"
                    agency_label: "A"
                    address: "Single Line"
                """)
        )
        a = AgencyResolver.load(p).resolve("Org A")
        assert a.english_name == "A EN"
        assert a.address == ("Single Line",)  # scalar promoted to 1-tuple


class TestAgencyWiring:
    """agencies.yaml → per-station RINEX OBSERVER/AGENCY + owner_org in fingerprints."""

    def _engine(self):
        from receivers.dissemination.agencies import AgencyResolver
        from receivers.dissemination.engine import EposDisseminate

        fmt = type("F", (), {"observer": "GNSSatIMO", "agency": "Vedurstofa Islands"})()
        tgt = type("T", (), {"format": fmt})()
        return EposDisseminate(
            tgt, agency_resolver=AgencyResolver.from_dict(TestAgencyResolver.RAW)
        )

    def test_owner_org_resolves_to_english_agency(self):
        eng = self._engine()
        # RINEX AGENCY = the full English institutional name (<=40); NATT's 36-char
        # english_name fits, IMO's 31-char fits.
        assert eng._resolve_observer_agency({"owner_org": "Landmælingar Íslands"}) == (
            "GNSSatNATT",
            "Natural Science Institute of Iceland",
        )
        assert eng._resolve_observer_agency({"owner_org": "Veðurstofa Íslands"}) == (
            "GNSSatIMO",
            "Icelandic Meteorological Office",
        )

    def test_unknown_org_and_no_session_fall_back_to_imo_english(self):
        # Unknown/absent owner org falls back to the IMO entity
        # (defaults.operator_agency), not a sync.yaml literal → English name.
        eng = self._engine()
        assert eng._resolve_observer_agency({"owner_org": "Nope"}) == (
            "GNSSatIMO",
            "Icelandic Meteorological Office",
        )
        assert eng._resolve_observer_agency(None) == (
            "GNSSatIMO",
            "Icelandic Meteorological Office",
        )

    def test_owner_org_in_session_and_history_fingerprints(self):
        from receivers.dissemination.tos_access import (
            history_fingerprint,
            session_fingerprint,
        )

        base = {"marker": "RHOF", "domes": "D", "owner_org": "Veðurstofa Íslands"}
        assert session_fingerprint(base) != session_fingerprint(
            {**base, "owner_org": "Landmælingar Íslands"}
        )
        dh = [
            {
                "time_from": datetime(2015, 1, 1),
                "time_to": None,
                "antenna": {"model": "A"},
            }
        ]
        assert history_fingerprint(
            dh, owner_org="Veðurstofa Íslands"
        ) != history_fingerprint(dh, owner_org="Landmælingar Íslands")


class TestSitelogAgencies:
    """Role-guided §11/§12/§13 assembly (TOS roles = who, agencies.yaml = render)."""

    class _Client:
        def __init__(self, rows):
            self._rows = rows

        def get_contacts(self, entity_id):
            return self._rows

    def _resolver(self):
        from receivers.dissemination.agencies import AgencyResolver

        raw = {
            "defaults": {
                "operator_agency": "Veðurstofa Íslands",
                "data_center_agency": "Veðurstofa Íslands",
                "url": "https://en.vedur.is",
            },
            "agencies": dict(TestAgencyResolver.RAW["agencies"]),
        }
        raw["agencies"]["Landmælingar Íslands"] = {
            **raw["agencies"]["Landmælingar Íslands"],
            "dc_label": "NATT",
        }
        return AgencyResolver.from_dict(raw)

    def _row(self, role_is, org):
        return {"role_is": role_is, "organization": org}

    def test_imo_owned_station_all_imo(self):
        from receivers.dissemination.sitelogs import resolve_sitelog_agencies

        client = self._Client([self._row("Eigandi stöðvar", "Veðurstofa Íslands")])
        ag = resolve_sitelog_agencies(client, {"id_entity": 1}, self._resolver())
        assert ag["poc"]["abbrev"] == "IMO"  # operator absent → IMO default
        assert ag["responsible"] is None  # owner == POC → §12 empty
        assert ag["data_center"] == {
            "primary": "IMO",
            "secondary": "",
            "url": "https://en.vedur.is",
        }

    def test_natt_owned_imo_operated(self):
        from receivers.dissemination.sitelogs import resolve_sitelog_agencies

        client = self._Client([self._row("Eigandi stöðvar", "Landmælingar Íslands")])
        ag = resolve_sitelog_agencies(client, {"id_entity": 1}, self._resolver())
        assert ag["poc"]["abbrev"] == "IMO"  # IMO operates by default
        assert ag["responsible"]["abbrev"] == "NSII"  # owner ≠ POC → §12 NATT
        assert ag["data_center"]["primary"] == "IMO"
        assert ag["data_center"]["secondary"] == "NATT"  # dc_label, not NSII

    def test_owner_operated_station_poc_stays_imo(self):
        # AKUR-like: TOS records the owner as Rekstraraðili too, but §11 is the
        # disseminating agency (IMO) regardless — the owner belongs in §12.
        from receivers.dissemination.sitelogs import resolve_sitelog_agencies

        client = self._Client(
            [
                self._row("Eigandi stöðvar", "Landmælingar Íslands"),
                self._row("Rekstraraðili stöðvar", "Landmælingar Íslands"),
            ]
        )
        ag = resolve_sitelog_agencies(client, {"id_entity": 1}, self._resolver())
        assert ag["poc"]["abbrev"] == "IMO"
        assert ag["responsible"]["abbrev"] == "NSII"  # owner ≠ §11 → §12 NATT
        assert ag["data_center"]["secondary"] == "NATT"

    def test_data_owner_role_distinguished_from_station_owner(self):
        # 'Eigandi gagna' must NOT be swallowed by the 'eigandi' owner match.
        from receivers.dissemination.sitelogs import resolve_sitelog_agencies

        client = self._Client(
            [
                self._row("Eigandi stöðvar", "Landmælingar Íslands"),
                self._row("Eigandi gagna", "Veðurstofa Íslands"),
            ]
        )
        ag = resolve_sitelog_agencies(client, {"id_entity": 1}, self._resolver())
        assert ag["data_center"]["primary"] == "IMO"  # from the data-owner role
        assert ag["responsible"]["abbrev"] == "NSII"  # owner stays the station owner

    def test_unknown_owner_org_falls_back_to_raw_name(self):
        from receivers.dissemination.sitelogs import resolve_sitelog_agencies

        client = self._Client([self._row("Eigandi stöðvar", "Óþekkt Stofnun")])
        ag = resolve_sitelog_agencies(client, {"id_entity": 1}, self._resolver())
        assert ag["responsible"] == {"name_lines": ["Óþekkt Stofnun"]}
        assert ag["data_center"]["secondary"] == "Óþekkt Stofnun"

    def test_contacts_failure_yields_imo_defaults(self):
        from receivers.dissemination.sitelogs import resolve_sitelog_agencies

        class _Boom:
            def get_contacts(self, entity_id):
                raise RuntimeError("tos down")

        ag = resolve_sitelog_agencies(_Boom(), {"id_entity": 1}, self._resolver())
        assert ag["poc"]["abbrev"] == "IMO"
        assert ag["responsible"] is None
        assert ag["data_center"]["primary"] == "IMO"


class TestSitelogDatedSeries:
    """M3G dated filenames (rhof00isl_YYYYMMDD.log) + §0 Previous Site Log chain."""

    class _Client:
        """Minimal TOS client: full-enough metadata + no contact roles."""

        def get_complete_station_metadata(self, sid):
            return {
                "id_entity": 1,
                "marker": "rhof",
                "name": "Raufarhöfn",
                "iers_domes_number": "10216M001",
                "date_start": "2001-07-19T00:00:00",
                "lat": 66.46,
                "lon": -15.95,
                "altitude": 78.8,
                "device_history": [],
            }

        def get_contacts(self, entity_id):
            return []

    def test_find_previous_site_log_orders_and_excludes_current(self, tmp_path):
        from receivers.dissemination.sitelogs import find_previous_site_log

        for name in (
            "rhof00isl_20240101.log",
            "rhof00isl_20240827.log",
            "rhof00isl_20260701.log",  # the current date's own file
            "akur00isl_20250101.log",  # other station — ignored
        ):
            (tmp_path / name).write_text("x")
        assert (
            find_previous_site_log(tmp_path, "RHOF00ISL", "20260701")
            == "rhof00isl_20240827.log"
        )
        # no prior logs → empty (first in series); missing dir → empty
        assert find_previous_site_log(tmp_path, "ELDC00ISL", "20260701") == ""
        assert find_previous_site_log(tmp_path / "nope", "RHOF00ISL", "20260701") == ""

    def test_generate_writes_dated_name_and_chains_previous(self, tmp_path):
        from receivers.dissemination.agencies import AgencyResolver
        from receivers.dissemination.sitelogs import generate_site_log

        resolver = AgencyResolver.from_dict(TestAgencyResolver.RAW)
        p1 = generate_site_log(
            "RHOF",
            tmp_path,
            client=self._Client(),
            custom_date="20240827",
            agency_resolver=resolver,
        )
        assert p1 is not None and p1.name == "rhof00isl_20240827.log"
        # IGS site logs are written latin-1 (site_log.py:63), deliberately —
        # `read_text()` defaults to UTF-8 and blows up on the 'ö' in the
        # Icelandic agency block. That is what broke this test when the corpus
        # moved UTF-8 -> ISO-8859 on 2026-08-19; the writer is right.
        assert "Previous Site Log       : \n" in p1.read_text(
            encoding="latin-1"
        )  # first in series (empty; M3G keeps trailing space)

        p2 = generate_site_log(
            "RHOF",
            tmp_path,
            client=self._Client(),
            custom_date="20241011",
            agency_resolver=resolver,
        )
        assert p2 is not None and p2.name == "rhof00isl_20241011.log"
        assert "Previous Site Log       : rhof00isl_20240827.log" in p2.read_text(
            encoding="latin-1"
        )

    def test_plain_name_still_available(self, tmp_path):
        from receivers.dissemination.agencies import AgencyResolver
        from receivers.dissemination.sitelogs import generate_site_log

        p = generate_site_log(
            "RHOF",
            tmp_path,
            client=self._Client(),
            include_date=False,
            agency_resolver=AgencyResolver.from_dict(TestAgencyResolver.RAW),
        )
        assert p is not None and p.name == "RHOF00ISL.log"


class _FakeResponse:
    """Minimal stand-in for requests.Response used by M3GClient tests."""

    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.content = (
            __import__("json").dumps(body).encode()
            if body is not None
            else text.encode()
        )

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body

    @property
    def ok(self):
        return self.status_code < 400


class TestM3GClient:
    """M3G API client: validate (auth-free) + upload-as-draft (auth)."""

    def _client(self, monkeypatch, responses):
        """Build an M3GClient whose requests.put returns canned responses in order."""
        import receivers.dissemination.m3g_client as mod

        calls = {"list": []}

        class _Req:
            def __getattr__(self, _):
                return lambda *a, **k: self._do(a, k)

            def _do(self, args, kwargs):
                calls["list"].append((args, kwargs))
                return responses.pop(0)

            RequestException = type("RequestException", (Exception,), {})

        monkeypatch.setattr(mod, "requests", _Req())
        c = mod.M3GClient(endpoint="https://gnss-metadata.eu/v1", token="tok")
        return c, calls

    def test_validate_ok_on_200(self, monkeypatch):
        c, _ = self._client(monkeypatch, [_FakeResponse(200, {"id": "RHOF00ISL"})])
        vr = c.validate_sitelog("content", network="EPOS")
        assert vr.ok is True
        assert vr.status_code == 200
        assert vr.errors == []

    def test_validate_422_collects_errors_and_blocks(self, monkeypatch):
        body = [{"field": "siteName", "message": "required"}]
        c, _ = self._client(monkeypatch, [_FakeResponse(422, body)])
        vr = c.validate_sitelog("bad", network="EPOS")
        assert vr.ok is False
        assert len(vr.errors) == 1
        assert vr.errors[0]["field"] == "siteName"

    def test_upload_dry_run_does_not_send(self, monkeypatch):
        c, calls = self._client(monkeypatch, [])  # no responses — must not call
        ur = c.upload_sitelog("RHOF00ISL", "content", dry_run=True)
        assert ur.ok is True and ur.dry_run is True
        assert calls["list"] == []  # no HTTP call made

    def test_upload_success_parses_links_and_md5(self, monkeypatch):
        body = {
            "id": "RHOF00ISL",
            "md5Sitelog": "abc123",
            "sitelogName": "rhof00isl_20260702.log",
            "preparedDate": "2026-07-02T00:00Z",
            "dateUpdate": "2026-07-02T14:00Z",
            "_links": {
                "self": {
                    "href": "https://gnss-metadata.eu/v1/sitelog/view?id=RHOF00ISL"
                },
            },
        }
        c, _ = self._client(monkeypatch, [_FakeResponse(200, body)])
        ur = c.upload_sitelog("RHOF00ISL", "content", dry_run=False)
        assert ur.ok is True
        assert ur.md5_sitelog == "abc123"
        assert ur.sitelog_name == "rhof00isl_20260702.log"
        assert "self" in ur.links
        # draft URL derived from the self link's host
        assert "gnss-metadata.eu/sitelog/modify" in ur.draft_url
        assert "station=RHOF00ISL" in ur.draft_url

    def test_upload_401_raises(self, monkeypatch):
        from receivers.dissemination.m3g_client import M3GError

        c, _ = self._client(monkeypatch, [_FakeResponse(401, {"message": "no"})])
        import pytest

        with pytest.raises(M3GError):
            c.upload_sitelog("RHOF00ISL", "content", dry_run=False)

    def test_token_resolution_env_var(self, monkeypatch):
        import receivers.dissemination.m3g_client as mod

        monkeypatch.setenv("M3G_TOKEN", "envtok")
        assert mod._resolve_token(None) == "envtok"

    def test_token_missing_raises_on_upload(self, monkeypatch):
        import receivers.dissemination.m3g_client as mod

        monkeypatch.delenv("M3G_TOKEN", raising=False)
        monkeypatch.setattr(mod, "_find_database_cfg", lambda cfg=None: None)
        c = mod.M3GClient(endpoint="prod")  # no token anywhere
        import pytest

        with pytest.raises(mod.M3GError):
            c.upload_sitelog("RHOF00ISL", "x", dry_run=False)

    def test_endpoint_alias_resolution(self, monkeypatch):
        import receivers.dissemination.m3g_client as mod

        assert mod._resolve_endpoint("prod") == mod.DEFAULT_M3G_ENDPOINT
        assert mod._resolve_endpoint("test") == mod.TEST_M3G_ENDPOINT
        assert mod._resolve_endpoint("https://custom/v2") == "https://custom/v2"


class TestSubmitToM3G:
    """submit_to_m3g: the validate-then-upload-as-draft flow."""

    class _MockClient:
        def __init__(self, validate_ok=True, upload_ok=True):
            self._v_ok = validate_ok
            self._u_ok = upload_ok
            self.validate_calls = []
            self.upload_calls = []

        def validate_sitelog(self, content, *, network="EPOS"):
            self.validate_calls.append((content, network))
            from receivers.dissemination.m3g_client import ValidationResult

            return ValidationResult(
                ok=self._v_ok,
                network=network,
                status_code=200 if self._v_ok else 422,
                messages=[] if self._v_ok else [{"field": "x", "message": "bad"}],
            )

        def upload_sitelog(self, sid, content, *, dry_run=True):
            self.upload_calls.append((sid, content, dry_run))
            from receivers.dissemination.m3g_client import UploadResult

            return UploadResult(
                ok=self._u_ok,
                station_id=sid,
                status_code=200 if self._u_ok else 500,
                dry_run=dry_run,
                md5_sitelog="abc" if self._u_ok else None,
                sitelog_name="rhof00isl_20260702.log" if self._u_ok else None,
                links=(
                    {"self": "https://gnss-metadata.eu/v1/sitelog/view?id=RHOF00ISL"}
                    if self._u_ok
                    else {}
                ),
                error=None if self._u_ok else "boom",
            )

    def test_dry_run_validates_but_does_not_upload(self, tmp_path):
        from receivers.dissemination.sitelogs import submit_to_m3g

        sl = tmp_path / "rhof00isl_20260702.log"
        sl.write_text("sitelog content")
        mc = self._MockClient()
        r = submit_to_m3g("RHOF", site_log_path=sl, client=mc, dry_run=True)
        assert r.validated is True
        assert len(mc.validate_calls) == 1
        assert len(mc.upload_calls) == 1
        assert mc.upload_calls[0][2] is True  # dry_run=True
        assert r.uploaded is True  # dry-run upload reports ok=True

    def test_submit_uploads_after_validation(self, tmp_path):
        from receivers.dissemination.sitelogs import submit_to_m3g

        sl = tmp_path / "rhof00isl_20260702.log"
        sl.write_text("content")
        mc = self._MockClient()
        r = submit_to_m3g("RHOF", site_log_path=sl, client=mc, dry_run=False)
        assert r.validated is True
        assert mc.upload_calls[0][2] is False  # actually sent
        assert r.uploaded is True

    def test_validation_failure_blocks_upload(self, tmp_path):
        from receivers.dissemination.sitelogs import submit_to_m3g

        sl = tmp_path / "rhof00isl_20260702.log"
        sl.write_text("bad")
        mc = self._MockClient(validate_ok=False)
        r = submit_to_m3g("RHOF", site_log_path=sl, client=mc, dry_run=False)
        assert r.validated is False
        assert r.uploaded is False
        assert len(mc.upload_calls) == 0  # never reached upload
        assert r.skipped is not None and "validation" in r.skipped.lower()

    def test_skip_validation_proceeds_to_upload(self, tmp_path):
        from receivers.dissemination.sitelogs import submit_to_m3g

        sl = tmp_path / "rhof00isl_20260702.log"
        sl.write_text("content")
        mc = self._MockClient()
        r = submit_to_m3g(
            "RHOF", site_log_path=sl, client=mc, dry_run=False, skip_validation=True
        )
        assert len(mc.validate_calls) == 0
        assert r.uploaded is True


class TestDisseminateJobRange:
    """dates= / supersede batching / force on run_epos_disseminate_job."""

    def _result(self, station, d, *, ok=True, cached=False, superseded=None):
        from receivers.dissemination.engine import DisseminateResult

        r = DisseminateResult(station=station, file_date=d)
        r.ok = ok
        r.cached = cached
        r.dry_run = False
        r.superseded_name = superseded
        r.relative_path = f"2013/jun/{station}/15s_24hr/rinex/x.crx.gz"
        r.artifact_path = None  # index skipped -> supersede must NOT trigger
        return r

    def test_explicit_dates_and_force(self, tmp_path):
        from datetime import date as _date

        from receivers.dissemination.job import run_epos_disseminate_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    active: false\n"
            "    tier: dissemination\n"
            "    host: ''\n"
            "    user: u\n"
            "    dest: /tmp/x\n"
            "    source_root: /tmp/src\n"
        )
        calls = []

        class _Eng:
            def run_one(self, station, d):
                calls.append((station, d))
                return TestDisseminateJobRange._result(self, station, d)

        dates = [_date(2013, 6, 14), _date(2026, 1, 2), _date(2013, 6, 15)]
        # active:false + force=False -> nothing runs
        s0 = run_epos_disseminate_job(
            config_path=str(cfg),
            markers=["RHOF"],
            no_qc=True,
            dates=dates,
            engine_factory=lambda t: _Eng(),
            epos_conn_factory=lambda: None,
        )
        assert s0["pushed"] == 0 and not calls
        # force=True -> runs all dates, sorted+deduped, year-chunked
        s = run_epos_disseminate_job(
            config_path=str(cfg),
            markers=["RHOF"],
            no_qc=True,
            dates=dates + [_date(2013, 6, 14)],  # dup dropped
            force=True,
            engine_factory=lambda t: _Eng(),
            epos_conn_factory=lambda: None,
        )
        assert s["pushed"] == 3 and s["failed"] == 0 and s["superseded"] == 0
        assert [d for _, d in calls] == sorted({*dates})

    def test_sequential_range_advances_progress_bar(self, tmp_path, monkeypatch):
        """The default (no --parallel) range run drives ONE progress bar over all
        dates: total = every date, one advance per date, closed at the end."""
        from datetime import date as _date

        import receivers.dissemination.job as job
        from receivers.dissemination.job import run_epos_disseminate_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    active: false\n"
            "    tier: dissemination\n"
            "    host: ''\n"
            "    user: u\n"
            "    dest: /tmp/x\n"
            "    source_root: /tmp/src\n"
        )

        class _Eng:
            def run_one(self, station, d):
                return TestDisseminateJobRange._result(self, station, d)

        spy = {"n": 0, "total": None, "desc": None, "closed": False}

        class _SpyProgress:
            def __init__(self, total, desc):
                spy["total"], spy["desc"] = total, desc

            def set_total(self, n):  # per-chunk call — ignored (grand total fixed)
                pass

            def advance(self, seconds=None):
                spy["n"] += 1

            def set_postfix(self, **counts):
                pass

            def close(self):
                spy["closed"] = True

        monkeypatch.setattr(job, "_SeqProgress", _SpyProgress)

        # Two year-chunks (2024, 2026) so a grand total across chunks is exercised.
        dates = [_date(2024, 1, 1), _date(2024, 1, 2), _date(2026, 5, 8)]
        s = run_epos_disseminate_job(
            config_path=str(cfg),
            markers=["RHOF"],
            no_qc=True,
            dates=dates,
            force=True,
            engine_factory=lambda t: _Eng(),
            epos_conn_factory=lambda: None,
        )
        assert s["pushed"] == 3
        assert spy["total"] == 3  # one bar over ALL dates, not per chunk
        assert spy["n"] == 3  # one advance per date
        assert spy["desc"] == "RHOF"  # single-station desc
        assert spy["closed"] is True

    def test_seqprogress_adapter_is_noop_safe(self):
        """_SeqProgress degrades cleanly (non-TTY / no tqdm) — never raises."""
        from receivers.dissemination.job import _SeqProgress

        p = _SeqProgress(5, "X")
        p.set_total(99)
        p.advance()
        p.advance(1.5)
        p.set_postfix(pushed=1, failed=0)
        p.close()

    def test_dead_epos_conn_reopened_mid_chunk(self, tmp_path):
        """A conn severed mid-chunk (laptop sleep, NAT expiry) is reopened at
        the next date instead of silently skipping indexes for the rest."""
        from datetime import date as _date

        from receivers.dissemination.job import run_epos_disseminate_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    active: false\n"
            "    tier: dissemination\n"
            "    host: ''\n"
            "    user: u\n"
            "    dest: /tmp/x\n"
            "    source_root: /tmp/src\n"
        )

        class _Conn:
            closed = 0

            def close(self):
                self.closed = 1

        conns: list = []

        def factory():
            c = _Conn()
            conns.append(c)
            return c

        outer = self

        class _Eng:
            def run_one(self, station, d):
                conns[-1].closed = 1  # peer dies while this date is processed
                return TestDisseminateJobRange._result(outer, station, d)

        run_epos_disseminate_job(
            config_path=str(cfg),
            markers=["RHOF"],
            no_qc=True,
            dates=[_date(2013, 6, 14), _date(2013, 6, 15)],
            force=True,
            engine_factory=lambda t: _Eng(),
            epos_conn_factory=factory,
        )
        # initial conn + one reopen when date 2 found it closed
        assert len(conns) == 2

    def test_supersede_gated_on_indexed(self, tmp_path, monkeypatch):
        """superseded_name WITHOUT a durable index must never queue removal."""
        from datetime import date as _date

        import receivers.dissemination.job as jobmod
        from receivers.dissemination.job import run_epos_disseminate_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    active: true\n"
            "    tier: dissemination\n"
            "    host: portal.example\n"
            "    user: epos\n"
            "    dest: /mnt/epos\n"
            "    source_root: /tmp/src\n"
        )

        class _Eng:
            def run_one(self, station, d):
                return TestDisseminateJobRange._result(
                    self, station, d, superseded="RHOF1650.13D.Z"
                )

        flushed = []
        monkeypatch.setattr(
            jobmod,
            "_index_pushed",
            lambda conn, tgt, res: None,  # index FAILED / skipped
        )

        def _boom(*a, **k):  # would be called only if a flush happens
            flushed.append(1)
            return {"removed": [], "skipped": [], "would_remove": [], "deindexed": []}

        import receivers.dissemination.rinex_index as ri

        monkeypatch.setattr(ri, "supersede_legacy_batch", _boom)
        s = run_epos_disseminate_job(
            config_path=str(cfg),
            markers=["RHOF"],
            no_qc=True,
            dates=[_date(2013, 6, 14)],
            engine_factory=lambda t: _Eng(),
            epos_conn_factory=lambda: object(),  # conn present, index still None
        )
        assert s["pushed"] == 1
        assert s["superseded"] == 0 and not flushed


class TestProducts:
    """format.products — multi-product per date (native + 30s decimated)."""

    def test_products_parse_and_fallback(self):
        from receivers.dissemination.config import DisseminationFormat, ProductSpec

        f = DisseminationFormat.from_dict(
            {
                "products": [
                    {"sample": None},
                    {"sample": 30},
                    {"sample": 60, "enabled": False},
                ]
            }
        )
        assert [p.sample for p in f.active_products()] == [None, 30]
        # legacy single-sample shape unchanged
        f2 = DisseminationFormat.from_dict({"sample": 30})
        assert [p.sample for p in f2.active_products()] == [30]
        # both set -> products win, top-level sample ignored
        f3 = DisseminationFormat.from_dict(
            {"sample": 30, "products": [{"sample": None}]}
        )
        assert f3.sample is None
        assert f3.active_products() == [ProductSpec(sample=None)]

    def test_cache_key_sample_slot(self, tmp_path):
        import gzip as _gzip

        from receivers.dissemination.convert import cache_key

        src = tmp_path / "RHOF1830.26D.gz"
        with _gzip.open(src, "wb") as fh:
            fh.write(b"obs content")
        base = cache_key(src, "fp")
        assert cache_key(src, "fp", sample=None) == base
        k30 = cache_key(src, "fp", sample=30)
        assert k30 == f"{base}-s30" and k30 != base

    def test_job_runs_each_product(self, tmp_path):
        from datetime import date as _date

        from receivers.dissemination.job import run_epos_disseminate_job

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    active: true\n"
            "    tier: dissemination\n"
            "    host: ''\n"
            "    user: u\n"
            "    dest: /tmp/x\n"
            "    source_root: /tmp/src\n"
            "    format:\n"
            "      products:\n"
            "        - sample: null\n"
            "        - sample: 30\n"
        )
        calls = []

        class _Eng:
            def run_one(self, station, d, product=None):
                calls.append((station, d, getattr(product, "sample", "MISSING")))
                r = TestDisseminateJobRange._result(self, station, d)
                return r

        s = run_epos_disseminate_job(
            config_path=str(cfg),
            markers=["RHOF"],
            no_qc=True,
            dates=[_date(2026, 7, 2)],
            engine_factory=lambda t: _Eng(),
            epos_conn_factory=lambda: None,
        )
        assert s["pushed"] == 2
        assert [c[2] for c in calls] == [None, 30]

    def test_engine_session_segment_token(self, tmp_path):
        from receivers.dissemination.config import load_dissemination_config
        from receivers.dissemination.engine import EposDisseminate

        cfg = tmp_path / "sync.yaml"
        cfg.write_text(
            "targets:\n"
            "  - name: epos\n"
            "    active: true\n"
            "    tier: dissemination\n"
            "    host: ''\n"
            "    user: u\n"
            f"    dest: {tmp_path}\n"
            f"    source_root: {tmp_path}\n"
            "    format:\n"
            '      dir_template: "%Y/#b/{station}/{session}/rinex/"\n'
        )
        target = load_dissemination_config(cfg)[0]
        eng = EposDisseminate(target)
        from datetime import date as _d2

        rel = eng.relative_dir("RHOF", _d2(2026, 7, 2), session_segment="30s_24hr")
        assert rel == "2026/jul/RHOF/30s_24hr/rinex"
        rel2 = eng.relative_dir("RHOF", _d2(2026, 7, 2))
        assert rel2 == "2026/jul/RHOF/15s_24hr/rinex"


class TestSourceRootCandidates:
    def test_first_existing_wins(self, tmp_path, monkeypatch):
        from receivers.dissemination.config import _resolve_source_root

        a = tmp_path / "a"
        b = tmp_path / "b"
        b.mkdir()
        assert _resolve_source_root([str(a), str(b)], "epos") == str(b)

    def test_string_form_passthrough(self, tmp_path):
        from receivers.dissemination.config import _resolve_source_root

        d = tmp_path / "root"
        d.mkdir()
        assert _resolve_source_root(str(d), "epos") == str(d)

    def test_none_existing_keeps_first_and_warns(self, tmp_path, caplog):
        import logging as _logging

        from receivers.dissemination.config import _resolve_source_root

        with caplog.at_level(_logging.WARNING):
            out = _resolve_source_root(
                [str(tmp_path / "x"), str(tmp_path / "y")], "epos"
            )
        assert out == str(tmp_path / "x")
        assert any("NO source_root" in r.message for r in caplog.records)


# -------------------------------------------------- TOS efficiency / log noise
class TestNoIntervalWarningDedup:
    """The 'no INTERVAL → naming as 15S' fallback logs once per station at WARNING
    (rest at DEBUG) — a 20-year backfill of INTERVAL-less RINEX-2 must not spam."""

    _HDR = (
        "     2.11           OBSERVATION DATA    G (GPS)             "
        "RINEX VERSION / TYPE\n" + " " * 60 + "END OF HEADER\n"
    )

    def test_warns_once_then_debug(self, tmp_path, caplog):
        import logging as _logging

        import receivers.dissemination.convert as dconv

        dconv._NO_INTERVAL_WARNED.discard("QQQQ")
        f1 = tmp_path / "QQQQ2090.06O"
        f2 = tmp_path / "QQQQ2100.06O"
        f1.write_text(self._HDR)
        f2.write_text(self._HDR)

        with caplog.at_level(_logging.DEBUG, logger="receivers.dissemination"):
            assert dconv._resolve_data_frequency(f1, None) == "15S"
            assert dconv._resolve_data_frequency(f2, None) == "15S"

        warns = [
            r
            for r in caplog.records
            if r.levelno == _logging.WARNING and "no INTERVAL" in r.message
        ]
        debugs = [
            r
            for r in caplog.records
            if r.levelno == _logging.DEBUG and "no INTERVAL" in r.message
        ]
        assert len(warns) == 1  # first file only
        assert len(debugs) == 1  # second file demoted

    def test_configured_sample_never_warns(self, tmp_path, caplog):
        import logging as _logging

        import receivers.dissemination.convert as dconv

        f = tmp_path / "QQQ52090.06O"
        f.write_text(self._HDR)
        with caplog.at_level(_logging.WARNING, logger="receivers.dissemination"):
            assert dconv._resolve_data_frequency(f, 30) == "30S"
        assert not [r for r in caplog.records if "no INTERVAL" in r.message]


class TestTosMetadataCache:
    """set_header threads a caller-owned metadata cache so a backfill makes 1 TOS
    fetch per station, not 1 per file (the per-file-reconnect fix)."""

    def test_set_header_forwards_cache(self, tmp_path, monkeypatch):
        import receivers.dissemination.convert as dconv

        captured = {}

        def fake_correct(**kw):
            captured.update(kw)
            return object()  # non-None ⇒ "applied"

        monkeypatch.setattr("tostools.rinex.correct_rinex_from_tos", fake_correct)
        cache: dict = {}
        f = tmp_path / "x.rnx"
        f.write_text("hdr")
        ok = dconv.set_header_from_tos(
            f, "NYLA", datetime(2026, 1, 1), tos_metadata_cache=cache
        )
        assert ok is True
        assert captured["tos_metadata_cache"] is cache  # same dict forwarded

    def test_engine_holds_shared_cache(self):
        from types import SimpleNamespace

        from receivers.dissemination.engine import EposDisseminate

        tgt = SimpleNamespace()
        shared: dict = {}
        eng = EposDisseminate(tgt, tos_metadata_cache=shared, agency_resolver=object())
        assert eng.tos_metadata_cache is shared
        # No cache passed ⇒ its own fresh dict (still dedupes within the engine).
        eng2 = EposDisseminate(tgt, agency_resolver=object())
        assert eng2.tos_metadata_cache == {}
        assert eng2.tos_metadata_cache is not shared
