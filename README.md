# protogroundstation

Receive a NOAA-20 X-band downlink on a rented AWS antenna and turn the raw radio into
calibrated, geolocated VIIRS imagery — no owned hardware, no software licences.

One contact is ~10 minutes of antenna time, ~43 GB of raw VITA-49, and ~100 minutes of
wall-clock to Level 1. The whole loop — feasibility, planning, booking, reception,
processing — is code.

```
plan_pass.py → reserve-contact → X-band → S3 → EventBridge → Step Functions
             → CodeBuild ×22 (SatDump) → CodeBuild (RT-STPS + CSPP) → SDR/GEO → GeoTIFF
```

**Start here:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full diagram, the
end-to-end sequence with real timings, and the constraints that shaped the design.

---

## Repository layout

| Path | What it holds |
|---|---|
| [`infra/`](infra/) | All Terraform: root config, `terraform.tfvars`, and `infra/modules/*` |
| [`docs/`](docs/) | Architecture, operational runbooks, contact log (below) |
| [`scripts/`](scripts/) | Pass planner, I/Q extraction, aggregation, VIIRS visualisation |
| [`buildspecs/`](buildspecs/) | CodeBuild buildspecs; `aggregation.yml` is read by Terraform via `file()` |
| [`docker/`](docker/) | Image definitions — SatDump, RT-STPS, CSPP SDR in one image |
| [`lambdas/`](lambdas/) | Lambda handlers (scheduler, aggregation trigger, VIIRS orchestrator) |
| [`articles/`](articles/) | Write-ups of the project, with their own `awsdac` figures |
| [`tests/`](tests/) | pytest suite |
| `.kiro/specs/` | Design specs and task lists per subsystem |

## Documentation

| Document | Read it when |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | You want the system in one picture and one sequence |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | **Before touching CSPP, or to see what is deployed.** The exact conditions CSPP needs — J01 LUTs, `sdr_luts.sh` online, and the RDR filename rule that caused a multi-week dead end — plus current deployment state and the hard-won specifics |
| [`docs/MEDITERRANEAN_PASS.md`](docs/MEDITERRANEAN_PASS.md) | Planning a targeted acquisition: reachability, the ~10° Stockholm mask, the 7-day booking wall |
| [`docs/CONTACTS.md`](docs/CONTACTS.md) | Which passes exist, what they contain, what they cost |

## Quick start

```bash
aws sso login --profile AWSAdminAccess-471112743408
export AWS_PROFILE=AWSAdminAccess-471112743408 AWS_REGION=eu-central-1

# Infrastructure
cd infra && terraform init && terraform plan

# Container image (needs nasa_software/ populated — see below)
cd docker/sdr-pipeline && bash build.sh
```

### Booking a pass

The scheduler cron is **DISABLED on purpose** — enabling it previously booked two
unintended paid contacts. Reserve manually, and rank against the real offer set rather
than a propagator alone:

```bash
aws groundstation list-contacts --status-list AVAILABLE \
  --ground-station "Stockholm 1" --mission-profile-arn "$MP" --satellite-arn "$SAT" \
  --start-time <now> --end-time <now+7d> > contacts.json

./test_env/Scripts/python.exe scripts/plan_pass.py --days 7 --contacts-json contacts.json --step 5
```

Full procedure and the constraints behind it: [`docs/MEDITERRANEAN_PASS.md`](docs/MEDITERRANEAN_PASS.md).

### Processing a pass

Automatic. The contact reaching `COMPLETED` fires
`groundstation-noaa20-contact-completed-sdr`, and Step Functions lists the chunks itself.
To reprocess a contact by hand, delete its `.processing` marker first — otherwise the
execution short-circuits to `AlreadyProcessing`:

```bash
aws s3api delete-object --bucket groundstation-noaa20-sdr-output-471112743408 \
  --key "contacts/<contact_id>/.processing"
```

## Vendor software (not redistributable)

Place in `nasa_software/` before building the image:

| File | Source | Size |
|---|---|---|
| `satdump_1.2.2_ubuntu_22.04_amd64.deb` | [SatDump releases](https://github.com/SatDump/SatDump/releases) | 35 MB |
| `RT-STPS_7.0.tar.gz` + `RT-STPS_7.0_PATCH_1.tar.gz` | [CIMSS](https://cimss.ssec.wisc.edu/cspp/download/) | 50 MB |
| `CSPP_SDR_V4.1.tar.gz` + `CSPP_SDR_V4.1.1_patch.tar.gz` | [CIMSS](https://cimss.ssec.wisc.edu/cspp/download/) | 845 MB |
| `CSPP_SDR_V4.1_static_luts_j01.tar.gz` | [CIMSS](https://cimss.ssec.wisc.edu/cspp/download/) | 174 MB |

The J01 LUT tarball is required — without it CSPP aborts with
`Installation problem .../SDR_4_1_DB/package needs to exist`.

## AWS resources

| Resource | Name |
|---|---|
| Reception bucket | `aws-groundstation-demo-reception-471112743408` |
| Output bucket | `groundstation-noaa20-sdr-output-471112743408` |
| State machine / CodeBuild / ECR | `groundstation-noaa20-sdr-pipeline` |
| Trigger rule | `groundstation-noaa20-contact-completed-sdr` (`…-pcap-uploaded` superseded, DISABLED) |
| Aggregation instance | `i-01d21ecae10f99fbb` — retained but no longer in the pipeline path |
| Region | `eu-central-1` (antennas in `eu-north-1` / `us-east-2`) |

## Tests

```bash
python -m pytest tests/ -v
cd infra && terraform validate
```
