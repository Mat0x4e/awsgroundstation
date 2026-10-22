#!/bin/bash
# aggregation_entrypoint.sh -- converged aggregation entrypoint for AWS Batch.
#
# Replaces BOTH buildspecs/aggregation.yml (async: CADU -> RT-STPS -> RDR -> SDR)
# and buildspecs/aggregation_sync.yml (sync: RDR -> SDR/GEO) via a single
# RTSTPS=on|off environment variable. Stage 2 of the Clean Compute Pipeline
# Migration (ADR-002).
#
# The Batch job downloads this script from S3 at startup so a redeploy takes
# effect without rebuilding the SDR ECR image -- same no-image-rebuild property
# as the CodeBuild buildspec approach. Deploy via scripts/deploy_aggregation_entrypoint.sh.
#
# Required env vars (set by Step Functions via ContainerOverrides.Environment):
#   RTSTPS          "on" (async: CADU->RT-STPS->RDR->CSPP->SDR) or
#                   "off" (sync: RDR already in S3->CSPP->SDR)
#   OUTPUT_BUCKET   S3 bucket; all artifacts under contacts/CONTACT_DATE/CONTACT_ID/
#   CONTACT_ID      Ground Station contact UUID
#   CONTACT_DATE    YYYY/MM/DD
#   KMS_KEY_ID      CMK ARN or key ID for SSE-KMS uploads
#
# Optional:
#   RECEPTION_BUCKET  passed by the async SFN for parity; not used (all I/O
#                     via OUTPUT_BUCKET -- see comments in scripts/aggregation.sh)
#   CSPP_HOME / CSPP_SDR_HOME / CSPP_RT_HOME / RTSTPS_HOME  -- defaulted below
#
# No SELF_STOP, no watchdog: this is a container torn down by Batch on exit.
# Scratch in /var/tmp (NOT /tmp): CSPP expands a ~670 MB VIIRS RDR into many GB
# of SDR/GEO scratch; the 200 GiB Batch root volume (Stage 1 launch template)
# provides ample space without competing with CSPP's RAM footprint.

set -euo pipefail

# ---------------------------------------------------------------------------
# Tool homes -- defaults match the buildspecs and the SDR ECR image layout
# ---------------------------------------------------------------------------
CSPP_HOME="${CSPP_HOME:-/opt/SDR_4_1}"
CSPP_SDR_HOME="${CSPP_SDR_HOME:-/opt/SDR_4_1}"
CSPP_RT_HOME="${CSPP_RT_HOME:-/opt/SDR_4_1}"
RTSTPS_HOME="${RTSTPS_HOME:-/opt/rt-stps}"

# ---------------------------------------------------------------------------
# Validate required env vars -- before the EXIT trap so failures are fatal/noisy
# ---------------------------------------------------------------------------
case "${RTSTPS:-}" in
    on|off) ;;
    *) printf '[ERROR] RTSTPS must be "on" or "off" (got: "%s")\n' \
           "${RTSTPS:-}" >&2
       exit 1 ;;
esac

: "${OUTPUT_BUCKET:?OUTPUT_BUCKET is required}"
: "${CONTACT_ID:?CONTACT_ID is required}"
: "${CONTACT_DATE:?CONTACT_DATE is required}"
: "${KMS_KEY_ID:?KMS_KEY_ID is required}"

# ---------------------------------------------------------------------------
# Derived paths (all required vars now validated)
# ---------------------------------------------------------------------------
WORK_DIR="/var/tmp/aggregation/${CONTACT_ID}"
LOG_FILE="${WORK_DIR}/aggregation.log"
S3_PREFIX="s3://${OUTPUT_BUCKET}/contacts/${CONTACT_DATE}/${CONTACT_ID}"
CSPP_LOG="${WORK_DIR}/cspp_sdr.log"

# ---------------------------------------------------------------------------
# Scratch directory
# Clean any remnants of a previous (failed) run, then recreate subdirs.
# ---------------------------------------------------------------------------
rm -rf "$WORK_DIR"
mkdir -p "${WORK_DIR}/cadu" "${WORK_DIR}/rdr" "${WORK_DIR}/sdr"

