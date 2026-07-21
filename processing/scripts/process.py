"""Main processing script for NOAA-20 DigIF → Imagery pipeline.

Runs inside the ECS Fargate container. Steps:
1. Download .pcap file from S3
2. Extract raw I/Q samples from VITA-49 packets (strip pcap/Ethernet/IP/UDP headers)
3. Run SatDump npp_hrd pipeline on the raw baseband
4. Upload results (images + metadata) to S3 output bucket

Environment variables:
    INPUT_BUCKET: S3 bucket with .pcap files
    INPUT_KEY: S3 key of the .pcap file to process
    OUTPUT_BUCKET: S3 bucket for results
    CONTACT_ID: Ground Station contact ID
    AWS_REGION: AWS region (default: eu-central-1)
"""

import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import boto3

INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "aws-groundstation-demo-reception-471112743408")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "aws-groundstation-demo-reception-471112743408")
CONTACT_ID = os.environ.get("CONTACT_ID", "unknown")
AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")
SAMPLE_RATE = 34312500  # 34.3125 MSps (from VITA-49 context packet)

WORK_DIR = Path("/tmp/processing")
INPUT_DIR = WORK_DIR / "input"
BASEBAND_DIR = WORK_DIR / "baseband"
OUTPUT_DIR = WORK_DIR / "output"


def setup_dirs():
    """Create working directories."""
    for d in [INPUT_DIR, BASEBAND_DIR, OUTPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def download_pcap(s3_client, input_key: str) -> Path:
    """Download .pcap file from S3."""
    filename = Path(input_key).name
    local_path = INPUT_DIR / filename
    print(f"Downloading s3://{INPUT_BUCKET}/{input_key} → {local_path}")
    s3_client.download_file(INPUT_BUCKET, input_key, str(local_path))
    size_gb = local_path.stat().st_size / (1024**3)
    print(f"  Downloaded: {size_gb:.2f} GB")
    return local_path


def extract_iq_from_pcap(pcap_path: Path) -> Path:
    """Extract raw I/Q samples from a VITA-49 pcap file.
    
    Strips pcap global header, pcap record headers, Ethernet, IP, UDP,
    and VITA-49 packet headers. Outputs raw cs8 (complex signed 8-bit).
    """
    output_path = BASEBAND_DIR / (pcap_path.stem + ".cs8")
    print(f"Extracting I/Q from {pcap_path.name} → {output_path.name}")

    start = time.time()
    total_samples = 0

    with open(pcap_path, "rb") as inf, open(output_path, "wb") as outf:
        # Skip pcap global header (24 bytes)
        inf.read(24)

        while True:
            # Pcap record header: 16 bytes
            rec_hdr = inf.read(16)
            if len(rec_hdr) < 16:
                break
            ts_sec, ts_usec, cap_len, orig_len = struct.unpack("<IIII", rec_hdr)

            # Read the captured frame
            frame = inf.read(cap_len)
            if len(frame) < cap_len:
                break

            # Skip Ethernet header (14 bytes)
            if len(frame) < 14:
                continue

            # IP header length (variable)
            ihl = (frame[14] & 0x0F) * 4
            udp_start = 14 + ihl

            # Skip UDP header (8 bytes) to get VITA-49 payload
            vita_start = udp_start + 8
            if len(frame) <= vita_start + 4:
                continue

            vita_payload = frame[vita_start:]

            # Parse VITA-49 header to find data payload offset
            h = int.from_bytes(vita_payload[:4], "big")
            pkt_type = (h >> 28) & 0xF

            if pkt_type != 1:  # Only process Signal Data packets (type 1)
                continue

            # Calculate header size based on flags
            c_bit = (h >> 27) & 0x1  # Class ID
            tsi = (h >> 22) & 0x3    # Integer timestamp
            tsf = (h >> 20) & 0x3    # Fractional timestamp

            hdr_words = 1  # Base header
            hdr_words += 1  # Stream ID (always present for type 1)
            if c_bit:
                hdr_words += 2  # Class ID
            if tsi:
                hdr_words += 1  # Integer timestamp
            if tsf:
                hdr_words += 2  # Fractional timestamp

            data_offset = hdr_words * 4
            iq_data = vita_payload[data_offset:]

            if iq_data:
                outf.write(iq_data)
                total_samples += len(iq_data) // 2  # 2 bytes per complex sample

    elapsed = time.time() - start
    size_gb = output_path.stat().st_size / (1024**3)
    duration_s = total_samples / SAMPLE_RATE
    print(f"  Extracted: {size_gb:.2f} GB ({total_samples:,} complex samples)")
    print(f"  Signal duration: {duration_s:.1f} seconds")
    print(f"  Time: {elapsed:.1f}s")

    return output_path


def run_satdump(baseband_path: Path) -> Path:
    """Run SatDump npp_hrd pipeline on the extracted baseband."""
    satdump_output = OUTPUT_DIR / "satdump"
    satdump_output.mkdir(parents=True, exist_ok=True)

    cmd = [
        "satdump", "npp_hrd",
        str(satdump_output),
        "--source", "file",
        "--input_file", str(baseband_path),
        "--samplerate", str(SAMPLE_RATE),
        "--baseband_format", "cs8",
    ]

    print(f"\nRunning SatDump: {' '.join(cmd)}")
    start = time.time()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=1800,  # 30 min max
    )

    elapsed = time.time() - start
    print(f"  SatDump completed in {elapsed:.1f}s")
    print(f"  Return code: {result.returncode}")

    if result.stdout:
        # Print last 50 lines of stdout
        lines = result.stdout.strip().split("\n")
        for line in lines[-50:]:
            print(f"  [satdump] {line}")

    if result.returncode != 0:
        print(f"  STDERR: {result.stderr[-2000:]}")
        raise RuntimeError(f"SatDump failed with code {result.returncode}")

    return satdump_output


