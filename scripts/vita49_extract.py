"""VITA-49 DigIF Extraction Script for AWS Ground Station .pcap files.

Parses a VITA-49 formatted .pcap file from S3, extracts signal metadata
from context packets, and dumps the first N bytes of raw I/Q samples
for inspection.

Usage (invoked via Lambda or locally with boto3):
    python vita49_extract.py <s3_key> [--sample-mb 10]

Output written to S3: analysis/{contact_id}/vita49_metadata.json
                      analysis/{contact_id}/iq_sample.bin (first N MB of raw IQ)
"""

import json
import struct
import sys
import time
from io import BytesIO

import boto3

INPUT_BUCKET = "aws-groundstation-demo-reception-471112743408"
OUTPUT_BUCKET = "aws-groundstation-demo-reception-471112743408"
SAMPLE_BYTES = 10 * 1024 * 1024  # 10 MB of raw IQ for inspection


def parse_vita_float(radix: int, bits: int) -> float:
    """Parse VITA-49 fixed-point number to float."""
    div = float(1 << radix)
    return float(bits) / div


def parse_vita_double(bits: int) -> float:
    """Parse VITA-49 64-bit fixed-point (radix 20) to float."""
    # Handle signed 64-bit
    if bits >= (1 << 63):
        bits -= (1 << 64)
    return parse_vita_float(20, bits)


def parse_vita_f16(bits: int) -> float:
    """Parse VITA-49 16-bit fixed-point (radix 7) to float."""
    if bits >= (1 << 15):
        bits -= (1 << 16)
    return parse_vita_float(7, bits)


def extract_vita49(body_stream, max_sample_bytes: int = SAMPLE_BYTES):
    """Parse VITA-49 packets from a binary stream.
    
    Returns:
        metadata: dict with signal characteristics from context packets
        iq_data: bytes of raw I/Q samples (up to max_sample_bytes)
        stats: dict with packet counts and sizes
    """
    contexts = []
    iq_buffer = BytesIO()
    total_iq_bytes = 0
    data_packets = 0
    context_packets = 0
    other_packets = 0
    total_bytes_read = 0

    while True:
        hdrb = body_stream.read(4)
        if not hdrb or len(hdrb) < 4:
            break

        h = int.from_bytes(hdrb, "big")
        pkt_type = (h >> 28) & 0xF
        sz_words = h & 0xFFFF
        sz_bytes = sz_words * 4

        # Read remaining packet data (size includes the 4-byte header)
        remaining = body_stream.read(sz_bytes - 4)
        if len(remaining) < sz_bytes - 4:
            break  # Truncated packet at end of file

        total_bytes_read += sz_bytes
        everything = hdrb + remaining

        if pkt_type == 1:
            # Signal Data Packet (type 1) — contains I/Q samples
            # Header is 7 words (28 bytes), payload follows
            data = everything[7 * 4:]
            data_packets += 1
            total_iq_bytes += len(data)

            if iq_buffer.tell() < max_sample_bytes:
                to_write = min(len(data), max_sample_bytes - iq_buffer.tell())
                iq_buffer.write(data[:to_write])

        elif pkt_type == 4:
            # Context Packet (type 4) — contains metadata
            context_packets += 1
            ctx_data = everything[7 * 4:]

            if len(ctx_data) >= 100:  # Enough for full context
                try:
                    unpacked = struct.unpack("!LLQQQQLLQQLLQ", ctx_data[:100])
                    names = [
                        "cif", "rp", "bw", "ifref", "rfref", "ifoff",
                        "refl", "gain", "rate", "tsadj", "tscal", "indic",
                        "payloadfmt",
                    ]
                    ctx = dict(zip(names, unpacked))
                    ctx["bw_hz"] = parse_vita_double(ctx["bw"])
                    ctx["if_ref_hz"] = parse_vita_double(ctx["ifref"])
                    ctx["rf_ref_hz"] = parse_vita_double(ctx["rfref"])
                    ctx["if_offset_hz"] = parse_vita_double(ctx["ifoff"])
                    ctx["sample_rate_hz"] = parse_vita_double(ctx["rate"])
                    ctx["ref_level_dbm"] = parse_vita_f16(ctx["refl"])
                    ctx["gain_db"] = {
                        "stage1": parse_vita_f16(ctx["gain"] & 0xFFFF),
                        "stage2": parse_vita_f16(ctx["gain"] >> 16),
                    }
                    ctx["ref_locked"] = bool(ctx["indic"] & (1 << 17))
                    ctx["bit_depth"] = ((ctx["payloadfmt"] >> 32) & 0x1F) + 1
                    contexts.append(ctx)
                except struct.error:
                    pass  # Malformed context packet
        else:
            other_packets += 1

        # Stop after processing enough for metadata + sample
        if data_packets > 0 and context_packets > 0 and iq_buffer.tell() >= max_sample_bytes:
            # We have metadata and enough sample data
            break

    iq_buffer.seek(0)
    iq_data = iq_buffer.read()

    # Build metadata summary from the first context packet
    metadata = {}
    if contexts:
        c = contexts[0]
        metadata = {
            "bandwidth_mhz": round(c["bw_hz"] / 1e6, 3),
            "center_frequency_mhz": round(c["rf_ref_hz"] / 1e6, 3),
            "if_frequency_mhz": round(c["if_ref_hz"] / 1e6, 3),
            "if_offset_mhz": round(c["if_offset_hz"] / 1e6, 3),
            "sample_rate_msps": round(c["sample_rate_hz"] / 1e6, 3),
            "ref_level_dbm": round(c["ref_level_dbm"], 2),
            "gain_db": c["gain_db"],
            "ref_locked": c["ref_locked"],
            "bit_depth": c["bit_depth"],
        }

    stats = {
        "data_packets": data_packets,
        "context_packets": context_packets,
        "other_packets": other_packets,
        "total_iq_bytes_in_file": total_iq_bytes,
        "total_bytes_read": total_bytes_read,
        "iq_sample_bytes_extracted": len(iq_data),
    }

    return metadata, iq_data, stats


