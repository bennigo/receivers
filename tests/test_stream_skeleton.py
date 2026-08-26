"""Tests for RINEX skeleton fill/refresh (validated vs the real GONH.SKL)."""

from receivers.streaming.skeleton import (
    UNKNOWN_SERIAL,
    SkeletonMetadata,
    build_skeleton,
    fill_skeleton,
    geodetic_to_ecef,
    metadata_from_tos,
    refresh_skeleton,
    upgrade_skeleton,
)


def _line(data: str, label: str) -> str:
    return f"{data:<60}{label}"


# Reconstructed GONH.SKL (label at col 61), the real stored skeleton on rek.
GONH_SKL = (
    "\n".join(
        [
            _line("File configured from IMO rt streams", "COMMENT"),
            _line("GONH", "MARKER NAME"),
            _line("GONH", "MARKER NUMBER"),
            _line("HMF/BGO/HG          JH/IMO", "OBSERVER / AGENCY"),
            _line(
                "3605273             SEPT MOSAIC-X5      4.8.0", "REC # / TYPE / VERS"
            ),
            _line("60283B0038          TRM115000.10    NONE", "ANT # / TYPE"),
            _line("  2605201.0352 -1066895.4189  5704422.1172", "APPROX POSITION XYZ"),
            _line("        0.0000        0.0000        0.0000", "ANTENNA: DELTA H/E/N"),
            _line("     1     1", "WAVELENGTH FACT L1/2"),
            _line("", "END OF HEADER"),
        ]
    )
    + "\n"
)


class TestFillReproducesGonh:
    def test_equipment_lines_match_real_skl(self):
        meta = SkeletonMetadata(
            marker_name="GONH",
            marker_number="GONH",
            rec_serial="3605273",
            rec_type="SEPT MOSAIC-X5",
            rec_version="4.8.0",
            ant_serial="60283B0038",
            ant_type="TRM115000.10",
            ant_radome="NONE",
            antenna_h=0.0,
            antenna_e=0.0,
            antenna_n=0.0,
        )
        out = fill_skeleton(GONH_SKL, meta)
        lines = {ln[60:].strip(): ln[:60] for ln in out.splitlines()}
        assert (
            lines["REC # / TYPE / VERS"]
            == "3605273             SEPT MOSAIC-X5      4.8.0               "
        )
        assert (
            lines["ANT # / TYPE"]
            == "60283B0038          TRM115000.10    NONE                    "
        )

    def test_static_lines_preserved(self):
        # No TOS values → everything stays identical to the template.
        out = fill_skeleton(GONH_SKL, SkeletonMetadata())
        assert out == GONH_SKL

    def test_position_never_touched(self):
        # Even a full refill leaves APPROX POSITION XYZ from the stored skeleton.
        meta = SkeletonMetadata(
            rec_serial="999", rec_type="SEPT POLARX5", rec_version="5.5.0"
        )
        out = fill_skeleton(GONH_SKL, meta)
        pos = next(ln for ln in out.splitlines() if "APPROX POSITION" in ln)
        assert "2605201.0352 -1066895.4189  5704422.1172" in pos


class TestRefresh:
    def test_detects_equipment_change(self):
        new_meta = SkeletonMetadata(
            rec_serial="4001234", rec_type="SEPT POLARX5", rec_version="5.6.0"
        )
        updated, changed = refresh_skeleton(GONH_SKL, new_meta)
        assert changed is True
        rec = next(ln for ln in updated.splitlines() if "REC #" in ln)
        assert "4001234" in rec and "SEPT POLARX5" in rec

    def test_no_change_when_identical(self):
        meta = SkeletonMetadata(
            rec_serial="3605273", rec_type="SEPT MOSAIC-X5", rec_version="4.8.0"
        )
        _, changed = refresh_skeleton(GONH_SKL, meta)
        assert changed is False


