"""Tests for the VIIRS visualization orchestrator Lambda.

Focus: the acquisition time handed to the SatDump path. Without it,
visualize_satdump.py falls back to midnight on the contact date and
propagates the TLE to the wrong point in the orbit -- contact #5 rendered
central Mediterranean imagery labelled with a Caspian bounding box.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError as BotoCoreClientError
from moto import mock_aws

_HANDLER = Path(__file__).resolve().parent.parent / "lambdas" / "viirs_visualizer" / "handler.py"
_spec = importlib.util.spec_from_file_location("viirs_visualizer_handler", _HANDLER)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


PREFIX = "contacts/2026/08/31/ba2c5446"

# 2026-08-31T11:59:57.684743Z -- SatDump's own timestamp for chunk_0 of
# contact #5, eight seconds after AOS.
CHUNK0_TS = 1788177597.684743
CHUNK3_TS = CHUNK0_TS + 90


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 signature
        return {"Body": io.BytesIO(self._objects[Key])}


class FakeCodeBuild:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        return {"build": {"id": "project:build-id"}}


def _orchestrator(objects=None, codebuild=None):
    return handler.VisualizationOrchestrator(
        s3_client=FakeS3(objects or {}),
        codebuild_client=codebuild or FakeCodeBuild(),
        input_bucket="bucket",
        codebuild_project="project",
        enable_geotiff="true",
    )


def _dataset(ts: float) -> bytes:
    return json.dumps({"products": ["VIIRS"], "satellite": "NOAA 20 (JPSS-1)", "timestamp": ts}).encode()


def test_contact_time_read_from_chunk_dataset():
    key = f"{PREFIX}/satdump/chunk_0/dataset.json"
    orch = _orchestrator({key: _dataset(CHUNK0_TS)})

    # A lone chunk gives a time but no cadence to measure.
    assert orch._lookup_acquisition("bucket", [key]) == ("11:59:57", None)


def test_lowest_numbered_chunk_wins():
    """Must match the buildspec's staging rule: lowest chunk wins each file."""
    keys = {
        f"{PREFIX}/satdump/chunk_3/dataset.json": _dataset(CHUNK3_TS),
        f"{PREFIX}/satdump/chunk_0/dataset.json": _dataset(CHUNK0_TS),
        f"{PREFIX}/satdump/chunk_10/dataset.json": _dataset(CHUNK3_TS),
    }
    orch = _orchestrator(keys)

    # chunk_10 must not beat chunk_3 on a string sort.
    time, _ = orch._lookup_acquisition("bucket", list(keys))
    assert time == "11:59:57"


def test_cadence_measured_from_consecutive_chunks():
    """The gap chunk_0 -> chunk_1 is what one composite covers along-track."""
    keys = {
        f"{PREFIX}/satdump/chunk_0/dataset.json": _dataset(CHUNK0_TS),
        f"{PREFIX}/satdump/chunk_1/dataset.json": _dataset(CHUNK0_TS + 30),
        f"{PREFIX}/satdump/chunk_2/dataset.json": _dataset(CHUNK0_TS + 60),
    }
    orch = _orchestrator(keys)

    assert orch._lookup_acquisition("bucket", list(keys)) == ("11:59:57", 30.0)


@pytest.mark.parametrize("gap", [-30.0, 0.0, 3600.0])
def test_implausible_cadence_is_dropped(gap):
    """Out-of-order or absurd gaps must not become a propagation window."""
    keys = {
        f"{PREFIX}/satdump/chunk_0/dataset.json": _dataset(CHUNK0_TS),
        f"{PREFIX}/satdump/chunk_1/dataset.json": _dataset(CHUNK0_TS + gap),
    }
    orch = _orchestrator(keys)

    time, duration = orch._lookup_acquisition("bucket", list(keys))
    assert time == "11:59:57"
    assert duration is None


@pytest.mark.parametrize(
    "objects, keys",
    [
        ({}, []),                                                        # no dataset.json
        ({f"{PREFIX}/satdump/chunk_0/dataset.json": b"{}"},              # no timestamp field
         [f"{PREFIX}/satdump/chunk_0/dataset.json"]),
        ({f"{PREFIX}/satdump/chunk_0/dataset.json": b"not json"},        # corrupt
         [f"{PREFIX}/satdump/chunk_0/dataset.json"]),
    ],
)
def test_missing_or_unusable_timestamp_returns_none(objects, keys):
    assert _orchestrator(objects)._lookup_acquisition("bucket", keys) == (None, None)


