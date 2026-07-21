"""Deep analysis of VITA-49 packets from AWS Ground Station pcap.

Focuses on understanding the exact data format:
- Packet header fields (stream ID, timestamp, etc.)
- Context packet structure
- I/Q sample format (bit depth, interleaving, endianness)
"""

import json
import struct
import time
from collections import defaultdict
from io import BytesIO

import boto3

INPUT_BUCKET = "aws-groundstation-demo-reception-471112743408"
S3_KEY = (
    "year=2026/month=06/day=19/satellite=33f035e1-73f7-47a5-9df8-fbc48636dca8/"
    "c14d25d6-d69c-4d9f-a255-85908ab17c13_20260619T114459Z_"
    "c6b27c0f-bf52-4bd9-b163-edda2afd83c2.pcap"
)


def main():
    session = boto3.Session(profile_name="AWSAdminAccess-471112743408", region_name="eu-central-1")
    s3 = session.client("s3")

    # Download first 10 MB
    print("Downloading first 10 MB...")
    response = s3.get_object(Bucket=INPUT_BUCKET, Key=S3_KEY, Range="bytes=0-10485759")
    data = response["Body"].read()

    # Skip pcap global header (24 bytes)
    offset = 24
    packets = []
    
    while offset + 16 <= len(data) and len(packets) < 200:
        ts_sec, ts_usec, cap_len, orig_len = struct.unpack("<IIII", data[offset:offset + 16])
        frame = data[offset + 16:offset + 16 + cap_len]
        offset += 16 + cap_len
        
        # Skip Ethernet (14) + IP header, extract UDP payload
        if len(frame) < 42:
            continue
        # IP header length
        ihl = (frame[14] & 0x0F) * 4
        udp_start = 14 + ihl
        # UDP payload starts after 8-byte UDP header
        vita_payload = frame[udp_start + 8:]
        if vita_payload:
            packets.append((ts_sec, ts_usec, vita_payload))

    print(f"Parsed {len(packets)} packets from first 10 MB\n")

    # Analyze packet types
    type_counts = defaultdict(int)
    type_examples = {}
    
    for ts_sec, ts_usec, pkt in packets:
        if len(pkt) < 4:
            continue
        h = int.from_bytes(pkt[:4], "big")
        pkt_type = (h >> 28) & 0xF
        type_counts[pkt_type] += 1
        if pkt_type not in type_examples:
            type_examples[pkt_type] = (ts_sec, ts_usec, pkt)

    print("=== PACKET TYPE DISTRIBUTION ===")
    TYPE_NAMES = {
        0: "IF Data (no stream ID)",
        1: "IF Data (with stream ID)",
        2: "Extension Data (no stream ID)",
        3: "Extension Data (with stream ID)",
        4: "IF Context",
        5: "Extension Context",
    }
    for t, count in sorted(type_counts.items()):
        print(f"  Type {t} ({TYPE_NAMES.get(t, 'Unknown')}): {count} packets")

    # Detailed header analysis for each type
    for pkt_type, (ts_sec, ts_usec, pkt) in sorted(type_examples.items()):
        print(f"\n{'='*60}")
        print(f"=== TYPE {pkt_type} ({TYPE_NAMES.get(pkt_type, 'Unknown')}) — DETAILED ===")
        print(f"{'='*60}")
        
        h = int.from_bytes(pkt[:4], "big")
        
        # VITA-49 header word breakdown
        ptype = (h >> 28) & 0xF
        c_bit = (h >> 27) & 0x1  # Class ID present
        t_bit = (h >> 26) & 0x1  # Trailer present  
        # For IF Data packets: TSI and TSF
        tsi = (h >> 22) & 0x3  # Integer timestamp
        tsf = (h >> 20) & 0x3  # Fractional timestamp
        pkt_count = (h >> 16) & 0xF
        pkt_size = h & 0xFFFF  # in 32-bit words
        
        print(f"  Header word: 0x{h:08X}")
        print(f"  Packet type: {ptype}")
        print(f"  Class ID present: {bool(c_bit)}")
        print(f"  Trailer present: {bool(t_bit)}")
        print(f"  TSI (int timestamp): {tsi} ({'none' if tsi==0 else 'UTC' if tsi==1 else 'GPS' if tsi==2 else 'other'})")
        print(f"  TSF (frac timestamp): {tsf} ({'none' if tsf==0 else 'sample count' if tsf==1 else 'real-time ps' if tsf==2 else 'free-running'})")
        print(f"  Packet count: {pkt_count}")
        print(f"  Packet size: {pkt_size} words = {pkt_size * 4} bytes")
        print(f"  Actual payload: {len(pkt)} bytes")

        # Parse further based on type
        word_offset = 1  # Already read word 0 (header)
        
        # Stream ID (present for types 1, 3, 4, 5)
        if pkt_type in (1, 3, 4, 5) and len(pkt) >= 8:
            stream_id = struct.unpack("!I", pkt[4:8])[0]
            print(f"  Stream ID: 0x{stream_id:08X}")
            word_offset = 2
        
        # Class ID (if c_bit set) — 2 words
        if c_bit and len(pkt) >= (word_offset + 2) * 4:
            class_oui = struct.unpack("!I", pkt[word_offset*4:(word_offset+1)*4])[0] & 0x00FFFFFF
            class_codes = struct.unpack("!I", pkt[(word_offset+1)*4:(word_offset+2)*4])[0]
            info_class = (class_codes >> 16) & 0xFFFF
            pkt_class = class_codes & 0xFFFF
            print(f"  Class OUI: 0x{class_oui:06X}")
            print(f"  Info Class Code: 0x{info_class:04X}")
            print(f"  Packet Class Code: 0x{pkt_class:04X}")
            word_offset += 2
        
        # Integer timestamp (if TSI != 0)
        if tsi != 0 and len(pkt) >= (word_offset + 1) * 4:
            int_ts = struct.unpack("!I", pkt[word_offset*4:(word_offset+1)*4])[0]
            print(f"  Integer Timestamp: {int_ts} (epoch seconds)")
            word_offset += 1
        
        # Fractional timestamp (if TSF != 0) — 2 words (64-bit)
        if tsf != 0 and len(pkt) >= (word_offset + 2) * 4:
            frac_ts = struct.unpack("!Q", pkt[word_offset*4:(word_offset+2)*4])[0]
            if tsf == 2:
                print(f"  Fractional Timestamp: {frac_ts} picoseconds")
            else:
                print(f"  Fractional Timestamp: {frac_ts}")
            word_offset += 2
        
        # Data payload starts at word_offset
        data_start = word_offset * 4
        data_payload = pkt[data_start:]
        print(f"  Data payload offset: {data_start} bytes")
        print(f"  Data payload size: {len(data_payload)} bytes")
        
        if pkt_type in (1, 0) and len(data_payload) >= 16:
            # Show first few samples — likely 16-bit I/Q interleaved
            print(f"  First 32 bytes of I/Q data: {data_payload[:32].hex()}")
            # Try interpreting as 16-bit signed integers (common for DigIF)
            samples_16 = struct.unpack(f">{len(data_payload[:32])//2}h", data_payload[:32])
            print(f"  As 16-bit signed (big-endian): {list(samples_16)}")
            samples_16_le = struct.unpack(f"<{len(data_payload[:32])//2}h", data_payload[:32])
            print(f"  As 16-bit signed (little-endian): {list(samples_16_le)}")
            # 8-bit
            samples_8 = struct.unpack(f"{len(data_payload[:16])}b", data_payload[:16])
            print(f"  As 8-bit signed: {list(samples_8)}")

        elif pkt_type == 4:
            # Context packet — dump all fields
            print(f"  Context data ({len(data_payload)} bytes):")
            print(f"    Hex: {data_payload[:64].hex()}")
            # Try parsing CIF (Context Indicator Field)
            if len(data_payload) >= 4:
                cif = struct.unpack("!I", data_payload[:4])[0]
                print(f"    CIF: 0x{cif:08X}")
                print(f"      Bandwidth: {'yes' if cif & (1<<29) else 'no'}")
                print(f"      IF Ref Freq: {'yes' if cif & (1<<28) else 'no'}")
                print(f"      RF Ref Freq: {'yes' if cif & (1<<27) else 'no'}")
                print(f"      Sample Rate: {'yes' if cif & (1<<21) else 'no'}")
                print(f"      Gain: {'yes' if cif & (1<<23) else 'no'}")

    # Summary
    print(f"\n{'='*60}")
    print("=== SUMMARY ===")
    print(f"  Total packets in first 10 MB: {len(packets)}")
    print(f"  Data (IQ) packets: {type_counts.get(1, 0) + type_counts.get(0, 0)}")
    print(f"  Context packets: {type_counts.get(4, 0) + type_counts.get(5, 0)}")
    
    # Estimate full file content
    file_size_gb = 2.18
    iq_per_pkt = 1408  # approximate based on 1472 - 28 header - 36 vita hdr
    pkts_per_mb = (1024 * 1024) / 1514  # pcap records per MB
    total_iq_gb = (file_size_gb * 1024 * pkts_per_mb * iq_per_pkt) / (1024**3)
    print(f"\n  Estimated total I/Q data in file: ~{total_iq_gb:.1f} GB")
    print(f"  At 30 MHz BW, 16-bit I/Q: sample rate = 30 MSps complex")
    print(f"  30 MSps × 2 (I+Q) × 2 bytes × 30 sec = ~3.6 GB per file (expected)")


if __name__ == "__main__":
    main()
