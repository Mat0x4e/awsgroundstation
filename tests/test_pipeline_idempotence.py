"""Property-based tests for pipeline idempotence via Step Functions execution names.

**Validates: Requirements 6.6**

Property 9 — Pipeline idempotence via execution name:
    For any contact_id and submission count N >= 1, Step Functions SHALL:
    - Accept the first start_execution call with name=contact_id
    - Reject all subsequent start_execution calls with the same name
      via ExecutionAlreadyExists
    - Result in exactly one execution per contact_id

The idempotence guarantee is provided by Step Functions STANDARD workflows:
duplicate execution names are rejected within the 90-day retention window.
The EventBridge rule sets execution name = contact_id when starting the
state machine (defined in modules/sdr_pipeline/step_functions.tf).
"""

import json
import os

import boto3
import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid Step Functions execution name characters: alphanumeric, hyphens, underscores
# Max length: 80 characters
_execution_name_chars = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

_contact_id_strategy = st.text(
    alphabet=_execution_name_chars,
    min_size=1,
    max_size=80,
)

# Submission count: at least 1 (the first) plus 0 or more duplicates.
# We cap at 5 to keep tests fast; the property holds for any N >= 1.
_submission_count_strategy = st.integers(min_value=1, max_value=5)


@st.composite
def contact_and_submissions(draw):
    """Generate a (contact_id, n_submissions) pair.

    n_submissions is the total number of times the pipeline trigger fires
    for this contact (the first succeeds; n-1 are duplicates).
    """
    contact_id = draw(_contact_id_strategy)
    n = draw(_submission_count_strategy)
    return contact_id, n


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_REGION = "eu-central-1"
_STATE_MACHINE_NAME = "sdr-pipeline-test"

# Minimal valid ASL definition for a STANDARD state machine
_SIMPLE_ASL = json.dumps({
    "Comment": "Minimal state machine for idempotence testing",
    "StartAt": "Succeed",
    "States": {
        "Succeed": {
            "Type": "Succeed",
        },
    },
})


def _create_state_machine(sfn_client: object) -> str:
    """Create a STANDARD state machine and return its ARN."""
    # IAM role ARN — moto accepts any syntactically valid ARN
    role_arn = "arn:aws:iam::123456789012:role/sdr-pipeline-sfn-role"

    response = sfn_client.create_state_machine(
        name=_STATE_MACHINE_NAME,
        definition=_SIMPLE_ASL,
        roleArn=role_arn,
        type="STANDARD",  # STANDARD workflows enforce unique execution names
    )
    return response["stateMachineArn"]


# ---------------------------------------------------------------------------
# Property 9: Pipeline idempotence via execution name
# ---------------------------------------------------------------------------

@given(params=contact_and_submissions())
@settings(max_examples=100, deadline=None)
def test_pipeline_idempotence_via_execution_name(params) -> None:
    """Step Functions STANDARD workflows reject duplicate execution names,
    guaranteeing that a contact_id triggers at most one pipeline execution
    regardless of how many times the EventBridge rule fires.

    For any (contact_id, N):
      - The first start_execution with name=contact_id MUST succeed.
      - Each of the N-1 subsequent calls MUST raise ExecutionAlreadyExists.
      - list_executions MUST show exactly one execution for the state machine.

    **Validates: Requirements 6.6**
    """
    contact_id, n_submissions = params

    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", _REGION)

        sfn = boto3.client(
            "stepfunctions",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            aws_session_token="testing",
        )
        state_machine_arn = _create_state_machine(sfn)

        # --- First submission: MUST succeed ---
        first_response = sfn.start_execution(
            stateMachineArn=state_machine_arn,
            name=contact_id,
            input=json.dumps({"contact_id": contact_id}),
        )
        first_execution_arn = first_response["executionArn"]
        assert first_execution_arn, (
            f"First start_execution for contact_id={contact_id!r} returned no ARN"
        )

        # --- Subsequent submissions: MUST be rejected with ExecutionAlreadyExists ---
        duplicate_errors = []
        for attempt in range(1, n_submissions):
            try:
                sfn.start_execution(
                    stateMachineArn=state_machine_arn,
                    name=contact_id,
                    input=json.dumps({"contact_id": contact_id, "attempt": attempt}),
                )
                duplicate_errors.append(
                    f"Attempt {attempt + 1} for contact_id={contact_id!r} succeeded "
                    f"but should have raised ExecutionAlreadyExists"
                )
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                assert error_code == "ExecutionAlreadyExists", (
                    f"Attempt {attempt + 1} for contact_id={contact_id!r} raised "
                    f"{error_code!r} instead of 'ExecutionAlreadyExists'"
                )

        assert not duplicate_errors, "\n".join(duplicate_errors)

        # --- Exactly one execution must exist ---
        executions_response = sfn.list_executions(stateMachineArn=state_machine_arn)
        executions = executions_response["executions"]

        assert len(executions) == 1, (
            f"Expected exactly 1 execution for contact_id={contact_id!r} "
            f"after {n_submissions} submission(s), found {len(executions)}"
        )
        assert executions[0]["executionArn"] == first_execution_arn, (
            f"The single execution ARN does not match the first start_execution ARN. "
            f"expected={first_execution_arn!r}, actual={executions[0]['executionArn']!r}"
        )
        assert executions[0]["name"] == contact_id, (
            f"Execution name mismatch: expected={contact_id!r}, "
            f"actual={executions[0]['name']!r}"
        )
