# Implementation Plan: NOAA-20 VIIRS Visualization Pipeline

## Overview

Pipeline de post-traitement serverless transformant les sorties VIIRS en images géoréférencées PNG (Cartopy) et GeoTIFF (EPSG:4326) avec métadonnées JSON. L'implémentation est découpée en trois phases : infrastructure Terraform, chemin SatDump (prioritaire), puis chemin NASA (futur). Les composants partagés (Cartopy_Renderer, Metadata_Generator, GeoTIFF_Exporter) sont implémentés avec le chemin SatDump puis réutilisés par le chemin NASA.

## Tasks

- [x] 1. Set up Terraform module structure and IAM roles
  - [x] 1.1 Create `modules/viirs_visualization/` directory structure with `main.tf`, `codebuild.tf`, `eventbridge.tf`, `iam.tf`, `variables.tf`, `outputs.tf`, `ecr.tf`
    - Define module variables: `project_name`, `account_id`, `sdr_output_bucket_name`, `sdr_output_bucket_arn`, `kms_key_arn`, `kms_key_id`, `sns_topic_arn`, `enable_geotiff`, `tags`
    - Define outputs: Lambda ARN, CodeBuild project name, ECR repository URL
    - _Requirements: 11.3, 11.4_

  - [x] 1.2 Implement IAM roles in `iam.tf` — Lambda execution role and CodeBuild service role
    - Lambda role: `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, `codebuild:StartBuild`, `s3:ListBucket`, `s3:GetObject` on SDR output bucket, `kms:Decrypt`
    - CodeBuild role: `s3:GetObject`, `s3:PutObject` on SDR output bucket, `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey`, `logs:*`, `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`
    - _Requirements: 11.3, 11.4_

  - [x] 1.3 Implement EventBridge rule in `eventbridge.tf` — trigger on ObjectCreated (manifest.json or composites PNG) in SDR output bucket
    - EventBridge rule captures `s3:ObjectCreated:*` events matching relevant prefixes
    - Target: Lambda orchestratrice with input transformer extracting S3 key
    - _Requirements: 11.1, 11.2_

  - [x] 1.4 Implement CodeBuild project in `codebuild.tf` — single project with dynamic buildspec selection
    - Compute type: `BUILD_GENERAL1_MEDIUM` (4 vCPU, 7 GB RAM)
    - Custom Docker image from ECR
    - Timeout: 15 minutes
    - Environment variables: `INPUT_BUCKET`, `KMS_KEY_ID`, `ENABLE_GEOTIFF`, `ENABLE_DESTRIPE`, `TLE_URL`, `TLE_FALLBACK`
    - _Requirements: 11.4_

  - [x] 1.5 Implement ECR repository in `ecr.tf` for the visualization Docker image
    - Lifecycle policy: keep last 5 images
    - Encryption with KMS CMK
    - _Requirements: 11.4_

  - [x] 1.6 Implement Lambda function resource in `main.tf` and CloudWatch Log Groups (90-day retention)
    - Lambda: Python 3.12, 512 MB, 60s timeout
    - Log groups for Lambda and CodeBuild
    - _Requirements: 11.3, 11.5_

  - [x] 1.7 Integrate module in root `main.tf` — conditional on `var.enable_sdr_pipeline`
    - Wire module inputs from `module.sdr_pipeline` and `module.security` outputs
    - _Requirements: 11.3_

- [x] 2. Checkpoint — Validate Terraform module
  - Ensure `terraform validate` passes and Checkov has no HIGH/CRITICAL findings, ask the user if questions arise.

- [x] 3. Implement Lambda orchestratrice (path detection + CodeBuild invocation)
  - [x] 3.1 Create `lambdas/viirs_visualizer/handler.py` with `VisualizationOrchestrator` class
    - EventBridge event handler: extract contact_id + contact_date from S3 key
    - List objects in contact folder via S3 ListObjectsV2
    - Detect path: check for `viirs_rgb_*.png` / `viirs_*_Thermal_IR_*.png` (→ satdump) or `SVI0*_npp_*.h5` / `SVOM15_npp_*.h5` (→ nasa). SatDump priority if both present.
    - Submit CodeBuild with `start_build()` passing env var overrides (`INPUT_PREFIX`, `CONTACT_ID`, `CONTACT_DATE`, `VIZ_PATH`)
    - Return `{ "build_id": str, "path": str, "contact_id": str }`
    - Log warning + skip gracefully if neither path detected
    - On failure: log to CloudWatch with context (input files, path selected, error message)
    - _Requirements: 11.1, 11.2, 11.3, 11.5_

  - [ ]* 3.2 Write property test for path detection (Property 9)
    - **Property 9: Path detection correctness**
    - For any set of S3 keys with SatDump patterns → returns "satdump"; with NASA patterns only → returns "nasa"; SatDump takes priority when both present
    - **Validates: Requirements 11.1, 11.2**

  - [ ]* 3.3 Write unit tests for Lambda orchestrator
    - Test path detection with mixed file sets
    - Test error case: neither path detected
    - Test CodeBuild submission with correct env vars
    - _Requirements: 11.1, 11.2, 11.3_

- [x] 4. Implement SatDump Visualizer — composite discovery and normalization
  - [x] 4.1 Create `scripts/viirs/satdump_visualizer.py` with `SatDumpVisualizer` class
    - `SUPPORTED_COMPOSITES` dict mapping composite type names to file patterns
    - `discover_composites(folder)`: scan for recognized PNG patterns (`viirs_rgb_*.png`, `viirs_*_Thermal_IR_*.png`), raise `NoCompositesError` with file listing if none found
    - `load_and_normalize(composite)`: load via Pillow, detect mode (I;16 → /65535, RGB/L → /255), mask pixels < 1e-6 as NaN
    - Return float32 array shape (H, W) or (H, W, 3)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [ ]* 4.2 Write property test for normalization (Property 1)
    - **Property 1: Normalization produces valid range and correct masking**
    - For any PNG array (8-bit or 16-bit), normalized values SHALL be in [0, 1], and pixels < 1e-6 SHALL be masked as NaN
    - **Validates: Requirements 1.3, 1.4, 1.5**

  - [ ]* 4.3 Write unit tests for composite discovery
    - Test with known folder structures → expected composites found
    - Test with empty folder → NoCompositesError raised with file listing
    - _Requirements: 1.1, 1.2, 1.6_

- [x] 5. Implement CBOR Reader and BBox Calculator
  - [x] 5.1 Create `scripts/viirs/cbor_reader.py` with `CBORReader` class
    - Search folder and subdirs for `product.cbor`
    - Extract timestamp (`timestamp` or `start_timestamp` → datetime UTC)
    - Extract satellite (`satellite` or `sat_name`, default "NOAA-20")
    - Extract projection coordinates (`projection` or `geo_correction`)
    - Fallback to defaults if file missing or cbor2 parse failure (log warning)
    - Return `CBORMetadata` dataclass
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 5.2 Create `scripts/viirs/bbox_calculator.py` with `BBoxCalculator` class
    - Priority order: .georef file → CBOR projection → TLE/sgp4
    - `_from_georef(path)`: read corner coordinates from JSON
    - `_from_cbor_projection(coords)`: extract bbox from CBOR projection metadata
    - `_from_tle(timestamp)`: fetch TLE from CelesTrak (fallback: embedded TLE), propagate orbit via sgp4, extend nadir by ±56° cross-track angle
    - Produce `BoundingBox(lat_min, lat_max, lon_min, lon_max)` in WGS84 degrees
    - Raise `NoBBoxSourceError` if no source available
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ]* 5.3 Write property test for CBOR metadata round-trip (Property 2)
    - **Property 2: CBOR metadata round-trip**
    - For any valid CBOR dict, serializing then reading SHALL produce identical values
    - **Validates: Requirements 2.1, 2.2, 2.3**

  - [ ]* 5.4 Write property tests for BBox Calculator (Properties 3 & 4)
    - **Property 3: Bounding box validity invariant** — lat in [-90, 90], lon in [-180, 180], min ≤ max
    - **Property 4: Swath extension containment** — swath bbox ⊇ nadir bbox
    - **Validates: Requirements 3.1, 3.2, 3.6**

- [x] 6. Implement Cartopy Renderer (shared component)
  - [x] 6.1 Create `scripts/viirs/cartopy_renderer.py` with `CartopyRenderer` class
    - Constants: DPI=300, coastline 10m white 0.8px, borders yellow dashed 0.6px, lakes alpha 0.2, grid alpha 0.6, POI bg alpha 0.45, margin 1.5°
    - `_auto_grid_step(bbox)`: 10° if span>40°, 5° if span>20°, 2° otherwise
    - `_add_overlays(ax, bbox)`: coastlines, borders, lakes, grid
    - `_add_poi_labels(ax, bbox)`: white text on semi-transparent black background for visible POI
    - `render_satdump(data, bbox, composite_type, metadata, output_path)`:
      - PlateCarree centered on bbox + 1.5° margin
      - Title: composite_type + datetime UTC + satellite + "Calibration communautaire SatDump — non certifiée NOAA"
      - Thermal IR → RdYlBu_r colormap + colorbar "Valeur normalisée SatDump"
      - True/False Color → RGB with bilinear interpolation
    - `render_nasa(data, lat, lon, mode, metadata, output_path)`:
      - pcolormesh with per-pixel lat/lon
      - Title: mode + datetime UTC + Orbit number + granule_id
      - Thermal → dual-axis colorbar (K and °C), range 210–305K
      - True Color → RGB composite
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [ ]* 6.2 Write unit tests for Cartopy Renderer
    - Verify output file exists at expected path
    - Verify DPI is 300
    - Verify title contains expected composite_type and calibration note
    - _Requirements: 4.5, 4.6_

- [x] 7. Implement Metadata Generator and GeoTIFF Exporter (shared components)
  - [x] 7.1 Create `scripts/viirs/metadata_generator.py` with `MetadataGenerator` class
    - `generate_satdump(composite_type, satellite, datetime_utc, bbox, output_png)`:
      - Fields: source="SatDump", satellite, datetime_utc, composite_type, bounding_box, calibration_note="Communautaire SatDump — non certifiée NOAA", visualization_path="satdump", output_file
    - `generate_nasa(granule_id, orbit_number, datetime_utc, mode, bbox, swath_width_km, output_png)`:
      - Fields: granule_id, orbit_number, datetime_utc, mode, bounding_box, swath_width_km, visualization_path="nasa", output_file
    - `extract_nasa_metadata(sdr_h5_path)`: read N_Granule_ID, N_Beginning_Orbit_Number, N_Beginning_Time_IET from HDF5 global attributes; convert IET (µs since 1958-01-01) to ISO 8601 UTC; fallback to defaults if missing
    - JSON file named with same base name as PNG + `.json`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 10.1, 10.2, 10.3, 10.4_

  - [x] 7.2 Create `scripts/viirs/geotiff_exporter.py` with `GeoTIFFExporter` class
    - `export_satdump(data, bbox, output_path)`: CRS EPSG:4326, affine from bbox + image dims, RGB (3 bands) or single band float32
    - `export_nasa(data, lat, lon, output_path)`: scipy.interpolate.griddata (linear), resolution ~0.0067°, CRS EPSG:4326, affine from grid bounds
    - File naming: `viirs_{composite_type}_{identifier}.tif`
    - Controlled by `ENABLE_GEOTIFF` env var
    - _Requirements: 12.1, 12.2, 12.3_

  - [ ]* 7.3 Write property tests for Metadata Generator (Properties 10, 11, 12)
    - **Property 10: Metadata JSON completeness** — all required fields present with non-empty values and correct visualization_path
    - **Property 11: Output file naming pattern consistency** — PNG matches `viirs_{path}_{type}_{id}.png`, JSON `.json`, GeoTIFF `.tif`
    - **Property 12: IET timestamp conversion round-trip** — IET µs → ISO 8601 → back within 1µs
    - **Validates: Requirements 5.1, 10.1, 10.3, 13.2, 13.3, 13.4**

  - [ ]* 7.4 Write unit tests for GeoTIFF Exporter
    - Verify output CRS is EPSG:4326 via rasterio
    - Verify band count matches input (3 for RGB, 1 for thermal)
    - Verify transform is consistent with bbox
    - _Requirements: 12.1, 12.2_

- [x] 8. Implement SatDump end-to-end pipeline script
  - [x] 8.1 Create `scripts/viirs/visualize_satdump.py` — main entry point for CodeBuild SatDump path
    - CLI args: `--input-dir`, `--coordinates-dir`, `--output-dir`, `--contact-id`, `--contact-date`, `--enable-geotiff`
    - Orchestrate: CBOR_Reader → BBox_Calculator → SatDump_Visualizer (discover + normalize) → Cartopy_Renderer → Metadata_Generator → GeoTIFF_Exporter (optional)
    - Output organization: `viirs_satdump_{composite_type}_{contact_id}.png`, `.json`, `.tif`
    - Process all discovered composites (True Color, Thermal IR, False Color, etc.)
    - Graceful error handling per composite (skip on error, continue with others)
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 5.1, 12.1, 13.1, 13.2, 13.3, 13.4, 13.5_

  - [ ]* 8.2 Write integration test for SatDump end-to-end
    - Provide sample composite PNG + product.cbor
    - Verify output PNG exists, JSON is valid, GeoTIFF has correct CRS
    - _Requirements: 1.1, 4.1, 5.1, 12.1, 13.2_

- [x] 9. Checkpoint — Validate SatDump path end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Implement Docker image and buildspecs
  - [x] 10.1 Create `docker/viirs-visualization/Dockerfile` with Python 3.12 + scientific stack
    - Base: python:3.12-slim
    - System deps: libgeos-dev, libproj-dev, proj-data, libgdal-dev, gdal-bin
    - Python deps: cartopy==0.23.0, matplotlib==3.9.0, numpy==1.26.4, Pillow==10.3.0, cbor2==5.6.0, sgp4==2.23, h5py==3.11.0, rasterio==1.3.10, scipy==1.13.0, boto3==1.34.0
    - Pre-download Natural Earth data (10m coastline + admin_0_boundary_lines_land)
    - Copy scripts/ to /opt/scripts/, set `MPLBACKEND=Agg`
    - _Requirements: 11.4_

  - [x] 10.2 Create `buildspecs/viirs_satdump.yml` — SatDump visualization buildspec
    - pre_build: download SatDump outputs (PNG + CBOR + .georef) + coordinates from S3
    - build: run `visualize_satdump.py` with CLI args from env vars
    - post_build: upload products to `s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/` with KMS SSE
    - _Requirements: 11.4, 13.1_

  - [x] 10.3 Create `buildspecs/viirs_nasa.yml` — NASA visualization buildspec
    - pre_build: download SDR + GEO HDF5 files from S3
    - build: run `visualize_nasa.py` with CLI args
    - post_build: upload products to S3 with KMS SSE
    - _Requirements: 11.4, 13.1_

- [ ] 11. Implement NASA path — SDR Reader, GEO Reader, BT Converter, Image Renderer
  - [x] 11.1 Create `scripts/viirs/sdr_reader.py` with `SDRReader` class
    - `read_reflectance(h5_path)`: open HDF5, find SDR group, extract Reflectance, apply linear calibration (value × scale + offset) → [0, 1], mask fill value 65535
    - `read_radiance(h5_path)`: extract Radiance, apply calibration → mW·m⁻²·sr⁻¹·µm⁻¹, mask fill value 65535
    - Raise `InvalidSDRFileError` if no SDR group found
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 11.2 Create `scripts/viirs/geo_reader.py` with `GEOReader` class
    - `read_iband(h5_path)`: extract Lat/Lon from `VIIRS-IMG-GEO_All` group, float32, mask values < -900
    - `read_mband(h5_path)`: extract Lat/Lon from `VIIRS-MOD-GEO_All` group, float32, mask values < -900
    - Produce arrays of same dimension as corresponding SDR data
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [x] 11.3 Create `scripts/viirs/bt_converter.py` with `BTConverter` class
    - Constants: C1=1.191042e8, C2=1.4387752e4, M15_WAVELENGTH=10.7630 µm
    - `convert(radiance, wavelength)`: apply inverse Planck law `BT = C2 / (λ × ln(C1 / (λ⁵ × L) + 1))`
    - Propagate input mask to output
    - Return brightness temperature in Kelvin (float32 masked array)
    - _Requirements: 8.1, 8.2, 8.3_

  - [x] 11.4 Create `scripts/viirs/image_renderer.py` with `ImageRenderer` class
    - `contrast_stretch(band)`: clip to [p2, p98] percentile, scale to [0, 1]
    - `gamma_correct(band, gamma=0.5)`: apply γ correction
    - `destripe(band)`: subtract per-detector column-wise median (16 detectors, cyclic period 16 lines)
    - `assemble_true_color(i1, i2, i3)`: per-band contrast_stretch + gamma_correct → RGB (H, W, 3)
    - _Requirements: 8.4, 8.5, 9.7_

  - [ ]* 11.5 Write property tests for NASA components (Properties 5, 6, 7, 8)
    - **Property 5: SDR linear calibration correctness and fill masking** — calibrated = raw × scale + offset for non-fill; fill → masked
    - **Property 6: Inverse Planck monotonicity and mask propagation** — L1 < L2 → BT(L1) < BT(L2); mask positions identical
    - **Property 7: Image processing output range invariant** — contrast_stretch + gamma_correct → all values in [0, 1]
    - **Property 8: Destriping eliminates detector bias** — per-detector median ≈ 0 after destriping
    - **Validates: Requirements 6.1, 6.2, 6.3, 8.1, 8.2, 8.3, 8.4, 8.5**

  - [ ]* 11.6 Write unit tests for SDR Reader and GEO Reader
    - Test invalid HDF5 (no SDR group) → InvalidSDRFileError
    - Test correct shape and dtype of outputs
    - Test fill value masking (65535 for SDR, < -900 for GEO)
    - _Requirements: 6.3, 6.4, 7.3_

- [ ] 12. Implement NASA end-to-end pipeline script
  - [x] 12.1 Create `scripts/viirs/visualize_nasa.py` — main entry point for CodeBuild NASA path
    - CLI args: `--input-dir`, `--output-dir`, `--contact-id`, `--contact-date`, `--enable-geotiff`, `--enable-destripe`
    - Orchestrate: SDR_Reader → GEO_Reader → BT_Converter (for M15) → Image_Renderer (True Color from I1/I2/I3) → Cartopy_Renderer → Metadata_Generator → GeoTIFF_Exporter (optional)
    - Output organization: `viirs_nasa_{mode}_{contact_id}.png`, `.json`, `.tif`
    - Extract NASA metadata from HDF5 attributes (granule_id, orbit_number, datetime)
    - _Requirements: 6.1, 7.1, 8.1, 8.4, 9.1, 10.1, 10.2, 10.3, 12.2, 13.1, 13.2, 13.3_

  - [ ]* 12.2 Write integration test for NASA end-to-end
    - Verify pipeline handles sample HDF5 input → produces PNG + JSON + GeoTIFF
    - Verify metadata JSON contains correct visualization_path="nasa"
    - _Requirements: 10.1, 13.2, 13.5_

- [x] 13. Implement shared data models and utilities
  - [x] 13.1 Create `scripts/viirs/models.py` with shared data classes
    - `BoundingBox` dataclass: lat_min, lat_max, lon_min, lon_max + methods (span_lat, span_lon, center, with_margin, to_dict)
    - `CBORMetadata` dataclass: timestamp, satellite, projection_coords, raw_data
    - `NASAMetadata` dataclass: granule_id, orbit_number, datetime_utc + `from_iet()` classmethod (IET_EPOCH = 1958-01-01)
    - `CompositeInfo` dataclass: path, composite_type, bit_depth
    - _Requirements: 3.6, 5.1, 10.1_

  - [x] 13.2 Create `scripts/viirs/__init__.py` — package init exposing public API
    - _Requirements: 11.4_

- [ ] 14. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The SatDump path (tasks 3–9) is the implementation priority as it's operational immediately
- The NASA path (tasks 11–12) can be deferred until RT-STPS is corrected
- Task 13 (shared models) should be implemented early — it's referenced by tasks 4–12 and placed last only to avoid circular deps in the plan (implement alongside first usage)
- All Python scripts live under `scripts/viirs/` and are copied into the Docker image at `/opt/scripts/`
- Property tests use Hypothesis with `@settings(max_examples=100)`
- Infrastructure uses the existing `module.sdr_pipeline` output bucket for both input and output (under `products/` prefix)
- The `ENABLE_GEOTIFF` environment variable controls optional GeoTIFF export without code changes

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "13.1", "13.2"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5"] },
    { "id": 2, "tasks": ["1.6", "1.7"] },
    { "id": 3, "tasks": ["3.1", "5.1", "5.2", "4.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "4.2", "4.3", "5.3", "5.4"] },
    { "id": 5, "tasks": ["6.1", "7.1", "7.2"] },
    { "id": 6, "tasks": ["6.2", "7.3", "7.4"] },
    { "id": 7, "tasks": ["8.1"] },
    { "id": 8, "tasks": ["8.2", "10.1", "10.2"] },
    { "id": 9, "tasks": ["11.1", "11.2", "11.3", "11.4"] },
    { "id": 10, "tasks": ["11.5", "11.6", "10.3"] },
    { "id": 11, "tasks": ["12.1"] },
    { "id": 12, "tasks": ["12.2"] }
  ]
}
```
