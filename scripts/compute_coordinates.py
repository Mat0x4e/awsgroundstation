"""Compute geographic coordinates for VIIRS imagery from NOAA-20.

Uses NOAA-20 TLE + scan line timestamps (from SatDump product.cbor)
to determine the satellite ground track and approximate bounding box.

Dependencies: pyorbital, requests, cbor2 (optional, for reading product.cbor)

Usage:
    python compute_coordinates.py [product.cbor path]
    
If no argument given, uses hardcoded timestamps from the first contact.
"""

from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from pyorbital.orbital import Orbital

# NOAA-20 (JPSS-1) NORAD ID
NORAD_ID = 43013
VIIRS_SWATH_DEG = 27  # ~3000 km swath ≈ 27° at equator

# Load timestamps — either from product.cbor argument or hardcoded
if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
    import cbor2
    with open(sys.argv[1], "rb") as f:
        product = cbor2.loads(f.read())
    # Use first image's timestamps
    timestamps = product["images"][0]["timestamps"]
    print(f"Loaded {len(timestamps)} timestamps from {sys.argv[1]}")
else:
    # Hardcoded from first contact chunk (2026-06-19)
    timestamps = [
        1781870075.2595382,
        1781870077.046122,
        1781870078.832707,
        1781870080.619289,
        1781870082.405873,
        1781870084.192477,
        1781870085.979062,
        1781870087.765645,
        1781870089.552228,
        1781870091.3388112,
        1781870093.125415,
        1781870094.912001,
        1781870096.6985838,
        1781870098.4851658,
        1781870100.27175,
        1781870102.0583541,
        1781870103.844939,
    ]

# Get NOAA-20 orbital position
# Fetch TLE directly from CelesTrak GP API (JSON format, more reliable)
import requests
resp = requests.get("https://celestrak.org/NORAD/elements/gp.php?CATNR=43013&FORMAT=3LE")
if resp.status_code == 200:
    lines = resp.text.strip().split("\n")
    line1 = lines[1].strip()
    line2 = lines[2].strip()
else:
    # Fallback: use a recent TLE for NOAA-20 (NORAD 43013) from June 2026
    # This is approximate — for exact geolocation use fresh TLE
    line1 = "1 43013U 17073A   26170.05920139  .00000045  00000+0  38906-4 0  9993"
    line2 = "2 43013  98.7267 230.8542 0001437  95.2845 264.8485 14.19558374448521"
    print("  (Using fallback TLE — CelesTrak unavailable)")

orb = Orbital("NOAA-20", line1=line1, line2=line2)
print("=== NOAA-20 Ground Track ===")
print()
print(f"{'Scan Line':<10} {'Timestamp (UTC)':<22} {'Lat':>8} {'Lon':>10} {'Alt (km)':>9}")
print("-" * 65)

lats = []
lons = []
alts = []
for i, ts in enumerate(timestamps):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    lon, lat, alt = orb.get_lonlatalt(dt)
    lats.append(lat)
    lons.append(lon)
    alts.append(alt)
    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{i+1:<10} {time_str:<22} {lat:>8.3f} {lon:>10.3f} {alt:>9.1f}")

print()
print("=== Image Bounding Box (nadir track) ===")
print(f"  North: {max(lats):.3f} deg")
print(f"  South: {min(lats):.3f} deg")
print(f"  East:  {max(lons):.3f} deg")
print(f"  West:  {min(lons):.3f} deg")
print()
half_swath = VIIRS_SWATH_DEG / 2
print("=== Approximate Swath Coverage ===")
print(f"  VIIRS swath width: ~3000 km (~{VIIRS_SWATH_DEG} deg at equator)")
print(f"  Track center longitude: ~{sum(lons)/len(lons):.1f} deg")
print(f"  Approximate image extent:")
print(f"    West edge: ~{min(lons) - half_swath:.1f} deg")
print(f"    East edge: ~{max(lons) + half_swath:.1f} deg")
print()
print("=== Acquisition Window ===")
start = datetime.fromtimestamp(timestamps[0], tz=timezone.utc)
end = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc)
print(f"  Start: {start.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC")
print(f"  End:   {end.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC")
print(f"  Duration: {(end - start).total_seconds():.1f} seconds")

# Output JSON for programmatic use
result = {
    "satellite": "NOAA-20 (JPSS-1)",
    "norad_id": NORAD_ID,
    "bounding_box": {
        "north": round(max(lats), 3),
        "south": round(min(lats), 3),
        "east": round(max(lons), 3),
        "west": round(min(lons), 3),
    },
    "swath_extent": {
        "west": round(min(lons) - half_swath, 1),
        "east": round(max(lons) + half_swath, 1),
    },
    "ground_track": [{"lat": round(la, 3), "lon": round(lo, 3)} for la, lo in zip(lats, lons)],
    "acquisition": {
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "duration_s": round((end - start).total_seconds(), 1),
    },
    "altitude_km": round(sum(alts) / len(alts), 1) if alts else None,
}

output_path = Path("output/coordinates.json")
output_path.parent.mkdir(parents=True, exist_ok=True)
with open(output_path, "w") as f:
    json.dump(result, f, indent=2)
print(f"\nCoordinates saved to: {output_path}")
