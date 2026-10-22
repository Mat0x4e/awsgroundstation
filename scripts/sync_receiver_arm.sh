#!/bin/bash
# Arms the live-UDP listener on the sync receiver EC2 instance, ahead of AOS.
# Deployed to /opt/scripts/sync_receiver_arm.sh. Executed via SSM Run Command
# by the receiver_arm Lambda (lambdas/receiver_arm/handler.py), triggered by
# the PREPASS EventBridge rule (infra/modules/sync_pipeline/eventbridge.tf).
#
# Starts scripts/sync_receiver_listen.py in the background (nohup) and returns
# immediately -- this command completing does NOT mean the pass is over, only
# that the socket is bound and ready. scripts/sync_receiver_finish.sh (sent
# separately, post-LOS) stops it and hands the capture off to RT-STPS.
#
# Usage: sync_receiver_arm.sh <bucket> <contact_id> <contact_date> <udp_port>
#
# NOTE: Ensure this file is executable after deployment:
#   chmod +x /opt/scripts/sync_receiver_arm.sh

set -euo pipefail

BUCKET="$1"
CONTACT_ID="$2"
CONTACT_DATE="$3"
UDP_PORT="$4"

WORK_DIR="/var/tmp/sync_receiver/${CONTACT_ID}"
CAPTURE_FILE="${WORK_DIR}/combined.cadu"
RAW_CAPTURE_FILE="${WORK_DIR}/raw_capture.bin"
PID_FILE="/var/run/sync_receiver_listen.pid"
LOG_FILE="/var/log/sync_receiver_listen.log"
STATE_FILE="/var/tmp/sync_receiver/current_contact"
READY_MARKER="/var/run/sync_receiver_ready"
RECEIVING_MARKER="/var/run/sync_receiver_receiving"

log_json() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    printf '{"timestamp":"%s","level":"%s","contact_id":"%s","message":"%s"}\n' \
        "$timestamp" "$level" "$CONTACT_ID" "$message" | tee -a "$LOG_FILE"
}

# Clean up readiness markers from any previous arm -- prevents a stale marker
# from making the poll loop exit immediately without the new listener having bound.
rm -f "$READY_MARKER" "$RECEIVING_MARKER"

# If a listener from a previous contact is somehow still running (e.g. a
# missed/failed finish step), stop it first -- one listener per instance.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log_json "WARN" "Stopping stale listener from a previous arm (pid $(cat "$PID_FILE"))"
    kill -TERM "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 2
fi

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# Recorded for sync_receiver_finish.sh, which is invoked with the same
# arguments by Step Functions but needs to know where THIS arm step wrote the
# capture file -- rather than trusting a second, independently-passed bucket.
cat > "$STATE_FILE" <<EOF
{"bucket":"${BUCKET}","contact_id":"${CONTACT_ID}","contact_date":"${CONTACT_DATE}","udp_port":${UDP_PORT},"capture_file":"${CAPTURE_FILE}"}
EOF

log_json "INFO" "Arming listener on UDP port ${UDP_PORT}, writing to ${CAPTURE_FILE}"

# Start the listener in the background.
# --raw-tee captures every raw UDP payload alongside the parsed frame stream,
# enabling replay if RT-STPS frame_sync fails (see docs/SYNC_PIPELINE.md's
# "Packet-format validation gate" section).
nohup python3 /opt/scripts/sync_receiver_listen.py "$UDP_PORT" "$CAPTURE_FILE" \
    --raw-tee "$RAW_CAPTURE_FILE" \
    >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# -----------------------------------------------------------------------
# Readiness poll: wait up to READY_TIMEOUT seconds for the listener to
# write READY_MARKER after successfully binding the UDP socket.  This is
# a preventive check -- we learn about a bind failure BEFORE AOS rather
# than discovering lost data after LOS.
#
# NOTE: at PREPASS there may be NO packets yet (AOS has not happened), so
# only socket-bind confirmation is required here.  First-packet arrival is
# logged separately by the listener as "first_packet_received" for post-hoc
# diagnosis -- it is NOT a gate at arm time.
# -----------------------------------------------------------------------
READY_TIMEOUT=15
READY=0

log_json "INFO" "Polling for listener bind confirmation (up to ${READY_TIMEOUT}s)..."
for _i in $(seq 1 "$READY_TIMEOUT"); do
    if [ -f "$READY_MARKER" ]; then
        READY=1
        log_json "INFO" "Listener bind confirmed after ${_i}s (pid $(cat "$PID_FILE"))"
        break
    fi
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        log_json "ERROR" "Listener process died before binding -- check ${LOG_FILE}"
        break
    fi
    sleep 1
done

if [ "$READY" -eq 0 ]; then
    log_json "WARN" "Listener did not confirm bind within ${READY_TIMEOUT}s"
fi

# -----------------------------------------------------------------------
# Emit ReceiverReadyFailure to CloudWatch (namespace SyncPipeline).
# Value 0 = socket bound successfully; value 1 = failed to bind.
#
# Best-effort observability: a put-metric-data failure must NOT abort the
# arm because the contact would be lost.  The receiver EC2 role already
# has namespace-conditioned PutMetricData permission for SyncPipeline.
# -----------------------------------------------------------------------
INSTANCE_ID=$(curl -sf --max-time 2 \
    http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null \
    || echo "unknown")
METRIC_VALUE=1
if [ "$READY" -eq 1 ]; then
    METRIC_VALUE=0
fi
aws cloudwatch put-metric-data \
    --namespace SyncPipeline \
    --metric-name ReceiverReadyFailure \
    --dimensions "Name=InstanceId,Value=${INSTANCE_ID}" \
    --value "$METRIC_VALUE" \
    --unit Count \
    || log_json "WARN" "put-metric-data failed (non-fatal -- metric is best-effort observability)"

if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log_json "ERROR" "Listener failed to start -- check ${LOG_FILE}"
    exit 1
fi

log_json "INFO" "Listener armed (pid $(cat "$PID_FILE"))"