def test_flag_rendered_into_satdump_buildspec():
    cb = FakeCodeBuild()
    orch = _orchestrator(codebuild=cb)

    orch._submit_codebuild(
        path="satdump",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time="11:59:57",
        chunk_duration=30.0,
    )

    spec = cb.calls[0]["buildspecOverride"]
    assert '--contact-time "11:59:57"' in spec
    assert '--pass-duration-seconds "30"' in spec
    assert "__CONTACT_TIME_ARG__" not in spec
    assert "__PASS_DURATION_ARG__" not in spec


def test_flag_omitted_when_time_unknown():
    """An empty --contact-time fails to parse; no flag is better than a bad one."""
    cb = FakeCodeBuild()
    orch = _orchestrator(codebuild=cb)

    orch._submit_codebuild(
        path="satdump",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time=None,
    )

    spec = cb.calls[0]["buildspecOverride"]
    assert "--contact-time" not in spec
    assert "--pass-duration-seconds" not in spec
    assert "__CONTACT_TIME_ARG__" not in spec
    assert "__PASS_DURATION_ARG__" not in spec


def test_nasa_path_carries_no_contact_time():
    """The NASA path takes geolocation from the GEO HDF5, not from a TLE."""
    cb = FakeCodeBuild()
    orch = _orchestrator(codebuild=cb)

    orch._submit_codebuild(
        path="nasa",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time="11:59:57",
    )

    assert "--contact-time" not in cb.calls[0]["buildspecOverride"]


def test_nasa_detection_matches_j01_names():
    """CSPP writes SVI01_j01_*; the old patterns only knew _npp_."""
    orch = _orchestrator()
    keys = [f"{PREFIX}/sdr/SVI01_j01_d20260831_t1206350_e1207596_b45517_c2026_cspp_dev.h5"]

    assert orch._detect_path(keys) == "nasa"


# ---------------------------------------------------------------------------
# ProductsWritten metric lifecycle tests — dcef1518 / Oregon #7 regression
#
# Bug: handle() called _publish_products_written_metric immediately after
# start_build() returned.  start_build() is fire-and-forget; it returns a
# build ID with no waiting.  Products are written by the CodeBuild job
# several minutes later, so the products/ prefix was always empty at the
# moment the Lambda measured it → ProductsWritten always published as 0.
#
# Fix: ProductsWritten is now emitted from the buildspec post_build phase,
# after `aws s3 sync`, so it reflects the actual files uploaded.  The Lambda
# no longer calls put_metric_data.
#
# Tests:
#   (a) REGRESSION — Lambda must NOT call put_metric_data at dispatch time.
#       This test FAILS against the old buggy code.
#   (b) SATDUMP_BUILDSPEC structural contract — put-metric-data appears in
#       post_build AFTER the s3 sync upload.
#   (c) NASA_BUILDSPEC same structural contract.
#   (d) NoVisualizableDataError fast-exit: no build, no metric call.
# ---------------------------------------------------------------------------


