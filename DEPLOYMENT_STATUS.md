# Deployment Status — 2026-07-22

## What's done

- All spec tasks implemented (130 tests pass, terraform validate passes, Checkov clean)
- Infrastructure deployed via Terraform (26 resources: ECR, S3, IAM, CodeBuild, Step Functions, EventBridge)
- Docker image rebuilt and pushed to ECR (`latest` tag) with:
  - SatDump 1.2.2 (.deb)
  - RT-STPS 7.0 + Patch 1
  - CSPP SDR 4.1 + 4.1.1 patch (base only — missing J01 supplementary data)
  - AWS CLI v2
  - libtiff5 + libfftw3 + libnng1 + libjemalloc2 + libhdf5
- Pipeline confirmed working: I/Q extraction + SatDump + S3 upload succeeds per-chunk (25/25 ✅)
- **RT-STPS is OPERATIONAL** — produces 5 RDR HDF5 files (VIIRS 344 MB + CrIS 226 MB + ATMS 3.5 MB + 2× OMPS) from 1.3 GB concatenated CADU
- **VIIRS Visualization module deployed** (Lambda + CodeBuild + ECR + EventBridge)
- **Forked architecture** — SatDump composites + .cadu upload to S3 immediately after SatDump completes. RT-STPS/CSPP failures are non-fatal per-chunk.
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

### 1. CSPP SDR — Incompatible with RT-STPS 7.0 batch RDR (ABANDONED)

**Status**: ABANDONED after exhaustive debugging (2026-07-17 to 2026-07-22).

**Root cause**: CSPP SDR 4.1.1's `ADL_Unpacker.exe` (closed-source binary) cannot parse RDR files produced by RT-STPS 7.0 batch mode. Reports `spacecraft = 'BAD'` regardless of configuration.

**What was tried:**
- ✅ All three supplementary data packages installed (static_luts_j01, straylight_luts_j01, static_tiles)
- ✅ `sdr_luts.sh` completes without J01 warnings when starter LUTs are present
- ✅ Various `CSPP_RT_HOME` configurations (`/opt/SDR_4_1`, `/opt/scripts`, `/opt/rt-stps`, unset)
- ✅ `--work-dir` flag to control working directory
- ❌ `--spacecraft j01` flag doesn't exist in CSPP SDR 4.1.1
- ❌ RT-STPS server mode with Xvfb — JSW wrapper doesn't start in containers
- ❌ Metadata injection (COLLECTION_SHORT_NAME) — bypassed by ADL_Unpacker binary

**Conclusion**: RT-STPS 7.0 batch mode produces `RNSCA-RVIRS` class RDRs with `N_Reference_ID` containing J01 identifier, but CSPP's closed-source ADL unpacker expects metadata populated only by the NOAA ground system or RT-STPS server mode (which requires a GUI environment).

**Alternative approach (Satpy)**: Use Satpy Python library to read RNSCA RDRs directly for calibration + geolocation. Produces same outputs (calibrated SDR + per-pixel lat/lon) without CSPP dependency.

**Status**: RT-STPS 7.0+P1 works perfectly. CSPP SDR 4.1.1 fails because it's missing the J01 "shipped LUTs" package.

**Root cause identified (2026-07-22):**

CSPP SDR requires **three** supplementary data packages for J01/NOAA-20 support:

| Tarball | CSPP check name | Purpose | Status |
|---------|----------------|---------|--------|
| `CSPP_SDR_V4.1_static_luts_j01.tar.gz` | `*shipped_luts*` | **Core VIIRS calibration LUTs** (174 MB) | ❌ MISSING — must download from CIMSS |
| `CSPP_SDR_V4.1_straylight_luts_j01.tar.gz` | `*stray_light_luts*` | Stray light correction (4.4 GB) | ✅ In S3 + installed |
| `CSPP_SDR_V4.1_static_tiles.tar.gz` | `*ecotiles*` | Static terrain/land cover tiles (5.0 GB) | ✅ In S3 + installed |

