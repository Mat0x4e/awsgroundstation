"""Reading the composites SatDump georeferences itself.

These are the geolocation of record: SatDump owns the VIIRS scan model and the
per-scan timestamps, so its projected GeoTIFFs beat anything reconstructed
downstream. scan_geometry stays only as a fallback.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_pkg = types.ModuleType("viirs")
_pkg.__path__ = [str(_ROOT / "scripts" / "viirs")]
sys.modules.setdefault("viirs", _pkg)
_spec = importlib.util.spec_from_file_location(
    "viirs.projected_reader", _ROOT / "scripts" / "viirs" / "projected_reader.py"
)
pr = importlib.util.module_from_spec(_spec)
sys.modules["viirs.projected_reader"] = pr
_spec.loader.exec_module(pr)

# Only the GeoTIFF reading needs rasterio; naming and discovery do not, and
# rasterio ships in the image rather than the local environment.
needs_rasterio = pytest.mark.skipif(
    importlib.util.find_spec("rasterio") is None, reason="rasterio only ships in the image"
)


def _write_geotiff(path: Path, data: np.ndarray, bounds, nodata=None):
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS

    bands = np.transpose(data, (2, 0, 1)) if data.ndim == 3 else data[np.newaxis]
    height, width = data.shape[:2]
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width,
        count=bands.shape[0], dtype=bands.dtype,
        crs=CRS.from_epsg(4326),
        transform=from_bounds(bounds[2], bounds[0], bounds[3], bounds[1], width, height),
        nodata=nodata,
    ) as dst:
        dst.write(bands)


class TestNaming:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("rgb_True Color_projected.tif", "True Color"),
            ("rgb_Day Microphysics_projected.tif", "Day Microphysics"),
            ("True Color_projected.tif", "True Color"),
            ("rgb_Thermal_IR_projected.tif", "Thermal IR"),
        ],
    )
    def test_composite_name_is_recovered_from_the_filename(self, filename, expected):
        assert pr.composite_name_from(Path(filename)) == expected


class TestDiscovery:
    def test_projected_files_are_found_under_chunk_folders(self, tmp_path):
        for chunk in ("chunk_0", "chunk_1"):
            folder = tmp_path / chunk / "VIIRS"
            folder.mkdir(parents=True)
            (folder / "rgb_True Color_projected.tif").touch()
            (folder / "viirs_rgb_True_Color.png").touch()

        found = pr.find_projected(tmp_path)

        assert len(found) == 2
        assert all(p.name.endswith("_projected.tif") for p in found)

    def test_an_empty_folder_yields_nothing_rather_than_failing(self, tmp_path):
        assert pr.find_projected(tmp_path) == []


@needs_rasterio
class TestReading:
    def test_extent_comes_from_the_geotiff_not_from_us(self, tmp_path):
        """No bounding box to guess: SatDump wrote it into the file."""
        path = tmp_path / "rgb_True Color_projected.tif"
        data = np.full((8, 16, 3), 200, dtype=np.uint8)
        _write_geotiff(path, data, bounds=(36.0, 42.0, -2.0, 33.0))

        composite = pr.read_projected(path)

        assert composite is not None
        assert composite.composite_type == "True Color"
        assert composite.lat_min == pytest.approx(36.0)
        assert composite.lat_max == pytest.approx(42.0)
        assert composite.lon_min == pytest.approx(-2.0)
        assert composite.lon_max == pytest.approx(33.0)

    def test_eight_bit_values_are_normalised(self, tmp_path):
        path = tmp_path / "rgb_True Color_projected.tif"
        _write_geotiff(path, np.full((4, 4, 3), 255, np.uint8), (0.0, 1.0, 0.0, 1.0))

        composite = pr.read_projected(path)

        assert np.nanmax(composite.data) == pytest.approx(1.0)

    def test_black_pixels_become_transparent_not_black_ground(self, tmp_path):
        """A projected swath does not fill its bounding box."""
        path = tmp_path / "rgb_True Color_projected.tif"
        data = np.zeros((4, 4, 3), np.uint8)
        data[1, 1] = 200
        _write_geotiff(path, data, (0.0, 1.0, 0.0, 1.0))

        composite = pr.read_projected(path)

        assert np.isfinite(composite.data[1, 1]).all()
        assert np.isnan(composite.data[0, 0]).all()

    def test_single_band_is_read_as_two_dimensions(self, tmp_path):
        path = tmp_path / "rgb_Thermal IR_projected.tif"
        _write_geotiff(path, np.full((4, 6), 128, np.uint8), (0.0, 1.0, 0.0, 1.0))

        composite = pr.read_projected(path)

        assert composite.data.ndim == 2
        assert composite.data.shape == (4, 6)

    def test_an_unreadable_file_returns_none_rather_than_raising(self, tmp_path):
        """One bad file must not cost the contact its other composites."""
        path = tmp_path / "rgb_True Color_projected.tif"
        path.write_bytes(b"not a GeoTIFF")

        assert pr.read_projected(path) is None
