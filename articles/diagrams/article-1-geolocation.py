"""Generate the scan-geometry figure for Part 1's geolocation chapter.

Draws a cross-track section through the VIIRS scan: the satellite, the nadir
ray, the +/-56 degree sweep, and where each ray meets the ellipsoid. The
detector aggregation zones are marked, because they are the reason a column
index does not map linearly onto a scan angle.

Angles and ground distances are computed, not sketched. Only the drawing scale
is chosen for legibility: the Earth's curvature is exaggerated so the arc reads
as an arc at this size.

Run: python articles/diagrams/article-1-geolocation.py
"""

from __future__ import annotations

import math
from pathlib import Path

R_KM = 6371.0
ALT_KM = 824.0
SCAN_HALF_DEG = 56.03

# Detector aggregation zone edges, as scan angles. Derived from the zone
# boundaries SatDump publishes in forced_gcps_x (+/-1184 and +/-1920 px of a
# 6400 px scan) via the cumulative sample-unit mapping: 3:1 aggregation out to
# 31.6 deg, 2:1 to 44.7 deg, 1:1 to the swath edge.
ZONES = [(0.0, 31.57, "3:1"), (31.57, 44.65, "2:1"), (44.65, SCAN_HALF_DEG, "1:1")]

WIDTH, HEIGHT = 940, 510
CX = WIDTH / 2          # nadir, horizontally centred
SURFACE_Y = 360.0       # where nadir meets the ground
R_PX = 1500.0           # exaggerated curvature, for legibility
EARTH_CY = SURFACE_Y + R_PX
SAT_Y = SURFACE_Y - (ALT_KM / R_KM) * R_PX * 0.62   # squashed, to fit the frame


def ground_arc_deg(nadir_angle_deg: float) -> float:
    """Earth-central angle from nadir to where a ray at this angle lands."""
    eta = math.radians(nadir_angle_deg)
    cos_eps = math.sin(eta) * (R_KM + ALT_KM) / R_KM
    if cos_eps >= 1.0:
        return math.degrees(math.acos(R_KM / (R_KM + ALT_KM)))
    return 90.0 - nadir_angle_deg - math.degrees(math.acos(cos_eps))


def surface_point(central_deg: float) -> tuple[float, float]:
    """Screen position of a ground point *central_deg* from nadir."""
    a = math.radians(central_deg)
    return CX + R_PX * math.sin(a), EARTH_CY - R_PX * math.cos(a)


