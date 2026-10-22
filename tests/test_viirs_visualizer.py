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
import re
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


class FakeBatch:
    """Records submit_job kwargs and returns a canned jobId."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"jobId": "job-123"}


# Batch job names: letters, digits, hyphen, underscore; start with
# alphanumeric; max 128 chars.
_BATCH_JOBNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _orchestrator(objects=None, codebuild=None):
    return handler.VisualizationOrchestrator(
        s3_client=FakeS3(objects or {}),
        codebuild_client=codebuild or FakeCodeBuild(),
        input_bucket="bucket",
        codebuild_project="project",
        enable_geotiff="true",
    )


def _batch_orchestrator(objects=None, batch=None):
    """Build a batch-mode orchestrator (RENDER_COMPUTE=batch)."""
    return handler.VisualizationOrchestrator(
        s3_client=FakeS3(objects or {}),
        codebuild_client=FakeCodeBuild(),
        batch_client=batch or FakeBatch(),
        input_bucket="bucket",
        codebuild_project="project",
        render_compute="batch",
        batch_job_queue="q",
        batch_job_definition="d",
        kms_key_id="arn:kms:...",
        enable_geotiff="true",
        enable_destripe="true",
    )


def _env_map(submit_kwargs: dict) -> dict[str, str]:
    """Flatten containerOverrides.environment into a {name: value} dict."""
    env = submit_kwargs["containerOverrides"]["environment"]
    return {e["name"]: e["value"] for e in env}


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


# ---------------------------------------------------------------------------
# AWS Batch dispatch path — Stage 3b strangler swap (RENDER_COMPUTE=batch)
#
# The render logic now lives in scripts/viirs/render_entrypoint.sh, driven by
# RENDER_BRANCH=satdump|nasa. When RENDER_COMPUTE=batch, the Lambda submits a
# Batch job and passes every value render_entrypoint.sh reads through
# containerOverrides.environment — NEVER via Batch Ref:: job-definition
# parameters (Ref:: does not resolve inside environment[], only command[]).
#
# The CodeBuild path is unchanged and stays the default; all tests above that
# don't set render_compute exercise it.
# ---------------------------------------------------------------------------


def test_render_compute_defaults_to_codebuild():
    """Flag default: no RENDER_COMPUTE env / kwarg → the codebuild path.

    A dispatch in default mode must go through the CodeBuild client, not Batch.
    """
    cb = FakeCodeBuild()
    batch = FakeBatch()
    orch = handler.VisualizationOrchestrator(
        s3_client=FakeS3({}),
        codebuild_client=cb,
        batch_client=batch,
        input_bucket="bucket",
        codebuild_project="project",
        enable_geotiff="true",
    )

    assert orch._render_compute == "codebuild"

    orch._submit_codebuild(
        path="satdump",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time="11:59:57",
        chunk_duration=30.0,
    )
    assert len(cb.calls) == 1, "default mode must dispatch via CodeBuild"
    assert batch.calls == [], "default mode must not dispatch via Batch"


def test_batch_submit_satdump():
    """SatDump Batch submit: correct environment, queue/definition, no Ref::."""
    batch = FakeBatch()
    orch = _batch_orchestrator(batch=batch)

    job_id = orch._submit_batch(
        path="satdump",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time="11:59:57",
        chunk_duration=30.0,
    )

    assert job_id == "job-123"
    assert len(batch.calls) == 1, "submit_job must be called exactly once"
    call = batch.calls[0]

    # Queue / definition passed through from constructor.
    assert call["jobQueue"] == "q"
    assert call["jobDefinition"] == "d"

    env = _env_map(call)
    assert env["RENDER_BRANCH"] == "satdump"
    assert env["INPUT_BUCKET"] == "bucket"
    assert env["INPUT_PREFIX"] == PREFIX
    assert env["CONTACT_ID"] == "ba2c5446"
    assert env["CONTACT_DATE"] == "2026/08/31"
    assert env["KMS_KEY_ID"] == "arn:kms:..."
    assert env["ENABLE_GEOTIFF"] == "true"
    # SatDump optionals populated when provided.
    assert env["CONTACT_TIME"] == "11:59:57"
    assert env["CHUNK_DURATION_S"] == "30"

    # Never resolve values through Batch Ref:: job-definition parameters.
    for name, value in env.items():
        assert "Ref::" not in value, f"{name} must not contain a Ref:: placeholder"

    # jobName must satisfy the Batch charset.
    assert _BATCH_JOBNAME_RE.match(call["jobName"]), call["jobName"]

    # Values go via containerOverrides.environment as [{"name","value"}], never
    # via a top-level job-definition parameters map.
    assert "parameters" not in call
    for entry in call["containerOverrides"]["environment"]:
        assert set(entry) == {"name", "value"}


def test_batch_submit_nasa():
    """NASA Batch submit: RENDER_BRANCH=nasa, ENABLE_DESTRIPE present,
    CONTACT_TIME empty (nasa passes contact_time=None)."""
    batch = FakeBatch()
    orch = _batch_orchestrator(batch=batch)

    orch._submit_batch(
        path="nasa",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time=None,
        chunk_duration=None,
    )

    env = _env_map(batch.calls[0])
    assert env["RENDER_BRANCH"] == "nasa"
    assert "ENABLE_DESTRIPE" in env, "nasa branch requires ENABLE_DESTRIPE"
    assert env["ENABLE_DESTRIPE"] == "true"
    assert env["CONTACT_TIME"] == "", "nasa passes None → empty string"
    assert env["CHUNK_DURATION_S"] == ""


def test_batch_job_name_sanitisation():
    """A contact id with chars outside [A-Za-z0-9_-] yields a valid jobName."""
    batch = FakeBatch()
    orch = _batch_orchestrator(batch=batch)

    orch._submit_batch(
        path="nasa",
        contact_id="path/with:bad*chars",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
    )

    job_name = batch.calls[0]["jobName"]
    assert _BATCH_JOBNAME_RE.match(job_name), job_name
    # '/', ':' and '*' must be replaced, not passed through.
    assert "/" not in job_name
    assert ":" not in job_name
    assert "*" not in job_name


def test_batch_job_name_within_length_limit():
    """Even an over-long id must truncate to Batch's 128-char limit."""
    batch = FakeBatch()
    orch = _batch_orchestrator(batch=batch)

    orch._submit_batch(
        path="nasa",
        contact_id="z" * 200,
        contact_date="2026/08/31",
        input_prefix=PREFIX,
    )

    job_name = batch.calls[0]["jobName"]
    assert len(job_name) <= 128
    assert _BATCH_JOBNAME_RE.match(job_name), job_name


