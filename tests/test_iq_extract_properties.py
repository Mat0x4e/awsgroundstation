"""Property-based tests for IQExtractor._validate_sample_rate().

Validates: Requirements 1.3

Property 2 — Sample rate validation boundary:
    The method accepts a declared sample rate when
    |declared_rate - 34_312_500| <= 1 Hz, and rejects all other rates.

    Acceptance band:  {34_312_499, 34_312_500, 34_312_501}
    Rejection band:   every other integer in [34_312_490, 34_312_510]
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.iq_extract import IQExtractor

# ---------------------------------------------------------------------------
# Constants (mirrors IQExtractor class attributes for readability in tests)
# ---------------------------------------------------------------------------
EXPECTED_RATE = 34_312_500
TOLERANCE = 1
ACCEPTED = {EXPECTED_RATE - 1, EXPECTED_RATE, EXPECTED_RATE + 1}  # {34_312_499, 34_312_500, 34_312_501}

# ---------------------------------------------------------------------------
# Shared extractor instance (stateless between calls)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def extractor() -> IQExtractor:
    return IQExtractor()


# ---------------------------------------------------------------------------
# Property 2: rates within tolerance are accepted
# ---------------------------------------------------------------------------
@given(rate=st.sampled_from(sorted(ACCEPTED)))
@settings(max_examples=100)
def test_accepted_rates_return_true(rate: int) -> None:
    """Any rate in {34_312_499, 34_312_500, 34_312_501} must be accepted.

    Validates: Requirements 1.3
    """
    extractor = IQExtractor()
    assert extractor._validate_sample_rate(rate) is True, (
        f"Rate {rate} should be accepted (|{rate} - {EXPECTED_RATE}| = "
        f"{abs(rate - EXPECTED_RATE)} <= {TOLERANCE})"
    )


# ---------------------------------------------------------------------------
# Property 2: rates outside tolerance are rejected
# ---------------------------------------------------------------------------
@given(
    rate=st.integers(min_value=EXPECTED_RATE - 10, max_value=EXPECTED_RATE + 10).filter(
        lambda r: r not in ACCEPTED
    )
)
@settings(max_examples=100)
def test_rejected_rates_return_false(rate: int) -> None:
    """Any rate in [34_312_490, 34_312_510] but outside the acceptance band
    must be rejected.

    Validates: Requirements 1.3
    """
    extractor = IQExtractor()
    assert extractor._validate_sample_rate(rate) is False, (
        f"Rate {rate} should be rejected (|{rate} - {EXPECTED_RATE}| = "
        f"{abs(rate - EXPECTED_RATE)} > {TOLERANCE})"
    )


# ---------------------------------------------------------------------------
# Explicit boundary cases (deterministic complement to the property tests)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rate,expected", [
    (34_312_499, True),   # lower boundary — accepted
    (34_312_500, True),   # exact centre — accepted
    (34_312_501, True),   # upper boundary — accepted
    (34_312_498, False),  # one below lower boundary — rejected
    (34_312_502, False),  # one above upper boundary — rejected
    (34_312_490, False),  # far below — rejected
    (34_312_510, False),  # far above — rejected
])
def test_sample_rate_boundary_cases(extractor: IQExtractor, rate: int, expected: bool) -> None:
    """Deterministic boundary checks.

    Validates: Requirements 1.3
    """
    result = extractor._validate_sample_rate(rate)
    assert result is expected, (
        f"_validate_sample_rate({rate}) returned {result}, expected {expected}"
    )
