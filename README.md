# protogroundstation

Receive a NOAA-20 X-band downlink on a rented AWS antenna and turn the raw radio into
calibrated, geolocated VIIRS imagery — no owned hardware, no software licences.

One contact is ~10 minutes of antenna time, ~43 GB of raw VITA-49, and ~100 minutes of
wall-clock to Level 1. The whole loop — feasibility, planning, booking, reception,
processing — is code.

```
plan_pass.py → reserve-contact → X-band → S3 → EventBridge → Step Functions
             → AWS Batch ×N chunk jobs (Spot, SatDump) → AWS Batch aggregation (on-demand, RT-STPS + CSPP)
             → SDR/GEO → AWS Batch render → GeoTIFF
```

> Runtime compute migrated from CodeBuild to **AWS Batch** on 2026-10-22 (fan-out, aggregation,
> and render), per ADR-002 / the `clean-compute-pipeline` spec. CodeBuild is retained only as a
> flag-gated fallback. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Start here:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full diagram, the
end-to-end sequence with real timings, and the constraints that shaped the design.

---

## Repository layout

| Path | What it holds |
|---|---|
| [`infra/`](infra/) | All Terraform: root config, `terraform.tfvars`, and `infra/modules/*` |
| [`docs/`](docs/) | Architecture, operational runbooks, contact log (below) |
| [`scripts/`](scripts/) | Pass planner, I/Q extraction, aggregation, VIIRS visualisation |
| [`buildspecs/`](buildspecs/) | Legacy CodeBuild buildspecs. Mostly superseded by the AWS Batch entrypoints (`scripts/aggregation_entrypoint.sh`, `scripts/viirs/render_entrypoint.sh`); `docker_build*.yml` still drive the image builds. `aggregation_sync.yml` is still `file()`-read on the CodeBuild sync fallback branch only |
| [`docker/`](docker/) | Image definitions — SatDump, RT-STPS, CSPP SDR in one image. `sdr-pipeline/enable_satdump_projection.py` turns on SatDump's own georeferencing at build time |
| [`lambdas/`](lambdas/) | Lambda handlers (scheduler, aggregation trigger, VIIRS orchestrator) |
| [`articles/`](articles/) | Write-ups of the project, with their own `awsdac` figures |
| [`tests/`](tests/) | pytest suite |
| `output/` | **Local, gitignored.** Working copies of pass products, one folder per contact — see [Collecting a pass locally](#collecting-a-pass-locally) |
| `.kiro/specs/` | Design specs and task lists per subsystem |

## Documentation

| Document | Read it when |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | You want the system in one picture and one sequence |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | **Before touching CSPP, or to see what is deployed.** The exact conditions CSPP needs — J01 LUTs, `sdr_luts.sh` online, and the RDR filename rule that caused a multi-week dead end — plus current deployment state and the hard-won specifics |
| [`docs/MEDITERRANEAN_PASS.md`](docs/MEDITERRANEAN_PASS.md) | Planning a targeted acquisition: reachability, the ~10° Stockholm mask, the 7-day booking wall |
| [`docs/CONTACTS.md`](docs/CONTACTS.md) | Which passes exist, what they contain, what they cost |
| [`docs/SATDUMP.md`](docs/SATDUMP.md) | **Before touching geolocation.** What SatDump already does — it georeferences its own composites, publishes the VIIRS scan model, stores per-scan timestamps, and needs a TLE — with the upstream file paths to check |
| [`docs/SYNC_PIPELINE.md`](docs/SYNC_PIPELINE.md) | The second, live demod/decode reception path — architecture and how it differs from the DigIF pipeline. **Blocked: cannot be flown** (NOAA-20 not onboarded for demod/decode on this account) |
| [`docs/SYNC_ONBOARDING_INVESTIGATION.md`](docs/SYNC_ONBOARDING_INVESTIGATION.md) | Why the sync path is blocked: evidence that NOAA-20 is onboarded for DigIF but not demod/decode (zero schedulable sync windows), the AWS-reference conformance check, and the PSA follow-up |

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
`groundstation-noaa20-contact-completed-sdr`, and Step Functions lists the chunks itself,
then fans the chunks out to **AWS Batch** (Spot) and runs aggregation + render as on-demand
Batch jobs. Monitor via Batch, not CodeBuild:

```bash
aws batch list-jobs --job-queue groundstation-noaa20-fanout-queue --region eu-central-1
aws batch list-jobs --job-queue groundstation-noaa20-agg-queue    --region eu-central-1
# logs: /aws/batch/groundstation-noaa20-{fanout,aggregation,render}
```

To reprocess a contact by hand: (1) delete its `.processing` marker — note the key has **no
date segment** (matches `step_functions.tf`); do **not** add the date — otherwise the
execution short-circuits to `AlreadyProcessing`; then (2) start a new execution:

```bash
aws s3 rm "s3://groundstation-noaa20-sdr-output-471112743408/contacts/<contact_id>/.processing"

aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:eu-central-1:471112743408:stateMachine:groundstation-noaa20-sdr-pipeline \
  --input '{"contact_id":"<id>","bucket":"aws-groundstation-demo-reception-471112743408","satellite_id":"33f035e1-73f7-47a5-9df8-fbc48636dca8","contact_time":"2026-10-17T20:33:15Z"}'
```

`contact_time` is **required** (the state machine derives `contact_date` from it); there is
no `contact_date` input field. The authoritative re-run recipe lives in
[`docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md`](docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md).

A `SUCCEEDED` execution is **not** proof that imagery exists: `StartVisualization`
catches to success on purpose, so the science products in S3 survive a rendering
failure. Check the products prefix, not the execution status:

```bash
aws s3 ls "s3://groundstation-noaa20-sdr-output-471112743408/products/<YYYY/MM/DD>/<contact_id>/"
```

Expect three composites (True Color, Thermal IR, Day Microphysics), each as PNG +
GeoTIFF + JSON sidecar. An empty prefix means visualization failed — the state
machine will not tell you.

### Collecting a pass locally

`output/` is the local working copy of what a pass produced. It is gitignored: S3
stays the source of truth, and nothing in it is needed to run the pipeline. It exists
so composites can be looked at, compared between passes, and pulled into articles.

One folder per contact, `contact-NN_<ground-station>_<YYYY-MM-DD>/`:

| Sub-folder | Contents |
|---|---|
| `VIIRS/` | SatDump VIIRS composites, from `chunk_0` unless noted |
| `VIIRS-DNB/` | SatDump day/night band composites |
| `ATMS/` | SatDump ATMS composites |
| `NASA-SDR/` | products rendered from CSPP SDR/GEO HDF5 |
| `products/` | products rendered by the pipeline's visualization stage |
| `dataset.json` | SatDump dataset descriptor for the chunk that was pulled |

A contact only has the folders it produced. After a pass completes and the
visualization build succeeds:

```bash
BUCKET=groundstation-noaa20-sdr-output-471112743408
CONTACT=<contact_id>; DATE=<YYYY/MM/DD>          # e.g. 2026/08/31
DEST=output/contact-<NN>_<station>_<YYYY-MM-DD>  # e.g. contact-05_stockholm-1_2026-08-31

mkdir -p "$DEST"

# Composites. chunk_0 is the first 30 s after AOS, so for a target at the southern
# edge of reach (the Mediterranean from Stockholm) it is the chunk holding it.
# Use a later chunk if the area of interest is later in the pass.
aws s3 sync "s3://$BUCKET/contacts/$DATE/$CONTACT/satdump/chunk_0/VIIRS/"     "$DEST/VIIRS/"     --exact-timestamps
aws s3 sync "s3://$BUCKET/contacts/$DATE/$CONTACT/satdump/chunk_0/VIIRS-DNB/" "$DEST/VIIRS-DNB/" --exact-timestamps
aws s3 cp   "s3://$BUCKET/contacts/$DATE/$CONTACT/satdump/chunk_0/dataset.json" "$DEST/dataset.json"

# Rendered products (PNG + GeoTIFF + JSON sidecar). --exact-timestamps matters:
# a re-render produces sidecars of identical byte length, and plain sync skips
# same-sized files, so you silently keep the previous run's bounding boxes.
aws s3 sync "s3://$BUCKET/products/$DATE/$CONTACT/" "$DEST/products/" --exact-timestamps
```

Do **not** sync `satdump/` wholesale — a contact holds ~9,000 PNGs across every
instrument and all 22 chunks, ~4 GB. Nor `sdr/`/`rdr/`: those are multi-GB HDF5 and
belong in S3.

Then check what you actually have, rather than trusting the sync:

```bash
# the sidecar bounding box should match the render you just made
cat "$DEST/products/"*true_color*.json
```

Record two things in [`docs/CONTACTS.md`](docs/CONTACTS.md) while the pass is fresh:
which chunk the composites came from, and whether the JSON sidecars' `bounding_box`
is plausible for the pass geometry.

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
| Terraform state | S3 backend `groundstation-noaa20-tfstate-471112743408` (`use_lockfile`, no DynamoDB) |
| State machine | `groundstation-noaa20-sdr-pipeline` |
| Batch job queues | `groundstation-noaa20-fanout-queue` (Spot), `groundstation-noaa20-agg-queue` (on-demand) |
| Batch job definitions | `groundstation-noaa20-chunk`, `...-aggregation`, `...-render` |
| Batch log groups | `/aws/batch/groundstation-noaa20-{fanout,aggregation,render}` |
| ECR images | `groundstation-noaa20-sdr-pipeline` (SatDump/RT-STPS/CSPP), `groundstation-noaa20-viirs-visualization` (dedicated render image) |
| Image-build CodeBuild | `groundstation-noaa20-sdr-docker-build`, `groundstation-noaa20-viirs-docker-build` (build from GitHub `main`) |
| Trigger rule | `groundstation-noaa20-contact-completed-sdr` (`...-pcap-uploaded` superseded, DISABLED) |
| Aggregation instance | `i-01d21ecae10f99fbb` — retained but out of the path (aggregation now runs on the Batch on-demand CE) |
| Retired | CodeBuild `groundstation-noaa20-sdr-pipeline` (fan-out/agg, task 8) and out-of-band `satdump-noaa20-processing` + `codebuild-satdump-role` (deleted 2026-10-22) |
| Region | `eu-central-1` (antennas in `eu-north-1` / `us-west-2` / `us-east-2`) |

## Tests

```bash
python -m pytest tests/ -v
cd infra && terraform validate
```
