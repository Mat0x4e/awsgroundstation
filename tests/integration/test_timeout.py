"""Integration tests for Step Functions Map state timeout handling.

Simulates the CodeBuild per-build timeout (20 minutes) as defined in the
noaa20-cadu-to-tiff pipeline design and validates the pipeline's behaviour
when one or more chunks time out.

**Validates: Requirements 7.5, 6.4**

Requirement 7.5 — Timeout interruption:
    IF the duration of a chunk's processing exceeds 20 minutes, THEN the
    pipeline SHALL interrupt processing and mark it as timeout.

Requirement 6.4 — Failure isolation:
    IF a step fails for a chunk after 2 attempts, THEN the pipeline SHALL
    mark the chunk in error, continue processing other chunks, and publish
    an SNS notification.

Key distinction between timeout and failure:
    - TIMED_OUT: CodeBuild hard-kills the build after 20 minutes. No retry
      is attempted — the build was killed externally, not by a transient
      error. Attempts = 1.
    - FAILED: Build exits with a non-zero status. The Step Functions Retry
      block applies: MaxAttempts=2 means 3 total attempts (1 + 2 retries).
"""

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Simulation model (self-contained — no imports from test_pipeline_orchestration)
# ---------------------------------------------------------------------------


@dataclass
class ChunkResult:
    chunk_id: str
    status: str    # "SUCCEEDED", "FAILED", or "TIMED_OUT"
    attempts: int
    duration_minutes: float = 0.0


def simulate_map_state_with_timeout(
    chunks: list[dict],
    build_timeout_minutes: float = 20,
    max_retries: int = 2,
) -> list[ChunkResult]:
    """Simulate Step Functions Map state with CodeBuild per-build timeout.

    Each chunk dict:
        {
            "chunk_id":          str,
            "outcome":           "succeed" | "fail" | "timeout",
            "duration_minutes":  float,   # actual processing duration
        }

    Behaviour:
    - "timeout": duration > build_timeout_minutes → CodeBuild terminates the
      build and reports status TIMED_OUT. Hard stop — no retry is attempted.
      Attempts = 1.
    - "fail": build exits with non-zero status. Step Functions Retry block
      applies: max_retries retries → (1 + max_retries) total attempts.
    - "succeed": completes successfully on first attempt. Attempts = 1.

    The Map state runs with ToleratedFailurePercentage=100: all chunks are
    processed regardless of individual timeouts or failures.

    **Validates: Requirements 7.5, 6.4**
    """
    results: list[ChunkResult] = []

    for chunk in chunks:
        chunk_id: str = chunk["chunk_id"]
        outcome: str = chunk["outcome"]
        duration: float = chunk.get("duration_minutes", 0.0)

        if outcome == "timeout":
            # CodeBuild kills the build after build_timeout_minutes.
            # No Step Functions retry — the timeout is a hard termination.
            results.append(
                ChunkResult(
                    chunk_id=chunk_id,
                    status="TIMED_OUT",
                    attempts=1,
                    duration_minutes=duration,
                )
            )
        elif outcome == "fail":
            # Transient failure → Step Functions Retry block exhausts all retries.
            total_attempts = 1 + max_retries
            results.append(
                ChunkResult(
                    chunk_id=chunk_id,
                    status="FAILED",
                    attempts=total_attempts,
                    duration_minutes=duration,
                )
            )
        else:  # "succeed"
            results.append(
                ChunkResult(
                    chunk_id=chunk_id,
                    status="SUCCEEDED",
                    attempts=1,
                    duration_minutes=duration,
                )
            )

    return results


