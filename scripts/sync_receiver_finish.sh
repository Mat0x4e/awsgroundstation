#!/bin/bash
# Finishes a sync receiver contact: stops the live-UDP listener, runs RT-STPS
# on the raw capture, and uploads the CADU capture + RDR HDF5 to S3.
# Deployed to /opt/scripts/sync_receiver_finish.sh. Executed via SSM Run
# Command by the FinishReceiver state in
# infra/modules/sync_pipeline/step_functions.tf, shortly after LOS.
#
# Unlike scripts/aggregation.sh, RT-STPS here runs with jpss1.xml UNMODIFIED
# (PnEncoded="true" kept, pn link node left wired): the demod/decode
# UncodedFramesEgress stream has NOT been PN-derandomized (that only happens
# in SatDump, upstream of the existing async pipeline's .cadu) -- see
# infra/modules/sync_pipeline/groundstation.tf's header comment. This script
# also does NOT run CSPP -- that happens afterward in CodeBuild (see
# buildspecs/aggregation_sync.yml), same "needs internet egress" reasoning as
# the existing pipeline (see scripts/aggregation.sh's header comment).
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
RTSTPS_HOME="/opt/rt-stps"
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

if [ ! -s "$CAPTURE_FILE" ]; then
    log_json "ERROR" "Capture file ${CAPTURE_FILE} missing or empty -- nothing to process"
    exit 1
fi

CAPTURE_SIZE=$(du -h "$CAPTURE_FILE" | cut -f1)
log_json "INFO" "Capture file size: ${CAPTURE_SIZE}"

# =============================================================================
# Step 2: Run RT-STPS (jpss1.xml unmodified -- PnEncoded="true" kept)
# =============================================================================

# Same rationale as scripts/aggregation.sh: /opt/data is a sibling of
# RTSTPS_HOME, shared across runs -- clear it first so this contact doesn't
# pick up a previous contact's leftover RDR.
mkdir -p /opt/data
log_json "INFO" "Clearing $(find /opt/data -maxdepth 1 -name '*.h5' | wc -l) RDR file(s) from previous runs"
find /opt/data -maxdepth 1 -name '*.h5' -delete
find /opt/data -maxdepth 1 -name '*.PDS' -delete

log_json "INFO" "Running RT-STPS batch processing (jpss1.xml, PnEncoded=true)..."
cd "$RTSTPS_HOME" && bin/batch.sh "${RTSTPS_HOME}/config/jpss1.xml" "$CAPTURE_FILE"

RDR_COUNT=$(find /opt/data -maxdepth 1 -name '*.h5' | wc -l)
log_json "INFO" "RT-STPS produced ${RDR_COUNT} RDR HDF5 file(s)"

if [ "$RDR_COUNT" -eq 0 ]; then
    log_json "ERROR" "RT-STPS produced no RDR HDF5 output -- frame sync likely failed to lock. See this script's docstring / scripts/sync_receiver_listen.py's header-format caveat."
fi

# =============================================================================
# Step 3: Upload capture + RDR to S3
# =============================================================================

log_json "INFO" "Uploading raw CADU capture to ${S3_PREFIX}/cadu/"
aws s3 cp "$CAPTURE_FILE" "${S3_PREFIX}/cadu/combined.cadu" \
    --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"

if [ "$(find /opt/data -maxdepth 1 -name '*.h5' 2>/dev/null | head -1)" ]; then
    log_json "INFO" "Uploading RDR HDF5 files to ${S3_PREFIX}/rdr/"
    aws s3 sync /opt/data/ "${S3_PREFIX}/rdr/" \
        --exclude '*' --include '*.h5' \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
    log_json "INFO" "RDR upload complete"
else
    log_json "INFO" "No RDR files to upload"
fi

log_json "INFO" "Receiver handoff complete for contact=${CONTACT_ID}"
