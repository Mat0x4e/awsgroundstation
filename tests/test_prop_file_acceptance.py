"""Property-based tests for IQExtractor file acceptance criterion.

# **Validates: Requirements 1.5, 1.6**

Property 4 — File acceptance criterion:
    For any .pcap file, the IQExtractor SHALL accept the file if and only if
    it contains at least one valid VITA-49 Signal Data packet.

    - A file with zero valid VITA-49 Signal Data packets SHALL raise
      NoValidPacketsError.
    - A file with at least one valid packet SHALL be accepted regardless of
      other errors (malformed packets, invalid sample rate, mixed packet types).
"""

import struct
import os
import tempfile
from pathlib import Path
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from scripts.iq_extract import IQExtractor, NoValidPacketsError

# ---------------------------------------------------------------------------
# PCAP / Frame construction helpers
# ---------------------------------------------------------------------------

def _pcap_global_header() -> bytes:
    """24-byte pcap global header with Ethernet link type (1)."""
    magic = 0xA1B2C3D4
    ver_major = 2
    ver_minor = 4
    thiszone = 0
    sigfigs = 0
    snaplen = 65535
    link_type = 1  # Ethernet
    return struct.pack("<IHHiIII", magic, ver_major, ver_minor, thiszone, sigfigs, snaplen, link_type)


def _pcap_record(frame: bytes) -> bytes:
    """Wrap a raw Ethernet frame in a 16-byte pcap record header."""
    cap_len = len(frame)
    orig_len = len(frame)
    ts_sec = 0
    ts_usec = 0
    return struct.pack("<IIII", ts_sec, ts_usec, cap_len, orig_len) + frame


def _ethernet_ipv4_udp_frame(udp_payload: bytes) -> bytes:
    """Build a minimal Ethernet → IPv4 → UDP frame around the given UDP payload."""
    # UDP header: src_port, dst_port, length, checksum
    udp_length = 8 + len(udp_payload)
    udp_header = struct.pack("!HHHH", 50000, 4991, udp_length, 0)

    # IPv4 header (20 bytes, no options): version/ihl, dscp, total_len,
    #   id, flags/frag_offset, ttl, protocol=17(UDP), checksum, src, dst
    ip_total_len = 20 + udp_length
    ip_header = struct.pack(
        "!BBHHHBBHII",
        0x45,          # version=4, IHL=5
        0,             # DSCP/ECN
        ip_total_len,  # total length
        0x1234,        # identification
        0,             # flags + fragment offset (unfragmented)
        64,            # TTL
        17,            # protocol = UDP
        0,             # checksum (not validated by IQExtractor)
        0xC0A80001,    # src IP 192.168.0.1
        0xC0A80002,    # dst IP 192.168.0.2
    )

    # Ethernet header: dst MAC (6), src MAC (6), ethertype 0x0800 (IPv4)
    eth_header = bytes(6) + bytes(6) + struct.pack("!H", 0x0800)

    return eth_header + ip_header + udp_header + udp_payload


def _vita49_signal_data_packet(seq_num: int, n_payload_words: int) -> bytes:
    """Build a valid VITA-49 Signal Data packet (type=1).

    Header is 7 words (28 bytes).  Payload is n_payload_words × 4 bytes of
    zeros.  Total size = 7 + n_payload_words words.

    Args:
        seq_num:        4-bit sequence number (0–15).
        n_payload_words: Number of 32-bit payload words (>= 1).
    """
    size_words = 7 + n_payload_words  # 7 header words + payload
    # Bits 31-28: type=1, bits 27-24: TSI/TSF=0, bits 23-20: C/T/R/R=0,
    # bits 19-16: seq_num, bits 15-0: size_words
    raw_word = (1 << 28) | ((seq_num & 0xF) << 16) | (size_words & 0xFFFF)
    header_word = struct.pack("!I", raw_word)
    # Remaining 6 header words (stream ID, class ID placeholder, timestamps…)
    rest_of_header = bytes(6 * 4)
    payload = bytes(n_payload_words * 4)
    return header_word + rest_of_header + payload


