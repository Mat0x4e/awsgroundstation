#!/usr/bin/env python3
"""Clean full-pass VIIRS true-color GeoTIFF+PNG from CSPP M-band SDR + GMTCO.

M5=red(0.67um) M4=green(0.55um) M3=blue(0.49um). Concatenate all granules,
resample to a regular WGS84 grid via cKDTree nearest with a distance cap
(no pcolormesh striping), gamma-stretch for natural color.
"""
import glob, sys, time, re
import numpy as np, h5py
from scipy.spatial import cKDTree
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

DIR = sys.argv[1] if len(sys.argv) > 1 else "c02"
RES = float(sys.argv[2]) if len(sys.argv) > 2 else 0.009  # ~1 km
FILL = 65528

def tkey(fn):  # sort by granule start time
    m = re.search(r"_t(\d+)_", fn); return m.group(1) if m else fn

def read_refl(band):
    files = sorted(glob.glob(f"{DIR}/SVM0{band}_*.h5"), key=tkey)
    out = []
    for f in files:
        with h5py.File(f, "r") as h:
            g = h[f"All_Data/VIIRS-M{band}-SDR_All"]
            raw = g["Reflectance"][()].astype(np.uint16)
            s, o = [float(x) for x in g["ReflectanceFactors"][()].ravel()[:2]]
        r = np.where(raw >= FILL, np.nan, raw * s + o).astype(np.float32)
        out.append(r)
    return np.concatenate(out, axis=0)

def read_geo():
    files = sorted(glob.glob(f"{DIR}/GMTCO_*.h5"), key=tkey)
    la, lo = [], []
    for f in files:
        with h5py.File(f, "r") as h:
            g = h["All_Data/VIIRS-MOD-GEO-TC_All"]
            la.append(g["Latitude"][()]); lo.append(g["Longitude"][()])
    lat = np.concatenate(la, axis=0); lon = np.concatenate(lo, axis=0)
    lat = np.where(lat < -900, np.nan, lat); lon = np.where(lon < -900, np.nan, lon)
    return lat.astype(np.float64), lon.astype(np.float64)

t0 = time.time()
red, green, blue = (read_refl(5), read_refl(4), read_refl(3))
lat, lon = read_geo()
print(f"[read] shape={red.shape} in {time.time()-t0:.1f}s", flush=True)

flat = lambda a: np.asarray(a).ravel()
lo, la, r, g, b = flat(lon), flat(lat), flat(red), flat(green), flat(blue)
ok = np.isfinite(lo) & np.isfinite(la) & np.isfinite(r) & np.isfinite(g) & np.isfinite(b)
lo, la, r, g, b = lo[ok], la[ok], r[ok], g[ok], b[ok]
print(f"[valid] {lo.size} px  lat[{la.min():.1f},{la.max():.1f}] lon[{lo.min():.1f},{lo.max():.1f}]", flush=True)

glon = np.arange(lo.min(), lo.max(), RES)
glat = np.arange(la.min(), la.max(), RES)
W, H = len(glon), len(glat)
GX, GY = np.meshgrid(glon, glat)
print(f"[grid] {W}x{H} @ {RES}deg, building KDTree...", flush=True)
tree = cKDTree(np.column_stack((lo, la)))
# query nearest source point per grid cell, cap distance ~1.5 px so ocean gaps stay empty
dist, idx = tree.query(np.column_stack((GX.ravel(), GY.ravel())), k=1,
                       distance_upper_bound=RES*2.0, workers=-1)
print(f"[grid] KDTree query done {time.time()-t0:.1f}s", flush=True)
valid = np.isfinite(dist)
idx_safe = np.where(valid, idx, 0)

def band_grid(vals):
    out = vals[idx_safe].astype(np.float32)
    out[~valid] = np.nan
    return out.reshape(H, W)

rgb = np.dstack([band_grid(r), band_grid(g), band_grid(b)])
rgb = np.flipud(rgb)  # north-up
print(f"[grid] filled {np.isfinite(rgb[...,0]).sum()}/{H*W}", flush=True)

# natural-color stretch: per-channel 2-98 percentile + gamma
def stretch(ch):
    m = np.isfinite(ch)
    lo2, hi2 = np.nanpercentile(ch[m], [2, 98])
    out = np.clip((ch - lo2) / (hi2 - lo2 + 1e-9), 0, 1)
    return np.power(out, 0.62)  # gamma
bands_u8 = []
for i in range(3):
    ch = stretch(rgb[..., i])
    ch[~np.isfinite(ch)] = 0.0
    bands_u8.append((ch * 255).astype(np.uint8))
alpha = (np.isfinite(rgb[..., 0]) * 255).astype(np.uint8)
lo_min, lo_max, la_min, la_max = lo.min(), lo.max(), la.min(), la.max()
# free big intermediates before the write
del tree, GX, GY, idx, idx_safe, dist, valid, r, g, b, lo, la, red, green, blue, lat, lon

transform = from_bounds(lo_min, la_min, lo_max, la_max, W, H)
with rasterio.open(f"{DIR}/out_truecolor.tif", "w", driver="GTiff", height=H, width=W,
                   count=3, dtype="uint8", crs=CRS.from_epsg(4326), transform=transform,
                   photometric="RGB", compress="deflate") as dst:
    for i in range(3): dst.write(bands_u8[i], i+1)
print(f"[tif] wrote out_truecolor.tif  {time.time()-t0:.1f}s", flush=True)
disp = np.dstack(bands_u8).astype(np.float32) / 255.0

# PNG with lat/lon axes (black where no data)
fig, ax = plt.subplots(figsize=(11, 13), dpi=120)
disp_show = np.dstack([disp, alpha.astype(np.float32) / 255.0])
ax.imshow(disp_show, extent=[lo_min, lo_max, la_min, la_max], origin="upper")
ax.set_facecolor("black")
ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
ax.set_title("NOAA-20 VIIRS True Color (M5/M4/M3) — contact-02, 2026-06-23\n"
             "CSPP SDR + terrain-corrected geolocation (sub-km)")
ax.grid(True, ls=":", lw=0.3, alpha=0.4, color="white")
fig.tight_layout(); fig.savefig(f"{DIR}/out_truecolor.png", facecolor="black")
print(f"[png] wrote out_truecolor.png  {time.time()-t0:.1f}s", flush=True)
