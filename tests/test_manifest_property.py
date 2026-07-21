"""Property-based tests for ManifestGenerator completeness.

**Validates: Requirements 6.7**

Property 10 — Manifest completeness:
    For any set of chunk processing results (mix of successes and failures),
    the generated manifest.json SHALL:
    - List all SDR + GEO files from successful chunks
    - List all failed chunks with error reasons
    - Satisfy: successful_chunks + len(failed_chunks) == total_chunks
"""

import json
import tempfile
import shutil
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.generate_manifest import ManifestGenerator


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Each chunk is either successful (True) or failed (False).
# Generate 1–20 chunks as described in the task strategy.
_chunk_outcomes = st.lists(st.booleans(), min_size=1, max_size=20)

# Failure reasons — realistic strings
_failure_reasons = st.sampled_from([
    "SatDump timeout",
    "RT-STPS crash",
    "CSPP SDR exit code 137",
    "No VIIRS data",
    "I/Q extraction failed",
    "Unknown error",
])

# Attempts count for failed chunks
_attempts = st.integers(min_value=1, max_value=2)

# Number of SDR files per successful chunk (0–5)
_sdr_count = st.integers(min_value=0, max_value=5)

# Number of GEO files per successful chunk (0–3)
_geo_count = st.integers(min_value=0, max_value=3)


# Composite strategy: generates a list of chunk descriptors
@st.composite
def chunk_results(draw):
    """Generate a list of chunk result descriptors.

    Each entry is a dict:
      - chunk_id: str
      - success: bool
      - reason: str (only relevant if success=False)
      - attempts: int (only relevant if success=False)
      - sdr_files: list[str] (filenames for successful chunks)
      - geo_files: list[str] (filenames for successful chunks)
    """
    outcomes = draw(_chunk_outcomes)
    chunks = []
    for i, success in enumerate(outcomes):
        chunk_id = f"chunk_{i + 1:03d}"
        if success:
            n_sdr = draw(_sdr_count)
            n_geo = draw(_geo_count)
            sdr_files = [
                f"SVI0{j + 1}_npp_d20260619_t1423{i:02d}_e1424{i:02d}_b00001_c00001.h5"
                for j in range(n_sdr)
            ]
            geo_files = [
                f"GIGTO_npp_d20260619_t1423{i:02d}_e1424{i:02d}_b00001_c00001.h5"
                if j == 0
                else f"GMODO_npp_d20260619_t1423{i:02d}_e1424{i:02d}_b00001_c00001.h5"
                for j in range(n_geo)
            ]
            chunks.append({
                "chunk_id": chunk_id,
                "success": True,
                "sdr_files": sdr_files,
                "geo_files": geo_files,
            })
        else:
            reason = draw(_failure_reasons)
            attempts = draw(_attempts)
            chunks.append({
                "chunk_id": chunk_id,
                "success": False,
                "reason": reason,
                "attempts": attempts,
                "sdr_files": [],
                "geo_files": [],
            })
    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_aggregation_dir(base_dir: Path, chunks: list[dict]) -> None:
    """Create the aggregation directory structure expected by ManifestGenerator.

    For successful chunks:
      - chunks/<chunk_id>/dataset.json (minimal valid file)
      - chunks/<chunk_id>/<sdr_file>.h5 (empty files)
      - chunks/<chunk_id>/<geo_file>.h5 (empty files)
      - coordinates/<chunk_id>.json (minimal bounding box)

    For failed chunks:
      - chunks/<chunk_id>/_failed.json with reason and attempts
    """
    chunks_dir = base_dir / "chunks"
    chunks_dir.mkdir(parents=True)
    coords_dir = base_dir / "coordinates"
    coords_dir.mkdir(parents=True)

    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        chunk_path = chunks_dir / chunk_id
        chunk_path.mkdir()

        if chunk["success"]:
            # Write a minimal dataset.json
            dataset = {
                "satellite": "NOAA-20",
                "frames_decoded": 100,
                "processing_start_time": "2026-06-19T14:20:00Z",
                "processing_end_time": "2026-06-19T14:25:00Z",
                "timings": {
                    "extraction_s": 7.5,
                    "satdump_s": 300.0,
                    "rtstps_s": 120.0,
                    "cspp_s": 310.0,
                },
            }
            (chunk_path / "dataset.json").write_text(
                json.dumps(dataset), encoding="utf-8"
            )

            # Create dummy SDR files
            for sdr_file in chunk["sdr_files"]:
                (chunk_path / sdr_file).touch()

            # Create dummy GEO files
            for geo_file in chunk["geo_files"]:
                (chunk_path / geo_file).touch()

            # Write a coordinates file
            coord_data = {
                "chunk_id": chunk_id,
                "bounding_box": {
                    "north": 52.1,
                    "south": 48.3,
                    "east": 12.5,
                    "west": 8.1,
                },
                "swath_bounding_box": {
                    "north": 55.2,
                    "south": 45.1,
                    "east": 18.0,
                    "west": 2.0,
                },
            }
            (coords_dir / f"{chunk_id}.json").write_text(
                json.dumps(coord_data), encoding="utf-8"
            )
        else:
            # Write _failed.json
            failed_data = {
                "reason": chunk["reason"],
                "attempts": chunk["attempts"],
            }
            (chunk_path / "_failed.json").write_text(
                json.dumps(failed_data), encoding="utf-8"
            )