def upload_results(s3_client, satdump_output: Path, contact_id: str) -> dict:
    """Upload SatDump output files to S3."""
    output_prefix = f"imagery/{contact_id}"
    uploaded = []

    for filepath in satdump_output.rglob("*"):
        if filepath.is_file():
            rel_path = filepath.relative_to(satdump_output)
            s3_key = f"{output_prefix}/{rel_path}"

            content_type = "application/octet-stream"
            if filepath.suffix in (".png", ".PNG"):
                content_type = "image/png"
            elif filepath.suffix in (".tif", ".tiff"):
                content_type = "image/tiff"
            elif filepath.suffix == ".json":
                content_type = "application/json"

            print(f"  Uploading: {s3_key} ({filepath.stat().st_size / 1024:.0f} KB)")
            s3_client.upload_file(
                str(filepath),
                OUTPUT_BUCKET,
                s3_key,
                ExtraArgs={"ContentType": content_type},
            )
            uploaded.append({
                "key": s3_key,
                "size_bytes": filepath.stat().st_size,
                "type": content_type,
            })

    return {"prefix": output_prefix, "files": uploaded, "count": len(uploaded)}


def main():
    input_key = os.environ.get("INPUT_KEY")
    if not input_key:
        print("ERROR: INPUT_KEY environment variable not set")
        sys.exit(1)

    print("=" * 60)
    print("NOAA-20 HRD DigIF → Imagery Pipeline")
    print("=" * 60)
    print(f"  Input: s3://{INPUT_BUCKET}/{input_key}")
    print(f"  Output: s3://{OUTPUT_BUCKET}/imagery/{CONTACT_ID}/")
    print(f"  Sample rate: {SAMPLE_RATE / 1e6} MSps")
    print()

    setup_dirs()
    session = boto3.Session(region_name=AWS_REGION)
    s3 = session.client("s3")

    # Step 1: Download
    pcap_path = download_pcap(s3, input_key)

    # Step 2: Extract I/Q
    baseband_path = extract_iq_from_pcap(pcap_path)

    # Clean up pcap to free disk space
    pcap_path.unlink()
    print(f"  Deleted pcap to free space")

    # Step 3: Run SatDump
    satdump_output = run_satdump(baseband_path)

    # Clean up baseband to free space
    baseband_path.unlink()
    print(f"  Deleted baseband to free space")

    # Step 4: Upload results
    print(f"\nUploading results to S3...")
    upload_result = upload_results(s3, satdump_output, CONTACT_ID)

    # Summary
    print(f"\n{'='*60}")
    print(f"COMPLETE — {upload_result['count']} files uploaded")
    print(f"  Output: s3://{OUTPUT_BUCKET}/imagery/{CONTACT_ID}/")
    print(f"{'='*60}")

    # Write result JSON for Step Functions
    result = {
        "status": "success",
        "contact_id": CONTACT_ID,
        "input_key": input_key,
        "output": upload_result,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
