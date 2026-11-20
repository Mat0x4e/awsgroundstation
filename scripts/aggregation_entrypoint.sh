#!/bin/bash
# aggregation_entrypoint.sh -- the shared aggregation chain for AWS Batch.
#
# ONE chain for both downlink paths (architecture principle "converge at the
# first common artifact", docs/ARCHITECTURE.md): frames in S3 -> RT-STPS -> RDR
# -> CSPP -> SDR/GEO. The two paths differ only by where their frames are and
# whether they are still PN-randomized:
#   async (DigIF):         SatDump .cadu under satdump/, PN already removed
#                          -> CADU_PREFIX=satdump, PN_ENCODED=false (defaults)
#   sync (demod/decode):   receiver capture under cadu/, UncodedFramesEgress
#                          (no PN, no Reed-Solomon) -> CADU_PREFIX=cadu,
#                          PN_ENCODED=true (RT-STPS default jpss1 chain)
# Replaces buildspecs/aggregation.yml and aggregation_sync.yml. The former
# RTSTPS=off branch (RDR made on the sync receiver) was retired 2026-11-20:
# RT-STPS now runs only here, from the image.
#
# The Batch job downloads this script from S3 at startup so a redeploy takes
# effect without rebuilding the SDR ECR image -- same no-image-rebuild property
# as the CodeBuild buildspec approach. Deploy via scripts/deploy_aggregation_entrypoint.sh.
#
# Required env vars (set by Step Functions via ContainerOverrides.Environment):
#   RTSTPS          "on" (kept for compatibility with the job definition; the
#                   only accepted value since the RTSTPS=off retirement)
#   OUTPUT_BUCKET   S3 bucket; all artifacts under contacts/CONTACT_DATE/CONTACT_ID/
#   CONTACT_ID      Ground Station contact UUID
#   CONTACT_DATE    YYYY/MM/DD
#   KMS_KEY_ID      CMK ARN or key ID for SSE-KMS uploads
#
# Optional:
#   CADU_PREFIX     sub-prefix of the contact holding the *.cadu frames
#                   (default "satdump"; sync passes "cadu")
#   PN_ENCODED      "false" (default: frames already PN-derandomized, RT-STPS
#                   jpss1.xml patched to skip the pn node) or "true" (frames
#                   still PN-randomized: the image's pristine
#                   config/jpss1.pn-encoded.xml is used unmodified)
#   ASSETS_BUCKET   bucket holding the shared assets software/ (CSPP J01 LUTs)
#                   and lut-cache/ (PERF-1); default OUTPUT_BUCKET for async, REQUIRED
#                   when PN_ENCODED=true (fails if empty). The sync
#                   pipeline passes the SDR output bucket so both paths share
#                   one set of assets.
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
    on) ;;
    off) printf '[ERROR] RTSTPS=off was retired 2026-11-20: sync frames now go through RT-STPS here (CADU_PREFIX=cadu, PN_ENCODED=true)\n' >&2
         exit 1 ;;
    *) printf '[ERROR] RTSTPS must be "on" (got: "%s")\n' "${RTSTPS:-}" >&2
       exit 1 ;;
esac
case "${PN_ENCODED:=false}" in
    true|false) ;;
    *) printf '[ERROR] PN_ENCODED must be "true" or "false" (got: "%s")\n' "$PN_ENCODED" >&2
       exit 1 ;;
esac
CADU_PREFIX="${CADU_PREFIX:-satdump}"
: "${OUTPUT_BUCKET:?OUTPUT_BUCKET is required}"
# ASSETS_BUCKET: async keeps the fallback (assets and output share a bucket).
# Sync (PN_ENCODED=true) must name it: the sync bucket holds neither software/
# nor lut-cache/, so a silent fallback only fails later on a missing LUT archive.
if [ "$PN_ENCODED" = "true" ] && [ -z "${ASSETS_BUCKET:-}" ]; then
    printf '[ERROR] ASSETS_BUCKET is required when PN_ENCODED=true (sync): the sync output bucket does not hold software/ or lut-cache/; refusing to fall back to OUTPUT_BUCKET\n' >&2
    exit 1
