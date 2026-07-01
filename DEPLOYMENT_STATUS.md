# Deployment Status — 2026-07-01

## What's done

- All spec tasks implemented (130 tests pass, terraform validate passes, Checkov clean)
- Infrastructure deployed via Terraform (26 resources: ECR, S3, IAM, CodeBuild, Step Functions, EventBridge)
- Docker image rebuilt and pushed to ECR (`latest` tag) with:
  - SatDump 1.2.2 (.deb)
  - RT-STPS 7.0 + Patch 1
  - CSPP SDR 4.1 + 4.1.1 patch
  - AWS CLI v2
  - libtiff5 + libfftw3 + libnng1 + libjemalloc2 + libhdf5
- Pipeline confirmed working: I/Q extraction + SatDump + S3 upload succeeds per-chunk
- **VIIRS Visualization module deployed** (Lambda + CodeBuild + ECR + EventBridge)
- **Forked architecture** — SatDump composites upload to S3 immediately after SatDump completes, before RT-STPS (which still fails). RT-STPS/CSPP failures are non-fatal.
- **Contact scheduler DISABLED** in Terraform (`state = "DISABLED"`) to prevent unintended contacts
- Contacts #3 (Oregon) and #4 (Stockholm) processed — composites in S3

## VIIRS Visualization Pipeline Status

| Component | Status |
|-----------|--------|
| Docker image (`groundstation-noaa20-viirs-visualization`) | ✅ Built (Cartopy + matplotlib + numpy + Pillow + cbor2 + sgp4 + h5py + rasterio + scipy + boto3 + AWS CLI) |
| Lambda orchestrator | ✅ Deployed (path detection + CodeBuild submission) |
| CodeBuild visualization project | ✅ Working (pixel-space rendering, native resolution) |
| CodeBuild Docker build project | ✅ Working (builds from GitHub, pushes to ECR) |
| Composite rendering | ✅ Working — True Color, Thermal IR, Day Microphysics at 3200×272 native |
| Cartographic overlay | ⚠️ Imprecise alignment (~100-300 km offset) — TLE-only without CPM |
| GeoTIFF export | ✅ Working |

### Known limitation — Geolocation accuracy (SatDump path)

The SatDump path produces composites without per-pixel geolocation. The overlay (coastlines, borders, POI) uses TLE+sgp4 propagation to estimate the geographic extent, but:

1. **Timing sensitivity** — a 5-second error in the contact start time → ~40 km nadir shift
2. **No CPM correction** — SatDump's community calibration doesn't include terrain correction
3. **Curvilinear swath** — the linear geo→pixel mapping assumes PlateCarree but VIIRS scans in a bowtie pattern

**Impact**: The overlay is offset by 100-300 km from the actual terrain. Coastlines don't align visually.

**Fix**: The NASA path (CSPP SDR) produces GMODO/GIGTO HDF5 files with per-pixel lat/lon corrected by NOAA. Once RT-STPS is fixed, the NASA visualization path (`pcolormesh` with per-pixel coordinates) will have sub-km accuracy.

## What needs fixing (prioritized)

### 1. RT-STPS exit code 254 (critical for NASA path)

RT-STPS 7.0+P1 fails with exit code 254 on all chunks from contacts #3 and #4. Same issue as contacts #1 and #2. Without RT-STPS → no RDR → no CSPP SDR → no per-pixel geolocation.

Potential causes:
- CADU frame format mismatch (SatDump 1.2.2 output vs RT-STPS 7.0 expectations)
- Missing ancillary data (leap seconds file, platform config)
- Java heap space (unlikely — 16 GB available)

### 2. Visualization geolocation accuracy (quality of life)

Current TLE approach gives ~100-300 km offset. Two improvement options:
- **Quick fix**: add `--bbox LAT_MIN LAT_MAX LON_MIN LON_MAX` CLI override for manual calibration per contact
- **Proper fix**: decode SatDump CBOR `projection_cfg` timestamps (J2000-based?) and apply GMST rotation for correct ECI→geographic conversion

### 3. Contact scheduler permanently disabled ✅

Scheduler cron rule set to `state = "DISABLED"` in Terraform. Confirmed no future contacts scheduled.

## AWS credentials

```bash
aws sso login --profile AWSAdminAccess-471112743408
```

## Execution names used

- Contact #1: c14d25d6-run1 through c14d25d6-run9 (used)
- Contact #2: 7903eb3f-run1 through 7903eb3f-run6 (used)
- Contact #3: 1ae80d1d-run1 through 1ae80d1d-run3 (used)
- Contact #4: 69c8c149-run1 through 69c8c149-run3 (used)

## Key learnings from this session

1. SatDump composites must be uploaded to S3 BEFORE RT-STPS (forked architecture) — otherwise they're lost when RT-STPS fails
2. CodeBuild ECR image reference MUST include `:latest` tag — without it, the image is cached indefinitely
3. `imshow(extent=bbox)` always distorts swath imagery — the correct approach is pixel-space overlay
4. The embedded fallback TLE must be updated periodically (2.5-year-old TLE → thousands of km error)
5. ECI ephemeris from SatDump CBOR gives correct latitude but wrong longitude (no GMST rotation without knowing the UTC time)
6. The geo→pixel linear mapping is only approximate for VIIRS (curvilinear scan geometry)
7. Per-pixel geolocation (NASA path / CSPP SDR GMODO files) is the only way to get sub-km overlay alignment
