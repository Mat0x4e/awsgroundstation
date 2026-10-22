#!/bin/bash
# render_entrypoint.sh -- VIIRS visualization render entrypoint for AWS Batch.
#
# Replaces the SATDUMP_BUILDSPEC and NASA_BUILDSPEC inline string constants in
# lambdas/viirs_visualizer/handler.py via a single RENDER_BRANCH=satdump|nasa
# environment variable. Stage 3 of the Clean Compute Pipeline Migration (ADR-002).
#
# Deploy: the script is baked into the viirs-visualization Docker image at build
# time via "COPY scripts/ /opt/scripts/" in docker/viirs-visualization/Dockerfile.
# No S3-pull bootstrap is needed (unlike aggregation_entrypoint.sh).
# Batch job command: ["/bin/bash", "/opt/scripts/viirs/render_entrypoint.sh"]
# In-image path:     /opt/scripts/viirs/render_entrypoint.sh
#
# Required env vars (set by viirs-orchestrator Lambda via ContainerOverrides.Environment
# at Batch submit time -- NOT via Ref:: job-definition placeholders):
#   RENDER_BRANCH    "satdump" or "nasa"
#   INPUT_BUCKET     SDR output bucket name
#   INPUT_PREFIX     contacts/{CONTACT_DATE}/{CONTACT_ID}  (Lambda pre-computes this)
#   CONTACT_ID       Ground Station contact UUID
#   CONTACT_DATE     YYYY/MM/DD
#   KMS_KEY_ID       CMK ARN or key ID for SSE-KMS uploads
#   ENABLE_GEOTIFF   "true" or "false"
#   ENABLE_DESTRIPE  "true" or "false" (NASA branch only)
#
# Optional env vars (SatDump branch only; pass empty string when unknown):
#   CONTACT_TIME      HH:MM:SS  -- passed as --contact-time if non-empty
#   CHUNK_DURATION_S  numeric   -- passed as --pass-duration-seconds if non-empty
#   These replace the __CONTACT_TIME_ARG__ / __PASS_DURATION_ARG__ token
#   substitution previously done by the Lambda before calling start_build().
#
# No SELF_STOP, no watchdog: the container is torn down by Batch on exit.
# No log upload in the EXIT trap (render logs go to CloudWatch via the Batch
# awslogs driver; no separate aggregation.log upload is needed).

set -euo pipefail

# ---------------------------------------------------------------------------
# Validate required env vars -- before the EXIT trap so failures are loud
# and are not misreported as render failures.
# ---------------------------------------------------------------------------
case "${RENDER_BRANCH:-}" in
    satdump|nasa) ;;
    *) printf '[ERROR] RENDER_BRANCH must be "satdump" or "nasa" (got: "%s")\n' \
           "${RENDER_BRANCH:-}" >&2
       exit 1 ;;
esac

: "${INPUT_BUCKET:?INPUT_BUCKET is required}"
: "${INPUT_PREFIX:?INPUT_PREFIX is required}"
: "${CONTACT_ID:?CONTACT_ID is required}"
: "${CONTACT_DATE:?CONTACT_DATE is required}"
: "${KMS_KEY_ID:?KMS_KEY_ID is required}"
: "${ENABLE_GEOTIFF:?ENABLE_GEOTIFF is required}"

# ---------------------------------------------------------------------------
# Structured logging -- timestamped lines to stdout (-> CloudWatch via Batch)
# ---------------------------------------------------------------------------
ts_log() {
    local level="$1"
    local msg="$2"
    local ts
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    printf '[%s] [%s] %s\n' "$ts" "$level" "$msg"
}

# ---------------------------------------------------------------------------
# EXIT trap: emit VisualizationFailed metric on any non-zero exit (Req 3.4).
# Fires for render failures, unexpected set -e exits, and explicit exit calls.
# set +e during the trap so the metric call cannot mask the original error code.
# ---------------------------------------------------------------------------
on_exit() {
    local rc=$?
    set +e
    if [ "$rc" -ne 0 ]; then
        ts_log "ERROR" "render_entrypoint.sh exiting with code ${rc} -- emitting VisualizationFailed metric"
        aws cloudwatch put-metric-data \
            --namespace SDRPipeline \
            --metric-name VisualizationFailed \
            --unit Count \
            --value 1 \
            --dimensions "Name=ContactId,Value=${CONTACT_ID}" || true
    fi
    return "$rc"
}
trap on_exit EXIT

