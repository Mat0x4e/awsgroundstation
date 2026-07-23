"""GeoTIFF export for VIIRS visualization pipeline.

Supports two export paths:
- SatDump: georeferenced composite arrays with a known BoundingBox
- NASA: swath data interpolated onto a regular lat/lon grid via scipy
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from .models import BoundingBox


class GeoTIFFExporter:
    """Exports data as GeoTIFF with EPSG:4326 CRS.

    Requirements 12.1, 12.2, 12.3.
    """

    TARGET_CRS = "EPSG:4326"
    NASA_RESOLUTION_DEG = 0.0067  # ~750 m
    QUERY_BUDGET = 500_000  # max grid points evaluated per strip (memory bound)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def export_satdump(
        self,
        data: np.ndarray,
        bbox: BoundingBox,
        output_path: Path,
    ) -> Path:
        """Export a SatDump composite array as a float32 GeoTIFF.

        Parameters
        ----------
        data:
            Float32 array of shape (H, W) for single-band (Thermal IR)
            or (H, W, 3) for three-band (True Color / False Color).
        bbox:
            Geographic bounding box in WGS84 degrees.
        output_path:
            Destination file path (will be created or overwritten).

        Returns
        -------
        Path
            The *output_path* that was written.
        """
        output_path = Path(output_path)
        data = np.asarray(data, dtype=np.float32)

        if data.ndim == 3:
            # (H, W, 3) → rasterio wants (bands, H, W)
            height, width, _ = data.shape
            bands = np.transpose(data, (2, 0, 1))  # (3, H, W)
            count = 3
        elif data.ndim == 2:
            height, width = data.shape
            bands = data[np.newaxis, :, :]  # (1, H, W)
            count = 1
        else:
            raise ValueError(
                f"data must be 2-D (H, W) or 3-D (H, W, 3), got shape {data.shape}"
            )

        transform = from_bounds(
            west=bbox.lon_min,
            south=bbox.lat_min,
            east=bbox.lon_max,
            north=bbox.lat_max,
            width=width,
            height=height,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            output_path,
            mode="w",
            driver="GTiff",
            height=height,
            width=width,
            count=count,
            dtype=rasterio.float32,
            crs=CRS.from_epsg(4326),
            transform=transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(bands)

        return output_path

    def export_nasa(
        self,
        data: np.ndarray,
        lat: np.ndarray,
        lon: np.ndarray,
        output_path: Path,
    ) -> Path:
        """Interpolate swath data onto a regular grid and export as GeoTIFF.

        Parameters
        ----------
        data:
            1-D or 2-D float array of measured values (flattened inside).
        lat:
            Array of per-pixel latitudes (same shape as *data*).
        lon:
            Array of per-pixel longitudes (same shape as *data*).
        output_path:
            Destination file path.

        Returns
        -------
        Path
            The *output_path* that was written.
        """
        # LinearNDInterpolator lets us build the triangulation once and evaluate
        # the target grid in strips, bounding peak memory — a single full-grid
        # query can allocate multiple GiB and OOM on modest containers.
        from scipy.interpolate import LinearNDInterpolator  # heavy dep — import lazily

        output_path = Path(output_path)
        data_flat = np.asarray(data, dtype=np.float32).ravel()
        lat_flat = np.asarray(lat, dtype=np.float64).ravel()
        lon_flat = np.asarray(lon, dtype=np.float64).ravel()

        # Drop fill values / masked pixels
        valid = np.isfinite(data_flat) & np.isfinite(lat_flat) & np.isfinite(lon_flat)
        data_flat = data_flat[valid]
        lat_flat = lat_flat[valid]
        lon_flat = lon_flat[valid]

        lat_min, lat_max = float(lat_flat.min()), float(lat_flat.max())
        lon_min, lon_max = float(lon_flat.min()), float(lon_flat.max())

        res = self.NASA_RESOLUTION_DEG
        grid_lon = np.arange(lon_min, lon_max, res)
        grid_lat = np.arange(lat_min, lat_max, res)
        width = len(grid_lon)
        height = len(grid_lat)

        interp = LinearNDInterpolator(
            np.column_stack((lon_flat, lat_flat)), data_flat
        )

        # Evaluate in row-strips of at most ~QUERY_BUDGET points to cap memory.
        gridded = np.empty((height, width), dtype=np.float32)
        rows_per_strip = max(1, min(height, self.QUERY_BUDGET // max(width, 1)))
        for r0 in range(0, height, rows_per_strip):
            r1 = min(r0 + rows_per_strip, height)
            gx, gy = np.meshgrid(grid_lon, grid_lat[r0:r1])
            gridded[r0:r1, :] = interp(gx, gy).astype(np.float32)

        # rasterio north-up: first row = northernmost latitude
        gridded = np.flipud(gridded)

        transform = from_bounds(
            west=lon_min,
            south=lat_min,
            east=lon_max,
            north=lat_max,
            width=width,
            height=height,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            output_path,
            mode="w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype=rasterio.float32,
            crs=CRS.from_epsg(4326),
            transform=transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(gridded[np.newaxis, :, :])

        return output_path

    # ------------------------------------------------------------------ #
    # Feature flag                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def is_enabled() -> bool:
        """Return True when GeoTIFF export is enabled via environment variable."""
        return os.environ.get("ENABLE_GEOTIFF", "false").lower() == "true"
