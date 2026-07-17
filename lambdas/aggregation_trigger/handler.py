"""Aggregation Trigger Lambda for NOAA-20 SDR Pipeline.

Starts the pre-provisioned EC2 aggregation instance, waits for it to reach
running state, then issues an SSM Run Command to execute the aggregation script.

Terraform-side config: Python 3.12 runtime, 256 MB memory, 120s timeout.
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INSTANCE_ID = os.environ["AGGREGATION_INSTANCE_ID"]
SCRIPT_PATH = "/opt/scripts/aggregation.sh"
SSM_EXECUTION_TIMEOUT = 1800  # 30 minutes
RUNNING_POLL_INTERVAL_SECONDS = 5
RUNNING_POLL_MAX_ATTEMPTS = 12  # 12 × 5s = 60s max


def lambda_handler(event, context):
    """Start EC2 aggregation instance, send SSM Run Command, return command details.

    Expected event keys:
      - bucket (str): S3 bucket containing contact data
      - contact_id (str): Unique identifier for the Ground Station contact
      - contact_date (str): Contact date in YYYY-MM-DD format

    Returns:
      dict with keys:
        - command_id (str): SSM Command ID for polling by Step Functions
        - instance_id (str): EC2 instance ID
    """
    bucket = event["bucket"]
    contact_id = event["contact_id"]
    contact_date = event["contact_date"]

    logger.info(
        json.dumps(
            {
                "action": "aggregation_trigger_start",
                "instance_id": INSTANCE_ID,
                "bucket": bucket,
                "contact_id": contact_id,
                "contact_date": contact_date,
            }
        )
    )

    ec2 = boto3.client("ec2")
    ssm = boto3.client("ssm")

    # Start the aggregation instance
    _start_instance(ec2, INSTANCE_ID)

    # Wait until the instance reaches running state (max 60s)
    _wait_for_running(ec2, INSTANCE_ID)

    # Issue SSM Run Command
    command_id = _send_command(ssm, INSTANCE_ID, bucket, contact_id, contact_date)

    logger.info(
        json.dumps(
            {
                "action": "aggregation_trigger_complete",
                "instance_id": INSTANCE_ID,
                "command_id": command_id,
            }
        )
    )

    return {
        "command_id": command_id,
        "instance_id": INSTANCE_ID,
    }


def _start_instance(ec2, instance_id):
    """Send StartInstances request. Tolerates already-running state."""
    try:
        ec2.start_instances(InstanceIds=[instance_id])
        logger.info(
            json.dumps({"action": "instance_start_requested", "instance_id": instance_id})
        )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        # IncorrectInstanceState is raised when already running — not a fatal error
        if error_code == "IncorrectInstanceState":
            logger.info(
                json.dumps(
                    {
                        "action": "instance_already_running",
                        "instance_id": instance_id,
                    }
                )
            )
        else:
            logger.error(
                json.dumps(
                    {
                        "action": "instance_start_error",
                        "instance_id": instance_id,
                        "error": str(e),
                    }
                )
            )
            raise


def _wait_for_running(ec2, instance_id):
    """Poll DescribeInstances until instance is running. Raises RuntimeError on timeout."""
    for attempt in range(RUNNING_POLL_MAX_ATTEMPTS):
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]

        logger.info(
            json.dumps(
                {
                    "action": "instance_state_poll",
                    "instance_id": instance_id,
                    "state": state,
                    "attempt": attempt + 1,
                }
            )
        )

        if state == "running":
            return

        if state == "terminated":
            raise RuntimeError(
                f"Instance {instance_id} is in terminated state and cannot be started"
            )

        time.sleep(RUNNING_POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        f"Instance {instance_id} did not reach running state within timeout"
    )


def _send_command(ssm, instance_id, bucket, contact_id, contact_date):
    """Issue SSM Run Command with aggregation script and parameters.

    Returns the SSM Command ID.
    """
    command_line = f"{SCRIPT_PATH} {bucket} {contact_id} {contact_date}"

    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [command_line],
                "executionTimeout": [str(SSM_EXECUTION_TIMEOUT)],
            },
            TimeoutSeconds=SSM_EXECUTION_TIMEOUT,
            Comment=f"Aggregation for contact {contact_id}",
        )
    except ClientError as e:
        logger.error(
            json.dumps(
                {
                    "action": "ssm_send_command_error",
                    "instance_id": instance_id,
                    "error": str(e),
                }
            )
        )
        raise

    command_id = response["Command"]["CommandId"]
    logger.info(
        json.dumps(
            {
                "action": "ssm_command_sent",
                "instance_id": instance_id,
                "command_id": command_id,
                "command_line": command_line,
            }
        )
    )

    return command_id
