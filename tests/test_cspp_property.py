"""Property-based tests for CSPPProcessor partial failure resilience.

**Validates: Requirements 4.5, 4.6**

Property 5 — CSPP partial failure resilience:
    For any set of RDR granules where at least one is processable by CSPP SDR:
    - SDR output is produced for successful granules
    - Failed granules are recorded with error details
    - Status is "success" (all pass), "partial" (some pass), or "failure" (none pass)

    All-failure case raises TotalCSPPFailure (requirement 4.6).
    Per-granule failures are tolerated; processing continues (requirement 4.5).
"""

import os
import shutil
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.cspp_process import CSPPProcessor, CSPPResult, TotalCSPPFailure

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A list of booleans: True = granule succeeds, False = granule fails.
# At least 1 granule required by the property.
_outcomes = st.lists(st.booleans(), min_size=1, max_size=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_granule_result(granule_path: str, success: bool) -> dict:
    """Return the dict that _process_granule would return."""
    if success:
        return {"status": "success", "granule": granule_path, "exit_code": 0}
    return {
        "status": "failure",
        "granule": granule_path,
        "exit_code": 1,
        "error": "viirs_sdr.sh exited with code 1",
    }


def _make_sdr_files(output_dir: str, n_successes: int) -> tuple[list[str], list[str]]:
    """Return (sdr_files, geo_files) with one mock file per successful granule."""
    sdr_files = [
        os.path.join(output_dir, f"SVI01_npp_d20240101_t000000{i}_e000001_b00001_c{i:05d}.h5")
        for i in range(n_successes)
    ]
    geo_files = [
        os.path.join(output_dir, f"GIGTO_npp_d20240101_t000000{i}_e000001_b00001_c{i:05d}.h5")
        for i in range(n_successes)
    ]
    return sdr_files, geo_files


# ---------------------------------------------------------------------------
# Property 5: partial failure resilience
# ---------------------------------------------------------------------------

@given(outcomes=_outcomes)
@settings(max_examples=100)
def test_cspp_partial_failure_resilience(outcomes: list[bool]) -> None:
    """For any set of RDR granules, CSPPProcessor correctly classifies the run
    as success/partial/failure based on per-granule outcomes.

    **Validates: Requirements 4.5, 4.6**
    """
    n_total = len(outcomes)
    n_success = sum(outcomes)
    n_failed = n_total - n_success

    tmp_dir = tempfile.mkdtemp()
    try:
        rdr_dir = Path(tmp_dir) / "rdr"
        rdr_dir.mkdir()
        output_dir = Path(tmp_dir) / "output"
        output_dir.mkdir()

        # Create dummy .h5 files for each granule
        rdr_files = []
        for i in range(n_total):
            f = rdr_dir / f"RNSCA-RVIRS_npp_d20240101_t000000{i}_e000001_b00001_c{i:05d}.h5"
            f.touch()
            rdr_files.append(str(f))

        # Sort to match the order CSPPProcessor.process() will glob them
        rdr_files_sorted = sorted(rdr_files)

        # Build side_effect: return success/failure in the same order as sorted filenames
        def _process_granule_side_effect(granule_rdr: str, out_dir: str) -> dict:
            idx = rdr_files_sorted.index(granule_rdr)
            return _make_granule_result(granule_rdr, outcomes[idx])

        sdr_files, geo_files = _make_sdr_files(str(output_dir), n_success)

        processor = CSPPProcessor()

        with patch.object(
            CSPPProcessor, "_process_granule", side_effect=_process_granule_side_effect
        ), patch.object(
            CSPPProcessor, "_collect_outputs", return_value=(sdr_files, geo_files)
        ):
            if n_success == 0:
                # All failures → TotalCSPPFailure (requirement 4.6)
                with pytest.raises(TotalCSPPFailure):
                    processor.process(str(rdr_dir), str(output_dir))
            else:
                result = processor.process(str(rdr_dir), str(output_dir))

                # --- granule counts ---
                assert result.granules_processed == n_total, (
                    f"Expected granules_processed={n_total}, got {result.granules_processed}"
                )
                assert result.granules_failed == n_failed, (
                    f"Expected granules_failed={n_failed}, got {result.granules_failed}"
                )

                # --- failed_granules list ---
                assert len(result.failed_granules) == n_failed, (
                    f"Expected {n_failed} entries in failed_granules, "
                    f"got {len(result.failed_granules)}"
                )
                for fg in result.failed_granules:
                    assert "status" in fg, "failed_granule entry missing 'status'"
                    assert "granule" in fg, "failed_granule entry missing 'granule'"
                    assert "exit_code" in fg, "failed_granule entry missing 'exit_code'"
                    assert "error" in fg, "failed_granule entry missing 'error'"
                    assert fg["status"] == "failure"

                # --- SDR files ---
                assert len(result.sdr_files) == n_success, (
                    f"Expected {n_success} SDR file(s), got {len(result.sdr_files)}"
                )

                # --- status ---
                if n_failed == 0:
                    assert result.status == "success", (
                        f"All granules passed → expected status='success', got '{result.status}'"
                    )
                else:
                    assert result.status == "partial", (
                        f"Mixed outcomes → expected status='partial', got '{result.status}'"
                    )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 5b: all-failure case carries failure details in the exception context
# ---------------------------------------------------------------------------

@given(n_granules=st.integers(min_value=1, max_value=8))
@settings(max_examples=100)
def test_total_cspp_failure_raised_when_all_granules_fail(n_granules: int) -> None:
    """When every granule fails, TotalCSPPFailure is raised.

    **Validates: Requirements 4.6**
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        rdr_dir = Path(tmp_dir) / "rdr"
        rdr_dir.mkdir()
        output_dir = Path(tmp_dir) / "output"
        output_dir.mkdir()

        for i in range(n_granules):
            (rdr_dir / f"RNSCA-RVIRS_npp_d20240101_t000000{i}_e000001_b00001_c{i:05d}.h5").touch()

        def _fail_every_granule(granule_rdr: str, out_dir: str) -> dict:
            return _make_granule_result(granule_rdr, success=False)

        processor = CSPPProcessor()

        with patch.object(
            CSPPProcessor, "_process_granule", side_effect=_fail_every_granule
        ), patch.object(
            CSPPProcessor, "_collect_outputs", return_value=([], [])
        ):
            with pytest.raises(TotalCSPPFailure) as exc_info:
                processor.process(str(rdr_dir), str(output_dir))

            # Error message should mention the rdr_dir for traceability
            assert str(rdr_dir) in str(exc_info.value), (
                "TotalCSPPFailure message should reference rdr_dir"
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Deterministic complementary cases
# ---------------------------------------------------------------------------

def test_single_success_status_is_success(tmp_path) -> None:
    """One granule, one success → status='success', no failed_granules.

    Validates: Requirements 4.5, 4.6
    """
    rdr_dir = tmp_path / "rdr"
    rdr_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (rdr_dir / "RNSCA_00.h5").touch()

    sdr_files, geo_files = _make_sdr_files(str(output_dir), 1)

    processor = CSPPProcessor()
    with patch.object(
        CSPPProcessor, "_process_granule",
        return_value={"status": "success", "granule": "RNSCA_00.h5", "exit_code": 0},
    ), patch.object(
        CSPPProcessor, "_collect_outputs", return_value=(sdr_files, geo_files)
    ):
        result = processor.process(str(rdr_dir), str(output_dir))

    assert result.status == "success"
    assert result.granules_processed == 1
    assert result.granules_failed == 0
    assert result.failed_granules == []
    assert len(result.sdr_files) == 1


def test_mixed_outcomes_status_is_partial(tmp_path) -> None:
    """Two granules, one success one failure → status='partial'.

    Validates: Requirements 4.5
    """
    rdr_dir = tmp_path / "rdr"
    rdr_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    granules = ["RNSCA_00.h5", "RNSCA_01.h5"]
    for g in granules:
        (rdr_dir / g).touch()

    granule_paths = sorted(str(rdr_dir / g) for g in granules)
    outcomes_map = {granule_paths[0]: True, granule_paths[1]: False}

    def _side_effect(granule_rdr: str, out_dir: str) -> dict:
        return _make_granule_result(granule_rdr, outcomes_map[granule_rdr])

    sdr_files, geo_files = _make_sdr_files(str(output_dir), 1)

    processor = CSPPProcessor()
    with patch.object(
        CSPPProcessor, "_process_granule", side_effect=_side_effect
    ), patch.object(
        CSPPProcessor, "_collect_outputs", return_value=(sdr_files, geo_files)
    ):
        result = processor.process(str(rdr_dir), str(output_dir))

    assert result.status == "partial"
    assert result.granules_processed == 2
    assert result.granules_failed == 1
    assert len(result.failed_granules) == 1
    fg = result.failed_granules[0]
    assert fg["status"] == "failure"
    assert "exit_code" in fg
    assert "error" in fg


def test_all_failures_raises_total_cspp_failure(tmp_path) -> None:
    """All granules fail → TotalCSPPFailure raised, no CSPPResult returned.

    Validates: Requirements 4.6
    """
    rdr_dir = tmp_path / "rdr"
    rdr_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    for i in range(3):
        (rdr_dir / f"RNSCA_0{i}.h5").touch()

    processor = CSPPProcessor()
    with patch.object(
        CSPPProcessor, "_process_granule",
        return_value={
            "status": "failure",
            "granule": "x",
            "exit_code": 1,
            "error": "viirs_sdr.sh exited with code 1",
        },
    ), patch.object(
        CSPPProcessor, "_collect_outputs", return_value=([], [])
    ):
        with pytest.raises(TotalCSPPFailure):
            processor.process(str(rdr_dir), str(output_dir))


def test_failed_granule_exception_is_caught_and_recorded(tmp_path) -> None:
    """If _process_granule raises an exception, it is caught and the granule is
    recorded as failed; processing continues for remaining granules.

    Validates: Requirements 4.5
    """
    rdr_dir = tmp_path / "rdr"
    rdr_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    (rdr_dir / "RNSCA_00.h5").touch()
    (rdr_dir / "RNSCA_01.h5").touch()
    granule_paths = sorted(str(rdr_dir / g) for g in ["RNSCA_00.h5", "RNSCA_01.h5"])

    call_count = {"n": 0}

    def _raise_then_succeed(granule_rdr: str, out_dir: str) -> dict:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Simulated crash in _process_granule")
        return {"status": "success", "granule": granule_rdr, "exit_code": 0}

    sdr_files, geo_files = _make_sdr_files(str(output_dir), 1)

    processor = CSPPProcessor()
    with patch.object(
        CSPPProcessor, "_process_granule", side_effect=_raise_then_succeed
    ), patch.object(
        CSPPProcessor, "_collect_outputs", return_value=(sdr_files, geo_files)
    ):
        result = processor.process(str(rdr_dir), str(output_dir))

    assert result.granules_processed == 2
    assert result.granules_failed == 1
    assert result.status == "partial"
    # The exception must be recorded in failed_granules
    assert len(result.failed_granules) == 1
    fg = result.failed_granules[0]
    assert fg["status"] == "failure"
    assert fg["exit_code"] == -1
    assert "Simulated crash" in fg["error"]
