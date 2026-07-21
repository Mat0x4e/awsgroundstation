"""Unit tests for scripts/publish_metrics.py."""
import json
import sys
import tempfile
import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# scripts/ is on sys.path via conftest.py
from scripts.publish_metrics import build_metric_data, publish_metrics, load_manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FULL_MANIFEST = {
    "contact_id": "ct-abc123",
    "satellite": "NOAA-20",
    "total_duration_s": 300.5,
    "successful_chunks": 42,
    "failed_chunks": ["chunk_7", "chunk_9"],
    "sdr_files": ["file_a.h5", "file_b.h5", "file_c.h5"],
    "metrics": {
        "extraction_avg_s": 1.2,
        "satdump_avg_s": 45.0,
        "rtstps_avg_s": 12.3,
        "cspp_avg_s": 60.1,
    },
}

_FAKE_CLIENT_ERROR = ClientError(
    {"Error": {"Code": "InternalFailure", "Message": "simulated error"}},
    "PutMetricData",
)


def _metric_by_name(entries: list, name: str) -> dict:
    matches = [e for e in entries if e["MetricName"] == name]
    assert len(matches) == 1, f"Expected 1 entry for {name!r}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. build_metric_data — full manifest produces 8 entries
# ---------------------------------------------------------------------------

class TestBuildMetricDataFull:
    def test_returns_eight_entries(self):
        entries = build_metric_data(FULL_MANIFEST)
        assert len(entries) == 8

    def test_contact_processing_duration(self):
        entries = build_metric_data(FULL_MANIFEST)
        m = _metric_by_name(entries, "ContactProcessingDuration")
        assert m["Value"] == 300.5
        assert m["Unit"] == "Seconds"

    def test_chunks_processed_success(self):
        entries = build_metric_data(FULL_MANIFEST)
        m = _metric_by_name(entries, "ChunksProcessedSuccess")
        assert m["Value"] == 42.0
        assert m["Unit"] == "Count"

    def test_chunks_processed_failed_uses_len(self):
        entries = build_metric_data(FULL_MANIFEST)
        m = _metric_by_name(entries, "ChunksProcessedFailed")
        assert m["Value"] == 2.0  # len(["chunk_7", "chunk_9"])
        assert m["Unit"] == "Count"

    def test_sdr_files_produced_uses_len(self):
        entries = build_metric_data(FULL_MANIFEST)
        m = _metric_by_name(entries, "SDRFilesProduced")
        assert m["Value"] == 3.0
        assert m["Unit"] == "Count"

    def test_per_stage_durations_present(self):
        entries = build_metric_data(FULL_MANIFEST)
        names = {e["MetricName"] for e in entries}
        assert {"ExtractionAvgDuration", "SatDumpAvgDuration", "RTSTPSAvgDuration", "CSPPAvgDuration"} <= names

    def test_dimensions_include_contact_id_and_satellite(self):
        entries = build_metric_data(FULL_MANIFEST)
        for entry in entries:
            dim_names = {d["Name"] for d in entry["Dimensions"]}
            assert "ContactId" in dim_names
            assert "Satellite" in dim_names

    def test_dimensions_values(self):
        entries = build_metric_data(FULL_MANIFEST)
        m = _metric_by_name(entries, "ContactProcessingDuration")
        dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        assert dims["ContactId"] == "ct-abc123"
        assert dims["Satellite"] == "NOAA-20"

    def test_per_stage_duration_values(self):
        entries = build_metric_data(FULL_MANIFEST)
        assert _metric_by_name(entries, "ExtractionAvgDuration")["Value"] == 1.2
        assert _metric_by_name(entries, "SatDumpAvgDuration")["Value"] == 45.0
        assert _metric_by_name(entries, "RTSTPSAvgDuration")["Value"] == 12.3
        assert _metric_by_name(entries, "CSPPAvgDuration")["Value"] == 60.1


# ---------------------------------------------------------------------------
# 2. build_metric_data — missing `metrics` key → 4 core metrics only
# ---------------------------------------------------------------------------

