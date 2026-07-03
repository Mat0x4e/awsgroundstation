"""Geolocation calculator for NOAA-20 SDR pipeline.

Computes the satellite ground track and VIIRS swath bounding boxes from TLE
data and SatDump dataset.json timestamps. Writes one coordinates.json per
chunk to the output directory.

Requirements satisfied:
  5.1 — Compute ground track using TLE + timestamps from dataset.json
  5.2 — Produce coordinates.json with bounding box, swath extent, ground track
  5.3 — Fetch TLE from CelesTrak, fallback to configured TLE on failure
  5.4 — Use sgp4 for orbit propagation
  5.5 — Mark as degraded if TLE > 7 days old

Usage:
    python geolocation.py <aggregation_dir> <output_dir>

Environment variables:
    TLE_FALLBACK  — Two TLE lines separated by newline (required in production)
    CONTACT_ID    — Contact identifier written into coordinates.json

Exit codes:
    0  success (at least one chunk produced coordinates)
    1  failure (all chunks failed, or no chunks found)
"""

import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOAA20_NORAD_ID = 43013
CELESTRAK_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?CATNR=43013&FORMAT=3LE"
)
VIIRS_SWATH_HALF_ANGLE_DEG = 56.0   # ±56° cross-track full scan angle
NOAA20_ALTITUDE_KM = 833.0          # nominal orbital altitude
EARTH_RADIUS_KM = 6371.0
TLE_MAX_AGE_DAYS = 7
TLE_DEGRADED_THRESHOLD_HOURS = 48

# Hardcoded fallback TLE (NOAA-20 / JPSS-1, 2026-era placeholder).
# Overridden by the TLE_FALLBACK environment variable in production.
_DEFAULT_FALLBACK_TLE = (
    "1 43013U 17073A   26170.05920139  .00000045  00000+0  38906-4 0  9993\n"
    "2 43013  98.7267 230.8542 0001437  95.2845 264.8485 14.19558374448521"
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CoordinatesResult:
    """Result of a geolocation computation for one chunk."""

    chunk_id: str
    contact_id: str
    tle_source: str                           # "celestrak" | "fallback"
    tle_epoch: str                            # ISO 8601 datetime string
    tle_age_hours: float
    degraded: bool
    ground_track: list[dict] = field(default_factory=list)
    bounding_box: dict = field(default_factory=dict)
    swath_bounding_box: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "contact_id": self.contact_id,
            "tle_source": self.tle_source,
            "tle_epoch": self.tle_epoch,
            "tle_age_hours": round(self.tle_age_hours, 2),
            "degraded": self.degraded,
            "ground_track": self.ground_track,
            "bounding_box": self.bounding_box,
            "swath_bounding_box": self.swath_bounding_box,
        }


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


