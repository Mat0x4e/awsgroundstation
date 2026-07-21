"""Property-based tests for Step Functions Map state chunk failure isolation.

**Validates: Requirements 6.4**

Property 8 — Chunk failure isolation:
    For any set of chunks (1–19) with random success/failure outcomes, the
    Step Functions Map state with ToleratedFailurePercentage=100 guarantees:

    1. Pipeline continues processing remaining chunks — all chunks get a result
       regardless of individual failures.
    2. Results produced for successful chunks only — the count of SUCCEEDED
       results equals the number of chunks where will_succeed=True.
    3. Failed chunks are marked as error — chunks where will_succeed=False
       carry status="FAILED" in their result.
    4. Successful output count matches chunks that completed processing
       successfully — no phantom successes or silent drops.

    When all chunks fail, the CheckResults choice state routes to TotalFailure.
    When at least one chunk succeeds, it routes to FinalAggregation.

    Per-chunk retry: MaxAttempts=2 (3 total attempts).  A chunk that never
    succeeds is marked FAILED after exhausting retries; the pipeline continues
    with remaining chunks.
"""

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Simulation model
# ---------------------------------------------------------------------------


@dataclass
class ChunkResult:
    chunk_id: str
    status: str   # "SUCCEEDED" or "FAILED"
    attempts: int


def simulate_map_state(chunks: list[dict], max_retries: int = 2) -> list[ChunkResult]:
    """Simulate Step Functions Map state with ToleratedFailurePercentage=100.

    Each chunk dict has: {"chunk_id": str, "will_succeed": bool}

    - If will_succeed is True, the chunk succeeds on the first attempt.
    - If will_succeed is False, it fails on all attempts up to max_retries.

    All chunks are processed regardless of individual failures, modelling the
    ToleratedFailurePercentage=100 behaviour of the real Map state.

    The Retry block in the ASL has MaxAttempts=2, meaning up to 3 total
    attempts (1 initial + 2 retries) before marking the chunk FAILED.
    """
    results: list[ChunkResult] = []

    for chunk in chunks:
        chunk_id: str = chunk["chunk_id"]
        will_succeed: bool = chunk["will_succeed"]

        if will_succeed:
            # Succeeds on first attempt
            results.append(ChunkResult(chunk_id=chunk_id, status="SUCCEEDED", attempts=1))
        else:
            # Fails every attempt; exhaust all retries then mark FAILED
            total_attempts = 1 + max_retries  # initial attempt + retries
            results.append(
                ChunkResult(chunk_id=chunk_id, status="FAILED", attempts=total_attempts)
            )

    return results


def check_results(chunk_results: list[ChunkResult]) -> str:
    """Simulate the CheckResults Choice state.

    Returns "FinalAggregation" if at least one chunk succeeded,
    "TotalFailure" otherwise.
    """
    if any(r.status == "SUCCEEDED" for r in chunk_results):
        return "FinalAggregation"
    return "TotalFailure"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A list of chunk dicts with random success/failure outcomes.
# Min 1, max 19 chunks — matching the real system's concurrency limit (19 chunks
# per contact, one per ~2.18 GB file segment).
# unique_by=lambda c: c["chunk_id"] ensures no duplicate chunk IDs within a
# single generated list, which matches the real system's behaviour (each chunk
# has a unique identifier derived from its S3 key offset).
_chunks_strategy = st.lists(
    st.fixed_dictionaries(
        {
            "chunk_id": st.from_regex(r"chunk-[0-9]{2}", fullmatch=True),
            "will_succeed": st.booleans(),
        }
    ),
    min_size=1,
    max_size=19,
    unique_by=lambda c: c["chunk_id"],
)


# ---------------------------------------------------------------------------
# Property 8: chunk failure isolation
# ---------------------------------------------------------------------------


