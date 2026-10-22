# Implementation Plan: Compute Bake-off — AWS Batch on EC2 Spot vs CodeBuild

## Overview

A disposable, measurement-only experiment that resolves the measured gate in ADR-001. Runs
under the Well-Architected Delta Workflow ([`.kiro/steering/wa-delta-workflow.md`](../../steering/wa-delta-workflow.md)):
Terraform → `terraform` subagent (plan-only + Checkov); harness → `python` subagent. The one
sanctioned deviation: the operator applies the gated module and runs the harness once, because
a real measurement is the deliverable. Everything is torn down at the end.

Pick the test contact first — a fully-received one from `docs/CONTACTS.md` (e.g. contact #5
`ba2c5446`, 22 chunks, already in S3).

## Tasks

- [ ] 1. **Throwaway Batch-on-Spot module (gated, default-off)**
  - [ ] 1.1 Create `infra/modules/compute_bakeoff/` with `enable_compute_bakeoff` gate:
    `aws_batch_compute_environment` (MANAGED, EC2 SPOT, `c6i`/`m6i`, `max_vcpus` ≥ N×8,
    `SPOT_CAPACITY_OPTIMIZED`), `aws_batch_job_queue`, `aws_batch_job_definition` (existing SDR
    ECR image, same per-chunk entrypoint/args). All `bakeoff-` prefixed, tagged
    `Objective=compute-bakeoff` / `Disposable=true`.
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3_
  - [ ] 1.2 Least-privilege IAM: Batch service/instance/execution roles — S3 read on reception
    bucket, S3 write scoped to `bakeoff/*` only, ECR pull on the sdr image repo, KMS, three logs
    write actions. No wildcards beyond un-scopeable AWS actions.
    - _Requirements: 1.3, 2.1_
  - [ ] 1.3 Wire the module into root `main.tf` behind `count = var.enable_compute_bakeoff ? 1 : 0`.
    - _Requirements: 2.3_

- [ ] 2. **Checkpoint — validate + security gate**
  - `terraform validate` + `plan` (plan-only). `security-scanner` Checkov on the module;
    HIGH/CRITICAL block. Confirm IAM has no unconditioned wildcards and S3 write is `bakeoff/*`-scoped.
    - _Requirements: 1.3, 2.1_

- [ ] 3. **Bake-off harness**
  - [ ] 3.1 `scripts/bakeoff/_measure.py` — submit the Batch array job for the test contact's
    chunks, poll `describe_jobs` to completion, collect wall-clock, per-element runtime,
    queue delay (`createdAt→startedAt`), Spot-interruption count (`attempts>1`).
    - _Requirements: 3.1_
  - [ ] 3.2 Extend the harness to run the CodeBuild baseline: controlled `start-build` of the
    same chunks (NOT a production trigger), collect wall-clock, build-minutes, queue time.
    - _Requirements: 3.2_
  - [ ] 3.3 Cost capture: tag-filtered Cost Explorer read for the run window + Spot-runtime
    cross-check (Batch); build-minutes × 2XLARGE rate (CodeBuild). Actual incurred, not list.
    - _Requirements: 3.4_
  - [ ] 3.4 Correctness check: compare per-chunk output object count + sizes between platforms.
    - _Requirements: 3.3_
  - [ ]* 3.5 Unit-test the measurement/parsing logic with mocked `describe_jobs` / Cost Explorer
    responses (moto where available).
    - _Requirements: 3.1, 3.3_

- [ ] 4. **Operator run (manual, sanctioned real spend)**
  - Operator: set `enable_compute_bakeoff = true`, `terraform apply` the module, run
    `scripts/bakeoff/run_bakeoff.sh <contact_id>`. (~$1–2 Spot + one CodeBuild baseline.)
    This is the one step the agent does not execute — it incurs real cost and touches the account.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 5. **Record result in ADR-001**
  - [ ] 5.1 Append the measured results table (both platforms: $/contact, wall-clock,
    Spot-interruptions) to ADR-001 in `docs/ARCHITECTURE_REFINEMENT.md`; update ADR-001 status
    to Accepted/Rejected with the measured justification. State aggregation remains deferred
    (Requirement 5).
    - _Requirements: 4.1, 4.2, 5.1, 5.2_
  - [ ] 5.2 IF the result recommends migration THEN open a new migration spec (do not implement
    here).
    - _Requirements: 4.3_

- [ ] 6. **Mandatory teardown + verification**
  - [ ] 6.1 Operator: `terraform destroy -target module.compute_bakeoff` (or
    `enable_compute_bakeoff = false` + apply); delete the `bakeoff/` S3 prefix.
    - _Requirements: 6.1_
  - [ ] 6.2 Verification: assert no `bakeoff-` Batch resources and no running Spot instances
    tagged `Objective=compute-bakeoff` remain.
    - _Requirements: 6.2, 6.3_

## Notes

- `*` tasks are optional (test-only).
- The module may stay in-repo (default-off) for repeatability; no live resources after teardown.
- This spec produces a **measurement + ADR update**, not a migration. Migration is a separate,
  future spec gated on this result (and, for aggregation, on PERF-1).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2"] },
    { "id": 2, "tasks": ["3.1", "3.2", "3.3", "3.4", "3.5"] },
    { "id": 3, "tasks": ["4"] },
    { "id": 4, "tasks": ["5.1", "5.2"] },
    { "id": 5, "tasks": ["6.1", "6.2"] }
  ]
}
```
