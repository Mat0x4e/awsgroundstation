# SatDump: what it already does, and where to look

Most of a day was spent reimplementing SatDump's VIIRS scan model from the
outside before anyone read its source. This page records what upstream
actually provides, with the file paths to check, so the next question about
geolocation starts here rather than in a notebook.

Everything below was verified against **SatDump 1.2.2**, the version the
`sdr-pipeline` image installs from `satdump_1.2.2_ubuntu_22.04_amd64.deb`.
Repository: <https://github.com/SatDump/SatDump>.

## It georeferences its own composites

Add a `project` block to a composite and SatDump reprojects it, writing
`rgb_<name>_projected<img_format>` beside the plain composite:

```json
"project": {
  "config": { "type": "equirec", "auto": true, "scalar_x": 0.01, "scalar_y": -0.01 },
  "img_format": ".tif"
}
```

- Consumed by `src-core/products/processor/image_processor.cpp` — look for
  `if (compo.value().contains("project") && img_products->has_proj_cfg())`.
- **Where composites live moved between versions.** At 1.2.2 they are in
  `satdump_cfg.json` under `viewer.instruments.<instrument>.rgb_composites`;
  on master they moved to `resources/instrument_cfgs/<instrument>.json` under
  `presets`. A SatDump upgrade will silently stop applying our patch —
  [`docker/sdr-pipeline/enable_satdump_projection.py`](../docker/sdr-pipeline/enable_satdump_projection.py)
  fails loudly rather than skipping, which is the intended behaviour.
- **There is no standalone reprojection command.**
  `src-cli/legacy/project/project.cpp.dis` is disabled, so projection only
  happens as part of pipeline processing. Reprojecting an old contact means
  re-running SatDump over its chunks.

### Composite projection does not work at 1.2.2 — parked 2026-09-02

Enabling it segfaults every chunk, and the cause is inside SatDump rather than
in our configuration.

`make_composite_from_product` sets the composite's timestamps to the correlated
intersection of its channels' arrays, and for VIIRS those are **per scan** — 16
values for a 256-row image. `projectImg` passes them through unchanged, and
`viirs_proj.h` rejects any row past the end of the array
(`if (y >= timestamps.size()) return 1`). With GCPs sampled every 100 rows,
almost nothing resolves:

```
Solving TPS equations for 0 GCPs...
(E) Failure solving!
Final Bounds are : 179, 89 - -179, -89     <- fell back to the whole globe
"width": -22375.0, "height": -11125.0      <- negative
Warping size 32000x16000                   <- then SIGSEGV
```

The `interpolate_timestamps: 32` and `interpolate_timestamps_scantime: 0.0555`
settings exist to expand per-scan to per-line, but they do not reach this path.

`enable_satdump_projection.py` therefore ships in the image but **is not run**
by the Dockerfile. Re-enable it when SatDump is next upgraded and re-test on a
single chunk before any fan-out; the script fails loudly if the config layout
has moved, which it did between 1.2.2 and master.

Worth knowing before retrying: `project_channels` projects individual channels
instead of composites, but it draws timestamps from `get_timestamps(chanid)` —
the same per-scan arrays — so it likely fails the same way, and would yield
per-channel rasters rather than composites.

## The VIIRS scan model is published

`resources/projections_settings/jpss1_viirs.json` is exactly what ends up in
`product.cbor` as `projection_cfg` — same fields, same values, same
commented-out alternatives:

| Field | Value | Meaning |
|---|---|---|
| `type` | `viirs_single_line` | one scan line at a time |
| `scan_angle` | 112 | ±56° across track |
| `forced_gcps_x` | 1279, 2015, 3199, 4383, 5119 | aggregation zone boundaries on a 6400 px scan (3:1 near nadir, 2:1, 1:1 at the edges) |
| `roll_offset` / `yaw_offset` | -0.05 / 0.15 | pointing corrections, applied by SatDump and by nothing downstream |
| `interpolate_timestamps` | 32 | lines per scan (I-band; M-band is 16) |
| `interpolate_timestamps_scantime` | 0.0555 | seconds per line |
| `timefilter.scan_time` | 1.786 | seconds per scan |

`plugins/jpss_support/jpss/instruments/viirs/viirs_proj.h` is the model: for
row `iy` it takes `timestamps[iy]`, asks the tracker for the satellite state,
and raytraces to Earth with an Euler pointing whose **roll is the scan angle**.

## Per-scan timestamps are in the product

`product.cbor` → `images[]` holds one entry per band, each with its own
`timestamps` array — for contact #5, 17 values 1.7866 s apart — plus `ifov_y`
lines per scan. Composites are *cropped* relative to those bands
(`needs_correlation: true`), which is why reconstructing row times downstream
never lands exactly.

## It needs a TLE, and its own updater fails here

SatDump resolves satellite positions through a TLE. At startup it tries
CelesTrak over plain http, which does not work from CodeBuild:

```
Loading TLEs from /root/.config/satdump/satdump_tles.txt
0 TLEs loaded!
Failed getting TLEs. Retrying...
Error updating TLEs. Not updated.
```

**Composites decode fine without a TLE**, so this sat in every chunk log for
months with no visible effect. Projection does not: with no TLE every ground
control point resolves to the same place, and the run ends in the same global
bounds and segfault described above. Fixing the TLE is necessary but not
sufficient — with `1 TLEs loaded!` the composite path still resolves 0 GCPs,
for the separate timestamp reason.

`--tle_override <file>` loads exactly that file and **skips the network update
entirely** (`src-core/init.cpp`, `tle_file_override`). That is what
[`scripts/satdump_process.sh`](../scripts/satdump_process.sh) passes: it fetches
a fresh TLE over https and falls back to `scripts/tle/noaa20.tle` baked into
the image, refusing to run with no TLE at all.

## Two CBOR quirks, in the path that actually runs

`scripts/viirs/scan_geometry.py` reconstructs the model from outside. It was
written as a fallback and is, while SatDump's own projection is parked, the
active geolocation path — measured at 90.5% land/sea agreement (MCC 0.738)
against Natural Earth coastlines. Two things it had to discover, documented
nowhere upstream:

- **Ephemeris timestamps are 2³² seconds low** — they read as year 1890. Add
  4294967296 and they match the acquisition time in `dataset.json`.
- **The wrap leaks into the frame.** SatDump rotated those vectors with its own
  wrapped clock, so undoing the rotation needs the *raw* timestamp while
  everything time-like needs the unwrapped one. Using the true stamp puts the
  pass 133.86° away. Done correctly, the ephemeris agrees with an independent
  SGP4 propagation of NOAA-20 to 0.8 km.
