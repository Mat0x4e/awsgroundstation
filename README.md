# NOAA-20 Ground Station — DigIF to VIIRS Imagery

Automated satellite data processing pipeline using AWS Ground Station, converting raw DigIF radio signals into calibrated VIIRS imagery (SDR + GEO products).

## Architecture

```
AWS Ground Station (Hawaii/Ohio)
        │ X-band downlink
        ▼
S3 Data Delivery (.pcap VITA-49 DigIF, ~40 GB per 10-min contact)
        │ EventBridge trigger
        ▼
Step Functions → N × CodeBuild (parallel per chunk)
        │
        ├── I/Q Extraction (Python) ─── .cs8
        ├── SatDump npp_hrd ─────────── .cadu
        ├── RT-STPS 7.0 ────────────── RDR HDF5
        └── CSPP SDR 4.1.1 ─────────── SDR + GEO HDF5 Level 1
        │
        ▼
VIIRS Visualization (georeferenced PNG/GeoTIFF)
```

## Project structure

```
├── main.tf / variables.tf / outputs.tf    Terraform root (eu-central-1)
├── modules/
│   ├── contact_scheduler/                 Lambda: schedule satellite contacts
│   ├── mission_profile/                   Ground Station mission profile config
│   ├── observability/                     CloudWatch dashboard + alarms
│   ├── s3_delivery/                       S3 bucket for Ground Station data delivery
│   ├── security/                          KMS, IAM, SNS, CloudTrail
│   └── sdr_pipeline/                      Full SDR pipeline (Step Functions + CodeBuild + ECR + S3)
├── lambdas/
│   ├── contact_scheduler/                 Contact scheduler Lambda source
│   └── data_processor/                    Data processor Lambda source
├── docker/sdr-pipeline/                   Docker image (SatDump + RT-STPS + CSPP SDR)
├── buildspecs/                            CodeBuild buildspec files
├── scripts/                               Pipeline Python scripts (IQ extract, geolocation, manifest, metrics)
├── postprocessing/                        VIIRS visualization scripts (final output)
├── processing/                            Legacy manual CodeBuild experiments
├── tests/                                 Python tests (unit + property-based + integration)
├── nasa_software/                         Licensed tools — not in git (see setup below)
├── output/                                Local satellite imagery downloads — not in git
└── CONTACTS.md                            Operational log of satellite contacts
```

## Prerequisites

- Terraform >= 1.5
- AWS CLI v2 with SSO access to account `471112743408`
- Python 3.12+ (for scripts and tests)

### Licensed software (not in git)

Place in `nasa_software/` before building the Docker image:

| File | Source | Size |
|------|--------|------|
| `satdump_1.2.2_ubuntu_22.04_amd64.deb` | [SatDump releases](https://github.com/SatDump/SatDump/releases) | 35 MB |
| `RT-STPS_7.0.tar.gz` + `RT-STPS_7.0_PATCH_1.tar.gz` | [CIMSS download portal](https://cimss.ssec.wisc.edu/cspp/download/) | 50 MB |
| `CSPP_SDR_V4.1.tar.gz` + `CSPP_SDR_V4.1.1_patch.tar.gz` | [CIMSS download portal](https://cimss.ssec.wisc.edu/cspp/download/) | 845 MB |

## Quick start

```bash
# 1. Login to AWS
aws sso login --profile AWSAdminAccess-471112743408

# 2. Deploy infrastructure
terraform init
terraform plan -out=plan.tfplan
terraform apply plan.tfplan

# 3. Build and push Docker image (requires nasa_software/ populated)
cd docker/sdr-pipeline
bash build.sh

# 4. Pipeline triggers automatically on new .pcap uploads via EventBridge
# Or trigger manually:
aws stepfunctions start-execution \
  --state-machine-arn "arn:aws:states:eu-central-1:471112743408:stateMachine:groundstation-noaa20-sdr-pipeline" \
  --name "<contact_id>" \
  --input '{"contact_id":"...","bucket":"...","contact_date":"...","chunks":[...]}'
```

## Satellite contacts

| # | Contact ID | Date | Ground Station | Chunks | Size |
|---|-----------|------|----------------|--------|------|
| 1 | `c14d25d6-d69c-4d9f-a255-85908ab17c13` | 2026-06-19 | Hawaii 1 | 19 | 40.9 GB |
| 2 | `7903eb3f-8126-4e3c-bb3d-74eef49f79b3` | 2026-06-23 | Hawaii 1 | 27 | 58.7 GB |

## Processing chain

| Step | Tool | Input | Output | Duration/chunk |
|------|------|-------|--------|----------------|
| 1 | Python IQ extractor | .pcap (VITA-49) | .cs8 (raw I/Q) | ~8s |
| 2 | SatDump 1.2.2 | .cs8 | .cadu + composites PNG | ~5 min |
| 3* | RT-STPS 7.0+P1 | combined .cadu (all chunks) | RDR HDF5 (Level 0) | ~2 min |
| 4* | CSPP SDR 4.1.1 | RDR HDF5 | SDR + GEO HDF5 (Level 1) | ~5 min |
| 5 | VIIRS visualizer | composites / SDR+GEO | PNG / GeoTIFF | ~1 min |

*Steps 3-4 run ONCE on concatenated CADU from all chunks (in aggregation phase), not per-chunk.

## Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run property-based tests only
python -m pytest tests/test_*_property.py -v

# Validate Terraform
terraform validate
```

## AWS resources

| Resource | Name/ARN |
|----------|----------|
| Reception bucket | `aws-groundstation-demo-reception-471112743408` |
| Output bucket | `groundstation-noaa20-sdr-output-471112743408` |
| ECR repository | `groundstation-noaa20-sdr-pipeline` |
| State machine | `groundstation-noaa20-sdr-pipeline` |
| CodeBuild project | `groundstation-noaa20-sdr-pipeline` |
| EventBridge rule | `groundstation-noaa20-pcap-uploaded` |
| Region | `eu-central-1` |

## Specs

| Spec | Description | Status |
|------|-------------|--------|
| `aws-ground-control-demo` | Ground Station infrastructure + contact scheduling | Deployed |
| `noaa20-cadu-to-tiff` | Automated DigIF → SDR pipeline | Deployed — full RF→SDR chain working (SatDump + RT-STPS + **CSPP SDR**, 2026-07-23) |
| `noaa20-viirs-visualization` | SDR → georeferenced imagery | Deployed — SatDump path ~100-300 km (TLE); **NASA/CSPP path sub-km geolocated GeoTIFFs delivered** (see DEPLOYMENT_STATUS.md) |
