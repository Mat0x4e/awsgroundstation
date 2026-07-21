"""Inspect the raw structure of an AWS Ground Station .pcap file.

Reads the first few KB to determine:
- Whether it's a standard pcap (magic 0xA1B2C3D4) or pcapng (0x0A0D0D0A)
- The link type (Ethernet, raw IP, or custom)
- The first few packet headers and their types
"""

import struct
import sys

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

    # Download first 1 MB for inspection
    response = s3.get_object(Bucket=INPUT_BUCKET, Key=S3_KEY, Range="bytes=0-1048575")
    data = response["Body"].read()

    print(f"File: {S3_KEY}")
    print(f"Downloaded: {len(data)} bytes")
    print(f"\n=== First 64 bytes (hex) ===")
    for i in range(0, min(64, len(data)), 16):
        hex_str = " ".join(f"{b:02x}" for b in data[i:i+16])
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"  {i:04x}: {hex_str}  {ascii_str}")

    # Check for pcap magic numbers
    magic = struct.unpack("<I", data[:4])[0]
    magic_be = struct.unpack(">I", data[:4])[0]

    if magic == 0xA1B2C3D4:
        print(f"\n=== PCAP file (little-endian) ===")
        parse_pcap_header(data, "<")
    elif magic == 0xD4C3B2A1:
        print(f"\n=== PCAP file (big-endian) ===")
        parse_pcap_header(data, ">")
    elif magic_be == 0x0A0D0D0A:
        print(f"\n=== PCAPng file ===")
        print("  (PCAPng format — more complex header)")
    else:
        print(f"\n=== Unknown format ===")
        print(f"  Magic (LE): 0x{magic:08X}")
        print(f"  Magic (BE): 0x{magic_be:08X}")
        # Maybe it's raw VITA-49 without pcap wrapper?
        check_vita49_direct(data)


def parse_pcap_header(data, endian):
    """Parse standard pcap file header."""
    fmt = f"{endian}IHHIIII"
    hdr = struct.unpack(fmt, data[:24])
    magic, vmajor, vminor, tz_offset, ts_accuracy, snap_len, link_type = hdr

    LINK_TYPES = {
        0: "NULL/Loopback",
        1: "Ethernet",
        101: "Raw IP",
        113: "Linux cooked",
        147: "User-defined (DLT_USER0)",
        148: "User-defined (DLT_USER1)",
        149: "User-defined (DLT_USER2)",
        195: "802.15.4",
        228: "Raw IPv4",
        229: "Raw IPv6",
    }

    print(f"  Version: {vmajor}.{vminor}")
    print(f"  Snap length: {snap_len}")
    print(f"  Link type: {link_type} ({LINK_TYPES.get(link_type, 'Unknown')})")
    print(f"  TZ offset: {tz_offset}")

    # Parse first few packets
    offset = 24
    print(f"\n=== First 5 pcap packets ===")
    for i in range(5):
        if offset + 16 > len(data):
            break
        ts_sec, ts_usec, cap_len, orig_len = struct.unpack(
            f"{endian}IIII", data[offset:offset+16]
        )
        pkt_data = data[offset+16:offset+16+min(cap_len, 128)]
        print(f"\n  Packet {i+1}:")
        print(f"    Timestamp: {ts_sec}.{ts_usec:06d}")
        print(f"    Captured: {cap_len} bytes, Original: {orig_len} bytes")
        print(f"    First 32 bytes: {pkt_data[:32].hex()}")

        # Check if payload starts with VITA-49 header
        if len(pkt_data) >= 4:
            vita_hdr = int.from_bytes(pkt_data[:4], "big")
            vita_type = (vita_hdr >> 28) & 0xF
            vita_size = (vita_hdr & 0xFFFF) * 4
            print(f"    VITA-49 type: {vita_type}, size: {vita_size} bytes")

        offset += 16 + cap_len


def check_vita49_direct(data):
    """Check if this might be raw VITA-49 without pcap wrapper."""
    print("\n  Checking if raw VITA-49 stream...")
    h = int.from_bytes(data[:4], "big")
    pkt_type = (h >> 28) & 0xF
    sz_words = h & 0xFFFF
    print(f"  First word: 0x{h:08X}")
    print(f"  Packet type: {pkt_type}")
    print(f"  Size (words): {sz_words} = {sz_words * 4} bytes")

    if pkt_type in (0, 1, 2, 3, 4, 5):
        print(f"  → Likely raw VITA-49 stream (no pcap header)")
    else:
        print(f"  → Not recognized as VITA-49")


if __name__ == "__main__":
    main()
