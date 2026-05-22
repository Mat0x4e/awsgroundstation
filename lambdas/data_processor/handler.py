"""Data Processor Lambda for AWS Ground Station.

Processes CADU files received from SQS/S3 notifications.
Validates CADU frame structure, extracts metadata, uploads
processed output to a separate bucket with date-based prefix,
and publishes CloudWatch custom metrics.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
cloudwatch_client = boto3.client("cloudwatch")

CADU_SYNC_MARKER = b"\x1A\xCF\xFC\x1D"
CADU_FRAME_LENGTH = 1024
METRICS_NAMESPACE = "GroundStation/DataProcessor"


def handler(event, context):
    """Lambda entry point for processing SQS events containing S3 notifications."""
    output_bucket = os.environ["OUTPUT_BUCKET_NAME"]
    project_name = os.environ.get("PROJECT_NAME", "groundstation")

    results = []

    for record in event.get("Records", []):
        body = json.loads(record.get("body", "{}"))
        s3_records = body.get("Records", [])

        for s3_record in s3_records:
            result = process_s3_record(s3_record, output_bucket, project_name)
            results.append(result)

    logger.info(
        json.dumps(
            {
                "action": "processing_complete",
                "total_records": len(results),
                "successful": sum(1 for r in results if r["status"] == "success"),
                "failed": sum(1 for r in results if r["status"] == "error"),
            }
        )
    )

    return {"statusCode": 200, "body": json.dumps({"results": results})}


def process_s3_record(s3_record, output_bucket, project_name):
    """Process a single S3 record from the SQS event."""
    start_time = time.time()
    bucket = s3_record.get("s3", {}).get("bucket", {}).get("name", "")
    key = s3_record.get("s3", {}).get("object", {}).get("key", "")
    size = s3_record.get("s3", {}).get("object", {}).get("size", 0)

    logger.info(
        json.dumps(
            {
                "action": "processing_start",
                "bucket": bucket,
                "key": key,
                "size": size,
            }
        )
    )

    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        data = response["Body"].read()
        input_size = len(data)

        metadata = validate_and_extract_metadata(data)

        now = datetime.now(timezone.utc)
        output_key = (
            f"output/year={now.year}/month={now.month:02d}/"
            f"day={now.day:02d}/{os.path.basename(key)}.json"
        )

        output_data = json.dumps(
            {
                "source_bucket": bucket,
                "source_key": key,
                "processed_at": now.isoformat(),
                "metadata": metadata,
                "input_size_bytes": input_size,
            }
        )

        s3_client.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=output_data.encode("utf-8"),
            ContentType="application/json",
        )

        duration_ms = int((time.time() - start_time) * 1000)

        publish_metrics(project_name, duration_ms, input_size, "success")

        logger.info(
            json.dumps(
                {
                    "action": "processing_success",
                    "source_key": key,
                    "output_key": output_key,
                    "duration_ms": duration_ms,
                    "input_size_bytes": input_size,
                }
            )
        )

        return {"status": "success", "key": key, "output_key": output_key}

    except ClientError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        publish_metrics(project_name, duration_ms, size, "error")
        logger.error(
            json.dumps(
                {
                    "action": "processing_error",
                    "key": key,
                    "error": str(e),
                }
            )
        )
        return {"status": "error", "key": key, "error": str(e)}

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        publish_metrics(project_name, duration_ms, size, "error")
        logger.error(
            json.dumps(
                {
                    "action": "processing_error",
                    "key": key,
                    "error": str(e),
                }
            )
        )
        return {"status": "error", "key": key, "error": str(e)}


def validate_and_extract_metadata(data):
    """Validate CADU frame structure and extract metadata."""
    metadata = {
        "total_bytes": len(data),
        "valid_frames": 0,
        "invalid_frames": 0,
        "sync_markers_found": 0,
    }

    if len(data) < CADU_FRAME_LENGTH:
        metadata["validation_status"] = "too_short"
        return metadata

    offset = 0
    while offset + CADU_FRAME_LENGTH <= len(data):
        frame = data[offset : offset + CADU_FRAME_LENGTH]

        if frame[:4] == CADU_SYNC_MARKER:
            metadata["sync_markers_found"] += 1
            metadata["valid_frames"] += 1
        else:
            metadata["invalid_frames"] += 1

        offset += CADU_FRAME_LENGTH

    total_frames = metadata["valid_frames"] + metadata["invalid_frames"]
    if total_frames > 0:
        metadata["frame_sync_rate"] = metadata["valid_frames"] / total_frames
    else:
        metadata["frame_sync_rate"] = 0.0

    metadata["validation_status"] = "valid" if metadata["valid_frames"] > 0 else "no_sync"

    return metadata


def publish_metrics(project_name, duration_ms, input_size, status):
    """Publish custom CloudWatch metrics for processing."""
    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRICS_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "processing_duration_ms",
                    "Value": duration_ms,
                    "Unit": "Milliseconds",
                    "Dimensions": [
                        {"Name": "Project", "Value": project_name},
                        {"Name": "Status", "Value": status},
                    ],
                },
                {
                    "MetricName": "input_size_bytes",
                    "Value": input_size,
                    "Unit": "Bytes",
                    "Dimensions": [
                        {"Name": "Project", "Value": project_name},
                    ],
                },
                {
                    "MetricName": "status",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Project", "Value": project_name},
                        {"Name": "Status", "Value": status},
                    ],
                },
            ],
        )
    except ClientError as e:
        logger.error(
            json.dumps(
                {
                    "action": "publish_metrics_error",
                    "error": str(e),
                }
            )
        )
