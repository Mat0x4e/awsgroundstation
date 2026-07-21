"""Integration test: IQ Extractor end-to-end with synthetic pcap data.

Generates a synthetic .pcap file containing valid VITA-49 Signal Data packets
(and one Context packet with the correct sample rate), then runs IQExtractor
and verifies the .cs8 output is correct.

Validates: Requirements 1.1, 1.7
"""

import struct
from pathlib import Path

import pytest

from scripts.iq_extract import ExtractionResult, IQExtractor, NoValidPacketsError


# ---------------------------------------------------------------------------
# Helper: build synthetic pcap with VITA-49 packets
# ---------------------------------------------------------------------------

SAMPLE_RATE_HZ = 34_312_500


def _build_pcap_global_header() -> bytes:
    """Build a 24-byte pcap global header (little-endian, Ethernet link type)."""
    return struct.pack(
        "<IHHIIII",
        0xA1B2C3D4,  # magic
        2, 4,        # version major, minor
        0, 0,        # timezone, sigfigs
        65535,       # snaplen
        1,           # link type = Ethernet
    )


def _build_vita49_context_packet(sample_rate_hz: int = SAMPLE_RATE_HZ) -> bytes:
    """Build a VITA-49 Context packet (type 4) declaring the sample rate.

    The sample rate is stored at word offset 8 (byte 32) as a 64-bit
    big-endian radix-20 fixed-point integer.
    """
    # Encode sample rate as radix-20 fixed point (unsigned 64-bit)
    rate_bits = sample_rate_hz << 20

    # Context packet: type=4, seq=0, size=10 words (40 bytes)
    # Header word: type(4)=0100 in top nibble -> 0x4, seq=0, size=10
    size_words = 10
    header_word = (4 << 28) | (0 << 16) | size_words
    header = struct.pack("!I", header_word)

    # Pad 6 words (24 bytes) between header word and rate field (words 1-6)
    # Then 1 word CIF indicator (word 7), then rate at word 8
    padding = b"\x00" * 28  # 7 words of padding to reach byte offset 32

    # Sample rate: 8 bytes at word offset 8
    rate_field = struct.pack("!Q", rate_bits)

    # Total should be 10 words = 40 bytes
    # header(4) + padding(28) + rate(8) = 40 ✓
    return header + padding + rate_field


def _build_vita49_signal_packet(seq_num: int, iq_payload: bytes) -> bytes:
    """Build a VITA-49 Signal Data packet (type 1) with given payload.

    Header is 7 words (28 bytes). Payload must be padded to 4-byte alignment.
    """
    # Pad payload to 4-byte boundary
    pad_len = (4 - (len(iq_payload) % 4)) % 4
    padded_payload = iq_payload + b"\x00" * pad_len

    payload_words = len(padded_payload) // 4
    size_words = 7 + payload_words  # 7 header words + payload words

    # Header word: type=1, seq_num in bits 19-16, size in bits 15-0
    header_word = (1 << 28) | ((seq_num & 0xF) << 16) | (size_words & 0xFFFF)
    header = struct.pack("!I", header_word)

    # Remaining 6 header words (24 bytes) — timestamps etc., zeroed
    remaining_header = b"\x00" * 24

    return header + remaining_header + padded_payload


def _wrap_in_ethernet_ip_udp(vita49_data: bytes) -> bytes:
    """Wrap VITA-49 data in Ethernet + IPv4 + UDP headers."""
    # UDP header (8 bytes)
    udp_length = 8 + len(vita49_data)
    udp_header = struct.pack("!HHHH", 4991, 4991, udp_length, 0)

    # IPv4 header (20 bytes)
    ip_total_length = 20 + udp_length
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,             # version=4, IHL=5
        0,                # TOS
        ip_total_length,  # total length
        0,                # identification
        0,                # flags + fragment offset (no fragmentation)
        64,               # TTL
        17,               # protocol = UDP
        0,                # checksum (not validated)
        b"\x0A\x00\x00\x01",  # src IP
        b"\x0A\x00\x00\x02",  # dst IP
    )

    # Ethernet header (14 bytes)
    eth_header = (
        b"\x00\x11\x22\x33\x44\x55"  # dst MAC
        b"\x66\x77\x88\x99\xAA\xBB"  # src MAC
        + struct.pack("!H", 0x0800)   # ethertype IPv4
    )

    return eth_header + ip_header + udp_header + vita49_data


