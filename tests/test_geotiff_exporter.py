"""Tests for GeoTIFFExporter.export_nasa -- single-band and 3-band RGB.

Root-caused bug: NASA True Color GeoTIFF is never written.
  GeoTIFFExporter.export_nasa only supported SINGLE-BAND swath data. For NASA
  True Color, visualize_nasa.py calls export_nasa with rgb_data of shape
  (H, W, 3) and lat/lon of shape (H, W). The old code did
  ``np.asarray(data).ravel()`` which flattened the RGB cube to 3*H*W elements,
  so the finite-mask ``isfinite(data) & isfinite(lat) & ...`` raised a
  shape/broadcast error. visualize_nasa.py swallowed the failure in a
  best-effort try/except, so PNG + JSON were written but no .tif -- while the
  sidecar still advertised ``geotiff_file``.

Test strategy (mirrors tests/test_nasa_render_bugs.py):
  * geotiff_exporter.py is pure numpy/rasterio/scipy -- imported via importlib
    without triggering the full viirs package init.
  * Small synthetic arrays (20x30) with a smooth lat/lon ramp so
    LinearNDInterpolator has enough non-coplanar points to triangulate.

  1. Single-band (H, W) -> 1-band GeoTIFF (thermal regression guard).
  2. Three-band (H, W, 3) RGB -> 3-band GeoTIFF. FAILS against the old code
     (raises); passes after the fix -- the regression gate for this bug.
  3. RGB path tolerates non-finite (masked -> NaN) lat/lon pixels without raising.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("scipy")

# ---------------------------------------------------------------------------
# Repo root / helper paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_VIIRS_DIR = _ROOT / "scripts" / "viirs"


# ---------------------------------------------------------------------------
# Utility: load a single viirs module without triggering the full package init
# (same pattern as tests/test_nasa_render_bugs.py::_load_viirs_module)
# ---------------------------------------------------------------------------

def _load_viirs_module(name: str):
    """Import scripts/viirs/<name>.py without triggering the full package init."""
    full_name = f"viirs.{name}"
    if full_name in sys.modules and not isinstance(sys.modules[full_name], MagicMock):
        return sys.modules[full_name]

    if "viirs" not in sys.modules:
        pkg = types.ModuleType("viirs")
        pkg.__path__ = [str(_VIIRS_DIR)]
        sys.modules["viirs"] = pkg

    spec = importlib.util.spec_from_file_location(full_name, _VIIRS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_exporter_cls():
    # geotiff_exporter imports ``from .models import BoundingBox`` -- load models
    # first so the relative import resolves inside the lightweight viirs package.
    _load_viirs_module("models")
    mod = _load_viirs_module("geotiff_exporter")
    return mod.GeoTIFFExporter


# ---------------------------------------------------------------------------
# Synthetic swath fixtures: smooth lat/lon ramp (non-coplanar) so the
# Delaunay triangulation in LinearNDInterpolator is well defined.
# ---------------------------------------------------------------------------

def _ramp_geo(h: int = 20, w: int = 30):
    """Return (lat, lon) ramps of shape (H, W) over a small geographic box."""
    lat_vec = np.linspace(40.0, 42.0, h)
    lon_vec = np.linspace(2.0, 5.0, w)
    lon, lat = np.meshgrid(lon_vec, lat_vec)
    return lat.astype(np.float64), lon.astype(np.float64)


# ===========================================================================
# Test 1 -- single-band regression guard (thermal path unchanged)
# ===========================================================================

def test_export_nasa_single_band_writes_one_band(tmp_path):
    """Single-band (H, W) swath -> 1-band float32 EPSG:4326 GeoTIFF."""
    GeoTIFFExporter = _load_exporter_cls()
    exporter = GeoTIFFExporter()

    lat, lon = _ramp_geo(20, 30)
    # Smooth scalar field (e.g. brightness temperature).
    data = (280.0 + lat).astype(np.float32)

    out = tmp_path / "thermal.tif"
    result = exporter.export_nasa(data=data, lat=lat, lon=lon, output_path=out)

    assert result == out
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.count == 1
        assert ds.crs.to_epsg() == 4326
        assert ds.width > 0 and ds.height > 0
        assert ds.dtypes[0] == "float32"


# ===========================================================================
# Test 2 -- 3-band RGB (THE regression gate for the bug)
# ===========================================================================

def test_export_nasa_rgb_writes_three_bands(tmp_path):
    """Three-band (H, W, 3) RGB swath -> 3-band float32 EPSG:4326 GeoTIFF.

    FAILS against the pre-fix code (ravel() mismatched data vs lat/lon lengths
    raised a shape error); PASSES after export_nasa handles (H, W, 3).
    """
    GeoTIFFExporter = _load_exporter_cls()
    exporter = GeoTIFFExporter()

    lat, lon = _ramp_geo(20, 30)
    h, w = lat.shape
    # Distinct smooth channels so interpolation has real structure per band.
    r = (lat - 40.0) / 2.0
    g = (lon - 2.0) / 3.0
    b = ((lat - 40.0) + (lon - 2.0)) / 5.0
    rgb = np.stack([r, g, b], axis=-1).astype(np.float32)
    assert rgb.shape == (h, w, 3)

    out = tmp_path / "true_color.tif"
    result = exporter.export_nasa(data=rgb, lat=lat, lon=lon, output_path=out)

    assert result == out
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.count == 3
        assert ds.crs.to_epsg() == 4326
        assert ds.width > 0 and ds.height > 0
        assert all(dt == "float32" for dt in ds.dtypes)


# ===========================================================================
# Test 3 -- RGB path tolerates non-finite (masked -> NaN) pixels
# ===========================================================================

def test_export_nasa_rgb_handles_nonfinite_pixels(tmp_path):
    """RGB export must not raise when some lat/lon/channel pixels are NaN.

    visualize_nasa.py fills masked swath pixels with np.nan before calling
    export_nasa, so non-finite pixels are the normal case.
    """
    GeoTIFFExporter = _load_exporter_cls()
    exporter = GeoTIFFExporter()

    lat, lon = _ramp_geo(20, 30)
    r = (lat - 40.0) / 2.0
    g = (lon - 2.0) / 3.0
    b = ((lat - 40.0) + (lon - 2.0)) / 5.0
    rgb = np.stack([r, g, b], axis=-1).astype(np.float32)

    # Punch holes: masked geolocation and masked channel values -> NaN.
    lat[0, 0] = np.nan
    lon[1, 2] = np.nan
    rgb[5, 5, :] = np.nan
    rgb[7, 8, 1] = np.nan  # a single channel non-finite -> whole pixel dropped

    out = tmp_path / "true_color_nan.tif"
    # Must not raise.
    result = exporter.export_nasa(data=rgb, lat=lat, lon=lon, output_path=out)

    assert out.exists()
    with rasterio.open(result) as ds:
        assert ds.count == 3
        assert ds.crs.to_epsg() == 4326