def main():
    session = boto3.Session(profile_name="AWSAdminAccess-471112743408", region_name="eu-central-1")
    s3 = session.client("s3")

    # Use the first .pcap file from the contact
    s3_key = (
        "year=2026/month=06/day=19/satellite=33f035e1-73f7-47a5-9df8-fbc48636dca8/"
        "c14d25d6-d69c-4d9f-a255-85908ab17c13_20260619T114459Z_"
        "c6b27c0f-bf52-4bd9-b163-edda2afd83c2.pcap"
    )

    if len(sys.argv) > 1:
        s3_key = sys.argv[1]

    contact_id = "c14d25d6-d69c-4d9f-a255-85908ab17c13"
    print(f"Downloading first 50 MB of: s3://{INPUT_BUCKET}/{s3_key}")

    # Stream only the first 50 MB to get metadata + sample
    # (avoids downloading the full 2.18 GB file)
    response = s3.get_object(
        Bucket=INPUT_BUCKET,
        Key=s3_key,
        Range="bytes=0-52428799",  # First 50 MB
    )
    body = response["Body"]

    print("Parsing VITA-49 packets...")
    start = time.time()
    metadata, iq_data, stats = extract_vita49(body, max_sample_bytes=SAMPLE_BYTES)
    elapsed = time.time() - start

    print(f"Extraction completed in {elapsed:.1f}s")
    print(f"\n=== Signal Metadata ===")
    print(json.dumps(metadata, indent=2))
    print(f"\n=== Packet Statistics ===")
    print(json.dumps(stats, indent=2))

    # Upload results to S3
    output_prefix = f"analysis/{contact_id}"

    result = {
        "source_file": s3_key,
        "source_bucket": INPUT_BUCKET,
        "extraction_time_s": round(elapsed, 2),
        "signal_metadata": metadata,
        "packet_stats": stats,
    }

    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=f"{output_prefix}/vita49_metadata.json",
        Body=json.dumps(result, indent=2).encode(),
        ContentType="application/json",
    )
    print(f"\nMetadata uploaded to: s3://{OUTPUT_BUCKET}/{output_prefix}/vita49_metadata.json")

    if iq_data:
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"{output_prefix}/iq_sample_10mb.bin",
            Body=iq_data,
            ContentType="application/octet-stream",
        )
        print(f"IQ sample uploaded to: s3://{OUTPUT_BUCKET}/{output_prefix}/iq_sample_10mb.bin")
        print(f"  Size: {len(iq_data) / 1024 / 1024:.1f} MB")

    return result


if __name__ == "__main__":
    result = main()
