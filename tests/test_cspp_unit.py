"""Unit tests for CSPPProcessor output collection and failure recording.

Tests cover:
  - _collect_outputs() SDR file pattern matching (I-band, M-band, DNB)
  - _collect_outputs() GEO file pattern matching (GIGTO, GMODO, GDNBO)
  - Per-granule failure recording with error details

Requirements validated: 4.2, 4.3
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from scripts.cspp_process import CSPPProcessor, CSPPResult, TotalCSPPFailure


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def processor() -> CSPPProcessor:
    return CSPPProcessor()


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Create a temporary output directory for SDR/GEO files."""
    return tmp_path / "cspp_output"


# ---------------------------------------------------------------------------
# Helper: create dummy HDF5 files matching NOAA naming conventions
# ---------------------------------------------------------------------------


def _create_files(directory: Path, filenames: list[str]) -> list[Path]:
    """Create empty files in directory and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    for name in filenames:
        p = directory / name
        p.write_bytes(b"")
        created.append(p)
    return created


# ---------------------------------------------------------------------------
# Tests: _collect_outputs — SDR file pattern matching
# ---------------------------------------------------------------------------


class TestCollectOutputsSDR:
    """Test that _collect_outputs correctly identifies SDR files."""

    def test_iband_sdr_files_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """I-band SDR files (SVI01–SVI05) are collected."""
        filenames = [
            "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVI02_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVI03_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVI04_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVI05_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        sdr_files, _ = processor._collect_outputs(str(output_dir))

        assert len(sdr_files) == 5
        for f in sdr_files:
            assert Path(f).name.startswith("SVI0")

    def test_mband_sdr_files_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """M-band SDR files (SVM01–SVM16) are collected."""
        filenames = [
            "SVM01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVM08_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVM16_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        sdr_files, _ = processor._collect_outputs(str(output_dir))

        assert len(sdr_files) == 3
        for f in sdr_files:
            assert Path(f).name.startswith("SVM")

    def test_dnb_sdr_files_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """DNB SDR files (SVDNB) are collected."""
        filenames = [
            "SVDNB_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        sdr_files, _ = processor._collect_outputs(str(output_dir))

        assert len(sdr_files) == 1
        assert Path(sdr_files[0]).name.startswith("SVDNB")

    def test_all_sdr_bands_mixed(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """A directory with I-band, M-band, and DNB SDR files collects all."""
        filenames = [
            "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVM05_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVDNB_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        sdr_files, _ = processor._collect_outputs(str(output_dir))

        assert len(sdr_files) == 3

    def test_non_sdr_files_not_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """Files that don't match SDR patterns are excluded."""
        filenames = [
            "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",  # GEO, not SDR
            "random_file.h5",
            "SVI01_npp_d20260619_t1423000.txt",  # wrong extension pattern
            "dataset.json",
        ]
        _create_files(output_dir, filenames)

        sdr_files, _ = processor._collect_outputs(str(output_dir))

        # Only GEO files should not appear in sdr_files
        sdr_names = [Path(f).name for f in sdr_files]
        assert "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5" not in sdr_names
        assert "random_file.h5" not in sdr_names
        assert "dataset.json" not in sdr_names

    def test_empty_directory_returns_empty_sdr(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """An empty directory yields no SDR files."""
        output_dir.mkdir(parents=True, exist_ok=True)

        sdr_files, _ = processor._collect_outputs(str(output_dir))

        assert sdr_files == []


# ---------------------------------------------------------------------------
# Tests: _collect_outputs — GEO file pattern matching
# ---------------------------------------------------------------------------


class TestCollectOutputsGEO:
    """Test that _collect_outputs correctly identifies GEO files."""

    def test_iband_geo_files_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """I-band GEO files (GIGTO) are collected."""
        filenames = [
            "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        _, geo_files = processor._collect_outputs(str(output_dir))

        assert len(geo_files) == 1
        assert Path(geo_files[0]).name.startswith("GIGTO")

    def test_mband_geo_files_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """M-band GEO files (GMODO) are collected."""
        filenames = [
            "GMODO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        _, geo_files = processor._collect_outputs(str(output_dir))

        assert len(geo_files) == 1
        assert Path(geo_files[0]).name.startswith("GMODO")

    def test_dnb_geo_files_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """DNB GEO files (GDNBO) are collected."""
        filenames = [
            "GDNBO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        _, geo_files = processor._collect_outputs(str(output_dir))

        assert len(geo_files) == 1
        assert Path(geo_files[0]).name.startswith("GDNBO")

    def test_all_geo_types_mixed(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """A directory with all three GEO types collects all."""
        filenames = [
            "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GMODO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GDNBO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        _, geo_files = processor._collect_outputs(str(output_dir))

        assert len(geo_files) == 3

    def test_non_geo_files_not_matched(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """SDR files and other files are excluded from GEO results."""
        filenames = [
            "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",  # SDR, not GEO
            "SVM05_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",  # SDR
            "random_file.h5",
        ]
        _create_files(output_dir, filenames)

        _, geo_files = processor._collect_outputs(str(output_dir))

        assert geo_files == []

    def test_empty_directory_returns_empty_geo(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """An empty directory yields no GEO files."""
        output_dir.mkdir(parents=True, exist_ok=True)

        _, geo_files = processor._collect_outputs(str(output_dir))

        assert geo_files == []


# ---------------------------------------------------------------------------
# Tests: _collect_outputs — combined SDR + GEO
# ---------------------------------------------------------------------------


class TestCollectOutputsCombined:
    """Test _collect_outputs with realistic mixed output directories."""

    def test_realistic_output_directory(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """A realistic CSPP output dir contains both SDR and GEO files."""
        filenames = [
            # SDR files
            "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVI02_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVM01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVM08_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVDNB_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            # GEO files
            "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GMODO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GDNBO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            # Non-matching files (should be ignored)
            "dataset.json",
            "satdump.log",
        ]
        _create_files(output_dir, filenames)

        sdr_files, geo_files = processor._collect_outputs(str(output_dir))

        assert len(sdr_files) == 5
        assert len(geo_files) == 3

    def test_results_are_sorted(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """Returned file lists are sorted alphabetically."""
        filenames = [
            "SVM08_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "SVDNB_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GMODO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        sdr_files, geo_files = processor._collect_outputs(str(output_dir))

        assert sdr_files == sorted(sdr_files)
        assert geo_files == sorted(geo_files)

    def test_returns_absolute_paths(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """Returned paths are absolute."""
        filenames = [
            "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
            "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5",
        ]
        _create_files(output_dir, filenames)

        sdr_files, geo_files = processor._collect_outputs(str(output_dir))

        for f in sdr_files + geo_files:
            assert Path(f).is_absolute()


# ---------------------------------------------------------------------------
# Tests: Per-granule failure recording
# ---------------------------------------------------------------------------


class TestPerGranuleFailureRecording:
    """Test that per-granule failures are recorded with error details."""

    def test_single_granule_failure_recorded(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """A single failing granule is recorded in failed_granules with error details."""
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create a fake RDR directory with one file
        rdr_dir = output_dir.parent / "rdr"
        rdr_dir.mkdir()
        (rdr_dir / "RNSCA-RVIRS_npp_d20260619_t1423000.h5").write_bytes(b"fake")

        # Mock _invoke_cspp to return non-zero (failure)
        with patch.object(processor, "_invoke_cspp", return_value=1):
            with pytest.raises(TotalCSPPFailure):
                processor.process(str(rdr_dir), str(output_dir))

    def test_failed_granule_contains_error_details(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """Failed granules dict includes status, granule path, exit_code, and error."""
        output_dir.mkdir(parents=True, exist_ok=True)

        rdr_dir = output_dir.parent / "rdr"
        rdr_dir.mkdir()
        granule_path = rdr_dir / "RNSCA-RVIRS_npp_d20260619_t1423000.h5"
        granule_path.write_bytes(b"fake")

        # Mock _invoke_cspp to return exit code 2
        with patch.object(processor, "_invoke_cspp", return_value=2):
            with pytest.raises(TotalCSPPFailure):
                processor.process(str(rdr_dir), str(output_dir))

        # Run _process_granule directly to inspect the failure dict
        with patch.object(processor, "_invoke_cspp", return_value=2):
            result = processor._process_granule(str(granule_path), str(output_dir))

        assert result["status"] == "failure"
        assert result["granule"] == str(granule_path)
        assert result["exit_code"] == 2
        assert "error" in result
        assert "2" in result["error"]

    def test_exception_during_granule_processing_recorded(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """An exception during granule processing is recorded with error message."""
        output_dir.mkdir(parents=True, exist_ok=True)

        rdr_dir = output_dir.parent / "rdr"
        rdr_dir.mkdir()
        granule_path = rdr_dir / "RNSCA-RVIRS_npp_d20260619_t1423000.h5"
        granule_path.write_bytes(b"fake")

        # Mock _invoke_cspp to raise an exception
        with patch.object(processor, "_invoke_cspp", side_effect=OSError("Permission denied")):
            result = processor._process_granule(str(granule_path), str(output_dir))

        assert result["status"] == "failure"
        assert result["exit_code"] == -1
        assert "Permission denied" in result["error"]

    def test_partial_success_records_failed_granules(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """When some granules fail and some succeed, failures are recorded and status is partial."""
        output_dir.mkdir(parents=True, exist_ok=True)

        rdr_dir = output_dir.parent / "rdr"
        rdr_dir.mkdir()
        (rdr_dir / "granule_01.h5").write_bytes(b"fake")
        (rdr_dir / "granule_02.h5").write_bytes(b"fake")

        call_count = [0]

        def mock_invoke(rdr_files, out_dir):
            call_count[0] += 1
            if call_count[0] == 1:
                # First granule succeeds — create SDR output
                sdr_name = "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5"
                (Path(out_dir) / sdr_name).write_bytes(b"sdr")
                return 0
            else:
                # Second granule fails
                return 1

        with patch.object(processor, "_invoke_cspp", side_effect=mock_invoke):
            result = processor.process(str(rdr_dir), str(output_dir))

        assert result.status == "partial"
        assert result.granules_processed == 2
        assert result.granules_failed == 1
        assert len(result.failed_granules) == 1
        assert result.failed_granules[0]["status"] == "failure"
        assert "granule_02.h5" in result.failed_granules[0]["granule"]

    def test_all_granules_succeed_no_failures(self, processor: CSPPProcessor, output_dir: Path) -> None:
        """When all granules succeed, failed_granules is empty and status is success."""
        output_dir.mkdir(parents=True, exist_ok=True)

        rdr_dir = output_dir.parent / "rdr"
        rdr_dir.mkdir()
        (rdr_dir / "granule_01.h5").write_bytes(b"fake")

        def mock_invoke(rdr_files, out_dir):
            sdr_name = "SVI01_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5"
            geo_name = "GIGTO_npp_d20260619_t1423000_e1424000_b00001_c20260619150000.h5"
            (Path(out_dir) / sdr_name).write_bytes(b"sdr")
            (Path(out_dir) / geo_name).write_bytes(b"geo")
            return 0

        with patch.object(processor, "_invoke_cspp", side_effect=mock_invoke):
            result = processor.process(str(rdr_dir), str(output_dir))

        assert result.status == "success"
        assert result.granules_failed == 0
        assert result.failed_granules == []
        assert len(result.sdr_files) == 1
        assert len(result.geo_files) == 1
