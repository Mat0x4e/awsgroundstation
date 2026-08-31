"""Pass planner - find the AWS Ground Station contact that best images a target area.

AWS Ground Station's ListContacts tells you *when the satellite is in radio view
of an antenna*. It says nothing about *what the instrument is looking at* during
that window. For a real-time direct-broadcast payload like VIIRS HRD those are
two different questions: the antenna must see the satellite at the same moment
the VIIRS swath is crossing the area you care about.

This script answers the second question offline, from a TLE:

  1. propagate NOAA-20 (SGP4) over the next N days
  2. find the windows where it is above the horizon mask of a ground station
  3. for each window, sweep the VIIRS cross-track swath over a set of target
     points and record which ones are actually imaged *while the link is up*
  4. score each pass on coverage, swath position, sun elevation and sun glint

Usage:
    python scripts/plan_pass.py [--days 14] [--station "Stockholm 1"] [--json out.json]

Dependencies: sgp4, requests.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone

import requests
from sgp4.api import Satrec, jday

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NORAD_ID = 43013  # NOAA-20 / JPSS-1
CELESTRAK_URL = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={NORAD_ID}&FORMAT=3LE"

WGS84_A = 6378.137  # km
WGS84_F = 1.0 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)
WGS84_E2 = WGS84_F * (2 - WGS84_F)
EARTH_R = 6371.0  # km, spherical mean, used for ground distances

# VIIRS scans +/-56.28 deg about nadir -> 3060 km swath at 833 km altitude.
SWATH_HALF_KM = 1530.0
# Beyond ~1000 km off nadir the bow-tie aggregation is exhausted and the ground
# sample grows past ~0.8 km (I-band); treat that as the usable "core".
SWATH_CORE_KM = 1000.0

# AWS Ground Station antennas onboarded for NOAA-20 on this account
# (see terraform.tfvars - Ireland 1 does NOT carry NOAA-20).
# Coordinates are published-site approximations; a few tens of km of error is
# irrelevant at these slant ranges.
STATIONS = {
    "Stockholm 1": (59.40, 18.00, 0.05),
    "Ohio 1": (39.98, -83.00, 0.25),
    "Oregon 1": (45.80, -119.70, 0.20),
    "Hawaii 1": (19.01, -155.66, 0.10),
    "Cape Town 1": (-33.93, 18.42, 0.05),
}

# Mediterranean sub-basins, west to east.
TARGETS = [
    ("Alboran Sea", 36.0, -3.5),
    ("Balearic Sea", 39.5, 3.0),
    ("Gulf of Lion", 42.5, 5.0),
    ("Ligurian Sea", 43.5, 8.5),
    ("Tyrrhenian Sea", 39.5, 12.5),
    ("Strait of Sicily", 37.0, 12.0),
    ("North Adriatic", 44.5, 13.0),
    ("South Adriatic", 41.5, 17.5),
    ("Gulf of Sidra", 33.0, 18.0),
    ("Ionian Sea", 36.5, 18.5),
    ("Aegean Sea", 38.0, 25.0),
    ("Sea of Crete", 34.5, 25.5),
    ("Levantine (Cyprus)", 34.5, 33.0),
]

# Naval anchorages worth a look inside the covered area.
PORTS = [
    ("Toulon (FR carrier base)", 43.10, 5.90),
    ("Naples (US 6th Fleet)", 40.80, 14.25),
    ("Souda Bay (Crete)", 35.50, 24.15),
    ("Augusta Bay (Sicily)", 37.20, 15.22),
]


# ---------------------------------------------------------------------------
# Geodesy / time helpers
# ---------------------------------------------------------------------------


def gmst_deg(jd: float, fr: float) -> float:
    """Greenwich mean sidereal time in degrees (IAU 1982)."""
    t = ((jd - 2451545.0) + fr) / 36525.0
    sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t * t
        - 6.2e-6 * t * t * t
    )
    return (sec % 86400.0) * 360.0 / 86400.0


def teme_to_ecef(r, g_deg):
    g = math.radians(g_deg)
    cg, sg = math.cos(g), math.sin(g)
    return (r[0] * cg + r[1] * sg, -r[0] * sg + r[1] * cg, r[2])


def ecef_to_geodetic(x, y, z):
    """Bowring's method. Returns (lat_deg, lon_deg, alt_km)."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    theta = math.atan2(z * WGS84_A, p * WGS84_B)
    ep2 = (WGS84_A**2 - WGS84_B**2) / WGS84_B**2
    lat = math.atan2(
        z + ep2 * WGS84_B * math.sin(theta) ** 3,
        p - WGS84_E2 * WGS84_A * math.cos(theta) ** 3,
    )
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), alt


