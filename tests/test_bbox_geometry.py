"""Geometry of the swath footprint a ground track is widened into.

Contact #5 rendered central Mediterranean imagery inside a 23.7 x 22.6 deg
box: the cross-track swath was added to latitude as well as longitude, and
converted at a flat 111 km/deg, which is only right on the equator.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import types
from pathlib import Path

import pytest

# Import the module without scripts/viirs/__init__, which pulls in matplotlib.
_ROOT = Path(__file__).resolve().parent.parent
_pkg = types.ModuleType("viirs")
_pkg.__path__ = [str(_ROOT / "scripts" / "viirs")]
sys.modules.setdefault("viirs", _pkg)
for _name in ("models", "bbox_calculator"):
    _spec = importlib.util.spec_from_file_location(
        f"viirs.{_name}", _ROOT / "scripts" / "viirs" / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"viirs.{_name}"] = _mod
    _spec.loader.exec_module(_mod)

bbox_calculator = sys.modules["viirs.bbox_calculator"]
BoundingBox = sys.modules["viirs.models"].BoundingBox
_swath_half_width_km = bbox_calculator._swath_half_width_km
_track_bearing_rad = bbox_calculator._track_bearing_rad
_extend_by_swath = bbox_calculator._extend_by_swath

VIIRS_ALT_KM = 824.0
VIIRS_HALF_ANGLE = 56.0


def test_half_swath_matches_the_documented_viirs_swath():
    """VIIRS images a ~3000 km swath, so the half-width is ~1500 km."""
    half = _swath_half_width_km(VIIRS_ALT_KM, VIIRS_HALF_ANGLE)

    assert 1450 < half < 1550


def test_half_swath_exceeds_the_flat_tangent_form():
    """Earth curves away from the sensor: the arc is longer than the tangent."""
    flat = VIIRS_ALT_KM * math.tan(math.radians(VIIRS_HALF_ANGLE))

    assert _swath_half_width_km(VIIRS_ALT_KM, VIIRS_HALF_ANGLE) > flat * 1.1


def test_scan_past_the_limb_is_capped_at_the_horizon():
    """An 89 deg scan angle points past the limb; the arc must stay finite."""
    horizon = 6371.0 * math.acos(6371.0 / (6371.0 + VIIRS_ALT_KM))

    half = _swath_half_width_km(VIIRS_ALT_KM, 89.0)

    assert 0 < half <= horizon + 1e-6


@pytest.mark.parametrize(
    "lats, lons, expected_deg",
    [
        ([40.0, 42.0], [15.0, 15.0], 0.0),      # due north
        ([40.0, 38.0], [15.0, 15.0], 180.0),    # due south
        ([40.0, 40.0], [15.0, 17.0], 90.0),     # due east
    ],
)
def test_track_bearing(lats, lons, expected_deg):
    bearing = math.degrees(_track_bearing_rad(lats, lons))

    assert abs(abs(bearing) - expected_deg) < 1.0


def test_track_bearing_none_when_track_is_a_single_point():
    assert _track_bearing_rad([40.0, 40.0], [15.0, 15.0]) is None


def test_near_polar_track_spills_mostly_into_longitude():
    """A sun-synchronous track is ~12 deg off the meridian at mid-latitudes."""
    nadir = BoundingBox(lat_min=40.3, lat_max=42.1, lon_min=15.0, lon_max=15.6)

    box = _extend_by_swath(
        nadir, [40.3, 42.1], [15.0, 15.6], VIIRS_ALT_KM, VIIRS_HALF_ANGLE
    )

    lat_span_km = (box.lat_max - box.lat_min) * 111.32
    lon_span_km = (box.lon_max - box.lon_min) * 111.32 * math.cos(math.radians(41.2))
    # ~3000 km across the swath, and far less along it.
    assert 2700 < lon_span_km < 3300
    assert lat_span_km < lon_span_km / 2


def test_longitude_extension_widens_with_latitude():
    """A degree of longitude shrinks as cos(lat); the box must widen to match."""
    equator = _extend_by_swath(
        BoundingBox(lat_min=0.0, lat_max=1.0, lon_min=15.0, lon_max=15.0),
        [0.0, 1.0], [15.0, 15.0], VIIRS_ALT_KM, VIIRS_HALF_ANGLE,
    )
    high = _extend_by_swath(
        BoundingBox(lat_min=59.5, lat_max=60.5, lon_min=15.0, lon_max=15.0),
        [59.5, 60.5], [15.0, 15.0], VIIRS_ALT_KM, VIIRS_HALF_ANGLE,
    )

    equator_width = equator.lon_max - equator.lon_min
    high_width = high.lon_max - high.lon_min
    # cos(60 deg) = 0.5, so roughly twice as many degrees for the same km.
    assert 1.8 < high_width / equator_width < 2.2


def test_over_the_pole_every_meridian_is_in_view():
    nadir = BoundingBox(lat_min=89.0, lat_max=89.5, lon_min=10.0, lon_max=20.0)

    box = _extend_by_swath(
        nadir, [89.0, 89.5], [10.0, 20.0], VIIRS_ALT_KM, VIIRS_HALF_ANGLE
    )

    assert box.lon_min == -180.0
    assert box.lon_max == 180.0


def test_inclination_fallback_when_the_track_is_one_point():
    """A single position still yields a tilt, from the orbit inclination."""
    nadir = BoundingBox(lat_min=41.0, lat_max=41.0, lon_min=15.0, lon_max=15.0)

    box = _extend_by_swath(
        nadir, [41.0], [15.0], VIIRS_ALT_KM, VIIRS_HALF_ANGLE,
        inclination_rad=math.radians(98.7),  # NOAA-20, sun-synchronous
    )

    # Tilted, so some latitude extent -- but far less than the longitude one.
    assert 0.5 < (box.lat_max - box.lat_min) < 8.0
    assert (box.lon_max - box.lon_min) > 30.0


def test_bbox_stays_inside_valid_wgs84_ranges():
    nadir = BoundingBox(lat_min=-89.9, lat_max=-89.0, lon_min=-179.0, lon_max=179.0)

    box = _extend_by_swath(
        nadir, [-89.9, -89.0], [-179.0, 179.0], VIIRS_ALT_KM, VIIRS_HALF_ANGLE
    )

    assert box.lat_min >= -90.0 and box.lat_max <= 90.0
    assert box.lon_min >= -180.0 and box.lon_max <= 180.0
