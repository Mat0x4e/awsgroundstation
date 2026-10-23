# Clean Compute Pipeline Migration — Work-In-Progress Handoff

Resume point for continuing the `clean-compute-pipeline` spec in Kiro web. This captures the
exact state as of the last clean checkpoint commit so you can pick up mid-Stage-3 without
re-discovering anything.

**Spec:** `.kiro/specs/clean-compute-pipeline/` (requirements.md, design.md, tasks.md)
**Decision record:** `docs/ARCHITECTURE_REFINEMENT.md` → ADR-001 + ADR-002
**Full execution log:** `docs/CLEAN_COMPUTE_PIPELINE_MIGRATION.md` (read this for the per-stage
detail, every gotcha, and the validation evidence)
**AWS steering (live-learned gotchas):** `.kiro/steering/aws-knowledge.md`

## Goal (ADR-002)

Migrate every runtime compute stage off AWS CodeBuild onto AWS Batch, as a strangler
(stage-by-stage, each validated on contact-7 warm data), and fix 3 NASA render bugs.
Account `471112743408`, region `eu-central-1`. Contact-7 warm test data:
`dcef1518-c454-401f-bdcd-dc2e73fe89a5`, date `2026/10/17` (async DigIF, Oregon).

## What is DONE, DEPLOYED and VALIDATED (in this commit)

| Stage / Task | Status |
|---|---|
| Stage 1 — chunk fan-out -> Batch EC2 Spot | DONE, validated on contact-7 (25/25 chunks, 19 concurrent) |
| Task 4 — retire CodeBuild fan-out branch from the SFN Map | DONE (Map is Batch-only) |
| Stage 2 — aggregation (async) -> Batch EC2 on-demand | DONE, validated on contact-7 (full run SUCCEEDED, ~21 min agg) |
| Task 7 — wire both SFNs to the Batch aggregation job def | DONE (async + sync, flag-gated) |
| Task 8 (async) — retire shared `sdr_pipeline` CodeBuild project + role + states | DONE (4 destroyed) |
| Task 9 — 3 NASA render bug fixes, test-first | DONE (12 tests, full suite 287 passed) |

Everything above is applied to the live account AND committed here. `enable_batch_compute=true`
must remain set for the pipeline to run (the Map + async aggregation are Batch-only now).

### Key deployed infra
- `infra/modules/batch_compute/` — Spot CE (fan-out `chunk` job def) + on-demand CE (`aggregation`
  job def), shared 200 GiB aws/ebs-encrypted launch template, SG egress 80+443, least-privilege IAM.
- `infra/main.tf` — `enable_batch_compute` var + module wiring + deterministic Batch ARN locals
  (cycle-break) for fan-out and aggregation.
- `infra/modules/sdr_pipeline/step_functions.tf` — Map body is Batch-only; `CheckResults ->
  SubmitBatchAggregation -> StartVisualization` (CodeBuild agg states removed).
- `infra/modules/sync_pipeline/step_functions.tf` — `StopReceiver -> InjectAggFlag ->
  ChooseAggregationCompute -> SubmitBatchAggregation(RTSTPS=off)` OR CodeBuild poll-loop (flag-gated).
- `scripts/aggregation_entrypoint.sh` + `scripts/deploy_aggregation_entrypoint.sh` — the converged
  RT-STPS+CSPP / CSPP-only entrypoint (deployed to `s3://.../software/aggregation_entrypoint.sh`).
- `scripts/viirs/` — the 3 NASA bug fixes (granule-token reconciliation, thermal NaN sanitise,
  single-stretch destripe-on-raw) + `tests/test_nasa_render_bugs.py`.

### Six Stage-1 launch gotchas (all fixed; full detail in aws-knowledge.md)
SCP Owner-tag requirement; encrypted EBS; aws/ebs-not-CMK on Spot EBS; LT version pinning
(numeric latest_version); 200 GiB root volume for the ~20 GiB image; `ContainerOverrides.Environment`
vs `Ref::`. Plus: SFN timeout 5400->10800; CSPP needs HTTP port 80 egress for jpssdb LUT fetches.

## OPEN ITEMS — where to resume (in priority order)

### 1. Stage 3 — Visualization render -> Batch (task 10, IN PROGRESS)
This is the next and largest remaining piece. The render currently still runs on CodeBuild
(`groundstation-noaa20-viirs-visualization`), triggered fire-and-forget by the
`viirs-orchestrator` Lambda.