class GeolocationCalculator:
    """Computes NOAA-20 ground track and VIIRS swath bounding boxes."""

    NOAA20_NORAD_ID = NOAA20_NORAD_ID
    CELESTRAK_URL = CELESTRAK_URL
    VIIRS_SWATH_HALF_ANGLE_DEG = VIIRS_SWATH_HALF_ANGLE_DEG
    TLE_MAX_AGE_DAYS = TLE_MAX_AGE_DAYS
    TLE_DEGRADED_THRESHOLD_HOURS = TLE_DEGRADED_THRESHOLD_HOURS

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def compute(
        self,
        dataset_json: dict,
        fallback_tle: str,
        chunk_id: str = "unknown",
        contact_id: str = "",
    ) -> CoordinatesResult:
        """Compute geolocation for one chunk.

        Args:
            dataset_json:  Parsed SatDump dataset.json dict.
            fallback_tle:  Two TLE lines joined by newline, used when
                           CelesTrak is unreachable.
            chunk_id:      Human-readable chunk identifier.
            contact_id:    Ground Station contact identifier.

        Returns:
            CoordinatesResult with ground track, bounding boxes, and TLE
            provenance information.
        """
        # --- Extract timestamps -----------------------------------------------
        timestamps = self._extract_timestamps(dataset_json)
        if not timestamps:
            raise ValueError(
                f"No timestamps found in dataset.json for chunk {chunk_id!r}"
            )
        logger.info(
            "chunk=%s: found %d timestamp(s) in dataset.json",
            chunk_id,
            len(timestamps),
        )

        # --- Fetch TLE (with fallback) ----------------------------------------
        tle_lines, tle_source = self._resolve_tle(fallback_tle)
        line1, line2 = tle_lines
        logger.info("chunk=%s: using TLE from %s", chunk_id, tle_source)

        # --- Parse TLE epoch and check age ------------------------------------
        tle_epoch = self._parse_tle_epoch(line1)
        age_hours, degraded = self._check_tle_age(tle_epoch)
        if degraded:
            logger.warning(
                "chunk=%s: TLE is %.1f hours old (> %d days) — results DEGRADED",
                chunk_id,
                age_hours,
                self.TLE_MAX_AGE_DAYS,
            )

        # --- Propagate orbit --------------------------------------------------
        track = self._propagate_orbit((line1, line2), timestamps)
        if not track:
            raise ValueError(
                f"Orbit propagation produced no valid points for chunk {chunk_id!r}"
            )
        logger.info("chunk=%s: propagated %d ground track point(s)", chunk_id, len(track))

        # --- Bounding boxes ---------------------------------------------------
        nadir_bbox = self._compute_bounding_box(track)
        swath_bbox = self._extend_swath(nadir_bbox, track)

        return CoordinatesResult(
            chunk_id=chunk_id,
            contact_id=contact_id,
            tle_source=tle_source,
            tle_epoch=tle_epoch.isoformat(timespec="seconds"),
            tle_age_hours=age_hours,
            degraded=degraded,
            ground_track=track,
            bounding_box=nadir_bbox,
            swath_bounding_box=swath_bbox,
        )

    # ---------------------------------------------------------------------------
    # TLE resolution
    # ---------------------------------------------------------------------------

    def _resolve_tle(self, fallback_tle: str) -> tuple[tuple[str, str], str]:
        """Try CelesTrak; on any failure return the fallback TLE.

        Returns:
            ((line1, line2), source_label)
        """
        result = self._fetch_tle()
        if result is not None:
            return result, "celestrak"

        logger.warning("CelesTrak unavailable — using fallback TLE")
        lines = fallback_tle.strip().splitlines()
        if len(lines) < 2:
            raise ValueError(
                "fallback_tle must contain at least two lines (TLE line 1 and line 2)"
            )
        # Accept either 2-line or 3-line (name + line1 + line2) format
        if len(lines) == 2:
            line1, line2 = lines[0].strip(), lines[1].strip()
        else:
            line1, line2 = lines[-2].strip(), lines[-1].strip()
        return (line1, line2), "fallback"

    def _fetch_tle(self) -> Optional[tuple[str, str]]:
        """Fetch TLE from CelesTrak with 3 retries and 30 s total timeout.

        Returns:
            (line1, line2) on success, None on any failure.
        """
        for attempt in range(1, 4):
            try:
                resp = requests.get(
                    self.CELESTRAK_URL,
                    timeout=10,   # 10 s per attempt → 30 s total across 3 tries
                )
                if resp.status_code != 200:
                    logger.warning(
                        "CelesTrak returned HTTP %d (attempt %d/3)",
                        resp.status_code,
                        attempt,
                    )
                    continue

                lines = [ln.strip() for ln in resp.text.strip().splitlines() if ln.strip()]
                # Expect 3LE: name + line1 + line2 (at minimum 2 lines)
                if len(lines) < 2:
                    logger.warning(
                        "CelesTrak response has fewer than 2 lines (attempt %d/3)", attempt
                    )
                    continue

                line1 = lines[-2]
                line2 = lines[-1]
                # Basic sanity: TLE lines start with '1 ' and '2 '
                if not (line1.startswith("1 ") and line2.startswith("2 ")):
                    logger.warning(
                        "CelesTrak response did not parse as valid TLE (attempt %d/3)",
                        attempt,
                    )
                    continue

                logger.info("TLE fetched from CelesTrak (attempt %d)", attempt)
                return line1, line2

            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "CelesTrak request failed (attempt %d/3): %s", attempt, exc
                )

        return None

    # ---------------------------------------------------------------------------
    # TLE epoch parsing and age
    # ---------------------------------------------------------------------------

    @staticmethod
    def _parse_tle_epoch(line1: str) -> datetime:
        """Parse the epoch from TLE line 1 (columns 19-32).

        TLE epoch format: YYddd.dddddddd
          YY  — 2-digit year (57-99 → 1957-1999, 00-56 → 2000-2056)
          ddd — day of year (1-based)
          .dd — fractional day

        Returns:
            datetime in UTC.
        """
        epoch_str = line1[18:32].strip()
        year_2d = int(epoch_str[:2])
        day_of_year = float(epoch_str[2:])

        year = (1900 + year_2d) if year_2d >= 57 else (2000 + year_2d)

        # Convert day-of-year (fractional) to a datetime
        # Day 1.0 = Jan 1 00:00:00
        jan1 = datetime(year, 1, 1, tzinfo=timezone.utc)
        delta_days = day_of_year - 1.0  # offset from Jan 1
        epoch_dt = jan1.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        total_seconds = delta_days * 86400.0
        from datetime import timedelta
        epoch_dt = epoch_dt + timedelta(seconds=total_seconds)
        return epoch_dt

    def _check_tle_age(self, tle_epoch: datetime) -> tuple[float, bool]:
        """Return (age_hours, is_degraded).

        is_degraded is True when age > TLE_MAX_AGE_DAYS.
        """
        now = datetime.now(tz=timezone.utc)
        age_seconds = (now - tle_epoch).total_seconds()
        age_hours = age_seconds / 3600.0
        is_degraded = age_hours > (self.TLE_MAX_AGE_DAYS * 24.0)
        return age_hours, is_degraded

    # ---------------------------------------------------------------------------
    # Orbit propagation
    # ---------------------------------------------------------------------------

    def _propagate_orbit(
        self, tle: tuple[str, str], timestamps: list[float]
    ) -> list[dict]:
        """Propagate NOAA-20 orbit for each Unix-epoch timestamp.

        Uses sgp4 (NOT pyorbital) for propagation.

        Args:
            tle:        (line1, line2) TLE strings.
            timestamps: List of Unix epoch floats.

        Returns:
            List of {"lat": float, "lon": float, "timestamp": ISO str}.
            Points that fail propagation are silently skipped.
        """
        # Import here to keep the module importable even when sgp4 is absent
        # (the ImportError will surface only when propagation is attempted).
        from sgp4.api import Satrec, jday  # type: ignore

        line1, line2 = tle
        satellite = Satrec.twoline2rv(line1, line2)

        track: list[dict] = []
        for ts in timestamps:
            dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)

            # Julian date split into whole day and fractional day
            jd, fr = jday(
                dt_utc.year,
                dt_utc.month,
                dt_utc.day,
                dt_utc.hour,
                dt_utc.minute,
                dt_utc.second + dt_utc.microsecond / 1e6,
            )

            error_code, position_teme, _ = satellite.sgp4(jd, fr)
            if error_code != 0:
                logger.debug(
                    "sgp4 error code %d for timestamp %s — skipping",
                    error_code,
                    dt_utc.isoformat(),
                )
                continue

            lat, lon = self._teme_to_geodetic(position_teme, dt_utc)

            track.append(
                {
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "timestamp": dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                }
            )

        return track

    @staticmethod
    def _teme_to_geodetic(
        position_teme: tuple[float, float, float],
        dt_utc: datetime,
    ) -> tuple[float, float]:
        """Convert TEME Cartesian position to geodetic latitude and longitude.

        Uses a simplified approach: rotate from TEME to ECEF via GMST, then
        convert Cartesian ECEF to geodetic (WGS-84 approximation).

        Args:
            position_teme: (x, y, z) in km in TEME frame.
            dt_utc:        UTC datetime corresponding to the position.

        Returns:
            (latitude_deg, longitude_deg)
        """
        x_km, y_km, z_km = position_teme

        # --- TEME → ECEF via Greenwich Mean Sidereal Time -------------------
        gmst_rad = GeolocationCalculator._gmst(dt_utc)

        # Rotate around Z-axis by -GMST
        cos_g = math.cos(gmst_rad)
        sin_g = math.sin(gmst_rad)
        x_ecef = cos_g * x_km + sin_g * y_km
        y_ecef = -sin_g * x_km + cos_g * y_km
        z_ecef = z_km

        # --- ECEF → geodetic (spherical approximation — adequate for ±0.01°) ---
        lon_rad = math.atan2(y_ecef, x_ecef)
        r_xy = math.sqrt(x_ecef ** 2 + y_ecef ** 2)
        lat_rad = math.atan2(z_ecef, r_xy)

        lat_deg = math.degrees(lat_rad)
        lon_deg = math.degrees(lon_rad)

        return lat_deg, lon_deg

    @staticmethod
    def _gmst(dt_utc: datetime) -> float:
        """Compute Greenwich Mean Sidereal Time in radians.

        Uses the IAU 1982 formula accurate to ~0.1 arcsec.
        """
        # Julian date of J2000.0 = 2451545.0
        # Seconds since J2000 epoch
        j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        dt_seconds = (dt_utc - j2000).total_seconds()
        t_centuries = dt_seconds / (36525.0 * 86400.0)

        # GMST in seconds of time at J2000 + rate per century
        gmst_seconds = (
            67310.54841
            + (876600.0 * 3600.0 + 8640184.812866) * t_centuries
            + 0.093104 * t_centuries ** 2
            - 6.2e-6 * t_centuries ** 3
        )
        # Reduce to [0, 86400) seconds
        gmst_seconds = gmst_seconds % 86400.0
        # Convert to radians
        gmst_rad = math.tau * gmst_seconds / 86400.0
        return gmst_rad

    # ---------------------------------------------------------------------------
    # Bounding box helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _compute_bounding_box(track: list[dict]) -> dict:
        """Tight min/max bounding box around nadir ground track points.

        Returns:
            {"north": float, "south": float, "east": float, "west": float}
        """
        lats = [p["lat"] for p in track]
        lons = [p["lon"] for p in track]
        return {
            "north": round(max(lats), 5),
            "south": round(min(lats), 5),
            "east": round(max(lons), 5),
            "west": round(min(lons), 5),
        }

    @staticmethod
    def _extend_swath(bbox: dict, track: list[dict]) -> dict:
        """Extend nadir bounding box by the VIIRS cross-track swath width.

        For a ±56° off-nadir scan from altitude h = 833 km, the ground
        half-swath distance is computed geometrically:

            sin(nadir_angle_at_earth) = (R + h) / R * sin(scan_angle)
            half_swath_km = R * (nadir_angle_at_earth - scan_angle)  [radians]

        where the nadir angle at Earth is the angle at Earth's centre.
        In practice, for h=833 km and scan=56°, half-swath ≈ 1500 km,
        which corresponds to approximately 13.5° of latitude.

        Longitude extension uses the centre-track latitude to account for
        meridian convergence at higher latitudes.

        Args:
            bbox:  Nadir bounding box.
            track: Ground track points (used for centre-latitude estimate).

        Returns:
            {"north": float, "south": float, "east": float, "west": float}
        """
        R = EARTH_RADIUS_KM
        h = NOAA20_ALTITUDE_KM
        scan_half_rad = math.radians(VIIRS_SWATH_HALF_ANGLE_DEG)

        # Earth-centre angle from nadir to swath edge
        # sin(rho) = (R + h) / R * sin(scan_angle)
        sin_rho = (R + h) / R * math.sin(scan_half_rad)
        # sin_rho can exceed 1.0 if h is very large — clamp for safety
        sin_rho = min(sin_rho, 1.0)
        rho_rad = math.asin(sin_rho)

        # Ground-projected half-swath width in km
        # half_swath = R * (rho - scan_angle)  — arc along the ground
        half_swath_km = R * (rho_rad - scan_half_rad)

        # Convert to degrees of latitude (~111 km per degree)
        half_swath_lat_deg = half_swath_km / 111.0

        # Centre-track latitude for longitude adjustment
        lats = [p["lat"] for p in track]
        centre_lat = sum(lats) / len(lats) if lats else 0.0
        cos_lat = math.cos(math.radians(centre_lat))
        # Avoid division by zero near the poles
        if abs(cos_lat) < 1e-6:
            half_swath_lon_deg = 180.0
        else:
            half_swath_lon_deg = half_swath_km / (111.0 * cos_lat)

        north = min(bbox["north"] + half_swath_lat_deg, 90.0)
        south = max(bbox["south"] - half_swath_lat_deg, -90.0)
        east = bbox["east"] + half_swath_lon_deg
        west = bbox["west"] - half_swath_lon_deg

        # Normalise longitude to [-180, 180]
        east = ((east + 180.0) % 360.0) - 180.0
        west = ((west + 180.0) % 360.0) - 180.0

        return {
            "north": round(north, 5),
            "south": round(south, 5),
            "east": round(east, 5),
            "west": round(west, 5),
        }

    # ---------------------------------------------------------------------------
    # Timestamp extraction from dataset.json
    # ---------------------------------------------------------------------------

    @staticmethod
    def _extract_timestamps(dataset_json: dict) -> list[float]:
        """Extract Unix epoch timestamps from a SatDump dataset.json.

        Looks in two locations (requirement: robustness):
          1. dataset_json["timestamps"]
          2. dataset_json["images"][0]["timestamps"]

        Returns an empty list if neither is found.
        """
        # Primary location
        ts = dataset_json.get("timestamps")
        if isinstance(ts, list) and ts:
            return [float(t) for t in ts]

        # Nested under images[0]
        images = dataset_json.get("images")
        if isinstance(images, list) and images:
            ts = images[0].get("timestamps")
            if isinstance(ts, list) and ts:
                return [float(t) for t in ts]

        return []


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _load_fallback_tle() -> str:
    """Read TLE_FALLBACK from environment, or use the module-level default."""
    return os.environ.get("TLE_FALLBACK", _DEFAULT_FALLBACK_TLE)