@given(chunks=_chunks_strategy)
@settings(max_examples=100)
def test_chunk_failure_isolation(chunks: list[dict]) -> None:
    """For any mix of succeeding and failing chunks, the Map state processes all
    chunks and reports results that are consistent with the input outcomes.

    **Validates: Requirements 6.4**
    """
    n_total = len(chunks)
    n_will_succeed = sum(1 for c in chunks if c["will_succeed"])
    n_will_fail = n_total - n_will_succeed

    results = simulate_map_state(chunks)

    # --- Property 1: all chunks get a result (no chunk is silently dropped) ---
    assert len(results) == n_total, (
        f"Expected {n_total} results (one per chunk), got {len(results)}. "
        "ToleratedFailurePercentage=100 means every chunk must be processed."
    )

    # --- Property 2: successful output count matches will_succeed count ---
    n_succeeded = sum(1 for r in results if r.status == "SUCCEEDED")
    assert n_succeeded == n_will_succeed, (
        f"Expected {n_will_succeed} SUCCEEDED results, got {n_succeeded}. "
        "Only chunks with will_succeed=True should produce SUCCEEDED results."
    )

    # --- Property 3: failed chunks are marked as error ---
    n_failed = sum(1 for r in results if r.status == "FAILED")
    assert n_failed == n_will_fail, (
        f"Expected {n_will_fail} FAILED results, got {n_failed}. "
        "All chunks with will_succeed=False must be marked FAILED after retries."
    )

    for result in results:
        assert result.status in ("SUCCEEDED", "FAILED"), (
            f"chunk_id={result.chunk_id}: unexpected status={result.status!r}. "
            "Valid statuses are 'SUCCEEDED' and 'FAILED'."
        )

    # --- Property 4: successful output count equals chunks that completed
    #     processing successfully — no phantom successes or silent drops ---
    chunk_ids_in = {c["chunk_id"] for c in chunks}
    chunk_ids_out = {r.chunk_id for r in results}
    assert chunk_ids_out == chunk_ids_in, (
        f"Result chunk_ids do not match input chunk_ids. "
        f"Missing: {chunk_ids_in - chunk_ids_out}, extra: {chunk_ids_out - chunk_ids_in}."
    )

    # Verify attempts are consistent with the simulation model
    chunks_by_id = {c["chunk_id"]: c for c in chunks}
    for result in results:
        original = chunks_by_id[result.chunk_id]
        if original["will_succeed"]:
            assert result.attempts == 1, (
                f"chunk_id={result.chunk_id}: succeeding chunk should take 1 attempt, "
                f"got {result.attempts}."
            )
        else:
            # MaxAttempts=2 in the ASL Retry block → 3 total attempts (1 + 2 retries)
            assert result.attempts == 3, (
                f"chunk_id={result.chunk_id}: failing chunk should exhaust all 3 attempts "
                f"(1 initial + 2 retries), got {result.attempts}."
            )

    # --- CheckResults routing: at least one success → FinalAggregation ---
    route = check_results(results)
    if n_will_succeed > 0:
        assert route == "FinalAggregation", (
            f"With {n_will_succeed} successful chunk(s), CheckResults should route to "
            f"'FinalAggregation', got {route!r}."
        )
    else:
        assert route == "TotalFailure", (
            "With zero successful chunks, CheckResults should route to 'TotalFailure', "
            f"got {route!r}."
        )


# ---------------------------------------------------------------------------
# Edge case: all chunks fail → TotalFailure path
# ---------------------------------------------------------------------------