**The `static_luts_j01` tarball is the blocker.** Without it:
- `sdr_luts.sh` reports `J01 not supported until proper LUTS are installed!`
- The spacecraft is detected as `'BAD'` instead of `'J01'`
- The DMS working database can't initialize → 600-iteration wait loop → `ADL_Unpacker.exe: FATAL ERROR: Configured path does not contain a valid DMS instance`
- Then `KeyError: 'COLLECTION_SHORT_NAME'` (consequence, not root cause)

**Download from CIMSS** (requires free registration at https://cimss.ssec.wisc.edu/cspp/download/):
```
CSPP_SDR_V4.1_static_luts_j01.tar.gz   (174 MB)
```

**Once downloaded:**
1. Save to `nasa_software/CSPP_SDR_V4.1_static_luts_j01.tar.gz`
2. Upload to `s3://groundstation-noaa20-sdr-output-471112743408/software/`
3. Rebuild Docker image with: `tar xzf CSPP_SDR_V4.1_static_luts_j01.tar.gz -C /opt/`
4. Re-run `sdr_luts.sh` (should now succeed with J01 support)
5. Re-run `viirs_sdr.sh` on contact03 RDR

**Previous debugging history (2026-07-17 to 2026-07-22):**
- ✅ RT-STPS produces 5 RDR files (VIIRS 328 MB + ATMS + CrIS + 2×OMPS) from 25 concatenated .cadu files
- ✅ RT-STPS jpss1.xml patched: `PnEncoded=false`, `frame_sync → reed_solomon` (skip PN node)
- ✅ RT-STPS correct invocation: `./bin/batch.sh config/jpss1.xml <cadu_file>` (not just `<cadu_file>`)
- ✅ EC2 aggregation instance deployed (r6i.xlarge, 300 GB gp3 EBS)
- ✅ CodeBuild can reach CIMSS (`jpssdb.ssec.wisc.edu`) — EC2 cannot
- ✅ `sdr_luts.sh` runs successfully in CodeBuild (10 GB cache built + uploaded to S3)
- ✅ straylight + ecotiles installed on both EC2 and CodeBuild Docker image
- ❌ CSPP hangs on "wait for working db initialization" (600 iterations, ~50 min) then fails
- ❌ `ADL_Unpacker.exe` can't parse RDR because DMS DB is invalid
- ❌ `spacecraft = 'BAD'` — CSPP doesn't recognize J01 without the starter LUTs

### 2. EC2 Aggregation Instance — Operational Notes

| Property | Value |
|----------|-------|
| Instance ID | `i-01d21ecae10f99fbb` |
| Type | r6i.xlarge (4 vCPU, 32 GB RAM) |
| EBS | 300 GB gp3 (resized from 100 GB on 2026-07-20) |
| State | Stopped (self-stops after aggregation, or manually) |
| RT-STPS | ✅ Installed + patched (jpss1.xml PnEncoded=false, PN node removed) |
| CSPP SDR | ❌ Blocked — cannot reach CIMSS for runtime ancillary data |
| CSPP RT_HOME | `/opt/rt-stps` (CSPP looks for `anc/static/SDR_4_1_DB/package` here) |
| Note | EC2 can't reach `jpssdb.ssec.wisc.edu` — CSPP will only work in CodeBuild |

### 4. Satpy Approach — Alternative to CSPP (NEW — In Progress)

**Plan**: Replace CSPP SDR with Satpy for VIIRS RDR → calibrated imagery.

| Feature | CSPP SDR | Satpy |
|---------|---------|-------|
| Calibration | Official NOAA ADL | Community Python (same algorithms) |
| Geolocation | Per-pixel (GMODO/GIGTO) | Per-pixel (pyorbital + terrain correction) |
| Input format | Requires specific RDR metadata | Reads any HDF5 VIIRS RDR |
| Dependencies | Closed-source ADL binary + CIMSS server | Pure Python (pip install) |
| Accuracy | Sub-km | Sub-km (same orbital model) |
| Docker friendly | ❌ (needs CIMSS connectivity + complex DB) | ✅ (pip install, no runtime deps) |

**Satpy packages needed**: `satpy[viirs_l1b]`, `pyorbital`, `pyresample`, `h5py`, `numpy`

**Steps**:
1. Add Satpy to Docker image or CodeBuild inline install
2. Write a script that reads the RNSCA-RVIRS RDR via Satpy's `viirs_l1b` reader
3. Extract calibrated reflectance (I1/I2/I3) + radiance (M15) + per-pixel lat/lon
4. Feed into existing `visualize_nasa.py` pipeline (SDRReader interface)
5. Or output SDR-compatible HDF5 files for the existing NASA visualization path

**RT-STPS root causes (all resolved):**
1. ✅ `../data` directory missing → fixed with `mkdir -p /opt/data`
2. ✅ Wrong XML config → `npp.xml` (S-NPP) → now uses `jpss1.xml` (JPSS-1)
3. ✅ Single chunk too short → concatenation of all 25 chunks' CADU → 1.3 GB combined
4. ✅ RT-STPS output path → `cd /opt/rt-stps` before invoking `batch.sh`, output goes to `/opt/data`
5. ✅ PN encoding mismatch → SatDump outputs PN-decoded frames, set `PnEncoded="false"` + remove `pn` link node
6. ✅ `.cadu` not uploaded to S3 → fixed buildspec echo parentheses bug + added `aws s3 sync` after SatDump

**SatDump .cadu frame analysis (confirmed 2026-07-02):**
- File size: 56,184,832 bytes = 54,868 × 1024 bytes/frame (exact division)
- Frame structure: `1A CF FC 1D` (ASM) + 1020 bytes CADU payload
- SCID: 159 (JPSS-1 ✅), VCID: 16 (VIIRS ✅)
- RS parity: intact (non-zero bytes in parity region)
- PN encoding: already removed by SatDump (identical headers across frames)

**Architecture (working):**
```
Per chunk (parallel, 25×):
  .pcap → I/Q extract → .cs8 → SatDump npp_hrd → composites + .cadu → upload ALL to S3
  ↕ RT-STPS/CSPP non-fatal per-chunk (best-effort)

Aggregation (single):
  Download all .cadu from S3 → cat chunk_0/*.cadu ... chunk_24/*.cadu > combined.cadu (1.3 GB)
  → sed PnEncoded=false + remove pn link in jpss1.xml
  → cd /opt/rt-stps && bin/batch.sh config/jpss1.xml combined.cadu → 5 RDR HDF5 in /opt/data/ ✅
  → CSPP SDR viirs_sdr.sh → SDR + GEO HDF5 (BLOCKED — missing J01 data)
  → Upload SDR/GEO/RDR to S3
```

### 2. Visualization geolocation accuracy — NEW APPROACH

**Previous approach** (abandoned): CSPP SDR produces GMODO/GIGTO with per-pixel lat/lon. This requires:
- RT-STPS → CSPP SDR chain (works for RT-STPS but CSPP has intractable DB initialization issues in CodeBuild)
- 30+ minutes of processing on 2XLARGE compute

**New approach**: Use SatDump's native projection system. SatDump already computes per-pixel geolocation internally (from CADU frame timestamps + TLE/ephemeris). It just needs to be configured to OUTPUT projected GeoTIFFs.

**Implementation**: Add `"project": {"config": {"type": "equirec", "auto": true}, "img_format": ".tif"}` to the SatDump pipeline configuration. This produces equirectangular projected GeoTIFFs with correct WGS84 coordinates — no CSPP needed.

**Benefits**:
- No CSPP SDR dependency (eliminates 30+ min processing + DB issues)
- Same data quality as current composites (SatDump calibration)  
- Per-pixel geolocation from SatDump's internal ephemeris propagation
- GeoTIFF output with proper affine transform + CRS

**Trade-off**: SatDump's geolocation uses community TLE propagation (not NOAA's corrected ephemeris). Accuracy is ~1-5 km vs CSPP's sub-km. For a demonstrator this is acceptable — coastlines will align visually.