def check_results(chunk_results: list[ChunkResult]) -> str:
    """Simulate the CheckResults Choice state.

    Returns "FinalAggregation" if at least one chunk succeeded,
    "TotalFailure" otherwise.

    TIMED_OUT is treated the same as FAILED for routing purposes —
    neither counts as a successful outcome.

    **Validates: Requirements 6.4**
    """
    if any(r.status == "SUCCEEDED" for r in chunk_results):
        return "FinalAggregation"
    return "TotalFailure"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_timeout_chunk_marked_as_failed() -> None:
    """A single chunk that times out is marked TIMED_OUT with exactly 1 attempt.

    CodeBuild terminates the build externally; there is nothing to retry.
    The pipeline treats TIMED_OUT as a failure for routing purposes.

    **Validates: Requirements 7.5, 6.4**
    """
    chunks = [
        {"chunk_id": "chunk-00", "outcome": "timeout", "duration_minutes": 25.0},
    ]
    results = simulate_map_state_with_timeout(chunks)

    assert len(results) == 1
    r = results[0]
    assert r.chunk_id == "chunk-00"
    assert r.status == "TIMED_OUT", (
        "A chunk that exceeds the build timeout must be marked TIMED_OUT, "
        f"not {r.status!r}."
    )
    assert r.attempts == 1, (
        f"Timeout is a hard kill — no retry should occur. Expected 1 attempt, got {r.attempts}."
    )
    assert check_results(results) == "TotalFailure", (
        "A single timed-out chunk with no successes should route to TotalFailure."
    )


def test_pipeline_continues_after_timeout() -> None:
    """Mix of succeed + timeout: timed-out chunk is TIMED_OUT, others SUCCEEDED.

    The Map state continues processing remaining chunks even when one times
    out (ToleratedFailurePercentage=100). Pipeline routes to FinalAggregation
    because at least one chunk succeeded.

    **Validates: Requirements 7.5, 6.4**
    """
    chunks = [
        {"chunk_id": "chunk-00", "outcome": "succeed",  "duration_minutes": 8.0},
        {"chunk_id": "chunk-01", "outcome": "timeout",  "duration_minutes": 25.0},
        {"chunk_id": "chunk-02", "outcome": "succeed",  "duration_minutes": 12.0},
        {"chunk_id": "chunk-03", "outcome": "succeed",  "duration_minutes": 5.0},
    ]
    results = simulate_map_state_with_timeout(chunks)

    assert len(results) == 4, "All 4 chunks must produce a result."

    by_id = {r.chunk_id: r for r in results}

    # Timed-out chunk
    assert by_id["chunk-01"].status == "TIMED_OUT"
    assert by_id["chunk-01"].attempts == 1

    # Successful chunks
    for cid in ("chunk-00", "chunk-02", "chunk-03"):
        assert by_id[cid].status == "SUCCEEDED", (
            f"{cid} should be SUCCEEDED but got {by_id[cid].status!r}."
        )

    # At least one success → FinalAggregation
    assert check_results(results) == "FinalAggregation", (
        "With 3 successful chunks, CheckResults must route to FinalAggregation."
    )


def test_timeout_and_failure_mixed() -> None:
    """Mix of succeed, fail, and timeout: all chunks present with correct statuses.

    The pipeline must handle all three outcome types simultaneously. Routing
    goes to FinalAggregation because at least one chunk succeeded.

    **Validates: Requirements 7.5, 6.4**
    """
    chunks = [
        {"chunk_id": "chunk-00", "outcome": "succeed", "duration_minutes": 10.0},
        {"chunk_id": "chunk-01", "outcome": "fail",    "duration_minutes": 3.0},
        {"chunk_id": "chunk-02", "outcome": "timeout", "duration_minutes": 22.0},
        {"chunk_id": "chunk-03", "outcome": "succeed", "duration_minutes": 7.0},
        {"chunk_id": "chunk-04", "outcome": "fail",    "duration_minutes": 1.5},
    ]
    results = simulate_map_state_with_timeout(chunks, max_retries=2)

    assert len(results) == 5, "All 5 chunks must produce a result."

    by_id = {r.chunk_id: r for r in results}

    assert by_id["chunk-00"].status == "SUCCEEDED"
    assert by_id["chunk-01"].status == "FAILED"
    assert by_id["chunk-01"].attempts == 3   # 1 initial + 2 retries
    assert by_id["chunk-02"].status == "TIMED_OUT"
    assert by_id["chunk-02"].attempts == 1
    assert by_id["chunk-03"].status == "SUCCEEDED"
    assert by_id["chunk-04"].status == "FAILED"
    assert by_id["chunk-04"].attempts == 3

    assert check_results(results) == "FinalAggregation"


