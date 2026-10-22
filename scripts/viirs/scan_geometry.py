"""Per-pixel geolocation of a SatDump VIIRS swath, from the CBOR ephemeris.

DEPRECATED -- fallback only
---------------------------
SatDump georeferences its own composites when its config carries a ``project``
block, and those ``rgb_<name>_projected.tif`` files are the geolocation of
record; see ``projected_reader`` and ``docker/sdr-pipeline/enable_satdump_projection.py``.
It owns the model this module reconstructs -- ``resources/projections_settings/
jpss1_viirs.json`` and ``plugins/jpss_support/.../viirs_proj.h`` -- including
two things reachable only from inside it:

* the pointing corrections ``roll_offset`` -0.05 and ``yaw_offset`` 0.15, which
  nothing here applies;
* the per-scan timestamps SatDump stores in the product's ``images[]`` entries
  (17 values 1.7866 s apart, expanded by ``interpolate_timestamps_scantime``),
  where this module reconstructs line times from a rate and a centring rule,
  and the composite is cropped relative to the bands those timestamps belong to.

Keep it for products decoded before projection was enabled. Do not extend it;
fix the SatDump config instead.

Why it exists at all
--------------------
The pipeline used to render a swath into an axis-aligned latitude/longitude
box and stretch it linearly. No box can fit a swath: the image is a curved,
tilted strip roughly 3,000 km across and 200 km along, and the box enclosing
it is several times its area. Contact #5 came out as 941 x 2,930 km for a
~200 x 3,000 km strip, which is why the coastlines never lined up.

SatDump ships what is needed to do it properly. ``product.cbor`` carries a
``projection_cfg`` with a ``viirs_single_line`` scan model and 30 ephemeris
points -- position *and* velocity in ECI -- spanning exactly the chunk.

Their timestamps look like garbage: they read as year 1890. They are not
broken, they are 2**32 seconds low, a signed/unsigned wraparound. Unwrapped,
they land on the acquisition time SatDump records independently in its own
``dataset.json``. That is what makes this module possible; without it the
reader discards the ephemeris and the whole chain falls back to a TLE.

The wrap leaks into the frame, and this is the subtle part. SatDump rotated
these vectors with its own wrapped clock, so they are Earth-fixed coordinates
turned by ``GMST(wrapped)`` rather than by ``GMST(true)`` -- for contact #5
that is a 133.86 degree error in longitude, a third of the way round the
planet. Undoing it means rotating with the *raw* timestamp, while using the
unwrapped one for everything time-like. Get this right and the 30 ephemeris
points agree with an independent SGP4 propagation of NOAA-20 to 0.8 km.

What it does
------------
For each image row, interpolate the satellite state and sweep the scan angle
across that row's columns. Each look ray is intersected with the WGS84
ellipsoid, and the intersection rotated from ECI to Earth-fixed by GMST at the
row's time. The result is a latitude and longitude for every pixel, which
``resample_to_equirect`` uses to put pixels where they belong.

Columns do not map linearly onto the scan angle. VIIRS aggregates samples in
zones across the scan -- 3:1 near nadir, then 2:1, then 1:1 at the edges -- so
a pixel's angular width changes three times per side. ``forced_gcps_x`` in the
CBOR gives those zone boundaries, and honouring them is not optional: a linear
mapping agrees with the coastlines 91% of the time near nadir and 53% at the
swath edge, which is to say not at all.

The zones also check out physically. On a 6400-pixel scan the boundaries give
12,608 sample units across 112.06 degrees, so a 3:1 nadir pixel spans 0.0267
degrees -- 375 m from 828 km, which is exactly VIIRS' I-band resolution.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# A 32-bit wraparound in the timestamps SatDump writes into product.cbor.
EPOCH_WRAP_SECONDS = 2 ** 32

# Bounds used to detect the wrap; outside these no acquisition is plausible.
MIN_PLAUSIBLE_EPOCH = 631152000.0   # 1990-01-01T00:00:00Z
MAX_PLAUSIBLE_EPOCH = 4102444800.0  # 2100-01-01T00:00:00Z

WGS84_A_KM = 6378.137
WGS84_F = 1.0 / 298.257223563
WGS84_B_KM = WGS84_A_KM * (1.0 - WGS84_F)
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)


def unwrap_epoch(timestamp: float) -> Optional[float]:
    """Undo the 2**32 s wraparound in a SatDump CBOR timestamp.

    Returns None when no number of wraps lands in a plausible range, so a
    genuinely broken value is rejected rather than silently shifted.
    """
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return None

    for _ in range(4):
        if MIN_PLAUSIBLE_EPOCH <= value <= MAX_PLAUSIBLE_EPOCH:
            return value
        value += EPOCH_WRAP_SECONDS
    return None


def gmst_rad(unix_seconds):
    """Greenwich Mean Sidereal Time in radians. Scalar or array."""
    julian_date = np.asarray(unix_seconds, dtype=float) / 86400.0 + 2440587.5
    t = (julian_date - 2451545.0) / 36525.0
    seconds = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t ** 2
        - 6.2e-6 * t ** 3
    )
    return np.radians((seconds / 240.0) % 360.0)


def eci_to_ecef(points, unix_seconds):
    """Rotate ECI vectors into the Earth-fixed frame about the shared z axis."""
    theta = np.asarray(gmst_rad(unix_seconds), dtype=float)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    x, y, z = points[..., 0], points[..., 1], points[..., 2]
    return np.stack([x * cos_t + y * sin_t, -x * sin_t + y * cos_t, z], axis=-1)


def ecef_to_geodetic(points):
    """ECEF km to geodetic latitude/longitude in degrees, via Bowring.

    Geodetic, not geocentric: the two differ by up to 0.19 deg near 45 deg
    latitude, about 21 km on the ground, which matters at this accuracy.
    """
    x, y, z = points[..., 0], points[..., 1], points[..., 2]
    lon = np.arctan2(y, x)

    p = np.hypot(x, y)
    theta = np.arctan2(z * WGS84_A_KM, p * WGS84_B_KM)
    e_prime_sq = (WGS84_A_KM ** 2 - WGS84_B_KM ** 2) / WGS84_B_KM ** 2
    lat = np.arctan2(
        z + e_prime_sq * WGS84_B_KM * np.sin(theta) ** 3,
        p - WGS84_E2 * WGS84_A_KM * np.cos(theta) ** 3,
    )
    return np.degrees(lat), np.degrees(lon)


@dataclass(frozen=True)
class SwathGeometry:
    """Latitude and longitude for every pixel of a swath image."""

    lat: np.ndarray
    lon: np.ndarray

    @property
    def valid(self) -> np.ndarray:
        return np.isfinite(self.lat) & np.isfinite(self.lon)


class SwathGeolocator:
    """Geolocates a scan-line image from satellite state vectors."""

    def __init__(
        self,
        times: np.ndarray,
        positions_eci_km: np.ndarray,
        velocities_eci: np.ndarray,
        scan_angle_deg: float = 112.0,
        scan_direction: int = 1,
        time_increases_with_row: bool = True,
        rotation_epoch_offset: float = 0.0,
        zone_boundaries: Optional[list] = None,
        scan_width_px: Optional[int] = None,
        line_period_seconds: Optional[float] = None,
    ) -> None:
        times = np.asarray(times, dtype=float)
        order = np.argsort(times)
        self.times = times[order]
        self.positions = np.asarray(positions_eci_km, dtype=float)[order]
        self.velocities = np.asarray(velocities_eci, dtype=float)[order]
        self.scan_angle_deg = float(scan_angle_deg)
        self.scan_direction = 1 if scan_direction >= 0 else -1
        self.time_increases_with_row = bool(time_increases_with_row)
        # Added to a true time to recover the clock the stored frame was
        # rotated with. Zero for a well-formed ephemeris; -2**32 for one that
        # SatDump wrote through a wrapped timestamp.
        self.rotation_epoch_offset = float(rotation_epoch_offset)
        self.zone_boundaries = (
            sorted(float(b) for b in zone_boundaries) if zone_boundaries else None
        )
        self.scan_width_px = int(scan_width_px) if scan_width_px else None
        self.line_period_seconds = (
            float(line_period_seconds) if line_period_seconds else None
        )

        if len(self.times) < 2:
            raise ValueError("At least two ephemeris points are required")

    @classmethod
    def from_projection_cfg(
        cls,
        cfg: Optional[dict],
        scan_direction: int = 1,
        time_increases_with_row: bool = True,
    ) -> Optional["SwathGeolocator"]:
        """Build from a SatDump ``projection_cfg``, or None when unusable.

        The two defaults are storage conventions, not geometry. They are
        settled against rasterised Natural Earth coastlines: classify every
        swath pixel as land or sea by colour, look up what is actually at its
        computed position, and score the correlation.

        Measured on this code as assembled, ``scan_direction=1`` with
        ``time_increases_with_row=True`` scores 90.6% agreement (MCC 0.741),
        with the best rigid offset at exactly zero. The other three
        combinations score 85.2%, 52.3% and 52.2% -- the last two being no
        correlation at all, i.e. a swath rotated 180 degrees.

        These values were briefly shipped inverted. The measurement that chose
        them had been taken through a monkeypatched ``row_times`` in a test
        script rather than against the class, and the two differed by exactly
        that rotation. Re-measure here, not in a harness, if either default is
        ever revisited.
        """
        if not cfg:
            return None

        ephemeris = cfg.get("ephemeris")
        if not ephemeris or len(ephemeris) < 2:
            logger.info("No usable ephemeris in projection_cfg")
            return None

        times, positions, velocities, wrap_offsets = [], [], [], []
        for point in ephemeris:
            try:
                raw = float(point["timestamp"])
                stamp = unwrap_epoch(raw)
                if stamp is None:
                    continue
                times.append(stamp)
                wrap_offsets.append(raw - stamp)
                positions.append([point["x"], point["y"], point["z"]])
                velocities.append([point["vx"], point["vy"], point["vz"]])
            except (KeyError, TypeError, ValueError):
                continue

        if len(times) < 2:
            logger.warning(
                "Ephemeris present but fewer than two points survived unwrapping "
                "-- falling back to the bounding-box chain"
            )
            return None

        offset = float(cfg.get("timestamp_offset") or 0.0)

        # Every point should carry the same wrap; if they disagree, the frame
        # is not recoverable by a single rotation and the ephemeris is refused.
        wrap = min(wrap_offsets)
        if max(wrap_offsets) - wrap > 1.0:
            logger.warning(
                "Ephemeris timestamps wrap inconsistently (%.0f..%.0f) -- refusing",
                wrap, max(wrap_offsets),
            )
            return None

        logger.info(
            "Ephemeris: %d points spanning %.1f s from %.1f "
            "(timestamp offset %.1f s, frame epoch offset %.0f s)",
            len(times), max(times) - min(times), min(times), offset, wrap,
        )

        return cls(
            times=np.asarray(times) + offset,
            positions_eci_km=np.asarray(positions),
            velocities_eci=np.asarray(velocities),
            scan_angle_deg=float(cfg.get("scan_angle") or 112.0),
            scan_direction=scan_direction,
            time_increases_with_row=time_increases_with_row,
            rotation_epoch_offset=wrap,
            zone_boundaries=cfg.get("forced_gcps_x"),
            scan_width_px=cfg.get("image_width"),
            line_period_seconds=_line_period(cfg),
        )

    def column_angles(self, width: int) -> np.ndarray:
        """Scan angle, in radians, at the centre of each of *width* columns.

        VIIRS aggregates samples in symmetric zones -- 3:1 near nadir, 2:1,
        then 1:1 at the edges -- so a pixel's angular width is not constant.
        ``forced_gcps_x`` marks the zone boundaries on the scan the CBOR
        describes; they are rescaled here to whatever width the composite
        actually has, since a composite may be a decimated version of it.

        Falls back to a linear sweep when no boundaries are available, which
        is right for a sensor that does not aggregate and wrong only toward
        the edges for one that does.
        """
        half = math.radians(self.scan_angle_deg / 2.0)
        if not self.zone_boundaries or width < 4:
            return np.linspace(-half, half, width)

        reference_width = self.scan_width_px or (max(self.zone_boundaries) + 1)
        scale = width / float(reference_width)
        edges = [b * scale for b in self.zone_boundaries]

        # Aggregation factor per pixel: 1 at the edges, 2, then 3 across the
        # middle. The centre boundary marks nadir and carries no change.
        columns = np.arange(width, dtype=float) + 0.5
        centre = width / 2.0
        distance = np.abs(columns - centre)

        # One of the boundaries marks nadir itself rather than a zone edge --
        # it sits at the centre and must not be mistaken for the inner edge of
        # the 3:1 zone, or that zone collapses to nothing.
        offsets = [
            abs(e - centre) for e in edges if abs(e - centre) > 0.05 * centre
        ]
        if not offsets:
            return np.linspace(-half, half, width)
        outer, inner = max(offsets), min(offsets)

        factor = np.full(width, 3.0)
        factor[distance >= inner] = 2.0
        factor[distance >= outer] = 1.0

        # Angle at each pixel centre: cumulative sample units, centred, scaled
        # so the outermost pixels sit at +/- half the scan angle.
        cumulative = np.cumsum(factor) - factor / 2.0
        cumulative -= cumulative[len(cumulative) // 2]
        span = (np.sum(factor) - (factor[0] + factor[-1]) / 2.0) / 2.0
        return cumulative / span * half

    def _state_at(self, row_times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Interpolate position and velocity onto the row times."""
        position = np.stack(
            [np.interp(row_times, self.times, self.positions[:, i]) for i in range(3)],
            axis=-1,
        )
        velocity = np.stack(
            [np.interp(row_times, self.times, self.velocities[:, i]) for i in range(3)],
            axis=-1,
        )
        return position, velocity

    def row_times(self, height: int) -> np.ndarray:
        """Acquisition time of each image row.

        A composite does not span the whole ephemeris window, and does not
        start at its beginning. The CBOR gives the line rate -- ``scan_time``
        divided by ``interpolate_timestamps`` lines per scan, 0.0555 s for
        VIIRS -- so the rows are clocked at that rate and **centred** in the
        ephemeris window, which carries margin either side of the imagery.

        Contact #5 shows why both parts matter. Its 256 rows are 14.2 s of
        imagery inside a 29 s ephemeris: spreading them over the whole window
        is a 2x along-track scale error, and clocking them correctly but from
        the window's start leaves the strip ~50 km south of where it belongs.
        Centring puts it right, and matches the offset found empirically
        against coastlines (+8 s measured, +7.4 s predicted).
        """
        if self.line_period_seconds:
            span = (height - 1) * self.line_period_seconds
            midpoint = 0.5 * (self.times[0] + self.times[-1])
            times = (
                midpoint - span / 2.0
                + np.arange(height) * self.line_period_seconds
            )
        else:
            times = np.linspace(self.times[0], self.times[-1], height)
        return times if self.time_increases_with_row else times[::-1]

    def locate(self, height: int, width: int) -> SwathGeometry:
        """Latitude and longitude for every pixel of a height x width image."""
        row_times = self.row_times(height)

        position, velocity = self._state_at(row_times)

        # Nadir, and the orbit normal the scan sweeps through.
        nadir = -position / np.linalg.norm(position, axis=-1, keepdims=True)
        normal = np.cross(position, velocity)
        normal = normal / np.linalg.norm(normal, axis=-1, keepdims=True)
        normal = normal * self.scan_direction

        angles = self.column_angles(width)

        look = (
            np.cos(angles)[None, :, None] * nadir[:, None, :]
            + np.sin(angles)[None, :, None] * normal[:, None, :]
        )

        ground = self._intersect_ellipsoid(position[:, None, :], look)
        # Rotate with the clock the stored frame was built on, not the true
        # one -- see the module docstring on the wrapped-GMST frame.
        rotation_times = row_times + self.rotation_epoch_offset
        ground_ecef = eci_to_ecef(ground, rotation_times[:, None])
        lat, lon = ecef_to_geodetic(ground_ecef)
        return SwathGeometry(lat=lat, lon=lon)

    def subsatellite_track(self, samples: int = 2) -> tuple[np.ndarray, np.ndarray]:
        """Nadir latitude/longitude along the chunk -- handy for validation.

        Uses an odd column count so that one column sits exactly at nadir; a
        single column would land on the scan-angle range's first endpoint,
        which is the swath edge, ~1,500 km away from the track.
        """
        geometry = self.locate(samples, 3)
        return geometry.lat[:, 1], geometry.lon[:, 1]

    @staticmethod
    def _intersect_ellipsoid(origin: np.ndarray, look: np.ndarray) -> np.ndarray:
        """First intersection of a ray with the WGS84 ellipsoid, else NaN.

        Scaling the axes turns the ellipsoid into a unit sphere, so this is an
        ordinary quadratic. The ellipsoid is symmetric about the z axis, so it
        is the same shape in ECI as in ECEF and the intersection may be done
        in either frame.
        """
        scale = np.array([WGS84_A_KM, WGS84_A_KM, WGS84_B_KM])
        o = origin / scale
        d = look / scale

        a = np.sum(d * d, axis=-1)
        b = 2.0 * np.sum(o * d, axis=-1)
        c = np.sum(o * o, axis=-1) - 1.0

        discriminant = b * b - 4.0 * a * c
        missed = discriminant < 0
        discriminant = np.where(missed, np.nan, discriminant)

        distance = (-b - np.sqrt(discriminant)) / (2.0 * a)
        point = origin + distance[..., None] * look
        return np.where(missed[..., None], np.nan, point)


