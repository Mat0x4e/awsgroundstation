"""Receiver Arm Lambda for the synchronous NOAA-20 pipeline.

Starts the pre-provisioned sync receiver EC2 instance, waits for it to reach
running state AND for its SSM agent to register as Online, then issues an SSM
Run Command to arm the live-UDP listener in the background BEFORE AOS. Invoked
directly by an EventBridge PREPASS rule (contactStatus PREPASS), not by Step
Functions -- there is no S3 buffer in this path, so the listener must already
be bound and ready the moment Ground Station starts streaming to the dataflow
endpoint.

The SSM-online wait matters: EC2 "running" only means the instance has booted,
not that the SSM agent has registered yet (scripts/deploy_aggregation.sh hits
this same gap and explicitly polls for it -- "the SSM agent registers a little
after the instance reports running"). Skipping this wait risks send_command
failing with InvalidInstanceId right when it matters most: moments before AOS,
with no S3 buffer to fall back on if the arm command is lost.

Terraform-side config: Python 3.12 runtime, 256 MB memory, 420s timeout (see
lambda.tf and variables.tf's contact_pre_pass_duration_seconds for the timing
budget this is sized against).
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INSTANCE_ID = os.environ["RECEIVER_INSTANCE_ID"]
UDP_PORT = os.environ["RECEIVER_UDP_PORT"]
SCRIPT_PATH = "/opt/scripts/sync_receiver_arm.sh"
SSM_EXECUTION_TIMEOUT = 120  # arming is fire-and-forget (nohup'd listener); this just starts it
RUNNING_POLL_INTERVAL_SECONDS = 5
RUNNING_POLL_MAX_ATTEMPTS = 48  # 48 x 5s = 240s max
STOPPED_GRACE_SECONDS = 30  # see _wait_for_running: tolerate stale "stopped" after start
_FAILED_START_STATES = ("stopped", "stopping", "shutting-down", "terminated")
SSM_ONLINE_POLL_INTERVAL_SECONDS = 5
SSM_ONLINE_POLL_MAX_ATTEMPTS = 24  # 24 x 5s = 120s max


def lambda_handler(event, context):
    """Start the receiver EC2 instance and arm its UDP listener ahead of AOS.

    Expected event keys (from eventbridge.tf's input_transformer, sourced from
    the Ground Station Contact State Change PREPASS event detail):
      - bucket (str): S3 bucket to eventually upload this contact's data to
      - contact_id (str): Unique identifier for the Ground Station contact
      - contact_time (str): Event timestamp, e.g. "2026-09-06T11:47:59Z"

    contact_date is derived here (not by the EventBridge input_transformer,
    which substitutes whole values and cannot reformat a timestamp) as
    YYYY/MM/DD, matching the S3 layout convention used throughout the rest of
    this pipeline and modules/sdr_pipeline.

    Returns:
      dict with keys:
        - command_id (str): SSM Command ID
        - instance_id (str): EC2 instance ID
    """
    bucket = event["bucket"]
    contact_id = event["contact_id"]
    contact_date = event["contact_time"].split("T")[0].replace("-", "/")

    logger.info(
        json.dumps(
            {
                "action": "receiver_arm_start",
                "instance_id": INSTANCE_ID,
                "bucket": bucket,
                "contact_id": contact_id,
                "contact_date": contact_date,
            }
        )
    )

    ec2 = boto3.client("ec2")
    ssm = boto3.client("ssm")

    _start_instance(ec2, INSTANCE_ID)
    _wait_for_running(ec2, INSTANCE_ID)
    _wait_for_ssm_online(ssm, INSTANCE_ID)
    command_id = _send_command(ssm, INSTANCE_ID, bucket, contact_id, contact_date)

    logger.info(
        json.dumps(
            {
                "action": "receiver_arm_complete",
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
    left_stopped = False
    for attempt in range(RUNNING_POLL_MAX_ATTEMPTS):
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        instance = resp["Reservations"][0]["Instances"][0]
        state = instance["State"]["Name"]
        reason = instance.get("StateReason") or {}
        reason_code = reason.get("Code")
        reason_message = reason.get("Message")

        logger.info(
            json.dumps(
                {
                    "action": "instance_state_poll",
                    "instance_id": instance_id,
                    "state": state,
                    "state_reason_code": reason_code,
                    "attempt": attempt + 1,
                }
            )
        )

        if state == "running":
            return

        if state in ("pending", "running"):
            left_stopped = True

        # Fail fast, but only on evidence the start really failed.
        # Right after StartInstances, DescribeInstances is eventually
        # consistent and can still return the PREVIOUS "stopped" state with
        # the PREVIOUS StateReason (seen live 2026-11-23: a stale
        # Client.InvalidKMSKey from contact #8, 91 ms after a start that then
        # succeeded). So stopped/stopping only counts as a failure when:
        #   - the instance was already seen leaving stopped (pending ->
        #     stopped is a genuine failed start), or
        #   - it is still stopped after STOPPED_GRACE_SECONDS (a healthy
        #     start leaves stopped within a few seconds; 30 s is generous
        #     for EC2 eventual consistency yet small vs the 240 s budget).
        # shutting-down / terminated are never stale-start artefacts.
        elapsed = attempt * RUNNING_POLL_INTERVAL_SECONDS
        stale_possible = (
            state in ("stopped", "stopping")
            and not left_stopped
            and elapsed < STOPPED_GRACE_SECONDS
        )
        if state in _FAILED_START_STATES and not stale_possible:
            logger.error(
                json.dumps(
                    {
                        "action": "instance_start_failed",
                        "instance_id": instance_id,
                        "state": state,
                        "state_reason_code": reason_code,
                        "state_reason_message": reason_message,
                    }
                )
            )
            raise RuntimeError(
                f"Instance {instance_id} failed to start: state={state}, "
                f"StateReason code={reason_code}, message={reason_message}"
            )

        time.sleep(RUNNING_POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        f"Instance {instance_id} did not reach running state within timeout"
    )


def _wait_for_ssm_online(ssm, instance_id):
    """Poll DescribeInstanceInformation until the SSM agent reports Online.

    EC2 'running' only means the OS has booted -- the SSM agent registers a
    little after that (same gap scripts/deploy_aggregation.sh polls for out
    of band). Sending the arm command before the agent is Online fails with
    InvalidInstanceId, right before a pass with no way to retry in time.
    Raises RuntimeError on timeout.
    """
    for attempt in range(SSM_ONLINE_POLL_MAX_ATTEMPTS):
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        info_list = resp.get("InstanceInformationList", [])
        ping_status = info_list[0]["PingStatus"] if info_list else "Unregistered"

        logger.info(
            json.dumps(
                {
                    "action": "ssm_online_poll",
                    "instance_id": instance_id,
                    "ping_status": ping_status,
                    "attempt": attempt + 1,
                }
            )
        )

        if ping_status == "Online":
            return

        time.sleep(SSM_ONLINE_POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        f"SSM agent on instance {instance_id} did not report Online within timeout"
    )


def _send_command(ssm, instance_id, bucket, contact_id, contact_date):
    """Issue SSM Run Command that arms the live-UDP listener in the background.

    scripts/sync_receiver_arm.sh nohup's the listener process and returns
    immediately -- this command completing does not mean the pass is over,
    just that the listener is bound and ready. Step Functions later sends a
    separate "finish" command (post-LOS) to stop the listener and run
    RT-STPS/upload -- see step_functions.tf.
    """
    command_line = f"{SCRIPT_PATH} {bucket} {contact_id} {contact_date} {UDP_PORT}"

    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [command_line],
                "executionTimeout": [str(SSM_EXECUTION_TIMEOUT)],
            },
            TimeoutSeconds=SSM_EXECUTION_TIMEOUT,
            Comment=f"Arm sync receiver for contact {contact_id}",
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
