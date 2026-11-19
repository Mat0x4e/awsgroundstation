"""Render the ground-station world map used on slides 12 and 17 (prototype).

Reads ``ground_stations.csv`` (name, operator, label, lat, lon, source) and draws
one coloured dot per station on a Natural Earth base map. Labels are placed
with adjustText so they overlap as little as possible.

Two variants are produced so they can be compared on the slides:

* ``all`` - every station is labelled (slide 12, network comparison)
* ``aws`` - only AWS sites are labelled, other operators are plain dots (slide 17)

Stations of different operators sitting on the same spot (for example Punta
Arenas) are spread a few hundred kilometres apart so each dot stays visible.

Usage (from the repository root)::

    .venv-maps/Scripts/python.exe scripts/slides/world_map.py
    .venv-maps/Scripts/python.exe scripts/slides/world_map.py --variant aws --font-size 10

Cartopy downloads the Natural Earth shapes on first use (internet needed once)
and caches them afterwards.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write PNG files, never open a window

import numpy as np  # noqa: E402
import cartopy.crs as ccrs  # noqa: E402
import cartopy.feature as cfeature  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from adjustText import adjust_text  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_CSV = HERE / "ground_stations.csv"
DEFAULT_OUT_DIR = HERE.parent.parent / "Slidedecks" / "assets"

# Palette matched to the dark slide background and the v15 legend colours.
BACKGROUND = "#0b1626"
LAND = "#1e3f6f"
COASTLINE = "#4a6fa5"
LABEL_LINE = "#9fb3c8"
LEGEND_BOX = "#0e1f38"

# Slide 12 picture is 9144000 x 4480560 EMU (10.0 x 4.9 in).
FIG_WIDTH_IN = 10.0
FIG_HEIGHT_IN = 4.9
DPI = 200

# Stations closer than this (projected metres) are treated as co-located.
COLOCATED_DISTANCE_M = 200_000
COLOCATED_SPACING_M = 330_000


@dataclass(frozen=True)
class OperatorStyle:
    display: str
    color: str
    size: float
    scope: str  # legend wording; {n} is the number of stations in the CSV


OPERATORS: dict[str, OperatorStyle] = {
    "AWS": OperatorStyle("AWS Ground Station", "#ff9900", 70, "{n} sites"),
    "KSAT": OperatorStyle("KSAT", "#00b4ff", 28, "{n} sites listés, 28 annoncés"),
    "SSC": OperatorStyle("SSC", "#2ecc71", 28, "{n} stations propres"),
    "Leaf": OperatorStyle("Leaf Space", "#ff3d8b", 28, "{n} sites localisés, 20+ annoncés"),
}


@dataclass(frozen=True)
class Station:
    name: str
    operator: str
    label: str
    lat: float
    lon: float


def load_stations(csv_path: Path) -> list[Station]:
    """Read the station list, failing loudly on an unknown operator."""
    stations: list[Station] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            operator = row["operator"]
            if operator not in OPERATORS:
                raise ValueError(f"Unknown operator {operator!r} for {row['name']!r}")
            stations.append(
                Station(
                    name=row["name"],
                    operator=operator,
                    label=row["label"],
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            )
    return stations


def cluster_colocated(points: dict[str, tuple[float, float]], order: list[str]) -> list[list[str]]:
    """Group stations that sit within COLOCATED_DISTANCE_M of a cluster's first member.

    ``order`` fixes the order inside each cluster, so the result is deterministic.
    """
    clusters: list[list[str]] = []
    for name in order:
        x, y = points[name]
        for cluster in clusters:
            cx, cy = points[cluster[0]]
            if math.hypot(x - cx, y - cy) < COLOCATED_DISTANCE_M:
                cluster.append(name)
                break
        else:
            clusters.append([name])
    return clusters


def spread_colocated(
    points: dict[str, tuple[float, float]], clusters: list[list[str]]
) -> dict[str, tuple[float, float]]:
    """Spread each cluster's dots sideways so every dot stays visible."""
    spread = dict(points)
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        cx = sum(points[n][0] for n in cluster) / len(cluster)
        cy = sum(points[n][1] for n in cluster) / len(cluster)
        for index, name in enumerate(cluster):
            offset = (index - (len(cluster) - 1) / 2) * COLOCATED_SPACING_M
            spread[name] = (cx + offset, cy)
    return spread