def _vita49_invalid_packet_wrong_type(pkt_type: int, size_words: int = 8) -> bytes:
    """Build a VITA-49 packet with an arbitrary non-1 type and a valid size."""
    raw_word = ((pkt_type & 0xF) << 28) | (0 << 16) | (size_words & 0xFFFF)
    header_word = struct.pack("!I", raw_word)
    rest = bytes((size_words - 1) * 4)
    return header_word + rest


def _vita49_invalid_packet_zero_size() -> bytes:
    """Build a VITA-49 packet with size_words=0 (will be treated as malformed)."""
    raw_word = (1 << 28) | (0 << 16) | 0  # type=1 but size=0
    return struct.pack("!I", raw_word)


def _vita49_truncated_bytes() -> bytes:
    """Return just 4 bytes with a plausible-looking type=1 header but no body.

    The size_words advertises 8 words (32 bytes) but only 4 bytes are present,
    so the IQExtractor will stop parsing this UDP payload (truncated check).
    """
    size_words = 8
    raw_word = (1 << 28) | (0 << 16) | size_words
    return struct.pack("!I", raw_word)  # Only the header word — body missing


def _vita49_garbage_bytes(n: int = 16) -> bytes:
    """Return n bytes of non-parseable data (all 0xFF)."""
    return bytes([0xFF] * n)


def _build_pcap(vita49_packets: list[bytes]) -> bytes:
    """Assemble a complete pcap file from a list of VITA-49 payloads.

    Each VITA-49 payload is wrapped in UDP → IPv4 → Ethernet → pcap record.
    """
    records = b""
    for pkt in vita49_packets:
        frame = _ethernet_ipv4_udp_frame(pkt)
        records += _pcap_record(frame)
    return _pcap_global_header() + records


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A valid Signal Data packet: 1–4 payload words, seq_num 0–15
_valid_signal_data = st.builds(
    _vita49_signal_data_packet,
    seq_num=st.integers(min_value=0, max_value=15),
    n_payload_words=st.integers(min_value=1, max_value=4),
)

# Invalid packets: wrong type (2–15 excluding 1), valid-looking size
_invalid_wrong_type = st.builds(
    _vita49_invalid_packet_wrong_type,
    pkt_type=st.integers(min_value=2, max_value=15).filter(lambda t: t != 1),
    size_words=st.integers(min_value=2, max_value=8),
)

# Context packets (type 4) — also not Signal Data
_invalid_context = st.builds(
    _vita49_invalid_packet_wrong_type,
    pkt_type=st.just(4),
    size_words=st.just(8),
)

# A pool of "invalid" things that could appear in a pcap
_invalid_vita49 = st.one_of(
    _invalid_wrong_type,
    _invalid_context,
    st.just(_vita49_invalid_packet_zero_size()),
    st.just(_vita49_truncated_bytes()),
    st.just(_vita49_garbage_bytes(16)),
)


# ---------------------------------------------------------------------------
# Shared extractor instance
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def extractor() -> IQExtractor:
    return IQExtractor()


# ---------------------------------------------------------------------------
# Property 4a: Files with zero valid Signal Data packets → NoValidPacketsError
# ---------------------------------------------------------------------------

@given(
    invalid_packets=st.lists(_invalid_vita49, min_size=0, max_size=10)
)
@settings(max_examples=100)
def test_file_with_no_valid_packets_raises_error(
    invalid_packets: list[bytes]
) -> None:
    """A pcap containing zero valid VITA-49 Signal Data packets must raise
    NoValidPacketsError.

    Validates: Requirements 1.5, 1.6
    """
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        pcap_bytes = _build_pcap(invalid_packets)
        pcap_file = tmp_path / "no_valid.pcap"
        pcap_file.write_bytes(pcap_bytes)
        output_file = tmp_path / "out.cs8"

        extractor = IQExtractor()
        with pytest.raises(NoValidPacketsError):
            extractor.extract(str(pcap_file), str(output_file))