# ---------------------------------------------------------------------------
# Property 10: Manifest completeness
# ---------------------------------------------------------------------------

@given(chunks=chunk_results())
@settings(max_examples=100)
def test_manifest_completeness(chunks: list[dict]) -> None:
    """For any set of chunk results, the manifest lists all SDR + GEO files
    from successful chunks, all failed chunks with error reasons, and satisfies
    successful_chunks + len(failed_chunks) == total_chunks.

    **Validates: Requirements 6.7**
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        base_dir = Path(tmp_dir) / "aggregation"
        _setup_aggregation_dir(base_dir, chunks)

        generator = ManifestGenerator()
        result = generator.generate(
            aggregation_dir=str(base_dir),
            contact_id="test-contact-001",
            contact_date="2026-06-19",
            pipeline_version="1.0.0",
        )

        # --- Invariant: successful + failed == total ---
        total_chunks = len(chunks)
        successful_chunks = [c for c in chunks if c["success"]]
        failed_chunks = [c for c in chunks if not c["success"]]

        assert result.total_chunks == total_chunks, (
            f"total_chunks mismatch: expected {total_chunks}, got {result.total_chunks}"
        )
        assert result.successful_chunks == len(successful_chunks), (
            f"successful_chunks mismatch: expected {len(successful_chunks)}, "
            f"got {result.successful_chunks}"
        )
        assert len(result.failed_chunks) == len(failed_chunks), (
            f"failed_chunks count mismatch: expected {len(failed_chunks)}, "
            f"got {len(result.failed_chunks)}"
        )
        assert result.successful_chunks + len(result.failed_chunks) == result.total_chunks, (
            f"Completeness invariant violated: "
            f"{result.successful_chunks} + {len(result.failed_chunks)} != {result.total_chunks}"
        )

        # --- All failed chunks listed with error reasons ---
        failed_chunk_ids_expected = {c["chunk_id"] for c in failed_chunks}
        failed_chunk_ids_actual = {fc.chunk_id for fc in result.failed_chunks}
        assert failed_chunk_ids_actual == failed_chunk_ids_expected, (
            f"Failed chunk IDs mismatch: "
            f"expected {failed_chunk_ids_expected}, got {failed_chunk_ids_actual}"
        )

        for fc in result.failed_chunks:
            # Every failed chunk must have a non-empty reason
            assert fc.reason, f"Failed chunk {fc.chunk_id} has empty reason"
            # Attempts must be a non-negative integer
            assert fc.attempts >= 0, (
                f"Failed chunk {fc.chunk_id} has negative attempts: {fc.attempts}"
            )

        # --- All SDR files from successful chunks are listed ---
        expected_sdr_filenames = set()
        for chunk in successful_chunks:
            for sdr_file in chunk["sdr_files"]:
                expected_sdr_filenames.add(sdr_file)

        actual_sdr_filenames = set()
        for sdr_entry in result.sdr_files:
            # The key is a full S3 path; extract the filename
            filename = sdr_entry.key.split("/")[-1]
            actual_sdr_filenames.add(filename)

        assert expected_sdr_filenames == actual_sdr_filenames, (
            f"SDR files mismatch:\n"
            f"  expected: {sorted(expected_sdr_filenames)}\n"
            f"  actual:   {sorted(actual_sdr_filenames)}"
        )

        # --- All GEO files from successful chunks are listed ---
        expected_geo_filenames = set()
        for chunk in successful_chunks:
            for geo_file in chunk["geo_files"]:
                expected_geo_filenames.add(geo_file)

        actual_geo_filenames = set()
        for geo_entry in result.geo_files:
            filename = geo_entry.key.split("/")[-1]
            actual_geo_filenames.add(filename)

        assert expected_geo_filenames == actual_geo_filenames, (
            f"GEO files mismatch:\n"
            f"  expected: {sorted(expected_geo_filenames)}\n"
            f"  actual:   {sorted(actual_geo_filenames)}"
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