def render(stations: list[Station], out_path: Path, labelled: set[str], font_size: float) -> None:
    """Draw the map and save it; ``labelled`` is the set of operators to label."""
    projection = ccrs.Robinson()
    geodetic = ccrs.PlateCarree()

    fig = plt.figure(figsize=(FIG_WIDTH_IN, FIG_HEIGHT_IN), dpi=DPI, facecolor=BACKGROUND)
    ax = fig.add_axes([0, 0, 1, 1], projection=projection)
    ax.set_facecolor(BACKGROUND)
    ax.set_extent([-180, 180, -76, 84], crs=geodetic)
    ax.spines["geo"].set_visible(False)

    ax.add_feature(
        cfeature.NaturalEarthFeature("physical", "land", "110m"),
        facecolor=LAND,
        edgecolor="none",
        zorder=1,
    )
    ax.add_feature(
        cfeature.NaturalEarthFeature("physical", "coastline", "110m"),
        facecolor="none",
        edgecolor=COASTLINE,
        linewidth=0.35,
        zorder=2,
    )

    # Project once: text placement works in the map's own (metre) coordinates.
    coords = projection.transform_points(
        geodetic,
        np.array([s.lon for s in stations]),
        np.array([s.lat for s in stations]),
    )
    raw = {s.name: (float(x), float(y)) for s, (x, y, _) in zip(stations, coords)}
    operator_rank = {name: rank for rank, name in enumerate(OPERATORS)}
    order = [s.name for s in sorted(stations, key=lambda s: operator_rank[s.operator])]
    clusters = cluster_colocated(raw, order)
    projected = spread_colocated(raw, clusters)
    cluster_of = {name: index for index, cluster in enumerate(clusters) for name in cluster}

    # Draw the small partner dots first so the larger AWS dots stay on top.
    for operator, style in reversed(list(OPERATORS.items())):
        group = [s for s in stations if s.operator == operator]
        if not group:
            continue
        ax.scatter(
            [projected[s.name][0] for s in group],
            [projected[s.name][1] for s in group],
            s=style.size,
            c=style.color,
            edgecolors="white",
            linewidths=0.4,
            zorder=4 + (1 if operator == "AWS" else 0),
        )

    texts = []
    seen_labels: set[tuple[int, str]] = set()
    for station in sorted(stations, key=lambda s: operator_rank[s.operator]):
        if station.operator not in labelled:
            continue
        # One label per name and site: the highest-priority operator keeps it.
        key = (cluster_of[station.name], station.label)
        if key in seen_labels:
            continue
        seen_labels.add(key)
        x, y = projected[station.name]
        style = OPERATORS[station.operator]
        texts.append(
            ax.text(
                x + 150_000,
                y + 150_000,
                station.label,
                color=style.color,
                fontsize=font_size,
                fontweight="bold" if station.operator == "AWS" else "normal",
                zorder=7,
            )
        )

    # Call last, after all plotting: adjustText needs the final axes extent.
    adjust_text(
        texts,
        x=[p[0] for p in projected.values()],
        y=[p[1] for p in projected.values()],
        ax=ax,
        arrowprops={"arrowstyle": "-", "color": LABEL_LINE, "linewidth": 0.4},
        expand=(1.1, 1.25),
        time_lim=10,
    )

    counts = Counter(s.operator for s in stations)
    handles = []
    for operator, style in OPERATORS.items():
        if not counts[operator]:
            continue
        handles.append(
            Line2D(
                [],
                [],
                marker="o",
                linestyle="",
                markersize=7,
                markerfacecolor=style.color,
                markeredgecolor="white",
                markeredgewidth=0.4,
                label=f"{style.display} ({style.scope.format(n=counts[operator])})",
            )
        )
    legend = ax.legend(
        handles=handles,
        loc="lower center",
        bbox_to_anchor=(0.58, 0.06),
        frameon=True,
        facecolor=LEGEND_BOX,
        edgecolor=COASTLINE,
        labelcolor="white",
        fontsize=8,
    )
    legend.set_zorder(10)

    fig.text(
        0.995,
        0.012,
        "Sources : documentation AWS (16/06/2026) ; KSAT, SSC, Leaf Space : sites publics ; "
        "coordonnées approximatives, sites colocalisés décalés",
        color="#7f93ab",
        fontsize=6,
        ha="right",
        va="bottom",
        style="italic",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=DPI, facecolor=BACKGROUND)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--variant", choices=("all", "aws", "both"), default="both")
    parser.add_argument("--font-size", type=float, default=10.0, help="label size in points")
    args = parser.parse_args()

    stations = load_stations(args.csv)
    variants = {
        "all": set(OPERATORS),
        "aws": {"AWS"},
    }
    chosen = variants if args.variant == "both" else {args.variant: variants[args.variant]}
    for name, labelled in chosen.items():
        out_path = args.out_dir / f"ground_station_map_{name}.png"
        render(stations, out_path, labelled, args.font_size)
        print(f"wrote {out_path} ({len(stations)} stations, labelled: {sorted(labelled)})")


if __name__ == "__main__":
    main()
