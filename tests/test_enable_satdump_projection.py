"""The build-time patch that turns on SatDump's own projection.

At SatDump 1.2.2 composites live in satdump_cfg.json under
viewer.instruments.<instrument>.rgb_composites, and
products/processor/image_processor.cpp reprojects any composite carrying a
`project` block. This script adds that block at image build time.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "enable_satdump_projection",
    _ROOT / "docker" / "sdr-pipeline" / "enable_satdump_projection.py",
)
patcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patcher)


# Shaped like the real file: JSON with // comments, which json.load rejects.
CONFIG = """{
    // Settings for the SatDump UI
    "viewer": {
        "instruments": {
            "viirs": {
                "handler": "image_handler",
                "name": "VIIRS",
                "rgb_composites": {
                    "True Color": {
                        "equation": "chm5, chm4, chm3",  // M-bands
                        "equalize": true
                    },
                    "Day Microphysics": {
                        "equation": "chi1, chi3^2.5,chi4",
                        "individual_equalize": true
                    },
                    "10.8um Thermal IR (Uncalibrated)": {
                        "equation": "chi5"
                    },
                    "Urban False Color": {
                        "equation": "chm11,chm10,chm5"
                    }
                }
            }
        }
    }
}
"""


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "satdump_cfg.json"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(patcher, "CANDIDATE_PATHS", [path])
    return path


def _composites(path: Path) -> dict:
    data = json.loads(patcher.strip_comments(path.read_text(encoding="utf-8")))
    return data["viewer"]["instruments"]["viirs"]["rgb_composites"]


class TestCommentStripping:
    def test_line_comments_are_removed(self):
        assert json.loads(patcher.strip_comments('{"a": 1} // trailing'))["a"] == 1

    def test_urls_inside_strings_survive(self):
        """A naive // strip would truncate any string holding a URL."""
        text = '{"url": "https://satdump.org/x"}'

        assert json.loads(patcher.strip_comments(text))["url"] == "https://satdump.org/x"


class TestPatching:
    def test_the_delivered_composites_gain_a_project_block(self, config_file):
        patcher.main()

        composites = _composites(config_file)
        assert composites["True Color"]["project"]["config"]["type"] == "equirec"
        assert composites["True Color"]["project"]["img_format"] == ".tif"
        assert "project" in composites["Day Microphysics"]
        assert "project" in composites["10.8um Thermal IR (Uncalibrated)"]

    def test_other_composites_are_left_alone(self, config_file):
        """Projecting every composite would multiply the work for no benefit."""
        patcher.main()

        assert "project" not in _composites(config_file)["Urban False Color"]

    def test_existing_equations_are_preserved(self, config_file):
        patcher.main()

        assert _composites(config_file)["True Color"]["equation"] == "chm5, chm4, chm3"

    def test_running_twice_changes_nothing(self, config_file):
        patcher.main()
        first = config_file.read_text(encoding="utf-8")

        patcher.main()

        assert config_file.read_text(encoding="utf-8") == first

    def test_the_original_is_kept_as_a_backup(self, config_file):
        patcher.main()

        backup = config_file.with_suffix(".json.orig")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == CONFIG

    def test_a_missing_config_fails_loudly(self, tmp_path, monkeypatch):
        """Silently skipping would ship an image that quietly does not project."""
        monkeypatch.setattr(patcher, "CANDIDATE_PATHS", [tmp_path / "absent.json"])

        with pytest.raises(SystemExit):
            patcher.main()

    def test_an_unrecognised_config_fails_loudly(self, tmp_path, monkeypatch):
        path = tmp_path / "satdump_cfg.json"
        path.write_text('{"viewer": {"instruments": {}}}', encoding="utf-8")
        monkeypatch.setattr(patcher, "CANDIDATE_PATHS", [path])

        with pytest.raises(SystemExit):
            patcher.main()
