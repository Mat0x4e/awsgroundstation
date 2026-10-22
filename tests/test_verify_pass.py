"""Unit tests for scripts/verify_pass.py.

Encodes the project's hard rule:
    A green Step Functions execution does NOT prove imagery.
    Verify against products/ S3 prefix and CloudWatch metrics, not execution status.

Test categories:
    A. _classify_products / check_products_s3 — 9/9 vs partial vs 0 (mocked S3)
    B. Green SFN ≠ good — overall verdict with SUCCEEDED execution but zero products
    C. check_chunk_metrics — threshold logic (ChunksSucceeded > 0)
    D. check_lut_cache — warm vs cold reporting
    E. _save_report — files written correctly to a tmp directory
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# conftest.py adds the repo root to sys.path, so scripts.verify_pass resolves.
from scripts.verify_pass import (
    COMPOSITES,
    CRITICAL_CHECKS,
    EXPECTED_PRODUCT_COUNT,
    FAIL,
    INFO,
    PASS,
    WARN,
    _classify_products,
    _save_report,
    check_chunk_metrics,
    check_lut_cache,
    check_products_s3,
    check_products_written,
    run_checks,
)

# ---------------------------------------------------------------------------
# Shared mock-building helpers
# ---------------------------------------------------------------------------

_CONTACT_ID = "dcef1518-c454-401f-bdcd-dc2e73fe89a5"
_DATE_PATH = "2026/10/17"
_PRODUCTS_PREFIX = f"products/{_DATE_PATH}/{_CONTACT_ID}/"


def _product_keys(
    composites: list[str] | None = None,
    exts: list[str] | None = None,
) -> list[str]:
    """Build fake S3 product key strings for the given composites and extensions."""
    composites = composites if composites is not None else COMPOSITES
    exts = exts if exts is not None else [".png", ".tif", ".json"]
    return [
        f"{_PRODUCTS_PREFIX}viirs_satdump_{c}_20261017T204312Z{e}"
        for c in composites
        for e in exts
    ]


def _s3_mock_with_keys(keys: list[str], json_content_length: int = 500) -> MagicMock:
    """Return a mock S3 client whose paginator yields the given keys."""
    s3 = MagicMock()
    pag = MagicMock()
    pag.paginate.return_value = [
        {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}
    ]
    s3.get_paginator.return_value = pag
    s3.head_object.return_value = {"ContentLength": json_content_length}
    return s3


def _s3_mock_empty() -> MagicMock:
    """Return a mock S3 client whose paginator returns no objects."""
    return _s3_mock_with_keys([])


def _cw_mock_with_values(succeeded: float, failed: float, products_written: float = 0) -> MagicMock:
    """Return a mock CloudWatch client returning specific chunk metric values."""
    cw = MagicMock()

    def _get_stats(**kwargs):
        name = kwargs["MetricName"]
        if name == "ChunksSucceeded":
            dps = [{"Sum": succeeded, "Maximum": succeeded}] if succeeded > 0 else []
        elif name == "ChunksFailed":
            dps = [{"Sum": failed, "Maximum": failed}] if failed > 0 else []
        elif name == "ProductsWritten":
            dps = [{"Maximum": products_written}] if products_written > 0 else []
        else:
            dps = []
        return {"Datapoints": dps}

    cw.get_metric_statistics.side_effect = _get_stats
    cw.describe_alarms.return_value = {"MetricAlarms": []}
    return cw


def _cw_mock_zero() -> MagicMock:
    """Return a mock CloudWatch client reporting zero metrics and critical alarms."""
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    cw.describe_alarms.return_value = {
        "MetricAlarms": [
            {
                "AlarmName": "groundstation-noaa20-sdr-chunks-all-failed",
                "StateValue": "ALARM",
            },
            {
                "AlarmName": "groundstation-noaa20-viirs-products-written-zero",
                "StateValue": "ALARM",
            },
            {
                "AlarmName": "groundstation-noaa20-sdr-visualization-failed",
                "StateValue": "ALARM",
            },
        ]
    }
    return cw


def _sfn_mock_succeeded(contact_id: str) -> MagicMock:
    """Return a mock SFN client that reports a SUCCEEDED execution for contact_id."""
    sfn = MagicMock()
    sfn.list_state_machines.return_value = {
        "stateMachines": [
            {
                "name": "groundstation-noaa20-sdr-pipeline",
                "stateMachineArn": (
                    "arn:aws:states:eu-central-1:471112743408:"
                    "stateMachine:groundstation-noaa20-sdr-pipeline"
                ),
            }
        ]
    }
    exec_arn = (
        "arn:aws:states:eu-central-1:471112743408:"
        "execution:groundstation-noaa20-sdr-pipeline:test-exec"
    )
    pag = MagicMock()
    pag.paginate.return_value = [{"executions": [{"executionArn": exec_arn}]}]
    sfn.get_paginator.return_value = pag
    sfn.describe_execution.return_value = {
        "executionArn": exec_arn,
        "status": "SUCCEEDED",
        "input": json.dumps({"contact_id": contact_id}),
        "startDate": datetime(2026, 10, 17, 20, 43, 0, tzinfo=timezone.utc),
        "stopDate": datetime(2026, 10, 17, 21, 0, 0, tzinfo=timezone.utc),
    }
    return sfn


def _sfn_mock_with_times(
    contact_id: str,
    exec_start: datetime,
    exec_stop: datetime,
    status: str = "SUCCEEDED",
) -> MagicMock:
    """Return a mock SFN client with a completed execution at explicitly given times.

    Use this when a test needs to assert on the execution window that is threaded
    into the CloudWatch metric queries (e.g. regression tests for the false-FAIL bug).
    """
    sfn = MagicMock()
    sfn.list_state_machines.return_value = {
        "stateMachines": [
            {
                "name": "groundstation-noaa20-sdr-pipeline",
                "stateMachineArn": (
                    "arn:aws:states:eu-central-1:471112743408:"
                    "stateMachine:groundstation-noaa20-sdr-pipeline"
                ),
            }
        ]
    }
    exec_arn = (
        "arn:aws:states:eu-central-1:471112743408:"
        "execution:groundstation-noaa20-sdr-pipeline:test-exec"
    )
    pag = MagicMock()
    pag.paginate.return_value = [{"executions": [{"executionArn": exec_arn}]}]
    sfn.get_paginator.return_value = pag
    sfn.describe_execution.return_value = {
        "executionArn": exec_arn,
        "status": status,
        "input": json.dumps({"contact_id": contact_id}),
        "startDate": exec_start,
        "stopDate": exec_stop,
    }
    return sfn


def _make_client_factory(sfn=None, cw=None, s3=None):
    """Return a _client() side-effect that dispatches by service name."""
    _defaults = {
        "stepfunctions": sfn or MagicMock(),
        "cloudwatch": cw or MagicMock(),
        "s3": s3 or _s3_mock_empty(),
    }

    def _factory(service: str):
        return _defaults.get(service, MagicMock())

    return _factory


# ===========================================================================
# A. Product classification and S3 completeness
# ===========================================================================

class TestClassifyProducts:
    """Tests for _classify_products — pure Python, no AWS calls."""

    def test_9_of_9_all_composites_all_extensions(self):
        keys = _product_keys()  # all 3 composites × 3 exts
        found = _classify_products(keys)
        total = sum(len(v) for v in found.values())
        assert total == EXPECTED_PRODUCT_COUNT  # 9
        for composite in COMPOSITES:
            assert found[composite] == {".png", ".tif", ".json"}

    def test_6_of_9_one_composite_missing(self):
        keys = _product_keys(composites=["true_color", "thermal_ir"])
        found = _classify_products(keys)
        total = sum(len(v) for v in found.values())
        assert total == 6
        assert found["day_microphysics"] == set()

    def test_6_of_9_all_composites_missing_tif(self):
        keys = _product_keys(exts=[".png", ".json"])
        found = _classify_products(keys)
        for composite in COMPOSITES:
            assert ".tif" not in found[composite]
            assert ".png" in found[composite]
            assert ".json" in found[composite]

    def test_0_of_9_empty_key_list(self):
        found = _classify_products([])
        total = sum(len(v) for v in found.values())
        assert total == 0

    def test_irrelevant_keys_ignored(self):
        """Keys without composite names in the filename must not be counted."""
        keys = [
            "lut-cache/SDR_4_1_DB/some.lut",
            "contacts/2026/10/17/dcef1518/sdr/RNSCA-RVIRS_npp_d20261017.h5",
            "contacts/2026/10/17/dcef1518/satdump/chunk_0/dataset.json",
        ]
        found = _classify_products(keys)
        total = sum(len(v) for v in found.values())
        assert total == 0

    def test_composite_names_do_not_overlap(self):
        """No composite name is a substring of another — validate assumption."""
        for i, c1 in enumerate(COMPOSITES):
            for j, c2 in enumerate(COMPOSITES):
                if i != j:
                    assert c1 not in c2, (
                        f"{c1!r} is a substring of {c2!r} — "
                        "_classify_products uniqueness assumption broken"
                    )


class TestCheckProductsS3:
    """Tests for check_products_s3 with mocked S3 calls."""

    def test_pass_9_of_9(self):
        keys = _product_keys()
        s3 = _s3_mock_with_keys(keys)
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_products_s3(_CONTACT_ID, _DATE_PATH)
        assert status == PASS
        assert "9/9" in detail

    def test_warn_partial_6_of_9(self):
        keys = _product_keys(composites=["true_color", "thermal_ir"])
        s3 = _s3_mock_with_keys(keys)
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_products_s3(_CONTACT_ID, _DATE_PATH)
        assert status == WARN
        assert "6/9" in detail

    def test_fail_zero_products_with_date(self):
        """No objects at the prefix → FAIL when date is known."""
        s3 = _s3_mock_empty()
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_products_s3(_CONTACT_ID, _DATE_PATH)
        assert status == FAIL

    def test_warn_no_products_without_date(self):
        """No objects when date is None → WARN (not yet complete, not a hard FAIL)."""
        s3 = _s3_mock_empty()
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_products_s3(_CONTACT_ID, None)
        assert status == WARN
        # Detail must hint that the operator should supply --date
        assert "--date" in detail or "may not have completed" in detail.lower()

    def test_warn_trivial_json_sidecar(self):
        """JSON sidecars of ≤ 2 bytes indicate an empty-bbox re-render → WARN."""
        keys = _product_keys()
        # Return 2 bytes for every head_object call (includes JSON sidecars)
        s3 = _s3_mock_with_keys(keys, json_content_length=2)
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_products_s3(_CONTACT_ID, _DATE_PATH)
        assert status == WARN
        assert "sidecar" in detail.lower() or "empty" in detail.lower()

    def test_pass_is_a_critical_check(self):
        """products_s3 must be in CRITICAL_CHECKS so FAIL propagates to exit code."""
        assert "products_s3" in CRITICAL_CHECKS


# ===========================================================================
# B. Green SFN ≠ Good — the core rule encoded as test assertions
# ===========================================================================

class TestGreenSfnNotGood:
    """
    Hard rule: a SUCCEEDED Step Functions execution is context, not a verdict.
    These tests would all fail if someone changed verify_pass.py to let
    execution status alone determine the overall PASS/FAIL outcome.
    """

    def test_overall_fail_when_sfn_succeeded_but_no_products(self):
        """
        SUCCEEDED execution + empty products prefix + zero metrics = FAIL overall.
        This is the primary assertion of the green≠good rule.
        """
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=_cw_mock_zero(),
            s3=_s3_mock_empty(),
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, _ = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        assert not all_passed, (
            "overall should be FAIL when products are empty — "
            "a green SFN execution alone must NEVER produce PASS"
        )
        assert len(failures) > 0, "At least one critical check must have failed"

    def test_sfn_row_is_info_not_pass(self):
        """
        The Step Functions row must be INFO or WARN regardless of execution status.
        It must NEVER be PASS — execution status is not a pass criterion.
        """
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=_cw_mock_zero(),
            s3=_s3_mock_empty(),
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, _, _, _ = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        sfn_row = next((r for r in rows if "Step Functions" in r[0]), None)
        assert sfn_row is not None, "Step Functions check row must be present"
        assert sfn_row[1] in (INFO, WARN), (
            f"SFN row status must be INFO or WARN, got {sfn_row[1]!r} — "
            "execution status must never drive the pass/fail verdict"
        )
        assert sfn_row[1] != PASS, "SFN row must not be PASS under any circumstances"
        assert sfn_row[1] != FAIL, "SFN row must not be FAIL either — it's context only"

    def test_no_s3_products_zero_metric_overall_fail(self):
        """
        SAFETY GUARD: no products in S3 AND ProductsWritten=0 → overall FAIL.

        This is the primary failure mode the tool exists to catch (e.g. ba2c5446
        rerun: 0 products in S3, metric=0).  When BOTH sources confirm no imagery
        was produced, ProductsWritten must stay FAIL (critical) and the overall
        verdict must be FAIL, regardless of SFN execution status.

        This is the INVERSE of the dcef1518 case.  In dcef1518, S3 had 9/9 products
        so metric=0 was downgraded to WARN.  Here S3 is also empty — both sources
        agree the pass failed.  DO NOT weaken this path.
        """
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=_cw_mock_zero(),
            s3=_s3_mock_empty(),
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, _ = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        pw_row = next((r for r in rows if "ProductsWritten" in r[0]), None)
        assert pw_row is not None
        assert pw_row[1] == FAIL, (
            "ProductsWritten must be FAIL when both S3 is empty AND metric=0 "
            "(ba2c5446 failure mode). Must NOT be downgraded to WARN — "
            "that downgrade only applies when S3 PASS confirms products exist."
        )
        assert not all_passed
        assert len(failures) > 0

    def test_products_written_zero_is_critical_check(self):
        """products_written must be in CRITICAL_CHECKS — enforce via the set."""
        assert "products_written" in CRITICAL_CHECKS


# ===========================================================================
# C. Chunk metric threshold logic
# ===========================================================================

class TestCheckChunkMetrics:
    """Tests for check_chunk_metrics with mocked CloudWatch responses."""

    # Fixed window used in all direct check_chunk_metrics tests.
    # The function no longer computes its own window — callers supply it.
    _START = datetime(2026, 10, 17, 20, 38, 0, tzinfo=timezone.utc)  # exec_start − 5 min
    _END   = datetime(2026, 10, 17, 21, 55, 35, tzinfo=timezone.utc)  # arbitrary past window end
    _DESC  = "execution window"

    def test_pass_when_chunks_succeeded_gt_zero(self):
        cw = _cw_mock_with_values(succeeded=24, failed=0)
        with patch("scripts.verify_pass._client", return_value=cw):
            status, detail = check_chunk_metrics(
                start_time=self._START,
                end_time=self._END,
                window_desc=self._DESC,
            )
        assert status == PASS
        assert "ChunksSucceeded=24" in detail

    def test_fail_when_0_succeeded_44_failed(self):
        """The exact scenario from the rerun that prompted this check."""
        cw = _cw_mock_with_values(succeeded=0, failed=44)
        with patch("scripts.verify_pass._client", return_value=cw):
            status, detail = check_chunk_metrics(
                start_time=self._START,
                end_time=self._END,
                window_desc=self._DESC,
            )
        assert status == FAIL
        assert "ChunksFailed=44" in detail

    def test_warn_when_no_data_at_all(self):
        """0 succeeded, 0 failed → no data yet, not a hard failure."""
        cw = _cw_mock_with_values(succeeded=0, failed=0)
        with patch("scripts.verify_pass._client", return_value=cw):
            status, detail = check_chunk_metrics(
                start_time=self._START,
                end_time=self._END,
                window_desc=self._DESC,
            )
        assert status == WARN
        assert "no data" in detail.lower() or "processing" in detail.lower()

    def test_warn_when_failure_rate_exceeds_20_pct(self):
        """27 % failure rate (3 of 11) must raise a WARN."""
        cw = _cw_mock_with_values(succeeded=8, failed=3)
        with patch("scripts.verify_pass._client", return_value=cw):
            status, detail = check_chunk_metrics(
                start_time=self._START,
                end_time=self._END,
                window_desc=self._DESC,
            )
        assert status == WARN

    def test_pass_when_failure_rate_below_20_pct(self):
        """8 % failure rate (2 of 24) is within tolerance."""
        cw = _cw_mock_with_values(succeeded=22, failed=2)
        with patch("scripts.verify_pass._client", return_value=cw):
            status, detail = check_chunk_metrics(
                start_time=self._START,
                end_time=self._END,
                window_desc=self._DESC,
            )
        assert status == PASS

    def test_chunk_metrics_is_critical_check(self):
        assert "chunk_metrics" in CRITICAL_CHECKS

    def test_fail_status_propagates_to_overall(self):
        """FAIL from chunk_metrics must appear in critical_failures."""
        cw = _cw_mock_with_values(succeeded=0, failed=44)
        s3 = _s3_mock_empty()

        def _factory(service):
            if service == "cloudwatch":
                return cw
            return s3

        with patch("scripts.verify_pass._client", side_effect=_factory):
            _, all_passed, failures, _ = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,  # explicit override keeps this test's intent unchanged
            )

        assert not all_passed
        assert any("REL-3" in f or "chunk" in f.lower() for f in failures), (
            f"Expected a chunk-related failure in {failures!r}"
        )


# ===========================================================================
# D. LUT cache state (informational)
# ===========================================================================

class TestCheckLutCache:
    def test_warm_cache_returns_info(self):
        s3 = _s3_mock_with_keys(["lut-cache/SDR_4_1_DB/jpss1_viirs.lut"])
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_lut_cache()
        assert status == INFO
        assert "warm" in detail.lower()

    def test_cold_cache_returns_info(self):
        s3 = _s3_mock_empty()
        with patch("scripts.verify_pass._client", return_value=s3):
            status, detail = check_lut_cache()
        assert status == INFO
        assert "cold" in detail.lower() or "empty" in detail.lower()

    def test_lut_cache_is_never_fail(self):
        """LUT cache is informational only — it must never cause a critical failure."""
        assert "lut_cache" not in CRITICAL_CHECKS


# ===========================================================================
# E. Report saving
# ===========================================================================

class TestSaveReport:
    """Tests for _save_report — no AWS calls, uses a tmp directory."""

    _ROWS = [
        ("Step Functions execution", INFO, "SUCCEEDED in 17m00s [context only]"),
        ("Chunk metrics (REL-3)", PASS, "ChunksSucceeded=24, ChunksFailed=0"),
        ("ProductsWritten (REL-4)", PASS, "Max=3 over last 6h"),
        ("Products in S3 [truth]", PASS, "9/9 files"),
        ("Alarm: chunks-all-failed", PASS, "OK"),
        ("Alarm: products-written-zero", PASS, "OK"),
        ("Alarm: visualization-failed", PASS, "OK"),
        ("CADU / science artifacts", WARN, "sdr/: empty"),
        ("LUT cache (PERF-1)", INFO, "warm cache"),
    ]

    def test_txt_file_created(self, tmp_path):
        _save_report(
            save_dir=str(tmp_path),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=True,
            failures=[],
            verdict_line="✅  OVERALL: PASS",
        )
        assert (tmp_path / "verify_pass_report.txt").exists()

    def test_json_file_created(self, tmp_path):
        _save_report(
            save_dir=str(tmp_path),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=True,
            failures=[],
            verdict_line="✅  OVERALL: PASS",
        )
        assert (tmp_path / "verify_pass_report.json").exists()

    def test_json_content_correct(self, tmp_path):
        _save_report(
            save_dir=str(tmp_path),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=True,
            failures=[],
            verdict_line="✅  OVERALL: PASS",
        )
        data = json.loads((tmp_path / "verify_pass_report.json").read_text())
        assert data["contact_id"] == _CONTACT_ID
        assert data["overall_pass"] is True
        assert data["critical_failures"] == []
        assert len(data["checks"]) == len(self._ROWS)

    def test_json_records_failures(self, tmp_path):
        _save_report(
            save_dir=str(tmp_path),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=False,
            failures=["Products in S3 [truth]"],
            verdict_line="❌  OVERALL: FAIL",
        )
        data = json.loads((tmp_path / "verify_pass_report.json").read_text())
        assert data["overall_pass"] is False
        assert "Products in S3 [truth]" in data["critical_failures"]

    def test_creates_output_dir_if_absent(self, tmp_path):
        nested = tmp_path / "contact-07_oregon-1_2026-10-17"
        assert not nested.exists()
        _save_report(
            save_dir=str(nested),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=True,
            failures=[],
            verdict_line="✅  OVERALL: PASS",
        )
        assert nested.exists()
        assert (nested / "verify_pass_report.txt").exists()

    def test_txt_contains_contact_id(self, tmp_path):
        _save_report(
            save_dir=str(tmp_path),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=True,
            failures=[],
            verdict_line="✅  OVERALL: PASS",
        )
        txt = (tmp_path / "verify_pass_report.txt").read_text(encoding="utf-8")
        assert _CONTACT_ID in txt

    def test_txt_contains_verdict(self, tmp_path):
        _save_report(
            save_dir=str(tmp_path),
            contact_id=_CONTACT_ID,
            rows=self._ROWS,
            all_passed=False,
            failures=["ProductsWritten (REL-4)"],
            verdict_line="❌  OVERALL: FAIL",
        )
        txt = (tmp_path / "verify_pass_report.txt").read_text(encoding="utf-8")
        assert "OVERALL: FAIL" in txt


# ===========================================================================
# F. Execution-window-derived metric queries (regression for false-FAIL bug)
# ===========================================================================

class TestExecutionWindowDerivation:
    """
    Tests proving that CloudWatch metric queries use the SFN execution
    start/stop times as the query window, NOT a fixed 'now − N hours' offset.

    Regression for contact dcef1518 (Oregon #7, 2026-10-17):
    The pass SUCCEEDED with ChunksSucceeded=50 and ProductsWritten=27, but
    verify_pass.py reported FALSE FAIL on REL-3 and REL-4 because:
        - Execution ran at ~23:00–23:40 CEST
        - verify_pass.py was run 2+ days later
        - The fixed 'now − 6h' window saw zero CloudWatch datapoints
        - Both metric checks returned FAIL due to missing data, not actual failure
    """

    def test_metric_window_uses_execution_times_not_fixed_lookback(self):
        """
        get_metric_statistics must be called with StartTime ≈ exec_start − 5 min
        and EndTime ≈ exec_stop + 30 min, NOT now − 6 h.

        The +30 min post-pad covers the visualization CodeBuild buildspec's
        ProductsWritten emit, which fires after the build completes
        (~10-15 min after exec_stop).
        """
        exec_start = datetime(2026, 10, 17, 21, 0, 44, tzinfo=timezone.utc)
        exec_stop  = datetime(2026, 10, 17, 21, 40, 35, tzinfo=timezone.utc)

        sfn = _sfn_mock_with_times(_CONTACT_ID, exec_start, exec_stop)
        cw  = _cw_mock_with_values(succeeded=50, failed=0)
        s3  = _s3_mock_with_keys(_product_keys())

        factory = _make_client_factory(sfn=sfn, cw=cw, s3=s3)
        with patch("scripts.verify_pass._client", side_effect=factory):
            run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=None,  # default: execution-derived
            )

        expected_start = exec_start - timedelta(minutes=5)
        expected_end   = exec_stop  + timedelta(minutes=30)

        assert cw.get_metric_statistics.called, (
            "get_metric_statistics was never called — metric checks did not run"
        )
        for call in cw.get_metric_statistics.call_args_list:
            start = call.kwargs.get("StartTime")
            end   = call.kwargs.get("EndTime")
            if start is None:
                continue  # skip calls without explicit time kwargs

            assert abs((start - expected_start).total_seconds()) < 60, (
                f"StartTime {start!r} should be within 60 s of "
                f"exec_start − 5 min = {expected_start!r}. "
                f"A large discrepancy means the old now-6h window is still in use."
            )
            assert abs((end - expected_end).total_seconds()) < 60, (
                f"EndTime {end!r} should be within 60 s of "
                f"exec_stop + 30 min = {expected_end!r}."
            )

    def test_regression_old_execution_passes_not_fails(self):
        """
        REGRESSION: contact dcef1518 (Oregon #7, 2026-10-17).

        A clean, SUCCEEDED pass that ran 2 days ago must produce an overall
        PASS verdict when verified at any later time.

        The time-aware CloudWatch mock returns metric data only when the query
        window overlaps the execution (StartTime < exec_stop + 1 h).

        With the OLD fixed-window code (now - 6 h):
            StartTime = now - 6h >> exec_stop + 1h  ->  no data  ->  FAIL  (bug)

        With the NEW execution-derived code:
            StartTime = exec_start - 5min < exec_stop  ->  data  ->  PASS  (fix)

        The execution is pinned 2 days in the past (relative to the test run
        clock) so that 'now - 6h' always falls AFTER exec_data_cutoff.
        This test MUST FAIL against the old fixed-window logic.
        """
        # Pin execution 2 days in the past so now-6h > exec_stop + 1h (old code gets no data)
        now = datetime.now(timezone.utc)
        exec_start = now - timedelta(days=2)
        exec_stop  = exec_start + timedelta(minutes=40)
        # Verify the invariant this test relies on:
        #   now - 6h  >  exec_stop + 1h  =>  old code sees no data
        assert (now - timedelta(hours=6)) > (exec_stop + timedelta(hours=1)), (
            "Test invariant broken: now-6h should be after exec_stop+1h "
            "(execution must be >7h in the past)"
        )

        sfn = _sfn_mock_with_times(_CONTACT_ID, exec_start, exec_stop)

        # Time-aware CW mock: data only when query window overlaps the execution.
        exec_data_cutoff = exec_stop + timedelta(hours=1)
        cw = MagicMock()

        def _time_aware_stats(**kwargs):
            query_start = kwargs.get("StartTime")
            name        = kwargs.get("MetricName")
            if query_start is not None and query_start < exec_data_cutoff:
                if name == "ChunksSucceeded":
                    return {"Datapoints": [{"Sum": 50.0}]}
                if name == "ChunksFailed":
                    return {"Datapoints": []}
                if name == "ProductsWritten":
                    return {"Datapoints": [{"Maximum": 27.0}]}
            # query_start >= exec_data_cutoff -> "no data"
            # This is what the old now-6h code received: StartTime was hours/days
            # after exec_stop, so CloudWatch returned zero datapoints.
            return {"Datapoints": []}

        cw.get_metric_statistics.side_effect = _time_aware_stats
        cw.describe_alarms.return_value = {
            "MetricAlarms": [
                {
                    "AlarmName": "groundstation-noaa20-sdr-chunks-all-failed",
                    "StateValue": "OK",
                },
                {
                    "AlarmName": "groundstation-noaa20-viirs-products-written-zero",
                    "StateValue": "OK",
                },
                {
                    "AlarmName": "groundstation-noaa20-sdr-visualization-failed",
                    "StateValue": "OK",
                },
            ]
        }

        s3 = _s3_mock_with_keys(_product_keys())
        factory = _make_client_factory(sfn=sfn, cw=cw, s3=s3)

        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, _ = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=None,  # default: execution-derived (the fix)
            )

        assert all_passed, (
            f"REGRESSION: a clean pass with data in its execution window must produce "
            f"PASS, not FAIL. Got failures: {failures}. "
            f"This means the metric window is still computed as now-6h (the old bug): "
            f"the query landed after exec_stop, saw no data, and returned FAIL."
        )
        assert failures == []

        # Also confirm that the specific metric rows are PASS, not WARN/FAIL
        chunk_row = next((r for r in rows if "REL-3" in r[0]), None)
        pw_row    = next((r for r in rows if "REL-4" in r[0]), None)
        assert chunk_row is not None and chunk_row[1] == PASS, (
            f"Chunk metrics row should be PASS, got {chunk_row}"
        )
        assert pw_row is not None and pw_row[1] == PASS, (
            f"ProductsWritten row should be PASS, got {pw_row}"
        )


# ===========================================================================
# G. S3-truth-over-metric reconciliation
# ===========================================================================

class TestS3TruthOverMetricReconciliation:
    """
    Tests for the S3-truth-over-metric reconciliation logic in run_checks.

    Principle: products in S3 are the authoritative evidence of a successful
    pass; CloudWatch metrics are corroboration only.  When the two sources
    disagree, S3 wins.

    Four cases tested:
        Case 2 (dcef1518): S3 PASS + metric=0  → WARN, overall PASS
        Case 1 (healthy):  S3 PASS + metric>0  → both PASS, overall PASS
        Case 3 (safety):   S3 partial + metric=0 → metric stays FAIL, overall FAIL
        Case 4 (anomaly):  S3 FAIL + metric>0  → metric WARN, overall FAIL (from S3)
    """

    def test_s3_pass_metric_zero_is_warn_overall_pass(self):
        """
        S3 has 9/9 products + ProductsWritten metric=0 → metric check is WARN,
        overall verdict is PASS.  Exit code must be 0.

        This is the dcef1518 (Oregon #7, 2026-10-17) case: the contact was
        processed BEFORE the metric-emission bug was fixed.  The metric=0 is a
        permanent historical artifact; S3 confirms the imagery is genuine.

        This test FAILS against the pre-fix code (which returned overall FAIL).
        """
        # CW: chunks fine, ProductsWritten=0 (no datapoints)
        cw = _cw_mock_with_values(succeeded=24, failed=0, products_written=0)
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=cw,
            s3=_s3_mock_with_keys(_product_keys()),  # 9/9 products in S3
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, recon = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        pw_row = next((r for r in rows if "ProductsWritten" in r[0]), None)
        assert pw_row is not None, "ProductsWritten row must be present"
        assert pw_row[1] == WARN, (
            f"ProductsWritten must be WARN when S3 has 9/9 products but metric=0 "
            f"(S3-truth-over-metric, dcef1518 case). Got {pw_row[1]!r}."
        )
        assert "S3" in pw_row[2] or "authoritative" in pw_row[2], (
            f"WARN detail should mention S3 authority. Got: {pw_row[2]!r}"
        )

        assert all_passed, (
            f"Overall must be PASS when S3 has 9/9 products even if metric=0 "
            f"(dcef1518 pre-fix case). Got failures: {failures}"
        )
        assert failures == [], f"No critical failures expected, got: {failures}"

        # Reconciliation log must record the raw→reconciled downgrade
        assert len(recon) == 1, f"Expected 1 reconciliation event, got {len(recon)}"
        assert recon[0]["check"] == "ProductsWritten (REL-4)"
        assert recon[0]["raw_metric_status"] == FAIL
        assert recon[0]["reconciled_status"] == WARN
        assert "dcef1518" in recon[0]["principle"] or "pre-fix" in recon[0]["principle"]

    def test_s3_pass_metric_nonzero_both_pass(self):
        """
        S3 has 9/9 products + ProductsWritten metric=9 → both PASS, overall PASS.

        This is the healthy case: both ground truth and corroboration agree.
        No reconciliation event should be recorded.
        """
        cw = _cw_mock_with_values(succeeded=24, failed=0, products_written=9)
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=cw,
            s3=_s3_mock_with_keys(_product_keys()),
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, recon = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        pw_row = next((r for r in rows if "ProductsWritten" in r[0]), None)
        assert pw_row is not None
        assert pw_row[1] == PASS, (
            f"ProductsWritten must be PASS when metric=9 and S3 has 9/9. "
            f"Got {pw_row[1]!r}."
        )

        s3_row = next((r for r in rows if "S3" in r[0]), None)
        assert s3_row is not None and s3_row[1] == PASS

        assert all_passed, f"Overall must be PASS. Failures: {failures}"
        assert failures == []
        # No reconciliation when both sources agree
        assert recon == [], f"No reconciliation expected for healthy case, got: {recon}"

    def test_s3_partial_metric_zero_overall_fail(self):
        """
        S3 has 3/9 products (WARN — partial) + ProductsWritten metric=0 → overall FAIL.

        Partial imagery is not a PASS.  The S3 check returns WARN (not PASS), so
        the reconciliation does NOT downgrade ProductsWritten — it stays FAIL.
        Overall verdict is FAIL because ProductsWritten is still a critical FAIL.
        """
        partial_keys = _product_keys(composites=["true_color"])  # 3/9 files
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=_cw_mock_zero(),
            s3=_s3_mock_with_keys(partial_keys),
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, recon = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        s3_row = next((r for r in rows if "S3" in r[0]), None)
        assert s3_row is not None
        assert s3_row[1] == WARN, (
            f"S3 row must be WARN for partial products (3/9). Got {s3_row[1]!r}."
        )

        pw_row = next((r for r in rows if "ProductsWritten" in r[0]), None)
        assert pw_row is not None
        assert pw_row[1] == FAIL, (
            f"ProductsWritten must stay FAIL when S3 is not PASS (partial=3/9). "
            f"The downgrade to WARN only applies on S3 PASS. Got {pw_row[1]!r}."
        )

        assert not all_passed, "Overall must be FAIL for partial imagery + metric=0"
        assert len(failures) > 0, f"Expected at least one critical failure, got: {failures}"
        # No reconciliation event should be emitted (Case 3, not Case 2)
        assert recon == [], f"No reconciliation expected for partial-S3 case, got: {recon}"

    def test_s3_fail_metric_nonzero_is_anomaly_warn_overall_fail(self):
        """
        S3 has 0 products (FAIL) + ProductsWritten metric=9 → metric is WARN
        (anomaly), overall FAIL (S3 drives the verdict).

        Contradictory data: the metric claims products were written but S3 has
        none.  The anomaly is surfaced as WARN on the metric check.  S3 is the
        authoritative truth → overall FAIL via the products_s3 critical check.
        """
        # CW: chunks fine, ProductsWritten=9; S3: empty
        cw = _cw_mock_with_values(succeeded=24, failed=0, products_written=9)
        factory = _make_client_factory(
            sfn=_sfn_mock_succeeded(_CONTACT_ID),
            cw=cw,
            s3=_s3_mock_empty(),
        )
        with patch("scripts.verify_pass._client", side_effect=factory):
            rows, all_passed, failures, recon = run_checks(
                contact_id=_CONTACT_ID,
                date_path=_DATE_PATH,
                window_hours=6,
            )

        pw_row = next((r for r in rows if "ProductsWritten" in r[0]), None)
        assert pw_row is not None
        assert pw_row[1] == WARN, (
            f"ProductsWritten must be WARN (anomaly) when metric>0 but S3 is empty. "
            f"Got {pw_row[1]!r}."
        )
        assert "anomaly" in pw_row[2].lower() or "S3" in pw_row[2], (
            f"WARN detail should mention the anomaly or S3 authority. Got: {pw_row[2]!r}"
        )

        assert not all_passed, (
            "Overall must be FAIL when S3 has no products (S3 is authoritative)"
        )
        # products_s3 FAIL drives the verdict; products_written is just WARN
        s3_failures = [f for f in failures if "S3" in f or "truth" in f.lower()]
        assert len(s3_failures) > 0, (
            f"products_s3 critical FAIL should be in failures. Got: {failures}"
        )

        # Reconciliation log records the anomaly
        assert len(recon) == 1, f"Expected 1 reconciliation event, got {len(recon)}"
        assert recon[0]["raw_metric_status"] == PASS
        assert recon[0]["reconciled_status"] == WARN
        assert "anomaly" in recon[0]["principle"].lower()