def geodetic_to_ecef(lat_deg, lon_deg, alt_km):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    return (
        (n + alt_km) * math.cos(lat) * math.cos(lon),
        (n + alt_km) * math.cos(lat) * math.sin(lon),
        (n * (1 - WGS84_E2) + alt_km) * math.sin(lat),
    )


def elevation_deg(sat_ecef, gs_ecef, gs_lat, gs_lon):
    """Topocentric elevation of sat_ecef as seen from the ground station."""
    dx = [sat_ecef[i] - gs_ecef[i] for i in range(3)]
    lat, lon = math.radians(gs_lat), math.radians(gs_lon)
    up = (
        math.cos(lat) * math.cos(lon) * dx[0]
        + math.cos(lat) * math.sin(lon) * dx[1]
        + math.sin(lat) * dx[2]
    )
    rng = math.sqrt(sum(c * c for c in dx))
    return math.degrees(math.asin(max(-1.0, min(1.0, up / rng)))), rng


def gc_distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def sun_ecef(jd, fr):
    """Low-precision solar position in ECEF (km). Accurate to ~0.01 deg."""
    d = (jd - 2451545.0) + fr
    mean_lon = (280.460 + 0.9856474 * d) % 360.0
    mean_anom = math.radians((357.528 + 0.9856003 * d) % 360.0)
    ecl_lon = math.radians(
        mean_lon + 1.915 * math.sin(mean_anom) + 0.020 * math.sin(2 * mean_anom)
    )
    obliq = math.radians(23.439 - 0.0000004 * d)
    r_au = 1.00014 - 0.01671 * math.cos(mean_anom) - 0.00014 * math.cos(2 * mean_anom)
    r_km = r_au * 149597870.7
    eci = (
        r_km * math.cos(ecl_lon),
        r_km * math.cos(obliq) * math.sin(ecl_lon),
        r_km * math.sin(obliq) * math.sin(ecl_lon),
    )
    return teme_to_ecef(eci, gmst_deg(jd, fr))


def local_zenith_azimuth(target_ecef, lat, lon, obj_ecef):
    """Zenith and azimuth angles of obj as seen from a surface point."""
    dx = [obj_ecef[i] - target_ecef[i] for i in range(3)]
    la, lo = math.radians(lat), math.radians(lon)
    up = (
        math.cos(la) * math.cos(lo) * dx[0]
        + math.cos(la) * math.sin(lo) * dx[1]
        + math.sin(la) * dx[2]
    )
    north = (
        -math.sin(la) * math.cos(lo) * dx[0]
        - math.sin(la) * math.sin(lo) * dx[1]
        + math.cos(la) * dx[2]
    )
    east = -math.sin(lo) * dx[0] + math.cos(lo) * dx[1]
    rng = math.sqrt(sum(c * c for c in dx))
    zen = math.degrees(math.acos(max(-1.0, min(1.0, up / rng))))
    az = math.degrees(math.atan2(east, north)) % 360.0
    return zen, az


def glint_angle_deg(sun_zen, sun_az, view_zen, view_az):
    """Angle between the specular reflection of the sun and the view direction.

    Small values (< ~20 deg) mean the sensor is staring into the sun's specular
    lobe on the sea surface: that is where wakes and slicks show up as contrast
    against a mirror, and where flat water saturates.
    """
    sz, vz = math.radians(sun_zen), math.radians(view_zen)
    dphi = math.radians(sun_az - view_az)
    c = math.cos(sz) * math.cos(vz) + math.sin(sz) * math.sin(vz) * math.cos(dphi)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# ---------------------------------------------------------------------------
# TLE
# ---------------------------------------------------------------------------


