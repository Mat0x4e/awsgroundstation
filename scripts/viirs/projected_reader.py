"""Read the georeferenced composites SatDump produces itself.

SatDump knows the VIIRS scan model -- the aggregation zones in
``forced_gcps_x``, the 112 degree scan, the -0.05 roll and 0.15 yaw
corrections in ``resources/projections_settings/jpss1_viirs.json`` -- and it
holds the per-scan timestamps inside the product. Given a ``project`` block in
its composite config it raytraces every line to the ellipsoid and writes
``rgb_<name>_projected.tif``, an equirectangular GeoTIFF.

That is the geolocation the pipeline should use. ``scan_geometry`` remains as
a fallback for products decoded before projection was enabled, but it
reimplements this model from the outside and is less accurate: it has no
access to the pointing corrections and reconstructs line times from a rate.

The GeoTIFF carries its own extent, so nothing here has to know any geometry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# rgb_True Color_projected.tif, rgb_Day Microphysics_projected.tif, ...
PROJECTED_PATTERN = "*_projected.tif"
_NAME_RE = re.compile(r"^(?:rgb_)?(?P<name>.+?)_projected$", re.IGNORECASE)


@dataclass(frozen=True)
class ProjectedComposite:
    """One georeferenced composite, as SatDump projected it."""

    path: Path
    composite_type: str
    data: np.ndarray          # (H, W) or (H, W, 3), float32 in [0, 1], NaN = no data
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


def composite_name_from(path: Path) -> str:
    """"rgb_True Color_projected.tif" -> "True Color"."""
    match = _NAME_RE.match(path.stem)
    return (match.group("name") if match else path.stem).replace("_", " ").strip()


def find_projected(folder: Path) -> list[Path]:
    """Projected GeoTIFFs in *folder*, in a stable order."""
    return sorted(Path(folder).rglob(PROJECTED_PATTERN))


def read_projected(path: Path) -> Optional[ProjectedComposite]:
    """Load one projected GeoTIFF, or None when it cannot be used.

    Returns None rather than raising so a single unreadable file cannot cost
    the whole contact its imagery.
    """
    try:
        import rasterio
    except ImportError:
        logger.warning("rasterio is not available -- cannot read %s", path.name)
        return None

    try:
        with rasterio.open(path) as src:
            bounds = src.bounds
            bands = src.read().astype(np.float32)
            nodata = src.nodata

        if bands.shape[0] >= 3:
            data = np.transpose(bands[:3], (1, 2, 0))
        else:
            data = bands[0]

        # Normalise to [0, 1]: SatDump writes 8- or 16-bit composites.
        peak = float(np.nanmax(data)) if data.size else 0.0
        if peak > 1.0:
            data = data / (65535.0 if peak > 255.0 else 255.0)

        if nodata is not None and not np.isnan(nodata):
            data = np.where(data == nodata, np.nan, data)
        # Fully black pixels are outside the swath, not black ground.
        blank = np.all(data <= 0.0, axis=-1) if data.ndim == 3 else (data <= 0.0)
        data = np.where(blank[..., None] if data.ndim == 3 else blank, np.nan, data)

    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        logger.warning("Could not read projected composite %s: %s", path.name, exc)
        return None

    logger.info(
        "Projected composite %s: %dx%d, lat=[%.3f, %.3f] lon=[%.3f, %.3f]",
        path.name, data.shape[1], data.shape[0],
        bounds.bottom, bounds.top, bounds.left, bounds.right,
    )

    return ProjectedComposite(
        path=path,
        composite_type=composite_name_from(path),
        data=data,
        lat_min=float(bounds.bottom),
        lat_max=float(bounds.top),
        lon_min=float(bounds.left),
        lon_max=float(bounds.right),
    )
