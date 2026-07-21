"""Unit tests for GeolocationCalculator.

Covers:
    - Req 5.3: CelesTrak fallback on HTTP timeout
    - Req 5.4: TLE age warning emission (degraded flag)
    - Req 5.5: coordinates.json schema compliance

Strategy:
    - requests.get is mocked to avoid actual HTTP calls.
    - sgp4 orbit propagation runs for real (not mocked) — required for
      schema compliance test to exercise the full compute() path.
    - datetime.now is mocked in age-check tests to produce deterministic
      age values relative to the fallback TLE epoch (2026-06-19).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

from scripts.geolocation import (
    GeolocationCalculator,
    CoordinatesResult,
    _DEFAULT_FALLBACK_TLE,
)


# ---------------------------------------------------------------------------
# Shared fixtures and constants
# ---------------------------------------------------------------------------

# The fallback TLE epoch is 26170.05920139 → 2026 day 170.059 ≈ 2026-06-19 01:25 UTC.
# Timestamps 1 minute apart, centred near that epoch, give valid sgp4 propagation.
_TLE_EPOCH_UTC = datetime(2026, 6, 19, 1, 25, 0, tzinfo=timezone.utc)

# 5 timestamps around the TLE epoch — well within sgp4 validity window
_TIMESTAMPS = [
    _TLE_EPOCH_UTC.timestamp() + i * 60.0
    for i in range(5)
]

# Minimal dataset.json dict with those timestamps
_DATASET_JSON = {"timestamps": _TIMESTAMPS}


@pytest.fixture
def calculator() -> GeolocationCalculator:
    return GeolocationCalculator()


# ---------------------------------------------------------------------------
# Scenario 1 — Req 5.3: CelesTrak fallback on HTTP timeout
# ---------------------------------------------------------------------------

class TestCelesTrakFallback:
    """CelesTrak is unreachable — calculator must use the fallback TLE.

    **Validates: Requirements 5.3**
    """

    def test_resolve_tle_returns_fallback_on_timeout(self, calculator):
        """When requests.get raises Timeout on every attempt, _resolve_tle()
        must return the fallback TLE lines and set tle_source = 'fallback'.

        **Validates: Requirements 5.3**
        """
        with patch(
            "scripts.geolocation.requests.get",
            side_effect=requests_lib.exceptions.Timeout("connection timed out"),
        ):
            (line1, line2), source = calculator._resolve_tle(_DEFAULT_FALLBACK_TLE)

        assert source == "fallback", (
            f"Expected tle_source='fallback', got {source!r}"
        )
        assert line1.startswith("1 "), (
            f"Fallback TLE line1 must start with '1 ', got: {line1!r}"
        )
        assert line2.startswith("2 "), (
            f"Fallback TLE line2 must start with '2 ', got: {line2!r}"
        )

    def test_resolve_tle_returns_fallback_on_connect_timeout(self, calculator):
        """requests.exceptions.ConnectTimeout (subclass of Timeout) is also
        caught and triggers fallback.

        **Validates: Requirements 5.3**
        """
        with patch(
            "scripts.geolocation.requests.get",
            side_effect=requests_lib.exceptions.ConnectTimeout("connect timed out"),
        ):
            (line1, line2), source = calculator._resolve_tle(_DEFAULT_FALLBACK_TLE)

        assert source == "fallback"

    def test_resolve_tle_returns_fallback_on_connection_error(self, calculator):
        """Any RequestException (network down, DNS failure, etc.) falls through
        to the fallback.

        **Validates: Requirements 5.3**
        """
        with patch(
            "scripts.geolocation.requests.get",
            side_effect=requests_lib.exceptions.ConnectionError("network unreachable"),
        ):
            _, source = calculator._resolve_tle(_DEFAULT_FALLBACK_TLE)

        assert source == "fallback"

    def test_resolve_tle_retries_three_times_before_fallback(self, calculator):
        """_fetch_tle() must attempt 3 requests before giving up.

        **Validates: Requirements 5.3**
        """
        mock_get = MagicMock(
            side_effect=requests_lib.exceptions.Timeout("timeout")
        )
        with patch("scripts.geolocation.requests.get", mock_get):
            _, source = calculator._resolve_tle(_DEFAULT_FALLBACK_TLE)

        assert mock_get.call_count == 3, (
            f"Expected 3 retry attempts, got {mock_get.call_count}"
        )
        assert source == "fallback"

    def test_fallback_tle_lines_match_default_constant(self, calculator):
        """The fallback lines returned must correspond to the _DEFAULT_FALLBACK_TLE
        constant shipped with the module.

        **Validates: Requirements 5.3**
        """
        with patch(
            "scripts.geolocation.requests.get",
            side_effect=requests_lib.exceptions.Timeout(),
        ):
            (line1, line2), _ = calculator._resolve_tle(_DEFAULT_FALLBACK_TLE)

        expected_lines = _DEFAULT_FALLBACK_TLE.strip().splitlines()
        assert line1 == expected_lines[-2].strip()
        assert line2 == expected_lines[-1].strip()

    def test_resolve_tle_uses_celestrak_when_reachable(self, calculator):
        """When CelesTrak responds with a valid TLE, tle_source = 'celestrak'.

        **Validates: Requirements 5.3 (positive case)**
        """
        fake_tle_text = (
            "NOAA 20\n"
            "1 43013U 17073A   26170.05920139  .00000045  00000+0  38906-4 0  9993\n"
            "2 43013  98.7267 230.8542 0001437  95.2845 264.8485 14.19558374448521\n"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = fake_tle_text

        with patch("scripts.geolocation.requests.get", return_value=mock_resp):
            (line1, line2), source = calculator._resolve_tle(_DEFAULT_FALLBACK_TLE)

        assert source == "celestrak"
        assert line1.startswith("1 ")
        assert line2.startswith("2 ")


# ---------------------------------------------------------------------------
# Scenario 2 — Req 5.4: TLE age warning / degraded flag
# ---------------------------------------------------------------------------

class TestTleAgeWarning:
    """_check_tle_age() returns (age_hours, degraded) based on TLE epoch.

    **Validates: Requirements 5.4**
    """

    def test_degraded_true_when_tle_older_than_7_days(self, calculator):
        """When the TLE epoch is more than 7 days ago, degraded must be True.

        **Validates: Requirements 5.4**
        """
        tle_epoch = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Simulate "now" = 8 days after the epoch
        fake_now = tle_epoch + timedelta(days=8)

        with patch("scripts.geolocation.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _, degraded = calculator._check_tle_age(tle_epoch)

        assert degraded is True, "Expected degraded=True when TLE is 8 days old"

    def test_degraded_false_when_tle_younger_than_7_days(self, calculator):
        """When the TLE epoch is less than 7 days ago, degraded must be False.

        **Validates: Requirements 5.4**
        """
        tle_epoch = datetime(2026, 6, 12, 0, 0, 0, tzinfo=timezone.utc)
        # Simulate "now" = 3 days after the epoch
        fake_now = tle_epoch + timedelta(days=3)

        with patch("scripts.geolocation.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _, degraded = calculator._check_tle_age(tle_epoch)

        assert degraded is False, "Expected degraded=False when TLE is 3 days old"

    def test_degraded_false_at_exactly_7_days(self, calculator):
        """At exactly 7 * 24 = 168 hours, degraded must be False (boundary is
        strictly greater-than, not >=).

        **Validates: Requirements 5.4**
        """
        tle_epoch = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        fake_now = tle_epoch + timedelta(days=7)  # exactly 168 hours

        with patch("scripts.geolocation.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            age_hours, degraded = calculator._check_tle_age(tle_epoch)

        assert age_hours == pytest.approx(168.0, abs=0.01)
        assert degraded is False, "Expected degraded=False at exactly 7 days (boundary)"

    def test_degraded_true_just_over_7_days(self, calculator):
        """At 7 days + 1 second, degraded must be True.

        **Validates: Requirements 5.4**
        """
        tle_epoch = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        fake_now = tle_epoch + timedelta(days=7, seconds=1)

        with patch("scripts.geolocation.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _, degraded = calculator._check_tle_age(tle_epoch)

        assert degraded is True

    def test_age_hours_is_correct(self, calculator):
        """age_hours in the return value must be the correct numeric age.

        **Validates: Requirements 5.4**
        """
        tle_epoch = datetime(2026, 6, 10, 6, 0, 0, tzinfo=timezone.utc)
        fake_now = tle_epoch + timedelta(hours=50)

        with patch("scripts.geolocation.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            age_hours, degraded = calculator._check_tle_age(tle_epoch)

        assert age_hours == pytest.approx(50.0, abs=0.01)
        assert degraded is False  # 50 h < 168 h


# ---------------------------------------------------------------------------
# Scenario 3 — Req 5.5: coordinates.json schema compliance
# ---------------------------------------------------------------------------

class TestSchemaCompliance:
    """compute() with a mocked (fallback) TLE and real sgp4 propagation must
    produce a CoordinatesResult whose to_dict() matches the coordinates.json
    schema exactly.

    **Validates: Requirements 5.5**
    """

    # Patch requests.get to prevent any real HTTP during schema tests
    @pytest.fixture(autouse=True)
    def _mock_celestrak_timeout(self):
        with patch(
            "scripts.geolocation.requests.get",
            side_effect=requests_lib.exceptions.Timeout(),
        ):
            yield

    def _run_compute(self, calculator):
        """Helper: run compute() with the fallback TLE and near-epoch timestamps."""
        return calculator.compute(
            dataset_json=_DATASET_JSON,
            fallback_tle=_DEFAULT_FALLBACK_TLE,
            chunk_id="chunk_001",
            contact_id="contact-test-42",
        )

    def test_to_dict_contains_all_required_keys(self, calculator):
        """to_dict() must contain exactly the 9 keys defined in the schema.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        required_keys = {
            "chunk_id",
            "contact_id",
            "tle_source",
            "tle_epoch",
            "tle_age_hours",
            "degraded",
            "ground_track",
            "bounding_box",
            "swath_bounding_box",
        }
        missing = required_keys - set(d.keys())
        assert not missing, f"Missing keys in to_dict(): {missing}"

    def test_chunk_id_and_contact_id_are_str(self, calculator):
        """chunk_id and contact_id must be strings matching what was passed in.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        assert isinstance(d["chunk_id"], str)
        assert d["chunk_id"] == "chunk_001"
        assert isinstance(d["contact_id"], str)
        assert d["contact_id"] == "contact-test-42"

    def test_tle_source_is_fallback_str(self, calculator):
        """tle_source must be the string 'fallback' when CelesTrak is unavailable.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        assert isinstance(d["tle_source"], str)
        assert d["tle_source"] in {"celestrak", "fallback"}
        assert d["tle_source"] == "fallback"

    def test_tle_epoch_is_iso8601_str(self, calculator):
        """tle_epoch must be an ISO 8601 datetime string parseable by
        datetime.fromisoformat().

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        assert isinstance(d["tle_epoch"], str)
        # Must parse without raising
        parsed = datetime.fromisoformat(d["tle_epoch"])
        assert parsed.tzinfo is not None or "T" in d["tle_epoch"], (
            "tle_epoch must be an ISO 8601 datetime string"
        )

    def test_tle_age_hours_is_float(self, calculator):
        """tle_age_hours must be a numeric float.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        assert isinstance(d["tle_age_hours"], (int, float))
        assert d["tle_age_hours"] >= 0.0

    def test_degraded_is_bool(self, calculator):
        """degraded must be a bool.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        assert isinstance(d["degraded"], bool)

    def test_ground_track_is_list_of_dicts_with_correct_types(self, calculator):
        """ground_track must be a non-empty list of dicts, each with:
          'lat' (float), 'lon' (float), 'timestamp' (str).

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        track = d["ground_track"]
        assert isinstance(track, list), "ground_track must be a list"
        assert len(track) > 0, "ground_track must not be empty"

        for i, point in enumerate(track):
            assert isinstance(point, dict), f"ground_track[{i}] must be a dict"
            assert "lat" in point, f"ground_track[{i}] missing 'lat'"
            assert "lon" in point, f"ground_track[{i}] missing 'lon'"
            assert "timestamp" in point, f"ground_track[{i}] missing 'timestamp'"
            assert isinstance(point["lat"], (int, float)), (
                f"ground_track[{i}]['lat'] must be numeric"
            )
            assert isinstance(point["lon"], (int, float)), (
                f"ground_track[{i}]['lon'] must be numeric"
            )
            assert isinstance(point["timestamp"], str), (
                f"ground_track[{i}]['timestamp'] must be str"
            )
            # Verify lat/lon are in valid ranges
            assert -90.0 <= point["lat"] <= 90.0, (
                f"ground_track[{i}]['lat']={point['lat']} out of [-90, 90]"
            )
            assert -180.0 <= point["lon"] <= 180.0, (
                f"ground_track[{i}]['lon']={point['lon']} out of [-180, 180]"
            )

    def test_bounding_box_has_correct_keys_and_types(self, calculator):
        """bounding_box must be a dict with 'north', 'south', 'east', 'west'
        keys, all floats.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        bbox = d["bounding_box"]
        assert isinstance(bbox, dict), "bounding_box must be a dict"
        for key in ("north", "south", "east", "west"):
            assert key in bbox, f"bounding_box missing key '{key}'"
            assert isinstance(bbox[key], (int, float)), (
                f"bounding_box['{key}'] must be numeric, got {type(bbox[key])}"
            )

        # Basic geometry sanity
        assert bbox["north"] >= bbox["south"], "bounding_box north must be >= south"

    def test_swath_bounding_box_has_correct_keys_and_types(self, calculator):
        """swath_bounding_box must be a dict with 'north', 'south', 'east', 'west'
        keys, all floats.

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        swath = d["swath_bounding_box"]
        assert isinstance(swath, dict), "swath_bounding_box must be a dict"
        for key in ("north", "south", "east", "west"):
            assert key in swath, f"swath_bounding_box missing key '{key}'"
            assert isinstance(swath[key], (int, float)), (
                f"swath_bounding_box['{key}'] must be numeric"
            )

        assert swath["north"] >= swath["south"], (
            "swath_bounding_box north must be >= south"
        )

    def test_swath_bounding_box_is_wider_than_bounding_box(self, calculator):
        """The VIIRS swath box must be at least as wide as the nadir bounding box
        (swath extends cross-track ±56°).

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        bbox = d["bounding_box"]
        swath = d["swath_bounding_box"]

        assert swath["north"] >= bbox["north"], (
            "swath north must be >= nadir north"
        )
        assert swath["south"] <= bbox["south"], (
            "swath south must be <= nadir south"
        )

    def test_ground_track_length_matches_timestamp_count(self, calculator):
        """ground_track must have one point per valid timestamp (5 in _DATASET_JSON).

        **Validates: Requirements 5.5**
        """
        result = self._run_compute(calculator)
        d = result.to_dict()

        assert len(d["ground_track"]) == len(_TIMESTAMPS), (
            f"Expected {len(_TIMESTAMPS)} track points, got {len(d['ground_track'])}"
        )

    def test_dataset_json_nested_timestamps_also_work(self, calculator):
        """compute() must also accept timestamps nested under images[0].timestamps.

        **Validates: Requirements 5.5**
        """
        nested_dataset = {
            "images": [{"timestamps": _TIMESTAMPS, "name": "VIIRS_M01"}]
        }
        result = calculator.compute(
            dataset_json=nested_dataset,
            fallback_tle=_DEFAULT_FALLBACK_TLE,
            chunk_id="nested_chunk",
            contact_id="",
        )
        d = result.to_dict()
        assert len(d["ground_track"]) == len(_TIMESTAMPS)
        assert isinstance(d["chunk_id"], str)

    def test_compute_raises_on_empty_timestamps(self, calculator):
        """compute() must raise ValueError when dataset.json has no timestamps.

        **Validates: Requirements 5.5**
        """
        with pytest.raises(ValueError, match="No timestamps"):
            calculator.compute(
                dataset_json={"timestamps": []},
                fallback_tle=_DEFAULT_FALLBACK_TLE,
                chunk_id="empty_chunk",
                contact_id="",
            )
