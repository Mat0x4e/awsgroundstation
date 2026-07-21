#!/bin/bash
# EC2 User Data script — processes one NOAA-20 DigIF .pcap chunk with SatDump
# The instance terminates itself when done (cattle, not pets).
set -euo pipefail

CONTACT_ID="c14d25d6-d69c-4d9f-a255-85908ab17c13"
INPUT_BUCKET="aws-groundstation-demo-reception-471112743408"
OUTPUT_BUCKET="aws-groundstation-demo-reception-471112743408"
# First chunk (smallest timestamp = first captured data)
INPUT_KEY="year=2026/month=06/day=19/satellite=33f035e1-73f7-47a5-9df8-fbc48636dca8/c14d25d6-d69c-4d9f-a255-85908ab17c13_20260619T114459Z_c6b27c0f-bf52-4bd9-b163-edda2afd83c2.pcap"
SAMPLE_RATE=34312500
REGION="eu-central-1"

LOG_FILE="/tmp/satdump_processing.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== NOAA-20 HRD Processing — $(date -u) ==="
echo "Instance: $(curl -s http://169.254.169.254/latest/meta-data/instance-id)"

# Install dependencies
echo "Installing SatDump..."
apt-get update -qq
add-apt-repository -y ppa:altillimity/satdump
apt-get update -qq
apt-get install -y satdump awscli python3 python3-pip
pip3 install boto3

# Create work directories
WORK="/tmp/processing"
mkdir -p "$WORK/input" "$WORK/baseband" "$WORK/output"

# Download the .pcap file
echo "Downloading pcap from S3..."
aws s3 cp "s3://${INPUT_BUCKET}/${INPUT_KEY}" "$WORK/input/chunk.pcap" --region "$REGION"
echo "Downloaded: $(du -h $WORK/input/chunk.pcap | cut -f1)"

# Extract I/Q from VITA-49 pcap
echo "Extracting I/Q samples from VITA-49..."
python3 - <<'PYTHON'
import struct, time
from pathlib import Path

pcap_path = Path("/tmp/processing/input/chunk.pcap")
output_path = Path("/tmp/processing/baseband/chunk.cs8")

start = time.time()
total_samples = 0

with open(pcap_path, "rb") as inf, open(output_path, "wb") as outf:
    inf.read(24)  # Skip pcap global header
    while True:
        rec_hdr = inf.read(16)
        if len(rec_hdr) < 16:
            break
        ts_sec, ts_usec, cap_len, orig_len = struct.unpack("<IIII", rec_hdr)
        frame = inf.read(cap_len)
        if len(frame) < cap_len:
            break
        if len(frame) < 42:
            continue
        ihl = (frame[14] & 0x0F) * 4
        vita_start = 14 + ihl + 8
        if len(frame) <= vita_start + 4:
            continue
        vita_payload = frame[vita_start:]
        h = int.from_bytes(vita_payload[:4], "big")
        pkt_type = (h >> 28) & 0xF
        if pkt_type != 1:
            continue
        c_bit = (h >> 27) & 0x1
        tsi = (h >> 22) & 0x3
        tsf = (h >> 20) & 0x3
        hdr_words = 2  # header + stream_id
        if c_bit: hdr_words += 2
        if tsi: hdr_words += 1
        if tsf: hdr_words += 2
        data_offset = hdr_words * 4
        iq_data = vita_payload[data_offset:]
        if iq_data:
            outf.write(iq_data)
            total_samples += len(iq_data) // 2

elapsed = time.time() - start
size_gb = output_path.stat().st_size / (1024**3)
print(f"Extracted: {size_gb:.2f} GB ({total_samples:,} samples) in {elapsed:.1f}s")
print(f"Signal duration: {total_samples / 34312500:.1f}s")
PYTHON

# Delete pcap to free disk space
rm -f "$WORK/input/chunk.pcap"
echo "Freed pcap disk space"

# Run SatDump
echo "Running SatDump npp_hrd pipeline..."
satdump npp_hrd "$WORK/output" \
    --source file \
    --input_file "$WORK/baseband/chunk.cs8" \
    --samplerate "$SAMPLE_RATE" \
    --baseband_format cs8 \
    2>&1 | tail -100

echo "SatDump complete. Output:"
find "$WORK/output" -type f | head -50
du -sh "$WORK/output"

# Delete baseband to free space
rm -f "$WORK/baseband/chunk.cs8"

# Upload results to S3
echo "Uploading results to S3..."
aws s3 sync "$WORK/output" "s3://${OUTPUT_BUCKET}/imagery/${CONTACT_ID}/chunk_001/" --region "$REGION"

# Upload the processing log
aws s3 cp "$LOG_FILE" "s3://${OUTPUT_BUCKET}/imagery/${CONTACT_ID}/processing_log.txt" --region "$REGION"

echo "=== PROCESSING COMPLETE — $(date -u) ==="
echo "Results at: s3://${OUTPUT_BUCKET}/imagery/${CONTACT_ID}/chunk_001/"

# Self-terminate (cattle approach)
echo "Terminating instance..."
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION"
