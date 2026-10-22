# Implementation Plan: Architecture Refinement (Well-Architected Delta)

## Overview

Remediation backlog derived from [`docs/ARCHITECTURE_REFINEMENT.md`](../../../docs/ARCHITECTURE_REFINEMENT.md).
Each task is independently verifiable and ordered by priority (1 = highest). Every task runs
through the four agentic quality gates in
[`.kiro/steering/wa-delta-workflow.md`](../../steering/wa-delta-workflow.md):
**implement → validate → security-gate → review**. Terraform is plan-only; `terraform apply`
is never run from here.

Routing: Terraform tasks → `terraform` subagent; Python/Lambda tasks → `python` subagent;
security gates → `security-scanner` subagent; doc tasks → `docs-writer` or handled directly.

## Tasks

- [x] 1. **(Pri 1, OE-1/SEC-3)** Codify the SDR-image build CodeBuild project
  - [x] 1.1 Add CodeBuild project + privileged role in `sdr_pipeline`, mirroring
    `viirs_visualization/codebuild.tf`; express the `docker-build/*` S3 read in Terraform.
    - _Requirements: 1.1, 1.2_
  - [x] 1.2 Provide a documented `terraform import` path so the existing out-of-band project is
    adopted, not recreated. (Rename-on-import kept per user decision; replacement caveat documented.)
    - _Requirements: 1.3_

- [x] 2. **(Pri 1, REL-4)** Emit a real `ProductsWritten` success signal
  - [x] 2.1 In `lambdas/viirs_visualizer/handler.py`, count objects under
    `products/{date}/{contact}/` and publish `ProductsWritten` (namespace `SDRPipeline`).
    - _Requirements: 2.1, 2.3_
  - [x]* 2.2 Unit test (moto) asserting the count and the `put_metric_data` call.
    - _Requirements: 2.4_
  - [x] 2.3 Add a `ProductsWritten == 0` alarm → shared SNS (viirs_visualization/monitoring.tf).
    - _Requirements: 2.2_

- [x] 3. **Checkpoint — validate + security gate (Wave 0/1 output)** — PASS
  - Run `terraform validate` + `terraform plan`; confirm Task 1 shows import/no-destroy.
  - Run `security-scanner` (Checkov) on `infra/`; HIGH/CRITICAL block.
  - Verify no new unconditioned IAM wildcards.

- [x] 4. **(Pri 2, REL-1)** Async alarm parity — create `sdr_pipeline/monitoring.tf`
  - [x] 4.1 `ExecutionsFailed` alarm on the SDR state machine → shared SNS.
    - _Requirements: 3.1, 3.3_
  - [x] 4.2 Lambda-error alarms on orchestrator + aggregation-trigger → shared SNS.
    - _Requirements: 3.2, 3.3_

- [x] 5. **(Pri 2, REL-2)** Make swallowed visualization failure observable
  - [x] 5.1 On the `StartVisualization` `States.ALL` catch, publish `VisualizationFailed` metric
    + SNS, preserving the `PipelineSucceeded` terminal.
    - _Requirements: 4.1, 4.2_
  - [x] 5.2 Add a `VisualizationFailed` threshold alarm.
    - _Requirements: 4.3_

- [x] 6. **(Pri 2, OE-2)** Remove the `/opt/*` silent-destruction failure mode
  - [x] 6.1 Author an SSM Document reconstructing `/opt/rt-stps` + `/opt/SDR_4_1` from the S3
    vendor tarballs; attach via `aws_ssm_association` (drift detection — full reconstruction deferred, documented).
    - _Requirements: 5.1, 5.2_
  - [x] 6.2 Update `docs/ARCHITECTURE.md`'s "exists in no Terraform" constraint.
    - _Requirements: 5.3_

- [x] 7. **(Pri 3, SEC-1/SEC-2)** IAM least-privilege corrections
  - [x] 7.1 Add `cloudwatch:namespace` condition to `processor_lambda` PutMetricData
    (`security/main.tf`). (namespace `GroundStation/DataProcessor`)
    - _Requirements: 6.1_
  - [x] 7.2 Narrow `viirs_visualization` CodeBuild log policy `logs:*` → three write actions.
    - _Requirements: 6.2_

- [x] 8. **Checkpoint — validate + security gate (Wave 2/3 output)** — PASS
  - `terraform validate` + `plan`; confirm only intended diffs.
  - `security-scanner` (Checkov) on `infra/`; HIGH/CRITICAL block; confirm 7.1/7.2 resolved.
  - Confirm the visualization success terminal is unchanged.

