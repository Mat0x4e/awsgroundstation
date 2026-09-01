"""Tests for the VIIRS visualization orchestrator Lambda.

Focus: the acquisition time handed to the SatDump path. Without it,
visualize_satdump.py falls back to midnight on the contact date and
propagates the TLE to the wrong point in the orbit -- contact #5 rendered
central Mediterranean imagery labelled with a Caspian bounding box.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HANDLER = Path(__file__).resolve().parent.parent / "lambdas" / "viirs_visualizer" / "handler.py"
_spec = importlib.util.spec_from_file_location("viirs_visualizer_handler", _HANDLER)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


PREFIX = "contacts/2026/08/31/ba2c5446"

# 2026-08-31T11:59:57.684743Z -- SatDump's own timestamp for chunk_0 of
# contact #5, eight seconds after AOS.
CHUNK0_TS = 1788177597.684743
CHUNK3_TS = CHUNK0_TS + 90


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 signature
        return {"Body": io.BytesIO(self._objects[Key])}


class FakeCodeBuild:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        return {"build": {"id": "project:build-id"}}


def _orchestrator(objects=None, codebuild=None):
    return handler.VisualizationOrchestrator(
        s3_client=FakeS3(objects or {}),
        codebuild_client=codebuild or FakeCodeBuild(),
        input_bucket="bucket",
        codebuild_project="project",
        enable_geotiff="true",
    )


def _dataset(ts: float) -> bytes:
    return json.dumps({"products": ["VIIRS"], "satellite": "NOAA 20 (JPSS-1)", "timestamp": ts}).encode()


def test_contact_time_read_from_chunk_dataset():
    key = f"{PREFIX}/satdump/chunk_0/dataset.json"
    orch = _orchestrator({key: _dataset(CHUNK0_TS)})

    # A lone chunk gives a time but no cadence to measure.
    assert orch._lookup_acquisition("bucket", [key]) == ("11:59:57", None)


def test_lowest_numbered_chunk_wins():
    """Must match the buildspec's staging rule: lowest chunk wins each file."""
    keys = {
        f"{PREFIX}/satdump/chunk_3/dataset.json": _dataset(CHUNK3_TS),
        f"{PREFIX}/satdump/chunk_0/dataset.json": _dataset(CHUNK0_TS),
        f"{PREFIX}/satdump/chunk_10/dataset.json": _dataset(CHUNK3_TS),
    }
    orch = _orchestrator(keys)

    # chunk_10 must not beat chunk_3 on a string sort.
    time, _ = orch._lookup_acquisition("bucket", list(keys))
    assert time == "11:59:57"


def test_cadence_measured_from_consecutive_chunks():
    """The gap chunk_0 -> chunk_1 is what one composite covers along-track."""
    keys = {
        f"{PREFIX}/satdump/chunk_0/dataset.json": _dataset(CHUNK0_TS),
        f"{PREFIX}/satdump/chunk_1/dataset.json": _dataset(CHUNK0_TS + 30),
        f"{PREFIX}/satdump/chunk_2/dataset.json": _dataset(CHUNK0_TS + 60),
    }
    orch = _orchestrator(keys)

    assert orch._lookup_acquisition("bucket", list(keys)) == ("11:59:57", 30.0)


@pytest.mark.parametrize("gap", [-30.0, 0.0, 3600.0])
def test_implausible_cadence_is_dropped(gap):
    """Out-of-order or absurd gaps must not become a propagation window."""
    keys = {
        f"{PREFIX}/satdump/chunk_0/dataset.json": _dataset(CHUNK0_TS),
        f"{PREFIX}/satdump/chunk_1/dataset.json": _dataset(CHUNK0_TS + gap),
    }
    orch = _orchestrator(keys)

    time, duration = orch._lookup_acquisition("bucket", list(keys))
    assert time == "11:59:57"
    assert duration is None


@pytest.mark.parametrize(
    "objects, keys",
    [
        ({}, []),                                                        # no dataset.json
        ({f"{PREFIX}/satdump/chunk_0/dataset.json": b"{}"},              # no timestamp field
         [f"{PREFIX}/satdump/chunk_0/dataset.json"]),
        ({f"{PREFIX}/satdump/chunk_0/dataset.json": b"not json"},        # corrupt
         [f"{PREFIX}/satdump/chunk_0/dataset.json"]),
    ],
)
def test_missing_or_unusable_timestamp_returns_none(objects, keys):
    assert _orchestrator(objects)._lookup_acquisition("bucket", keys) == (None, None)


def test_flag_rendered_into_satdump_buildspec():
    cb = FakeCodeBuild()
    orch = _orchestrator(codebuild=cb)

    orch._submit_codebuild(
        path="satdump",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time="11:59:57",
        chunk_duration=30.0,
    )

    spec = cb.calls[0]["buildspecOverride"]
    assert '--contact-time "11:59:57"' in spec
    assert '--pass-duration-seconds "30"' in spec
    assert "__CONTACT_TIME_ARG__" not in spec
    assert "__PASS_DURATION_ARG__" not in spec


def test_flag_omitted_when_time_unknown():
    """An empty --contact-time fails to parse; no flag is better than a bad one."""
    cb = FakeCodeBuild()
    orch = _orchestrator(codebuild=cb)

    orch._submit_codebuild(
        path="satdump",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time=None,
    )

    spec = cb.calls[0]["buildspecOverride"]
    assert "--contact-time" not in spec
    assert "--pass-duration-seconds" not in spec
    assert "__CONTACT_TIME_ARG__" not in spec
    assert "__PASS_DURATION_ARG__" not in spec


def test_nasa_path_carries_no_contact_time():
    """The NASA path takes geolocation from the GEO HDF5, not from a TLE."""
    cb = FakeCodeBuild()
    orch = _orchestrator(codebuild=cb)

    orch._submit_codebuild(
        path="nasa",
        contact_id="ba2c5446",
        contact_date="2026/08/31",
        input_prefix=PREFIX,
        contact_time="11:59:57",
    )

    assert "--contact-time" not in cb.calls[0]["buildspecOverride"]


def test_nasa_detection_matches_j01_names():
    """CSPP writes SVI01_j01_*; the old patterns only knew _npp_."""
    orch = _orchestrator()
    keys = [f"{PREFIX}/sdr/SVI01_j01_d20260831_t1206350_e1207596_b45517_c2026_cspp_dev.h5"]

    assert orch._detect_path(keys) == "nasa"
