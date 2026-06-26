"""Cartopy-based renderer for VIIRS visualization pipeline.

Renders georeferenced PNG images with cartographic overlays (coastlines,
borders, lakes, lat/lon grid, POI labels) for both the SatDump composite
path and the NASA SDR path.
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — must be set before pyplot import

import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.ticker as mticker
import numpy as np
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
from cartopy.mpl.gridliner import LONGITUDE_FORMATTER, LATITUDE_FORMATTER

from .models import BoundingBox, CBORMetadata, NASAMetadata


# ---------------------------------------------------------------------------
# Static POI list — global coverage (lat, lon, name)
# Includes European cities, Caribbean/Central America, Pacific islands
# ---------------------------------------------------------------------------
_POI_LIST: list[tuple[float, float, str]] = [
    # ── Europe ──
    (48.8566, 2.3522, "Paris"),
    (51.5074, -0.1278, "London"),
    (52.5200, 13.4050, "Berlin"),
    (40.4168, -3.7038, "Madrid"),
    (41.9028, 12.4964, "Rome"),
    (52.3676, 4.9041, "Amsterdam"),
    (50.8503, 4.3517, "Brussels"),
    (59.9139, 10.7522, "Oslo"),
    (59.3293, 18.0686, "Stockholm"),
    (64.1355, -21.8954, "Reykjavik"),
    (60.1699, 24.9384, "Helsinki"),
    (55.6761, 12.5683, "Copenhagen"),
    (47.3769, 8.5417, "Zurich"),
    (48.2082, 16.3738, "Vienna"),
    (50.0755, 14.4378, "Prague"),
    (47.4979, 19.0402, "Budapest"),
    (52.2297, 21.0122, "Warsaw"),
    (37.9838, 23.7275, "Athens"),
    (38.7223, -9.1393, "Lisbon"),
    (41.3851, 2.1734, "Barcelona"),
    (53.3498, -6.2603, "Dublin"),
    # ── Caribbean — countries & islands ──
    (21.5, -79.9, "Cuba"),
    (18.9, -72.3, "Haïti"),
    (18.7, -69.9, "Rép. Dominicaine"),
    (18.2, -66.5, "Porto Rico"),
    (18.1, -77.3, "Jamaïque"),
    (13.1, -59.6, "Barbade"),
    (14.6, -61.0, "Martinique"),
    (16.0, -61.7, "Guadeloupe"),
    (10.5, -61.3, "Trinidad"),
    (12.1, -68.9, "Curaçao"),
    (17.3, -62.7, "St-Kitts"),
    # ── Central America ──
    (14.6, -90.5, "Guatemala"),
    (13.7, -89.2, "El Salvador"),
    (14.1, -87.2, "Honduras"),
    (12.1, -86.3, "Nicaragua"),
    (9.9, -84.1, "Costa Rica"),
    (9.0, -79.5, "Panamá"),
    (20.5, -87.4, "Yucatán"),
    (19.4, -99.1, "México"),
    # ── South America (north) ──
    (10.5, -66.9, "Venezuela"),
    (4.7, -74.1, "Colombia"),
    (6.8, -58.2, "Guyana"),
    # ── USA (south) ──
    (25.8, -80.2, "Miami"),
    (30.3, -81.7, "Jacksonville"),
    (29.8, -90.0, "New Orleans"),
    (29.4, -98.5, "San Antonio"),
    (25.9, -97.5, "Brownsville"),
    # ── Pacific ──
    (21.3, -157.8, "Honolulu"),
    (20.9, -156.4, "Maui"),
    (19.7, -155.1, "Hawaiʻi"),
    (37.8, -122.4, "San Francisco"),
    (34.1, -118.2, "Los Angeles"),
]


class CartopyRenderer:
    """Renders georeferenced PNG with cartographic overlays via Cartopy.

    Supports two rendering paths:
    - render_satdump: SatDump PNG composites (PlateCarree projection)
    - render_nasa: NASA SDR data with per-pixel lat/lon via pcolormesh
    """

    # -----------------------------------------------------------------------
    # Constants
    # -----------------------------------------------------------------------
    DPI: int = 300
    COASTLINE_RESOLUTION: str = "10m"
    COASTLINE_COLOR: str = "white"
    COASTLINE_WIDTH: float = 0.8
    BORDER_COLOR: str = "yellow"
    BORDER_WIDTH: float = 0.6
    BORDER_STYLE: str = "--"
    LAKE_ALPHA: float = 0.2
    GRID_ALPHA: float = 0.6
    GRID_COLOR: str = "white"
    GRID_STYLE: str = "--"
    POI_BG_ALPHA: float = 0.45
    MARGIN_DEG: float = 1.5

    # (span_threshold_degrees, grid_step_degrees)
    GRID_STEPS: list[tuple[int, int]] = [(40, 10), (20, 5), (0, 2)]

    THERMAL_CMAP: str = "RdYlBu_r"
    THERMAL_COLORBAR_LABEL: str = "Valeur normalisée SatDump"
    NASA_THERMAL_RANGE: tuple[int, int] = (210, 305)  # Kelvin

    # Composite type keywords used to identify thermal imagery
    _THERMAL_KEYWORDS: tuple[str, ...] = ("thermal", "ir", "infrared")

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _auto_grid_step(self, bbox: BoundingBox) -> float:
        """Return grid step in degrees based on the largest bbox span."""
        span = max(bbox.span_lat(), bbox.span_lon())
        for threshold, step in self.GRID_STEPS:
            if span > threshold:
                return float(step)
        return 2.0

    def _add_overlays(self, ax, bbox: BoundingBox) -> None:
        """Add coastlines, country borders, lakes, and a lat/lon grid."""
        # Coastlines
        ax.add_feature(
            cfeature.NaturalEarthFeature(
                "physical", "coastline", self.COASTLINE_RESOLUTION
            ),
            edgecolor=self.COASTLINE_COLOR,
            linewidth=self.COASTLINE_WIDTH,
            facecolor="none",
            zorder=10,
        )

        # Country borders — dashed yellow
        ax.add_feature(
            cfeature.NaturalEarthFeature(
                "cultural", "admin_0_boundary_lines_land", self.COASTLINE_RESOLUTION
            ),
            edgecolor=self.BORDER_COLOR,
            linewidth=self.BORDER_WIDTH,
            linestyle=self.BORDER_STYLE,
            facecolor="none",
            zorder=11,
        )

        # Lakes — semi-transparent
        ax.add_feature(
            cfeature.NaturalEarthFeature(
                "physical", "lakes", self.COASTLINE_RESOLUTION
            ),
            facecolor=cfeature.COLORS["water"],
            alpha=self.LAKE_ALPHA,
            edgecolor="none",
            zorder=9,
        )

        # Lat/lon grid
        step = self._auto_grid_step(bbox)
        mbbox = bbox.with_margin(self.MARGIN_DEG)

        try:
            gl = ax.gridlines(
                crs=ccrs.PlateCarree(),
                draw_labels=True,
                linewidth=0.5,
                color=self.GRID_COLOR,
                alpha=self.GRID_ALPHA,
                linestyle=self.GRID_STYLE,
                zorder=12,
            )
            # Build tick locations within bbox + margin
            lon_ticks = np.arange(
                _round_to(mbbox.lon_min, step), mbbox.lon_max + step, step
            )
            lat_ticks = np.arange(
                _round_to(mbbox.lat_min, step), mbbox.lat_max + step, step
            )
            gl.xlocator = mticker.FixedLocator(lon_ticks.tolist())
            gl.ylocator = mticker.FixedLocator(lat_ticks.tolist())
            gl.xformatter = LONGITUDE_FORMATTER
            gl.yformatter = LATITUDE_FORMATTER
            gl.xlabel_style = {"size": 6, "color": self.GRID_COLOR}
            gl.ylabel_style = {"size": 6, "color": self.GRID_COLOR}
            gl.top_labels = False
            gl.right_labels = False
        except Exception:
            # Gridlines are non-critical — continue silently if they fail
            pass

    def _add_poi_labels(self, ax, bbox: BoundingBox) -> None:
        """Annotate visible POI with white text on a semi-transparent black background."""
        mbbox = bbox.with_margin(self.MARGIN_DEG)
        for lat, lon, name in _POI_LIST:
            if (
                mbbox.lat_min <= lat <= mbbox.lat_max
                and mbbox.lon_min <= lon <= mbbox.lon_max
            ):
                ax.text(
                    lon,
                    lat,
                    name,
                    transform=ccrs.PlateCarree(),
                    fontsize=5,
                    color="white",
                    ha="left",
                    va="bottom",
                    zorder=20,
                    bbox=dict(
                        boxstyle="round,pad=0.2",
                        facecolor="black",
                        alpha=self.POI_BG_ALPHA,
                        edgecolor="none",
                    ),
                )

    # -----------------------------------------------------------------------
    # Public rendering methods
    # -----------------------------------------------------------------------

    def render_satdump(
        self,
        data: np.ndarray,
        bbox: BoundingBox,
        composite_type: str,
        metadata: CBORMetadata,
        output_path: Path,
    ) -> Path:
        """Render a SatDump composite with Cartopy overlays.

        Parameters
        ----------
        data:
            Float32 array — (H, W) for thermal, (H, W, 3) for colour.
        bbox:
            Geographic extent of the data in WGS84 degrees.
        composite_type:
            Human-readable name, e.g. "True Color", "Thermal IR".
        metadata:
            CBORMetadata instance for timestamp and satellite name.
        output_path:
            Destination PNG file path.

        Returns
        -------
        Path
            The written output file path.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        mbbox = bbox.with_margin(self.MARGIN_DEG)
        center_lat, center_lon = bbox.center()

        # Use PlateCarree without central_longitude offset — avoids coordinate
        # shift between the map projection and the data extent.
        projection = ccrs.PlateCarree()
        data_crs = ccrs.PlateCarree()

        # Bug fix 1 — figure size matches geographic aspect ratio so the image
        # fills the extent without pixel-aspect distortion.
        lon_span = mbbox.lon_max - mbbox.lon_min
        lat_span = mbbox.lat_max - mbbox.lat_min
        fig_width = 12
        fig_height = fig_width * (lat_span / lon_span)
        fig = plt.figure(figsize=(fig_width, fig_height), dpi=self.DPI)

        ax = fig.add_subplot(1, 1, 1, projection=projection)
        ax.set_extent(
            [mbbox.lon_min, mbbox.lon_max, mbbox.lat_min, mbbox.lat_max],
            crs=data_crs,
        )
        ax.set_facecolor("black")

        is_thermal = any(kw in composite_type.lower() for kw in self._THERMAL_KEYWORDS)

        if is_thermal:
            # Single-band float32 → pseudo-colour
            band = data[:, :, 0] if data.ndim == 3 else data
            # SatDump descending pass: south at top → flipud; east-west scan inverted → fliplr
            band = band[::-1, ::-1]
            im = ax.imshow(
                band,
                origin="upper",
                extent=[bbox.lon_min, bbox.lon_max, bbox.lat_min, bbox.lat_max],
                transform=data_crs,
                cmap=self.THERMAL_CMAP,
                vmin=0.0,
                vmax=1.0,
                interpolation="bilinear",
                zorder=1,
            )
            cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.04, shrink=0.8)
            cbar.set_label(self.THERMAL_COLORBAR_LABEL, fontsize=7, color="white")
            cbar.ax.yaxis.set_tick_params(color="white", labelsize=6)
            plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")
        else:
            # RGB or single-band colour
            if data.ndim == 2:
                rgb = np.stack([data, data, data], axis=-1)
            else:
                rgb = data
            # Clip to [0, 1] defensively
            rgb = np.clip(rgb, 0.0, 1.0)
            # SatDump descending pass: south at top → flipud; east-west scan inverted → fliplr
            rgb = rgb[::-1, ::-1, :]
            ax.imshow(
                rgb,
                origin="upper",
                extent=[bbox.lon_min, bbox.lon_max, bbox.lat_min, bbox.lat_max],
                transform=data_crs,
                interpolation="bilinear",
                zorder=1,
            )

        self._add_overlays(ax, bbox)
        self._add_poi_labels(ax, bbox)

        # Title
        datetime_str = _format_cbor_datetime(metadata)
        satellite = metadata.satellite or "NOAA-20"
        title_line1 = f"{composite_type} — {datetime_str} — {satellite}"
        title_line2 = "Calibration communautaire SatDump — non certifiée NOAA"
        ax.set_title(f"{title_line1}\n{title_line2}", fontsize=7, color="white", pad=4)

        fig.patch.set_facecolor("black")
        fig.tight_layout(pad=0.5)
        fig.savefig(output_path, dpi=self.DPI, bbox_inches="tight", facecolor="black")
        plt.close(fig)

        return output_path

    def render_nasa(
        self,
        data: np.ndarray,
        lat: np.ndarray,
        lon: np.ndarray,
        mode: str,
        metadata: NASAMetadata,
        output_path: Path,
    ) -> Path:
        """Render NASA SDR data with per-pixel lat/lon using pcolormesh.

        Parameters
        ----------
        data:
            Float32 array — (H, W) for thermal (Kelvin), (H, W, 3) for RGB.
        lat:
            Per-pixel latitude array (H, W), float32.
        lon:
            Per-pixel longitude array (H, W), float32.
        mode:
            Rendering mode, e.g. "True Color", "Thermal".
        metadata:
            NASAMetadata instance for title construction.
        output_path:
            Destination PNG file path.

        Returns
        -------
        Path
            The written output file path.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Derive bbox from lat/lon arrays
        valid_lat = lat[np.isfinite(lat)]
        valid_lon = lon[np.isfinite(lon)]
        if valid_lat.size == 0 or valid_lon.size == 0:
            raise ValueError("render_nasa: lat/lon arrays contain no finite values")

        bbox = BoundingBox(
            lat_min=float(valid_lat.min()),
            lat_max=float(valid_lat.max()),
            lon_min=float(valid_lon.min()),
            lon_max=float(valid_lon.max()),
        )
        mbbox = bbox.with_margin(self.MARGIN_DEG)
        _, center_lon = bbox.center()

        projection = ccrs.PlateCarree(central_longitude=center_lon)
        data_crs = ccrs.PlateCarree()

        fig = plt.figure(figsize=(10, 8), dpi=self.DPI)
        ax = fig.add_subplot(1, 1, 1, projection=projection)
        ax.set_extent(
            [mbbox.lon_min, mbbox.lon_max, mbbox.lat_min, mbbox.lat_max],
            crs=data_crs,
        )
        ax.set_facecolor("black")

        is_thermal = any(kw in mode.lower() for kw in self._THERMAL_KEYWORDS)

        if is_thermal:
            band = data[:, :, 0] if data.ndim == 3 else data
            vmin_k, vmax_k = self.NASA_THERMAL_RANGE
            pcm = ax.pcolormesh(
                lon,
                lat,
                band,
                transform=data_crs,
                cmap=self.THERMAL_CMAP,
                vmin=vmin_k,
                vmax=vmax_k,
                shading="auto",
                zorder=1,
            )
            # Primary colorbar in Kelvin
            cbar = fig.colorbar(pcm, ax=ax, fraction=0.03, pad=0.04, shrink=0.8)
            cbar.set_label("Température de brillance (K)", fontsize=7, color="white")
            cbar.ax.yaxis.set_tick_params(color="white", labelsize=6)
            plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")

            # Secondary axis in °C
            ax2 = cbar.ax.twinx()
            ax2.set_ylim(vmin_k - 273.15, vmax_k - 273.15)
            ax2.yaxis.set_tick_params(color="white", labelsize=6)
            ax2.set_ylabel("°C", fontsize=7, color="white")
            plt.setp(ax2.yaxis.get_ticklabels(), color="white")
        else:
            # RGB True Color — normalise to [0, 1] and use imshow over pcolormesh
            # for correct colour rendering; pcolormesh only supports scalar fields
            if data.ndim == 3:
                rgb = np.clip(data, 0.0, 1.0)
            else:
                rgb = np.stack(
                    [np.clip(data, 0.0, 1.0)] * 3, axis=-1
                )
            # Use imshow bounded by derived bbox
            ax.imshow(
                rgb,
                origin="upper",
                extent=[bbox.lon_min, bbox.lon_max, bbox.lat_min, bbox.lat_max],
                transform=data_crs,
                interpolation="bilinear",
                zorder=1,
            )

        self._add_overlays(ax, bbox)
        self._add_poi_labels(ax, bbox)

        # Title
        datetime_str = metadata.datetime_utc or "unknown"
        orbit = metadata.orbit_number
        granule = metadata.granule_id
        title = f"{mode} — {datetime_str} — Orbit {orbit} — {granule}"
        ax.set_title(title, fontsize=7, color="white", pad=4)

        fig.patch.set_facecolor("black")
        fig.tight_layout(pad=0.5)
        fig.savefig(output_path, dpi=self.DPI, bbox_inches="tight", facecolor="black")
        plt.close(fig)

        return output_path


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _round_to(value: float, step: float) -> float:
    """Round *value* down to the nearest multiple of *step*."""
    return np.floor(value / step) * step


def _format_cbor_datetime(metadata: CBORMetadata) -> str:
    """Return a human-readable UTC datetime string from CBORMetadata."""
    if metadata.timestamp is not None:
        try:
            return metadata.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        except AttributeError:
            pass
    return "unknown"
