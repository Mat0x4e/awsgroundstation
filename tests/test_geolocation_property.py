"""Property-based tests for TLE degradation classification in GeolocationCalculator.

**Validates: Requirements 5.5**

Property 7 — TLE degradation classification:
    For any TLE epoch age from 0 to 30 days:
    - is_degraded is True if and only if age_hours > 168.0 (7 days * 24 hours)
    - is_degraded is False for age_hours <= 168.0 (boundary is NOT degraded)
    - age_hours returned is numerically consistent with the input age
    - The exact boundary (168.0 h) is NOT degraded; 168.0 + epsilon IS degraded
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.geolocation import GeolocationCalculator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_THRESHOLD_HOURS = 7 * 24  # 168.0 hours — TLE_MAX_AGE_DAYS * 24

# A fixed "now" used as the frozen clock in every test.
_FROZEN_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _epoch_from_age_hours(age_hours: float) -> datetime:
    """Return a TLE epoch datetime that is exactly *age_hours* old relative to _FROZEN_NOW."""
    return _FROZEN_NOW - timedelta(hours=age_hours)


def _call_check_tle_age(age_hours: float):
    """Call _check_tle_age with a frozen clock and a TLE epoch *age_hours* old.

    Patches scripts.geolocation.datetime so that datetime.now() returns
    _FROZEN_NOW, making the test fully deterministic regardless of wall-clock time.
    """
    tle_epoch = _epoch_from_age_hours(age_hours)

    calc = GeolocationCalculator()

    # Build a mock datetime class that:
    # - still allows datetime(...) construction (for _parse_tle_epoch and the rest
    #   of the module which uses datetime(...))
    # - overrides datetime.now(tz=...) to return _FROZEN_NOW
    mock_datetime = MagicMock(wraps=datetime)
    mock_datetime.now.return_value = _FROZEN_NOW

    with patch("scripts.geolocation.datetime", mock_datetime):
        return calc._check_tle_age(tle_epoch)


# ---------------------------------------------------------------------------
# Property 7 — degradation flag is determined solely by the 168-hour threshold
# ---------------------------------------------------------------------------


@given(age_hours=st.floats(min_value=0.0, max_value=30 * 24, allow_nan=False))
@settings(max_examples=200)
def test_degradation_flag_matches_threshold(age_hours: float) -> None:
    """is_degraded is True iff age_hours > 168.0 (strictly greater than).

    **Validates: Requirements 5.5**
    """
    returned_hours, is_degraded = _call_check_tle_age(age_hours)

    if age_hours > _THRESHOLD_HOURS:
        assert is_degraded is True, (
            f"Expected degraded=True for age {age_hours:.6f} h (> {_THRESHOLD_HOURS} h), "
            f"got degraded=False"
        )
    else:
        assert is_degraded is False, (
            f"Expected degraded=False for age {age_hours:.6f} h (<= {_THRESHOLD_HOURS} h), "
            f"got degraded=True"
        )


@given(age_hours=st.floats(min_value=0.0, max_value=30 * 24, allow_nan=False))
@settings(max_examples=200)
def test_returned_age_hours_matches_input(age_hours: float) -> None:
    """The returned age_hours value is numerically consistent with the input age.

    **Validates: Requirements 5.5**
    """
    returned_hours, _ = _call_check_tle_age(age_hours)

    # Allow a small floating-point tolerance (timedelta round-trip)
    assert abs(returned_hours - age_hours) < 1e-6, (
        f"Returned age_hours ({returned_hours:.9f}) differs from input ({age_hours:.9f}) "
        f"by more than 1e-6 h"
    )


# ---------------------------------------------------------------------------
# Deterministic boundary tests
# ---------------------------------------------------------------------------


def test_exact_boundary_168h_not_degraded() -> None:
    """168.0 hours exactly is NOT degraded (condition is strictly >).

    **Validates: Requirements 5.5**
    """
    returned_hours, is_degraded = _call_check_tle_age(168.0)
    assert is_degraded is False, (
        f"168.0 h is the threshold — should NOT be degraded (condition is >), "
        f"got degraded=True"
    )
    assert abs(returned_hours - 168.0) < 1e-6


def test_just_above_boundary_168_0001h_is_degraded() -> None:
    """168.0001 hours IS degraded (one step above the boundary).

    **Validates: Requirements 5.5**
    """
    age = 168.0 + 1e-4  # 168.0001 hours
    returned_hours, is_degraded = _call_check_tle_age(age)
    assert is_degraded is True, (
        f"168.0001 h should be degraded (age > 168 h), got degraded=False"
    )
    assert abs(returned_hours - age) < 1e-6


def test_zero_age_not_degraded() -> None:
    """A brand-new TLE (age = 0) is never degraded.

    **Validates: Requirements 5.5**
    """
    returned_hours, is_degraded = _call_check_tle_age(0.0)
    assert is_degraded is False
    assert abs(returned_hours) < 1e-6


def test_one_second_below_boundary_not_degraded() -> None:
    """167.999722... hours (168 h minus 1 second) is NOT degraded.

    **Validates: Requirements 5.5**
    """
    age = 168.0 - (1 / 3600.0)  # one second below the threshold
    _, is_degraded = _call_check_tle_age(age)
    assert is_degraded is False


def test_one_second_above_boundary_is_degraded() -> None:
    """168.000277... hours (168 h plus 1 second) IS degraded.

    **Validates: Requirements 5.5**
    """
    age = 168.0 + (1 / 3600.0)  # one second above the threshold
    _, is_degraded = _call_check_tle_age(age)
    assert is_degraded is True


def test_30_day_old_tle_is_degraded() -> None:
    """A 30-day-old TLE is well above the 7-day threshold — always degraded.

    **Validates: Requirements 5.5**
    """
    _, is_degraded = _call_check_tle_age(30 * 24.0)
    assert is_degraded is True


# ===========================================================================
# Property 6 — Ground track validity and bounding box containment
# ===========================================================================
#
# **Validates: Requirements 5.1, 5.2**
#
# For any valid TLE (epoch < 7 days) and timestamp range:
#   1. All points have latitude in [-90, 90] and longitude in [-180, 180]
#   2. The computed bounding box contains all ground track points
#   3. The swath bounding box extends the nadir bounding box by the VIIRS
#      cross-track angle (swath north >= nadir north, swath south <= nadir south)
#
# Uses the hardcoded _DEFAULT_FALLBACK_TLE (NOAA-20, epoch ≈ 2026-06-19 01:25 UTC)
# so no CelesTrak mock is needed — only the core propagation + bbox logic is tested.
# ===========================================================================

from scripts.geolocation import _DEFAULT_FALLBACK_TLE

# ---------------------------------------------------------------------------
# Constants for Property 6
# ---------------------------------------------------------------------------

# TLE epoch: day 170.059 of 2026 ≈ 2026-06-19 01:25 UTC
_P6_TLE_EPOCH_UTC = datetime(2026, 6, 19, 1, 25, 0, tzinfo=timezone.utc)
_P6_TLE_EPOCH_TS = _P6_TLE_EPOCH_UTC.timestamp()

# Start anywhere within ±3 days of the TLE epoch (well inside the 7-day validity window)
_P6_START_OFFSET_MIN = -3 * 24 * 3600
_P6_START_OFFSET_MAX =  3 * 24 * 3600

# Duration between 30 seconds and 5 minutes
_P6_DURATION_MIN_S = 30
_P6_DURATION_MAX_S = 5 * 60

# TLE lines (parsed once)
_P6_TLE_LINES = tuple(_DEFAULT_FALLBACK_TLE.strip().splitlines())  # (line1, line2)

# Shared calculator instance (stateless)
_p6_calc = GeolocationCalculator()


# ---------------------------------------------------------------------------
# Strategy: generates a list of evenly-spaced timestamps within a short window
# ---------------------------------------------------------------------------

@st.composite
def _p6_timestamp_ranges(draw):
    """Return a list of 10–100 evenly-spaced Unix timestamps near the TLE epoch."""
    start_offset = draw(st.floats(
        min_value=_P6_START_OFFSET_MIN,
        max_value=_P6_START_OFFSET_MAX,
        allow_nan=False,
        allow_infinity=False,
    ))
    duration = draw(st.floats(
        min_value=_P6_DURATION_MIN_S,
        max_value=_P6_DURATION_MAX_S,
        allow_nan=False,
        allow_infinity=False,
    ))
    n_points = draw(st.integers(min_value=10, max_value=100))

    start_ts = _P6_TLE_EPOCH_TS + start_offset
    step = duration / (n_points - 1)
    return [start_ts + i * step for i in range(n_points)]


# ---------------------------------------------------------------------------
# Property 6a — All ground track points have valid geodetic coordinates
# ---------------------------------------------------------------------------

@given(timestamps=_p6_timestamp_ranges())
@settings(max_examples=100)
def test_ground_track_points_have_valid_lat_lon(timestamps):
    """All propagated ground track points must satisfy WGS-84 range constraints.

    **Validates: Requirements 5.1, 5.2**
    """
    track = _p6_calc._propagate_orbit(_P6_TLE_LINES, timestamps)

    # A valid TLE must produce at least one propagated point
    assert len(track) > 0, "Expected at least one propagated point from a valid TLE"

    for point in track:
        lat = point["lat"]
        lon = point["lon"]
        assert -90.0 <= lat <= 90.0, (
            f"Latitude {lat} out of geodetic bounds [-90, 90]"
        )
        assert -180.0 <= lon <= 180.0, (
            f"Longitude {lon} out of geodetic bounds [-180, 180]"
        )


# ---------------------------------------------------------------------------
# Property 6b — Nadir bounding box contains all ground track points
# ---------------------------------------------------------------------------

@given(timestamps=_p6_timestamp_ranges())
@settings(max_examples=100)
def test_bounding_box_contains_all_track_points(timestamps):
    """The nadir bounding box must enclose every ground track point.

    For short passes (30 s – 5 min), the simple min/max bbox must contain
    all points. For the rare antimeridian-crossing case (west > east), the
    test verifies the weaker disjunction: lon >= west OR lon <= east.

    **Validates: Requirements 5.1, 5.2**
    """
    track = _p6_calc._propagate_orbit(_P6_TLE_LINES, timestamps)
    assert len(track) > 0

    bbox = _p6_calc._compute_bounding_box(track)

    # Required keys
    assert {"north", "south", "east", "west"} == set(bbox.keys())

    # Latitude containment: straightforward (no antimeridian equivalent for lat)
    for point in track:
        lat = point["lat"]
        assert bbox["south"] <= lat <= bbox["north"], (
            f"Point lat={lat} not contained in nadir bbox "
            f"south={bbox['south']}, north={bbox['north']}"
        )

    # Longitude containment
    if bbox["west"] <= bbox["east"]:
        # Normal case: simple range check
        for point in track:
            lon = point["lon"]
            assert bbox["west"] <= lon <= bbox["east"], (
                f"Point lon={lon} not contained in bbox "
                f"west={bbox['west']}, east={bbox['east']}"
            )
    else:
        # Antimeridian crossing: point is in swath if lon >= west OR lon <= east
        for point in track:
            lon = point["lon"]
            assert lon >= bbox["west"] or lon <= bbox["east"], (
                f"Point lon={lon} not contained in antimeridian bbox "
                f"west={bbox['west']}, east={bbox['east']}"
            )


# ---------------------------------------------------------------------------
# Property 6c — Swath bounding box extends beyond the nadir bounding box
# ---------------------------------------------------------------------------

@given(timestamps=_p6_timestamp_ranges())
@settings(max_examples=100)
def test_swath_extends_nadir_bounding_box(timestamps):
    """The VIIRS swath bbox latitude extent must be at least as wide as the nadir bbox.

    swath["north"] >= nadir["north"] (swath reaches further north or equal)
    swath["south"] <= nadir["south"] (swath reaches further south or equal)

    Longitude extension may wrap via modulo normalisation so only latitude and
    WGS-84 validity are asserted on the swath. The computation must not raise.

    **Validates: Requirements 5.1, 5.2**
    """
    track = _p6_calc._propagate_orbit(_P6_TLE_LINES, timestamps)
    assert len(track) > 0

    bbox = _p6_calc._compute_bounding_box(track)
    swath = _p6_calc._extend_swath(bbox, track)

    # Required keys
    assert {"north", "south", "east", "west"} == set(swath.keys())

    # Swath latitude must enclose (or equal) the nadir bbox
    assert swath["north"] >= bbox["north"], (
        f"Swath north={swath['north']} < nadir north={bbox['north']} — swath shrank"
    )
    assert swath["south"] <= bbox["south"], (
        f"Swath south={swath['south']} > nadir south={bbox['south']} — swath shrank"
    )

    # Swath latitude values must remain within WGS-84 bounds
    assert -90.0 <= swath["south"] <= 90.0, f"Swath south={swath['south']} out of bounds"
    assert -90.0 <= swath["north"] <= 90.0, f"Swath north={swath['north']} out of bounds"
    assert swath["south"] <= swath["north"], (
        f"Swath south={swath['south']} > swath north={swath['north']}"
    )

    # Swath longitude values must be within WGS-84 bounds (after normalisation)
    assert -180.0 <= swath["west"] <= 180.0, f"Swath west={swath['west']} out of bounds"
    assert -180.0 <= swath["east"] <= 180.0, f"Swath east={swath['east']} out of bounds"
