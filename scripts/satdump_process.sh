#!/bin/bash
# satdump_process.sh — SatDump npp_hrd wrapper with output validation
# Executes SatDump to demodulate/decode baseband I/Q (.cs8) into CADU frames.
#
# Usage: satdump_process.sh <input.cs8> <output_dir>
#
# Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "[ERROR] Usage: $0 <input_cs8> <output_dir>"
    exit 1
fi

INPUT_CS8="$1"
OUTPUT_DIR="$2"

# Validate input file exists and is non-empty
if [ ! -f "$INPUT_CS8" ]; then
    echo "[ERROR] Input file does not exist: ${INPUT_CS8}"
    exit 1
fi

if [ ! -s "$INPUT_CS8" ]; then
    echo "[ERROR] Input file is empty: ${INPUT_CS8}"
    exit 1
fi

# Ensure output directory exists
mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# TLE provisioning.
#
# SatDump resolves satellite positions through a TLE, and its own updater
# fetches CelesTrak over plain http at startup. That fetch fails in CodeBuild:
# "0 TLEs loaded! ... Error updating TLEs. Not updated."
#
# Without a TLE for NORAD 43013 the composites still decode, so this went
# unnoticed for months -- but projection resolves every ground control point to
# the same place, so the bounds collapse to the whole globe, the output is
# sized 32000x16000 and SatDump segfaults (2026-09-01).
#
# --tle_override makes SatDump load exactly this file and skip its network
# update entirely. Prefer a fresh fetch; fall back to the TLE baked into the
# image, which is only ever a propagation accuracy question, never a crash.
# ---------------------------------------------------------------------------
BUNDLED_TLE="$(dirname "$0")/tle/noaa20.tle"
TLE_FILE="${OUTPUT_DIR}/noaa20.tle"
NORAD=43013

if curl -fsSL --max-time 20 --retry 2 \
        "https://celestrak.org/NORAD/elements/gp.php?CATNR=${NORAD}&FORMAT=tle" \
        -o "${TLE_FILE}.fetched" 2>/dev/null \
   && grep -q "^1 ${NORAD}" "${TLE_FILE}.fetched" \
   && grep -q "^2 ${NORAD}" "${TLE_FILE}.fetched"; then
    mv "${TLE_FILE}.fetched" "${TLE_FILE}"
    echo "[SatDump] TLE: fetched fresh from CelesTrak"
elif [ -s "$BUNDLED_TLE" ]; then
    rm -f "${TLE_FILE}.fetched"
    cp "$BUNDLED_TLE" "$TLE_FILE"
    echo "[SatDump] TLE: CelesTrak unreachable, using bundled ${BUNDLED_TLE}"
else
    echo "[ERROR] No TLE available and none bundled at ${BUNDLED_TLE}"
    echo "[ERROR] Projection would collapse to global bounds and crash"
    exit 1
fi
echo "[SatDump] TLE epoch line: $(sed -n '2p' "$TLE_FILE" | cut -c19-32)"

echo "[SatDump] Processing: ${INPUT_CS8}"
echo "[SatDump] Output dir: ${OUTPUT_DIR}"

# Execute SatDump npp_hrd pipeline, capturing stdout/stderr to log
satdump npp_hrd baseband "${INPUT_CS8}" "${OUTPUT_DIR}" \
    --samplerate 34312500 \
    --baseband_format cs8 \
    --tle_override "${TLE_FILE}" \
    2>&1 | tee "${OUTPUT_DIR}/satdump.log"

EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -ne 0 ]; then
    echo "[ERROR] SatDump failed with exit code ${EXIT_CODE}"
    echo "[ERROR] Full log available at: ${OUTPUT_DIR}/satdump.log"
    cat "${OUTPUT_DIR}/satdump.log" >&2
    exit $EXIT_CODE
fi

# Validation: .cadu file must exist and be non-empty
CADU_FILE=$(find "${OUTPUT_DIR}" -name "*.cadu" -type f | head -1)
if [ -z "$CADU_FILE" ] || [ ! -s "$CADU_FILE" ]; then
    echo "[ERROR] No valid .cadu file produced"
    echo "[ERROR] SatDump ran successfully but produced no demodulated frames"
    echo "[ERROR] Check signal quality in: ${OUTPUT_DIR}/satdump.log"
    exit 1
fi

# Validation: dataset.json should exist (warning if missing)
if [ ! -f "${OUTPUT_DIR}/dataset.json" ]; then
    echo "[WARNING] No dataset.json produced — metadata unavailable for downstream processing"
fi

# Success — report file size
CADU_SIZE=$(du -h "$CADU_FILE" | cut -f1)
echo "[SatDump] Success: ${CADU_SIZE} CADU produced at ${CADU_FILE}"
