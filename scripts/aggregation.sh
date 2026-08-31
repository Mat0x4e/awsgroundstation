#!/bin/bash
# Aggregation script for NOAA-20 SDR pipeline.
# Deployed to /opt/scripts/aggregation.sh on the EC2 aggregation instance.
# Executed via SSM Run Command by the Trigger Lambda.
#
# Usage: aggregation.sh <reception_bucket> <contact_id> <contact_date> [output_bucket]
#
# NOTE: Ensure this file is executable after deployment:
#   chmod +x /opt/scripts/aggregation.sh

set -euo pipefail

# --- Arguments ---
BUCKET="$1"
CONTACT_ID="$2"
CONTACT_DATE="$3"

# --- Configuration ---
LOG_FILE="/var/log/aggregation.log"
KMS_KEY_ID="70451aac-a58c-4a93-be24-4587cd55a795"
# NOT /tmp: that is a 16 GB tmpfs (RAM-backed) on this instance, while / has
# ~230 GB free. CSPP expands a ~670 MB VIIRS RDR into many GB of SDR/GEO scratch,
# which both risks ENOSPC and competes for the RAM CSPP itself needs. /var/tmp
# also survives the reboot, so logs are still there to read after a failure.
WORK_DIR="/var/tmp/aggregation/${CONTACT_ID}"
RTSTPS_HOME="/opt/rt-stps"
CSPP_HOME="${CSPP_HOME:-/opt/SDR_4_1}"
# The Trigger Lambda passes the *reception* bucket in $1 (where Ground Station
# dropped the raw .pcap), but every artifact this script touches lives in the SDR
# output bucket: the per-chunk CodeBuild jobs wrote their .cadu there via
# OUTPUT_BUCKET, and the RDR/SDR products belong alongside them. Reading .cadu
# from $BUCKET finds nothing and aborts the run.
OUTPUT_BUCKET="${4:-${OUTPUT_BUCKET:-groundstation-noaa20-sdr-output-471112743408}}"
S3_PREFIX="s3://${OUTPUT_BUCKET}/contacts/${CONTACT_DATE}/${CONTACT_ID}"
CSPP_LOG="${WORK_DIR}/cspp_sdr.log"

# 1 on the EC2 aggregation instance, which must power itself off when done.
# 0 in the CodeBuild container, which has no shutdown(8) and is torn down by the
# build service -- CSPP's ancillary fetch needs the internet egress CodeBuild has
# and the EC2 instance deliberately does not.
SELF_STOP="${SELF_STOP:-1}"

# --- Structured Logging (JSON) ---
log_json() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    printf '{"timestamp":"%s","level":"%s","contact_id":"%s","contact_date":"%s","message":"%s"}\n' \
        "$timestamp" "$level" "$CONTACT_ID" "$CONTACT_DATE" "$message" | tee -a "$LOG_FILE"
}

# --- Upload logs to S3 before powering off ---
# The EXIT trap shuts the instance down, which also severs SSM's reporting
# channel -- without this the only record of a failure dies with the box.
upload_logs() {
    aws s3 cp "$LOG_FILE" "${S3_PREFIX}/logs/aggregation.log"         --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID" >/dev/null 2>&1 || true
    if [ -f "$CSPP_LOG" ]; then
        aws s3 cp "$CSPP_LOG" "${S3_PREFIX}/logs/cspp_sdr.log"             --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID" >/dev/null 2>&1 || true
    fi
}

# --- EXIT Trap: self-stop instance on any exit (success or failure) ---
# A failing command inside an EXIT trap aborts the trap under `set -e`, which
# silently skips everything after it. That is how a bare `kill ""` (empty
# WATCHDOG_PID, exit 2) swallowed CSPP's real error and its log upload. Disable
# -e for the duration of the handler so cleanup always runs to completion.
on_exit() {
    local rc=$?
    set +e
    [ -n "${WATCHDOG_PID:-}" ] && kill "$WATCHDOG_PID" 2>/dev/null
    upload_logs
    if [ "$SELF_STOP" = "1" ]; then
        log_json "INFO" "Stopping instance (rc=${rc})..."
        shutdown -h now
    else
        log_json "INFO" "Exiting (rc=${rc})"
    fi
    return "$rc"
}
trap on_exit EXIT

# --- Watchdog: force shutdown after 35 minutes as safety net ---
# SSM executionTimeout (30 min) should terminate the process first, but if
# the SSM agent itself hangs, this ensures the instance always self-stops.
if [ "$SELF_STOP" = "1" ]; then
    (sleep 2100 && log_json "ERROR" "Watchdog timeout reached (35 min) — forcing shutdown" && shutdown -h now) &
    WATCHDOG_PID=$!
else
    WATCHDOG_PID=""   # CodeBuild enforces its own build timeout
fi

# --- Clean up working directory (in case of previous failed run) ---
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"/{cadu,rdr,sdr}

log_json "INFO" "Starting aggregation for contact=${CONTACT_ID} date=${CONTACT_DATE} bucket=${BUCKET}"

# =============================================================================
# Step 1: Download .cadu files from S3
# =============================================================================
log_json "INFO" "Downloading .cadu files from ${S3_PREFIX}/satdump/"
aws s3 sync "${S3_PREFIX}/satdump/" "$WORK_DIR/cadu/" \
    --exclude '*' --include '*.cadu'

