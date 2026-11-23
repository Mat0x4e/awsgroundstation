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
    # Guard against vacuous assertions if sendto failed silently
    assert res["sent"] == 20, f"Expected 20 sent, got {res['sent']}, errors: {res['errors']}"
    assert len(got) == 20
    assert all(len(d) == 300 and d[:8] == b"GSCANARY" for d in got)
    assert sorted(struct.unpack(">I", d[8:12])[0] for d in got) == list(range(20))
    assert res["failed"] == 0
    assert (res["first_seq"], res["last_seq"]) == (0, 19)
    assert res["target"] == "127.0.0.1" and res["port"] == port
    assert res["payload_size"] == 300 and res["errors"] == {}


def test_defaults_from_env(rx, monkeypatch):
    port = rx.getsockname()[1]
    monkeypatch.setenv("CANARY_TARGET_IP", "127.0.0.1")
    monkeypatch.setenv("CANARY_PORT", str(port))
    res = h.handler({"count": 3, "pacing": 0})
    # Guard against sendto failures
    assert res["sent"] == 3, f"Expected 3 sent, got {res['sent']}, errors: {res['errors']}"
    assert res["payload_size"] == 1000
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
    # Flaky sends alternating success/fail: 5 succeed, 5 fail
    assert res["sent"] == 5, f"Expected 5 sent, got {res['sent']}, errors: {res['errors']}"
    assert res["failed"] == 5
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


# --- vita49 framing: validated against the REAL listener parser ---------------
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sync_receiver_listen",
    Path(__file__).resolve().parent.parent / "scripts" / "sync_receiver_listen.py")
_listen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_listen)
extract_frame_payload = _listen.extract_frame_payload


def test_vita49_datagrams_accepted_by_real_listener_parser(rx):
    port = rx.getsockname()[1]
    res = h.handler({"target_ip": "127.0.0.1", "port": port, "count": 25,
                     "payload_size": 1000, "pacing": 0, "vita49": True})
    got = _drain(rx)
    # Guard against vacuous truth if no datagrams received
    assert res["sent"] == 25, f"Expected 25 sent, got {res['sent']}, errors: {res['errors']}"
    assert len(got) == 25 and res["vita49"] is True
    total = 0
    for d in got:
        assert len(d) <= 1400 and len(d) % 4 == 0
        payload = extract_frame_payload(d)
        assert payload is not None
        assert payload == d[28:]
        assert payload[:8] == b"GSCANARY"
        total += len(payload)
    assert sorted(struct.unpack(">I", extract_frame_payload(d)[8:12])[0]
                  for d in got) == list(range(25))
    assert total == res["expected_listener_payload_bytes"] == 25 * (1000 - 28)


def test_vita49_size_rounded_to_multiple_of_four(rx):
    port = rx.getsockname()[1]
    res = h.handler({"target_ip": "127.0.0.1", "port": port, "count": 2,
                     "payload_size": 1003, "pacing": 0, "vita49": True})
    got = _drain(rx)
    assert res["payload_size"] == 1000
    assert all(len(d) == 1000 and extract_frame_payload(d) for d in got)


def test_vita49_header_fields():
    d = h.build_vita49_datagram(7, 100)
    header = int.from_bytes(d[:4], "big")
    assert header >> 28 == 1 and header & 0xFFFF == 25
    assert len(d) == 100


def test_vita49_too_small_or_non_bool_rejected():
    with patch.object(socket.socket, "sendto") as send:
        with pytest.raises(h.ValidationError):
            h.handler({"target_ip": "127.0.0.1", "payload_size": 36, "vita49": True})
        with pytest.raises(h.ValidationError):
            h.handler({"target_ip": "127.0.0.1", "vita49": "yes"})
        send.assert_not_called()


def test_plain_datagram_rejected_by_listener_parser(rx):
    """Documents current (default) behaviour: no VITA-49 header -> dropped."""
    port = rx.getsockname()[1]
    res = h.handler({"target_ip": "127.0.0.1", "port": port, "count": 5,
                     "payload_size": 300, "pacing": 0})
    got = _drain(rx)
    # Guard against vacuous truth — ensure datagrams were sent before checking parser
    assert res["sent"] == 5, f"Expected 5 sent, got {res['sent']}, errors: {res['errors']}"
    assert len(got) == 5 and res["expected_listener_payload_bytes"] == 0
    assert all(extract_frame_payload(d) is None for d in got)
