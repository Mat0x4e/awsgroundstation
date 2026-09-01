"""Per-pixel geolocation of a SatDump VIIRS swath.

The facts pinned here were each established against contact #5's real
product.cbor and an independent SGP4 propagation of NOAA-20.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_pkg = types.ModuleType("viirs")
_pkg.__path__ = [str(_ROOT / "scripts" / "viirs")]
sys.modules.setdefault("viirs", _pkg)
for _name in ("models", "scan_geometry"):
    _spec = importlib.util.spec_from_file_location(
        f"viirs.{_name}", _ROOT / "scripts" / "viirs" / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"viirs.{_name}"] = _mod
    _spec.loader.exec_module(_mod)

sg = sys.modules["viirs.scan_geometry"]

# Contact #5, chunk_0: the first ephemeris point as SatDump wrote it.
RAW_TIMESTAMP = -2506789711.874815
TRUE_TIMESTAMP = 1788177584.125185  # 2026-08-31T11:59:44Z
POSITION = [4172.887125374066, 3632.556522377582, 4608.943924183183]
VELOCITY = [-2.555861276089058, -4.1849054073334, 5.597504720441066]

# Where NOAA-20 actually was, from SGP4.
TRUE_LAT, TRUE_LON = 39.971, 15.288


def _contact5_cfg(points: int = 30, span: float = 29.0) -> dict:
    """A projection_cfg shaped like the real one, with a straight-line orbit."""
    ephemeris = []
    for i in range(points):
        fraction = i * span / (points - 1)
        ephemeris.append(
            {
                "timestamp": RAW_TIMESTAMP + fraction,
                "x": POSITION[0] + VELOCITY[0] * fraction,
                "y": POSITION[1] + VELOCITY[1] * fraction,
                "z": POSITION[2] + VELOCITY[2] * fraction,
                "vx": VELOCITY[0], "vy": VELOCITY[1], "vz": VELOCITY[2],
            }
        )
    return {
        "type": "viirs_single_line",
        "ephemeris": ephemeris,
        "scan_angle": 112.0,
        "image_width": 6400,
        "forced_gcps_x": [1279, 2015, 3199, 4383, 5119],
        "timestamp_offset": 0.0,
        "interpolate_timestamps": 32,
        "interpolate_timestamps_scantime": 0.0555,
        "timefilter": {"scan_time": 1.786, "max_diff": 10.0, "type": "simple"},
    }


class TestEpochUnwrap:
    def test_wrapped_timestamp_is_recovered(self):
        assert sg.unwrap_epoch(RAW_TIMESTAMP) == pytest.approx(TRUE_TIMESTAMP)

    def test_a_sane_timestamp_is_left_alone(self):
        assert sg.unwrap_epoch(TRUE_TIMESTAMP) == pytest.approx(TRUE_TIMESTAMP)

    @pytest.mark.parametrize("value", [float("nan"), None, "not a number"])
    def test_unusable_values_are_refused(self, value):
        assert sg.unwrap_epoch(value) is None

    def test_a_value_no_wrap_can_rescue_is_refused(self):
        """Rather than shift a genuinely broken timestamp into plausibility."""
        assert sg.unwrap_epoch(-1e18) is None


class TestFrames:
    def test_gmst_matches_the_standard_value_at_j2000(self):
        j2000 = 946728000.0  # 2000-01-01T12:00:00Z
        assert math.degrees(float(sg.gmst_rad(j2000))) == pytest.approx(280.46, abs=0.01)

    def test_rotation_with_the_raw_stamp_lands_on_the_true_position(self):
        """The stored frame was built with SatDump's wrapped clock."""
        ecef = sg.eci_to_ecef(np.array(POSITION), RAW_TIMESTAMP)
        lat, lon = sg.ecef_to_geodetic(ecef)

        assert lat == pytest.approx(TRUE_LAT, abs=0.05)
        assert lon == pytest.approx(TRUE_LON, abs=0.05)

    def test_rotation_with_the_unwrapped_stamp_is_a_third_of_the_planet_out(self):
        """The mistake this module exists to avoid."""
        _, lon = sg.ecef_to_geodetic(sg.eci_to_ecef(np.array(POSITION), TRUE_TIMESTAMP))

        assert abs(lon - TRUE_LON) > 100.0

    def test_geodetic_latitude_is_not_geocentric(self):
        """They differ by ~0.19 deg at 45 deg -- about 21 km on the ground."""
        point = np.array([sg.WGS84_A_KM / math.sqrt(2), 0.0, sg.WGS84_B_KM / math.sqrt(2)])
        lat, _ = sg.ecef_to_geodetic(point)
        geocentric = math.degrees(math.atan2(point[2], point[0]))

        assert lat - geocentric == pytest.approx(0.19, abs=0.03)