- [x] 9. **(Pri 3, REL-5/REL-6)** De-risk the synchronous reception path
  - [x] 9.1 PREPASS readiness check: verify listener bound + first-packet arrival; readiness
    metric + alarm that fires before LOS.
    - _Requirements: 7.1_
  - [x] 9.2 Raw-capture tee to S3 for replayability.
    - _Requirements: 7.2_
  - [x] 9.3 Fixed-IP secondary ENI on the receiver; bind the dataflow endpoint group to it.
    - _Requirements: 7.4_
  - [x] 9.4 Document the packet-format validation gate (capture + verify before flight-ready).
    - _Requirements: 7.3_

- [x] 10. **(Pri 4, REL-3)** Chunk success/failure metric
  - [x] 10.1 Emit `ChunksSucceeded` / `ChunksFailed` from the Map result; alarm on all-chunks-failed
    (metric-math). Kept `ToleratedFailurePercentage = 100`.
    - _Requirements: (REL-3; supports 3.x observability intent)_

- [x] 11. **(Pri 4, SEC-4/COST-1)** Cost + audit guardrails
  - [x] 11.1 `aws_budgets_budget` → SNS (account-wide; GS-service `cost_filter` committed, commented, ready to enable).
    - _Requirements: 9.1_
  - [x] 11.2 Enable versioning on the CloudTrail bucket; drop `force_destroy`.
    - _Requirements: 9.2_

- [x] 12. **(Pri 5, PERF-1)** Cache CSPP ancillary/LUTs in S3
  - [x] 12.1 Stage ancillary/LUTs from an S3 cache prefix in `buildspecs/aggregation.yml` (cache-then-fallback).
    - _Requirements: 8.1, 8.3_
  - [x] 12.2 Scheduled cache-refresh job (EventBridge rate(7 days) → CodeBuild).
    - _Requirements: 8.2_

- [x] 13. **(Pri 5, OE-3/REL-7/COST-2/PERF-2)** Document tradeoffs + buildspec contract
  - [x] 13.1 Record accepted tradeoffs (single-region/no-DR, single CMK, single-AZ receiver,
    scheduler-disabled, tolerate-100%, account-wide budget) in `docs/ARCHITECTURE.md`.
    - _Requirements: 9.3, 10.1_
  - [x] 13.2 Document the NO_SOURCE + buildspecOverride contract; add a test asserting the stub
    cannot run standalone. (Surfaced + fixed a real gap: sdr_pipeline stub now `exit 1`.)
    - _Requirements: 10.2_

- [x] 13b. **(Open decision, OPEN-1)** Compute-platform tradeoff study — **after Task 12 (PERF-1)**
  - [x] 13b.1 Compare CodeBuild vs AWS Batch (Fargate / EC2 Spot) vs ECS-Fargate-via-SFN vs
    EC2+LUT-cache, each with per-contact cost + wall-clock + cost-of-change; record an ADR-style
    decision in `docs/ARCHITECTURE_REFINEMENT.md`. Produces a decision document, not an
    implementation; any migration is a separate spec.
    - _Requirements: 11.1, 11.2, 11.3, 11.4_
    - **Done:** ADR-001 recorded. Recommends AWS Batch on EC2 Spot for the fan-out; gated on
      PERF-1 + one measured bake-off before a migration spec. Note: study performed ahead of
      PERF-1 using real contact timings + list-price estimates (estimates flagged per AC 11.2);
      the measured bake-off remains the gate before implementation.

- [x] 14. **Final Checkpoint — full validation + security gate + review** — PASS
  - `terraform validate` ✅ + `fmt -check` ✅ (plan-only; apply owned by operator/pipeline).
  - `security-scanner` (Checkov) ✅ — 492 passed / 81 failed / 14 skipped; **0 net-new HIGH/CRITICAL**
    introduced by the refinement (all 5 HIGH findings pre-existing on unchanged resources).
  - Full Python suite ✅ — 237 passed / 5 skipped / 0 failed (ProductsWritten 4, sync listener 10,
    buildspec contract 2 all green).
  - All non-optional acceptance criteria verified; docs updated where constraints changed.

## Notes

- Tasks marked `*` are optional (test-only) and can be deferred for a faster path.
- No `terraform apply` from this spec — plan-only; operators/pipeline own apply.
- Each checkpoint is an **agentic quality gate**: the workflow blocks progression on a failed
  gate (see steering).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "2.3", "3"] },
    { "id": 2, "tasks": ["4.1", "4.2", "5.1", "7.1", "7.2"] },
    { "id": 3, "tasks": ["5.2", "6.1", "8"] },
    { "id": 4, "tasks": ["6.2", "9.1", "9.2", "9.3", "9.4"] },
    { "id": 5, "tasks": ["10.1", "11.1", "11.2"] },
    { "id": 6, "tasks": ["12.1", "12.2", "13.1", "13.2"] },
    { "id": 7, "tasks": ["13b.1", "14"] }
  ]
}
```
