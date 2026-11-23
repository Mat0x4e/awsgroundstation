"""Tests for lambdas/network_canary/handler.py (real localhost UDP socket)."""
import socket
import struct
from unittest.mock import patch

import pytest

from lambdas.network_canary import handler as h


@pytest.fixture
def rx():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.5)
    yield s
    s.close()


def _drain(s):
    out = []
    try:
        while True:
            out.append(s.recvfrom(4096)[0])
    except socket.timeout:
        return out


def test_sends_all_datagrams_with_header_seq_and_size(rx):
    port = rx.getsockname()[1]
    res = h.handler({"target_ip": "127.0.0.1", "port": port, "count": 20,
                     "payload_size": 300, "pacing": 0})
    got = _drain(rx)
    assert len(got) == 20
    assert all(len(d) == 300 and d[:8] == b"GSCANARY" for d in got)
    assert sorted(struct.unpack(">I", d[8:12])[0] for d in got) == list(range(20))
    assert res["sent"] == 20 and res["failed"] == 0
    assert (res["first_seq"], res["last_seq"]) == (0, 19)
    assert res["target"] == "127.0.0.1" and res["port"] == port
    assert res["payload_size"] == 300 and res["errors"] == {}


def test_defaults_from_env(rx, monkeypatch):
    port = rx.getsockname()[1]
    monkeypatch.setenv("CANARY_TARGET_IP", "127.0.0.1")
    monkeypatch.setenv("CANARY_PORT", str(port))
    res = h.handler({"count": 3, "pacing": 0})
    assert res["sent"] == 3 and res["payload_size"] == 1000
    assert [len(d) for d in _drain(rx)] == [1000] * 3


def test_send_errors_are_counted(rx):
    port = rx.getsockname()[1]
    real = socket.socket.sendto
    calls = {"n": 0}

    def flaky(self, data, addr):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise OSError("boom")
        return real(self, data, addr)

    with patch.object(socket.socket, "sendto", flaky):
        res = h.handler({"target_ip": "127.0.0.1", "port": port, "count": 10,
                         "payload_size": 100, "pacing": 0})
    assert res["sent"] == 5 and res["failed"] == 5
    assert res["errors"] == {"OSError: boom": 5}
    assert len(_drain(rx)) == 5


@pytest.mark.parametrize("ev", [
    {"payload_size": 1401},
    {"payload_size": 11},
    {"count": 0},
    {"count": -1},
    {"count": "x"},
    {"port": 0},
    {"port": 70000},
    {"port": "abc"},
    {"pacing": -0.1},
    {"pacing": 5},
    {"target_ip": "not-an-ip"},
])
def test_validation_rejects_bad_input(ev):
    ev = {"target_ip": "127.0.0.1", **ev}
    with patch.object(socket.socket, "sendto") as send:
        with pytest.raises(h.ValidationError):
            h.handler(ev)
        send.assert_not_called()


def test_missing_target_rejected(monkeypatch):
    monkeypatch.delenv("CANARY_TARGET_IP", raising=False)
    with pytest.raises(h.ValidationError, match="target_ip"):
        h.handler({})