class TestProductsWrittenMetric:
    """ProductsWritten metric lifecycle: emitted by buildspec, not by Lambda."""

    CONTACT_ID = "ba2c5446"
    CONTACT_DATE = "2026/08/31"
    BUCKET = "test-sdr-bucket"

    def _seed_s3_for_dispatch(self, s3) -> None:
        """Seed only the input objects needed for path detection — no products/.

        Reflects the real state at dispatch time: the CodeBuild build has not
        yet run, so products/ is empty.  The old tests seeded products/ BEFORE
        handle() and thus encoded the wrong lifecycle assumption.
        """
        s3.create_bucket(Bucket=self.BUCKET)
        # SatDump composite so _detect_path returns "satdump"
        s3.put_object(
            Bucket=self.BUCKET,
            Key=(
                f"contacts/{self.CONTACT_DATE}/{self.CONTACT_ID}"
                f"/satdump/viirs_rgb_True_Color.png"
            ),
            Body=b"fake-composite",
        )
        # No products/ objects — correct state at dispatch time.

    def _orchestrator(self, s3, mock_cw):
        return handler.VisualizationOrchestrator(
            s3_client=s3,
            codebuild_client=FakeCodeBuild(),
            cloudwatch_client=mock_cw,
            input_bucket=self.BUCKET,
            codebuild_project="project",
            enable_geotiff="false",
        )

    def _event(self) -> dict:
        return {
            "bucket": self.BUCKET,
            "key": (
                f"contacts/{self.CONTACT_DATE}/{self.CONTACT_ID}/manifest.json"
            ),
        }

    @mock_aws
    def test_lambda_does_not_publish_products_written_at_dispatch(self):
        """REGRESSION: handle() must NOT call put_metric_data at dispatch time.

        The Lambda dispatches the build with start_build() (fire-and-forget)
        and returns immediately.  Products are written by the build minutes
        later.  Calling put_metric_data from handle() always produced count=0
        because the products/ prefix is empty at that moment (dcef1518 bug).

        The metric is now emitted from the buildspec post_build phase.

        This test FAILS against the old (buggy) code that called
        _publish_products_written_metric immediately after _submit_codebuild.
        """
        s3 = boto3.client("s3", region_name="us-east-1")
        self._seed_s3_for_dispatch(s3)
        mock_cw = MagicMock()

        result = self._orchestrator(s3, mock_cw).handle(self._event())

        assert result["build_id"] is not None, "CodeBuild must be dispatched"
        # Regression gate — Lambda must NOT publish at dispatch time
        mock_cw.put_metric_data.assert_not_called()

    def test_satdump_buildspec_emits_products_written_after_sync(self):
        """SATDUMP_BUILDSPEC: put-metric-data must appear in post_build AFTER
        the aws s3 sync upload so the count reflects actual uploaded files."""
        spec = handler.SATDUMP_BUILDSPEC
        upload_pos = spec.find("aws s3 sync /tmp/output/")
        metric_pos = spec.find("put-metric-data")
        assert upload_pos != -1, "SATDUMP_BUILDSPEC must contain the upload sync"
        assert metric_pos != -1, (
            "SATDUMP_BUILDSPEC must contain 'put-metric-data' — metric must be "
            "emitted from the build, not from the Lambda"
        )
        assert metric_pos > upload_pos, (
            "put-metric-data must appear AFTER the s3 sync so it counts "
            "products that have already been uploaded, not an empty prefix"
        )
        region = spec[metric_pos : metric_pos + 300]
        assert "SDRPipeline" in region, "must target the SDRPipeline namespace"
        assert "ProductsWritten" in region, "MetricName must be ProductsWritten"
        assert "ContactId" in region, "must include the ContactId dimension"

    def test_nasa_buildspec_emits_products_written_after_sync(self):
        """NASA_BUILDSPEC: same post_build structural contract as SatDump."""
        spec = handler.NASA_BUILDSPEC
        upload_pos = spec.find("aws s3 sync /tmp/output/")
        metric_pos = spec.find("put-metric-data")
        assert upload_pos != -1, "NASA_BUILDSPEC must contain the upload sync"
        assert metric_pos != -1, (
            "NASA_BUILDSPEC must contain 'put-metric-data' — metric must be "
            "emitted from the build, not from the Lambda"
        )
        assert metric_pos > upload_pos, (
            "put-metric-data must appear AFTER the s3 sync"
        )
        region = spec[metric_pos : metric_pos + 300]
        assert "SDRPipeline" in region
        assert "ProductsWritten" in region
        assert "ContactId" in region

    @mock_aws
    def test_no_metric_when_no_visualizable_data(self):
        """NoVisualizableDataError: handle() returns early without dispatching
        a build, so the buildspec never runs.  put_metric_data must not be
        called at Lambda level either (no change from previous behaviour)."""
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=self.BUCKET)
        # Only a manifest — no VIIRS composites or SDR HDF5 files
        s3.put_object(
            Bucket=self.BUCKET,
            Key=f"contacts/{self.CONTACT_DATE}/{self.CONTACT_ID}/manifest.json",
            Body=b"{}",
        )

        mock_cw = MagicMock()
        result = self._orchestrator(s3, mock_cw).handle(self._event())

        assert result["build_id"] is None
        mock_cw.put_metric_data.assert_not_called()