fi
ASSETS_BUCKET="${ASSETS_BUCKET:-$OUTPUT_BUCKET}"
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
ts_log "INFO" "Step 1: extracting J01 shipped LUTs from s3://${ASSETS_BUCKET}/software/"
aws s3 cp "s3://${ASSETS_BUCKET}/software/CSPP_SDR_V4.1_static_luts_j01.tar.gz" \
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
ts_log "INFO" "Step 2: PERF-1 -- staging LUT DB from s3://${ASSETS_BUCKET}/lut-cache/SDR_4_1_DB/"
aws s3 sync "s3://${ASSETS_BUCKET}/lut-cache/SDR_4_1_DB/" \
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
# The shared chain: frames in S3 -> RT-STPS -> RDR -> CSPP -> SDR/GEO
#   Ports scripts/aggregation.sh, minus EC2/self-stop/watchdog logic.
# ===========================================================================

    ts_log "INFO" "=== shared chain: frames (${CADU_PREFIX}/, PN_ENCODED=${PN_ENCODED}) -> RT-STPS -> CSPP ==="

    # -----------------------------------------------------------------------
    # Step 3a: Download .cadu files from S3
    # -----------------------------------------------------------------------
    ts_log "INFO" "Step 3a: downloading .cadu from ${S3_PREFIX}/${CADU_PREFIX}/"
    aws s3 sync "${S3_PREFIX}/${CADU_PREFIX}/" "${WORK_DIR}/cadu/" \
        --exclude '*' --include '*.cadu'

    CADU_COUNT=$(find "${WORK_DIR}/cadu" -name '*.cadu' | wc -l)
    ts_log "INFO" "Step 3a: downloaded ${CADU_COUNT} .cadu file(s)"
    if [ "$CADU_COUNT" -eq 0 ]; then
        ts_log "ERROR" "No .cadu files in ${S3_PREFIX}/${CADU_PREFIX}/ -- aborting"
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
    # PN_ENCODED=false (async, SatDump frames already PN-derandomized): three
    # munges, same as scripts/aggregation.sh:
    #   1. PnEncoded="true" -> "false"
    #   2. Delete the from="pn" to="reed_solomon" link node (bypass pn stage)
    #   3. from="frame_sync" to="pn" -> to="reed_solomon" (direct link)
    # PN_ENCODED=true (sync, UncodedFramesEgress: no PN removal, no
    # Reed-Solomon): use the pristine RT-STPS config the image keeps as
    # config/jpss1.pn-encoded.xml, unmodified (frame_sync -> pn -> reed_solomon).
    # -----------------------------------------------------------------------
    if [ "$PN_ENCODED" = "true" ]; then
        PN_CONFIG="${RTSTPS_HOME}/config/jpss1.pn-encoded.xml"
        test -s "$PN_CONFIG" \
            || { ts_log "ERROR" "pristine RT-STPS config missing from image: ${PN_CONFIG} (rebuild the SDR image)"; exit 1; }
        ts_log "INFO" "Step 5a: using pristine RT-STPS config (PnEncoded=true, pn node kept)"
        cp "$PN_CONFIG" "${WORK_DIR}/jpss1.xml"
    else
        ts_log "INFO" "Step 5a: configuring RT-STPS jpss1.xml (PnEncoded=false, bypass pn)"
        cp "${RTSTPS_HOME}/config/jpss1.xml" "${WORK_DIR}/jpss1.xml"

        sed -i 's/PnEncoded="true"/PnEncoded="false"/' "${WORK_DIR}/jpss1.xml"
        sed -i '/from="pn" to="reed_solomon"/d' "${WORK_DIR}/jpss1.xml"
        sed -i 's|from="frame_sync" to="pn"|from="frame_sync" to="reed_solomon"|' \
            "${WORK_DIR}/jpss1.xml"
    fi

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

    # -----------------------------------------------------------------------
    # Step 9a: output contract for the sync configuration (PN_ENCODED=true).
    # A sync contact exists to produce imagery: zero VIIRS RDR or zero SDR/GEO
    # means the capture was not parsed (bad framing / PN), so the job must FAIL
    # rather than report a green pass with an empty sdr/ prefix. Runs after the
    # uploads so any RDR evidence is preserved. Async (PN_ENCODED=false) keeps
    # warn-and-continue for non-VIIRS passes.
    # -----------------------------------------------------------------------
    if [ "$PN_ENCODED" = "true" ]; then
        if [ -z "$VIIRS_RDR" ]; then
            ts_log "ERROR" "Step 9a: sync contact produced no VIIRS RDR (RNSCA-RVIRS*.h5; ${RDR_COUNT} other RDR file(s)) -- failing the job"
            exit 1
        fi
        if [ "$SDR_COUNT" -eq 0 ]; then
            ts_log "ERROR" "Step 9a: sync contact: CSPP produced zero SDR/GEO files from ${VIIRS_RDR} -- failing the job"
            exit 1
        fi
    fi


# ===========================================================================
# Done
# ===========================================================================
ts_log "INFO" "aggregation_entrypoint.sh complete: CADU_PREFIX=${CADU_PREFIX} PN_ENCODED=${PN_ENCODED} contact=${CONTACT_ID} SDR_COUNT=${SDR_COUNT}"
# EXIT trap uploads aggregation.log (and cspp_sdr.log if present) to S3.
