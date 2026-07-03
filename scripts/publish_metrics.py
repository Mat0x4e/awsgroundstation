"""CloudWatch Metrics Publisher for the NOAA-20 SDR Pipeline.

Reads a manifest.json produced by the DigIF-to-SDR pipeline and publishes
key processing metrics to CloudWatch under the SDRPipeline namespace.

Requirements satisfied:
  6.1 — Publish ContactProcessingDuration, ChunksProcessedSuccess/Failed, SDRFilesProduced
  6.2 — Publish per-stage average durations (extraction, SatDump, RT-STPS, CSPP)
  6.3 — CloudWatch publish failure must NOT abort the pipeline (exit 0 on CW error)
  6.4 — Unreadable/unparseable manifest exits with code 1

Usage:
    python publish_metrics.py <manifest.json>

Exit codes:
    0  metrics published successfully, or CloudWatch API call failed (non-fatal)
    1  manifest file could not be read or parsed
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

NAMESPACE = "SDRPipeline"


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def load_manifest(path: str) -> dict[str, Any]:
    """Read and parse the pipeline manifest JSON file.

    Args:
        path: Filesystem path to manifest.json.

    Returns:
        Parsed manifest as a dict.

    Raises:
        SystemExit(1): If the file cannot be read or is not valid JSON.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read manifest file %s: %s", path, exc)
        sys.exit(1)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Manifest file %s is not valid JSON: %s", path, exc)
        sys.exit(1)


def build_metric_data(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Construct CloudWatch MetricData entries from the manifest.

    Missing optional keys (e.g. the ``metrics`` sub-object) are skipped with
    a warning rather than raising an exception.

    Args:
        manifest: Parsed manifest dict.

    Returns:
        List of MetricDatum dicts ready for ``put_metric_data``.
    """
    contact_id = manifest.get("contact_id", "unknown")
    satellite = manifest.get("satellite", "unknown")

    dimensions = [
        {"Name": "ContactId", "Value": contact_id},
        {"Name": "Satellite", "Value": satellite},
    ]

    entries: list[dict[str, Any]] = []

    def _add(name: str, unit: str, value: float | int | None) -> None:
        if value is None:
            logger.warning("Metric %s: value is None, skipping", name)
            return
        entries.append(
            {
                "MetricName": name,
                "Dimensions": dimensions,
                "Unit": unit,
                "Value": float(value),
            }
        )

    # Core pipeline metrics
    _add("ContactProcessingDuration", "Seconds", manifest.get("total_duration_s"))
    _add("ChunksProcessedSuccess", "Count", manifest.get("successful_chunks"))

    failed_chunks = manifest.get("failed_chunks")
    if failed_chunks is not None:
        _add("ChunksProcessedFailed", "Count", len(failed_chunks))
    else:
        logger.warning("Metric ChunksProcessedFailed: 'failed_chunks' key absent, skipping")

    sdr_files = manifest.get("sdr_files")
    if sdr_files is not None:
        _add("SDRFilesProduced", "Count", len(sdr_files))
    else:
        logger.warning("Metric SDRFilesProduced: 'sdr_files' key absent, skipping")

    # Per-stage average durations (optional sub-object)
    metrics_obj = manifest.get("metrics")
    if metrics_obj is None:
        logger.warning(
            "'metrics' key absent from manifest — skipping all per-stage duration metrics"
        )
    else:
        _add("ExtractionAvgDuration", "Seconds", metrics_obj.get("extraction_avg_s"))
        _add("SatDumpAvgDuration", "Seconds", metrics_obj.get("satdump_avg_s"))
        _add("RTSTPSAvgDuration", "Seconds", metrics_obj.get("rtstps_avg_s"))
        _add("CSPPAvgDuration", "Seconds", metrics_obj.get("cspp_avg_s"))

    return entries


# ---------------------------------------------------------------------------
# CloudWatch publisher
# ---------------------------------------------------------------------------


def publish_metrics(metric_data: list[dict[str, Any]], region: str | None = None) -> bool:
    """Publish MetricData to CloudWatch.

    Logs a WARNING on any boto3 ClientError and returns False rather than
    raising — publishing failure must not break the pipeline.

    Args:
        metric_data: List of MetricDatum dicts.
        region: AWS region override; uses the default boto3 region if None.

    Returns:
        True on success, False if CloudWatch returned an error.
    """
    if not metric_data:
        logger.warning("No metrics to publish — skipping put_metric_data call")
        return True

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region

    cw = boto3.client("cloudwatch", **kwargs)

    logger.info(
        "Publishing %d metric(s) to CloudWatch namespace '%s'",
        len(metric_data),
        NAMESPACE,
    )

    try:
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)
        logger.info("CloudWatch put_metric_data succeeded")
        return True
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.warning(
            "CloudWatch put_metric_data failed (continuing): [%s] %s",
            error_code,
            error_msg,
        )
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish NOAA-20 SDR pipeline metrics from a manifest.json to CloudWatch."
    )
    parser.add_argument(
        "manifest",
        metavar="MANIFEST",
        help="Path to the pipeline manifest.json file",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region for CloudWatch (defaults to boto3 session region)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    logger.info("Loading manifest: %s", args.manifest)
    manifest = load_manifest(args.manifest)

    contact_id = manifest.get("contact_id", "<unknown>")
    satellite = manifest.get("satellite", "<unknown>")
    logger.info("Contact: %s  Satellite: %s", contact_id, satellite)

    metric_data = build_metric_data(manifest)
    logger.debug("Built %d metric entries", len(metric_data))

    publish_metrics(metric_data, region=args.region)
    # Always exit 0 — CloudWatch publish failure is non-fatal (see req 6.3)
    sys.exit(0)


if __name__ == "__main__":
    main()