class TestBuildSkeleton:
    def test_ecef_matches_gonh_survey(self):
        # cfg LLH -> ECEF within a couple metres of the surveyed GONH.SKL position
        x, y, z = geodetic_to_ecef(63.885537, -22.270311, 347.41)
        assert abs(x - 2605201.0352) < 3
        assert abs(y - -1066895.4189) < 3
        assert abs(z - 5704422.1172) < 3

    def test_build_full_header(self):
        meta = SkeletonMetadata(
            marker_name="GONH",
            marker_number="GONH",
            rec_serial="3605273",
            rec_type="SEPT MOSAIC-X5",
            rec_version="4.8.0",
            ant_serial="60283B0038",
            ant_type="TRM115000.10",
            ant_radome="NONE",
        )
        skl = build_skeleton(
            meta, latitude=63.885537, longitude=-22.270311, height=347.41
        )
        lines = skl.splitlines()
        labels = [ln[60:].strip() for ln in lines]
        # 3.04 schema: version line first, MARKER TYPE present, no WAVELENGTH FACT
        assert labels[0] == "RINEX VERSION / TYPE" and labels[-1] == "END OF HEADER"
        assert "MARKER TYPE" in labels
        assert "WAVELENGTH FACT L1/2" not in labels
        assert "APPROX POSITION XYZ" in labels and "REC # / TYPE / VERS" in labels
        # version line content matches the sbf2rin product layout
        vline = lines[0]
        assert vline[:60] == "     3.04           OBSERVATION DATA    M".ljust(60)
        # the freshly-built header round-trips through fill_skeleton unchanged
        assert fill_skeleton(skl, SkeletonMetadata()) == skl

    def test_build_is_refreshable(self):
        meta = SkeletonMetadata(marker_name="GONH", rec_serial="111")
        skl = build_skeleton(meta, latitude=63.9, longitude=-22.3, height=300.0)
        _, changed = refresh_skeleton(
            skl, SkeletonMetadata(rec_serial="222", rec_type="SEPT POLARX5")
        )
        assert changed is True


class TestUpgradeSkeleton:
    def test_legacy_rinex2_upgraded_to_304(self):
        # GONH_SKL is the legacy RINEX-2 shape (no version line, WAVELENGTH FACT).
        upgraded, changed = upgrade_skeleton(GONH_SKL)
        assert changed is True
        labels = [ln[60:].strip() for ln in upgraded.splitlines()]
        assert labels[0] == "RINEX VERSION / TYPE"
        assert "MARKER TYPE" in labels
        assert "WAVELENGTH FACT L1/2" not in labels
        # MARKER TYPE inserted right after MARKER NUMBER
        assert labels[labels.index("MARKER NUMBER") + 1] == "MARKER TYPE"

    def test_position_preserved_verbatim(self):
        # The surveyed ECEF must survive the structural upgrade untouched.
        upgraded, _ = upgrade_skeleton(GONH_SKL)
        pos = next(ln for ln in upgraded.splitlines() if "APPROX POSITION" in ln)
        assert "2605201.0352 -1066895.4189  5704422.1172" in pos

    def test_idempotent_on_already_304(self):
        once, _ = upgrade_skeleton(GONH_SKL)
        twice, changed = upgrade_skeleton(once)
        assert changed is False and twice == once

    def test_built_header_is_not_changed(self):
        # A freshly built 3.04 skeleton is already upgraded → no-op.
        skl = build_skeleton(
            SkeletonMetadata(marker_name="GONH", marker_number="GONH"),
            latitude=63.9,
            longitude=-22.3,
            height=300.0,
        )
        _, changed = upgrade_skeleton(skl)
        assert changed is False


