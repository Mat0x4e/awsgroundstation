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
