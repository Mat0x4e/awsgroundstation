"""VITA-49 extraction from AWS Ground Station .pcap files.

The .pcap contains Ethernet → IP → UDP → VITA-49 packets.
IP packets are fragmented across multiple Ethernet frames (1514 bytes each).
This script reassembles IP fragments, strips UDP headers, and parses VITA-49.

Steps:
1. Parse pcap global header
2. Read pcap records (each is an Ethernet frame)
3. Reassemble fragmented IP datagrams
4. Strip UDP header (8 bytes) from reassembled datagrams
5. Parse VITA-49 packets from the UDP payload
"""

import json
import struct
import sys
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

# Only process first N bytes of the pcap to get metadata + sample
MAX_READ_BYTES = 100 * 1024 * 1024  # 100 MB


def parse_vita_float(radix: int, bits: int) -> float:
    if bits >= (1 << 63):
        bits -= (1 << 64)
    return float(bits) / float(1 << radix)


def parse_vita_double(bits: int) -> float:
    return parse_vita_float(20, bits)


def parse_vita_f16(bits: int) -> float:
    if bits >= (1 << 15):
        bits -= (1 << 16)
    return parse_vita_float(7, bits)


def reassemble_and_extract(data: bytes):
    """Parse pcap, reassemble IP fragments, extract VITA-49 from UDP payloads."""

    # Pcap global header: 24 bytes
    magic, vmajor, vminor, tz, ts_acc, snap_len, link_type = struct.unpack(
        "<IHHIIII", data[:24]
    )
    assert link_type == 1, f"Expected Ethernet (1), got {link_type}"

    offset = 24
    ip_fragments = defaultdict(dict)  # id -> {offset: payload}
    vita49_packets = []
    datagrams_reassembled = 0
    pcap_records = 0

    while offset + 16 <= len(data):
        # Pcap record header
        ts_sec, ts_usec, cap_len, orig_len = struct.unpack(
            "<IIII", data[offset:offset + 16]
        )
        rec_start = offset + 16
        rec_end = rec_start + cap_len
        if rec_end > len(data):
            break
        frame = data[rec_start:rec_end]
        offset = rec_end
        pcap_records += 1

        # Ethernet header: 14 bytes (dst[6] + src[6] + ethertype[2])
        if len(frame) < 14:
            continue
        ethertype = struct.unpack("!H", frame[12:14])[0]
        if ethertype != 0x0800:  # Not IPv4
            continue

        # IPv4 header
        ip_hdr = frame[14:]
        if len(ip_hdr) < 20:
            continue
        ihl = (ip_hdr[0] & 0x0F) * 4
        total_len = struct.unpack("!H", ip_hdr[2:4])[0]
        ip_id = struct.unpack("!H", ip_hdr[4:6])[0]
        flags_frag = struct.unpack("!H", ip_hdr[6:8])[0]
        mf = (flags_frag >> 13) & 0x1  # More Fragments
        frag_offset = (flags_frag & 0x1FFF) * 8  # in bytes
        protocol = ip_hdr[9]

        if protocol != 17:  # Not UDP
            continue

        ip_payload = ip_hdr[ihl:total_len]

        if frag_offset == 0 and mf == 0:
            # Unfragmented datagram
            if len(ip_payload) > 8:
                udp_payload = ip_payload[8:]  # Strip UDP header
                vita49_packets.append(udp_payload)
                datagrams_reassembled += 1
        else:
            # Fragment — collect
            ip_fragments[ip_id][frag_offset] = ip_payload
            # Check if we have all fragments (last fragment has MF=0)
            if mf == 0:
                # We have the last fragment; try reassembly
                frags = ip_fragments[ip_id]
                # Sort by offset and concatenate
                sorted_offsets = sorted(frags.keys())
                reassembled = b""
                complete = True
                expected_next = 0
                for fo in sorted_offsets:
                    if fo != expected_next:
                        complete = False
                        break
                    reassembled += frags[fo]
                    expected_next = fo + len(frags[fo])

                if complete and len(reassembled) > 8:
                    udp_payload = reassembled[8:]  # Strip UDP header from first fragment
                    vita49_packets.append(udp_payload)
                    datagrams_reassembled += 1
                del ip_fragments[ip_id]

    return vita49_packets, pcap_records, datagrams_reassembled


def parse_vita49_packets(packets: list):
    """Parse VITA-49 signal data and context packets."""
    contexts = []
    data_packet_count = 0
    total_iq_bytes = 0
    iq_sample = BytesIO()
    max_iq_sample = 10 * 1024 * 1024  # 10 MB

    for pkt in packets:
        if len(pkt) < 4:
            continue

        h = int.from_bytes(pkt[:4], "big")
        pkt_type = (h >> 28) & 0xF
        pkt_size_words = h & 0xFFFF

        if pkt_type == 1:
            # Signal Data Packet
            payload = pkt[7 * 4:]  # Skip 7-word header
            data_packet_count += 1
            total_iq_bytes += len(payload)
            if iq_sample.tell() < max_iq_sample:
                to_write = min(len(payload), max_iq_sample - iq_sample.tell())
                iq_sample.write(payload[:to_write])

        elif pkt_type == 4:
            # Context Packet
            ctx_data = pkt[7 * 4:]
            if len(ctx_data) >= 100:
                try:
                    unpacked = struct.unpack("!LLQQQQLLQQLLQ", ctx_data[:100])
                    names = [
                        "cif", "rp", "bw", "ifref", "rfref", "ifoff",
                        "refl", "gain", "rate", "tsadj", "tscal", "indic",
                        "payloadfmt",
                    ]
                    ctx = dict(zip(names, unpacked))
                    ctx["bw_hz"] = parse_vita_double(ctx["bw"])
                    ctx["rf_ref_hz"] = parse_vita_double(ctx["rfref"])
                    ctx["if_ref_hz"] = parse_vita_double(ctx["ifref"])
                    ctx["if_offset_hz"] = parse_vita_double(ctx["ifoff"])
                    ctx["sample_rate_hz"] = parse_vita_double(ctx["rate"])
                    ctx["ref_level_dbm"] = parse_vita_f16(ctx["refl"])
                    ctx["bit_depth"] = ((ctx["payloadfmt"] >> 32) & 0x1F) + 1
                    ctx["ref_locked"] = bool(ctx["indic"] & (1 << 17))
                    contexts.append(ctx)
                except struct.error:
                    pass

    iq_sample.seek(0)
    return contexts, data_packet_count, total_iq_bytes, iq_sample.read()


