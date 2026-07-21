"""Directly call one test function to verify it works outside pytest."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGGREGATION_INSTANCE_ID"] = "i-0123456789abcdef0"

# Re-use the helpers from the test file
from unittest.mock import patch, MagicMock
import importlib

_INSTANCE_ID = "i-0123456789abcdef0"
_FAKE_COMMAND_ID = "cmd-0abc1234567890def"
_SAMPLE_EVENT = {
    "bucket": "test-bucket",
    "contact_id": "ct-abc123",
    "contact_date": "2026-01-15",
}


def make_running_ec2():
    mock_ec2 = MagicMock()
    mock_ec2.start_instances.return_value = {}
    mock_ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": _INSTANCE_ID, "State": {"Name": "running", "Code": 16}}]}]
    }
    return mock_ec2


def make_ssm():
    mock_ssm = MagicMock()
    mock_ssm.send_command.return_value = {"Command": {"CommandId": _FAKE_COMMAND_ID}}
    return mock_ssm


def client_factory(mock_ec2, mock_ssm):
    def factory(service, **kwargs):
        if service == "ec2":
            return mock_ec2
        if service == "ssm":
            return mock_ssm
        raise ValueError(service)
    return factory


print("Loading handler...")
import lambdas.aggregation_trigger.handler as mod
importlib.reload(mod)
print("Handler loaded, INSTANCE_ID =", mod.INSTANCE_ID)

print("Running test 1: happy path...")
mock_ec2 = make_running_ec2()
mock_ssm = make_ssm()

with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=client_factory(mock_ec2, mock_ssm)):
    with patch("lambdas.aggregation_trigger.handler.time.sleep"):
        result = mod.lambda_handler(_SAMPLE_EVENT, None)

print("Result:", result)
assert "command_id" in result
assert result["instance_id"] == _INSTANCE_ID
assert result["command_id"] == _FAKE_COMMAND_ID
print("Test 1 PASSED")

print("Running test 2: timeout failure...")
mock_ec2_pending = MagicMock()
mock_ec2_pending.start_instances.return_value = {}
mock_ec2_pending.describe_instances.return_value = {
    "Reservations": [{"Instances": [{"InstanceId": _INSTANCE_ID, "State": {"Name": "pending", "Code": 0}}]}]
}
mock_ssm2 = make_ssm()

importlib.reload(mod)

raised = False
try:
    with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=client_factory(mock_ec2_pending, mock_ssm2)):
        with patch("lambdas.aggregation_trigger.handler.time.sleep"):
            mod.lambda_handler(_SAMPLE_EVENT, None)
except RuntimeError as e:
    raised = True
    print("RuntimeError:", e)
    assert "did not reach running state" in str(e)
    assert _INSTANCE_ID in str(e)
    print("Test 2 PASSED")

if not raised:
    print("FAIL: expected RuntimeError was not raised")
    sys.exit(1)

print("All direct tests PASSED")