class TestBuildMetricDataMissingMetrics:
    def setup_method(self):
        self.manifest = {k: v for k, v in FULL_MANIFEST.items() if k != "metrics"}

    def test_returns_four_entries(self):
        entries = build_metric_data(self.manifest)
        assert len(entries) == 4

    def test_core_metrics_still_present(self):
        entries = build_metric_data(self.manifest)
        names = {e["MetricName"] for e in entries}
        assert names == {"ContactProcessingDuration", "ChunksProcessedSuccess",
                         "ChunksProcessedFailed", "SDRFilesProduced"}

    def test_per_stage_metrics_absent(self):
        entries = build_metric_data(self.manifest)
        names = {e["MetricName"] for e in entries}
        assert "ExtractionAvgDuration" not in names

    def test_warning_logged(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="scripts.publish_metrics"):
            build_metric_data(self.manifest)
        assert any("metrics" in msg.lower() for msg in caplog.messages)


# ---------------------------------------------------------------------------
# 3. build_metric_data — missing `failed_chunks` → ChunksProcessedFailed skipped
# ---------------------------------------------------------------------------

class TestBuildMetricDataMissingFailedChunks:
    def setup_method(self):
        self.manifest = {k: v for k, v in FULL_MANIFEST.items() if k != "failed_chunks"}

    def test_chunks_failed_absent(self):
        entries = build_metric_data(self.manifest)
        names = [e["MetricName"] for e in entries]
        assert "ChunksProcessedFailed" not in names

    def test_other_core_metrics_present(self):
        entries = build_metric_data(self.manifest)
        names = {e["MetricName"] for e in entries}
        assert "ContactProcessingDuration" in names
        assert "ChunksProcessedSuccess" in names

    def test_warning_logged(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="scripts.publish_metrics"):
            build_metric_data(self.manifest)
        assert any("failed_chunks" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# 4. publish_metrics — ClientError returns False, does not raise
# ---------------------------------------------------------------------------

class TestPublishMetricsClientError:
    def test_returns_false_on_client_error(self):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = _FAKE_CLIENT_ERROR

        with patch("scripts.publish_metrics.boto3.client", return_value=mock_cw):
            result = publish_metrics([{"MetricName": "Foo", "Value": 1.0, "Unit": "Count", "Dimensions": []}])

        assert result is False

    def test_does_not_raise_on_client_error(self):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = _FAKE_CLIENT_ERROR

        with patch("scripts.publish_metrics.boto3.client", return_value=mock_cw):
            try:
                publish_metrics([{"MetricName": "Foo", "Value": 1.0, "Unit": "Count", "Dimensions": []}])
            except ClientError:
                pytest.fail("publish_metrics raised ClientError — it should return False instead")

    def test_returns_true_on_success(self):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.return_value = {}

        with patch("scripts.publish_metrics.boto3.client", return_value=mock_cw):
            result = publish_metrics([{"MetricName": "Foo", "Value": 1.0, "Unit": "Count", "Dimensions": []}])

        assert result is True


# ---------------------------------------------------------------------------
# 5. load_manifest — non-existent file calls sys.exit(1)
# ---------------------------------------------------------------------------

class TestLoadManifestNonExistent:
    def test_exits_on_missing_file(self):
        with pytest.raises(SystemExit) as exc_info:
            load_manifest("/does/not/exist/manifest.json")
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 6. load_manifest — invalid JSON calls sys.exit(1)
# ---------------------------------------------------------------------------

class TestLoadManifestInvalidJson:
    def test_exits_on_invalid_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json}", encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            load_manifest(str(bad_file))
        assert exc_info.value.code == 1

    def test_valid_json_returns_dict(self, tmp_path):
        good_file = tmp_path / "manifest.json"
        good_file.write_text(json.dumps(FULL_MANIFEST), encoding="utf-8")

        result = load_manifest(str(good_file))
        assert result["contact_id"] == "ct-abc123"
        assert result["satellite"] == "NOAA-20"