def main() -> None:
    out = Path(__file__).with_name("article-1-geolocation.svg")
    parts: list[str] = []
    add = parts.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" font-family="Helvetica,Arial,sans-serif">')
    add('<style>'
        '.lbl{font-size:13px;fill:#1a2b3c}.sub{font-size:11px;fill:#5b6b7b}'
        '.ttl{font-size:15px;font-weight:600;fill:#1a2b3c}'
        '.ray{stroke:#2f6fb2;stroke-width:1.1;opacity:.55}'
        '.edge{stroke:#1b4f80;stroke-width:1.8}'
        '.nadir{stroke:#1b4f80;stroke-width:1.8;stroke-dasharray:5 4}'
        '</style>')
    add(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>')

    edge_arc = ground_arc_deg(SCAN_HALF_DEG)
    left = surface_point(-edge_arc)
    right = surface_point(edge_arc)

    # Earth surface
    add(f'<path d="M {left[0]:.1f} {left[1]:.1f} A {R_PX} {R_PX} 0 0 1 '
        f'{right[0]:.1f} {right[1]:.1f}" fill="none" stroke="#7c8b99" stroke-width="2"/>')
    add(f'<path d="M {left[0]:.1f} {left[1]:.1f} A {R_PX} {R_PX} 0 0 1 '
        f'{right[0]:.1f} {right[1]:.1f} L {right[0]:.1f} {HEIGHT} L {left[0]:.1f} {HEIGHT} Z" '
        f'fill="#eef2f5"/>')

    # Zone shading, mirrored either side of nadir
    shades = {"3:1": "#cfe3f5", "2:1": "#e3eef8", "1:1": "#f2f6fa"}
    for lo, hi, name in ZONES:
        for sign in (-1, 1):
            a0, a1 = sign * ground_arc_deg(lo), sign * ground_arc_deg(hi)
            p0, p1 = surface_point(a0), surface_point(a1)
            sweep = 1 if sign > 0 else 0
            add(f'<path d="M {CX:.1f} {SAT_Y:.1f} L {p0[0]:.1f} {p0[1]:.1f} '
                f'A {R_PX} {R_PX} 0 0 {sweep} {p1[0]:.1f} {p1[1]:.1f} Z" '
                f'fill="{shades[name]}" opacity=".75"/>')

    # Sample rays
    for i in range(-14, 15):
        angle = SCAN_HALF_DEG * i / 14.0
        pt = surface_point(ground_arc_deg(abs(angle)) * (1 if angle >= 0 else -1))
        cls = "edge" if abs(i) == 14 else ("nadir" if i == 0 else "ray")
        add(f'<line x1="{CX:.1f}" y1="{SAT_Y:.1f}" x2="{pt[0]:.1f}" y2="{pt[1]:.1f}" class="{cls}"/>')

    # Satellite
    add(f'<circle cx="{CX:.1f}" cy="{SAT_Y:.1f}" r="6" fill="#1b4f80"/>')
    add(f'<text class="lbl" x="{CX + 12:.1f}" y="{SAT_Y - 6:.1f}">NOAA-20</text>')
    add(f'<text class="sub" x="{CX + 12:.1f}" y="{SAT_Y + 9:.1f}">824 km, position and velocity '
        f'from the product file</text>')

    # Zone labels and ground distances
    for lo, hi, name in ZONES:
        mid_arc = (ground_arc_deg(lo) + ground_arc_deg(hi)) / 2
        for sign in (-1, 1):
            px, py = surface_point(sign * mid_arc)
            add(f'<text class="lbl" x="{px:.1f}" y="{py + 22:.1f}" text-anchor="middle">{name}</text>')
        km = R_KM * math.radians(ground_arc_deg(hi))
        px, py = surface_point(ground_arc_deg(hi))
        add(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{px:.1f}" y2="{py + 8:.1f}" stroke="#7c8b99"/>')
        add(f'<text class="sub" x="{px:.1f}" y="{py + 40:.1f}" text-anchor="middle">'
            f'{hi:.0f}° · {km:,.0f} km</text>')

    add(f'<text class="sub" x="{CX:.1f}" y="{surface_point(0)[1] + 40:.1f}" text-anchor="middle">nadir</text>')

    # Swath extent
    swath_km = 2 * R_KM * math.radians(edge_arc)
    y = HEIGHT - 26
    add(f'<line x1="{left[0]:.1f}" y1="{y}" x2="{right[0]:.1f}" y2="{y}" stroke="#1b4f80" stroke-width="1.2"/>')
    for x in (left[0], right[0]):
        add(f'<line x1="{x:.1f}" y1="{y - 5}" x2="{x:.1f}" y2="{y + 5}" stroke="#1b4f80" stroke-width="1.2"/>')
    add(f'<rect x="{CX - 118:.1f}" y="{y - 11}" width="236" height="22" fill="#ffffff"/>')
    add(f'<text class="lbl" x="{CX:.1f}" y="{y + 4}" text-anchor="middle">'
        f'swath ≈ {swath_km:,.0f} km</text>')

    # Titles
    add('<text class="ttl" x="24" y="30">Cross-track scan geometry: one row of a VIIRS composite</text>')
    add('<text class="sub" x="24" y="50">Each ray is intersected with the WGS84 ellipsoid to give one '
        'pixel a latitude and longitude.</text>')
    add('<text class="sub" x="24" y="68">Detector aggregation changes a pixel\'s angular width three '
        'times per side, so column index is not linear in scan angle.</text>')
    add('</svg>')

    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out}")
    print(f"  swath {swath_km:,.0f} km, edge at {edge_arc:.2f}° central angle")
    for lo, hi, name in ZONES:
        print(f"  {name} zone: {lo:.2f}–{hi:.2f}° scan, out to "
              f"{R_KM * math.radians(ground_arc_deg(hi)):,.0f} km from nadir")


if __name__ == "__main__":
    main()
