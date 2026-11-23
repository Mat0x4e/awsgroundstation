"""Tests for lambdas/receiver_arm/handler.py _wait_for_running fail-fast."""

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mod():
    env = {"RECEIVER_INSTANCE_ID": "i-0abc", "RECEIVER_UDP_PORT": "4001"}
    with patch.dict(os.environ, env):
        import lambdas.receiver_arm.handler as m

        yield importlib.reload(m)


def _ec2(*instances):
    ec2 = MagicMock()
    ec2.describe_instances.side_effect = [
        {"Reservations": [{"Instances": [i]}]} for i in instances
    ]
    return ec2


def test_stopped_with_state_reason_fails_fast(mod):
    """Instance back to stopped (Client.InvalidKMSKey) raises at once, no 4 min poll."""
    stopped = {
        "State": {"Name": "stopped"},
        "StateReason": {
            "Code": "Client.InvalidKMSKey",
            "Message": "Client.InvalidKMSKey: KMS key invalid",
        },
    }
    # Only one response queued: a second poll would raise StopIteration.
    ec2 = _ec2(stopped)
    with patch.object(mod.time, "sleep") as sleep:
        with pytest.raises(RuntimeError) as exc:
            mod._wait_for_running(ec2, "i-0abc")
    assert "Client.InvalidKMSKey" in str(exc.value)
    assert "did not reach running" not in str(exc.value)
    assert ec2.describe_instances.call_count == 1
    sleep.assert_not_called()


def test_pending_then_stopped_fails_fast(mod):
    pending = {"State": {"Name": "pending"}}
    stopped = {
        "State": {"Name": "stopped"},
        "StateReason": {"Code": "Client.InvalidKMSKey", "Message": "bad key"},
    }
    ec2 = _ec2(pending, stopped)
    with patch.object(mod.time, "sleep"):
        with pytest.raises(RuntimeError, match="Client.InvalidKMSKey"):
            mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == 2


def test_slow_pending_still_times_out(mod):
    pending = {"State": {"Name": "pending"}}
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [pending]}]}
    with patch.object(mod.time, "sleep"):
        with pytest.raises(RuntimeError, match="did not reach running state within timeout"):
            mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == mod.RUNNING_POLL_MAX_ATTEMPTS


def test_pending_then_running_succeeds(mod):
    ec2 = _ec2({"State": {"Name": "pending"}}, {"State": {"Name": "running"}})
    with patch.object(mod.time, "sleep"):
        mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == 2
