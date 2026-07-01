"""SatDump visualization pipeline — main entry point for CodeBuild.

Invoked by the SatDump buildspec as:

    python3 /opt/scripts/visualize_satdump.py \\
        --input-dir      /tmp/input/satdump \\
        --coordinates-dir /tmp/input/coordinates \\
        --output-dir     /tmp/output \\
        --contact-id     <contact_id> \\
        --contact-date   <YYYY/MM/DD> \\
        --enable-geotiff true|false

Exit codes:
  0 — success (all composites rendered, or partial with logged warnings)
  1 — total failure (no composites found, or bounding box cannot be computed)

Requirements: 1.1, 2.1, 3.1, 4.1, 5.1, 12.1, 13.1, 13.2, 13.3, 13.4, 13.5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path bootstrap — allows this script to be run directly as
#   python3 /opt/scripts/visualize_satdump.py
# without requiring the caller to set PYTHONPATH.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viirs.cbor_reader import CBORReader
from viirs.bbox_calculator import BBoxCalculator, NoBBoxSourceError
from viirs.satdump_visualizer import SatDumpVisualizer, NoCompositesError
from viirs.cartopy_renderer import CartopyRenderer
from viirs.metadata_generator import MetadataGenerator
from viirs.geotiff_exporter import GeoTIFFExporter

# ---------------------------------------------------------------------------
# Logging — INFO to stdout so CodeBuild captures it in the build log
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

def _composite_slug(composite_type: str) -> str:
    """Convert composite type to a safe filename slug.

    Examples:
        "True Color"        → "true_color"
        "Thermal IR"        → "thermal_ir"
        "Day Microphysics"  → "day_microphysics"
    """
    return composite_type.lower().replace(" ", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render SatDump VIIRS composites with Cartopy overlays.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Path to the SatDump output folder (PNGs + product.cbor + .georef).",
    )
    parser.add_argument(
        "--coordinates-dir",
        required=False,
        type=Path,
        default=None,
        help="Optional path to the coordinates folder (additional .georef data).",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Path to write output products (PNG, JSON, optional TIF).",
    )
    parser.add_argument(
        "--contact-id",
        required=True,
        help="Contact/pass identifier used in output filenames.",
    )
    parser.add_argument(
        "--contact-date",
        required=True,
        help="Contact date in YYYY/MM/DD format.",
    )
    parser.add_argument(
        "--contact-time",
        required=False,
        default=None,
        help="Contact start time in HH:MM:SS UTC format (improves geolocation accuracy).",
    )
    parser.add_argument(
        "--enable-geotiff",
        required=True,
        choices=["true", "false"],
        help='Enable GeoTIFF export ("true" or "false").',
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Bounding-box resolution with coordinates-dir fallback (Req 3.1)
# ---------------------------------------------------------------------------

def _resolve_bbox(
    cbor_meta,
    input_dir: Path,
    coordinates_dir: Path | None,
    bbox_calc: BBoxCalculator,
):
    """Resolve bounding box, trying coordinates-dir first if provided.

    Priority (Req 3.1):
      1. .georef file in coordinates_dir (if provided)
      2. .georef file in input_dir  (standard BBoxCalculator priority 1)
      3. CBOR projection_coords     (standard BBoxCalculator priority 2)
      4. TLE propagation            (standard BBoxCalculator priority 3)

    Raises NoBBoxSourceError if none of the sources yield a result.
    """
    # Try coordinates_dir first (Req 3.1 — coordinates-dir for .georef)
    if coordinates_dir is not None and coordinates_dir.exists():
        georef_files = list(coordinates_dir.glob("*.georef"))
        if georef_files:
            try:
                bbox = bbox_calc._from_georef(georef_files[0])
                logger.info("BBox resolved from coordinates-dir .georef: %s", georef_files[0].name)
                return bbox
            except Exception as exc:
                logger.warning(
                    "Failed to read .georef from coordinates-dir %s: %s — falling back",
                    georef_files[0],
                    exc,
                )

    # Fall through to standard priority chain (input_dir .georef → CBOR → TLE)
    return bbox_calc.compute(cbor_meta, input_dir)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the SatDump visualization pipeline.

    Returns:
        0 on success (all or some composites rendered).
        1 on total failure (no composites, or bbox cannot be computed).
    """
    args = _parse_args(argv)

    input_dir: Path = args.input_dir
    coordinates_dir: Path | None = args.coordinates_dir
    output_dir: Path = args.output_dir
    contact_id: str = args.contact_id
    geotiff_enabled: bool = args.enable_geotiff.lower() == "true"

    logger.info(
        "SatDump visualization pipeline starting — contact_id=%s date=%s geotiff=%s",
        contact_id,
        args.contact_date,
        geotiff_enabled,
    )
    logger.info("Input dir      : %s", input_dir)
    logger.info("Coordinates dir: %s", coordinates_dir or "(none)")
    logger.info("Output dir     : %s", output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Step 1 — Read CBOR metadata (Req 2.1)
    # -----------------------------------------------------------------------
    cbor_reader = CBORReader()
    cbor_meta = cbor_reader.read(input_dir)
    logger.info(
        "CBOR metadata — satellite=%s timestamp=%s",
        cbor_meta.satellite,
        cbor_meta.timestamp,
    )

    # -----------------------------------------------------------------------
    # Step 2 — Discover composites (Req 1.1, 4.1)
    # -----------------------------------------------------------------------
    visualizer = SatDumpVisualizer()
    try:
        composites = visualizer.discover_composites(input_dir)
    except NoCompositesError as exc:
        logger.error("No composites found — aborting: %s", exc)
        return 1

    logger.info("Discovered %d composite(s): %s", len(composites), [c.composite_type for c in composites])

    # -----------------------------------------------------------------------
    # Step 3 — Compute bounding box (Req 3.1)
    # -----------------------------------------------------------------------
    # If CBOR timestamp is missing, synthesise one from --contact-date so that
    # the TLE fallback path has a usable acquisition time.
    if cbor_meta.timestamp is None:
        try:
            from datetime import datetime, timezone
            date_str = args.contact_date  # "YYYY/MM/DD"
            if args.contact_time:
                dt_str = f"{date_str} {args.contact_time}"
                contact_dt = datetime.strptime(dt_str, "%Y/%m/%d %H:%M:%S")
            else:
                contact_dt = datetime.strptime(date_str, "%Y/%m/%d")
            cbor_meta.timestamp = contact_dt.replace(tzinfo=timezone.utc)
            logger.info(
                "CBOR timestamp was None — synthesised from --contact-date: %s",
                cbor_meta.timestamp,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not parse contact datetime: %s", exc)

    bbox_calc = BBoxCalculator()
    try:
        bbox = _resolve_bbox(cbor_meta, input_dir, coordinates_dir, bbox_calc)
    except NoBBoxSourceError as exc:
        logger.error("Cannot compute bounding box — aborting: %s", exc)
        return 1

    logger.info(
        "BBox: lat=[%.3f, %.3f] lon=[%.3f, %.3f]",
        bbox.lat_min,
        bbox.lat_max,
        bbox.lon_min,
        bbox.lon_max,
    )

    # -----------------------------------------------------------------------
    # Shared component instances
    # -----------------------------------------------------------------------
    renderer = CartopyRenderer()
    meta_gen = MetadataGenerator()
    geotiff_exp = GeoTIFFExporter() if geotiff_enabled else None

    # Derive datetime_utc string from CBOR timestamp for metadata
    if cbor_meta.timestamp is not None:
        try:
            datetime_utc = cbor_meta.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        except AttributeError:
            datetime_utc = "unknown"
    else:
        datetime_utc = "unknown"

    # -----------------------------------------------------------------------
    # Step 4 — Per-composite processing
    # -----------------------------------------------------------------------
    success_count = 0
    failure_count = 0

    for i, composite in enumerate(composites):
        slug = _composite_slug(composite.composite_type)
        # Req 13.2–13.4: output filenames
        png_name  = f"viirs_satdump_{slug}_{contact_id}.png"
        json_name = f"viirs_satdump_{slug}_{contact_id}.json"
        tif_name  = f"viirs_satdump_{slug}_{contact_id}.tif"

        png_path  = output_dir / png_name
        tif_path  = output_dir / tif_name

        logger.info("Processing composite: %s → %s", composite.composite_type, png_name)

        try:
            # 4a. Load and normalize (Req 1.1)
            data = visualizer.load_and_normalize(composite)
            logger.debug(
                "  Loaded %s — shape=%s dtype=%s",
                composite.path.name,
                data.shape,
                data.dtype,
            )

            # 4b. Render with Cartopy overlays (Req 4.1)
            renderer.render_satdump(
                data=data,
                bbox=bbox,
                composite_type=composite.composite_type,
                metadata=cbor_meta,
                output_path=png_path,
            )
            logger.info("  PNG written: %s", png_path)

            # 4c. Generate and save metadata JSON (Req 5.1)
            metadata_dict = meta_gen.generate_satdump(
                composite_type=composite.composite_type,
                satellite=cbor_meta.satellite,
                datetime_utc=datetime_utc,
                bbox=bbox,
                output_png=png_path,
            )

            # Augment with GeoTIFF filename if export is enabled (Req 13.4)
            if geotiff_enabled:
                metadata_dict["geotiff_file"] = tif_name

            meta_gen.save(metadata_dict, png_path)
            logger.info("  JSON written: %s", output_dir / json_name)

            # 4d. GeoTIFF export (Req 12.1)
            if geotiff_enabled and geotiff_exp is not None:
                geotiff_exp.export_satdump(
                    data=data,
                    bbox=bbox,
                    output_path=tif_path,
                )
                logger.info("  TIF written: %s", tif_path)

            success_count += 1

        except Exception as exc:  # noqa: BLE001 — graceful per-composite error handling
            logger.error(
                "Failed to process composite %s: %s",
                composite.composite_type,
                exc,
                exc_info=True,
            )
            failure_count += 1
            # Continue with next composite (Req 13.5)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    logger.info(
        "Pipeline complete — %d succeeded, %d failed",
        success_count,
        failure_count,
    )

    if success_count == 0:
        logger.error("All composites failed — no products written.")
        return 1

    if failure_count > 0:
        logger.warning(
            "%d composite(s) failed but %d succeeded — exit 0 (partial success).",
            failure_count,
            success_count,
        )

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