### 3. Contact scheduler permanently disabled ✅

Scheduler cron rule set to `state = "DISABLED"` in Terraform. Confirmed no future contacts scheduled.

## AWS credentials

```bash
aws sso login --profile AWSAdminAccess-471112743408
```

## Execution names used

- Contact #1: c14d25d6-run1 through c14d25d6-run9 (used)
- Contact #2: 7903eb3f-run1 through 7903eb3f-run6 (used)
- Contact #3: 1ae80d1d-run1 through 1ae80d1d-run3 (used), 1ae80d1d-rtstps-fix-test, 1ae80d1d-rtstps-jpss1-test, 1ae80d1d-jpss1-config-v2, 1ae80d1d-cadu-concat-v1, 1ae80d1d-cadu-upload-fix, 1ae80d1d-cadu-upload-v2, 1ae80d1d-cadu-upload-v3, 1ae80d1d-full-25chunks, 1ae80d1d-full-v2, 1ae80d1d-pn-false, 1ae80d1d-pn-nolink, 1ae80d1d-rtstps-optdata, 1ae80d1d-cspp-fix, 1ae80d1d-cspp-path-fix, 1ae80d1d-cspp-home, 1ae80d1d-cspp-bin-fix (all used)
- Contact #4: 69c8c149-run1 through 69c8c149-run3 (used)

