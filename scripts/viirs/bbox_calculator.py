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
from datetime import datetime, timezone
from pathlib import Path

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
    "NOAA 20\n"
    "1 43013U 17073A   24001.50000000  .00000060  00000-0  34000-4 0  9994\n"
    "2 43013  98.7190 100.4910 0001200  83.1200 277.0100 14.19530100327801"
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
                return self._from_tle(cbor_meta.timestamp)
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

        # Extend lat AND lon by the physical cross-track half-swath
        mean_altitude = sum(altitudes) / len(altitudes) if altitudes else SATELLITE_ALTITUDE_KM
        half_angle_rad = math.radians(scan_angle / 2.0)
        cross_track_km = mean_altitude * math.tan(half_angle_rad)
        cross_track_deg = cross_track_km / 111.0

        logger.debug(
            "_from_ephemeris: mean_alt=%.1f km cross_track=%.1f km (%.3f°) "
            "nadir=[%.3f,%.3f]×[%.3f,%.3f]",
            mean_altitude, cross_track_km, cross_track_deg,
            nadir_bbox.lat_min, nadir_bbox.lat_max,
            nadir_bbox.lon_min, nadir_bbox.lon_max,
        )

        return BoundingBox(
            lat_min=max(-90.0, nadir_bbox.lat_min - cross_track_deg),
            lat_max=min(90.0, nadir_bbox.lat_max + cross_track_deg),
            lon_min=max(-180.0, nadir_bbox.lon_min - cross_track_deg),
            lon_max=min(180.0, nadir_bbox.lon_max + cross_track_deg),
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

    def _from_tle(self, timestamp: datetime) -> BoundingBox:
        """Propagate NOAA-20 orbit via sgp4 and return a swath bounding box.

        Steps:
          1. Fetch TLE from CelesTrak (fall back to env var TLE_FALLBACK or
             the embedded TLE if the fetch fails).
          2. Parse with sgp4.api.Satrec.
          3. Propagate every 30 s over PASS_DURATION_MINUTES.
          4. Convert ECI → geodetic (lat/lon).
          5. Compute nadir bbox from ground track min/max.
          6. Extend lat/lon by the VIIRS cross-track swath width.
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

        step_seconds = 30
        n_steps = (PASS_DURATION_MINUTES * 60) // step_seconds + 1

        for i in range(n_steps):
            t = ts_utc.replace(
                second=(ts_utc.second + i * step_seconds) % 60,
                minute=ts_utc.minute
                + (ts_utc.second + i * step_seconds) // 60,
            )
            # Recompute properly to avoid minute/hour overflow
            from datetime import timedelta

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

        # Cross-track extension
        cross_track_km = SATELLITE_ALTITUDE_KM * math.tan(
            math.radians(VIIRS_CROSS_TRACK_ANGLE_DEG)
        )
        cross_track_deg = cross_track_km / 111.0  # ~111 km/degree

        return BoundingBox(
            lat_min=max(-90.0, nadir_bbox.lat_min - cross_track_deg),
            lat_max=min(90.0, nadir_bbox.lat_max + cross_track_deg),
            lon_min=max(-180.0, nadir_bbox.lon_min - cross_track_deg),
            lon_max=min(180.0, nadir_bbox.lon_max + cross_track_deg),
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
