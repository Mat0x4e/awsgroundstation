"""TEMPORARY network canary: sends UDP datagrams to the sync receiver.

Runs inside the VPC to validate the security-group path (Lambda -> dataflow
ENI) before a paid contact. No AWS SDK calls, no internet: UDP only.

Event (all optional):
    target_ip     str   default env CANARY_TARGET_IP
    port          int   default env CANARY_PORT (55888), 1..65535
    count         int   default 100, 1..MAX_COUNT
    payload_size  int   default 1000, HEADER_LEN..1400 bytes
    pacing        float default 0.001 s between datagrams, 0..MAX_PACING

    vita49        bool  default false. True: prefix each datagram with a VITA-49
                  Signal Data header the receiver listener accepts; payload_size
                  is then the total datagram length, rounded down to a multiple
                  of 4. Response field expected_listener_payload_bytes = bytes
                  the listener should write to combined.cadu.

Datagram: MAGIC (b'GSCANARY') + 4-byte big-endian sequence, zero padded.
"""
import ipaddress
import os
import socket
import struct
import time

MAGIC = b"GSCANARY"
HEADER_LEN = len(MAGIC) + 4
MAX_PAYLOAD = 1400
MAX_COUNT = 100000
MAX_PACING = 1.0
DEFAULT_PORT = 55888
DEFAULT_COUNT = 100
DEFAULT_PAYLOAD = 1000
DEFAULT_PACING = 0.001


class ValidationError(ValueError):
    """Raised for invalid event parameters."""


def _int(value, name):
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer, got {value!r}")


def parse_params(event):
    event = event or {}
    target = event.get("target_ip") or os.environ.get("CANARY_TARGET_IP")
    if not target:
        raise ValidationError("target_ip missing (event or CANARY_TARGET_IP)")
    try:
        ipaddress.ip_address(target)
    except ValueError:
        raise ValidationError(f"target_ip is not a valid IP: {target!r}")

    port = _int(event.get("port", os.environ.get("CANARY_PORT", DEFAULT_PORT)), "port")
    if not 1 <= port <= 65535:
        raise ValidationError(f"port must be 1..65535, got {port}")

    count = _int(event.get("count", DEFAULT_COUNT), "count")
    if not 1 <= count <= MAX_COUNT:
        raise ValidationError(f"count must be 1..{MAX_COUNT}, got {count}")

    size = _int(event.get("payload_size", DEFAULT_PAYLOAD), "payload_size")
    if not HEADER_LEN <= size <= MAX_PAYLOAD:
        raise ValidationError(
            f"payload_size must be {HEADER_LEN}..{MAX_PAYLOAD}, got {size}")

    pacing = event.get("pacing", DEFAULT_PACING)
    if isinstance(pacing, bool) or not isinstance(pacing, (int, float)):
        raise ValidationError(f"pacing must be a number, got {pacing!r}")
    if not 0 <= pacing <= MAX_PACING:
        raise ValidationError(f"pacing must be 0..{MAX_PACING}, got {pacing}")

    return target, port, count, size, float(pacing)


def build_datagram(seq, size):
    return (MAGIC + struct.pack(">I", seq)).ljust(size, b"\x00")


VITA49_HEADER_LEN = 28          # 7 words, as assumed by sync_receiver_listen
VITA49_PKT_TYPE_SIGNAL_DATA = 1
VITA49_MIN_SIZE = VITA49_HEADER_LEN + HEADER_LEN


def vita49_size(size):
    """Total datagram length for vita49 framing: multiple of 4, >= minimum."""
    size -= size % 4
    if size < VITA49_MIN_SIZE:
        raise ValidationError(
            f"payload_size must be >= {VITA49_MIN_SIZE} with vita49, got {size}")
    return size


def build_vita49_datagram(seq, size):
    """Signal Data header (type 1, size in 32-bit words incl. header) + 28-byte
    header (words 1..6: word 1 = sequence counter, rest zero) + canary payload.
    `size` must already be a multiple of 4 (see vita49_size)."""
    header = (VITA49_PKT_TYPE_SIGNAL_DATA << 28) | (size // 4)
    head = struct.pack(">I", header) + struct.pack(">I", seq & 0xFFFFFFFF)
    head = head.ljust(VITA49_HEADER_LEN, b"\x00")
    return head + build_datagram(seq, size - VITA49_HEADER_LEN)


def handler(event, context=None):
    target, port, count, size, pacing = parse_params(event)
    vita49 = (event or {}).get("vita49", False)
    if not isinstance(vita49, bool):
        raise ValidationError(f"vita49 must be a boolean, got {vita49!r}")
    if vita49:
        size = vita49_size(size)
    build = build_vita49_datagram if vita49 else build_datagram
    sent = failed = 0
    errors = {}
    first_seq = last_seq = None
    sock = socket.socket(socket.AF_INET6 if ":" in target else socket.AF_INET,
                         socket.SOCK_DGRAM)
    try:
        for seq in range(count):
            try:
                sock.sendto(build(seq, size), (target, port))
                sent += 1
                if first_seq is None:
                    first_seq = seq
                last_seq = seq
            except OSError as exc:
                failed += 1
                key = f"{type(exc).__name__}: {exc}"
                errors[key] = errors.get(key, 0) + 1
            if pacing and seq < count - 1:
                time.sleep(pacing)
    finally:
        sock.close()
    return {
        "sent": sent,
        "failed": failed,
        "target": target,
        "port": port,
        "payload_size": size,
        "vita49": vita49,
        "expected_listener_payload_bytes":
            sent * (size - VITA49_HEADER_LEN) if vita49 else 0,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "errors": errors,
    }