def fetch_tle():
    resp = requests.get(CELESTRAK_URL, timeout=30)
    resp.raise_for_status()
    lines = [ln.strip() for ln in resp.text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        raise RuntimeError(f"unexpected CelesTrak response: {resp.text[:200]!r}")
    return lines[0], lines[1], lines[2]


# ---------------------------------------------------------------------------
# Propagation and pass extraction
# ---------------------------------------------------------------------------


def propagate(sat, start: datetime, days: int, step_s: int):
    """Yield (datetime, jd, fr, sat_ecef, sublat, sublon) at a fixed cadence."""
    n = int(days * 86400 / step_s)
    for i in range(n + 1):
        t = start + timedelta(seconds=i * step_s)
        jd, fr = jday(
            t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond * 1e-6
        )
        err, r, _v = sat.sgp4(jd, fr)
        if err != 0:
            continue
        ecef = teme_to_ecef(r, gmst_deg(jd, fr))
        lat, lon, _ = ecef_to_geodetic(*ecef)
        yield t, jd, fr, ecef, lat, lon


def find_passes(samples, gs_lat, gs_lon, gs_alt, min_elev):
    """Split a propagated track into above-mask windows."""
    gs = geodetic_to_ecef(gs_lat, gs_lon, gs_alt)
    passes, current = [], []
    for t, jd, fr, ecef, lat, lon in samples:
        el, rng = elevation_deg(ecef, gs, gs_lat, gs_lon)
        if el >= min_elev:
            current.append(
                {
                    "t": t,
                    "jd": jd,
                    "fr": fr,
                    "ecef": ecef,
                    "lat": lat,
                    "lon": lon,
                    "elev": el,
                    "range": rng,
                }
            )
        elif current:
            passes.append(current)
            current = []
    if current:
        passes.append(current)
    return passes


def windows_from_listcontacts(samples, path, gs_lat, gs_lon, gs_alt):
    """Use the windows AWS actually offers instead of an assumed horizon mask.

    Feed it the output of:

        aws groundstation list-contacts --status-list AVAILABLE \\
            --ground-station "Stockholm 1" --satellite-arn <arn> \\
            --mission-profile-arn <arn> \\
            --start-time <now> --end-time <now+14d> > contacts.json

    The assumed mask is the weakest input to this whole calculation: the four
    contacts already flown imply an effective Stockholm mask well above 10 deg,
    which moves the southern edge of the reachable swath by hundreds of km.
    ListContacts removes the guess.
    """
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    offered = []
    for c in payload.get("contactList", []):
        # The CLI renders these with the caller's local UTC offset, not "Z".
        # Convert, do not merely strip the offset.
        start = datetime.fromisoformat(c["startTime"]).astimezone(timezone.utc)
        end = datetime.fromisoformat(c["endTime"]).astimezone(timezone.utc)
        offered.append((start.replace(tzinfo=None), end.replace(tzinfo=None), c))

    gs = geodetic_to_ecef(gs_lat, gs_lon, gs_alt)
    buckets = [[] for _ in offered]
    for t, jd, fr, ecef, lat, lon in samples:
        for i, (start, end, _c) in enumerate(offered):
            if start <= t <= end:
                el, rng = elevation_deg(ecef, gs, gs_lat, gs_lon)
                buckets[i].append(
                    {"t": t, "jd": jd, "fr": fr, "ecef": ecef, "lat": lat,
                     "lon": lon, "elev": el, "range": rng}
                )
    return [b for b in buckets if len(b) >= 3], offered


def analyse_pass(window, targets, ports):
    """Which targets does the VIIRS swath sweep while the downlink is up?"""
    if len(window) < 3:
        return None

    # Ground-track heading, needed to place a target left or right of nadir.
    for i, s in enumerate(window):
        nxt = window[min(i + 1, len(window) - 1)]
        prv = window[max(i - 1, 0)]
        s["heading"] = bearing_deg(prv["lat"], prv["lon"], nxt["lat"], nxt["lon"])

    covered = []
    for name, tlat, tlon in targets:
        best = None
        for idx, s in enumerate(window):
            d = gc_distance_km(s["lat"], s["lon"], tlat, tlon)
            if best is None or d < best[0]:
                best = (d, idx, s)
        d, idx, s = best
        # A closest approach pinned to a window edge means the true perpendicular
        # scan of this target falls outside the window: the swath was still
        # sweeping toward it when the link dropped, or had not opened yet.
        edge = idx == 0 or idx == len(window) - 1
        if d > SWATH_HALF_KM or edge:
            continue

        t_ecef = geodetic_to_ecef(tlat, tlon, 0.0)
        sun = sun_ecef(s["jd"], s["fr"])
        sun_zen, sun_az = local_zenith_azimuth(t_ecef, tlat, tlon, sun)
        view_zen, view_az = local_zenith_azimuth(t_ecef, tlat, tlon, s["ecef"])
        side_bearing = bearing_deg(s["lat"], s["lon"], tlat, tlon)
        rel = (side_bearing - s["heading"]) % 360.0
        side = "right" if 180.0 < rel < 360.0 else "left"

        covered.append(
            {
                "target": name,
                "lat": tlat,
                "lon": tlon,
                "imaged_at": s["t"].replace(tzinfo=timezone.utc).isoformat(),
                # How deep into the contact the basin is scanned. Anything in the
                # first ~85 s lands in the opening granule, the one most exposed
                # to acquisition transients.
                "s_after_aos": int((s["t"] - window[0]["t"]).total_seconds()),
                "cross_track_km": round(d, 1),
                "in_core": d <= SWATH_CORE_KM,
                "side": side,
                "sun_elevation_deg": round(90.0 - sun_zen, 1),
                "view_zenith_deg": round(view_zen, 1),
                "glint_angle_deg": round(
                    glint_angle_deg(sun_zen, sun_az, view_zen, view_az), 1
                ),
                "link_elevation_deg": round(s["elev"], 1),
            }
        )

    if not covered:
        return None

    port_hits = []
    for name, plat, plon in ports:
        best = min((gc_distance_km(s["lat"], s["lon"], plat, plon), i)
                   for i, s in enumerate(window))
        # Same pre-AOS / post-LOS test as the basins: a closest approach pinned
        # to a window edge means the scan line through this port fell outside
        # the contact, so nothing of it was received.
        if best[0] <= SWATH_HALF_KM and 0 < best[1] < len(window) - 1:
            s = window[best[1]]
            t_ecef = geodetic_to_ecef(plat, plon, 0.0)
            sun = sun_ecef(s["jd"], s["fr"])
            sun_zen, sun_az = local_zenith_azimuth(t_ecef, plat, plon, sun)
            view_zen, view_az = local_zenith_azimuth(t_ecef, plat, plon, s["ecef"])
            port_hits.append(
                {
                    "port": name,
                    "cross_track_km": round(best[0], 1),
                    "s_after_aos": int((s["t"] - window[0]["t"]).total_seconds()),
                    "glint_angle_deg": round(
                        glint_angle_deg(sun_zen, sun_az, view_zen, view_az), 1
                    ),
                    "sun_elevation_deg": round(90.0 - sun_zen, 1),
                }
            )

    aos, los = window[0], window[-1]
    duration_s = (los["t"] - aos["t"]).total_seconds()
    max_el = max(s["elev"] for s in window)
    sun_els = [c["sun_elevation_deg"] for c in covered]
    core = [c for c in covered if c["in_core"]]
    best_glint = min(c["glint_angle_deg"] for c in covered)
    # Link elevation while the Mediterranean is actually being scanned. Every
    # previous contact lost packets at low elevation, and a lost packet is a
    # partial granule that CSPP will not calibrate - so this, not the pass
    # maximum, is what decides how much of the basin survives to Level 1.
    med_link_el = min(c["link_elevation_deg"] for c in covered)

    # Score: coverage first, then image quality (core swath, sun high, link
    # solid while the basin is under the swath), then pass length - a longer
    # link is more 85.4 s granules on the ground.
    score = (
        len(covered) * 10.0
        + len(core) * 6.0
        + min(sun_els) * 0.4
        + med_link_el * 0.5
        + duration_s / 60.0 * 2.0
        - max(0.0, best_glint - 25.0) * 0.15
    )

    return {
        "aos_utc": aos["t"].replace(tzinfo=timezone.utc).isoformat(),
        "los_utc": los["t"].replace(tzinfo=timezone.utc).isoformat(),
        "duration_s": int(duration_s),
        "granules_est": round(duration_s / 85.4, 1),
        "max_elevation_deg": round(max_el, 1),
        "min_link_elevation_over_med_deg": round(med_link_el, 1),
        "min_sun_elevation_deg": round(min(sun_els), 1),
        "targets_covered": len(covered),
        "targets_in_core_swath": len(core),
        "best_glint_angle_deg": best_glint,
        "coverage": covered,
        "ports": port_hits,
        "subtrack_aos": [round(aos["lat"], 2), round(aos["lon"], 2)],
        "subtrack_los": [round(los["lat"], 2), round(los["lon"], 2)],
        "score": round(score, 1),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--station", default="Stockholm 1", choices=sorted(STATIONS))
    ap.add_argument(
        "--min-elevation",
        type=float,
        default=5.0,
        help="antenna horizon mask in degrees (AWS books from ~5 deg)",
    )
    ap.add_argument("--step", type=int, default=15, help="propagation step, seconds")
    ap.add_argument("--contacts-json",
                    help="output of 'aws groundstation list-contacts'; use the "
                         "windows AWS really offers instead of --min-elevation")
    ap.add_argument("--all-passes", action="store_true",
                    help="do not filter out night passes")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--json", help="write full results to this path")
    args = ap.parse_args()

    name, l1, l2 = fetch_tle()
    sat = Satrec.twoline2rv(l1, l2)
    epoch = datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(
        days=(sat.jdsatepoch + sat.jdsatepochF) - 2451545.0
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    age_h = (now.replace(tzinfo=timezone.utc) - epoch).total_seconds() / 3600.0

    print(f"TLE      : {name.strip()}  (epoch {epoch:%Y-%m-%d %H:%M} UTC, {age_h:.1f} h old)")
    gs_lat, gs_lon, gs_alt = STATIONS[args.station]
    source = ("AWS ListContacts" if args.contacts_json
              else f"assumed {args.min_elevation:.0f} deg mask")
    print(f"Station  : {args.station} @ {gs_lat:.2f}N {gs_lon:.2f}E, windows from {source}")
    print(f"Window   : {now:%Y-%m-%d %H:%M} UTC + {args.days} d, step {args.step} s")
    print(f"Swath    : +/-{SWATH_HALF_KM:.0f} km (core +/-{SWATH_CORE_KM:.0f} km)\n")

    samples = propagate(sat, now, args.days, args.step)
    if args.contacts_json:
        windows, offered = windows_from_listcontacts(
            samples, args.contacts_json, gs_lat, gs_lon, gs_alt
        )
        print(f"{len(offered)} contacts offered by AWS at {args.station}.")
    else:
        windows = find_passes(samples, gs_lat, gs_lon, gs_alt, args.min_elevation)

    results = []
    for w in windows:
        r = analyse_pass(w, TARGETS, PORTS)
        if r is None:
            continue
        if not args.all_passes and r["min_sun_elevation_deg"] < 10.0:
            continue
        results.append(r)

    results.sort(key=lambda r: r["score"], reverse=True)

    print(f"{len(windows)} passes above the mask, "
          f"{len(results)} of them image the Mediterranean in daylight.\n")

    for i, r in enumerate(results[: args.top], 1):
        aos = datetime.fromisoformat(r["aos_utc"])
        print(f"--- #{i}  score {r['score']}  "
              f"{aos:%a %Y-%m-%d %H:%M} UTC -> {r['los_utc'][11:16]} UTC "
              f"({r['duration_s'] // 60}m{r['duration_s'] % 60:02d}s)")
        print(f"    max elevation {r['max_elevation_deg']:>5.1f} deg | "
              f"link over the basin >= {r['min_link_elevation_over_med_deg']:.1f} deg | "
              f"~{r['granules_est']} granules")
        print(f"    sub-track {r['subtrack_aos']} -> {r['subtrack_los']}")
        print(f"    basins imaged {r['targets_covered']}/{len(TARGETS)} "
              f"({r['targets_in_core_swath']} inside the core swath) | "
              f"best glint angle {r['best_glint_angle_deg']} deg")
        for c in sorted(r["coverage"], key=lambda c: c["lon"]):
            flag = "core" if c["in_core"] else "EDGE"
            print(f"      {c['target']:<20} {c['imaged_at'][11:19]} "
                  f"+{c['s_after_aos']:>3}s  "
                  f"{c['cross_track_km']:>6.0f} km {c['side']:<6} {flag}  "
                  f"sun {c['sun_elevation_deg']:>4.1f} deg  "
                  f"glint {c['glint_angle_deg']:>5.1f} deg  "
                  f"link {c['link_elevation_deg']:>4.1f} deg")
        for p in r["ports"]:
            print(f"      * {p['port']:<26} +{p['s_after_aos']:>3}s  "
                  f"{p['cross_track_km']:>6.0f} km off nadir, "
                  f"glint {p['glint_angle_deg']:.1f} deg")
        print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "tle": [name.strip(), l1, l2],
                    "station": args.station,
                    "generated_utc": now.isoformat(),
                    "passes": results,
                },
                fh,
                indent=2,
            )
        print(f"Full results -> {args.json}")

    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
