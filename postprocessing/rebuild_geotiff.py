#!/usr/bin/env python3
"""Rebuild a geolocated GeoTIFF from CSPP VIIRS SDR + terrain-corrected GEO.

Reads the official CSPP BrightnessTemperature (Kelvin) for a thermal band and
the per-pixel terrain-corrected lat/lon from the matching G*TCO geo file, then
resamples the swath onto a regular WGS84 (EPSG:4326) grid and writes a north-up
GeoTIFF (+ a quick PNG preview).

Inputs come from the CSPP SDR step (see scripts/cspp_rdr_input.yml). Note CSPP
emits terrain-corrected geo (GMTCO/GITCO, groups VIIRS-*-GEO-TC_All), not the
ellipsoid GMODO/GIGTO the deployed scripts/viirs/geo_reader.py expects.

Deps (pip): h5py numpy rasterio scipy matplotlib  (all have Windows wheels)

Examples
--------
# M-band M15 (10.76 um), ~750 m:
python rebuild_geotiff.py \\
    --sdr SVM15.h5 --geo GMTCO.h5 \\
    --band-group VIIRS-M15-SDR_All --geo-group VIIRS-MOD-GEO-TC_All \\
    --res-deg 0.0067 --out-tif m15.tif --out-png m15.png --label "M15 (10.76um)"

# I-band I5 (11.45 um), ~375 m (slower — ~9.8M pts):
python rebuild_geotiff.py \\
    --sdr SVI05.h5 --geo GITCO.h5 \\
    --band-group VIIRS-I5-SDR_All --geo-group VIIRS-IMG-GEO-TC_All \\
    --res-deg 0.004 --out-tif i5.tif --out-png i5.png --label "I5 (11.45um, 375m)"
"""
import argparse, sys, time
from pathlib import Path
import h5py
import numpy as np
from scipy.interpolate import griddata
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# VIIRS uint16 fill/NA sentinels occupy the top of the range (65528..65535).
FILL_MIN = 65528

def read_bt(sdr_path, band_group):
    with h5py.File(sdr_path, "r") as h:
        g = h[f"All_Data/{band_group}"]
        raw = g["BrightnessTemperature"][()].astype(np.uint16)
        scale, offset = [float(x) for x in g["BrightnessTemperatureFactors"][()].ravel()[:2]]
    bt = raw.astype(np.float32) * scale + offset
    bt = np.ma.masked_array(bt, mask=(raw >= FILL_MIN), fill_value=np.nan)
    return bt

def read_geo(geo_path, geo_group):
    with h5py.File(geo_path, "r") as h:
        g = h[f"All_Data/{geo_group}"]
        lat = g["Latitude"][()].astype(np.float32)
        lon = g["Longitude"][()].astype(np.float32)
    lat = np.ma.masked_array(lat, mask=(lat < -900))
    lon = np.ma.masked_array(lon, mask=(lon < -900))
    return lat, lon

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdr", required=True)
    ap.add_argument("--geo", required=True)
    ap.add_argument("--band-group", required=True, help="e.g. VIIRS-M15-SDR_All")
    ap.add_argument("--geo-group", required=True, help="e.g. VIIRS-MOD-GEO-TC_All")
    ap.add_argument("--res-deg", type=float, default=0.0067, help="output grid resolution (deg)")
    ap.add_argument("--method", default="linear", choices=["linear", "nearest"])
    ap.add_argument("--out-tif", required=True)
    ap.add_argument("--out-png", required=True)
    ap.add_argument("--label", default="VIIRS")
    a = ap.parse_args()

    t0 = time.time()
    bt = read_bt(a.sdr, a.band_group)
    lat, lon = read_geo(a.geo, a.geo_group)
    print(f"[read] BT {bt.shape} valid={ (~bt.mask).sum() }  "
          f"BT range {bt.min():.1f}..{bt.max():.1f} K", flush=True)

    d = np.asarray(bt.filled(np.nan), np.float32).ravel()
    la = np.asarray(lat.filled(np.nan), np.float64).ravel()
    lo = np.asarray(lon.filled(np.nan), np.float64).ravel()
    ok = np.isfinite(d) & np.isfinite(la) & np.isfinite(lo)
    d, la, lo = d[ok], la[ok], lo[ok]
    print(f"[bbox] lat {la.min():.3f}..{la.max():.3f}  lon {lo.min():.3f}..{lo.max():.3f}  "
          f"npix={d.size}", flush=True)

    res = a.res_deg
    glon = np.arange(lo.min(), lo.max(), res)
    glat = np.arange(la.min(), la.max(), res)
    W, H = len(glon), len(glat)
    print(f"[grid] {W} x {H} @ {res} deg, method={a.method}", flush=True)
    GX, GY = np.meshgrid(glon, glat)
    grid = griddata((lo, la), d, (GX, GY), method=a.method).astype(np.float32)
    grid = np.flipud(grid)  # north-up
    print(f"[grid] filled cells={np.isfinite(grid).sum()} / {grid.size}", flush=True)

    transform = from_bounds(lo.min(), la.min(), lo.max(), la.max(), W, H)
    Path(a.out_tif).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(a.out_tif, "w", driver="GTiff", height=H, width=W, count=1,
                       dtype=rasterio.float32, crs=CRS.from_epsg(4326),
                       transform=transform, nodata=float("nan"),
                       compress="deflate") as dst:
        dst.write(grid, 1)
        dst.set_band_description(1, f"{a.label} brightness temperature (K)")
    print(f"[tif ] wrote {a.out_tif}", flush=True)

    # PNG preview (percentile stretch, inverted so cold clouds are bright)
    vmin, vmax = np.nanpercentile(grid, [2, 98])
    fig, ax = plt.subplots(figsize=(11, 8), dpi=110)
    im = ax.imshow(grid, extent=[lo.min(), lo.max(), la.min(), la.max()],
                   origin="upper", cmap="inferno_r", vmin=vmin, vmax=vmax,
                   interpolation="nearest")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.set_title(f"NOAA-20 VIIRS {a.label} — brightness temperature (K)\n"
                 f"CSPP SDR + terrain-corrected geolocation")
    ax.grid(True, ls=":", lw=0.4, alpha=0.5)
    fig.colorbar(im, ax=ax, shrink=0.8, label="Kelvin")
    fig.tight_layout(); fig.savefig(a.out_png); plt.close(fig)
    print(f"[png ] wrote {a.out_png}", flush=True)
    print(f"[done] {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    sys.exit(main())