CADU_COUNT=$(find "$WORK_DIR/cadu" -name '*.cadu' | wc -l)
log_json "INFO" "Downloaded ${CADU_COUNT} .cadu files"

if [ "$CADU_COUNT" -eq 0 ]; then
    log_json "ERROR" "No .cadu files found — aborting"
    exit 1
fi

# =============================================================================
# Step 2: Concatenate all .cadu files in sorted order
# =============================================================================
log_json "INFO" "Concatenating .cadu files in sorted order..."
find "$WORK_DIR/cadu" -name '*.cadu' | sort | xargs cat > "$WORK_DIR/combined.cadu"

COMBINED_SIZE=$(du -h "$WORK_DIR/combined.cadu" | cut -f1)
log_json "INFO" "Combined CADU size: ${COMBINED_SIZE}"

# =============================================================================
# Step 3: Configure and run RT-STPS
# =============================================================================
log_json "INFO" "Configuring RT-STPS (PnEncoded=false, removing pn link node)..."

# Copy config to working directory
cp "${RTSTPS_HOME}/config/jpss1.xml" "$WORK_DIR/jpss1.xml"

# Set PnEncoded="false"
sed -i 's/PnEncoded="true"/PnEncoded="false"/' "$WORK_DIR/jpss1.xml"

# Remove the pn link node: bypass pn by routing frame_sync directly to reed_solomon
sed -i '/from="pn" to="reed_solomon"/d' "$WORK_DIR/jpss1.xml"
sed -i 's|from="frame_sync" to="pn"|from="frame_sync" to="reed_solomon"|' "$WORK_DIR/jpss1.xml"

# Ensure RT-STPS output directory exists, and clear any RDRs left by an earlier
# contact. jpss1.xml sends output to /opt/data (a sibling of RTSTPS_HOME), which
# is shared across runs -- without this, step 4 can select a previous contact's
# VIIRS RDR and step 5 uploads it under this contact's prefix.
mkdir -p /opt/data
log_json "INFO" "Clearing $(find /opt/data -name '*.h5' | wc -l) RDR file(s) from previous runs"
find /opt/data -maxdepth 1 -name '*.h5' -delete
find /opt/data -maxdepth 1 -name '*.PDS' -delete

log_json "INFO" "Running RT-STPS batch processing..."
cd "$RTSTPS_HOME" && bin/batch.sh "$WORK_DIR/jpss1.xml" "$WORK_DIR/combined.cadu"

RDR_COUNT=$(find /opt/data -name '*.h5' | wc -l)
log_json "INFO" "RT-STPS produced ${RDR_COUNT} RDR HDF5 files"

# =============================================================================
# Step 4: Run CSPP SDR (only if VIIRS RDR exists)
# =============================================================================
VIIRS_RDR=$(find /opt/data -name 'RNSCA-RVIRS*.h5' | sort | head -1)

if [ -n "$VIIRS_RDR" ]; then
    log_json "INFO" "VIIRS RDR found: ${VIIRS_RDR} — running CSPP SDR viirs_sdr.sh..."
    export CSPP_SDR_HOME="$CSPP_HOME"
    export CSPP_RT_HOME="$CSPP_HOME"
    "$CSPP_HOME/bin/viirs_sdr.sh" --work-dir "$WORK_DIR/sdr" -p 4 "$VIIRS_RDR" >>"$CSPP_LOG" 2>&1

    SDR_COUNT=$(find "$WORK_DIR/sdr" -name 'SV*.h5' -o -name 'G*.h5' | wc -l)
    log_json "INFO" "CSPP SDR produced ${SDR_COUNT} SDR/GEO HDF5 files"
else
    log_json "WARN" "No VIIRS RDR file produced by RT-STPS — skipping CSPP SDR"
fi

# =============================================================================
# Step 5: Upload results to S3 with KMS encryption
# =============================================================================

# Upload SDR and GEO HDF5 files
if [ -d "$WORK_DIR/sdr" ] && [ "$(find "$WORK_DIR/sdr" -name '*.h5' 2>/dev/null | head -1)" ]; then
    log_json "INFO" "Uploading SDR/GEO HDF5 files to ${S3_PREFIX}/sdr/"
    aws s3 sync "$WORK_DIR/sdr/" "${S3_PREFIX}/sdr/" \
        --exclude '*' --include '*.h5' \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
    log_json "INFO" "SDR/GEO upload complete"
else
    log_json "INFO" "No SDR/GEO files to upload"
fi

# Upload RDR HDF5 files
if [ "$(find /opt/data -name '*.h5' 2>/dev/null | head -1)" ]; then
    log_json "INFO" "Uploading RDR HDF5 files to ${S3_PREFIX}/rdr/"
    aws s3 sync /opt/data/ "${S3_PREFIX}/rdr/" \
        --exclude '*' --include '*.h5' \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
    log_json "INFO" "RDR upload complete"
else
    log_json "INFO" "No RDR files to upload"
fi

log_json "INFO" "Aggregation complete for contact=${CONTACT_ID}"
# EXIT trap will stop the instance
