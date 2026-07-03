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

echo "[SatDump] Processing: ${INPUT_CS8}"
echo "[SatDump] Output dir: ${OUTPUT_DIR}"

# Execute SatDump npp_hrd pipeline, capturing stdout/stderr to log
satdump npp_hrd baseband "${INPUT_CS8}" "${OUTPUT_DIR}" \
    --samplerate 34312500 \
    --baseband_format cs8 \
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