# Matplotlib must not try to open a display inside the container.
export MPLBACKEND="Agg"

ts_log "INFO" "render_entrypoint.sh starting: RENDER_BRANCH=${RENDER_BRANCH} contact=${CONTACT_ID} date=${CONTACT_DATE}"

# ===========================================================================
# RENDER_BRANCH=satdump
# Ports SATDUMP_BUILDSPEC from lambdas/viirs_visualizer/handler.py exactly.
# ===========================================================================
if [ "$RENDER_BRANCH" = "satdump" ]; then

    # -----------------------------------------------------------------------
    # pre_build: Download SatDump VIIRS composites and coordinates from S3
    # -----------------------------------------------------------------------
    ts_log "INFO" "=== satdump pre_build: downloading SatDump VIIRS outputs from S3 ==="

    mkdir -p /tmp/input/chunks /tmp/input/satdump /tmp/input/coordinates /tmp/output

    ts_log "INFO" "Syncing satdump VIIRS files from s3://${INPUT_BUCKET}/${INPUT_PREFIX}/satdump/"
    aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/satdump/" /tmp/input/chunks/ \
        --exclude "*" \
        --include "*/VIIRS/*.png" \
        --include "*/VIIRS/*.cbor" \
        --include "*/VIIRS/*.georef" \
        --include "*/VIIRS/*_projected.tif"

    # SatDumpVisualizer.discover_composites globs its input dir non-recursively,
    # so flatten the per-chunk subdirs into a single staging directory.
    # Lowest chunk number wins each filename (chunk_0 = first 30s after AOS =
    # southernmost swath segment). This is the layout every successful manual
    # run used (see .build/ reference outputs).
    ts_log "INFO" "Flattening per-chunk VIIRS folders into /tmp/input/satdump/ (lowest chunk wins)"
    while IFS= read -r d; do
        cp -n "$d"/VIIRS/* /tmp/input/satdump/ 2>/dev/null || true
    done < <(find /tmp/input/chunks -maxdepth 1 -type d -name 'chunk_*' 2>/dev/null | sort -V)

    COMPOSITE_COUNT=$(find /tmp/input/satdump -maxdepth 1 -type f | wc -l)
    ts_log "INFO" "Composites staged: ${COMPOSITE_COUNT} file(s) in /tmp/input/satdump/"

    ts_log "INFO" "Syncing coordinates from s3://${INPUT_BUCKET}/${INPUT_PREFIX}/coordinates/"
    aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/coordinates/" /tmp/input/coordinates/

    # -----------------------------------------------------------------------
    # build: Run SatDump visualization pipeline
    #
    # --contact-time and --pass-duration-seconds are appended only when their
    # env vars are non-empty.  This replaces the __CONTACT_TIME_ARG__ /
    # __PASS_DURATION_ARG__ token substitution that the Lambda previously
    # applied to the buildspec string before calling start_build().
    # -----------------------------------------------------------------------
    ts_log "INFO" "=== satdump build: running SatDump visualization pipeline ==="

    RENDER_ARGS=(
        --input-dir        /tmp/input/satdump
        --coordinates-dir  /tmp/input/coordinates
        --output-dir       /tmp/output
        --contact-id       "${CONTACT_ID}"
        --contact-date     "${CONTACT_DATE}"
        --enable-geotiff   "${ENABLE_GEOTIFF}"
    )

    if [ -n "${CONTACT_TIME:-}" ]; then
        RENDER_ARGS+=(--contact-time "${CONTACT_TIME}")
        ts_log "INFO" "Optional arg: --contact-time ${CONTACT_TIME}"
    fi

    if [ -n "${CHUNK_DURATION_S:-}" ]; then
        RENDER_ARGS+=(--pass-duration-seconds "${CHUNK_DURATION_S}")
        ts_log "INFO" "Optional arg: --pass-duration-seconds ${CHUNK_DURATION_S}"
    fi

    RENDER_RC=0
    python3 /opt/scripts/viirs/visualize_satdump.py "${RENDER_ARGS[@]}" || RENDER_RC=$?

    if [ "$RENDER_RC" -ne 0 ]; then
        ts_log "ERROR" "visualize_satdump.py exited with code ${RENDER_RC}"
        exit "$RENDER_RC"
    fi

# ===========================================================================
# RENDER_BRANCH=nasa
# Ports NASA_BUILDSPEC from lambdas/viirs_visualizer/handler.py exactly.
# ===========================================================================
else

    # NASA branch requires ENABLE_DESTRIPE; validate here (not globally, as
    # the SatDump branch does not use it and the Lambda may not pass it).
    : "${ENABLE_DESTRIPE:?ENABLE_DESTRIPE is required for nasa branch}"

    # -----------------------------------------------------------------------
    # pre_build: Download SDR + GEO HDF5 files from S3
    # -----------------------------------------------------------------------
    ts_log "INFO" "=== nasa pre_build: downloading SDR + GEO files from S3 ==="

    mkdir -p /tmp/input/sdr /tmp/output

    # aggregation.sh uploads CSPP output to <prefix>/sdr/. <prefix>/chunks/
    # holds per-chunk RT-STPS metadata only -- no .h5 ever lands there.
    # Both the terrain-corrected names CSPP writes for J01 (SVM15/GITCO/GMTCO)
    # and the older ellipsoid ones (SVOM15/GIGTO/GMODO) are pulled.
    ts_log "INFO" "Syncing SDR/GEO from s3://${INPUT_BUCKET}/${INPUT_PREFIX}/sdr/"
    aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/sdr/" /tmp/input/sdr/ \
        --exclude "*" \
        --include "SVI0*.h5" \
        --include "SVM15*.h5" \
        --include "SVOM15*.h5" \
        --include "GITCO*.h5" \
        --include "GIGTO*.h5" \
        --include "GMTCO*.h5" \
        --include "GMODO*.h5"

    SDR_FILE_COUNT=$(find /tmp/input/sdr -maxdepth 1 -name '*.h5' | wc -l)
    ts_log "INFO" "Downloaded ${SDR_FILE_COUNT} HDF5 file(s) to /tmp/input/sdr/"

    # -----------------------------------------------------------------------
    # build: Run NASA visualization pipeline
    # -----------------------------------------------------------------------
    ts_log "INFO" "=== nasa build: running NASA visualization pipeline ==="

    RENDER_RC=0
    python3 /opt/scripts/viirs/visualize_nasa.py \
        --input-dir       /tmp/input/sdr \
        --output-dir      /tmp/output \
        --contact-id      "${CONTACT_ID}" \
        --contact-date    "${CONTACT_DATE}" \
        --enable-geotiff  "${ENABLE_GEOTIFF}" \
        --enable-destripe "${ENABLE_DESTRIPE}" \
        || RENDER_RC=$?

    if [ "$RENDER_RC" -ne 0 ]; then
        ts_log "ERROR" "visualize_nasa.py exited with code ${RENDER_RC}"
        exit "$RENDER_RC"
    fi

fi

# ===========================================================================
# post_build (both branches): Upload products to S3 + emit ProductsWritten
# ===========================================================================
ts_log "INFO" "=== post_build: uploading products to S3 ==="

aws s3 sync /tmp/output/ \
    "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/" \
    --sse aws:kms --sse-kms-key-id "${KMS_KEY_ID}"

# DISPATCH-VS-COMPLETION (REL-4): emit ProductsWritten AFTER the sync so the
# metric reflects what was actually uploaded, not the count at dispatch time.
# The Batch job role needs cloudwatch:PutMetricData scoped to namespace SDRPipeline.
# The metric emit is non-fatal (|| echo "WARNING") so a CloudWatch blip cannot
# roll back a successful render.
PRODUCT_COUNT=$(aws s3 ls \
    "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/" \
    --recursive 2>/dev/null | wc -l | tr -d ' ' || echo "0")

aws cloudwatch put-metric-data \
    --namespace SDRPipeline \
    --metric-name ProductsWritten \
    --unit Count \
    --value "${PRODUCT_COUNT}" \
    --dimensions "Name=ContactId,Value=${CONTACT_ID}" \
    || echo "WARNING: ProductsWritten metric publish failed (non-fatal)"

ts_log "INFO" "ProductsWritten metric published: contact_id=${CONTACT_ID} count=${PRODUCT_COUNT}"
ts_log "INFO" "render_entrypoint.sh complete: RENDER_BRANCH=${RENDER_BRANCH} contact=${CONTACT_ID}"
# EXIT trap fires here with rc=0 -- VisualizationFailed metric is NOT emitted.