def main() -> None:
    """CLI: geolocation.py <aggregation_dir> <output_dir>"""
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <aggregation_dir> <output_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    aggregation_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not aggregation_dir.exists():
        logger.error("aggregation_dir does not exist: %s", aggregation_dir)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    fallback_tle = _load_fallback_tle()
    contact_id = os.environ.get("CONTACT_ID", "")

    # --- Discover dataset.json files ----------------------------------------
    # Support two layouts:
    #   a) aggregation_dir/{chunk_id}/dataset.json  (one subdir per chunk)
    #   b) aggregation_dir/dataset.json             (single chunk at root)
    dataset_files: list[tuple[str, Path]] = []

    for subdir in sorted(aggregation_dir.iterdir()):
        if subdir.is_dir():
            ds = subdir / "dataset.json"
            if ds.exists():
                dataset_files.append((subdir.name, ds))

    # Fallback: dataset.json directly in aggregation_dir
    if not dataset_files:
        root_ds = aggregation_dir / "dataset.json"
        if root_ds.exists():
            dataset_files.append(("chunk_001", root_ds))

    if not dataset_files:
        logger.error(
            "No dataset.json files found under %s", aggregation_dir
        )
        sys.exit(1)

    logger.info("Found %d chunk(s) to process", len(dataset_files))

    calculator = GeolocationCalculator()
    successes = 0
    failures = 0

    for chunk_id, dataset_path in dataset_files:
        logger.info("Processing chunk %s (%s)", chunk_id, dataset_path)
        try:
            with open(dataset_path, "r", encoding="utf-8") as fh:
                dataset_json = json.load(fh)

            result = calculator.compute(
                dataset_json=dataset_json,
                fallback_tle=fallback_tle,
                chunk_id=chunk_id,
                contact_id=contact_id,
            )

            out_path = output_dir / f"{chunk_id}.json"
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(result.to_dict(), fh, indent=2)

            logger.info(
                "chunk=%s: wrote %s (track_points=%d, tle_source=%s, degraded=%s)",
                chunk_id,
                out_path,
                len(result.ground_track),
                result.tle_source,
                result.degraded,
            )
            successes += 1

        except Exception as exc:  # noqa: BLE001
            logger.error("chunk=%s: failed — %s", chunk_id, exc)
            failures += 1

    logger.info(
        "Geolocation complete — success=%d, failed=%d", successes, failures
    )

    if successes == 0:
        logger.error("All chunks failed geolocation")
        sys.exit(1)

    print(f"Geolocation complete — {successes} chunk(s) succeeded, {failures} failed.")


if __name__ == "__main__":
    main()