def main():
    session = boto3.Session(profile_name="AWSAdminAccess-471112743408", region_name="eu-central-1")
    s3 = session.client("s3")

    print(f"Downloading first {MAX_READ_BYTES // (1024*1024)} MB of pcap...")
    response = s3.get_object(
        Bucket=INPUT_BUCKET,
        Key=S3_KEY,
        Range=f"bytes=0-{MAX_READ_BYTES - 1}",
    )
    data = response["Body"].read()
    print(f"Downloaded: {len(data) / 1024 / 1024:.1f} MB")

    print("\nReassembling IP fragments...")
    start = time.time()
    vita49_packets, pcap_records, datagrams = reassemble_and_extract(data)
    reassembly_time = time.time() - start
    print(f"  Pcap records: {pcap_records}")
    print(f"  UDP datagrams reassembled: {datagrams}")
    print(f"  VITA-49 packets: {len(vita49_packets)}")
    print(f"  Time: {reassembly_time:.1f}s")

    if vita49_packets:
        # Show first packet structure
        first = vita49_packets[0]
        h = int.from_bytes(first[:4], "big")
        print(f"\n  First VITA-49 packet:")
        print(f"    Header word: 0x{h:08X}")
        print(f"    Type: {(h >> 28) & 0xF}")
        print(f"    Size: {(h & 0xFFFF) * 4} bytes")
        print(f"    Total payload: {len(first)} bytes")
        print(f"    First 32 bytes: {first[:32].hex()}")

    print("\nParsing VITA-49 content...")
    contexts, data_count, total_iq, iq_sample = parse_vita49_packets(vita49_packets)

    print(f"  Context packets: {len(contexts)}")
    print(f"  Data packets: {data_count}")
    print(f"  Total I/Q bytes: {total_iq / 1024 / 1024:.1f} MB")
    print(f"  I/Q sample extracted: {len(iq_sample) / 1024 / 1024:.1f} MB")

    if contexts:
        c = contexts[0]
        print(f"\n=== SIGNAL METADATA ===")
        print(f"  Bandwidth: {c['bw_hz'] / 1e6:.3f} MHz")
        print(f"  RF Center Freq: {c['rf_ref_hz'] / 1e6:.3f} MHz")
        print(f"  IF Freq: {c['if_ref_hz'] / 1e6:.3f} MHz")
        print(f"  Sample Rate: {c['sample_rate_hz'] / 1e6:.3f} MSps")
        print(f"  Bit Depth: {c['bit_depth']} bits")
        print(f"  Ref Level: {c['ref_level_dbm']:.1f} dBm")
        print(f"  Ref Locked: {c['ref_locked']}")
    else:
        print("\n  No context packets found — trying to infer from data packets...")
        if vita49_packets:
            # Print a few packet type distributions
            types = defaultdict(int)
            for p in vita49_packets[:100]:
                if len(p) >= 4:
                    h = int.from_bytes(p[:4], "big")
                    types[(h >> 28) & 0xF] += 1
            print(f"  Packet type distribution (first 100): {dict(types)}")

    # Save results
    result = {
        "source_file": S3_KEY,
        "pcap_records": pcap_records,
        "udp_datagrams": datagrams,
        "vita49_packets": len(vita49_packets),
        "context_packets": len(contexts),
        "data_packets": data_count,
        "total_iq_mb": round(total_iq / 1024 / 1024, 2),
        "iq_sample_mb": round(len(iq_sample) / 1024 / 1024, 2),
        "signal_metadata": {
            "bandwidth_mhz": round(contexts[0]["bw_hz"] / 1e6, 3) if contexts else None,
            "rf_center_mhz": round(contexts[0]["rf_ref_hz"] / 1e6, 3) if contexts else None,
            "sample_rate_msps": round(contexts[0]["sample_rate_hz"] / 1e6, 3) if contexts else None,
            "bit_depth": contexts[0]["bit_depth"] if contexts else None,
        } if contexts else {},
    }
    print(f"\n=== RESULT ===")
    print(json.dumps(result, indent=2))

    # Upload to S3
    contact_id = "c14d25d6-d69c-4d9f-a255-85908ab17c13"
    s3.put_object(
        Bucket=INPUT_BUCKET,
        Key=f"analysis/{contact_id}/vita49_metadata.json",
        Body=json.dumps(result, indent=2).encode(),
        ContentType="application/json",
    )
    if iq_sample:
        s3.put_object(
            Bucket=INPUT_BUCKET,
            Key=f"analysis/{contact_id}/iq_sample_10mb.bin",
            Body=iq_sample,
            ContentType="application/octet-stream",
        )
    print(f"\nResults uploaded to s3://{INPUT_BUCKET}/analysis/{contact_id}/")


if __name__ == "__main__":
    main()
