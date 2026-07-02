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

### 1. RT-STPS — CADU concatenation needed (critical for NASA path)

**Status**: Root causes identified, partially fixed. One remaining issue.

**Root causes found and fixed:**
1. ✅ `../data` directory missing → RT-STPS npp.xml writes to `../data` relative to its cwd. Fixed by adding `mkdir -p /opt/rt-stps/data` in buildspec.
2. ✅ Wrong XML config → `npp.xml` (S-NPP) was used but NOAA-20 is JPSS-1. Fixed: now uses `jpss1.xml`.
3. ✅ Single chunk too short → A 30-second chunk doesn't produce a complete VIIRS granule (~85s needed). Fix: concatenate all chunks' CADU files into one stream before feeding to RT-STPS. Implemented in the aggregation buildspec.
4. ✅ RT-STPS output path → `batch.sh` cd's to its own dir (`/opt/rt-stps/`), so `../data` resolves to `/opt/rt-stps/data/` (not relative to caller cwd). Fixed in aggregation buildspec.

**Remaining issue:**
- ❌ **CSPP SDR fails at installation check** — the Docker image is missing JPSS-1 support data:
  - `shipped_luts*.tar.gz` (lookup tables for VIIRS calibration)
  - `ecotiles*.tar.gz` (terrain data for geolocation)
  - `stray_light_luts*.tar.gz` (stray light correction)
  - Static ancillary database at `{CSPP_SDR_HOME}/anc/static/SDR_4_1_DB/package`
  
  These are separate downloads from CIMSS that need to be added to the Dockerfile.

**RT-STPS is now WORKING ✅** — produces 5 RDR HDF5 files (344 MB VIIRS + CrIS + ATMS + OMPS) from 1.3 GB concatenated CADU.

**Fix applied:** `PnEncoded="false"` + removed `pn` node from link chain via runtime `sed` in buildspec.

**Next step to complete the full chain:**
1. Download CSPP J01 support tarballs from CIMSS: https://cimss.ssec.wisc.edu/cspp/jpss_sdr_v4.1.1.shtml
2. Add them to the Docker build context and extract in Dockerfile
3. Set `CSPP_SDR_HOME=/opt/SDR_4_1` in the Dockerfile ENV
4. Rebuild + push Docker image
5. Re-run aggregation — should produce SDR + GEO HDF5 (per-pixel lat/lon)
6. Then the VIIRS visualization NASA path will have sub-km geolocation accuracy

**Architecture (target):**
```
Per chunk (parallel, 25×):
  .pcap → I/Q extract → .cs8 → SatDump npp_hrd → composites + .cadu → upload ALL to S3

Aggregation (single):
  Download all .cadu from S3 → cat chunk_0/*.cadu chunk_1/*.cadu ... > combined.cadu
  → RT-STPS (jpss1.xml) combined.cadu → RDR HDF5 in /opt/rt-stps/data/
  → CSPP SDR → SDR + GEO HDF5 (per-pixel lat/lon!)
  → Upload SDR/GEO/RDR to S3
```

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
- Contact #3: 1ae80d1d-run1 through 1ae80d1d-run3 (used), 1ae80d1d-rtstps-fix-test, 1ae80d1d-rtstps-jpss1-test, 1ae80d1d-jpss1-config-v2, 1ae80d1d-cadu-concat-v1 (used)
- Contact #4: 69c8c149-run1 through 69c8c149-run3 (used)

## Key learnings from this session

1. SatDump composites must be uploaded to S3 BEFORE RT-STPS (forked architecture) — otherwise they're lost when RT-STPS fails
2. CodeBuild ECR image reference MUST include `:latest` tag — without it, the image is cached indefinitely
3. `imshow(extent=bbox)` always distorts swath imagery — the correct approach is pixel-space overlay
4. The embedded fallback TLE must be updated periodically (2.5-year-old TLE → thousands of km error)
5. ECI ephemeris from SatDump CBOR gives correct latitude but wrong longitude (no GMST rotation without knowing the UTC time)
6. The geo→pixel linear mapping is only approximate for VIIRS (curvilinear scan geometry)
7. Per-pixel geolocation (NASA path / CSPP SDR GMODO files) is the only way to get sub-km overlay alignment
8. RT-STPS `npp.xml` is for S-NPP — NOAA-20 (JPSS-1) requires `jpss1.xml`
9. RT-STPS `batch.sh` cd's to its own directory — `../data` resolves to `/opt/rt-stps/data/`, not caller cwd
10. A single 30-second CADU chunk has insufficient data for RT-STPS to form a VIIRS granule (~85s needed) — concatenation of all chunks before RT-STPS is required
11. SatDump `.cadu` output location needs verification — the file may not be where `aws s3 sync` expects it
12. Parentheses in echo messages cause `/bin/sh` syntax errors in CodeBuild — never use `()` in inline buildspec echo text
13. RT-STPS resolves `../data` relative to its cwd — when cwd is `/opt/rt-stps`, output goes to `/opt/data` (not `/opt/rt-stps/data`)
14. SatDump `.cadu` files ARE standard 1024-byte CADUs with ASM (0x1ACFFC1D) + RS parity intact, but PN already removed
15. RT-STPS with PnEncoded=true corrupts already-decoded frames — must use PnEncoded=false + remove pn node from link chain
16. CSPP SDR 4.1 installs to /opt/SDR_4_1/ (not /opt/cspp-sdr/), viirs_sdr.sh is in bin/ not viirs/
17. CSPP requires CSPP_SDR_HOME env var + J01 support tarballs (shipped_luts, ecotiles, stray_light_luts) + static ancillary DB