**Already written (in this commit, but NOT yet wired/deployed):**
- `scripts/viirs/render_entrypoint.sh` — relocates the inline `SATDUMP_BUILDSPEC` / `NASA_BUILDSPEC`
  logic from `lambdas/viirs_visualizer/handler.py` into one image-baked entrypoint, driven by
  `RENDER_BRANCH=satdump|nasa`. (Authored by the python subagent; `bash -n` passed. NOT yet
  referenced by any Batch job def — it is inert until task 10 wires it.)

**Still TODO for task 10 (not started):**
- (a) Terraform: add a `render` Batch job definition to `infra/modules/batch_compute/` on the
  on-demand CE, pointing at the **dedicated viirs-visualization ECR image** (NOT the sdr-pipeline
  image — see below), command `["/bin/bash","/opt/scripts/viirs/render_entrypoint.sh"]`, plus a
  render job IAM role (S3 rw products/*, read contacts/*, KMS, ECR pull on the viirs repo, logs,
  namespace-conditioned PutMetricData). Delegate to the `terraform` subagent.
- (b) Lambda: swap `viirs-orchestrator` `handler.py` from `codebuild.start_build(buildspecOverride=...)`
  to `batch.submit_job(...)` **behind an env flag** (strangler — keep the CodeBuild path until
  validated). Keep `_detect_path` (satdump/nasa) and `_lookup_acquisition` in the Lambda; pass
  `RENDER_BRANCH`, `INPUT_PREFIX`, `CONTACT_ID`, `CONTACT_DATE`, `KMS_KEY_ID`, `ENABLE_GEOTIFF`,
  `ENABLE_DESTRIPE`, `CONTACT_TIME`, `CHUNK_DURATION_S` via `ContainerOverrides.Environment`.
  Update `tests/test_viirs_visualizer.py`. Delegate to the `python` subagent.
- (c) **CRITICAL — image rebuild:** `scripts/viirs/` is baked into the dedicated viirs image via
  `COPY scripts/ /opt/scripts/` (docker/viirs-visualization/Dockerfile). So BOTH the task-9 NASA
  bug fixes AND `render_entrypoint.sh` only reach the render runtime after the viirs image is
  rebuilt + pushed (the `groundstation-noaa20-viirs-docker-build` CodeBuild project, or an
  equivalent). The NASA bug fixes are committed but NOT live in the render image until this rebuild.
- (d) Validate both branches on contact-7, then retire the `viirs-visualization` CodeBuild project
  (task 13).

**Grounding already done:** a context-gatherer produced a full map of the render dispatch
(handler.py inline constants, dedicated viirs image, `_detect_path`, `_lookup_acquisition`,
ProductsWritten in post_build). The facts are in `docs/CLEAN_COMPUTE_PIPELINE_MIGRATION.md`
Stage 3 notes and the conversation history. Re-read `lambdas/viirs_visualizer/handler.py`
(SATDUMP_BUILDSPEC ~37-96, NASA_BUILDSPEC ~98-152) and `infra/modules/viirs_visualization/`
before implementing.

### 2. Stage 2 sync-branch validation (owed before task 8 can finish)
The sync aggregation path (`RTSTPS=off`) is coded + deployed + flag-gated, but NOT runtime-validated
— contact-7 is async DigIF and cannot exercise the sync RDR->CSPP path. Before retiring the
`groundstation-noaa20-sync-aggregation` CodeBuild project, a real sync contact (or a replayed sync
RDR set) must validate it. Also: the sync SFN has an inner per-state `TimeoutSeconds=1800` (30 min)
on its aggregation state that is tight against a ~40-min cold-cache `sdr_luts.sh` fallback — review
before the sync validation run.

### 3. Dead resource cleanup (out of spec scope)
`satdump-noaa20-processing` — the old out-of-IaC CodeBuild project (OE-1 finding) is now dead
(fan-out on Batch) but is NOT in Terraform. Needs a deliberate manual console/CLI removal.

### 4. Bake-off / ADR status
ADR-001 recommended Batch-on-Spot gated on a measured bake-off; the production Stage-1 validation
effectively served as that measurement (19 concurrent Spot, concurrency win demonstrated). Update
ADR-001/ADR-002 status to Accepted with the real contact-7 numbers when convenient (task 15.2).

## How to run / validate (operator)
```
# Windows PowerShell — set these every session:
$env:AWS_PROFILE="AWSAdminAccess-471112743408"; $env:AWS_REGION="eu-central-1"; $env:PYTHONUTF8="1"
aws sso login --profile AWSAdminAccess-471112743408

# Terraform (plan-only from agents; operator applies):
terraform -chdir=infra plan -var "enable_batch_compute=true" -out plan.tfplan   # -out with a SPACE, not =
terraform -chdir=infra apply -var "enable_batch_compute=true"   # fresh apply, not a saved plan (latest_version churn)

# Re-run a contact through the SFN (clear the idempotence marker first):
aws s3 rm "s3://groundstation-noaa20-sdr-output-471112743408/contacts/<CONTACT_ID>/.processing"
aws stepfunctions start-execution --state-machine-arn <sdr-pipeline arn> --input file://<input.json>
# input.json: { "contact_id","bucket":"aws-groundstation-demo-reception-471112743408","satellite_id":"33f035e1-...","contact_time":"2026-10-17T20:33:15Z" }

# Python tests (root tests/ dir — NOT the apn lambda_src):
# run the same way tests/test_bbox_geometry.py is run (pytest from repo root with conftest sys.path shim)
```

## Delegation rules (enforced)
- All `*.tf` -> `terraform` subagent. All `*.py` -> `python` / `python-haiku` / `python-debugger`.
  Never edit infra/Python directly. Agents run Terraform plan-only; operator applies (SSO + spend).
- `bash scripts/*.sh` from PowerShell may not inherit the refreshed SSO token — run AWS CLI
  directly in PowerShell if a `bash`-wrapped command fails with `InvalidToken` (see aws-knowledge.md).

## Session update -- 2026-10-22 (Stage 3 delivered + cleanup)

Stage 3 (task 10: visualization render -> Batch) is DONE, APPLIED to the live account, and
VALIDATED on contact-7 for both render branches. Several Stage-2/OE-1 open items were also
closed. Resume context below.

### What was done this session (all applied to account 471112743408, eu-central-1)

1. **Stage 3 (a) render Batch job definition + IAM** -- `infra/modules/batch_compute/`.
   - New `render` job definition on the EXISTING on-demand CE + `agg-queue` (render is a single
     job, not a fan-out -- reused the CE/queue, no new ones). Rev 1 live.
   - Targets the DEDICATED `groundstation-noaa20-viirs-visualization` ECR image (NOT the
     sdr-pipeline image). command = ["/bin/bash","/opt/scripts/viirs/render_entrypoint.sh"];
     image-baked, no S3 bootstrap fetch (unlike the aggregation job def).
   - Dedicated least-privilege render JOB role (S3 ro contacts/*, rw products/*, ECR pull viirs
     repo, KMS, logs, PutMetricData conditioned to namespace SDRPipeline) + a dedicated render
     EXECUTION role scoped to the viirs repo (the shared batch_execution role is sdr-scoped).

2. **Stage 3 (b) Lambda strangler swap** -- `lambdas/viirs_visualizer/handler.py` +
   `infra/modules/viirs_visualization/`.
   - viirs-orchestrator now dispatches via `batch.submit_job` behind the `RENDER_COMPUTE` env
     flag (default "codebuild"; set to "batch" when enable_batch_compute=true). CodeBuild path
     is KEPT intact (strangler) until task 13.
   - All render env passed via ContainerOverrides.Environment -- never Ref:: (canary gotcha #6).
   - Lambda IAM gained batch:SubmitJob (agg-queue + render jobdef) and iam:PassRole on the two
     render roles (conditioned iam:PassedToService=ecs-tasks.amazonaws.com). New Lambda env:
     RENDER_COMPUTE, ENABLE_DESTRIPE, KMS_KEY_ID, BATCH_JOB_QUEUE, BATCH_JOB_DEFINITION.
   - Deterministic render Batch ARNs added to root locals (cycle-break, same pattern as Stage
     1/2); viirs image URL wired into module.batch_compute (acyclic one-way edge).
   - Tests: viirs suite 23/23 green. (The ~20 full-suite failures are PRE-EXISTING missing
     native deps -- matplotlib/sgp4/cartopy -- confirmed identical on the stashed baseline.)

3. **Stage 3 (c) image rebuild** -- rebuilt `viirs-visualization:latest` so render_entrypoint.sh
   and the task-9 NASA fixes are actually in the render runtime.

4. **Stage 3 (d) validation on contact-7** (`dcef1518-c454-401f-bdcd-dc2e73fe89a5`, 2026/10/17):
   - satdump branch (dispatched via the live Lambda): SUCCEEDED, ProductsWritten=11.
   - nasa branch (direct submit; _detect_path prioritises satdump so nasa needs a direct run):
     SUCCEEDED, ProductsWritten=14.
   - Verified by COMPLETION evidence (ProductsWritten emitted post-sync, "render_entrypoint.sh
     complete", fresh products/2026/10/17/.../ timestamps), not merely exit code.

5. **Cleanup / docs:**
   - ADR-001 status Proposed -> Accepted (2026-10-22) with the real contact-7 bake-off numbers.
   - Deleted the dead out-of-band CodeBuild project `satdump-noaa20-processing` + its orphaned
     role `codebuild-satdump-role` (not in IaC, superseded by the IaC sdr-docker-build project
     and the Batch fan-out; idle since 2026-09-02). Trimmed the stale import/rename comment in
     `sdr_pipeline/codebuild_image.tf`.
   - Steering: added the "image-build source ref must hold the code before Batch validation"
     learning to `.kiro/steering/aws-knowledge.md` (see the CRITICAL gotcha below).

### CRITICAL gotcha discovered (do not repeat)
The `viirs-docker-build` CodeBuild project builds from GitHub `sourceVersion=main`, but the
Stage-3 code (render_entrypoint.sh, NASA fixes) lived only on branch `med-pass-cspp-codebuild`.
The first rebuild produced an image from `main` WITHOUT render_entrypoint.sh -> every render job
failed with "/opt/scripts/viirs/render_entrypoint.sh: No such file or directory". Fix: commit +
push the branch, then rebuild pinned to it: `aws codebuild start-build --project-name
groundstation-noaa20-viirs-docker-build --source-version med-pass-cspp-codebuild`. A container
job runs IMAGE-BAKED code, so the image-build source ref is part of the deploy. Once this merges
to `main`, a default rebuild from `main` is correct again. Full writeup in aws-knowledge.md.

### Current architecture (as deployed 2026-10-22) -- tested [T] vs untested [U]
See `docs/Asciiart-archi.txt` "TARGET" diagram -- it now matches reality EXCEPT render uses the
DEDICATED viirs-visualization image (not the shared sdr-pipeline image).

```
  Ground Station -> EventBridge
     |
     +-- ASYNC (DigIF): SFN sdr_pipeline
     |     Map -> SubmitBatchChunk (Batch SPOT, chunk job)            [T] contact-7: 50/50 chunks
     |     CheckResults -> SubmitBatchAggregation (RTSTPS=on)         [T] contact-7: async agg OK
     |                     Batch ON-DEMAND, aggregation job
     |
     +-- SYNC (demod): SFN sync_pipeline
           FinishReceiver (RT-STPS on receiver EC2, TimeoutSeconds=1800)  [--] unchanged/proven
           SubmitBatchAggregation (RTSTPS=off, RDR->CSPP)             [U] NOT run on real data
                     Batch ON-DEMAND, aggregation job (no inner TO; jobdef 3600s)

  (both async+sync) SDR+GEO -> S3
     |
  Lambda viirs-orchestrator (RENDER_COMPUTE=batch) -> batch.submit_job [T] Lambda->Batch dispatch
     |                                                                 [U] codebuild fallback path
  Batch ON-DEMAND render job (render_entrypoint.sh, DEDICATED viirs image)
     RENDER_BRANCH=satdump  [T] contact-7 ProductsWritten=11
     RENDER_BRANCH=nasa     [T] contact-7 ProductsWritten=14
     |
  products/ -> png + geotiff + json   (NASA fixes live in rebuilt image)
```

TESTED [T]:   Stage 1 fan-out (Spot) . Stage 2 async aggregation . Stage 3 render both branches .
              Lambda->Batch dispatch . dedicated viirs image + render roles.
UNTESTED [U]: Stage 2 SYNC aggregation (RTSTPS=off) -- blocked on a real/replayed sync RDR set;
              CodeBuild render fallback (retained but not re-exercised since the flip).
UNCHANGED [--]: receiver EC2 live-demod + RT-STPS-on-EC2. The 1800s timeout is on that RT-STPS
              receiver step (~2 min typical), NOT on aggregation -- reviewed, no change needed.

### OPEN ITEMS after this session (priority order)
1. **Stage 2 sync-branch runtime validation (STILL OPEN, now the top blocker).** The RTSTPS=off
   RDR->CSPP path is coded + deployed + flag-gated but never run on real data. Needs a real sync
   contact OR a replayed sync RDR set staged under sync_output/contacts/.../rdr/. Until it passes,
   do NOT retire the `groundstation-noaa20-sync-aggregation` CodeBuild project (task 8).
2. **Task 13 -- retire the viirs-visualization CodeBuild project.** Deferred deliberately: keep
   the CodeBuild render fallback until the Batch render path has run for several REAL production
   contacts (not just the contact-7 re-render). Flip is reversible via RENDER_COMPUTE.
3. **Merge PR #2** (`med-pass-cspp-codebuild` -> `main`). After merge, the default
   viirs-docker-build (sourceVersion=main) rebuilds the correct image; the --source-version
   workaround is then unnecessary.
4. **ADR-002 numbers (optional, task 15.2).** ADR-001 is now Accepted with real numbers; ADR-002
   is already Accepted(direction). Add the measured Stage-3 render timings if desired.

### How to re-validate the render path quickly (no full pipeline re-run)
contact-7 inputs already exist in S3 (satdump/ composites + sdr/ HDF5). To re-render:
- satdump: invoke the Lambda with {bucket:<sdr-output>, key:contacts/2026/10/17/<id>/manifest.json}
  (it detects satdump and submits a Batch render job).
- nasa: submit the render job directly (satdump wins _detect_path when both inputs exist):
  `aws batch submit-job --job-queue groundstation-noaa20-agg-queue --job-definition
   groundstation-noaa20-render --job-name render-<id>-nasa --container-overrides <env json with
   RENDER_BRANCH=nasa, INPUT_BUCKET, INPUT_PREFIX=contacts/2026/10/17/<id>, CONTACT_ID,
   CONTACT_DATE=2026/10/17, KMS_KEY_ID, ENABLE_GEOTIFF=true, ENABLE_DESTRIPE=true,
   CONTACT_TIME="", CHUNK_DURATION_S="">`
Then confirm fresh objects under products/2026/10/17/<id>/ and ProductsWritten in the render log
group /aws/batch/groundstation-noaa20-render.

## NASA render verification on contact-7 (2026-10-23) + two fix items

Verified the NASA-precision render products for contact-7 (`dcef1518...`, latest pass) live in
S3 (`products/2026/10/17/dcef1518-.../`, rendered 2026-10-22 13:25). The NASA path IS the
precision path (reads CSPP calibrated SDR + per-pixel terrain-corrected GEO, not a TLE scan model).

Quantified from the NASA THERMAL GeoTIFF (read with rasterio):
- CRS EPSG:4326, 6502 x 1657, float32, nodata=NaN.
- Bounds 43.97-55.06 N, -141.96 to -98.40 W (correct for the Oregon-1 descending pass over
  N. America); swath ~3077 km matches VIIRS geometry.
- Pixel size ~0.0067 deg = ~0.48 km (lon @ 49.5N) x ~0.75 km (lat) -> **sub-km per-pixel,
  confirming the NASA-precision claim quantitatively.**

### FIX ITEM 1 (real bug) -- NASA true_color GeoTIFF is missing despite the sidecar advertising it
`viirs_nasa_true_color_<id>.json` has `"geotiff_file": "...true_color...tif"`, but that `.tif`
does NOT exist in S3 (head-object -> 404). Only the PNG + JSON were written for the NASA
true-color product; the NASA thermal product has its `.tif`. So on the latest pass the NASA
true-color is PNG-only, not a GeoTIFF. The satdump true_color `.tif` DOES exist, so this is
specific to the NASA true-color branch of the render. Investigate `scripts/viirs/` NASA
true-color GeoTIFF export (and make the sidecar `geotiff_file` field conditional on the file
actually being written, so it stops advertising a non-existent product). Python change ->
python subagent.

### FIX ITEM 2 (minor) -- NASA sidecar provenance fields are placeholders
Both NASA sidecars carry `granule_id: "unknown"`, `orbit_number: 0`, `datetime_utc: "unknown"`.
Geolocation (bounding_box, swath_width_km) is correctly populated; only provenance is missing.
The NASA render reads geolocation from the GEO HDF5 but does not populate granule/orbit/time into
the sidecar. Low priority; fix if those fields are consumed downstream.