def _line_period(cfg: dict) -> Optional[float]:
    """Seconds between image rows, from whatever the CBOR states.

    ``interpolate_timestamps_scantime`` gives it directly; otherwise it is the
    scan time divided by the number of lines per scan. Returns None when
    neither is present, and the caller then spreads rows across the ephemeris.
    """
    direct = cfg.get("interpolate_timestamps_scantime")
    if direct:
        return float(direct)

    lines_per_scan = cfg.get("interpolate_timestamps")
    scan_time = (cfg.get("timefilter") or {}).get("scan_time")
    if lines_per_scan and scan_time:
        return float(scan_time) / float(lines_per_scan)
    return None


def _fill_small_holes(grid: np.ndarray, passes: int = 2) -> np.ndarray:
    """Fill single-pixel gaps left by scattering a swath onto a finer grid.

    Neighbour fill, a couple of passes. Anything larger than that is genuinely
    outside the swath and must stay transparent rather than be invented.
    """
    for _ in range(passes):
        holes = ~np.isfinite(grid if grid.ndim == 2 else grid[..., 0])
        if not holes.any():
            break
        for shift, axis in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
            if not holes.any():
                break
            candidate = np.roll(grid, shift, axis=axis)
            usable = np.isfinite(candidate if grid.ndim == 2 else candidate[..., 0])
            take = holes & usable
            grid[take] = candidate[take]
            holes &= ~take
    return grid


