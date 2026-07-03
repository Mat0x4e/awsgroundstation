"""Manifest generator for the NOAA-20 SDR pipeline — Final Aggregation phase.

Scans a local aggregation directory (populated by download_chunk_metadata.py)
and emits a manifest.json that summarises all chunk processing results,
SDR/GEO file inventory, bounding boxes, and timing metrics.

Requirements satisfied:
  - Aggregate chunk results from <aggregation_dir>/chunks/
  - Detect successful chunks (dataset.json present, no _failed.json)
  - Detect failed chunks (_failed.json present)
  - List SDR and GEO files with band/resolution metadata
  - Read per-chunk bounding boxes from <aggregation_dir>/coordinates/
  - Assert successful + failed == total
  - Compute duration and metric averages from dataset.json timing fields
  - Write manifest.json with indent=2

Usage:
    python generate_manifest.py <aggregation_dir> <output_manifest_path>

Environment variables (override CLI defaults):
    CONTACT_ID         — contact identifier (required if not in dataset.json)
    CONTACT_DATE       — ISO date string, e.g. "2026-06-19"
    PIPELINE_VERSION   — default "1.0.0"

Exit codes:
    0  success
    1  error
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Band / resolution classification tables
# ---------------------------------------------------------------------------

# SDR prefixes → (band_label, resolution_m)
_SDR_BAND_MAP: dict[str, tuple[str, int]] = {
    "SVI01": ("I1", 375),
    "SVI02": ("I2", 375),
    "SVI03": ("I3", 375),
    "SVI04": ("I4", 375),
    "SVI05": ("I5", 375),
    "SVM01": ("M1", 750),
    "SVM02": ("M2", 750),
    "SVM03": ("M3", 750),
    "SVM04": ("M4", 750),
    "SVM05": ("M5", 750),
    "SVM06": ("M6", 750),
    "SVM07": ("M7", 750),
    "SVM08": ("M8", 750),
    "SVM09": ("M9", 750),
    "SVM10": ("M10", 750),
    "SVM11": ("M11", 750),
    "SVM12": ("M12", 750),
    "SVM13": ("M13", 750),
    "SVM14": ("M14", 750),
    "SVM15": ("M15", 750),
    "SVM16": ("M16", 750),
    "SVDNB": ("DNB", 750),
}

# GEO prefixes → resolution_m
_GEO_RESOLUTION_MAP: dict[str, int] = {
    "GIGTO": 375,
    "GMODO": 750,
    "GDNBO": 750,
}

# Glob patterns used when scanning chunk directories
_SDR_GLOBS = ["SVI0?_*.h5", "SVM??_*.h5", "SVDNB_*.h5"]
_GEO_GLOBS = ["GIGTO_*.h5", "GMODO_*.h5", "GDNBO_*.h5"]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FailedChunk:
    chunk_id: str
    reason: str
    attempts: int


@dataclass
class SdrFileEntry:
    key: str
    band: str
    resolution_m: int
    type: str = "SDR"


@dataclass
class GeoFileEntry:
    key: str
    type: str = "GEO"
    resolution_m: int = 750


@dataclass
class BoundingBoxEntry:
    chunk_id: str
    nadir: dict
    swath: dict


@dataclass
class ManifestResult:
    contact_id: str
    contact_date: str
    satellite: str
    norad_id: int
    processing_timestamp: str
    pipeline_version: str
    total_chunks: int
    successful_chunks: int
    failed_chunks: list[FailedChunk]
    sdr_files: list[SdrFileEntry]
    geo_files: list[GeoFileEntry]
    bounding_boxes: list[BoundingBoxEntry]
    total_duration_s: float
    metrics: dict[str, float]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class ManifestGenerator:
    """Aggregates chunk results and produces a manifest.json."""

    SATELLITE = "NOAA-20"
    NORAD_ID = 43013

    def generate(
        self,
        aggregation_dir: str,
        contact_id: str,
        contact_date: str,
        pipeline_version: str,
    ) -> ManifestResult:
        """Scan aggregation_dir and build a ManifestResult.

        Args:
            aggregation_dir:  Root of the local aggregation tree.
            contact_id:       Contact identifier string.
            contact_date:     ISO date string (YYYY-MM-DD).
            pipeline_version: Pipeline version string.

        Returns:
            Populated ManifestResult.

        Raises:
            AssertionError: If successful + failed != total chunks.
            FileNotFoundError: If aggregation_dir/chunks/ does not exist.
        """
        agg = Path(aggregation_dir)
        chunks_dir = agg / "chunks"
        coords_dir = agg / "coordinates"

        if not chunks_dir.is_dir():
            raise FileNotFoundError(
                f"chunks directory not found: {chunks_dir}"
            )

        # ----------------------------------------------------------------
        # Scan chunk subdirectories
        # ----------------------------------------------------------------
        chunk_dirs = sorted(
            p for p in chunks_dir.iterdir() if p.is_dir()
        )
        total_chunks = len(chunk_dirs)
        logger.info("Found %d chunk directory(ies) in %s", total_chunks, chunks_dir)

        failed_chunks: list[FailedChunk] = []
        successful_chunk_ids: list[str] = []

        for chunk_path in chunk_dirs:
            chunk_id = chunk_path.name
            failed_marker = chunk_path / "_failed.json"
            dataset_json = chunk_path / "dataset.json"

            if failed_marker.exists():
                reason, attempts = self._read_failed_marker(failed_marker)
                failed_chunks.append(FailedChunk(
                    chunk_id=chunk_id,
                    reason=reason,
                    attempts=attempts,
                ))
                logger.info("Chunk %s: FAILED (reason=%r, attempts=%d)", chunk_id, reason, attempts)
            elif dataset_json.exists():
                successful_chunk_ids.append(chunk_id)
                logger.info("Chunk %s: SUCCESS", chunk_id)
            else:
                # No dataset.json and no _failed.json — treat as failed (unknown reason)
                logger.warning(
                    "Chunk %s: neither dataset.json nor _failed.json found — treating as failed",
                    chunk_id,
                )
                failed_chunks.append(FailedChunk(
                    chunk_id=chunk_id,
                    reason="unknown — no dataset.json or _failed.json",
                    attempts=0,
                ))

        successful_count = len(successful_chunk_ids)
        logger.info(
            "Chunk summary: total=%d, successful=%d, failed=%d",
            total_chunks,
            successful_count,
            len(failed_chunks),
        )

        # Invariant check
        assert successful_count + len(failed_chunks) == total_chunks, (
            f"Chunk count mismatch: {successful_count} successful + "
            f"{len(failed_chunks)} failed != {total_chunks} total"
        )

        # ----------------------------------------------------------------
        # Build SDR / GEO file lists for successful chunks
        # ----------------------------------------------------------------
        sdr_files: list[SdrFileEntry] = []
        geo_files: list[GeoFileEntry] = []

        for chunk_id in successful_chunk_ids:
            chunk_path = chunks_dir / chunk_id
            sdr_files.extend(
                self._classify_sdr_files(chunk_path, chunk_id, contact_id, contact_date)
            )
            geo_files.extend(
                self._classify_geo_files(chunk_path, chunk_id, contact_id, contact_date)
            )

        logger.info(
            "File inventory: %d SDR file(s), %d GEO file(s)",
            len(sdr_files),
            len(geo_files),
        )

        # ----------------------------------------------------------------
        # Read bounding boxes from coordinates directory
        # ----------------------------------------------------------------
        bounding_boxes: list[BoundingBoxEntry] = []
        for chunk_id in successful_chunk_ids:
            coord_file = coords_dir / f"{chunk_id}.json"
            if coord_file.exists():
                bb = self._read_bounding_box(coord_file, chunk_id)
                if bb is not None:
                    bounding_boxes.append(bb)
            else:
                logger.warning(
                    "No coordinates file for chunk %s (expected %s)", chunk_id, coord_file
                )

        # ----------------------------------------------------------------
        # Compute timing metrics from dataset.json files
        # ----------------------------------------------------------------
        total_duration_s, metrics = self._aggregate_metrics(
            chunks_dir, successful_chunk_ids
        )

        processing_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return ManifestResult(
            contact_id=contact_id,
            contact_date=contact_date,
            satellite=self.SATELLITE,
            norad_id=self.NORAD_ID,
            processing_timestamp=processing_timestamp,
            pipeline_version=pipeline_version,
            total_chunks=total_chunks,
            successful_chunks=successful_count,
            failed_chunks=failed_chunks,
            sdr_files=sdr_files,
            geo_files=geo_files,
            bounding_boxes=bounding_boxes,
            total_duration_s=total_duration_s,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_failed_marker(self, path: Path) -> tuple[str, int]:
        """Parse _failed.json and return (reason, attempts)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            reason = str(data.get("reason", "unknown"))
            attempts = int(data.get("attempts", 0))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not parse %s: %s", path, exc)
            reason = "unparseable _failed.json"
            attempts = 0
        return reason, attempts

    def _s3_key(
        self, contact_date: str, contact_id: str, chunk_id: str, filename: str
    ) -> str:
        """Build the S3 key for a given file."""
        return f"contacts/{contact_date}/{contact_id}/chunks/{chunk_id}/{filename}"

    def _sdr_band_info(self, filename: str) -> tuple[str, int] | None:
        """Return (band, resolution_m) for an SDR filename, or None if unrecognised."""
        for prefix, info in _SDR_BAND_MAP.items():
            if filename.startswith(prefix):
                return info
        return None

    def _geo_resolution(self, filename: str) -> int | None:
        """Return resolution_m for a GEO filename, or None if unrecognised."""
        for prefix, res in _GEO_RESOLUTION_MAP.items():
            if filename.startswith(prefix):
                return res
        return None

    def _classify_sdr_files(
        self,
        chunk_path: Path,
        chunk_id: str,
        contact_id: str,
        contact_date: str,
    ) -> list[SdrFileEntry]:
        """Collect and classify all SDR files in a chunk directory."""
        entries: list[SdrFileEntry] = []
        for glob in _SDR_GLOBS:
            for fpath in sorted(chunk_path.glob(glob)):
                filename = fpath.name
                band_info = self._sdr_band_info(filename)
                if band_info is None:
                    logger.warning("Unrecognised SDR filename pattern: %s", filename)
                    continue
                band, resolution_m = band_info
                entries.append(SdrFileEntry(
                    key=self._s3_key(contact_date, contact_id, chunk_id, filename),
                    band=band,
                    resolution_m=resolution_m,
                ))
        return entries

    def _classify_geo_files(
        self,
        chunk_path: Path,
        chunk_id: str,
        contact_id: str,
        contact_date: str,
    ) -> list[GeoFileEntry]:
        """Collect and classify all GEO files in a chunk directory."""
        entries: list[GeoFileEntry] = []
        for glob in _GEO_GLOBS:
            for fpath in sorted(chunk_path.glob(glob)):
                filename = fpath.name
                resolution_m = self._geo_resolution(filename)
                if resolution_m is None:
                    logger.warning("Unrecognised GEO filename pattern: %s", filename)
                    continue
                entries.append(GeoFileEntry(
                    key=self._s3_key(contact_date, contact_id, chunk_id, filename),
                    resolution_m=resolution_m,
                ))
        return entries

    def _read_bounding_box(self, coord_file: Path, chunk_id: str) -> BoundingBoxEntry | None:
        """Parse a coordinates JSON file and return a BoundingBoxEntry."""
        try:
            data = json.loads(coord_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not parse coordinates file %s: %s", coord_file, exc)
            return None

        nadir = data.get("bounding_box")
        swath = data.get("swath_bounding_box")

        if nadir is None or swath is None:
            logger.warning(
                "Coordinates file %s missing bounding_box or swath_bounding_box", coord_file
            )
            return None

        return BoundingBoxEntry(chunk_id=chunk_id, nadir=nadir, swath=swath)

    def _aggregate_metrics(
        self, chunks_dir: Path, successful_chunk_ids: list[str]
    ) -> tuple[float, dict[str, float]]:
        """Compute total_duration_s and average step timings from dataset.json files.

        Expected dataset.json timing fields (all optional):
            processing_start_time  — ISO timestamp
            processing_end_time    — ISO timestamp
            timings.extraction_s   — float seconds
            timings.satdump_s      — float seconds
            timings.rtstps_s       — float seconds
            timings.cspp_s         — float seconds

        Returns:
            (total_duration_s, metrics_dict)
        """
        extraction_times: list[float] = []
        satdump_times: list[float] = []
        rtstps_times: list[float] = []
        cspp_times: list[float] = []
        durations: list[float] = []

        for chunk_id in successful_chunk_ids:
            dataset_path = chunks_dir / chunk_id / "dataset.json"
            if not dataset_path.exists():
                continue
            try:
                data = json.loads(dataset_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not parse dataset.json for %s: %s", chunk_id, exc)
                continue

            # Per-chunk duration from start/end timestamps
            start_str = data.get("processing_start_time")
            end_str = data.get("processing_end_time")
            if start_str and end_str:
                try:
                    start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    durations.append((end_dt - start_dt).total_seconds())
                except (ValueError, TypeError) as exc:
                    logger.debug(
                        "Could not parse timestamps for %s: %s", chunk_id, exc
                    )

            # Step timings
            timings = data.get("timings", {})
            if isinstance(timings, dict):
                _maybe_append(timings.get("extraction_s"), extraction_times)
                _maybe_append(timings.get("satdump_s"), satdump_times)
                _maybe_append(timings.get("rtstps_s"), rtstps_times)
                _maybe_append(timings.get("cspp_s"), cspp_times)

        total_duration_s = sum(durations) if durations else 0.0
        metrics = {
            "extraction_avg_s": _average(extraction_times),
            "satdump_avg_s": _average(satdump_times),
            "rtstps_avg_s": _average(rtstps_times),
            "cspp_avg_s": _average(cspp_times),
        }

        return total_duration_s, metrics


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _maybe_append(value: object, target: list[float]) -> None:
    """Append value to target if it is a valid non-negative number."""
    if value is None:
        return
    try:
        f = float(value)
        if f >= 0.0:
            target.append(f)
    except (TypeError, ValueError):
        pass


def _average(values: list[float]) -> float:
    """Return the arithmetic mean, or 0.0 for an empty list."""
    return round(sum(values) / len(values), 3) if values else 0.0


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _result_to_dict(result: ManifestResult) -> dict:
    """Convert a ManifestResult to a JSON-serialisable dict."""
    return {
        "contact_id": result.contact_id,
        "contact_date": result.contact_date,
        "satellite": result.satellite,
        "norad_id": result.norad_id,
        "processing_timestamp": result.processing_timestamp,
        "pipeline_version": result.pipeline_version,
        "total_chunks": result.total_chunks,
        "successful_chunks": result.successful_chunks,
        "failed_chunks": [
            {
                "chunk_id": fc.chunk_id,
                "reason": fc.reason,
                "attempts": fc.attempts,
            }
            for fc in result.failed_chunks
        ],
        "sdr_files": [
            {
                "key": e.key,
                "band": e.band,
                "resolution_m": e.resolution_m,
                "type": e.type,
            }
            for e in result.sdr_files
        ],
        "geo_files": [
            {
                "key": e.key,
                "type": e.type,
                "resolution_m": e.resolution_m,
            }
            for e in result.geo_files
        ],
        "bounding_boxes": [
            {
                "chunk_id": bb.chunk_id,
                "nadir": bb.nadir,
                "swath": bb.swath,
            }
            for bb in result.bounding_boxes
        ],
        "total_duration_s": result.total_duration_s,
        "metrics": result.metrics,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI wrapper: generate_manifest.py <aggregation_dir> <output_manifest_path>."""
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <aggregation_dir> <output_manifest_path>",
            file=sys.stderr,
        )
        sys.exit(1)

    aggregation_dir = sys.argv[1]
    output_manifest_path = sys.argv[2]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Resolve metadata from environment
    contact_id = os.environ.get("CONTACT_ID", "")
    contact_date = os.environ.get("CONTACT_DATE", "")
    pipeline_version = os.environ.get("PIPELINE_VERSION", "1.0.0")

    if not contact_id:
        logger.error("CONTACT_ID environment variable is required but not set")
        sys.exit(1)

    if not contact_date:
        logger.error("CONTACT_DATE environment variable is required but not set")
        sys.exit(1)

    generator = ManifestGenerator()
    try:
        result = generator.generate(
            aggregation_dir=aggregation_dir,
            contact_id=contact_id,
            contact_date=contact_date,
            pipeline_version=pipeline_version,
        )
    except FileNotFoundError as exc:
        logger.error("Aggregation directory error: %s", exc)
        sys.exit(1)
    except AssertionError as exc:
        logger.error("Chunk count assertion failed: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error: %s", exc)
        sys.exit(1)

    # Write manifest
    output_path = Path(output_manifest_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_dict = _result_to_dict(result)
    output_path.write_text(
        json.dumps(manifest_dict, indent=2), encoding="utf-8"
    )

    logger.info("Manifest written to %s", output_path)

    # Summary to stdout (mirrors cspp_process.py style)
    print(f"Manifest generated — {result.contact_id} / {result.contact_date}")
    print(f"  Total chunks      : {result.total_chunks}")
    print(f"  Successful chunks : {result.successful_chunks}")
    print(f"  Failed chunks     : {len(result.failed_chunks)}")
    print(f"  SDR files         : {len(result.sdr_files)}")
    print(f"  GEO files         : {len(result.geo_files)}")
    print(f"  Bounding boxes    : {len(result.bounding_boxes)}")
    print(f"  Total duration    : {result.total_duration_s:.1f}s")
    if result.failed_chunks:
        print("  Failed chunks detail:")
        for fc in result.failed_chunks:
            print(f"    - {fc.chunk_id}: {fc.reason} (attempts={fc.attempts})")


if __name__ == "__main__":
    main()
