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
PID_FILE="/var/run/sync_receiver_listen.pid"
LOG_FILE="/var/log/sync_receiver_listen.log"
STATE_FILE="/var/tmp/sync_receiver/current_contact"

log_json() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    printf '{"timestamp":"%s","level":"%s","contact_id":"%s","message":"%s"}\n' \
        "$timestamp" "$level" "$CONTACT_ID" "$message" | tee -a "$LOG_FILE"
}

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

nohup python3 /opt/scripts/sync_receiver_listen.py "$UDP_PORT" "$CAPTURE_FILE" \
    >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log_json "ERROR" "Listener failed to start -- check ${LOG_FILE}"
    exit 1
fi

log_json "INFO" "Listener armed (pid $(cat "$PID_FILE"))"