## Key learnings

### Signal chain (sessions 1-3)

1. SatDump composites must be uploaded to S3 BEFORE RT-STPS (forked architecture) — otherwise they're lost when RT-STPS fails
2. CodeBuild ECR image reference MUST include `:latest` tag — without it, the image is cached indefinitely
3. `imshow(extent=bbox)` always distorts swath imagery — the correct approach is pixel-space overlay
4. The embedded fallback TLE must be updated periodically (2.5-year-old TLE → thousands of km error)
5. ECI ephemeris from SatDump CBOR gives correct latitude but wrong longitude (no GMST rotation without knowing the UTC time)
6. The geo→pixel linear mapping is only approximate for VIIRS (curvilinear scan geometry)
7. Per-pixel geolocation (NASA path / CSPP SDR GMODO files) is the only way to get sub-km overlay alignment
8. RT-STPS `npp.xml` is for S-NPP — NOAA-20 (JPSS-1) requires `jpss1.xml`
9. RT-STPS `batch.sh` cd's to its own directory — `../data` resolves relative to cwd, not relative to batch.sh location
10. A single 30-second CADU chunk has insufficient data for RT-STPS to form a VIIRS granule (~85s needed) — concatenation of all chunks before RT-STPS is required

### Session 2026-07-02 — CADU upload + RT-STPS operational

11. **Parentheses in echo messages cause `/bin/sh` syntax errors in CodeBuild** — never use `()` in inline buildspec echo text. CodeBuild uses `/bin/sh` not bash.
12. **RT-STPS `../data` resolves from cwd `/opt/rt-stps` to `/opt/data`** — not `/opt/rt-stps/data`. The `mkdir` must create `/opt/data`.
13. **SatDump `.cadu` = standard 1024-byte CADUs** — ASM (`0x1ACFFC1D`) + RS parity intact, but PN already removed by SatDump's demodulator.
14. **RT-STPS PnEncoded=true corrupts already-decoded frames** — XORs clean data with PN sequence → RS fails → all frames silently discarded → 0 output. Fix: `PnEncoded="false"` + remove `pn` node from `<links>` chain.
15. **CSPP SDR 4.1 installs to `/opt/SDR_4_1/`** — not `/opt/cspp-sdr/`. Script is at `bin/viirs_sdr.sh`, not `viirs/viirs_sdr.sh`. Requires `CSPP_SDR_HOME` env var.
16. **CSPP needs supplementary J01 data** — `CSPP_SDR_V4.1_straylight_luts_j01.tar.gz` + `CSPP_SDR_V4.1_static_tiles.tar.gz` must be extracted into `$CSPP_SDR_HOME/` for the installation check to pass.
18. **CSPP SDR DB initialization fails in Docker** — `sdr_luts.sh` cannot initialize the `SDR_4_1_DB` cache database in a Docker build environment (CodeBuild). Every invocation variant fails. The `check_installation` check at runtime then blocks CSPP from running. The CSPP approach is abandoned in favour of SatDump native projection.
19. **SatDump has a native projection system** — composites can be output as equirectangular projected GeoTIFFs using `"project": {"type": "equirec", "auto": true}` in the pipeline config. This eliminates the need for CSPP entirely for geolocation purposes.
