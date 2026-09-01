"""Bounding box calculator for VIIRS passes.

Priority order:
  1. .georef file (corner coordinates JSON)
  2. CBOR projection metadata
  3. TLE + sgp4 orbit propagation (CelesTrak, fallback to embedded TLE)

Produces BoundingBox(lat_min, lat_max, lon_min, lon_max) in WGS84 degrees.
Raises NoBBoxSourceError if no source is available.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import BoundingBox, CBORMetadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOAA20_NORAD_ID = 43013
VIIRS_CROSS_TRACK_ANGLE_DEG = 56.0
PASS_DURATION_MINUTES = 10
EARTH_RADIUS_KM = 6371.0
SATELLITE_ALTITUDE_KM = 824.0

# Environment-configurable CelesTrak endpoint
CELESTRAK_URL_DEFAULT = (
    "https://celestrak.org/NORAD/elements/gp.php?CATNR=43013&FORMAT=3LE"
)

# Embedded fallback TLE (NOAA-20, epoch ~2024 — used only if live fetch fails
# and TLE_FALLBACK env var is not set).  Update periodically for accuracy.
_EMBEDDED_TLE = (
    "NOAA 20 (JPSS-1)\n"
    "1 43013U 17073A   26182.16908491  .00000066  00000+0  52219-4 0  9990\n"
    "2 43013  98.7773 121.5075 0000816 133.1891 226.9353 14.19514284446458"
)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class NoBBoxSourceError(Exception):
    """Raised when no bounding box source is available."""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class BBoxCalculator:
    """Compute geographic bounding box from .georef, CBOR projection, or TLE."""

    NOAA20_NORAD_ID = NOAA20_NORAD_ID
    VIIRS_CROSS_TRACK_ANGLE_DEG = VIIRS_CROSS_TRACK_ANGLE_DEG
    CELESTRAK_URL = property(
        lambda self: os.environ.get("TLE_URL", CELESTRAK_URL_DEFAULT)
    )

    def compute(
        self,
        cbor_meta: CBORMetadata,
        folder: Path,
        duration_seconds: float | None = None,
    ) -> BoundingBox:
        """Return a BoundingBox using the highest-priority available source.

        Priority:
          1. Ephemeris from CBOR projection_cfg (ECI positions)
          2. Any *.georef file found in *folder*
          3. CBOR projection_coords from *cbor_meta*
          4. TLE/sgp4 propagation from cbor_meta.timestamp

        Parameters
        ----------
        cbor_meta:
            Metadata extracted from the SatDump product.cbor file.
        folder:
            Directory to scan for *.georef files (source 2).
        duration_seconds:
            Along-track window to propagate for source 4, in seconds. Defaults
            to PASS_DURATION_MINUTES. Pass the duration the image actually
            covers -- a SatDump composite spans one chunk (~30 s), and
            propagating a whole 10 min pass for it overstates the box roughly
            twentyfold.

        Raises NoBBoxSourceError if none of the four sources can produce a result.
        """
        # 1. Ephemeris from CBOR projection_cfg
        if cbor_meta.ephemeris:
            try:
                return self._from_ephemeris(cbor_meta.ephemeris, cbor_meta.scan_angle)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to compute bbox from ephemeris: %s", exc)

        # 2. .georef file
        georef_files = list(folder.glob("*.georef"))
        if georef_files:
            try:
                return self._from_georef(georef_files[0])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to read .georef file %s: %s", georef_files[0], exc)

        # 3. CBOR projection coordinates
        if cbor_meta.projection_coords:
            try:
                return self._from_cbor_projection(cbor_meta.projection_coords)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to extract bbox from CBOR projection: %s", exc)

        # 4. TLE propagation
        if cbor_meta.timestamp is not None:
            try:
                return self._from_tle(cbor_meta.timestamp, duration_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to compute bbox from TLE: %s", exc)

        raise NoBBoxSourceError(
            "No bounding box source available: no ephemeris, no .georef file, no CBOR "
            "projection coordinates, and no valid timestamp for TLE propagation."
        )

    # ------------------------------------------------------------------
    # Source 0: Ephemeris from CBOR projection_cfg (ECI positions)
    # ------------------------------------------------------------------

    def _from_ephemeris(
        self,
        ephemeris: list[dict],
        scan_angle: float = 112.0,
    ) -> BoundingBox:
        """Compute swath bounding box from ECI ephemeris positions.

        Algorithm
        ---------
        For each point in the ephemeris list:
          - lat = degrees(asin(z / r))   — exact (ECI z = geographic z)
          - lon = degrees(atan2(y, x))   — ECI inertial longitude; error is
            Earth rotation × elapsed time.  For a ~30 s pass the drift is
            ~0.12° — negligible for bbox purposes.

        The nadir ground track is extended laterally by the physical cross-track
        half-swath width (altitude × tan(scan_angle/2)).  This gives the true
        geographic swath extent regardless of the image pixel dimensions.

        Parameters
        ----------
        ephemeris:
            List of dicts with at minimum keys ``x``, ``y``, ``z`` (km, ECI).
        scan_angle:
            Total scan angle in degrees (default 112° = ±56° for VIIRS M-band).

        Returns
        -------
        BoundingBox
            WGS84 bounding box covering the full swath.

        Raises
        ------
        ValueError
            If ephemeris contains no valid ECI positions.
        """
        lats: list[float] = []
        lons: list[float] = []
        altitudes: list[float] = []

        for point in ephemeris:
            try:
                x = float(point["x"])
                y = float(point["y"])
                z = float(point["z"])
            except (KeyError, TypeError, ValueError):
                continue

            r = math.sqrt(x * x + y * y + z * z)
            if r < 1.0:
                # Degenerate position — skip
                continue

            lat = math.degrees(math.asin(max(-1.0, min(1.0, z / r))))
            lon = math.degrees(math.atan2(y, x))
            altitude_km = r - EARTH_RADIUS_KM

            lats.append(lat)
            lons.append(lon)
            altitudes.append(altitude_km)

        if not lats:
            raise ValueError(
                "_from_ephemeris: no valid ECI positions found in ephemeris list"
            )

        nadir_bbox = _make_bbox(lats, lons)

        mean_altitude = sum(altitudes) / len(altitudes) if altitudes else SATELLITE_ALTITUDE_KM

        logger.debug(
            "_from_ephemeris: mean_alt=%.1f km nadir=[%.3f,%.3f]x[%.3f,%.3f]",
            mean_altitude,
            nadir_bbox.lat_min, nadir_bbox.lat_max,
            nadir_bbox.lon_min, nadir_bbox.lon_max,
        )

        return _extend_by_swath(
            nadir_bbox, lats, lons, mean_altitude, scan_angle / 2.0
        )

    # ------------------------------------------------------------------
    # Source 1: .georef JSON
    # ------------------------------------------------------------------

    def _from_georef(self, path: Path) -> BoundingBox:
        """Read corner coordinates from a .georef JSON file.

        Expected JSON structure::

            {
              "top_left":     {"lat": 55.1, "lon": 2.3},
              "top_right":    {"lat": 55.0, "lon": 18.4},
              "bottom_left":  {"lat": 45.2, "lon": 2.1},
              "bottom_right": {"lat": 45.0, "lon": 18.5}
            }
        """
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        corners = ("top_left", "top_right", "bottom_left", "bottom_right")
        lats: list[float] = []
        lons: list[float] = []

        for key in corners:
            corner = data[key]
            lats.append(float(corner["lat"]))
            lons.append(float(corner["lon"]))

        return _make_bbox(lats, lons)

    # ------------------------------------------------------------------
    # Source 2: CBOR projection coordinates
    # ------------------------------------------------------------------

    def _from_cbor_projection(self, coords: dict) -> BoundingBox:
        """Extract bounding box from SatDump CBOR projection metadata.

        Handles two common shapes:

        * ``{"corners": {"top_left": {"lat": ..., "lon": ...}, ...}}``
        * ``{"min_lat": ..., "max_lat": ..., "min_lon": ..., "max_lon": ...}``
        * ``{"tl_lat": ..., "tl_lon": ..., "tr_lat": ..., "tr_lon": ...,
             "bl_lat": ..., "bl_lon": ..., "br_lat": ..., "br_lon": ...}``
        """
        # Shape A: explicit min/max
        if all(k in coords for k in ("min_lat", "max_lat", "min_lon", "max_lon")):
            return BoundingBox(
                lat_min=float(coords["min_lat"]),
                lat_max=float(coords["max_lat"]),
                lon_min=float(coords["min_lon"]),
                lon_max=float(coords["max_lon"]),
            )

        # Shape B: nested corners dict
        if "corners" in coords:
            corners = coords["corners"]
            corner_keys = ("top_left", "top_right", "bottom_left", "bottom_right")
            lats = [float(corners[k]["lat"]) for k in corner_keys if k in corners]
            lons = [float(corners[k]["lon"]) for k in corner_keys if k in corners]
            if lats and lons:
                return _make_bbox(lats, lons)

        # Shape C: flat tl/tr/bl/br lat+lon keys
        tl_lat = coords.get("tl_lat") or coords.get("top_left_lat")
        tl_lon = coords.get("tl_lon") or coords.get("top_left_lon")
        tr_lat = coords.get("tr_lat") or coords.get("top_right_lat")
        tr_lon = coords.get("tr_lon") or coords.get("top_right_lon")
        bl_lat = coords.get("bl_lat") or coords.get("bottom_left_lat")
        bl_lon = coords.get("bl_lon") or coords.get("bottom_left_lon")
        br_lat = coords.get("br_lat") or coords.get("bottom_right_lat")
        br_lon = coords.get("br_lon") or coords.get("bottom_right_lon")

        raw_lats = [tl_lat, tr_lat, bl_lat, br_lat]
        raw_lons = [tl_lon, tr_lon, bl_lon, br_lon]
        if all(v is not None for v in raw_lats + raw_lons):
            return _make_bbox(
                [float(v) for v in raw_lats],
                [float(v) for v in raw_lons],
            )

        raise ValueError(
            "CBOR projection_coords has no recognisable lat/lon structure: "
            + repr(list(coords.keys()))
        )

    # ------------------------------------------------------------------
    # Source 3: TLE + sgp4 propagation
    # ------------------------------------------------------------------

    def _from_tle(
        self,
        timestamp: datetime,
        duration_seconds: float | None = None,
    ) -> BoundingBox:
        """Propagate NOAA-20 orbit via sgp4 and return a swath bounding box.

        Steps:
          1. Fetch TLE from CelesTrak (fall back to env var TLE_FALLBACK or
             the embedded TLE if the fetch fails).
          2. Parse with sgp4.api.Satrec.
          3. Propagate every 30 s over *duration_seconds* (default
             PASS_DURATION_MINUTES).
          4. Convert ECI → geodetic (lat/lon).
          5. Compute nadir bbox from ground track min/max.
          6. Extend the ground track into the swath footprint, split
             between lat and lon by the track's bearing.
          7. Clamp to valid WGS84 ranges and return.
        """
        tle_text = self._fetch_tle()
        lines = [ln.strip() for ln in tle_text.strip().splitlines() if ln.strip()]
        # Accept 2-line (no name) or 3-line (with name) TLE
        if len(lines) >= 3:
            line1, line2 = lines[-2], lines[-1]
        elif len(lines) == 2:
            line1, line2 = lines[0], lines[1]
        else:
            raise ValueError(f"Cannot parse TLE — got {len(lines)} non-empty lines")

        try:
            from sgp4.api import Satrec, jday  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "sgp4 library is required for TLE propagation. "
                "Install with: pip install sgp4"
            ) from exc

        satellite = Satrec.twoline2rv(line1, line2)

        # Ensure timestamp is UTC-aware
        if timestamp.tzinfo is None:
            ts_utc = timestamp.replace(tzinfo=timezone.utc)
        else:
            ts_utc = timestamp.astimezone(timezone.utc)

        lats: list[float] = []
        lons: list[float] = []

        window = (
            float(duration_seconds)
            if duration_seconds and duration_seconds > 0
            else PASS_DURATION_MINUTES * 60
        )
        # At least two points, so a sub-step window still yields a ground track.
        step_seconds = min(30, window / 2)
        n_steps = int(window // step_seconds) + 1
        logger.info(
            "TLE propagation window: %.0f s from %s (%d steps)",
            window, timestamp.isoformat(), n_steps,
        )

        for i in range(n_steps):
            # timedelta carries seconds into minutes, minutes into hours and
            # across a day boundary. An earlier .replace(second=..., minute=...)
            # here raised "minute must be in 0..59" the moment the seconds
            # carried -- unreachable while the ephemeris path won, and fatal as
            # soon as --contact-time forced this branch.
            t = ts_utc + timedelta(seconds=i * step_seconds)

            jd, fr = jday(
                t.year, t.month, t.day,
                t.hour, t.minute, t.second + t.microsecond / 1e6,
            )
            e, r, _ = satellite.sgp4(jd, fr)
            if e != 0:
                logger.debug("sgp4 error code %d at step %d — skipping", e, i)
                continue

            lat, lon = _eci_to_geodetic(r, jd, fr)
            lats.append(lat)
            lons.append(lon)

        if not lats:
            raise ValueError("sgp4 propagation produced no valid positions")

        # Nadir bbox
        nadir_bbox = _make_bbox(lats, lons)

        return _extend_by_swath(
            nadir_bbox,
            lats,
            lons,
            SATELLITE_ALTITUDE_KM,
            VIIRS_CROSS_TRACK_ANGLE_DEG,
            inclination_rad=getattr(satellite, "inclo", None),
        )

    # ------------------------------------------------------------------
    # TLE fetching helpers
    # ------------------------------------------------------------------

    def _fetch_tle(self) -> str:
        """Return TLE text, trying CelesTrak first then fallbacks."""
        url = os.environ.get("TLE_URL", CELESTRAK_URL_DEFAULT)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "noaa20-viirs-visualization/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                tle_text = resp.read().decode("utf-8")
            if tle_text.strip():
                logger.debug("TLE fetched from CelesTrak (%d chars)", len(tle_text))
                return tle_text
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("CelesTrak TLE fetch failed (%s) — using fallback", exc)

        # Env-var fallback
        fallback = os.environ.get("TLE_FALLBACK", "").strip()
        if fallback:
            logger.info("Using TLE_FALLBACK env var")
            return fallback

        # Embedded fallback
        logger.info("Using embedded fallback TLE for NOAA-20")
        return _EMBEDDED_TLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


KM_PER_DEG_LAT = 111.32


def _swath_half_width_km(altitude_km: float, nadir_half_angle_deg: float) -> float:
    """Ground-arc half-width of a swath scanned to *nadir_half_angle_deg*.

    Spherical, not flat. The tangent-plane form ``h * tan(eta)`` understates
    the arc badly at wide scan angles: for VIIRS (eta 56 deg, h 824 km) it
    gives 1221 km where the true ground arc is ~1500 km -- the documented
    ~3000 km swath.

    From the sensor at radius R+h, a ray at nadir angle *eta* meets the ground
    at elevation *eps*, where ``sin(eta) = (R / (R+h)) * cos(eps)``. The Earth
    central angle is then ``90 - eta - eps``, and the ground arc is R times it.
    """
    eta = math.radians(nadir_half_angle_deg)
    horizon = math.acos(EARTH_RADIUS_KM / (EARTH_RADIUS_KM + altitude_km))

    cos_eps = math.sin(eta) * (EARTH_RADIUS_KM + altitude_km) / EARTH_RADIUS_KM
    if cos_eps >= 1.0:
        # The scan angle reaches past the limb; the horizon arc is the cap.
        return EARTH_RADIUS_KM * horizon

    central_angle = math.pi / 2.0 - eta - math.acos(cos_eps)
    return EARTH_RADIUS_KM * max(0.0, min(central_angle, horizon))


def _track_bearing_rad(lats: list[float], lons: list[float]) -> Optional[float]:
    """Bearing of the ground track from north, or None if it cannot be read.

    Two distinct points are enough. Longitude differences are scaled by
    cos(lat) so the bearing is measured on the ground, not in degree space.
    """
    for lat2, lon2 in zip(reversed(lats), reversed(lons)):
        if (lat2, lon2) != (lats[0], lons[0]):
            mean_lat = math.radians((lats[0] + lat2) / 2.0)
            dlon = (lon2 - lons[0] + 540.0) % 360.0 - 180.0  # shortest way round
            return math.atan2(dlon * math.cos(mean_lat), lat2 - lats[0])
    return None


def _extend_by_swath(
    nadir: BoundingBox,
    lats: list[float],
    lons: list[float],
    altitude_km: float,
    nadir_half_angle_deg: float,
    inclination_rad: Optional[float] = None,
) -> BoundingBox:
    """Widen a nadir ground track into the swath footprint it images.

    The swath is perpendicular to the ground track, so how it splits between
    latitude and longitude depends on the track's bearing. A sun-synchronous
    track is near-polar but not vertical -- about 12 deg off the meridian at
    mid-latitudes -- so the swath spills mostly into longitude and only a
    little into latitude. Extending both by the same number of degrees, as
    this used to, overstates latitude several-fold and understates longitude
    everywhere off the equator, where a degree of longitude is shorter than a
    degree of latitude.
    """
    half_km = _swath_half_width_km(altitude_km, nadir_half_angle_deg)
    mean_lat = (nadir.lat_min + nadir.lat_max) / 2.0

    bearing = _track_bearing_rad(lats, lons)
    if bearing is None and inclination_rad is not None:
        # Single position: fall back on the orbit. For a circular orbit,
        # sin(bearing) = cos(inclination) / cos(latitude).
        cos_lat = max(1e-6, math.cos(math.radians(mean_lat)))
        bearing = math.asin(
            max(-1.0, min(1.0, math.cos(inclination_rad) / cos_lat))
        )
    if bearing is None:
        bearing = 0.0  # assume a due-north track: all swath in longitude

    lat_ext_deg = abs(half_km * math.sin(bearing)) / KM_PER_DEG_LAT

    # Over a pole every meridian is in view, and a one-sided extension would
    # still leave a wedge uncovered once clamped -- take the whole range.
    cos_lat = math.cos(math.radians(mean_lat))
    over_the_pole = cos_lat < 0.02
    lon_ext_deg = (
        180.0
        if over_the_pole
        else abs(half_km * math.cos(bearing)) / (KM_PER_DEG_LAT * cos_lat)
    )

    logger.debug(
        "swath extension: half=%.0f km bearing=%.1f deg -> lat +/-%.2f deg, lon +/-%.2f deg",
        half_km, math.degrees(bearing), lat_ext_deg, lon_ext_deg,
    )

    # Note: a box crossing the antimeridian cannot be expressed this way and
    # is clamped, same as before.
    return BoundingBox(
        lat_min=max(-90.0, nadir.lat_min - lat_ext_deg),
        lat_max=min(90.0, nadir.lat_max + lat_ext_deg),
        lon_min=-180.0 if over_the_pole else max(-180.0, nadir.lon_min - lon_ext_deg),
        lon_max=180.0 if over_the_pole else min(180.0, nadir.lon_max + lon_ext_deg),
    )


def _make_bbox(lats: list[float], lons: list[float]) -> BoundingBox:
    """Return a BoundingBox from lists of lat and lon values."""
    return BoundingBox(
        lat_min=max(-90.0, min(lats)),
        lat_max=min(90.0, max(lats)),
        lon_min=max(-180.0, min(lons)),
        lon_max=min(180.0, max(lons)),
    )


def _eci_to_geodetic(r_km: tuple[float, float, float], jd: float, fr: float) -> tuple[float, float]:
    """Convert ECI position vector to geodetic lat/lon (degrees).

    Uses the Greenwich Mean Sidereal Time (GMST) to rotate from ECI to ECEF,
    then applies a spherical-Earth geodetic conversion.  Accuracy is sufficient
    for bounding-box computation (errors < ~0.1°).
    """
    # GMST in radians (Vallado simplified formula)
    jd_ut1 = jd + fr
    t_ut1 = (jd_ut1 - 2451545.0) / 36525.0
    gmst_deg = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t_ut1
        + 0.093104 * t_ut1 ** 2
        - 6.2e-6 * t_ut1 ** 3
    ) / 240.0  # seconds → degrees (÷ 240)
    gmst_rad = math.radians(gmst_deg % 360.0)

    x_eci, y_eci, z_eci = r_km

    # Rotate ECI → ECEF
    x_ecef = x_eci * math.cos(gmst_rad) + y_eci * math.sin(gmst_rad)
    y_ecef = -x_eci * math.sin(gmst_rad) + y_eci * math.cos(gmst_rad)
    z_ecef = z_eci

    # Spherical approximation (adequate for bbox, ~0.3° error max)
    lon_rad = math.atan2(y_ecef, x_ecef)
    p = math.sqrt(x_ecef ** 2 + y_ecef ** 2)
    lat_rad = math.atan2(z_ecef, p)

    return math.degrees(lat_rad), math.degrees(lon_rad)
