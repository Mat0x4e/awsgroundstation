"""NASA SDR visualization pipeline — main entry point for CodeBuild.

Invoked by the NASA buildspec as:

    python3 /opt/scripts/visualize_nasa.py \\
        --input-dir      /tmp/input/chunks \\
        --output-dir     /tmp/output \\
        --contact-id     <contact_id> \\
        --contact-date   <YYYY/MM/DD> \\
        --enable-geotiff true|false \\
        --enable-destripe true|false

Orchestration:
  True Color  — SDRReader (I1/I2/I3 reflectance) → GEOReader (I-band) →
                optional destripe → ImageRenderer.assemble_true_color →
                CartopyRenderer.render_nasa → MetadataGenerator.generate_nasa →
                optional GeoTIFFExporter.export_nasa
  Thermal     — SDRReader (M15 radiance) → GEOReader (M-band) →
                BTConverter.convert → CartopyRenderer.render_nasa →
                MetadataGenerator.generate_nasa → optional GeoTIFFExporter.export_nasa

Exit codes:
  0 — success (at least one product rendered)
  1 — total failure (both True Color and Thermal failed)

Requirements: 12.1, 13.2, 13.3, 13.4
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path bootstrap — allows the script to run as:
#   python3 /opt/scripts/visualize_nasa.py
# without requiring PYTHONPATH to be set by the caller.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from viirs.sdr_reader import SDRReader
from viirs.geo_reader import GEOReader
from viirs.bt_converter import BTConverter
from viirs.image_renderer import ImageRenderer
from viirs.cartopy_renderer import CartopyRenderer
from viirs.metadata_generator import MetadataGenerator
from viirs.geotiff_exporter import GeoTIFFExporter
from viirs.models import BoundingBox, NASAMetadata

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
# File-discovery helpers
# ---------------------------------------------------------------------------

def _find_hdf5_files(input_dir: Path, *prefixes: str) -> list[Path]:
    """Return all HDF5 files under *input_dir* (recursively) matching any *prefix*.

    Searches both the top-level directory and any ``chunk_XXX`` subdirectories
    that the upstream pipeline may have created. Multiple prefixes let a single
    product be matched under either of its CSPP names (e.g. terrain-corrected
    ``GITCO`` or ellipsoid ``GIGTO``).

    Parameters
    ----------
    input_dir:
        Root directory to scan.
    *prefixes:
        One or more filename prefixes to match, e.g. ``"SVM15"``, ``"SVOM15"``.

    Returns
    -------
    list[Path]
        Sorted, de-duplicated list of matching ``.h5`` file paths.
    """
    matches: set[Path] = set()
    for prefix in prefixes:
        matches.update(input_dir.rglob(f"{prefix}_*.h5"))
    return sorted(matches)


# ---------------------------------------------------------------------------
# BoundingBox helper
# ---------------------------------------------------------------------------

def _bbox_from_geo(lat: np.ma.MaskedArray, lon: np.ma.MaskedArray) -> BoundingBox:
    """Compute tight bounding box from GEO arrays (masking invalid pixels)."""
    return BoundingBox(
        lat_min=float(lat.min()),
        lat_max=float(lat.max()),
        lon_min=float(lon.min()),
        lon_max=float(lon.max()),
    )


# ---------------------------------------------------------------------------
# Swath-width estimation
# ---------------------------------------------------------------------------

def _swath_width_km(lat: np.ma.MaskedArray, lon: np.ma.MaskedArray) -> float:
    """Estimate swath width in km from the GEO lat/lon arrays.

    Uses the longitude span at the centre latitude as a rough approximation.

    Parameters
    ----------
    lat:
        Per-pixel latitude masked array.
    lon:
        Per-pixel longitude masked array.

    Returns
    -------
    float
        Estimated swath width in kilometres.
    """
    try:
        centre_lat = float(lat.mean())
        lon_span = float(lon.max()) - float(lon.min())
        # 1 degree of longitude ≈ 111.32 × cos(lat) km
        km_per_deg = 111.32 * abs(np.cos(np.deg2rad(centre_lat)))
        return round(lon_span * km_per_deg, 1)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Multi-granule concatenation
# ---------------------------------------------------------------------------

def _concat_masked(arrays: list[np.ma.MaskedArray]) -> np.ma.MaskedArray:
    """Concatenate a list of masked arrays along axis 0 (along-track)."""
    return np.ma.concatenate(arrays, axis=0)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render NASA VIIRS SDR products with Cartopy overlays.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Path to the directory containing NASA HDF5 SDR files.",
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
        "--enable-geotiff",
        required=True,
        choices=["true", "false"],
        help='Enable GeoTIFF export ("true" or "false").',
    )
    parser.add_argument(
        "--enable-destripe",
        required=True,
        choices=["true", "false"],
        help='Enable destriping of I-band reflectance ("true" or "false").',
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# SDR reading helpers (multi-file concat)
# ---------------------------------------------------------------------------

def _read_reflectance_concat(
    sdr_reader: SDRReader, h5_files: list[Path]
) -> np.ma.MaskedArray:
    """Read and concatenate reflectance arrays from multiple HDF5 files."""
    return _concat_masked([sdr_reader.read_reflectance(f) for f in h5_files])


def _read_radiance_concat(
    sdr_reader: SDRReader, h5_files: list[Path]
) -> np.ma.MaskedArray:
    """Read and concatenate radiance arrays from multiple HDF5 files."""
    return _concat_masked([sdr_reader.read_radiance(f) for f in h5_files])


def _read_geo_iband_concat(
    geo_reader: GEOReader, h5_files: list[Path]
) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    """Read and concatenate I-band GEO arrays from multiple HDF5 files."""
    lats, lons = zip(*[geo_reader.read_iband(f) for f in h5_files])
    return _concat_masked(list(lats)), _concat_masked(list(lons))


def _read_geo_mband_concat(
    geo_reader: GEOReader, h5_files: list[Path]
) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    """Read and concatenate M-band GEO arrays from multiple HDF5 files."""
    lats, lons = zip(*[geo_reader.read_mband(f) for f in h5_files])
    return _concat_masked(list(lats)), _concat_masked(list(lons))


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the NASA SDR visualization pipeline.

    Returns:
        0 on success (at least one product rendered).
        1 on total failure (both rendering paths failed).
    """
    args = _parse_args(argv)

    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir
    contact_id: str = args.contact_id
    geotiff_enabled: bool = args.enable_geotiff.lower() == "true"
    destripe_enabled: bool = args.enable_destripe.lower() == "true"

    logger.info(
        "NASA visualization pipeline starting — contact_id=%s date=%s geotiff=%s destripe=%s",
        contact_id,
        args.contact_date,
        geotiff_enabled,
        destripe_enabled,
    )
    logger.info("Input dir  : %s", input_dir)
    logger.info("Output dir : %s", output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Step 1 — Scan input directory for HDF5 files by product prefix
    # -----------------------------------------------------------------------
    # CSPP produces SVM15 / GITCO / GMTCO (terrain-corrected); older/ellipsoid
    # names (SVOM15 / GIGTO / GMODO) are accepted too for forward-compatibility.
    i1_files = _find_hdf5_files(input_dir, "SVI01")
    i2_files = _find_hdf5_files(input_dir, "SVI02")
    i3_files = _find_hdf5_files(input_dir, "SVI03")
    m15_files = _find_hdf5_files(input_dir, "SVM15", "SVOM15")
    igeo_files = _find_hdf5_files(input_dir, "GITCO", "GIGTO")
    mgeo_files = _find_hdf5_files(input_dir, "GMTCO", "GMODO")

    logger.info(
        "HDF5 files found — I1:%d I2:%d I3:%d M15:%d IGEO:%d MGEO:%d",
        len(i1_files), len(i2_files), len(i3_files),
        len(m15_files), len(igeo_files), len(mgeo_files),
    )

    # -----------------------------------------------------------------------
    # Step 2 — Extract NASA metadata from the first available SDR file
    # -----------------------------------------------------------------------
    meta_gen = MetadataGenerator()
    nasa_meta: NASAMetadata = NASAMetadata()  # default fallback

    first_sdr = next(
        (f for f in (i1_files or i2_files or i3_files or m15_files) if f),
        None,
    )
    if first_sdr:
        nasa_meta = meta_gen.extract_nasa_metadata(first_sdr)
        logger.info(
            "NASA metadata — granule=%s orbit=%s datetime=%s",
            nasa_meta.granule_id,
            nasa_meta.orbit_number,
            nasa_meta.datetime_utc,
        )
    else:
        logger.warning("No SDR files found — metadata will use defaults")

    # -----------------------------------------------------------------------
    # Shared component instances
    # -----------------------------------------------------------------------
    sdr_reader = SDRReader()
    geo_reader = GEOReader()
    bt_converter = BTConverter()
    img_renderer = ImageRenderer()
    cartopy_renderer = CartopyRenderer()
    geotiff_exp = GeoTIFFExporter() if geotiff_enabled else None

    success_count = 0
    failure_count = 0

    # -----------------------------------------------------------------------
    # Step 3 — True Color rendering (I1 / I2 / I3 + I-band GEO)
    # -----------------------------------------------------------------------
    tc_png = output_dir / f"viirs_nasa_true_color_{contact_id}.png"
    tc_tif = output_dir / f"viirs_nasa_true_color_{contact_id}.tif"

    try:
        if not i1_files or not i2_files or not i3_files:
            raise FileNotFoundError(
                f"Missing I-band files — I1:{len(i1_files)} I2:{len(i2_files)} I3:{len(i3_files)}"
            )
        if not igeo_files:
            raise FileNotFoundError("No I-band GEO files (GIGTO) found")

        logger.info("True Color — reading I1/I2/I3 reflectance (%d granules each)", len(i1_files))
        i1 = _read_reflectance_concat(sdr_reader, i1_files)
        i2 = _read_reflectance_concat(sdr_reader, i2_files)
        i3 = _read_reflectance_concat(sdr_reader, i3_files)

        logger.info("True Color — reading I-band GEO (%d files)", len(igeo_files))
        lat_i, lon_i = _read_geo_iband_concat(geo_reader, igeo_files)

        if destripe_enabled:
            logger.info("True Color — applying destripe to I1/I2/I3")
            # contrast_stretch returns plain ndarray; destripe expects ndarray in [0,1].
            # Re-wrap as masked arrays so assemble_true_color receives the right type.
            i1 = np.ma.masked_array(
                img_renderer.destripe(img_renderer.contrast_stretch(i1)),
                mask=np.ma.getmaskarray(i1),
            )
            i2 = np.ma.masked_array(
                img_renderer.destripe(img_renderer.contrast_stretch(i2)),
                mask=np.ma.getmaskarray(i2),
            )
            i3 = np.ma.masked_array(
                img_renderer.destripe(img_renderer.contrast_stretch(i3)),
                mask=np.ma.getmaskarray(i3),
            )

        logger.info("True Color — assembling RGB")
        rgb_data = img_renderer.assemble_true_color(i1, i2, i3)

        bbox_tc = _bbox_from_geo(lat_i, lon_i)
        logger.info(
            "True Color BBox: lat=[%.3f, %.3f] lon=[%.3f, %.3f]",
            bbox_tc.lat_min, bbox_tc.lat_max, bbox_tc.lon_min, bbox_tc.lon_max,
        )

        logger.info("True Color — rendering PNG → %s", tc_png)
        cartopy_renderer.render_nasa(
            data=rgb_data,
            lat=lat_i.filled(np.nan),
            lon=lon_i.filled(np.nan),
            mode="True Color",
            metadata=nasa_meta,
            output_path=tc_png,
        )
        logger.info("True Color PNG written: %s", tc_png)

        swath_km = _swath_width_km(lat_i, lon_i)
        tc_meta_dict = meta_gen.generate_nasa(
            granule_id=nasa_meta.granule_id,
            orbit_number=nasa_meta.orbit_number,
            datetime_utc=nasa_meta.datetime_utc,
            mode="True Color",
            bbox=bbox_tc,
            swath_width_km=swath_km,
            output_png=tc_png,
        )
        if geotiff_enabled:
            tc_meta_dict["geotiff_file"] = tc_tif.name
        meta_gen.save(tc_meta_dict, tc_png)
        logger.info("True Color JSON written: %s", tc_png.with_suffix(".json"))

        # PNG + JSON are the primary product — count success now so a later
        # (optional) GeoTIFF failure cannot discard a good render.
        success_count += 1

        if geotiff_enabled and geotiff_exp is not None:
            try:
                logger.info("True Color — exporting GeoTIFF → %s", tc_tif)
                geotiff_exp.export_nasa(
                    data=rgb_data,
                    lat=lat_i.filled(np.nan),
                    lon=lon_i.filled(np.nan),
                    output_path=tc_tif,
                )
                logger.info("True Color TIF written: %s", tc_tif)
            except Exception as exc:  # noqa: BLE001 — GeoTIFF is best-effort
                logger.error("True Color GeoTIFF export failed: %s", exc, exc_info=True)

    except Exception as exc:  # noqa: BLE001
        logger.error("True Color rendering failed: %s", exc, exc_info=True)
        failure_count += 1

    # -----------------------------------------------------------------------
    # Step 4 — Thermal rendering (M15 + M-band GEO)
    # -----------------------------------------------------------------------
    th_png = output_dir / f"viirs_nasa_thermal_{contact_id}.png"
    th_tif = output_dir / f"viirs_nasa_thermal_{contact_id}.tif"

    try:
        if not m15_files:
            raise FileNotFoundError("No M15 files (SVOM15) found")
        if not mgeo_files:
            raise FileNotFoundError("No M-band GEO files (GMODO) found")

        logger.info("Thermal — reading M15 radiance (%d granules)", len(m15_files))
        radiance = _read_radiance_concat(sdr_reader, m15_files)

        logger.info("Thermal — reading M-band GEO (%d files)", len(mgeo_files))
        lat_m, lon_m = _read_geo_mband_concat(geo_reader, mgeo_files)

        logger.info("Thermal — converting radiance → brightness temperature (M15)")
        bt_data = bt_converter.convert(radiance)

        bbox_th = _bbox_from_geo(lat_m, lon_m)
        logger.info(
            "Thermal BBox: lat=[%.3f, %.3f] lon=[%.3f, %.3f]",
            bbox_th.lat_min, bbox_th.lat_max, bbox_th.lon_min, bbox_th.lon_max,
        )

        logger.info("Thermal — rendering PNG → %s", th_png)
        cartopy_renderer.render_nasa(
            data=bt_data.filled(np.nan),
            lat=lat_m.filled(np.nan),
            lon=lon_m.filled(np.nan),
            mode="Thermal",
            metadata=nasa_meta,
            output_path=th_png,
        )
        logger.info("Thermal PNG written: %s", th_png)

        swath_km = _swath_width_km(lat_m, lon_m)
        th_meta_dict = meta_gen.generate_nasa(
            granule_id=nasa_meta.granule_id,
            orbit_number=nasa_meta.orbit_number,
            datetime_utc=nasa_meta.datetime_utc,
            mode="Thermal",
            bbox=bbox_th,
            swath_width_km=swath_km,
            output_png=th_png,
        )
        if geotiff_enabled:
            th_meta_dict["geotiff_file"] = th_tif.name
        meta_gen.save(th_meta_dict, th_png)
        logger.info("Thermal JSON written: %s", th_png.with_suffix(".json"))

        # PNG + JSON are the primary product — count success now so a later
        # (optional) GeoTIFF failure cannot discard a good render.
        success_count += 1

        if geotiff_enabled and geotiff_exp is not None:
            try:
                logger.info("Thermal — exporting GeoTIFF → %s", th_tif)
                geotiff_exp.export_nasa(
                    data=bt_data.filled(np.nan),
                    lat=lat_m.filled(np.nan),
                    lon=lon_m.filled(np.nan),
                    output_path=th_tif,
                )
                logger.info("Thermal TIF written: %s", th_tif)
            except Exception as exc:  # noqa: BLE001 — GeoTIFF is best-effort
                logger.error("Thermal GeoTIFF export failed: %s", exc, exc_info=True)

    except Exception as exc:  # noqa: BLE001
        logger.error("Thermal rendering failed: %s", exc, exc_info=True)
        failure_count += 1

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    logger.info(
        "Pipeline complete — %d succeeded, %d failed",
        success_count,
        failure_count,
    )

    if success_count == 0:
        logger.error("All rendering paths failed — no products written.")
        return 1

    if failure_count > 0:
        logger.warning(
            "%d path(s) failed but %d succeeded — exit 0 (partial success).",
            failure_count,
            success_count,
        )

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
