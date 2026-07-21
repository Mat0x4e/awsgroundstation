"""Extract raw I/Q samples from VITA-49 pcap file."""
import struct
import sys
import time
from pathlib import Path

pcap_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])

start = time.time()
total_samples = 0

with open(pcap_path, "rb") as inf, open(output_path, "wb") as outf:
    inf.read(24)
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
        hdr_words = 2
        if c_bit:
            hdr_words += 2
        if tsi:
            hdr_words += 1
        if tsf:
            hdr_words += 2
        data_offset = hdr_words * 4
        iq_data = vita_payload[data_offset:]
        if iq_data:
            outf.write(iq_data)
            total_samples += len(iq_data) // 2

elapsed = time.time() - start
size_gb = output_path.stat().st_size / (1024**3)
print(f"Extracted: {size_gb:.2f} GB ({total_samples:,} samples) in {elapsed:.1f}s")
print(f"Signal duration: {total_samples / 34312500:.1f}s")