def resample_to_equirect(
    data: np.ndarray,
    geometry: SwathGeometry,
    max_dimension: int = 4000,
):
    """Place swath pixels where they belong, on a north-up lat/lon grid.

    Returns ``(grid, (lat_min, lat_max, lon_min, lon_max))``, or None when the
    swath cannot be gridded this way -- currently only when it crosses the
    antimeridian, which no rectangle in these coordinates can express.

    Forward scatter rather than an inverse warp: every source pixel is thrown
    at the cell its own coordinates name. Output resolution is chosen to match
    the source across track, so gaps are at most a pixel wide and get filled;
    everything outside the swath stays NaN and renders transparent.
    """
    lat, lon = geometry.lat, geometry.lon
    valid = np.isfinite(lat) & np.isfinite(lon)
    if data.ndim == 3:
        valid &= np.isfinite(data).all(axis=-1)
    else:
        valid &= np.isfinite(data)
    if not valid.any():
        logger.warning("Nothing to resample: no pixel has both data and a position")
        return None

    lat_valid, lon_valid = lat[valid], lon[valid]
    lat_min, lat_max = float(lat_valid.min()), float(lat_valid.max())
    lon_min, lon_max = float(lon_valid.min()), float(lon_valid.max())

    if lon_max - lon_min > 180.0:
        logger.warning(
            "Swath spans %.1f deg of longitude -- it crosses the antimeridian, "
            "which this grid cannot express; falling back",
            lon_max - lon_min,
        )
        return None

    width = data.shape[1]
    step_lon = (lon_max - lon_min) / min(max_dimension, max(width, 2))
    mean_lat = math.radians((lat_min + lat_max) / 2.0)
    # Square-ish cells on the ground, not in degrees.
    step_lat = max(step_lon * max(math.cos(mean_lat), 0.05), 1e-6)

    n_cols = int(min(max_dimension, math.ceil((lon_max - lon_min) / step_lon) + 1))
    n_rows = int(min(max_dimension, math.ceil((lat_max - lat_min) / step_lat) + 1))
    step_lon = (lon_max - lon_min) / max(n_cols - 1, 1)
    step_lat = (lat_max - lat_min) / max(n_rows - 1, 1)

    shape = (n_rows, n_cols) + ((data.shape[2],) if data.ndim == 3 else ())
    grid = np.full(shape, np.nan, dtype=np.float32)

    rows = np.clip(((lat_max - lat_valid) / step_lat).astype(int), 0, n_rows - 1)
    cols = np.clip(((lon_valid - lon_min) / step_lon).astype(int), 0, n_cols - 1)
    grid[rows, cols] = data[valid]

    filled = np.isfinite(grid if grid.ndim == 2 else grid[..., 0]).sum()
    logger.info(
        "Resampled %d swath pixels onto %d x %d cells (%.4f deg lon, %.4f deg lat); "
        "%.1f%% of the grid covered",
        int(valid.sum()), n_rows, n_cols, step_lon, step_lat,
        100.0 * filled / (n_rows * n_cols),
    )

    return _fill_small_holes(grid), (lat_min, lat_max, lon_min, lon_max)


def orient_for_display(data: np.ndarray, north_up: bool) -> np.ndarray:
    """Orient an array for a renderer whose y axis runs bottom-up.

    A raw SatDump swath is stored in scan order, which for a descending pass
    puts south at the top and reverses the scan across track, so it needs both
    flips before display.

    A grid that ``resample_to_equirect`` has produced is already north-up and
    east-right. Flipping it across track mirrors the imagery about the middle
    of its own bounding box: the map overlay, computed from geographic
    coordinates, stays put while the coastlines underneath move -- which looks
    like a swath curving the wrong way against a correctly drawn map.
    """
    if north_up:
        return np.flipud(data)
    return np.flipud(np.fliplr(data))
