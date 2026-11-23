"""Tests for lambdas/receiver_arm/handler.py _wait_for_running fail-fast.

Polling time is driven by attempt count x RUNNING_POLL_INTERVAL_SECONDS and
time.sleep is patched, so these run instantly.
"""

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest

KMS_STOPPED = {
    "State": {"Name": "stopped"},
    "StateReason": {
        "Code": "Client.InvalidKMSKey",
        "Message": "Client.InvalidKMSKey: KMS key invalid",
    },
}
PENDING = {"State": {"Name": "pending"}}
RUNNING = {"State": {"Name": "running"}}


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


def test_stale_stopped_state_then_pending_then_running_succeeds(mod):
    """Stale stopped+StateReason right after StartInstances must not fail the start."""
    ec2 = _ec2(KMS_STOPPED, KMS_STOPPED, PENDING, RUNNING)
    with patch.object(mod.time, "sleep"):
        mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == 4


def test_pending_then_stopped_fails_fast_with_reason(mod):
    ec2 = _ec2(PENDING, KMS_STOPPED)
    with patch.object(mod.time, "sleep"):
        with pytest.raises(RuntimeError, match="Client.InvalidKMSKey"):
            mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == 2


def test_stopped_beyond_grace_period_fails_with_reason(mod):
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [KMS_STOPPED]}]}
    with patch.object(mod.time, "sleep"):
        with pytest.raises(RuntimeError) as exc:
            mod._wait_for_running(ec2, "i-0abc")
    assert "Client.InvalidKMSKey" in str(exc.value)
    assert "did not reach running" not in str(exc.value)
    # polls at 0,5,...,30 s: fails on the first poll at/after the grace period
    expected = mod.STOPPED_GRACE_SECONDS // mod.RUNNING_POLL_INTERVAL_SECONDS + 1
    assert ec2.describe_instances.call_count == expected


def test_slow_pending_still_times_out(mod):
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [PENDING]}]}
    with patch.object(mod.time, "sleep"):
        with pytest.raises(RuntimeError, match="did not reach running state within timeout"):
            mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == mod.RUNNING_POLL_MAX_ATTEMPTS


def test_pending_then_running_succeeds(mod):
    ec2 = _ec2(PENDING, RUNNING)
    with patch.object(mod.time, "sleep"):
        mod._wait_for_running(ec2, "i-0abc")
    assert ec2.describe_instances.call_count == 2
