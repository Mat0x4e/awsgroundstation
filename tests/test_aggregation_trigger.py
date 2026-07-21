"""Unit tests for lambdas/aggregation_trigger/handler.py.

All tests mock boto3.client directly using unittest.mock so there are no
blocking network calls, no waiter loops, and no Windows-specific moto
state-transition delays.

The three required scenarios:
1. Instance starts successfully → SSM command sent → command_id + instance_id returned.
2. Instance never reaches 'running' → RuntimeError raised with expected message.
3. command_id in the return value matches what SSM's send_command returned.
"""
import os
import importlib
import pytest
from unittest.mock import patch, MagicMock, call

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_INSTANCE_ID = "i-0123456789abcdef0"
_FAKE_COMMAND_ID = "cmd-0abc1234567890def"
_FAKE_BUCKET = "groundstation-noaa20-sdr-output-test"
_FAKE_CONTACT_ID = "ct-abc123"
_FAKE_CONTACT_DATE = "2026-01-15"

_SAMPLE_EVENT = {
    "bucket": _FAKE_BUCKET,
    "contact_id": _FAKE_CONTACT_ID,
    "contact_date": _FAKE_CONTACT_DATE,
}


# ---------------------------------------------------------------------------
# Helper: build mock EC2 / SSM clients
# ---------------------------------------------------------------------------

def _make_running_ec2() -> MagicMock:
    """EC2 mock that immediately reports the instance as 'running'."""
    mock_ec2 = MagicMock()
    mock_ec2.start_instances.return_value = {}
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": _INSTANCE_ID,
                        "State": {"Name": "running", "Code": 16},
                    }
                ]
            }
        ]
    }
    return mock_ec2


def _make_stopped_then_running_ec2(stopped_polls: int = 2) -> MagicMock:
    """EC2 mock that returns 'stopped' for N polls, then 'running'."""
    mock_ec2 = MagicMock()
    mock_ec2.start_instances.return_value = {}

    call_count = {"n": 0}

    def describe_side_effect(**kwargs):
        call_count["n"] += 1
        name = "running" if call_count["n"] > stopped_polls else "stopped"
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": _INSTANCE_ID,
                            "State": {"Name": name, "Code": 16 if name == "running" else 80},
                        }
                    ]
                }
            ]
        }

    mock_ec2.describe_instances.side_effect = describe_side_effect
    return mock_ec2


def _make_pending_ec2() -> MagicMock:
    """EC2 mock that always returns 'pending' — triggers the timeout RuntimeError."""
    mock_ec2 = MagicMock()
    mock_ec2.start_instances.return_value = {}
    mock_ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": _INSTANCE_ID,
                        "State": {"Name": "pending", "Code": 0},
                    }
                ]
            }
        ]
    }
    return mock_ec2


def _make_ssm(command_id: str = _FAKE_COMMAND_ID) -> MagicMock:
    """SSM mock whose send_command returns the given command_id."""
    mock_ssm = MagicMock()
    mock_ssm.send_command.return_value = {
        "Command": {
            "CommandId": command_id,
        }
    }
    return mock_ssm


def _make_client_factory(mock_ec2: MagicMock, mock_ssm: MagicMock):
    """Return a side_effect for boto3.client that returns the right mock per service."""
    def factory(service, **kwargs):
        if service == "ec2":
            return mock_ec2
        if service == "ssm":
            return mock_ssm
        raise ValueError(f"Unexpected service: {service!r}")
    return factory


def _load_handler():
    """(Re-)import the handler with AGGREGATION_INSTANCE_ID set."""
    with patch.dict(os.environ, {"AGGREGATION_INSTANCE_ID": _INSTANCE_ID}):
        import lambdas.aggregation_trigger.handler as mod
        importlib.reload(mod)
        return mod


# ---------------------------------------------------------------------------
# Test 1: Instance starts successfully and SSM command is sent
# ---------------------------------------------------------------------------

