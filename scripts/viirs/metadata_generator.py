"""Metadata generator for the VIIRS visualization pipeline.

Produces JSON sidecar files for SatDump and NASA SDR rendered products.
Shared component used by both visualization paths.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import h5py

from .models import BoundingBox, NASAMetadata

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "1.0.0"


def _utcnow_iso() -> str:
    """Returns the current UTC timestamp as ISO 8601 with a 'Z' suffix."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


class MetadataGenerator:
    """Generates JSON metadata sidecars for each rendered VIIRS image.

    Covers both the SatDump path (Requirements 5.1–5.4) and the NASA path
    (Requirements 10.1–10.4).
    """

    # ------------------------------------------------------------------ #
    # SatDump path                                                         #
    # ------------------------------------------------------------------ #

    def generate_satdump(
        self,
        composite_type: str,
        satellite: str,
        datetime_utc: str,
        bbox: BoundingBox,
        output_png: Path,
    ) -> dict:
        """Build metadata dict for a SatDump rendered composite.

        Args:
            composite_type: Human-readable composite name, e.g. "True Color".
            satellite: Satellite identifier, e.g. "NOAA-20".
            datetime_utc: Pass start time as ISO 8601 UTC string.
            bbox: Geographic bounding box of the swath.
            output_png: Path to the rendered PNG output file.

        Returns:
            Metadata dict conforming to the SatDump JSON schema (Req 5.1–5.4).
        """
        return {
            "source": "SatDump",
            "visualization_path": "satdump",          # Req 5.4
            "satellite": satellite,
            "datetime_utc": datetime_utc,
            "composite_type": composite_type,
            "bounding_box": bbox.to_dict(),            # Req 5.1
            "calibration_note": "Communautaire SatDump — non certifiée NOAA",  # Req 5.3
            "output_file": output_png.name,            # Req 5.2 — same base name as PNG
            "pipeline_version": PIPELINE_VERSION,
            "processing_timestamp": _utcnow_iso(),
        }

    # ------------------------------------------------------------------ #
    # NASA path                                                            #
    # ------------------------------------------------------------------ #

    def generate_nasa(
        self,
        granule_id: str,
        orbit_number: int,
        datetime_utc: str,
        mode: str,
        bbox: BoundingBox,
        swath_width_km: float,
        output_png: Path,
    ) -> dict:
        """Build metadata dict for a NASA SDR rendered product.

        Args:
            granule_id: HDF5 N_Granule_ID value.
            orbit_number: HDF5 N_Beginning_Orbit_Number value.
            datetime_utc: Granule start time as ISO 8601 UTC string.
            mode: Render mode, e.g. "True Color" or "Thermal IR".
            bbox: Geographic bounding box of the swath.
            swath_width_km: Estimated swath width in kilometres.
            output_png: Path to the rendered PNG output file.

        Returns:
            Metadata dict conforming to the NASA JSON schema (Req 10.1).
        """
        return {
            "source": "NASA CSPP SDR",
            "visualization_path": "nasa",              # Req 10.1
            "granule_id": granule_id,
            "orbit_number": orbit_number,
            "datetime_utc": datetime_utc,
            "mode": mode,
            "bounding_box": bbox.to_dict(),
            "swath_width_km": swath_width_km,
            "output_file": output_png.name,
            "pipeline_version": PIPELINE_VERSION,
            "processing_timestamp": _utcnow_iso(),
        }

    def extract_nasa_metadata(self, sdr_h5_path: Path) -> NASAMetadata:
        """Extract granule metadata from global attributes of an HDF5 SDR file.

        Reads N_Granule_ID, N_Beginning_Orbit_Number, and N_Beginning_Time_IET
        from the HDF5 root attributes.  The IET timestamp (microseconds since
        1958-01-01T00:00:00Z) is converted to ISO 8601 UTC via
        NASAMetadata.from_iet().

        If any attribute is missing or the file cannot be opened, returns a
        NASAMetadata instance with default values — never raises (Req 10.4).

        Args:
            sdr_h5_path: Path to the HDF5 SDR file.

        Returns:
            NASAMetadata with granule_id, orbit_number, datetime_utc populated.
        """
        try:
            with h5py.File(sdr_h5_path, "r") as h5:
                attrs = dict(h5.attrs)

            granule_id = _decode_attr(attrs.get("N_Granule_ID"))
            orbit_raw = attrs.get("N_Beginning_Orbit_Number")
            iet_raw = attrs.get("N_Beginning_Time_IET")

            if granule_id is None:
                logger.warning(
                    "%s: N_Granule_ID attribute missing — using default",
                    sdr_h5_path.name,
                )
                granule_id = "unknown"

            orbit_number: int = 0
            if orbit_raw is not None:
                try:
                    orbit_number = int(orbit_raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "%s: N_Beginning_Orbit_Number cannot be cast to int: %r",
                        sdr_h5_path.name,
                        orbit_raw,
                    )

            if iet_raw is not None:
                try:
                    # Req 10.3: convert IET µs since 1958-01-01 → ISO 8601 UTC
                    return NASAMetadata.from_iet(
                        iet_microseconds=int(iet_raw),
                        granule_id=granule_id,
                        orbit_number=orbit_number,
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    logger.warning(
                        "%s: N_Beginning_Time_IET conversion failed (%s) — using default datetime",
                        sdr_h5_path.name,
                        exc,
                    )

            # Req 10.4: fall back to defaults if attributes absent / unconvertible
            return NASAMetadata(
                granule_id=granule_id,
                orbit_number=orbit_number,
                datetime_utc="unknown",
            )

        except OSError as exc:
            logger.warning(
                "Cannot open HDF5 file %s (%s) — returning default NASAMetadata",
                sdr_h5_path,
                exc,
            )
            return NASAMetadata()

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def save(self, metadata_dict: dict, output_path: Path) -> Path:
        """Write *metadata_dict* to a JSON file alongside the PNG output.

        The JSON file is placed next to the PNG with the same stem and the
        `.json` extension (Req 5.2 / 10.1).

        Args:
            metadata_dict: Metadata dict produced by generate_satdump() or
                generate_nasa().
            output_path: Path to the rendered PNG (or any file whose stem
                will be reused for the sidecar).

        Returns:
            Path to the written JSON file.
        """
        json_path = output_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(metadata_dict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Metadata written to %s", json_path)
        return json_path


# ------------------------------------------------------------------ #
# Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _decode_attr(value: object) -> str | None:
    """Decode an HDF5 attribute value to a plain Python string.

    HDF5 attributes can be bytes, numpy bytes, or already str.
    Returns None if *value* is None.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace").strip("\x00")
    # numpy scalar (e.g. np.bytes_)
    if hasattr(value, "tobytes"):
        return value.tobytes().decode("utf-8", errors="replace").strip("\x00")
    return str(value).strip("\x00")
