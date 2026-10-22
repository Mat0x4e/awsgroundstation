"""
VIIRS Visualization Orchestrator Lambda

Detects the visualization path (SatDump vs NASA) from files in the contact
folder and dispatches a render job on the compute selected by RENDER_COMPUTE:
  * "codebuild" (default) — the legacy inline-buildspec CodeBuild path.
  * "batch"               — an AWS Batch job running render_entrypoint.sh,
                            driven entirely by containerOverrides environment.

Strangler migration (Clean Compute Pipeline, ADR-002, Stage 3b): the Batch
path is added behind the RENDER_COMPUTE env flag while the CodeBuild path is
kept fully intact until the Batch path is validated (task d). Nothing in the
CodeBuild path changes.

Design reference: .kiro/specs/noaa20-viirs-visualization/design.md §1
Requirements: 11.1, 11.2, 11.3, 11.5
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Build spec inlines — the Lambda passes these to CodeBuild as overrides.
# The actual pipeline scripts live inside the Docker image at
# /opt/scripts/viirs/ (the Dockerfile does COPY scripts/ /opt/scripts/);
# these buildspecs just orchestrate the download → render → upload workflow.
# ---------------------------------------------------------------------------

# RETAINED FOR THE LEGACY CODEBUILD STRANGLER PATH ONLY. The render logic has
# been relocated to scripts/viirs/render_entrypoint.sh (RENDER_BRANCH=satdump),
# which the AWS Batch path (RENDER_COMPUTE=batch) uses. This inline buildspec is
# kept because the still-active CodeBuild dispatch and its structural-contract
# tests reference it; it will be removed when the CodeBuild project is retired
# (task 13), after the Batch path is validated. Do not delete it in this change.
SATDUMP_BUILDSPEC = """version: 0.2

env:
  variables:
    MPLBACKEND: "Agg"

