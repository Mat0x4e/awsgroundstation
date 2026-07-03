"""I/Q sample extractor for NOAA-20 DigIF → SDR pipeline.

Extracts raw Complex 8-bit signed (cs8) I/Q samples from VITA-49 packets
embedded in .pcap files delivered by AWS Ground Station.

Processing chain:
    .pcap → Ethernet → IP (with fragment reassembly) → UDP → VITA-49 → .cs8

The output .cs8 file is consumed by SatDump as the first step in:
    .cs8 → SatDump → RT-STPS → CSPP SDR

Usage:
    python iq_extract.py <pcap_path> <output_path>

VITA-49 Header Word Layout (first 32-bit word, big-endian):
    Bits 31-28: Packet Type (1=Signal Data, 4=Context)
    Bits 27-26: TSI  (Timestamp Integer type)
    Bits 25-24: TSF  (Timestamp Fractional type)
    Bit  23:    C    (Class ID present)
    Bit  22:    T    (Trailer present)
    Bits 21-20: Reserved
    Bits 19-16: Packet Count (4-bit modulo-16 sequence number)
    Bits 15-0:  Packet Size  (in 32-bit words, including header)
"""

import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class NoValidPacketsError(Exception):
    """Raised when the pcap contains zero valid VITA-49 Signal Data packets."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VITA49Header:
    """Parsed fields from the first 32-bit word of a VITA-49 packet.

    Attributes:
        packet_type:   4-bit type field (1=Signal Data, 4=Context).
        seq_num:       4-bit modulo-16 packet counter (bits 19:16).
        size_words:    Packet size in 32-bit words (includes header word).
        size_bytes:    Packet size in bytes (size_words × 4).
        has_class_id:  True if the Class ID field is present (bit 23).
        has_trailer:   True if the Trailer field is present (bit 22).
        raw_word:      The original 32-bit header word (for debugging).
    """
    packet_type: int
    seq_num: int
    size_words: int
    size_bytes: int
    has_class_id: bool
    has_trailer: bool
    raw_word: int


@dataclass
class ExtractionResult:
    """Summary of a completed I/Q extraction run.

    Attributes:
        output_path:      Path to the written .cs8 file.
        total_packets:    Total VITA-49 packets encountered (all types).
        valid_packets:    Signal Data packets (type 1) with valid payloads.
        gaps_detected:    Number of sequence discontinuities found.
        zeros_inserted:   Total zero bytes written to fill gaps.
        sample_rate_valid: True if a context packet declared a sample rate
                           within ±1 Hz of 34 312 500 Hz.
        duration_seconds: Estimated signal duration (valid_packets ×
                          samples_per_packet / sample_rate).
    """
    output_path: str
    total_packets: int
    valid_packets: int
    gaps_detected: int
    zeros_inserted: int
    sample_rate_valid: bool
    duration_seconds: float


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

class IQExtractor:
    """Extracts raw I/Q samples from VITA-49 packets in pcap files.

    Packet pipeline per call to :meth:`extract`:
        1. Load the entire pcap into memory (performance: ~2.18 GB fits in RAM).
        2. Parse pcap global header → validate Ethernet link type.
        3. Iterate pcap records → parse Ethernet → IPv4 frames.
        4. Reassemble IP fragments by IP ID before VITA-49 parsing.
        5. Strip the 8-byte UDP header from each reassembled datagram.
        6. Parse VITA-49 packets from the UDP payload.
        7. Accumulate I/Q payloads from Signal Data packets (type 1).
        8. Insert zero-filled gaps wherever sequence numbers are
           discontinuous (modulo-16 arithmetic).
        9. Write the result to a binary .cs8 file.

    Thread safety:
        Instances are stateless between calls; :meth:`extract` is safe to
        call from multiple threads on different (path, output) pairs.
    """

    EXPECTED_SAMPLE_RATE: int = 34_312_500  # Hz (NOAA-20 DigIF)
    SAMPLE_RATE_TOLERANCE: int = 1           # Hz

    # VITA-49 Signal Data header is 7 words (28 bytes); I/Q payload follows.
    _VITA49_DATA_HEADER_WORDS: int = 7

    def extract(self, pcap_path: str, output_path: str) -> ExtractionResult:
        """Extract I/Q samples from a pcap file and write a .cs8 output file.

        Parses the full pcap → Ethernet → IP (with fragment reassembly) →
        UDP → VITA-49 stack.  Extracts the raw payload bytes from every
        Signal Data packet (type 1) in sequential order, inserting zero
        padding wherever sequence number gaps are detected.

        The sample rate is read from the first Context packet (type 4) found
        and validated against :attr:`EXPECTED_SAMPLE_RATE`.

        Args:
            pcap_path:   Path to the input .pcap file.
            output_path: Path for the output .cs8 file (created/overwritten).

        Returns:
            :class:`ExtractionResult` with extraction statistics.

        Raises:
            NoValidPacketsError: If zero VITA-49 Signal Data packets are found.
            FileNotFoundError:   If *pcap_path* does not exist.
            ValueError:          If the pcap link type is not Ethernet (1).
        """
        # ------------------------------------------------------------------
        # 1. Load pcap into memory
        # ------------------------------------------------------------------
        with open(pcap_path, "rb") as fh:
            data = fh.read()

        # ------------------------------------------------------------------
        # 2. Parse pcap global header (24 bytes)
        # ------------------------------------------------------------------
        if len(data) < 24:
            raise ValueError(f"File too short to be a valid pcap: {len(data)} bytes")

        _magic, _vmaj, _vmin, _tz, _ts_acc, _snap_len, link_type = struct.unpack(
            "<IHHIIII", data[:24]
        )
        if link_type != 1:
            raise ValueError(
                f"Expected Ethernet link type (1), got {link_type}. "
                "Only standard Ethernet pcaps are supported."
            )

        # ------------------------------------------------------------------
        # 3 & 4. Iterate pcap records, parse Ethernet/IP, reassemble fragments
        # ------------------------------------------------------------------
        udp_payloads = self._reassemble_udp_payloads(data)

        # ------------------------------------------------------------------
        # 5 & 6. Parse VITA-49 packets from UDP payloads
        # ------------------------------------------------------------------
        (
            iq_chunks,
            seq_numbers,
            total_packets,
            sample_rate,
        ) = self._parse_vita49_stream(udp_payloads)

        if not iq_chunks:
            raise NoValidPacketsError(
                f"No valid VITA-49 Signal Data packets found in {pcap_path}"
            )

        # ------------------------------------------------------------------
        # 7. Validate sample rate
        # ------------------------------------------------------------------
        sample_rate_valid = self._validate_sample_rate(sample_rate) if sample_rate is not None else False

        # ------------------------------------------------------------------
        # 8. Detect gaps and build final byte stream
        # ------------------------------------------------------------------
        samples_per_packet = len(iq_chunks[0]) // 2  # 2 bytes per sample (1I + 1Q)
        gaps_detected = 0
        zeros_inserted = 0

        # ------------------------------------------------------------------
        # 9. Write output file, inserting zero padding at gap points
        # ------------------------------------------------------------------
        # A single forward pass mirrors _detect_gaps logic and writes output
        # in one shot: write each chunk, and before it write zeros if the
        # sequence number jumped (gap between chunk[i-1] and chunk[i]).
        with open(output_path, "wb") as out:
            for i, chunk in enumerate(iq_chunks):
                if i > 0:
                    expected_seq = (seq_numbers[i - 1] + 1) % 16
                    actual_seq = seq_numbers[i]
                    if actual_seq != expected_seq:
                        gap_length = (actual_seq - expected_seq) % 16
                        zero_bytes = gap_length * samples_per_packet * 2
                        out.write(b"\x00" * zero_bytes)
                        zeros_inserted += zero_bytes
                        gaps_detected += 1
                out.write(chunk)

        # ------------------------------------------------------------------
        # Compute duration
        # ------------------------------------------------------------------
        effective_rate = sample_rate if (sample_rate and sample_rate > 0) else self.EXPECTED_SAMPLE_RATE
        valid_packets = len(iq_chunks)
        total_samples = valid_packets * samples_per_packet
        duration_seconds = total_samples / effective_rate

        return ExtractionResult(
            output_path=output_path,
            total_packets=total_packets,
            valid_packets=valid_packets,
            gaps_detected=gaps_detected,
            zeros_inserted=zeros_inserted,
            sample_rate_valid=sample_rate_valid,
            duration_seconds=round(duration_seconds, 6),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reassemble_udp_payloads(self, data: bytes) -> list[bytes]:
        """Parse pcap records, reassemble IP fragments, yield UDP payloads.

        Each entry in the returned list is the UDP payload (i.e., the bytes
        immediately following the 8-byte UDP header) from one reassembled
        IP datagram.  Datagrams whose protocol is not UDP (17) are skipped.

        Fragment reassembly uses the IP Identification field as the key.
        Fragments are collected in a ``defaultdict(dict)`` keyed by
        ``{ip_id: {frag_offset: ip_payload_fragment}}``.  Reassembly is
        triggered when the fragment with MF=0 arrives.  Incomplete
        reassembly (missing earlier fragments) is silently skipped.

        Args:
            data: The entire pcap file contents as bytes.

        Returns:
            List of UDP payload byte strings, one per reassembled datagram.
        """
        offset = 24  # skip 24-byte pcap global header
        ip_fragments: dict[int, dict[int, bytes]] = defaultdict(dict)
        udp_payloads: list[bytes] = []

        while offset + 16 <= len(data):
            # Pcap record header: ts_sec(4) ts_usec(4) cap_len(4) orig_len(4)
            ts_sec, ts_usec, cap_len, orig_len = struct.unpack(
                "<IIII", data[offset : offset + 16]
            )
            rec_start = offset + 16
            rec_end = rec_start + cap_len
            if rec_end > len(data):
                break  # truncated record at end of file
            frame = data[rec_start:rec_end]
            offset = rec_end

            # ----------------------------------------------------------
            # Ethernet header: dst[6] + src[6] + ethertype[2] = 14 bytes
            # ----------------------------------------------------------
            if len(frame) < 14:
                continue
            ethertype = struct.unpack("!H", frame[12:14])[0]
            if ethertype != 0x0800:  # Not IPv4
                continue

            # ----------------------------------------------------------
            # IPv4 header
            # ----------------------------------------------------------
            ip_hdr = frame[14:]
            if len(ip_hdr) < 20:
                continue

            ihl = (ip_hdr[0] & 0x0F) * 4          # Internet Header Length
            total_len = struct.unpack("!H", ip_hdr[2:4])[0]
            ip_id = struct.unpack("!H", ip_hdr[4:6])[0]
            flags_frag = struct.unpack("!H", ip_hdr[6:8])[0]
            mf = (flags_frag >> 13) & 0x1          # More Fragments flag
            frag_offset = (flags_frag & 0x1FFF) * 8  # fragment byte offset
            protocol = ip_hdr[9]

            if protocol != 17:  # 17 = UDP
                continue

            # ip_payload is the portion of the IP packet after the IP header,
            # up to total_len (clips any Ethernet padding).
            ip_payload = ip_hdr[ihl : total_len]

            if frag_offset == 0 and mf == 0:
                # Unfragmented datagram — strip 8-byte UDP header directly
                if len(ip_payload) > 8:
                    udp_payloads.append(ip_payload[8:])

            else:
                # Fragment — accumulate by IP ID
                ip_fragments[ip_id][frag_offset] = ip_payload

                if mf == 0:
                    # Last fragment received; attempt reassembly
                    frags = ip_fragments[ip_id]
                    sorted_offsets = sorted(frags)
                    reassembled = b""
                    complete = True
                    expected_next = 0
                    for fo in sorted_offsets:
                        if fo != expected_next:
                            complete = False  # gap in fragments — discard
                            break
                        reassembled += frags[fo]
                        expected_next = fo + len(frags[fo])

                    if complete and len(reassembled) > 8:
                        # Strip UDP header from the start of the first fragment
                        udp_payloads.append(reassembled[8:])

                    del ip_fragments[ip_id]

        return udp_payloads

    def _parse_vita49_stream(
        self, udp_payloads: list[bytes]
    ) -> tuple[list[bytes], list[int], int, int | None]:
        """Parse VITA-49 packets from a list of UDP payloads.

        Iterates over every UDP payload, extracting consecutive VITA-49
        packets (a single payload may contain more than one VITA-49 packet).

        Signal Data packets (type 1):
            The header is 7 words (28 bytes).  The I/Q payload is everything
            after the header, up to the packet boundary.

        Context packets (type 4):
            The sample rate is at word offset 8 (bytes 32–39) as a 64-bit
            big-endian integer in radix-20 fixed-point format.
            Only the first valid sample rate is stored.

        Args:
            udp_payloads: List of raw UDP payload bytes (post-reassembly).

        Returns:
            A 4-tuple:
                iq_chunks:     List of raw I/Q byte payloads, one per
                               Signal Data packet.
                seq_numbers:   4-bit sequence numbers in encounter order,
                               parallel to *iq_chunks*.
                total_packets: Count of all VITA-49 packets parsed (all types).
                sample_rate:   Integer Hz from the first valid Context packet,
                               or ``None`` if no Context packet was seen.
        """
        iq_chunks: list[bytes] = []
        seq_numbers: list[int] = []
        total_packets = 0
        sample_rate: int | None = None

        header_bytes = self._VITA49_DATA_HEADER_WORDS * 4  # 28 bytes

        for udp_payload in udp_payloads:
            pkt_offset = 0
            while pkt_offset + 4 <= len(udp_payload):
                header = self._parse_vita49_header(
                    udp_payload[pkt_offset : pkt_offset + 4]
                )
                if header.size_bytes == 0 or pkt_offset + header.size_bytes > len(udp_payload):
                    break  # malformed or truncated packet — stop parsing this payload

                pkt_data = udp_payload[pkt_offset : pkt_offset + header.size_bytes]
                total_packets += 1

                if header.packet_type == 1:
                    # Signal Data — I/Q payload follows the 7-word header
                    if len(pkt_data) > header_bytes:
                        payload = pkt_data[header_bytes:]
                        iq_chunks.append(payload)
                        seq_numbers.append(header.seq_num)

                elif header.packet_type == 4 and sample_rate is None:
                    # Context packet — extract sample rate from word offset 8
                    # Byte offset: 7 words (fixed header) = 28 bytes,
                    # then 1 word CIF indicator = 32 bytes, then the rate
                    # field at word 8 = byte offset 32.
                    # The rate is a 64-bit signed fixed-point, radix 20.
                    rate_offset = 8 * 4  # word 8 = byte 32
                    if len(pkt_data) >= rate_offset + 8:
                        rate_bits = struct.unpack("!Q", pkt_data[rate_offset : rate_offset + 8])[0]
                        sample_rate = self._parse_vita49_sample_rate(rate_bits)

                pkt_offset += header.size_bytes

        return iq_chunks, seq_numbers, total_packets, sample_rate

    def _parse_vita49_header(self, header_bytes: bytes) -> VITA49Header:
        """Parse the first 4 bytes of a VITA-49 packet into a :class:`VITA49Header`.

        The single 32-bit big-endian word contains all required metadata:

        +--------+--------+--------+--------+
        | 31..28 | 27..24 | 23..20 | 19..16 |
        | type   | TSI/TSF| C T R R| seq_num|
        +--------+--------+--------+--------+
        | 15..0                             |
        | packet_size_in_words              |
        +-----------------------------------+

        Args:
            header_bytes: Exactly 4 bytes from the start of a VITA-49 packet.

        Returns:
            Populated :class:`VITA49Header` instance.
        """
        raw = int.from_bytes(header_bytes[:4], "big")
        pkt_type   = (raw >> 28) & 0xF
        seq_num    = (raw >> 16) & 0xF
        has_class  = bool((raw >> 23) & 0x1)
        has_trail  = bool((raw >> 22) & 0x1)
        size_words = raw & 0xFFFF

        return VITA49Header(
            packet_type=pkt_type,
            seq_num=seq_num,
            size_words=size_words,
            size_bytes=size_words * 4,
            has_class_id=has_class,
            has_trailer=has_trail,
            raw_word=raw,
        )

    def _validate_sample_rate(self, declared_rate: int) -> bool:
        """Return True if the declared sample rate is within tolerance.

        Acceptance criterion: |declared_rate - 34_312_500| ≤ 1 Hz.

        Args:
            declared_rate: Sample rate in Hz as parsed from a Context packet.

        Returns:
            True if within tolerance, False otherwise.
        """
        return abs(declared_rate - self.EXPECTED_SAMPLE_RATE) <= self.SAMPLE_RATE_TOLERANCE

    def _detect_gaps(self, seq_numbers: list[int]) -> list[tuple[int, int]]:
        """Detect discontinuities in a modulo-16 sequence number list.

        VITA-49 packet counts are 4-bit counters wrapping at 16.  A gap is
        any point where ``seq[i]`` is not ``(seq[i-1] + 1) % 16``.  The gap
        length is the number of missing packets between the two observed
        sequence numbers.

        Example:
            seq_numbers = [0, 1, 2, 5, 6]
            → gap at seq 3, length 3  (missing 3, 4, 5 before the 5)
            returns [(3, 3)]

        Args:
            seq_numbers: Ordered list of observed 4-bit sequence numbers.

        Returns:
            List of ``(gap_start, gap_length)`` tuples where *gap_start* is
            the first *missing* sequence number and *gap_length* is the count
            of missing packets.  Returns an empty list if no gaps exist.
        """
        gaps: list[tuple[int, int]] = []
        for i in range(1, len(seq_numbers)):
            expected = (seq_numbers[i - 1] + 1) % 16
            actual = seq_numbers[i]
            if actual != expected:
                # Compute gap length using modulo-16 arithmetic
                gap_length = (actual - expected) % 16
                gaps.append((expected, gap_length))
        return gaps

    # ------------------------------------------------------------------
    # Private utility
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_vita49_sample_rate(bits: int) -> int:
        """Convert a VITA-49 64-bit radix-20 fixed-point value to integer Hz.

        The VITA-49 standard encodes frequencies as signed 64-bit integers
        with an implicit binary point at position 20 (radix 20):
            value_hz = bits_signed / (1 << 20)

        Args:
            bits: Unsigned 64-bit integer read from the packet (big-endian).

        Returns:
            Sample rate in Hz, rounded to the nearest integer.
        """
        # Interpret as signed 64-bit
        if bits >= (1 << 63):
            bits -= (1 << 64)
        rate_float = bits / (1 << 20)
        return round(rate_float)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python iq_extract.py <pcap_path> <output_path>")
        sys.exit(1)

    pcap_path = sys.argv[1]
    output_path = sys.argv[2]

    print(f"Extracting I/Q samples from: {pcap_path}")
    t0 = time.time()

    extractor = IQExtractor()
    result = extractor.extract(pcap_path, output_path)

    elapsed = time.time() - t0
    print(f"Extraction complete ({elapsed:.1f}s): {result}")