# ---------------------------------------------------------------------------
# Structured logging -- timestamped lines to both stdout (-> CloudWatch via
# Batch) and the log file (-> S3 via EXIT trap below).
# ---------------------------------------------------------------------------
ts_log() {
    local level="$1"
    local msg="$2"
    local ts
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    printf '[%s] [%s] %s\n' "$ts" "$level" "$msg" | tee -a "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# EXIT trap: upload aggregation.log and cspp_sdr.log to S3 on any exit.
# Best-effort (|| true) -- a log-upload failure must not mask the real error.
# set +e for the duration so every upload attempt runs regardless of order.
# ---------------------------------------------------------------------------
on_exit() {
    local rc=$?
    set +e
    if [ -f "$CSPP_LOG" ]; then
        aws s3 cp "$CSPP_LOG" "${S3_PREFIX}/logs/cspp_sdr.log" \
            --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID" \
            >/dev/null 2>&1 || true
    fi
    aws s3 cp "$LOG_FILE" "${S3_PREFIX}/logs/aggregation.log" \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID" \
        >/dev/null 2>&1 || true
    return "$rc"
}
trap on_exit EXIT

# ---------------------------------------------------------------------------
# Validate binaries (after trap -- failure message lands in the log)
# ---------------------------------------------------------------------------
ts_log "INFO" "aggregation_entrypoint.sh starting: RTSTPS=${RTSTPS} contact=${CONTACT_ID} date=${CONTACT_DATE}"

test -x "${CSPP_HOME}/bin/viirs_sdr.sh" \
    || { ts_log "ERROR" "CSPP missing from image: ${CSPP_HOME}/bin/viirs_sdr.sh"; exit 1; }

if [ "$RTSTPS" = "on" ]; then
    test -x "${RTSTPS_HOME}/bin/batch.sh" \
        || { ts_log "ERROR" "RT-STPS missing from image: ${RTSTPS_HOME}/bin/batch.sh"; exit 1; }
fi

# ===========================================================================
# Step 1 (common): Extract J01 shipped LUTs
# ===========================================================================
# The SDR ECR image carries straylight and ecotiles but NOT shipped_luts/j01.
# Without j01 LUTs, check_installation() aborts: "SDR_4_1_DB/package needs to
# exist". Do NOT also extract straylight/static_tiles here -- they are already
# in the image and the redundant ~10 GB fill the Batch root volume (DEPLOYMENT.md req 1).
ts_log "INFO" "Step 1: extracting J01 shipped LUTs from s3://${OUTPUT_BUCKET}/software/"
aws s3 cp "s3://${OUTPUT_BUCKET}/software/CSPP_SDR_V4.1_static_luts_j01.tar.gz" \
    /tmp/j01luts.tgz
tar xzf /tmp/j01luts.tgz -C /opt/
rm -f /tmp/j01luts.tgz
test -d "${CSPP_HOME}/anc/static/shipped_luts/j01" \
    || { ts_log "ERROR" "shipped_luts/j01 missing after extract"; exit 1; }
ts_log "INFO" "Step 1: J01 shipped LUTs extracted OK"

# ===========================================================================
# Step 2 (common): PERF-1 LUT DB cache fast-path
# ===========================================================================
# Fast path (cache hit):  S3 sync stages the DB in seconds; sdr_luts.sh skipped
#                         entirely -- saves ~10 min per contact.
# Slow path (cache miss): first run before the weekly lut_cache_refresh job has
#                         populated the cache; sdr_luts.sh runs as fallback,
#                         identical to pre-PERF-1 behaviour.
#
# CONVERGENCE FIX: aggregation_sync.yml always ran sdr_luts.sh unconditionally
# (no PERF-1 cache). The converged entrypoint gives the sync branch the same
# fast-path for the first time.
ts_log "INFO" "Step 2: PERF-1 -- staging LUT DB from s3://${OUTPUT_BUCKET}/lut-cache/SDR_4_1_DB/"
aws s3 sync "s3://${OUTPUT_BUCKET}/lut-cache/SDR_4_1_DB/" \
    "${CSPP_HOME}/anc/static/SDR_4_1_DB/" || true

if test -e "${CSPP_HOME}/anc/static/SDR_4_1_DB/package"; then
    ts_log "INFO" "Step 2: PERF-1 cache hit -- LUT DB staged from S3, skipping sdr_luts.sh"
else
    ts_log "INFO" "Step 2: PERF-1 cache miss -- running sdr_luts.sh fallback (may take ~10 min)"
    ( cd "${CSPP_HOME}" || exit 1
      timeout 2400 ./bin/sdr_luts.sh > /tmp/luts.log 2>&1 ) || true
    if ! test -e "${CSPP_HOME}/anc/static/SDR_4_1_DB/package"; then
        ts_log "ERROR" "LUT DB absent after sdr_luts.sh fallback"
        tail -40 /tmp/luts.log >&2
        exit 1
    fi
    ts_log "INFO" "Step 2: sdr_luts.sh fallback complete"
fi

# ===========================================================================
# Branch: RTSTPS=on  (async -- CADU -> RT-STPS -> RDR -> CSPP -> SDR/GEO)
#   Ports scripts/aggregation.sh, minus EC2/self-stop/watchdog logic.
# ===========================================================================
if [ "$RTSTPS" = "on" ]; then

    ts_log "INFO" "=== async branch: CADU -> RT-STPS -> CSPP ==="

    # -----------------------------------------------------------------------
    # Step 3a: Download .cadu files from S3
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 3a: downloading .cadu from ${S3_PREFIX}/satdump/"
    aws s3 sync "${S3_PREFIX}/satdump/" "${WORK_DIR}/cadu/" \
        --exclude '*' --include '*.cadu'

    CADU_COUNT=$(find "${WORK_DIR}/cadu" -name '*.cadu' | wc -l)
    ts_log "INFO" "Step 3a: downloaded ${CADU_COUNT} .cadu file(s)"
    if [ "$CADU_COUNT" -eq 0 ]; then
        ts_log "ERROR" "No .cadu files in ${S3_PREFIX}/satdump/ -- aborting"
        exit 1
    fi

    # -----------------------------------------------------------------------
    # Step 4a: Concatenate .cadu files in sorted order -> combined.cadu
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 4a: concatenating ${CADU_COUNT} .cadu files in sorted order"
    # SC2038: filenames produced by SatDump contain no whitespace; sort order is
    # chunk-sequence order which is the required CADU reassembly order.
    # shellcheck disable=SC2038
    find "${WORK_DIR}/cadu" -name '*.cadu' | sort | xargs cat > "${WORK_DIR}/combined.cadu"
    COMBINED_SIZE=$(du -h "${WORK_DIR}/combined.cadu" | cut -f1)
    ts_log "INFO" "Step 4a: combined CADU size: ${COMBINED_SIZE}"

    # -----------------------------------------------------------------------
    # Step 5a: Configure RT-STPS jpss1.xml
    # Three munges (same as scripts/aggregation.sh):
    #   1. PnEncoded="true" -> "false"  (data arrives PN-decoded from SatDump)
    #   2. Delete the from="pn" to="reed_solomon" link node (bypass pn stage)
    #   3. from="frame_sync" to="pn" -> to="reed_solomon" (direct link)
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 5a: configuring RT-STPS jpss1.xml (PnEncoded=false, bypass pn)"
    cp "${RTSTPS_HOME}/config/jpss1.xml" "${WORK_DIR}/jpss1.xml"

    sed -i 's/PnEncoded="true"/PnEncoded="false"/' "${WORK_DIR}/jpss1.xml"
    sed -i '/from="pn" to="reed_solomon"/d' "${WORK_DIR}/jpss1.xml"
    sed -i 's|from="frame_sync" to="pn"|from="frame_sync" to="reed_solomon"|' \
        "${WORK_DIR}/jpss1.xml"

    # -----------------------------------------------------------------------
    # Step 6a: Clear /opt/data stale files, then run RT-STPS
    # /opt/data is RT-STPS's hardcoded output directory (set in jpss1.xml).
    # Clearing it prevents a previous contact's RDR from being picked up in
    # Step 7a or uploaded under this contact's S3 prefix.
    # -----------------------------------------------------------------------
    mkdir -p /opt/data
    STALE_H5=$(find /opt/data -maxdepth 1 -name '*.h5' | wc -l)
    STALE_PDS=$(find /opt/data -maxdepth 1 -name '*.PDS' | wc -l)
    ts_log "INFO" "Step 6a: clearing ${STALE_H5} .h5 and ${STALE_PDS} .PDS from /opt/data"
    find /opt/data -maxdepth 1 -name '*.h5'  -delete
    find /opt/data -maxdepth 1 -name '*.PDS' -delete

    ts_log "INFO" "Step 6a: running RT-STPS batch.sh"
    ( cd "$RTSTPS_HOME" || exit 1
      bin/batch.sh "${WORK_DIR}/jpss1.xml" "${WORK_DIR}/combined.cadu" )

    RDR_COUNT=$(find /opt/data -name '*.h5' | wc -l)
    ts_log "INFO" "Step 6a: RT-STPS produced ${RDR_COUNT} RDR HDF5 file(s)"

    # -----------------------------------------------------------------------
    # Step 7a: Run CSPP SDR (only if VIIRS RDR is present)
    # If RT-STPS produced no VIIRS RDR (e.g. non-VIIRS pass), warn and skip
    # rather than aborting -- the RDR upload in Step 8a still runs.
    # -----------------------------------------------------------------------
    VIIRS_RDR=$(find /opt/data -name 'RNSCA-RVIRS*.h5' | sort | head -1)

    if [ -n "$VIIRS_RDR" ]; then
        ts_log "INFO" "Step 7a: VIIRS RDR ${VIIRS_RDR} -- running CSPP viirs_sdr.sh"
        export CSPP_SDR_HOME
        export CSPP_RT_HOME
        "${CSPP_HOME}/bin/viirs_sdr.sh" --work-dir "${WORK_DIR}/sdr" -p 4 \
            "$VIIRS_RDR" >> "$CSPP_LOG" 2>&1
        SDR_COUNT=$(find "${WORK_DIR}/sdr" \( -name 'SV*.h5' -o -name 'G*.h5' \) | wc -l)
        ts_log "INFO" "Step 7a: CSPP SDR produced ${SDR_COUNT} SDR/GEO HDF5 file(s)"
    else
        ts_log "WARN" "Step 7a: no VIIRS RDR (RNSCA-RVIRS*.h5) from RT-STPS -- skipping CSPP SDR"
        SDR_COUNT=0
    fi

    # -----------------------------------------------------------------------
    # Step 8a: Upload SDR/GEO (SV*.h5, G*.h5) and RDR (*.h5) to S3
    # -----------------------------------------------------------------------
    if [ "$SDR_COUNT" -gt 0 ]; then
        ts_log "INFO" "Step 8a: uploading ${SDR_COUNT} SDR/GEO files to ${S3_PREFIX}/sdr/"
        aws s3 sync "${WORK_DIR}/sdr/" "${S3_PREFIX}/sdr/" \
            --exclude '*' --include '*.h5' \
            --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
        ts_log "INFO" "Step 8a: SDR/GEO upload complete"
    else
        ts_log "INFO" "Step 8a: no SDR/GEO files to upload"
    fi

    if [ "$RDR_COUNT" -gt 0 ]; then
        ts_log "INFO" "Step 8a: uploading ${RDR_COUNT} RDR files to ${S3_PREFIX}/rdr/"
        aws s3 sync /opt/data/ "${S3_PREFIX}/rdr/" \
            --exclude '*' --include '*.h5' \
            --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
        ts_log "INFO" "Step 8a: RDR upload complete"
    else
        ts_log "INFO" "Step 8a: no RDR files to upload"
    fi

# ===========================================================================
# Branch: RTSTPS=off  (sync -- RDR already uploaded -> CSPP -> SDR/GEO)
#   Ports buildspecs/aggregation_sync.yml.
# ===========================================================================
else

    ts_log "INFO" "=== sync branch: RDR -> CSPP -> SDR/GEO ==="

    # -----------------------------------------------------------------------
    # Step 3b: Download pre-made RDR from S3
    # The sync receiver (scripts/sync_receiver_finish.sh) ran RT-STPS right
    # after LOS and uploaded the RDR; it is already in S3 when we start.
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 3b: downloading RDR from ${S3_PREFIX}/rdr/"
    aws s3 sync "${S3_PREFIX}/rdr/" "${WORK_DIR}/rdr/" \
        --exclude '*' --include '*.h5'

    VIIRS_RDR=$(find "${WORK_DIR}/rdr" -name 'RNSCA-RVIRS*.h5' | sort | head -1)
    if [ -z "$VIIRS_RDR" ]; then
        ts_log "ERROR" "No VIIRS RDR (RNSCA-RVIRS*.h5) in ${S3_PREFIX}/rdr/ -- aborting (did sync_receiver_finish.sh run successfully?)"
        exit 1
    fi
    ts_log "INFO" "Step 3b: VIIRS RDR: ${VIIRS_RDR}"

    # -----------------------------------------------------------------------
    # Step 4b: Run CSPP SDR
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 4b: running CSPP viirs_sdr.sh"
    export CSPP_SDR_HOME
    export CSPP_RT_HOME
    "${CSPP_HOME}/bin/viirs_sdr.sh" --work-dir "${WORK_DIR}/sdr" -p 4 \
        "$VIIRS_RDR" >> "$CSPP_LOG" 2>&1

    SDR_COUNT=$(find "${WORK_DIR}/sdr" \( -name 'SV*.h5' -o -name 'G*.h5' \) | wc -l)
    ts_log "INFO" "Step 4b: CSPP SDR produced ${SDR_COUNT} SDR/GEO HDF5 file(s)"

    if [ "$SDR_COUNT" -eq 0 ]; then
        ts_log "ERROR" "CSPP SDR produced no output -- aborting"
        exit 1
    fi

    # -----------------------------------------------------------------------
    # Step 5b: Upload SDR/GEO to S3
    # RDR is already in S3 (uploaded by sync_receiver_finish.sh) -- no re-upload.
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 5b: uploading ${SDR_COUNT} SDR/GEO files to ${S3_PREFIX}/sdr/"
    aws s3 sync "${WORK_DIR}/sdr/" "${S3_PREFIX}/sdr/" \
        --exclude '*' --include '*.h5' \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
    ts_log "INFO" "Step 5b: SDR/GEO upload complete"

fi

# ===========================================================================
# Done
# ===========================================================================
ts_log "INFO" "aggregation_entrypoint.sh complete: RTSTPS=${RTSTPS} contact=${CONTACT_ID} SDR_COUNT=${SDR_COUNT}"
# EXIT trap uploads aggregation.log (and cspp_sdr.log if present) to S3.
