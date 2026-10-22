"""Unit tests for RTSTPSProcessor.

Covers:
    - Req 3.3: VIIRS granule validation (present and absent)
    - Req 3.4: RT-STPS failure propagation (non-zero exit code)
    - Req 3.5: Non-critical instrument (ATMS, CrIS) absence warnings

Strategy:
    - subprocess.run is mocked to avoid needing RT-STPS installed.
    - Real .h5 files are created on disk so that _validate_output and
      _find_rdr_files exercise their actual logic.
    - Those files go in the *sibling* ``data/`` directory, not the working
      directory: RT-STPS's bundled config/jpss1.xml declares
      ``<RDR ... directory="../data"/>``, so RDRs land next to the cwd rather
      than inside it. See the ``rtstps_dirs`` fixture.
"""

import subprocess
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.rtstps_process import (
    NoVIIRSDataError,
    RTSTPSError,
    RTSTPSProcessor,
    RTSTPSResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(returncode: int) -> subprocess.CompletedProcess:
    """Return a CompletedProcess stub with the given exit code."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout="RT-STPS mock output",
        stderr="",
    )


def _touch_h5(directory: Path, name: str) -> Path:
    """Create an empty .h5 file in *directory* and return its path."""
    p = directory / name
    p.write_bytes(b"")
    return p


# ---------------------------------------------------------------------------
# Fixture: a processor whose subprocess.run is stubbed to succeed (exit 0)
# ---------------------------------------------------------------------------

@pytest.fixture
def processor() -> RTSTPSProcessor:
    return RTSTPSProcessor()


@pytest.fixture
def rtstps_dirs(tmp_path) -> tuple[Path, Path]:
    """Mirror RT-STPS's real on-disk layout.

    ``batch.sh`` runs with ``cwd=<work>`` and the bundled ``config/jpss1.xml``
    writes RDRs to ``../data`` — a *sibling* of the working directory. The
    deployed ``aggregation.sh`` depends on exactly this: it runs batch.sh from
    ``/opt/rt-stps`` and collects ``/opt/data``.

    Nesting both under a per-test ``tmp_path`` also keeps the data directory
    isolated. Passing ``tmp_path`` itself as the working directory would make
    the scanned path ``<pytest-root>/data``, shared by every test in the run.

    Returns:
        ``(work, data)`` — pass ``work`` to ``process()``, create fixture .h5
        files in ``data``.
    """
    work = tmp_path / "rt-stps"
    data = tmp_path / "data"
    work.mkdir()
    data.mkdir()
    return work, data


# ---------------------------------------------------------------------------
# Test 1 — Req 3.3 (VIIRS present): process() returns a valid RTSTPSResult
# ---------------------------------------------------------------------------

def test_process_returns_result_when_viirs_files_exist(rtstps_dirs, processor):
    """When VIIRS .h5 files are present in RT-STPS's data dir, process() returns
    an RTSTPSResult with viirs_granules > 0 and 'VIIRS' in instruments_found.

    **Validates: Requirements 3.3**
    """
    work, data = rtstps_dirs
    # Create fake VIIRS RDR files (names contain "VIIRS" — matches the pattern)
    _touch_h5(data, "VIIRS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    _touch_h5(data, "VIIRS_npp_d20240101_t0001200_e0002399_b00001_c00000.h5")
    # Also create ATMS and CrIS files so only VIIRS is the focus here
    _touch_h5(data, "ATMS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    _touch_h5(data, "CrIS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")

    with patch("subprocess.run", return_value=_make_completed_process(0)):
        result = processor.process("input.cadu", str(work))

    assert isinstance(result, RTSTPSResult)
    assert result.viirs_granules == 2, (
        f"Expected 2 VIIRS granules, got {result.viirs_granules}"
    )
    assert "VIIRS" in result.instruments_found, (
        f"'VIIRS' should be in instruments_found, got {result.instruments_found}"
    )
    assert len(result.rdr_files) == 4


# ---------------------------------------------------------------------------
# Test 2 — Req 3.3 (VIIRS absent): process() raises NoVIIRSDataError
# ---------------------------------------------------------------------------

def test_process_raises_no_viirs_error_when_no_viirs_files(rtstps_dirs, processor):
    """When the data directory contains ATMS/CrIS files but NO VIIRS files,
    process() must raise NoVIIRSDataError — treating the absence of VIIRS as a
    complete failure and stopping processing.

    **Validates: Requirements 3.3**
    """
    work, data = rtstps_dirs
    # Only non-critical instruments present — no VIIRS
    _touch_h5(data, "ATMS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    _touch_h5(data, "CrIS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")

    with patch("subprocess.run", return_value=_make_completed_process(0)):
        with pytest.raises(NoVIIRSDataError) as exc_info:
            processor.process("input.cadu", str(work))

    message = str(exc_info.value)
    assert str(work) in message, (
        "NoVIIRSDataError message should name the working directory it was given"
    )
    assert str(data) in message, (
        "NoVIIRSDataError message should name the directory actually scanned, "
        f"so the ../data indirection is visible when debugging. Got: {message}"
    )


def test_process_raises_no_viirs_error_when_data_dir_is_empty(rtstps_dirs, processor):
    """When RT-STPS produces no output files at all, process() raises NoVIIRSDataError.

    **Validates: Requirements 3.3, 3.4**
    """
    work, _data = rtstps_dirs  # data dir exists but is empty
    with patch("subprocess.run", return_value=_make_completed_process(0)):
        with pytest.raises(NoVIIRSDataError):
            processor.process("input.cadu", str(work))


def test_process_scans_sibling_data_dir_not_working_dir(rtstps_dirs, processor):
    """Regression guard for the ../data contract.

    RDRs written *into* the working directory must NOT be picked up: RT-STPS's
    config/jpss1.xml sends them to ``../data``. A change that made process()
    scan output_dir would silently pass every other test in this file while
    breaking against real RT-STPS output.

    **Validates: Requirements 3.3**
    """
    work, _data = rtstps_dirs
    # Decoys in the working directory — the wrong place.
    _touch_h5(work, "VIIRS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    _touch_h5(work, "ATMS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")

    with patch("subprocess.run", return_value=_make_completed_process(0)):
        with pytest.raises(NoVIIRSDataError):
            processor.process("input.cadu", str(work))


# ---------------------------------------------------------------------------
# Test 3 — Req 3.5: non-critical instrument warnings when ATMS or CrIS absent
# ---------------------------------------------------------------------------

def test_process_warns_when_atms_missing_but_viirs_present(rtstps_dirs, processor):
    """When VIIRS is present but ATMS is absent, process() must:
      1. Emit a UserWarning via warnings.warn (not raise an exception)
      2. Include the warning message in result.warnings
    Processing must complete successfully — ATMS absence is non-critical.

    **Validates: Requirements 3.5**
    """
    work, data = rtstps_dirs
    _touch_h5(data, "VIIRS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    _touch_h5(data, "CrIS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    # ATMS intentionally absent

    with patch("subprocess.run", return_value=_make_completed_process(0)):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = processor.process("input.cadu", str(work))

    # Processing should not raise — ATMS is non-critical
    assert isinstance(result, RTSTPSResult)
    assert result.viirs_granules >= 1

    # A UserWarning mentioning ATMS must have been emitted
    atms_warnings = [w for w in caught if issubclass(w.category, UserWarning) and "ATMS" in str(w.message)]
    assert len(atms_warnings) >= 1, (
        f"Expected a UserWarning about ATMS, got: {[str(w.message) for w in caught]}"
    )

    # The warning must also be recorded in result.warnings
    assert any("ATMS" in msg for msg in result.warnings), (
        f"Expected 'ATMS' in result.warnings, got: {result.warnings}"
    )


def test_process_warns_when_cris_missing_but_viirs_present(rtstps_dirs, processor):
    """When VIIRS is present but CrIS is absent, process() must warn and continue.

    **Validates: Requirements 3.5**
    """
    work, data = rtstps_dirs
    _touch_h5(data, "VIIRS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    _touch_h5(data, "ATMS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    # CrIS intentionally absent

    with patch("subprocess.run", return_value=_make_completed_process(0)):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = processor.process("input.cadu", str(work))

    assert isinstance(result, RTSTPSResult)

    cris_warnings = [w for w in caught if issubclass(w.category, UserWarning) and "CrIS" in str(w.message)]
    assert len(cris_warnings) >= 1, (
        f"Expected a UserWarning about CrIS, got: {[str(w.message) for w in caught]}"
    )
    assert any("CrIS" in msg for msg in result.warnings)


def test_process_warns_for_both_atms_and_cris_when_both_absent(rtstps_dirs, processor):
    """When VIIRS is present but both ATMS and CrIS are absent, process() emits
    two separate UserWarnings and records both in result.warnings.

    **Validates: Requirements 3.5**
    """
    work, data = rtstps_dirs
    _touch_h5(data, "VIIRS_npp_d20240101_t0000000_e0001199_b00001_c00000.h5")
    # Neither ATMS nor CrIS present

    with patch("subprocess.run", return_value=_make_completed_process(0)):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = processor.process("input.cadu", str(work))

    assert isinstance(result, RTSTPSResult)
    assert result.viirs_granules >= 1

    instrument_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    warned_instruments = {
        inst
        for inst in ("ATMS", "CrIS")
        if any(inst in str(w.message) for w in instrument_warnings)
    }
    assert "ATMS" in warned_instruments, "Expected UserWarning for ATMS"
    assert "CrIS" in warned_instruments, "Expected UserWarning for CrIS"

    assert any("ATMS" in msg for msg in result.warnings)
    assert any("CrIS" in msg for msg in result.warnings)


# ---------------------------------------------------------------------------
# Test 4 — Req 3.4: RT-STPS subprocess failure raises RTSTPSError
# ---------------------------------------------------------------------------

def test_process_raises_rtstps_error_on_nonzero_exit_code(tmp_path, processor):
    """When the RT-STPS subprocess returns a non-zero exit code, process() must
    raise RTSTPSError containing the exit code, and must NOT proceed to output
    validation (no files need to exist in tmp_path).

    **Validates: Requirements 3.4**
    """
    # No output files — but process() should fail before it ever checks them
    with patch("subprocess.run", return_value=_make_completed_process(1)):
        with pytest.raises(RTSTPSError) as exc_info:
            processor.process("input.cadu", str(tmp_path))

    # The error message must reference the non-zero exit code
    assert "1" in str(exc_info.value), (
        f"RTSTPSError message should include exit code 1, got: {exc_info.value}"
    )


@pytest.mark.parametrize("exit_code", [2, 127, 255])
def test_process_raises_rtstps_error_for_various_exit_codes(tmp_path, processor, exit_code):
    """RTSTPSError is raised for any non-zero exit code, not just 1.

    **Validates: Requirements 3.4**
    """
    with patch("subprocess.run", return_value=_make_completed_process(exit_code)):
        with pytest.raises(RTSTPSError) as exc_info:
            processor.process("input.cadu", str(tmp_path))

    assert str(exit_code) in str(exc_info.value), (
        f"RTSTPSError message should include exit code {exit_code}"
    )


def test_process_does_not_call_validate_output_on_rtstps_failure(tmp_path, processor):
    """When RT-STPS fails, _validate_output must never be called — error is
    propagated immediately without inspecting the output directory.

    **Validates: Requirements 3.4**
    """
    with patch("subprocess.run", return_value=_make_completed_process(1)):
        with patch.object(processor, "_validate_output") as mock_validate:
            with pytest.raises(RTSTPSError):
                processor.process("input.cadu", str(tmp_path))

    mock_validate.assert_not_called()
