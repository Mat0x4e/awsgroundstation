#!/usr/bin/env bats
# Unit tests for scripts/satdump_process.sh — SatDump wrapper validation logic
# Tests: error propagation (Req 2.4), empty .cadu detection (Req 2.5), missing dataset.json warning (Req 2.5)
#
# These tests mock the `satdump` command to isolate the wrapper's validation logic.
# Run: bats tests/test_satdump_process.bats

SCRIPT_UNDER_TEST="scripts/satdump_process.sh"

setup() {
    # Create temp directories for test I/O
    TEST_DIR="$(mktemp -d)"
    INPUT_DIR="${TEST_DIR}/input"
    OUTPUT_DIR="${TEST_DIR}/output"
    MOCK_BIN="${TEST_DIR}/mock_bin"

    mkdir -p "$INPUT_DIR" "$OUTPUT_DIR" "$MOCK_BIN"

    # Create a valid (non-empty) input .cs8 file
    echo "fake baseband data" > "${INPUT_DIR}/test.cs8"

    # Prepend mock bin to PATH so our fake `satdump` is found first
    export PATH="${MOCK_BIN}:${PATH}"
}

teardown() {
    rm -rf "$TEST_DIR"
}

# --- Helper: create a mock satdump that succeeds and produces expected outputs ---
create_mock_satdump_success() {
    local cadu_content="${1:-fake_cadu_frame_data}"
    local produce_dataset_json="${2:-true}"

    cat > "${MOCK_BIN}/satdump" << 'MOCK_SCRIPT'
#!/bin/bash
# Mock satdump — simulates successful execution
OUTPUT_DIR="$4"  # satdump npp_hrd baseband <input> <output_dir> ...
MOCK_SCRIPT

    # Append dynamic parts
    cat >> "${MOCK_BIN}/satdump" << MOCK_DYNAMIC
echo "${cadu_content}" > "\${OUTPUT_DIR}/npp_hrd.cadu"
MOCK_DYNAMIC

    if [ "$produce_dataset_json" = "true" ]; then
        cat >> "${MOCK_BIN}/satdump" << 'MOCK_JSON'
cat > "${OUTPUT_DIR}/dataset.json" << 'EOF'
{"satellite": "NOAA-20", "frames_decoded": 1500, "snr": 12.5}
EOF
MOCK_JSON
    fi

    cat >> "${MOCK_BIN}/satdump" << 'MOCK_END'
echo "SatDump mock: processing complete"
exit 0
MOCK_END

    chmod +x "${MOCK_BIN}/satdump"
}

# --- Helper: create a mock satdump that fails with given exit code ---
create_mock_satdump_failure() {
    local exit_code="${1:-1}"

    cat > "${MOCK_BIN}/satdump" << MOCK_SCRIPT
#!/bin/bash
# Mock satdump — simulates failure
OUTPUT_DIR="\$4"
echo "ERROR: demodulation failed — no signal lock" >&2
exit ${exit_code}
MOCK_SCRIPT

    chmod +x "${MOCK_BIN}/satdump"
}

# --- Helper: create a mock satdump that succeeds but produces an empty .cadu ---
create_mock_satdump_empty_cadu() {
    cat > "${MOCK_BIN}/satdump" << 'MOCK_SCRIPT'
#!/bin/bash
# Mock satdump — succeeds but produces empty .cadu
OUTPUT_DIR="$4"
touch "${OUTPUT_DIR}/npp_hrd.cadu"  # empty file
cat > "${OUTPUT_DIR}/dataset.json" << 'EOF'
{"satellite": "NOAA-20", "frames_decoded": 0, "snr": 0.0}
EOF
echo "SatDump mock: processing complete (no frames)"
exit 0
MOCK_SCRIPT

    chmod +x "${MOCK_BIN}/satdump"
}

# --- Helper: create a mock satdump that succeeds but produces no .cadu file at all ---
create_mock_satdump_no_cadu() {
    cat > "${MOCK_BIN}/satdump" << 'MOCK_SCRIPT'
#!/bin/bash
# Mock satdump — succeeds but produces no .cadu
OUTPUT_DIR="$4"
cat > "${OUTPUT_DIR}/dataset.json" << 'EOF'
{"satellite": "NOAA-20", "frames_decoded": 0}
EOF
echo "SatDump mock: processing complete (no output)"
exit 0
MOCK_SCRIPT

    chmod +x "${MOCK_BIN}/satdump"
}

# ==============================================================================
# Test: Error propagation on non-zero exit code (Requirement 2.4)
# ==============================================================================

@test "non-zero exit code from satdump is propagated" {
    create_mock_satdump_failure 42

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 42 ]
}

@test "satdump failure with exit code 1 propagates correctly" {
    create_mock_satdump_failure 1

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    # set -eo pipefail causes immediate exit on satdump failure through the pipe
    [ "$status" -eq 1 ]
}

@test "stderr from satdump is captured on failure" {
    create_mock_satdump_failure 3

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 3 ]
    # The log file should exist (tee writes regardless of exit code)
    [ -f "${OUTPUT_DIR}/satdump.log" ]
}

# ==============================================================================
# Test: Empty .cadu detection (Requirement 2.5)
# ==============================================================================

@test "empty .cadu file causes exit 1" {
    create_mock_satdump_empty_cadu

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 1 ]
}

@test "empty .cadu produces descriptive error message" {
    create_mock_satdump_empty_cadu

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [[ "$output" == *"[ERROR] No valid .cadu file produced"* ]]
}

@test "no .cadu file at all causes exit 1" {
    create_mock_satdump_no_cadu

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] No valid .cadu file produced"* ]]
}

# ==============================================================================
# Test: Missing dataset.json warning (Requirement 2.5)
# ==============================================================================

@test "missing dataset.json emits warning but does not fail" {
    create_mock_satdump_success "valid_cadu_data" "false"

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARNING] No dataset.json produced"* ]]
}

@test "present dataset.json does not emit warning" {
    create_mock_satdump_success "valid_cadu_data" "true"

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 0 ]
    [[ "$output" != *"WARNING"* ]]
}

# ==============================================================================
# Test: Happy path — successful processing (sanity check)
# ==============================================================================

@test "successful processing exits 0 with success message" {
    create_mock_satdump_success "valid_cadu_frame_data" "true"

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/test.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 0 ]
    [[ "$output" == *"[SatDump] Success:"* ]]
}

# ==============================================================================
# Test: Input validation
# ==============================================================================

@test "missing input file causes exit 1" {
    run bash "$SCRIPT_UNDER_TEST" "/nonexistent/file.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] Input file does not exist"* ]]
}

@test "empty input file causes exit 1" {
    > "${INPUT_DIR}/empty.cs8"  # create empty file

    run bash "$SCRIPT_UNDER_TEST" "${INPUT_DIR}/empty.cs8" "$OUTPUT_DIR"

    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] Input file is empty"* ]]
}

@test "wrong number of arguments causes exit 1" {
    run bash "$SCRIPT_UNDER_TEST"

    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] Usage:"* ]]
}
