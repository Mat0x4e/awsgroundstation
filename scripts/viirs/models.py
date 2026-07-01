"""Shared data models for the VIIRS visualization pipeline.

Defines dataclasses used across the SatDump composite discovery, metadata
extraction, and GeoTIFF projection stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class BoundingBox:
    """Geographic bounding box in WGS84 degrees."""

    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def span_lat(self) -> float:
        """Latitude extent in degrees."""
        return self.lat_max - self.lat_min

    def span_lon(self) -> float:
        """Longitude extent in degrees."""
        return self.lon_max - self.lon_min

    def center(self) -> tuple[float, float]:
        """Returns (lat, lon) of the bounding box centre."""
        return (
            (self.lat_min + self.lat_max) / 2,
            (self.lon_min + self.lon_max) / 2,
        )

    def with_margin(self, margin_deg: float) -> BoundingBox:
        """Returns a new BoundingBox expanded by *margin_deg* on all sides.

        Results are clamped to valid WGS84 ranges:
        latitude  → [-90, 90]
        longitude → [-180, 180]
        """
        return BoundingBox(
            lat_min=max(-90.0, self.lat_min - margin_deg),
            lat_max=min(90.0, self.lat_max + margin_deg),
            lon_min=max(-180.0, self.lon_min - margin_deg),
            lon_max=min(180.0, self.lon_max + margin_deg),
        )

    def to_dict(self) -> dict:
        """Serialises the bounding box to a plain dict."""
        return {
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
        }


@dataclass
class CBORMetadata:
    """Metadata extracted from SatDump's product.cbor file."""

    timestamp: datetime | None = None
    satellite: str = "NOAA-20"
    projection_coords: dict | None = None
    raw_data: dict = field(default_factory=dict)
    # Ephemeris-based georeferencing (from projection_cfg)
    ephemeris: list[dict] | None = None  # list of {x, y, z, timestamp, vx, vy, vz}
    scan_angle: float = 112.0  # total scan angle in degrees (±56° for VIIRS)
    image_width: int = 6400  # native pixel width from CBOR


@dataclass
class NASAMetadata:
    """Metadata from NASA HDF5 granule attributes."""

    granule_id: str = "unknown"
    orbit_number: int = 0
    datetime_utc: str = "unknown"

    # IET epoch: 1958-01-01T00:00:00Z (International Atomic Time origin used by NASA)
    IET_EPOCH: datetime = field(
        default=datetime(1958, 1, 1, tzinfo=timezone.utc),
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_iet(
        cls, iet_microseconds: int, granule_id: str, orbit_number: int
    ) -> NASAMetadata:
        """Constructs a NASAMetadata instance from an IET microsecond timestamp.

        IET (International Atomic Time) counts microseconds since
        1958-01-01T00:00:00Z.  The resulting datetime is formatted as
        ISO 8601 with a 'Z' suffix, e.g. '2026-06-19T14:23:00Z'.
        """
        dt = datetime(1958, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=iet_microseconds
        )
        return cls(
            granule_id=granule_id,
            orbit_number=orbit_number,
            datetime_utc=dt.replace(tzinfo=None).isoformat() + "Z",
        )


@dataclass
class CompositeInfo:
    """A discovered SatDump composite PNG file."""

    path: Path
    composite_type: str
    bit_depth: int
