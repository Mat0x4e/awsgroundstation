# Requirements: Compute Bake-off — AWS Batch on EC2 Spot vs CodeBuild

## Introduction

This spec is the **measured gate** required by ADR-001 (OPEN-1) in
[`docs/ARCHITECTURE_REFINEMENT.md`](../../../docs/ARCHITECTURE_REFINEMENT.md) before any
decision to migrate the SDR chunk fan-out off CodeBuild. ADR-001 recommends AWS Batch on EC2
Spot on the strength of a cost/fit study built from real contact timings and list-price
*estimates*. This spec replaces the estimates with **one real measurement**: run a single
contact's chunks through a throwaway Batch-on-Spot compute environment, record actual
per-contact cost and wall-clock, and compare head-to-head against the CodeBuild baseline.

**This is an experiment, not a migration.** Everything it creates is disposable and torn down
at the end. It does not touch the production state machine, the production CodeBuild project,
or any pipeline resource. It produces a measurement and a go/no-go recommendation; the actual
migration (if any) is a separate spec gated on this result.

**Hard prerequisite:** ADR-001 states the bake-off is gated on PERF-1 (the S3 LUT cache) so
that no stage needs the public internet. However, the **chunk fan-out stage does not run CSPP**
— only SatDump — so the fan-out bake-off can proceed *without* PERF-1. The **aggregation**
stage (CSPP, which needs the LUTs) is explicitly out of scope for this bake-off for exactly
that reason. See Requirement 5.

## Scope boundary

| In scope | Out of scope |
|---|---|
| Chunk fan-out stage only (SatDump: VITA-49 → I/Q → CADU + composites) | Aggregation / CSPP stage (needs PERF-1 first) |
| One already-received contact's chunks, replayed from S3 | Any live contact or Ground Station booking |
| A throwaway Batch compute environment (EC2 Spot) + job queue + job definition | The production state machine / CodeBuild project |
| Measured $/contact and wall-clock for both platforms | Fargate option (deferred — EC2 Spot is the ADR's primary) |
| Teardown of all experiment resources | Permanent infrastructure |

---

## Requirement 1 — Reuse the existing container image unchanged

**User story:** As an architect, I want the bake-off to run the exact same ECR image the
production CodeBuild chunk builds use, so the measurement compares platforms, not images.

#### Acceptance Criteria
1. WHEN the Batch job definition is created THEN it SHALL reference the existing
   `${project_name}-sdr-pipeline` ECR image (`:latest`), not a new or modified image.
2. WHEN the chunk-processing command runs under Batch THEN it SHALL invoke the same entrypoint /
   script the CodeBuild chunk buildspec invokes, with the same per-chunk arguments.
3. WHEN inputs are read THEN they SHALL come from the same S3 reception-bucket keys the
   production pipeline reads (an already-received contact), with outputs written to a throwaway
   prefix so production products are untouched.

---

## Requirement 2 — Throwaway Batch-on-Spot environment, fully isolated

**User story:** As an operator, I want the experiment's infrastructure isolated and clearly
labelled disposable, so it cannot be confused with or interfere with production.

#### Acceptance Criteria
1. WHEN the environment is created THEN it SHALL be a dedicated Batch compute environment
   (managed, EC2 **Spot**), job queue, and job definition, all name-prefixed `bakeoff-` and
   tagged `Objective = "compute-bakeoff"` / `Disposable = "true"`.
2. WHEN the compute environment is sized THEN its instance types SHALL match the CodeBuild chunk
   compute class as closely as practical (the `BUILD_GENERAL1_2XLARGE` ~8 vCPU / 16 GB shape —
   e.g. `c6i`/`m6i` family) so the comparison is like-for-like, and `maxvCpus` SHALL be set high
   enough that all chunks of the test contact can run concurrently (the point is to remove the
   CodeBuild concurrency throttle).
3. WHEN the experiment is defined in Terraform THEN it SHALL live in a self-contained module
   gated behind an `enable_compute_bakeoff` variable defaulting to `false`, so it is never
   created by a normal apply and can be destroyed independently.
4. WHEN the experiment is torn down THEN removing the one variable / targeted destroy SHALL
   remove every bake-off resource with no orphans.

---

## Requirement 3 — Measure both platforms on the same contact

**User story:** As an architect, I want apples-to-apples measurements of wall-clock and cost for
the same workload on both platforms, so the ADR estimate is replaced by data.

#### Acceptance Criteria
1. WHEN the test contact's N chunks are run on Batch-on-Spot THEN the harness SHALL record:
   total wall-clock (submit → last job SUCCEEDED), per-job runtime distribution, queue/scheduling
   delay, number of Spot interruptions/retries, and the EC2 Spot cost incurred.
2. WHEN the same N chunks are run on the existing CodeBuild project (a controlled re-run, not a
   production trigger) THEN the harness SHALL record: total wall-clock, per-build runtime,
   CodeBuild **queue** time, and build-minute cost.
3. WHEN both runs complete THEN the outputs SHALL be byte-count/structure-comparable (same CADU +
   composites per chunk) to confirm the platform swap did not change the result.
4. WHEN cost is computed THEN it SHALL use actual incurred cost (Cost Explorer / Spot billing),
   not list-price estimates — this is the whole point of the bake-off (ADR-001 AC 11.2).

---

## Requirement 4 — Record the result as an ADR-001 update

**User story:** As a maintainer, I want the measured result folded back into ADR-001, so the
decision record carries data, not estimates.

#### Acceptance Criteria
1. WHEN the bake-off completes THEN a results table (measured $/contact and wall-clock, both
   platforms, Spot-interruption count) SHALL be appended to ADR-001 in
   `docs/ARCHITECTURE_REFINEMENT.md`.
2. WHEN the result is recorded THEN ADR-001's status SHALL be updated to Accepted or Rejected
   (migrate / do-not-migrate) with the measured justification.
3. IF the result recommends migration THEN a new migration spec SHALL be opened (not implemented
   here).

---

## Requirement 5 — CSPP aggregation explicitly deferred

**User story:** As an architect, I want it on record that aggregation is not baked off here, so
no one assumes the fan-off result transfers to the CSPP stage.

#### Acceptance Criteria
1. WHEN the bake-off scope is documented THEN it SHALL state that the aggregation/CSPP stage is
   deferred because it depends on PERF-1 (the S3 LUT cache) to run without internet.
2. WHEN the fan-out result is recorded THEN it SHALL NOT be extrapolated to aggregation; a
   separate measurement after PERF-1 is required for that stage.

---

## Requirement 6 — Teardown is mandatory and verified

**User story:** As an owner, I want certainty that the experiment leaves no running cost behind.

#### Acceptance Criteria
1. WHEN the measurement is captured THEN all bake-off resources (compute environment, queue, job
   definition, any Spot instances, the throwaway output prefix) SHALL be destroyed.
2. WHEN teardown completes THEN a check SHALL confirm no `bakeoff-`-prefixed Batch resources and
   no running Spot instances tagged `Objective = compute-bakeoff` remain.
3. WHEN the spec is closed THEN the throwaway Terraform module MAY be kept in-repo (gated,
   default-off) for repeatability, but no bake-off resources SHALL exist in the account.
