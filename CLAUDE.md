# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AWS Ground Station pipeline that receives a NOAA-20 X-band downlink and turns raw
VITA-49 radio into calibrated, geolocated VIIRS imagery. One contact is ~10 minutes of
antenna time, ~43 GB of raw data, ~100 minutes wall-clock to Level 1. See
[`README.md`](README.md) for the full picture and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the end-to-end sequence, state machine, and the constraints that shaped the design —
read that before changing anything in the processing chain.

```
plan_pass.py → reserve-contact → X-band → S3 → EventBridge → Step Functions
             → CodeBuild ×22 (SatDump) → CodeBuild (RT-STPS + CSPP) → SDR/GEO → GeoTIFF
```

## Commands

```bash
# Tests (Python venv lives in test_env/, Windows checkout)
./test_env/Scripts/python.exe -m pytest tests/ -v
./test_env/Scripts/python.exe -m pytest tests/test_geolocation_unit.py -v          # single file
./test_env/Scripts/python.exe -m pytest tests/test_geolocation_unit.py::test_name  # single test
./test_env/Scripts/python.exe -m pytest tests/integration/ -v                      # integration only

# Shell-script unit tests (bats)
bats tests/test_satdump_process.bats

# Terraform
cd infra && terraform init && terraform plan
cd infra && terraform validate

# AWS session (required before any AWS-touching command)
aws sso login --profile AWSAdminAccess-471112743408
export AWS_PROFILE=AWSAdminAccess-471112743408 AWS_REGION=eu-central-1
```

Property-based tests (Hypothesis) live in `tests/test_*_property.py` and
`tests/test_pipeline_idempotence.py`/`test_prop_file_acceptance.py` — run these when
touching parsing, geometry, or manifest logic, since they're the ones that catch edge
cases the example-based tests miss.

## Repository layout

| Path | What it holds |
|---|---|
| `infra/` | All Terraform: root config, `terraform.tfvars`, `infra/modules/*` (contact_scheduler, mission_profile, observability, processing_pipeline, s3_delivery, sdr_pipeline, security, viirs_visualization) |
| `docs/` | Architecture, deployment status, operational runbooks, contact log |
| `scripts/` | Pass planner, I/Q extraction, aggregation, and `scripts/viirs/` — the VIIRS processing package |
| `buildspecs/` | CodeBuild buildspecs; `aggregation.yml` is read by Terraform via `file()` — it's the one actually used by the state machine |
| `docker/sdr-pipeline/` | Single image combining SatDump + RT-STPS + CSPP SDR |
| `lambdas/` | Handlers: contact_scheduler, aggregation_trigger, data_processor, viirs_visualizer |
| `tests/` | pytest suite + one bats file; `tests/integration/` for end-to-end pipeline tests |
| `output/` | Local, gitignored working copies of pass products — not needed to run the pipeline |
| `.kiro/specs/` | Design specs and task lists per subsystem |
| `nasa_software/` | Vendor tarballs (CSPP, RT-STPS, SatDump .deb) — not redistributable, must be placed manually before building the image |

## Architecture essentials

**Processing chain** (each step strips a container or adds meaning):
```
.pcap (VITA-49) → .cs8 (I/Q) → SatDump (QPSK/Viterbi/Reed-Solomon) → .cadu (CCSDS)
  → RT-STPS → RDR HDF5 (Level 0) → CSPP viirs_sdr.sh → SDR+GEO HDF5 (Level 1) → GeoTIFF
```
SatDump also renders composite PNGs directly from CADU (uncalibrated, its own scan-model
georeferencing) — that's the cheap/fast branch; CSPP is the accurate/slow branch
(calibrated radiances, terrain-corrected per-pixel lat/lon).

**Step Functions state machine** (`groundstation-noaa20-sdr-pipeline`, defined in
`infra/modules/sdr_pipeline/step_functions.tf`) fans out 22 chunks to CodeBuild (19
concurrent, tolerates 100% failure), then runs an aggregation build (CSPP), then calls
the VIIRS visualization Lambda directly. **A `SUCCEEDED` execution proves neither good
chunks nor imagery** — `StartVisualization` catches all errors to success so a rendering
failure doesn't fail the run. Always verify against the `products/` S3 prefix, not
execution status.

**Non-obvious operational facts** (see `docs/ARCHITECTURE.md` for the full list with
detail — these are the ones most likely to cause confusion during development):
- The contact scheduler cron is **disabled on purpose**; contacts are booked manually.
- The visualization buildspec is **not** `buildspecs/viirs_*.yml` — those are manual-run
  copies. The real spec is passed as `buildspecOverride` from `lambdas/viirs_visualizer/handler.py`.
- The SDR CodeBuild project (`satdump-noaa20-processing`) exists **outside Terraform**
  entirely — a `terraform apply` won't create or repair it.
- CSPP requires internet access (`sdr_luts.sh`) and therefore must run in CodeBuild, not
  on the EC2 aggregation instance (which is SSM-outbound-only and no longer in the pipeline path).
- Reprocessing a contact requires deleting its `.processing` marker in S3 first, or the
  execution short-circuits.
- SatDump's own composite georeferencing is **parked** (segfaults at 1.2.2 — see
  `docs/SATDUMP.md`); the active geolocation fallback is `scripts/viirs/scan_geometry.py`,
  which reconstructs the scan model from outside SatDump. Don't extend it — the intent is
  to fix the SatDump config once upgraded, not to invest further in the fallback.

**`docs/DEPLOYMENT.md`** has the exact CSPP invocation recipe and the five conditions it
needs (original RDR filename preserved, J01 LUTs installed, `sdr_luts.sh` run online,
`.h5` not `.PDS`, correct AWS profile) — read it before touching anything CSPP-related.

**`scripts/viirs/`** is the core processing package (readers for CBOR/GEO/SDR HDF5,
BT conversion, image rendering, GeoTIFF export, bounding-box calculation) — its public
API is re-exported through `scripts/viirs/__init__.py`.

## Terraform conventions

- **Prefer standard, published Terraform modules (registry.terraform.io / well-known
  community modules) over hand-rolled resources wherever one reasonably fits** —
  e.g. VPC, networking, and other generic infrastructure. Hand-roll only when the
  resource is AWS Ground Station-specific (`awscc_groundstation_*`), needs the
  project's specific least-privilege IAM/KMS/tagging conventions, or no suitable
  standard module exists.

## Windows-specific note

This is a Windows checkout (`test_env/` is a Windows venv). Shell scripts, Python, and
Terraform files are forced to LF via `.gitattributes` because they execute inside Linux
CodeBuild/EC2 containers — a CRLF shebang breaks with "required file not found". Don't
let editors re-introduce CRLF on `*.sh`, `*.py`, `*.tf`, `*.yml`, `*.yaml`.