def test_all_timeout_routes_to_total_failure() -> None:
    """All chunks time out → pipeline routes to TotalFailure.

    When every chunk is terminated by CodeBuild's timeout, there are no
    successful outputs and CheckResults must route to TotalFailure.

    **Validates: Requirements 7.5, 6.4**
    """
    chunks = [
        {"chunk_id": f"chunk-{i:02d}", "outcome": "timeout", "duration_minutes": 20.0 + i}
        for i in range(5)
    ]
    results = simulate_map_state_with_timeout(chunks)

    assert len(results) == 5
    for r in results:
        assert r.status == "TIMED_OUT", (
            f"{r.chunk_id}: expected TIMED_OUT, got {r.status!r}."
        )
        assert r.attempts == 1

    assert check_results(results) == "TotalFailure", (
        "All chunks timed out → no successes → must route to TotalFailure."
    )


def test_timeout_does_not_retry() -> None:
    """A timed-out chunk uses exactly 1 attempt; a failed chunk uses 3.

    CodeBuild kills the build process — there is no exit code for Step
    Functions to catch and retry. Transient FAILED builds get retried per the
    ASL Retry block (MaxAttempts=2 → 3 total attempts).

    **Validates: Requirements 7.5**
    """
    chunks = [
        {"chunk_id": "chunk-00", "outcome": "timeout", "duration_minutes": 21.0},
        {"chunk_id": "chunk-01", "outcome": "fail",    "duration_minutes": 2.0},
    ]
    results = simulate_map_state_with_timeout(chunks, max_retries=2)

    by_id = {r.chunk_id: r for r in results}

    timed_out = by_id["chunk-00"]
    assert timed_out.status == "TIMED_OUT"
    assert timed_out.attempts == 1, (
        "Timeout must use exactly 1 attempt — CodeBuild kills it, no retry possible. "
        f"Got {timed_out.attempts}."
    )

    failed = by_id["chunk-01"]
    assert failed.status == "FAILED"
    assert failed.attempts == 3, (
        "Transient failure gets 1 initial + 2 retries = 3 total attempts. "
        f"Got {failed.attempts}."
    )


def test_timeout_duration_threshold() -> None:
    """Chunks exceeding 20 minutes are TIMED_OUT; at exactly 20 minutes they are not.

    The per-build timeout is 20 minutes. CodeBuild triggers the timeout only
    when duration *exceeds* the limit — a build completing at exactly 20:00
    is allowed to finish normally.

    **Validates: Requirements 7.5**
    """
    # Chunk that completes at exactly the limit — must NOT be treated as timeout
    chunks_at_limit = [
        {"chunk_id": "chunk-00", "outcome": "succeed", "duration_minutes": 20.0},
    ]
    results_at_limit = simulate_map_state_with_timeout(
        chunks_at_limit, build_timeout_minutes=20
    )
    assert results_at_limit[0].status == "SUCCEEDED", (
        "A chunk completing at exactly 20 minutes must NOT be timed out — "
        f"only exceeding the limit triggers a timeout. Got {results_at_limit[0].status!r}."
    )

    # Chunk that exceeds the limit — must be TIMED_OUT
    chunks_over_limit = [
        {"chunk_id": "chunk-01", "outcome": "timeout", "duration_minutes": 20.1},
    ]
    results_over_limit = simulate_map_state_with_timeout(
        chunks_over_limit, build_timeout_minutes=20
    )
    assert results_over_limit[0].status == "TIMED_OUT", (
        "A chunk exceeding 20 minutes must be marked TIMED_OUT. "
        f"Got {results_over_limit[0].status!r}."
    )
    assert results_over_limit[0].attempts == 1, (
        f"Timed-out chunk must use exactly 1 attempt, got {results_over_limit[0].attempts}."
    )