class TestInstanceStartsSuccessfully:
    """EC2 instance reaches 'running'; handler sends SSM command and returns IDs."""

    def test_returns_command_id_key(self):
        mod = _load_handler()
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                result = mod.lambda_handler(_SAMPLE_EVENT, None)

        assert "command_id" in result

    def test_returns_instance_id_key(self):
        mod = _load_handler()
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                result = mod.lambda_handler(_SAMPLE_EVENT, None)

        assert result["instance_id"] == _INSTANCE_ID

    def test_ssm_send_command_called_once(self):
        mod = _load_handler()
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                mod.lambda_handler(_SAMPLE_EVENT, None)

        mock_ssm.send_command.assert_called_once()

    def test_ssm_send_command_targets_correct_instance(self):
        mod = _load_handler()
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                mod.lambda_handler(_SAMPLE_EVENT, None)

        kwargs = mock_ssm.send_command.call_args.kwargs
        assert _INSTANCE_ID in kwargs.get("InstanceIds", [])

    def test_start_instances_called_with_correct_id(self):
        mod = _load_handler()
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                mod.lambda_handler(_SAMPLE_EVENT, None)

        mock_ec2.start_instances.assert_called_once()
        args_kwargs = mock_ec2.start_instances.call_args
        assert _INSTANCE_ID in args_kwargs.kwargs.get("InstanceIds", args_kwargs.args[0] if args_kwargs.args else [])

    def test_handler_works_when_instance_starts_after_a_few_polls(self):
        """Handler polls until 'running'; works even when first polls return 'stopped'."""
        mod = _load_handler()
        mock_ec2 = _make_stopped_then_running_ec2(stopped_polls=3)
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                result = mod.lambda_handler(_SAMPLE_EVENT, None)

        assert result["instance_id"] == _INSTANCE_ID
        assert "command_id" in result


# ---------------------------------------------------------------------------
# Test 2: Instance fails to reach running state → RuntimeError
# ---------------------------------------------------------------------------

class TestInstanceTimeoutFailure:
    """describe_instances always returns 'pending' → RuntimeError after max attempts."""

    def test_raises_runtime_error(self):
        mod = _load_handler()
        mock_ec2 = _make_pending_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                with pytest.raises(RuntimeError):
                    mod.lambda_handler(_SAMPLE_EVENT, None)

    def test_error_message_contains_expected_text(self):
        mod = _load_handler()
        mock_ec2 = _make_pending_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                with pytest.raises(RuntimeError) as exc_info:
                    mod.lambda_handler(_SAMPLE_EVENT, None)

        assert "did not reach running state" in str(exc_info.value)

    def test_error_message_contains_instance_id(self):
        mod = _load_handler()
        mock_ec2 = _make_pending_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                with pytest.raises(RuntimeError) as exc_info:
                    mod.lambda_handler(_SAMPLE_EVENT, None)

        assert _INSTANCE_ID in str(exc_info.value)

    def test_ssm_send_command_not_called_on_timeout(self):
        """SSM must not be invoked when the instance never starts."""
        mod = _load_handler()
        mock_ec2 = _make_pending_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                with pytest.raises(RuntimeError):
                    mod.lambda_handler(_SAMPLE_EVENT, None)

        mock_ssm.send_command.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: SSM SendCommand returns command_id correctly
# ---------------------------------------------------------------------------

class TestSSMCommandIdReturned:
    """command_id in the handler response matches SSM's send_command output."""

    def test_command_id_matches_ssm_response(self):
        """The command_id in the return dict equals what SSM send_command returned."""
        mod = _load_handler()
        expected_id = "cmd-0xdeadbeef12345678"
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm(command_id=expected_id)

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                result = mod.lambda_handler(_SAMPLE_EVENT, None)

        assert result["command_id"] == expected_id

    def test_different_command_ids_are_passed_through(self):
        """Handler does not hard-code or transform the command_id — it passes it through."""
        mod = _load_handler()

        for expected_id in ["cmd-aaa", "cmd-bbb", "cmd-000"]:
            mock_ec2 = _make_running_ec2()
            mock_ssm = _make_ssm(command_id=expected_id)

            with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
                with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                    result = mod.lambda_handler(_SAMPLE_EVENT, None)

            assert result["command_id"] == expected_id, f"Expected {expected_id!r}, got {result['command_id']!r}"

    def test_command_id_is_string(self):
        """command_id in the response is always a string."""
        mod = _load_handler()
        mock_ec2 = _make_running_ec2()
        mock_ssm = _make_ssm()

        with patch("lambdas.aggregation_trigger.handler.boto3.client", side_effect=_make_client_factory(mock_ec2, mock_ssm)):
            with patch("lambdas.aggregation_trigger.handler.time.sleep"):
                result = mod.lambda_handler(_SAMPLE_EVENT, None)

        assert isinstance(result["command_id"], str)
