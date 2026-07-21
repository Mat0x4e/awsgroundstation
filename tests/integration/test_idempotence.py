"""Integration test for pipeline idempotence via Step Functions execution name.

**Validates: Requirements 6.6**

Deterministic companion to the property-based test in
tests/test_pipeline_idempotence.py. Uses a fixed contact_id to make
failures easy to reproduce and debug.
"""

import json
import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_REGION = "eu-central-1"
_STATE_MACHINE_NAME = "sdr-pipeline-integration-test"
_CONTACT_ID = "contact-2024-eu1-abc123"

_SIMPLE_ASL = json.dumps({
    "Comment": "Minimal state machine for idempotence integration testing",
    "StartAt": "Succeed",
    "States": {
        "Succeed": {
            "Type": "Succeed",
        },
    },
})


def test_duplicate_contact_id_raises_execution_already_exists():
    """Submitting the same contact_id twice raises ExecutionAlreadyExists on the second call.

    Verifies that Step Functions STANDARD workflows enforce unique execution
    names — the mechanism by which the pipeline guarantees idempotence when
    an EventBridge rule fires multiple times for the same contact.

    **Validates: Requirements 6.6**
    """
    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", _REGION)

        sfn = boto3.client(
            "stepfunctions",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            aws_session_token="testing",
        )

        # Create a STANDARD state machine
        state_machine_arn = sfn.create_state_machine(
            name=_STATE_MACHINE_NAME,
            definition=_SIMPLE_ASL,
            roleArn="arn:aws:iam::123456789012:role/sdr-pipeline-sfn-role",
            type="STANDARD",
        )["stateMachineArn"]

        # First submission — must succeed and return an execution ARN
        first_response = sfn.start_execution(
            stateMachineArn=state_machine_arn,
            name=_CONTACT_ID,
            input=json.dumps({"contact_id": _CONTACT_ID}),
        )
        assert first_response["executionArn"], (
            "First start_execution must return a non-empty executionArn"
        )

        # Second submission with the same name — must raise ExecutionAlreadyExists.
        # AWS (and moto) only raise when input differs or the execution has ended.
        # In practice, a duplicate EventBridge trigger may carry a different timestamp
        # or sequence number; we use a distinct input to exercise the error path.
        with pytest.raises(ClientError) as exc_info:
            sfn.start_execution(
                stateMachineArn=state_machine_arn,
                name=_CONTACT_ID,
                input=json.dumps({"contact_id": _CONTACT_ID, "attempt": 2}),
            )

        error_code = exc_info.value.response["Error"]["Code"]
        assert error_code == "ExecutionAlreadyExists", (
            f"Expected ExecutionAlreadyExists but got {error_code!r}"
        )