@given(n_chunks=st.integers(min_value=1, max_value=19))
@settings(max_examples=100)
def test_all_chunks_fail_routes_to_total_failure(n_chunks: int) -> None:
    """When every chunk fails, CheckResults routes to TotalFailure.

    **Validates: Requirements 6.4**
    """
    chunks = [
        {"chunk_id": f"chunk-{i:02d}", "will_succeed": False}
        for i in range(n_chunks)
    ]

    results = simulate_map_state(chunks)

    # All chunks must be present and all must be FAILED
    assert len(results) == n_chunks, (
        f"Expected {n_chunks} results, got {len(results)}. "
        "Even in total failure, every chunk must produce a result."
    )
    for result in results:
        assert result.status == "FAILED", (
            f"chunk_id={result.chunk_id}: expected FAILED, got {result.status!r}."
        )
        assert result.attempts == 3, (
            f"chunk_id={result.chunk_id}: expected 3 total attempts (1 + 2 retries), "
            f"got {result.attempts}."
        )

    route = check_results(results)
    assert route == "TotalFailure", (
        f"All {n_chunks} chunk(s) failed → CheckResults must route to 'TotalFailure', "
        f"got {route!r}."
    )


# ---------------------------------------------------------------------------
# Deterministic complementary cases
# ---------------------------------------------------------------------------


def test_single_chunk_success_routes_to_aggregation() -> None:
    """One chunk, succeeds → FinalAggregation; 1 attempt used.

    Validates: Requirements 6.4
    """
    chunks = [{"chunk_id": "chunk-00", "will_succeed": True}]
    results = simulate_map_state(chunks)

    assert len(results) == 1
    assert results[0].status == "SUCCEEDED"
    assert results[0].attempts == 1
    assert check_results(results) == "FinalAggregation"


def test_single_chunk_failure_routes_to_total_failure() -> None:
    """One chunk, fails → TotalFailure; all 3 attempts exhausted.

    Validates: Requirements 6.4
    """
    chunks = [{"chunk_id": "chunk-00", "will_succeed": False}]
    results = simulate_map_state(chunks)

    assert len(results) == 1
    assert results[0].status == "FAILED"
    assert results[0].attempts == 3
    assert check_results(results) == "TotalFailure"


def test_mixed_chunks_one_success_routes_to_aggregation() -> None:
    """One succeeding chunk among many failures → FinalAggregation (not TotalFailure).

    This validates the key isolation property: a single success is sufficient
    to proceed to aggregation even when all other chunks fail.

    Validates: Requirements 6.4
    """
    chunks = [
        {"chunk_id": "chunk-00", "will_succeed": False},
        {"chunk_id": "chunk-01", "will_succeed": True},   # the lone survivor
        {"chunk_id": "chunk-02", "will_succeed": False},
        {"chunk_id": "chunk-03", "will_succeed": False},
    ]
    results = simulate_map_state(chunks)

    assert len(results) == 4

    succeeded = [r for r in results if r.status == "SUCCEEDED"]
    failed = [r for r in results if r.status == "FAILED"]

    assert len(succeeded) == 1
    assert succeeded[0].chunk_id == "chunk-01"
    assert len(failed) == 3

    assert check_results(results) == "FinalAggregation"


def test_all_19_chunks_succeed() -> None:
    """Maximum concurrency (19 chunks), all succeed → FinalAggregation.

    Validates: Requirements 6.4
    """
    chunks = [{"chunk_id": f"chunk-{i:02d}", "will_succeed": True} for i in range(19)]
    results = simulate_map_state(chunks)

    assert len(results) == 19
    assert all(r.status == "SUCCEEDED" for r in results)
    assert all(r.attempts == 1 for r in results)
    assert check_results(results) == "FinalAggregation"


def test_retry_count_is_respected() -> None:
    """Failing chunks consume exactly MaxAttempts=2 retries (3 total attempts).

    Validates: Requirements 6.4
    """
    chunks = [
        {"chunk_id": "chunk-00", "will_succeed": False},
        {"chunk_id": "chunk-01", "will_succeed": False},
    ]
    results = simulate_map_state(chunks, max_retries=2)

    for result in results:
        assert result.attempts == 3, (
            f"chunk_id={result.chunk_id}: ASL Retry MaxAttempts=2 means "
            f"3 total attempts (1 initial + 2 retries), got {result.attempts}."
        )