phases:
  pre_build:
    commands:
      - echo "Downloading SatDump outputs from S3..."
      - mkdir -p /tmp/input/chunks /tmp/input/satdump /tmp/input/coordinates /tmp/output
      # SatDump writes one composite set per 30 s chunk, under
      # <prefix>/satdump/chunk_N/<INSTRUMENT>/. Pull the VIIRS ones only: a
      # contact holds ~9,000 PNGs across all instruments and the visualizer
      # reads ~36 of them.
      - aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/satdump/" /tmp/input/chunks/
          --exclude "*"
          --include "*/VIIRS/*.png"
          --include "*/VIIRS/*.cbor"
          --include "*/VIIRS/*.georef"
          --include "*/VIIRS/*_projected.tif"
      # SatDumpVisualizer.discover_composites globs its input dir non-recursively,
      # so flatten the per-chunk folders into it. Lowest chunk number wins each
      # name: chunk_0 is the first 30 s after AOS, the southernmost part of the
      # swath. This is the layout every successful manual run used (.build/).
      - for d in $(ls -d /tmp/input/chunks/chunk_* 2>/dev/null | sort -V); do cp -n "$d"/VIIRS/* /tmp/input/satdump/ 2>/dev/null || true; done
      - echo "Composites staged:" && ls /tmp/input/satdump | head -20
      - aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/coordinates/" /tmp/input/coordinates/

  build:
    commands:
      - echo "Running SatDump visualization pipeline..."
      - python3 /opt/scripts/viirs/visualize_satdump.py
          --input-dir /tmp/input/satdump
          --coordinates-dir /tmp/input/coordinates
          --output-dir /tmp/output
          --contact-id "${CONTACT_ID}"
          --contact-date "${CONTACT_DATE}"__CONTACT_TIME_ARG____PASS_DURATION_ARG__
          --enable-geotiff "${ENABLE_GEOTIFF}"

  post_build:
    commands:
      - echo "Uploading products to S3..."
      - aws s3 sync /tmp/output/
          "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/"
          --sse aws:kms --sse-kms-key-id "${KMS_KEY_ID}"
      # DISPATCH-VS-COMPLETION: products are created here, by this build, not
      # by the Lambda orchestrator.  The Lambda fires start_build() and returns
      # immediately -- the products/ prefix is empty at dispatch time.  Emit
      # ProductsWritten now, after the sync, so the metric reflects what was
      # actually uploaded.  Requires cloudwatch:PutMetricData (namespace
      # SDRPipeline) on the CodeBuild service role (iam.tf).
      - PRODUCT_COUNT=$(aws s3 ls "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/" --recursive 2>/dev/null | wc -l | tr -d ' ')
      - aws cloudwatch put-metric-data --namespace SDRPipeline --metric-name ProductsWritten --unit Count --value "${PRODUCT_COUNT}" --dimensions "Name=ContactId,Value=${CONTACT_ID}" || echo "WARNING:ProductsWritten metric publish failed (non-fatal)"
      - echo "ProductsWritten metric published: contact_id=${CONTACT_ID} count=${PRODUCT_COUNT}"
      - echo "Visualization complete"
"""

# RETAINED FOR THE LEGACY CODEBUILD STRANGLER PATH ONLY. The render logic has
# been relocated to scripts/viirs/render_entrypoint.sh (RENDER_BRANCH=nasa),
# which the AWS Batch path (RENDER_COMPUTE=batch) uses. This inline buildspec is
# kept because the still-active CodeBuild dispatch and its structural-contract
# tests reference it; it will be removed when the CodeBuild project is retired
# (task 13), after the Batch path is validated. Do not delete it in this change.
NASA_BUILDSPEC = """version: 0.2

env:
  variables:
    MPLBACKEND: "Agg"
    ENABLE_DESTRIPE: "true"

phases:
  pre_build:
    commands:
      - echo "Downloading SDR + GEO files from S3..."
      - mkdir -p /tmp/input/sdr /tmp/output
      # aggregation.sh uploads CSPP output to <prefix>/sdr/. <prefix>/chunks/
      # holds per-chunk RT-STPS metadata only -- no .h5 ever lands there.
      # Both the terrain-corrected names CSPP writes for J01 (SVM15/GITCO/GMTCO)
      # and the older ellipsoid ones (SVOM15/GIGTO/GMODO) are pulled, matching
      # what scripts/viirs/visualize_nasa.py accepts.
      - aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/sdr/" /tmp/input/sdr/
          --exclude "*"
          --include "SVI0*.h5"
          --include "SVM15*.h5"
          --include "SVOM15*.h5"
          --include "GITCO*.h5"
          --include "GIGTO*.h5"
          --include "GMTCO*.h5"
          --include "GMODO*.h5"

  build:
    commands:
      - echo "Running NASA visualization pipeline..."
      - python3 /opt/scripts/viirs/visualize_nasa.py
          --input-dir /tmp/input/sdr
          --output-dir /tmp/output
          --contact-id "${CONTACT_ID}"
          --contact-date "${CONTACT_DATE}"
          --enable-geotiff "${ENABLE_GEOTIFF}"
          --enable-destripe "${ENABLE_DESTRIPE}"

  post_build:
    commands:
      - echo "Uploading products to S3..."
      - aws s3 sync /tmp/output/
          "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/"
          --sse aws:kms --sse-kms-key-id "${KMS_KEY_ID}"
      # DISPATCH-VS-COMPLETION: products are created here, by this build, not
      # by the Lambda orchestrator.  The Lambda fires start_build() and returns
      # immediately -- the products/ prefix is empty at dispatch time.  Emit
      # ProductsWritten now, after the sync, so the metric reflects what was
      # actually uploaded.  Requires cloudwatch:PutMetricData (namespace
      # SDRPipeline) on the CodeBuild service role (iam.tf).
      - PRODUCT_COUNT=$(aws s3 ls "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/" --recursive 2>/dev/null | wc -l | tr -d ' ')
      - aws cloudwatch put-metric-data --namespace SDRPipeline --metric-name ProductsWritten --unit Count --value "${PRODUCT_COUNT}" --dimensions "Name=ContactId,Value=${CONTACT_ID}" || echo "WARNING:ProductsWritten metric publish failed (non-fatal)"
      - echo "ProductsWritten metric published: contact_id=${CONTACT_ID} count=${PRODUCT_COUNT}"
      - echo "Visualization complete"
"""


class NoVisualizableDataError(Exception):
    """Raised when no recognizable VIIRS data is found in the contact folder."""


class VisualizationOrchestrator:
    """Detects visualization path and dispatches a render job.

    The render compute is selected by RENDER_COMPUTE (strangler switch):
        "codebuild" (default) — legacy inline-buildspec CodeBuild build.
        "batch"               — AWS Batch job running render_entrypoint.sh.

    Environment variables consumed:
        INPUT_BUCKET          — SDR output bucket name (required)
        RENDER_COMPUTE        — "codebuild" (default) or "batch"
        ENABLE_GEOTIFF        — "true" or "false"
        ENABLE_DESTRIPE       — "true" or "false" (nasa branch; always passed to Batch)
        KMS_KEY_ID            — CMK ARN / key id for SSE-KMS uploads (Batch path)
        CODEBUILD_PROJECT     — CodeBuild project name (CodeBuild path only; optional
                                so a batch-only deployment that omits it still builds)
        BATCH_JOB_QUEUE       — Batch job queue (Batch path)
        BATCH_JOB_DEFINITION  — Batch job definition (Batch path)
    """

    # Glob patterns matched against the basename of each S3 key.
    # SatDump composites (PNG) take priority when both path types are present.
    SATDUMP_PATTERNS = [
        "viirs_rgb_*.png",
        "viirs_*_Thermal_IR_*.png",
    ]
    # Satellite token is _j01_ for NOAA-20 and _npp_ for Suomi-NPP; CSPP writes
    # SVM15 for J01 where older runs wrote SVOM15.
    NASA_PATTERNS = [
        "SVI0*.h5",
        "SVM15*.h5",
        "SVOM15*.h5",
    ]

    def __init__(
        self,
        s3_client=None,
        codebuild_client=None,
        cloudwatch_client=None,
        batch_client=None,
        input_bucket: Optional[str] = None,
        codebuild_project: Optional[str] = None,
        enable_geotiff: Optional[str] = None,
        enable_destripe: Optional[str] = None,
        kms_key_id: Optional[str] = None,
        render_compute: Optional[str] = None,
        batch_job_queue: Optional[str] = None,
        batch_job_definition: Optional[str] = None,
    ) -> None:
        self._s3 = s3_client or boto3.client("s3")
        self._cb = codebuild_client or boto3.client("codebuild")
        self._cw = cloudwatch_client or boto3.client("cloudwatch")
        self._batch = batch_client or boto3.client("batch")
        self._input_bucket = input_bucket or os.environ["INPUT_BUCKET"]
        # Tolerant resolution: a batch-only deployment may omit CODEBUILD_PROJECT,
        # so default to "" instead of crashing at construction. The CodeBuild path
        # still works when the var is set; the Batch path never reads it.
        self._codebuild_project = codebuild_project or os.environ.get("CODEBUILD_PROJECT", "")
        self._enable_geotiff = (enable_geotiff or os.environ.get("ENABLE_GEOTIFF", "false")).lower()
        self._enable_destripe = (enable_destripe or os.environ.get("ENABLE_DESTRIPE", "true")).lower()
        # KMS_KEY_ID defaults to "" — the CodeBuild path never read it from the
        # Lambda (the buildspec takes it from its own env), so an empty default
        # keeps the CodeBuild path's behaviour identical. The Batch path passes it
        # through to render_entrypoint.sh (which requires it on both branches).
        self._kms_key_id = kms_key_id if kms_key_id is not None else os.environ.get("KMS_KEY_ID", "")
        # Strangler switch: "codebuild" (default) or "batch".
        self._render_compute = (render_compute or os.environ.get("RENDER_COMPUTE", "codebuild")).lower()
        self._batch_job_queue = batch_job_queue if batch_job_queue is not None else os.environ.get("BATCH_JOB_QUEUE", "")
        self._batch_job_definition = (
            batch_job_definition if batch_job_definition is not None else os.environ.get("BATCH_JOB_DEFINITION", "")
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle(self, event: dict) -> dict:
        """EventBridge event handler.

        Receives: { "bucket": str, "key": str }
        Returns:  { "build_id": str, "path": "satdump"|"nasa", "contact_id": str }
        """
        bucket: str = event["bucket"]
        key: str = event["key"]

        logger.info("Received event: bucket=%s key=%s", bucket, key)

        contact_id, contact_date = self._parse_key(key)
        input_prefix = f"contacts/{contact_date}/{contact_id}"

        logger.info(
            "Parsed key: contact_id=%s contact_date=%s prefix=%s",
            contact_id,
            contact_date,
            input_prefix,
        )

        s3_keys = self._list_contact_objects(bucket, input_prefix)
        logger.info("Found %d objects under %s", len(s3_keys), input_prefix)

        try:
            path = self._detect_path(s3_keys)
        except NoVisualizableDataError:
            logger.warning(
                "No visualizable data found for contact %s (prefix=%s). "
                "Files present: %s. Skipping.",
                contact_id,
                input_prefix,
                json.dumps(s3_keys),
            )
            return {"build_id": None, "path": None, "contact_id": contact_id}

        logger.info("Detected visualization path: %s", path)

        # SatDump-only: the NASA path reads true geolocation out of the GEO
        # HDF5 (GITCO/GMTCO lat/lon arrays) and needs no acquisition time.
        contact_time, chunk_duration = (
            self._lookup_acquisition(bucket, s3_keys)
            if path == "satdump"
            else (None, None)
        )

        # Strangler switch: dispatch to AWS Batch when RENDER_COMPUTE=batch,
        # otherwise keep the legacy CodeBuild path exactly as-is. Both are
        # fire-and-forget; build_id carries the CodeBuild build id or the Batch
        # jobId (same field — downstream SFN ResultPath depends on the shape).
        try:
            if self._render_compute == "batch":
                build_id = self._submit_batch(
                    path=path,
                    contact_id=contact_id,
                    contact_date=contact_date,
                    input_prefix=input_prefix,
                    contact_time=contact_time,
                    chunk_duration=chunk_duration,
                )
            else:
                build_id = self._submit_codebuild(
                    path=path,
                    contact_id=contact_id,
                    contact_date=contact_date,
                    input_prefix=input_prefix,
                    contact_time=contact_time,
                    chunk_duration=chunk_duration,
                )
        except ClientError as exc:
            logger.error(
                "Render dispatch failed: compute=%s contact_id=%s path=%s "
                "input_files=%s error=%s",
                self._render_compute,
                contact_id,
                path,
                json.dumps(s3_keys),
                str(exc),
            )
            raise

        logger.info(
            "Dispatched render job: id=%s path=%s compute=%s",
            build_id,
            path,
            self._render_compute,
        )
        # NOTE: ProductsWritten is NOT published here.  The Lambda dispatches the
        # render job (start_build / submit_job) and returns immediately; products
        # are written by that job several minutes later.  The metric is emitted
        # from the render job itself, after `aws s3 sync`, so it reflects the
        # actual files uploaded.  See SATDUMP_BUILDSPEC / NASA_BUILDSPEC (CodeBuild)
        # and scripts/viirs/render_entrypoint.sh post_build (Batch).
        return {"build_id": build_id, "path": path, "contact_id": contact_id}

    # ------------------------------------------------------------------
    # Key parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_key(key: str) -> tuple[str, str]:
        """Extract contact_id and contact_date (YYYY/MM/DD) from an S3 key.

        Supported formats:
            contacts/YYYY/MM/DD/{contact_id}/manifest.json
            contacts/YYYY/MM/DD/{contact_id}/satdump/viirs_rgb_True_Color.png
            contacts/YYYY/MM/DD/{contact_id}/           (bare prefix)

        Returns:
            (contact_id, "YYYY/MM/DD")
        """
        parts = key.lstrip("/").split("/")
        # Expected layout: contacts / YYYY / MM / DD / contact_id / ...
        if len(parts) < 5 or parts[0] != "contacts":
            raise ValueError(
                f"Cannot parse contact_id / contact_date from S3 key: {key!r}. "
                "Expected format: contacts/YYYY/MM/DD/<contact_id>/..."
            )
        year, month, day, contact_id = parts[1], parts[2], parts[3], parts[4]
        contact_date = f"{year}/{month}/{day}"
        return contact_id, contact_date

    # ------------------------------------------------------------------
    # S3 listing
    # ------------------------------------------------------------------

    def _list_contact_objects(self, bucket: str, prefix: str) -> list[str]:
        """Return all S3 keys under *prefix* (relative key strings)."""
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    # ------------------------------------------------------------------
    # Path detection
    # ------------------------------------------------------------------

    def _detect_path(self, s3_keys: list[str]) -> str:
        """Determine visualization path from the set of S3 keys.

        Returns "satdump" or "nasa". SatDump takes priority if both are
        present. Raises NoVisualizableDataError if neither pattern matches.
        """
        basenames = [k.split("/")[-1] for k in s3_keys]

        has_satdump = any(
            fnmatch.fnmatch(name, pattern)
            for name in basenames
            for pattern in self.SATDUMP_PATTERNS
        )
        if has_satdump:
            return "satdump"

        has_nasa = any(
            fnmatch.fnmatch(name, pattern)
            for name in basenames
            for pattern in self.NASA_PATTERNS
        )
        if has_nasa:
            return "nasa"

        raise NoVisualizableDataError(
            f"No SatDump or NASA patterns found in {len(s3_keys)} keys."
        )

    # ------------------------------------------------------------------
    # Acquisition time
    # ------------------------------------------------------------------

    # contacts/<date>/<id>/satdump/chunk_<N>/dataset.json
    _DATASET_KEY_RE = re.compile(r"/satdump/chunk_(\d+)/dataset\.json$")

    def _read_chunk_timestamp(self, bucket: str, key: str) -> Optional[datetime]:
        """Return the acquisition time recorded in one chunk's dataset.json."""
        try:
            body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            timestamp = json.loads(body)["timestamp"]
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (ClientError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Could not read acquisition time from %s: %s", key, exc)
            return None

    def _lookup_acquisition(
        self, bucket: str, s3_keys: list[str]
    ) -> tuple[Optional[str], Optional[float]]:
        """Return ("HH:MM:SS" UTC, chunk duration in seconds), either may be None.

        SatDump's product.cbor carries no timestamp, so visualize_satdump.py
        would otherwise fall back to midnight on --contact-date and propagate
        the TLE to the wrong point in the orbit -- a whole-continent error.
        SatDump does record the acquisition time per chunk in dataset.json.

        The lowest-numbered chunk is used, matching the buildspec's staging
        rule (lowest chunk wins each composite filename). The gap to the next
        chunk is the downlink cadence, which is also the along-track duration
        one composite covers -- the TLE propagation window. Measuring it beats
        assuming 30 s, and beats the 10 min whole-pass default that overstates
        a composite's box roughly twentyfold.

        Either value is None when it cannot be established; the corresponding
        flag is then left off the command line rather than passed wrong.
        """
        chunks: list[tuple[int, str]] = []
        for key in s3_keys:
            match = self._DATASET_KEY_RE.search(key)
            if match:
                chunks.append((int(match.group(1)), key))
        if not chunks:
            logger.warning("No satdump chunk dataset.json found -- no acquisition time")
            return None, None

        chunks.sort()
        first = self._read_chunk_timestamp(bucket, chunks[0][1])
        if first is None:
            return None, None

        duration: Optional[float] = None
        if len(chunks) > 1:
            second = self._read_chunk_timestamp(bucket, chunks[1][1])
            if second is not None:
                gap = (second - first).total_seconds()
                # Guard against out-of-order or duplicated chunk timestamps.
                if 0 < gap <= 600:
                    duration = gap
                else:
                    logger.warning("Implausible chunk cadence %.1f s -- ignoring", gap)

        logger.info(
            "Acquisition %s from %s, chunk cadence %s",
            first.isoformat(), chunks[0][1],
            f"{duration:.1f}s" if duration else "unknown",
        )
        return first.strftime("%H:%M:%S"), duration

    # ------------------------------------------------------------------
    # CloudWatch metrics
    # ------------------------------------------------------------------

    def _count_products(self, bucket: str, contact_date: str, contact_id: str) -> int:
        """Count S3 objects under products/{contact_date}/{contact_id}/.

        Uses a paginator per project boto3 convention — no manual NextToken loop.
        Products are written there by the visualization CodeBuild buildspec
        (aws s3 sync /tmp/output/ s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/).

        NOTE: No longer called from handle() at dispatch time — doing so always
        produced 0 because the products/ prefix is empty when the Lambda fires
        (dcef1518 / Oregon #7 regression).  ProductsWritten is now emitted from
        the buildspec post_build phase, after the sync.  Retained as a reference
        and for potential future use.
        """
        prefix = f"products/{contact_date}/{contact_id}/"
        count = 0
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            count += len(page.get("Contents", []))
        return count

    def _publish_products_written_metric(
        self, bucket: str, contact_date: str, contact_id: str
    ) -> None:
        """Count products and publish ProductsWritten to the SDRPipeline namespace.

        NOT called from handle() since the dcef1518 fix (Oregon #7 regression).
        The metric is now emitted from the buildspec post_build phase, after
        `aws s3 sync`, so it reflects actual uploaded files rather than the
        empty prefix seen at dispatch time.  Retained here as a reference.

        Defensive: a failure to publish must never fail the visualization path.
        Logs a WARNING and continues on any exception, consistent with the
        pipeline's 'products must survive' philosophy.

        Namespace SDRPipeline matches the namespace already permitted by the
        sdr_pipeline IAM conditions (cloudwatch:namespace StringEquals SDRPipeline).
        """
        try:
            count = self._count_products(bucket, contact_date, contact_id)
            self._cw.put_metric_data(
                Namespace="SDRPipeline",
                MetricData=[
                    {
                        "MetricName": "ProductsWritten",
                        "Dimensions": [{"Name": "ContactId", "Value": contact_id}],
                        "Value": float(count),
                        "Unit": "Count",
                    }
                ],
            )
            logger.info(
                "ProductsWritten metric published: contact_id=%s count=%d",
                contact_id,
                count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to publish ProductsWritten metric (non-fatal): "
                "contact_id=%s error=%s",
                contact_id,
                exc,
            )

    # ------------------------------------------------------------------
    # CodeBuild submission
    # ------------------------------------------------------------------

    def _submit_codebuild(
        self,
        path: str,
        contact_id: str,
        contact_date: str,
        input_prefix: str,
        contact_time: Optional[str] = None,
        chunk_duration: Optional[float] = None,
    ) -> str:
        """Start a CodeBuild build for the detected path.

        Returns the build ID string.
        """
        buildspec = SATDUMP_BUILDSPEC if path == "satdump" else NASA_BUILDSPEC

        # The flag is omitted entirely when the acquisition time is unknown:
        # an empty --contact-time fails to parse and would leave the run worse
        # off than the date-only fallback.
        buildspec = buildspec.replace(
            "__CONTACT_TIME_ARG__",
            f'\n          --contact-time "{contact_time}"' if contact_time else "",
        )
        # Likewise the propagation window: without a measured cadence the
        # script keeps its own default rather than being handed a guess.
        buildspec = buildspec.replace(
            "__PASS_DURATION_ARG__",
            f'\n          --pass-duration-seconds "{chunk_duration:.0f}"'
            if chunk_duration
            else "",
        )

        env_overrides = [
            {"name": "INPUT_PREFIX", "value": input_prefix, "type": "PLAINTEXT"},
            {"name": "CONTACT_ID", "value": contact_id, "type": "PLAINTEXT"},
            {"name": "CONTACT_DATE", "value": contact_date, "type": "PLAINTEXT"},
            {"name": "CONTACT_TIME", "value": contact_time or "", "type": "PLAINTEXT"},
            {
                "name": "CHUNK_DURATION_S",
                "value": f"{chunk_duration:.0f}" if chunk_duration else "",
                "type": "PLAINTEXT",
            },
            {"name": "VIZ_PATH", "value": path, "type": "PLAINTEXT"},
            {"name": "ENABLE_GEOTIFF", "value": self._enable_geotiff, "type": "PLAINTEXT"},
        ]

        response = self._cb.start_build(
            projectName=self._codebuild_project,
            buildspecOverride=buildspec,
            environmentVariablesOverride=env_overrides,
        )
        return response["build"]["id"]

    # ------------------------------------------------------------------
    # AWS Batch submission (strangler target)
    # ------------------------------------------------------------------

    # Batch job names allow only letters, digits, hyphen and underscore, up to
    # 128 chars, and must start with a letter or digit. Contact UUIDs are
    # hex+hyphen so already safe, but sanitise defensively against any id.
    _JOBNAME_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9_-]")

    @classmethod
    def _batch_job_name(cls, contact_id: str) -> str:
        """Derive a Batch-safe job name from *contact_id*.

        Replaces any character outside [A-Za-z0-9_-] with '-' and prefixes
        "render-" so the name always starts with an alphanumeric and never
        begins with a disallowed character. Truncated to Batch's 128-char limit.
        """
        sanitized = cls._JOBNAME_DISALLOWED_RE.sub("-", contact_id)
        return f"render-{sanitized}"[:128]

    def _submit_batch(
        self,
        path: str,
        contact_id: str,
        contact_date: str,
        input_prefix: str,
        contact_time: Optional[str] = None,
        chunk_duration: Optional[float] = None,
    ) -> str:
        """Submit an AWS Batch job running render_entrypoint.sh.

        Returns the Batch jobId string.

        All values are passed via containerOverrides["environment"] as
        [{"name":.., "value":..}] entries — NEVER via Batch Ref:: job-definition
        parameters. Ref:: placeholders resolve only inside the job definition's
        command[], not inside environment[], so an environment value of
        "Ref::foo" would reach the container verbatim (documented project
        gotcha). The env list sets exactly the vars render_entrypoint.sh reads.
        """
        environment = [
            {"name": "RENDER_BRANCH", "value": path},
            {"name": "INPUT_BUCKET", "value": self._input_bucket},
            {"name": "INPUT_PREFIX", "value": input_prefix},
            {"name": "CONTACT_ID", "value": contact_id},
            {"name": "CONTACT_DATE", "value": contact_date},
            {"name": "KMS_KEY_ID", "value": self._kms_key_id},
            {"name": "ENABLE_GEOTIFF", "value": self._enable_geotiff},
            # Always included so both branches are covered. render_entrypoint.sh
            # only requires it on the nasa branch, but passing it on satdump is
            # harmless (the satdump branch ignores it).
            {"name": "ENABLE_DESTRIPE", "value": self._enable_destripe},
            # SatDump-only optionals: empty string when unknown / on the nasa
            # path (nasa passes contact_time=None/chunk_duration=None). An empty
            # value makes render_entrypoint.sh skip the corresponding flag.
            {"name": "CONTACT_TIME", "value": contact_time or ""},
            {
                "name": "CHUNK_DURATION_S",
                "value": f"{chunk_duration:.0f}" if chunk_duration else "",
            },
        ]

        response = self._batch.submit_job(
            jobName=self._batch_job_name(contact_id),
            jobQueue=self._batch_job_queue,
            jobDefinition=self._batch_job_definition,
            containerOverrides={"environment": environment},
        )
        return response["jobId"]


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

_orchestrator: Optional[VisualizationOrchestrator] = None


def _get_orchestrator() -> VisualizationOrchestrator:
    """Return a module-level singleton (created once per container)."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = VisualizationOrchestrator()
    return _orchestrator


def lambda_handler(event: dict, context) -> dict:
    """EventBridge handler — delegates to VisualizationOrchestrator.

    Receives (via EventBridge input transformer):
        { "bucket": "<name>", "key": "<prefix/file>" }

    Returns:
        { "build_id": str | None, "path": str | None, "contact_id": str }
    """
    return _get_orchestrator().handle(event)
