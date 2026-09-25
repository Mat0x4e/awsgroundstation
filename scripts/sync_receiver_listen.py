"""Live UDP listener for AWS Ground Station's antenna-downlink-demod-decode
dataflow ("Demodulated/Decoded Data/IP Format").

Deployed to /opt/scripts/sync_receiver_listen.py on the sync receiver EC2
instance (infra/modules/sync_pipeline/ec2.tf) and started in the background by
scripts/sync_receiver_arm.sh, ahead of AOS, so the socket is already bound and
draining when Ground Station starts streaming.

*** OPEN RISK -- READ BEFORE TRUSTING OUTPUT ***
AWS documents that demod/decode dataflows deliver a "VITA-49 Extension"
packet format that is explicitly DIFFERENT from the "VITA-49 Signal Data/IP
Format" used by DigIF (antenna-downlink) dataflows, and that the exact
byte-level layout is provided during satellite onboarding, not published.
This script's packet parsing is adapted from scripts/vita49_extract.py /
vita49_pcap_extract.py (the DigIF reference, itself an exploratory script with
a hardcoded assumption of a fixed 7-word/28-byte header), on the working
assumption that the demod/decode Extension packet uses the same 4-byte
big-endian VITA-49 header (packet type in top 4 bits, size in low 16 bits,
in 32-bit words including the header) with a Signal Data payload that IS the
decoded CCSDS frame stream (UncodedFramesEgress -- post-Viterbi, post-NRZ-M,
each frame still carrying its CCSDS attached sync marker, since Reed-Solomon
correction and PN derandomization have NOT been applied -- see
infra/modules/sync_pipeline/groundstation.tf's header comment). If real
traffic does not sync in RT-STPS (frame_sync repeatedly failing to lock),
capture a short raw sample with --raw-passthrough and inspect it by hand
against the onboarding-provided spec before assuming this parser is wrong --
it may instead be a header-length variant (the C/T/TSI/TSF flag bits change
header length by up to 5 words, which vita49_extract.py's fixed 7-word
assumption does not account for).

Usage:
    sync_receiver_listen.py <port> <output_file> [--raw-passthrough]

  --raw-passthrough: write raw UDP payloads (no VITA-49 header parsing) to
    output_file instead. Useful for capturing a sample to inspect by hand if
    the parsed-frame output doesn't sync in RT-STPS.

Writes newline-free, concatenated decoded-frame bytes to output_file --
directly consumable by rt-stps/bin/batch.sh the same way a SatDump .cadu is
(see scripts/aggregation.sh's Step 3), since both are a stream of
sync-marker-delimited CCSDS frames.

Stops on SIGTERM (sent by sync_receiver_finish.sh) or SIGINT, flushing and
closing the output file cleanly first.
"""

import argparse
import json
import logging
import signal
import socket
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("sync_receiver_listen")

RECV_BUFFER_SIZE = 65536  # generous -- UDP payloads here are well under the 1500 MTU set on the dataflow endpoint
STATS_LOG_INTERVAL_SECONDS = 30

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    logger.info(json.dumps({"action": "signal_received", "signal": signum}))
    _shutdown = True


def extract_frame_payload(packet: bytes) -> bytes | None:
    """Strip the VITA-49-style header from one UDP payload, returning the
    decoded-frame bytes, or None if the packet is not a Signal Data packet
    (e.g. a Context packet, which carries link metadata, not frame data).

    See this module's docstring for the header-format caveat.
    """
    if len(packet) < 4:
        return None

    header = int.from_bytes(packet[:4], "big")
    pkt_type = (header >> 28) & 0xF
    size_words = header & 0xFFFF
    size_bytes = size_words * 4

    if size_bytes > len(packet):
        # Truncated / malformed packet -- UDP does not guarantee delivery or
        # ordering, so this is expected occasionally, not necessarily an error.
        return None

    if pkt_type != 1:
        # Not a Signal Data packet (e.g. type 4 = Context). Frame data only
        # comes from Signal Data packets.
        return None

    # 7-word (28-byte) header assumption -- see docstring caveat.
    HEADER_WORDS = 7
    payload_start = HEADER_WORDS * 4
    if size_bytes <= payload_start:
        return None

    return packet[payload_start:size_bytes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int)
    parser.add_argument("output_file")
    parser.add_argument(
        "--raw-passthrough",
        action="store_true",
        help="Write raw UDP payloads without VITA-49 header parsing",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)  # so the loop can notice _shutdown even with no traffic

    logger.info(
        json.dumps(
            {
                "action": "listener_started",
                "port": args.port,
                "output_file": args.output_file,
                "raw_passthrough": args.raw_passthrough,
            }
        )
    )

    packets_received = 0
    frames_extracted = 0
    bytes_written = 0
    last_stats_log = time.monotonic()

    with open(args.output_file, "wb") as out:
        while not _shutdown:
            try:
                packet, _addr = sock.recvfrom(RECV_BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError as e:
                logger.error(json.dumps({"action": "recv_error", "error": str(e)}))
                continue

            packets_received += 1

            if args.raw_passthrough:
                out.write(packet)
                bytes_written += len(packet)
            else:
                frame = extract_frame_payload(packet)
                if frame:
                    out.write(frame)
                    bytes_written += len(frame)
                    frames_extracted += 1

            now = time.monotonic()
            if now - last_stats_log >= STATS_LOG_INTERVAL_SECONDS:
                logger.info(
                    json.dumps(
                        {
                            "action": "listener_stats",
                            "packets_received": packets_received,
                            "frames_extracted": frames_extracted,
                            "bytes_written": bytes_written,
                        }
                    )
                )
                last_stats_log = now

        out.flush()

    sock.close()
    logger.info(
        json.dumps(
            {
                "action": "listener_stopped",
                "packets_received": packets_received,
                "frames_extracted": frames_extracted,
                "bytes_written": bytes_written,
            }
        )
    )


if __name__ == "__main__":
    main()
