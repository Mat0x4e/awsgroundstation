# Design: Architecture Refinement (Well-Architected Delta)

## Overview

This spec is a **remediation backlog**, not a greenfield build. Each requirement targets one
confirmed finding and changes one well-bounded part of the existing Terraform/handlers. The
design here records, per requirement, *where* the change lands, *how* it is implemented, and
*how it is verified* — because verification is the whole point of the refinement (success must
become observable, not inferred).

All work routes through the generic workflow in
[`.kiro/steering/wa-delta-workflow.md`](../../steering/wa-delta-workflow.md): every task passes
**implement → validate → security-gate → review** before it is done. Terraform changes are
plan-only locally; `terraform apply` is never run from here (the pipeline/operators own apply).

## Design per requirement

### R1 — Codify SDR-image build project
- **Where:** new `infra/modules/sdr_pipeline/codebuild_image.tf` (or extend `codebuild.tf`) +
  `iam.tf`.
- **How:** replicate `viirs_visualization/codebuild.tf`'s `viirs_docker_build` pattern — a
  CodeBuild project (privileged, ECR push) with source matching how `satdump-noaa20-processing`
  is run today. Add the `docker-build/*` S3 read statement (`GetObject`, `ListBucket`) to the
  role in Terraform. Provide a `terraform import` block / documented import command so the
  existing project is adopted, not recreated.
- **Verify:** `terraform plan` shows import/no-destroy; Checkov clean or justified skip.

### R2 — ProductsWritten success signal
- **Where:** `lambdas/viirs_visualizer/handler.py` (count + `put_metric_data`) and a new alarm in
  `sdr_pipeline/monitoring.tf`.
- **How:** after the visualization build, list `products/{date}/{contact}/` and publish
  `ProductsWritten` (namespace `SDRPipeline`, dimensioned by contact) via `cloudwatch:PutMetricData`
  (IAM already allows it, namespace-conditioned). Alarm on `ProductsWritten == 0`.
- **Verify:** unit test (moto) asserts the count and the `put_metric_data` call; alarm present in
  plan.

### R3 — Async alarm parity
- **Where:** new `infra/modules/sdr_pipeline/monitoring.tf`.
- **How:** `aws_cloudwatch_metric_alarm` on `AWS/States ExecutionsFailed` for the SDR state
  machine, and Lambda-error alarms on the orchestrator + aggregation-trigger functions, all
  → `var.sns_topic_arn`. Mirror the resource shapes in `sync_pipeline/monitoring.tf`.
- **Verify:** `terraform validate` + `plan`; alarms reference the correct ARNs.

### R4 — Observable swallowed visualization failure
- **Where:** `sdr_pipeline/step_functions.tf` (the `StartVisualization` catch path) +
  `monitoring.tf`.
- **How:** on the `States.ALL` catch, add a state that publishes `VisualizationFailed` (metric)
  and an SNS message, then proceeds to `PipelineSucceeded` (unchanged terminal). Alarm on the
  metric over a window.
- **Verify:** state-machine JSON validates; plan shows the alarm; the success terminal is
  unchanged (diff review).

### R5 — Remove /opt silent-destruction
- **Where:** `sdr_pipeline/` and `sync_pipeline/` — SSM Document + State Manager association; docs.
- **How:** author an SSM Document that reconstructs `/opt/rt-stps` and `/opt/SDR_4_1` from the
  vendor tarballs already staged in S3 (`docker-build/` / `nasa_software/`). Attach via
  `aws_ssm_association` so drift is at least detected on a schedule. Update
  `docs/ARCHITECTURE.md`'s "exists in no Terraform" constraint.
- **Verify:** `terraform validate`; the association targets the instances by tag; docs updated.

### R6 — IAM least-privilege corrections
- **Where:** `security/main.tf` (processor PutMetricData condition) and
  `viirs_visualization/iam.tf` (`logs:*` → three actions).
- **How:** add `Condition { StringEquals { "cloudwatch:namespace" = "<ns>" } }`; replace the verb
  wildcard with the three write actions.
- **Verify:** Checkov no new HIGH/CRITICAL; `terraform plan` shows only the two policy diffs.

### R7 — De-risk sync path
- **Where:** `scripts/sync_receiver_*`, `lambdas/receiver_arm/handler.py`,
  `sync_pipeline/{ec2,monitoring,groundstation}.tf`.
- **How:** (a) PREPASS readiness — after arming, verify the UDP socket bound and first packets
  arrived; publish a readiness metric and alarm before LOS. (b) Raw tee — the listener also
  appends raw bytes to a capture uploaded to S3. (c) Packet-format gate — documented procedure to
  capture + verify a real sample before marking flight-ready. (d) Fixed-IP secondary ENI attached
  to the receiver, referenced by the dataflow endpoint group.
- **Verify:** unit tests for the readiness logic; `terraform plan` for the ENI + alarm; the
  format gate is a documented checklist item, not an automated claim.

### R8 — Cache CSPP ancillary/LUTs
- **Where:** `buildspecs/aggregation.yml` + a scheduled refresh (EventBridge rule → CodeBuild/Lambda).
- **How:** stage the ancillary/LUT set from an S3 cache prefix at build start; a scheduled job
  refreshes the cache off the hot path.
- **Verify:** aggregation buildspec reads from S3; measured wall-clock drop (operational check,
  recorded in `docs/CONTACTS.md` on the next real contact).

### R9 — Cost + audit guardrails
- **Where:** new budget resource (root or `observability`); `security/main.tf` CloudTrail bucket.
- **How:** `aws_budgets_budget` with a Ground Station cost filter + SNS notification; enable
  versioning on the trail bucket and drop `force_destroy`; document trail scope.
- **Verify:** `terraform plan`; Checkov on the trail bucket.

### R10 — Document tradeoffs + buildspec contract
- **Where:** `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_REFINEMENT.md`, a test under `tests/`.
- **How:** write the accepted-tradeoff records; add a test asserting the NO_SOURCE stub buildspec
  exits non-zero / is never used standalone.
- **Verify:** test passes; docs reviewed.

## Cross-cutting verification

- **No `terraform apply`** from this spec — plan-only. Operators/pipeline apply.
- **Checkov** at every checkpoint; HIGH/CRITICAL block.
- **IAM least-privilege** reviewed at every checkpoint (no new unconditioned wildcards).
- **Docs truth** — any change that invalidates a constraint in `docs/ARCHITECTURE.md` updates it
  in the same task (the project's continuous-learning convention).

## Testing strategy

- Python (Lambda) changes: pytest + moto, including the DynamoDB-free metric-publish paths.
- Terraform: `terraform validate` + `terraform plan` + Checkov; HCL `*.tftest.hcl` where a plan
  assertion adds value (alarms wired to the right ARNs).
- Property-based tests are **not** added here unless a change touches parsing/geometry/manifest
  logic (none of these do) — consistent with the project's existing testing note.