class TestSwathGeolocator:
    def test_nadir_lands_on_the_sub_satellite_point(self):
        geo = sg.SwathGeolocator.from_projection_cfg(_contact5_cfg())

        lat, lon = geo.subsatellite_track(2)

        assert lat[0] == pytest.approx(TRUE_LAT, abs=0.1)
        assert lon[0] == pytest.approx(TRUE_LON, abs=0.1)

    def test_swath_is_as_wide_as_viirs(self):
        geo = sg.SwathGeolocator.from_projection_cfg(_contact5_cfg())

        g = geo.locate(16, 3200)
        row = g.lat.shape[0] // 2
        width_km = math.hypot(
            (g.lat[row, 0] - g.lat[row, -1]) * 111.32,
            (g.lon[row, 0] - g.lon[row, -1]) * 111.32
            * math.cos(math.radians(g.lat[row, 1600])),
        )

        assert 2800 < width_km < 3200  # VIIRS images ~3000 km

    def test_rows_are_clocked_at_the_line_rate_not_stretched(self):
        """256 rows at 0.0555 s is 14.2 s, not the ephemeris' 29 s."""
        geo = sg.SwathGeolocator.from_projection_cfg(_contact5_cfg())

        times = geo.row_times(256)

        assert times[-1] - times[0] == pytest.approx(255 * 0.0555, rel=1e-6)

    def test_rows_span_the_ephemeris_when_no_line_rate_is_given(self):
        cfg = _contact5_cfg()
        del cfg["interpolate_timestamps_scantime"]
        del cfg["interpolate_timestamps"]
        geo = sg.SwathGeolocator.from_projection_cfg(cfg)

        times = geo.row_times(256)

        assert times[-1] - times[0] == pytest.approx(29.0, rel=1e-6)

    def test_aggregation_zones_widen_pixels_toward_nadir(self):
        """3:1 aggregation at nadir, 1:1 at the edge, so nadir pixels are wider."""
        geo = sg.SwathGeolocator.from_projection_cfg(_contact5_cfg())

        angles = geo.column_angles(3200)
        nadir_step = abs(angles[1600] - angles[1599])
        edge_step = abs(angles[1] - angles[0])

        assert nadir_step == pytest.approx(3 * edge_step, rel=0.05)

    def test_scan_angles_span_the_full_swath_symmetrically(self):
        geo = sg.SwathGeolocator.from_projection_cfg(_contact5_cfg())

        angles = geo.column_angles(3200)

        assert math.degrees(angles[0]) == pytest.approx(-56.0, abs=0.1)
        assert math.degrees(angles[-1]) == pytest.approx(56.0, abs=0.1)

    def test_a_ray_that_misses_the_earth_is_not_invented(self):
        origin = np.array([[[7200.0, 0.0, 0.0]]])
        look = np.array([[[0.0, 0.0, 1.0]]])  # straight out along z, misses

        point = sg.SwathGeolocator._intersect_ellipsoid(origin, look)

        assert np.isnan(point).all()

    def test_ephemeris_with_inconsistent_wraps_is_refused(self):
        cfg = _contact5_cfg(points=4)
        cfg["ephemeris"][2]["timestamp"] = TRUE_TIMESTAMP  # already unwrapped

        assert sg.SwathGeolocator.from_projection_cfg(cfg) is None

    @pytest.mark.parametrize("cfg", [None, {}, {"ephemeris": []}, {"ephemeris": [{}]}])
    def test_unusable_configs_fall_back_rather_than_raise(self, cfg):
        assert sg.SwathGeolocator.from_projection_cfg(cfg) is None


class TestResampling:
    def _geometry(self, height=8, width=64):
        geo = sg.SwathGeolocator.from_projection_cfg(_contact5_cfg())
        return geo.locate(height, width)

    def test_pixels_land_inside_the_reported_extent(self):
        geometry = self._geometry()
        data = np.random.default_rng(0).random(geometry.lat.shape, dtype=np.float32)

        grid, extent = sg.resample_to_equirect(data, geometry)
        lat_min, lat_max, lon_min, lon_max = extent

        assert lat_min < lat_max and lon_min < lon_max
        assert np.isfinite(grid).any()

    def test_rgb_survives_resampling(self):
        geometry = self._geometry()
        data = np.zeros(geometry.lat.shape + (3,), dtype=np.float32)
        data[..., 0] = 1.0

        grid, _ = sg.resample_to_equirect(data, geometry)

        painted = np.isfinite(grid[..., 0])
        assert painted.any()
        assert np.allclose(grid[painted][:, 0], 1.0)

    def test_area_outside_the_swath_stays_transparent(self):
        """A tilted strip cannot fill its own bounding box, and must not try."""
        geometry = self._geometry(height=8, width=512)
        data = np.ones(geometry.lat.shape, dtype=np.float32)

        grid, _ = sg.resample_to_equirect(data, geometry)

        assert np.isnan(grid).any()

    def test_nothing_to_place_returns_none(self):
        geometry = sg.SwathGeometry(
            lat=np.full((4, 4), np.nan), lon=np.full((4, 4), np.nan)
        )

        assert sg.resample_to_equirect(np.ones((4, 4)), geometry) is None

    def test_a_swath_crossing_the_antimeridian_is_refused(self):
        """No rectangle in these coordinates can express it; say so."""
        lat = np.array([[10.0, 10.0], [11.0, 11.0]])
        lon = np.array([[-179.0, 179.0], [-179.0, 179.0]])
        geometry = sg.SwathGeometry(lat=lat, lon=lon)

        assert sg.resample_to_equirect(np.ones((2, 2)), geometry) is None

    def test_single_pixel_gaps_are_filled(self):
        grid = np.array([[1.0, np.nan, 1.0], [1.0, 1.0, 1.0]], dtype=np.float32)

        filled = sg._fill_small_holes(grid.copy())

        assert np.isfinite(filled).all()