class TestMetadataFromTos:
    def test_maps_current_session_fields(self):
        # station dict shaped like TOSClient.get_complete_station_metadata
        station = {
            "device_history": [
                {
                    "time_to": None,  # open = current
                    "gnss_receiver": {
                        "model": "PolaRx5",
                        "serial_number": "4101636",
                        "firmware_version": "5.6.0",
                    },
                    "antenna": {"model": "TRM115000.10", "serial_number": "0001"},
                    "radome": {"model": "NONE"},
                }
            ]
        }
        meta = metadata_from_tos(station, station_id="HRSC")
        assert meta.marker_name == "HRSC"
        assert meta.rec_serial == "4101636" and meta.rec_version == "5.6.0"
        assert meta.rec_type == "SEPT POLARX5"  # IGS-standardised
        assert meta.ant_serial == "0001"
        # TRM115000.10 is not in the IGS table -> falls back to the raw TOS value
        assert meta.ant_type == "TRM115000.10"
        assert meta.ant_radome == "NONE"

    def test_synthetic_serials_replaced_with_unknown(self):
        # SEY9-class case: serial-less antenna carries a synthetic TOS serial
        # (antenna-<STID>-<YYYYMMDD>, 21 chars) that overflows the RINEX field.
        station = {
            "device_history": [
                {
                    "time_to": None,
                    "gnss_receiver": {
                        "model": "PolaRx5",
                        "serial_number": "receiver-SEY9-20210325",  # synthetic
                        "firmware_version": "5.4.0",
                    },
                    "antenna": {
                        "model": "SEPPOLANT_X_MF",
                        "serial_number": "antenna-SEY9-20210325",  # synthetic, 21 ch
                    },
                    "radome": {"model": "NONE"},
                }
            ]
        }
        meta = metadata_from_tos(station, station_id="SEY9")
        assert meta.ant_serial == UNKNOWN_SERIAL
        assert meta.rec_serial == UNKNOWN_SERIAL
        # rendered ANT line: serial fits, antenna type/radome stay column-aligned
        skl = build_skeleton(meta, latitude=64.0, longitude=-21.0, height=100.0)
        ant = next(ln for ln in skl.splitlines() if "ANT # / TYPE" in ln)
        assert ant.startswith(UNKNOWN_SERIAL)
        assert ant[20:35].strip() == "SEPPOLANT_X_MF"  # type field at cols 21-35
        assert ant[36:40].strip() == "NONE"  # radome at cols 37-40

    def test_real_serial_passes_through(self):
        station = {
            "device_history": [
                {
                    "time_to": None,
                    "gnss_receiver": {"model": "PolaRx5", "serial_number": "3605273"},
                    "antenna": {"model": "TRM115000.10", "serial_number": "60283B0038"},
                    "radome": {"model": "NONE"},
                }
            ]
        }
        meta = metadata_from_tos(station, station_id="GONH")
        assert meta.ant_serial == "60283B0038" and meta.rec_serial == "3605273"

    def test_observer_agency_from_station_config(self):
        # OBSERVER / AGENCY is cfg-sourced (not a TOS attribute); without a
        # station_config the line stays blank, with one it is filled.
        station = {
            "device_history": [
                {
                    "time_to": None,
                    "gnss_receiver": {
                        "model": "PolaRx5",
                        "serial_number": "4101636",
                        "firmware_version": "5.6.0",
                    },
                    "antenna": {"model": "TRM115000.10", "serial_number": "0001"},
                    "radome": {"model": "NONE"},
                }
            ]
        }
        meta_blank = metadata_from_tos(station, station_id="HRSC")
        assert meta_blank.observer is None and meta_blank.agency is None

        cfg = {"rinex_observer": "BGO", "rinex_agency": "IMO"}
        meta = metadata_from_tos(station, station_id="HRSC", station_config=cfg)
        assert meta.observer == "BGO" and meta.agency == "IMO"
        # and it renders into the OBSERVER / AGENCY line of a built header
        skl = build_skeleton(meta, latitude=64.0, longitude=-21.0, height=100.0)
        obs_line = next(ln for ln in skl.splitlines() if "OBSERVER / AGENCY" in ln)
        assert obs_line.startswith("BGO") and "IMO" in obs_line