# ---------------------------------------------------------------------------
# Property 4b: Files with >= 1 valid Signal Data packet → accepted (no error)
# ---------------------------------------------------------------------------

@given(
    valid_packets=st.lists(
        _valid_signal_data, min_size=1, max_size=10
    ),
    invalid_packets=st.lists(_invalid_vita49, min_size=0, max_size=10),
    interleave=st.booleans(),
)
@settings(max_examples=100)
def test_file_with_at_least_one_valid_packet_is_accepted(
    valid_packets: list[bytes], invalid_packets: list[bytes], interleave: bool
) -> None:
    """A pcap with at least one valid VITA-49 Signal Data packet must be
    accepted regardless of other malformed or non-Signal-Data packets.

    Validates: Requirements 1.5, 1.6
    """
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        if interleave:
            # Mix valid and invalid packets together
            all_pkts = valid_packets + invalid_packets
        else:
            # Put invalids first, then valids
            all_pkts = invalid_packets + valid_packets

        pcap_bytes = _build_pcap(all_pkts)
        pcap_file = tmp_path / "mixed.pcap"
        pcap_file.write_bytes(pcap_bytes)
        output_file = tmp_path / "out.cs8"

        extractor = IQExtractor()
        # Must not raise any exception
        result = extractor.extract(str(pcap_file), str(output_file))

        # Basic sanity: at least one valid packet was counted
        assert result.valid_packets >= 1
        # Output file was written
        assert output_file.exists()
        assert output_file.stat().st_size > 0


# ---------------------------------------------------------------------------
# Deterministic complementary cases
# ---------------------------------------------------------------------------

def test_empty_pcap_raises_error(tmp_path) -> None:
    """A pcap with only the global header and no records raises NoValidPacketsError."""
    pcap_file = tmp_path / "empty.pcap"
    pcap_file.write_bytes(_pcap_global_header())
    output_file = tmp_path / "out.cs8"

    extractor = IQExtractor()
    with pytest.raises(NoValidPacketsError):
        extractor.extract(str(pcap_file), str(output_file))


def test_only_context_packets_raises_error(tmp_path) -> None:
    """A pcap with only VITA-49 Context packets (type 4) raises NoValidPacketsError."""
    context_pkt = _vita49_invalid_packet_wrong_type(pkt_type=4, size_words=8)
    pcap_file = tmp_path / "context_only.pcap"
    pcap_file.write_bytes(_build_pcap([context_pkt, context_pkt]))
    output_file = tmp_path / "out.cs8"

    extractor = IQExtractor()
    with pytest.raises(NoValidPacketsError):
        extractor.extract(str(pcap_file), str(output_file))


def test_single_valid_packet_accepted(tmp_path) -> None:
    """A pcap with exactly one valid Signal Data packet is accepted."""
    valid_pkt = _vita49_signal_data_packet(seq_num=0, n_payload_words=2)
    pcap_file = tmp_path / "one_valid.pcap"
    pcap_file.write_bytes(_build_pcap([valid_pkt]))
    output_file = tmp_path / "out.cs8"

    extractor = IQExtractor()
    result = extractor.extract(str(pcap_file), str(output_file))
    assert result.valid_packets == 1
    assert output_file.stat().st_size == 2 * 4  # 2 payload words × 4 bytes


def test_valid_packet_after_garbage_is_accepted(tmp_path) -> None:
    """A valid packet that follows garbage bytes is still accepted."""
    garbage = _vita49_garbage_bytes(32)
    valid_pkt = _vita49_signal_data_packet(seq_num=0, n_payload_words=2)
    # Garbage packet: IQExtractor will fail to parse and stop at the garbage UDP payload,
    # then move to the next UDP payload which contains the valid packet.
    pcap_file = tmp_path / "garbage_then_valid.pcap"
    pcap_file.write_bytes(_build_pcap([garbage, valid_pkt]))
    output_file = tmp_path / "out.cs8"

    extractor = IQExtractor()
    result = extractor.extract(str(pcap_file), str(output_file))
    assert result.valid_packets >= 1
