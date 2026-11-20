#!/bin/bash
# Finishes a sync receiver contact: stops the live-UDP listener and uploads the
# frame capture (and the raw UDP tee) to S3. Deployed to
# /opt/scripts/sync_receiver_finish.sh. Executed via SSM Run Command by the
# FinishReceiver state in infra/modules/sync_pipeline/step_functions.tf, shortly
# after LOS.
#
# Since 2026-11-20 the receiver does NOT run RT-STPS (architecture principle
# "converge at the first common artifact", docs/ARCHITECTURE.md). The capture
# goes to <bucket>/contacts/<date>/<contact>/cadu/combined.cadu and the shared
# aggregation Batch job (scripts/aggregation_entrypoint.sh) runs RT-STPS + CSPP
# on it, exactly as for the async path, with CADU_PREFIX=cadu and
# PN_ENCODED=true: the demod/decode UncodedFramesEgress stream has NOT been
# PN-derandomized nor Reed-Solomon corrected (see
# infra/modules/sync_pipeline/groundstation.tf's header comment). So the
# receiver needs only /opt/scripts, no /opt/rt-stps.
#
# Usage: sync_receiver_finish.sh <bucket> <contact_id> <contact_date>
#
# NOTE: Ensure this file is executable after deployment:
#   chmod +x /opt/scripts/sync_receiver_finish.sh

set -euo pipefail

BUCKET="$1"
CONTACT_ID="$2"
CONTACT_DATE="$3"

LOG_FILE="/var/log/sync_receiver_finish.log"
KMS_KEY_ID="${KMS_KEY_ID:-70451aac-a58c-4a93-be24-4587cd55a795}"
WORK_DIR="/var/tmp/sync_receiver/${CONTACT_ID}"
CAPTURE_FILE="${WORK_DIR}/combined.cadu"
PID_FILE="/var/run/sync_receiver_listen.pid"
S3_PREFIX="s3://${BUCKET}/contacts/${CONTACT_DATE}/${CONTACT_ID}"

log_json() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    printf '{"timestamp":"%s","level":"%s","contact_id":"%s","message":"%s"}\n' \
        "$timestamp" "$level" "$CONTACT_ID" "$message" | tee -a "$LOG_FILE"
}

upload_logs() {
    aws s3 cp "$LOG_FILE" "${S3_PREFIX}/logs/sync_receiver_finish.log" \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID" >/dev/null 2>&1 || true
}
trap upload_logs EXIT

log_json "INFO" "Finishing contact=${CONTACT_ID} date=${CONTACT_DATE} bucket=${BUCKET}"

# =============================================================================
# Step 1: Stop the live-UDP listener
# =============================================================================
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    listener_pid=$(cat "$PID_FILE")
    log_json "INFO" "Stopping listener (pid ${listener_pid})"
    kill -TERM "$listener_pid" 2>/dev/null || true
    # Give the listener a moment to flush and close the capture file cleanly
    # (its SIGTERM handler flushes before exiting -- see
    # scripts/sync_receiver_listen.py).
    for _ in $(seq 1 10); do
        kill -0 "$listener_pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$listener_pid" 2>/dev/null; then
        log_json "WARN" "Listener did not exit after SIGTERM -- sending SIGKILL"
        kill -KILL "$listener_pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
else
    log_json "WARN" "No running listener found (pid file absent or stale) -- proceeding with whatever was captured"
fi

# =============================================================================
# Step 2: Upload the raw UDP tee first (packet-format gate evidence), even if
# the parsed capture turns out empty: it is the only copy of what arrived.
# =============================================================================
RAW_CAPTURE_FILE="${WORK_DIR}/raw_capture.bin"
if [ -f "$RAW_CAPTURE_FILE" ] && [ -s "$RAW_CAPTURE_FILE" ]; then
    log_json "INFO" "Uploading raw UDP tee capture to ${S3_PREFIX}/raw/"
    # Best-effort: the raw tee is evidence only. A failure here (it is the
    # larger file) must never block the parsed capture upload in Step 3, which
    # is what the aggregation job consumes.
    if aws s3 cp "$RAW_CAPTURE_FILE" "${S3_PREFIX}/raw/raw_capture.bin" \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"; then
        log_json "INFO" "Raw capture upload complete"
    else
        log_json "WARN" "Raw capture upload failed -- continuing with the parsed capture upload"
    fi
else
    log_json "INFO" "No raw tee capture found -- skipping raw/ upload"
fi

# =============================================================================
# Step 3: Upload the frame capture for the shared aggregation chain
# =============================================================================
if [ ! -s "$CAPTURE_FILE" ]; then
    log_json "ERROR" "Capture file ${CAPTURE_FILE} missing or empty -- nothing for the aggregation job"
    exit 1
fi

CAPTURE_SIZE=$(du -h "$CAPTURE_FILE" | cut -f1)
log_json "INFO" "Uploading frame capture (${CAPTURE_SIZE}) to ${S3_PREFIX}/cadu/"
aws s3 cp "$CAPTURE_FILE" "${S3_PREFIX}/cadu/combined.cadu" \
    --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"

log_json "INFO" "Receiver handoff complete for contact=${CONTACT_ID} (RT-STPS + CSPP run in the shared aggregation job)"