class TestBatchDispatchMetricContract:
    """Batch mode honours the same dispatch-vs-completion contract as CodeBuild:
    handle() must NOT publish ProductsWritten at dispatch time. The metric is
    emitted by render_entrypoint.sh post_build, after the s3 sync."""

    CONTACT_ID = "ba2c5446"
    CONTACT_DATE = "2026/08/31"
    BUCKET = "test-sdr-bucket"

    def _seed_s3_for_dispatch(self, s3) -> None:
        s3.create_bucket(Bucket=self.BUCKET)
        # SatDump composite so _detect_path returns "satdump".
        s3.put_object(
            Bucket=self.BUCKET,
            Key=(
                f"contacts/{self.CONTACT_DATE}/{self.CONTACT_ID}"
                f"/satdump/viirs_rgb_True_Color.png"
            ),
            Body=b"fake-composite",
        )

    def _event(self) -> dict:
        return {
            "bucket": self.BUCKET,
            "key": f"contacts/{self.CONTACT_DATE}/{self.CONTACT_ID}/manifest.json",
        }

    @mock_aws
    def test_batch_mode_does_not_publish_products_written_at_dispatch(self):
        """Mirror of the CodeBuild regression, but with render_compute="batch".

        moto provides S3 for seeding/path detection; Batch is faked so no real
        AWS Batch is needed.
        """
        s3 = boto3.client("s3", region_name="us-east-1")
        self._seed_s3_for_dispatch(s3)
        mock_cw = MagicMock()
        batch = FakeBatch()

        orch = handler.VisualizationOrchestrator(
            s3_client=s3,
            codebuild_client=FakeCodeBuild(),
            cloudwatch_client=mock_cw,
            batch_client=batch,
            input_bucket=self.BUCKET,
            codebuild_project="project",
            render_compute="batch",
            batch_job_queue="q",
            batch_job_definition="d",
            kms_key_id="arn:kms:...",
            enable_geotiff="false",
            enable_destripe="true",
        )
        result = orch.handle(self._event())

        # Dispatched via Batch — build_id carries the Batch jobId.
        assert result["build_id"] == "job-123"
        assert result["path"] == "satdump"
        assert len(batch.calls) == 1, "Batch must be dispatched"
        # Regression gate — Lambda must NOT publish at dispatch time.
        mock_cw.put_metric_data.assert_not_called()
