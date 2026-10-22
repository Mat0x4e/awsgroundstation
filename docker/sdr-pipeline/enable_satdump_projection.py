#!/usr/bin/env python3
"""Turn on SatDump's own projection for the VIIRS composites we deliver.

SatDump can georeference its composites itself. It knows the VIIRS scan model
(``resources/projections_settings/jpss1_viirs.json``: aggregation zones in
``forced_gcps_x``, ``scan_angle`` 112, ``roll_offset`` -0.05, ``yaw_offset``
0.15) and it has the per-scan timestamps in the product, so it raytraces each
line to the ellipsoid with the right pointing. Reimplementing that downstream
means reproducing a model that ships in this very image, and getting the parts
it does not document -- the roll/yaw corrections, the exact line timing --
wrong.

At SatDump 1.2.2 composites live in ``satdump_cfg.json`` under
``viewer.instruments.<instrument>.rgb_composites``, and
``products/processor/image_processor.cpp`` reprojects any composite carrying a
``project`` block, writing ``rgb_<name>_projected<img_format>``. This script
adds that block to the composites the pipeline delivers.

Idempotent: running it twice changes nothing. Run at image build time, after
the SatDump .deb is installed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CANDIDATE_PATHS = [
    Path("/usr/share/satdump/satdump_cfg.json"),
    Path("/usr/local/share/satdump/satdump_cfg.json"),
    Path("/opt/satdump/satdump_cfg.json"),
]

INSTRUMENT = "viirs"

# The three composites the visualization stage publishes.
COMPOSITES = ["True Color", "Day Microphysics"]
THERMAL_MATCH = re.compile(r"thermal ir", re.IGNORECASE)

# Equirectangular, sized automatically from the swath. 0.01 deg is about 1.1 km,
# which matches VIIRS M-band at nadir without inventing resolution at the edges.
PROJECT_BLOCK = {
    "config": {
        "type": "equirec",
        "auto": True,
        "scalar_x": 0.01,
        "scalar_y": -0.01,
    },
    "img_format": ".tif",
}


def strip_comments(text: str) -> str:
    """SatDump's config is JSON with // comments, which json.load rejects."""
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)(?<![:\"\w])//.*?$", "", without_block)


def find_config() -> Path:
    for path in CANDIDATE_PATHS:
        if path.is_file():
            return path
    searched = ", ".join(str(p) for p in CANDIDATE_PATHS)
    raise SystemExit(f"satdump_cfg.json not found in any of: {searched}")


def main() -> int:
    config_path = find_config()
    original = config_path.read_text(encoding="utf-8")
    config = json.loads(strip_comments(original))

    try:
        composites = config["viewer"]["instruments"][INSTRUMENT]["rgb_composites"]
    except KeyError as exc:
        raise SystemExit(
            f"no viewer.instruments.{INSTRUMENT}.rgb_composites in {config_path}: {exc}"
        )

    wanted = [name for name in composites if name in COMPOSITES or THERMAL_MATCH.search(name)]
    if not wanted:
        raise SystemExit(
            f"none of the expected composites found. Available: {sorted(composites)}"
        )

    changed = []
    for name in wanted:
        if composites[name].get("project") == PROJECT_BLOCK:
            continue
        composites[name]["project"] = dict(PROJECT_BLOCK)
        changed.append(name)

    if not changed:
        print(f"projection already enabled for {len(wanted)} composite(s); nothing to do")
        return 0

    backup = config_path.with_suffix(".json.orig")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")

    config_path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    print(f"enabled SatDump projection for: {', '.join(changed)}")
    print(f"  config : {config_path}")
    print(f"  backup : {backup}")
    print("  output : rgb_<name>_projected.tif alongside each composite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