def _build_pcap_record(frame: bytes, ts_sec: int = 1718806980) -> bytes:
    """Wrap an Ethernet frame in a pcap record header."""
    cap_len = len(frame)
    return struct.pack("<IIII", ts_sec, 0, cap_len, cap_len) + frame


def generate_synthetic_pcap(
    num_signal_packets: int = 10,
    samples_per_packet: int = 512,
    include_context: bool = True,
    gap_at: int | None = None,
) -> tuple[bytes, list[bytes]]:
    """Generate a complete synthetic pcap with VITA-49 packets.

    Args:
        num_signal_packets: Number of Signal Data packets to include.
        samples_per_packet: Number of I/Q sample pairs per packet.
        include_context: Whether to include a Context packet with sample rate.
        gap_at: If set, skip this sequence number to create a gap.

    Returns:
        Tuple of (pcap_bytes, list_of_iq_payloads) where the payloads list
        contains the raw bytes written into each Signal Data packet.
    """
    pcap_data = _build_pcap_global_header()
    iq_payloads: list[bytes] = []

    # Optionally add a Context packet first
    if include_context:
        ctx_pkt = _build_vita49_context_packet(SAMPLE_RATE_HZ)
        frame = _wrap_in_ethernet_ip_udp(ctx_pkt)
        pcap_data += _build_pcap_record(frame)

    # Add Signal Data packets
    seq = 0
    for i in range(num_signal_packets):
        if gap_at is not None and seq == gap_at:
            seq = (seq + 1) % 16  # skip one sequence number

        # Generate deterministic I/Q payload
        # Each sample is 2 bytes (1 byte I + 1 byte Q), using signed int8
        payload = bytes(
            ((i * samples_per_packet + s) % 256) for s in range(samples_per_packet * 2)
        )
        iq_payloads.append(payload)

        pkt = _build_vita49_signal_packet(seq, payload)
        frame = _wrap_in_ethernet_ip_udp(pkt)
        pcap_data += _build_pcap_record(frame)

        seq = (seq + 1) % 16

    return pcap_data, iq_payloads


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestIQExtractorIntegration:
    """End-to-end integration tests for the IQ extraction pipeline."""

    def test_extraction_produces_valid_cs8_output(self, tmp_path: Path):
        """IQ extraction from synthetic pcap produces a valid .cs8 file.

        Validates Requirements 1.1 (pcap → .cs8 extraction) and 1.7 (completes
        without timeout).
        """
        num_packets = 10
        samples_per_packet = 512

        pcap_bytes, expected_payloads = generate_synthetic_pcap(
            num_signal_packets=num_packets,
            samples_per_packet=samples_per_packet,
        )

        pcap_path = tmp_path / "test_input.pcap"
        pcap_path.write_bytes(pcap_bytes)

        output_path = tmp_path / "output.cs8"

        # Run extraction
        extractor = IQExtractor()
        result = extractor.extract(str(pcap_path), str(output_path))

        # Verify output file exists and is non-empty
        assert output_path.exists(), "Output .cs8 file was not created"
        assert output_path.stat().st_size > 0, "Output .cs8 file is empty"

        # Verify output size matches expected payload concatenation
        expected_size = sum(len(p) for p in expected_payloads)
        assert output_path.stat().st_size == expected_size, (
            f"Output size {output_path.stat().st_size} != expected {expected_size}"
        )

        # Verify output content matches concatenated payloads
        actual_content = output_path.read_bytes()
        expected_content = b"".join(expected_payloads)
        assert actual_content == expected_content, "Output content does not match expected I/Q data"

    def test_extraction_result_metrics_are_sensible(self, tmp_path: Path):
        """ExtractionResult metrics contain sensible values after extraction.

        Validates Requirements 1.1, 1.7.
        """
        num_packets = 15
        samples_per_packet = 256

        pcap_bytes, _ = generate_synthetic_pcap(
            num_signal_packets=num_packets,
            samples_per_packet=samples_per_packet,
        )

        pcap_path = tmp_path / "metrics_test.pcap"
        pcap_path.write_bytes(pcap_bytes)
        output_path = tmp_path / "metrics_output.cs8"

        extractor = IQExtractor()
        result = extractor.extract(str(pcap_path), str(output_path))

        # Verify result type
        assert isinstance(result, ExtractionResult)

        # Verify packet counts
        assert result.valid_packets == num_packets
        assert result.total_packets >= num_packets  # includes context packet
        assert result.total_packets == num_packets + 1  # +1 for context packet

        # Verify gap detection (no gaps in this test)
        assert result.gaps_detected == 0
        assert result.zeros_inserted == 0

        # Verify sample rate validation
        assert result.sample_rate_valid is True

        # Verify duration is positive and sensible
        assert result.duration_seconds > 0
        expected_duration = (num_packets * samples_per_packet) / SAMPLE_RATE_HZ
        assert abs(result.duration_seconds - expected_duration) < 1e-4

        # Verify output path
        assert result.output_path == str(output_path)

    def test_extraction_with_gap_inserts_zeros(self, tmp_path: Path):
        """When a sequence number gap exists, zeros are inserted in output.

        Validates Requirement 1.4 (gap detection and zero-fill).
        """
        num_packets = 8
        samples_per_packet = 128

        pcap_bytes, expected_payloads = generate_synthetic_pcap(
            num_signal_packets=num_packets,
            samples_per_packet=samples_per_packet,
            gap_at=3,  # Skip sequence number 3
        )

        pcap_path = tmp_path / "gap_test.pcap"
        pcap_path.write_bytes(pcap_bytes)
        output_path = tmp_path / "gap_output.cs8"

        extractor = IQExtractor()
        result = extractor.extract(str(pcap_path), str(output_path))

        # Should detect exactly one gap
        assert result.gaps_detected == 1

        # Zero bytes inserted = 1 missing packet × samples_per_packet × 2 bytes
        expected_zeros = 1 * samples_per_packet * 2
        assert result.zeros_inserted == expected_zeros

        # Output file size = payloads + zeros
        expected_size = sum(len(p) for p in expected_payloads) + expected_zeros
        assert output_path.stat().st_size == expected_size

    def test_extraction_rejects_empty_pcap(self, tmp_path: Path):
        """A pcap with no valid VITA-49 packets raises NoValidPacketsError.

        Validates Requirement 1.5.
        """
        # Build a pcap with only the global header (no packets)
        pcap_bytes = _build_pcap_global_header()

        pcap_path = tmp_path / "empty.pcap"
        pcap_path.write_bytes(pcap_bytes)
        output_path = tmp_path / "empty_output.cs8"

        extractor = IQExtractor()
        with pytest.raises(NoValidPacketsError):
            extractor.extract(str(pcap_path), str(output_path))

    def test_extraction_without_context_packet(self, tmp_path: Path):
        """Extraction succeeds without a Context packet (sample rate unknown).

        Validates Requirement 1.6 (accepts file with at least one valid packet).
        """
        num_packets = 5
        samples_per_packet = 64

        pcap_bytes, expected_payloads = generate_synthetic_pcap(
            num_signal_packets=num_packets,
            samples_per_packet=samples_per_packet,
            include_context=False,
        )

        pcap_path = tmp_path / "no_context.pcap"
        pcap_path.write_bytes(pcap_bytes)
        output_path = tmp_path / "no_context_output.cs8"

        extractor = IQExtractor()
        result = extractor.extract(str(pcap_path), str(output_path))

        # Extraction should succeed
        assert result.valid_packets == num_packets
        assert result.sample_rate_valid is False  # no context → can't validate rate
        assert output_path.exists()
        assert output_path.stat().st_size == sum(len(p) for p in expected_payloads)
