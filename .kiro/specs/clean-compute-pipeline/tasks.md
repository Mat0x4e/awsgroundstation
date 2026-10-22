# Implementation Plan: Clean Compute Pipeline — Whole-Pipeline Migration off CodeBuild

## Overview

Executes ADR-002 as a **strangler**: each stage stands up on AWS Batch alongside its CodeBuild
equivalent, is proven on contact-7 warm data (`dcef1518-c454-401f-bdcd-dc2e73fe89a5`,
`2026/10/17`, Oregon), then the CodeBuild project is retired, in dependency order
(fan-out → aggregation → render). Runs under the Well-Architected Delta Workflow
([`.kiro/steering/wa-delta-workflow.md`](../../steering/wa-delta-workflow.md)): implement →
validate → security-gate → review.

**Delegation (non-negotiable):** all `*.tf` → `terraform` subagent; all `*.py` → `python` /
`python-haiku` / `python-debugger` per routing rules; Checkov → `security-scanner`. The
orchestrator never edits infra or Python directly, and runs Terraform **plan-only** — the
operator applies (SSO + real spend). Image-build CI CodeBuild is **kept**; the runtime CodeBuild
usage is retired (three resources — fan-out and async aggregation share one project).

---

## Stage 1 — Chunk fan-out → Batch EC2 Spot

- [x] 1. **Production Batch module (promote compute_bakeoff Spot pattern — this is a rewrite)**
  - [x] 1.1 Create `infra/modules/batch_compute/` — Spot CE (`c6i/m6i.2xlarge`,
    `SPOT_CAPACITY_OPTIMIZED`, `max_vcpus ≥ 27×8`), fan-out job queue, `chunk` job definition
    (existing ECR image, same `iq_extract.py`→`satdump_process.sh`→upload entrypoint). Outputs to
    `contacts/` (not `bakeoff/`). **Author the Spot `retryStrategy`** (`attempts=2`,
    `evaluateOnExit` RETRY on `Host EC2*`, EXIT otherwise) — it does NOT exist in
    `compute_bakeoff`. Entry clears the chunk prefix before `aws s3 sync` (or `--delete`) so a
    Spot-retry after a partial upload leaves no stale objects. The `compute_bakeoff` module is a
    single throwaway job with no metric path and no on-demand CE — treat this as a rewrite.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.8_
  - [x] 1.2 Least-privilege IAM (service/instance/job/execution): S3 read reception, S3 write
    `contacts/*`, ECR pull sdr image, KMS pipeline key, `PutMetricData` namespace-conditioned,
    logs write. Carry the proven `spot_iam_fleet_role` + `checkov:skip` justifications.
    - _Requirements: 8.1, 8.2, 8.4_
  - [x] 1.3 Wire module into root `main.tf`; gate with a stage flag so it can stand up alongside
    the CodeBuild fan-out during strangler cutover.
    - _Requirements: 1.1, 7.4_

- [x] 2. **Checkpoint — validate + security gate (Stage 1)**
  - `terraform validate` + `plan` (plan-only); `security-scanner` Checkov; HIGH/CRITICAL block.
    Confirm no unconditioned wildcards, S3 write scoped to `contacts/*`. ✅ 53 passed, 0 failed.
    - _Requirements: 8.1, 8.3_

- [ ] 3. **Fan-out orchestration swap (additive) + contact-7 validation**
  - [ ] 3.0 **(DONE — pre-3.1 canary de-risking)** One-chunk contact-7 canary proved the Batch
    EC2 Spot path end-to-end: Spot instance placed in eu-central-1, aws/ebs-encrypted volume,
    ECR image pulled, chunk ran ~23 min (within CodeBuild 8.7-38.9 min baseline), produced
    `npp_hrd.cadu` (56 MB) + 415 SatDump composite objects to the `contacts/` prefix. CE scaled
    back to 0. Surfaced + fixed 6 launch gotchas (Owner-tag SCP, encrypted EBS, CMK-vs-aws/ebs on
    Spot, LT version pinning, 200 GiB root volume, Ref::-vs-containerOverrides) — all backported
    to `.kiro/steering/aws-knowledge.md`. Key input for 3.1: the SFN Map must pass per-item
    values via `ContainerOverrides.Environment[]`, NOT job-def `Ref::` env vars.
  - [x] 3.1 **DONE + DEPLOYED.** Added `ChooseCompute` Choice (branches on injected `use_batch`
    bool) -> `SubmitBatchChunk` (`:::batch:submitJob.sync`, single non-array job per item,
    per-item values via `ContainerOverrides.Environment`) OR unchanged `StartCodeBuild` poll loop.
    `ToleratedFailurePercentage=100` retained; `Catch -> MarkChunkFailed` mirrors CodeBuild. New
    SFN IAM `sfn_batch` (SubmitJob/DescribeJobs/TerminateJob + managed-rule events perms; needed a
    re-apply for IAM propagation). Module cycle broken via deterministic-ARN root locals. Verified
    in the live definition.
    - _Requirements: 1.5_
  - [ ] 3.2 Operator run: apply (flag on), replay contact-7 chunks through Batch fan-out.
    - _Requirements: 1.6, 9.1_
  - [ ] 3.3 Validate: diff per-chunk CADU + composite object count/size vs the CodeBuild baseline
    for contact-7; **plus** fault-inject a Spot reclaim to exercise `retryStrategy`, and run a
    high-chunk-count contact to measure the concurrency win vs the throttled baseline.
    (`python` subagent harness.)
    - _Requirements: 1.6, 9.1, 9.3, 9.4_
  - [ ] 3.4 IF `eu-central-1` Spot capacity thin THEN switch CE to Fargate (job-def
    `platform_capabilities`), re-run, record in ADR-002. (`terraform` subagent.)
    - _Requirements: 1.7, 10.3_
  - [ ] 3.5 Record fan-out measurement ($/contact, wall-clock, Spot-interruptions) in ADR-001;
    update ADR-002 status. (Orchestrator — docs.)
    - _Requirements: 10.1_

- [x] 4. **Retire the fan-out CodeBuild BRANCH only (NOT the shared project)**
  - DONE + DEPLOYED. Removed `ChooseCompute`, `StartCodeBuild`, `WaitForBuild`, `CheckBuildStatus`,
    `EvaluateBuildStatus` from the Map; `StartAt` -> `SubmitBatchChunk`; `sfn_batch` IAM made
    unconditional. Verified live: fan-out is Batch-only, `StartAggregationBuild` + shared
    `aws_codebuild_project.sdr_pipeline` UNTOUCHED (async aggregation still uses them until Stage 2).
    Apply: 0 added, 2 changed, 0 destroyed. Coupling noted: `enable_batch_compute` must stay true
    (Map is Batch-only now).
    - _Requirements: 7.1, 7.5_

---

## Stage 2 — Aggregation → Batch on-demand (async + sync converged)

- [x] 5. **On-demand CE + aggregation job definition (CSPP stage converged, fallback KEPT)**
  - [x] 5.1 DONE+DEPLOYED. On-demand CE (`agg-ce`, EC2 BEST_FIT_PROGRESSIVE, min=0/max=16, reuses
    the 200 GiB encrypted LT + SG + roles), `agg-queue`, `aggregation` job def (pulls
    `aggregation_entrypoint.sh` from `software/`, `RTSTPS=on|off`, 3600s timeout, attempts=1).
    `scripts/aggregation_entrypoint.sh` authored: RTSTPS=on (CADU->RT-STPS->CSPP) / off (RDR->CSPP),
    PERF-1 cache fast-path for BOTH branches + `sdr_luts.sh` fallback KEPT (fixes sync cold-cache
    gap), `/opt/data` cleanup, EXIT-trap log upload, no SELF_STOP/watchdog. CE egress: 443 permitted
    (shared fanout SG). Verified CE/queue VALID/ENABLED, entrypoint in S3.
    - _Requirements: 2.1, 2.2, 2.5_
  - [x] 5.2 DONE. `batch_aggregation_job` role: S3 rw contacts/*, ro software/*, ro lut-cache/*,
    ListBucket 3-prefix, reception ro, KMS, ECR, logs on agg group. (No PutMetricData — agg emits none.)
    - _Requirements: 8.1, 8.2_

- [x] 6. **Checkpoint — validate + security gate (Stage 2)**
  - DONE. `terraform validate` + apply (6 added, 1 changed, 0 destroyed); Checkov 72 passed /
    0 failed / 2 skipped (justified log-retention skips) — GO.
    - _Requirements: 8.3_

- [x] 7. **Converge CSPP stage across branches + validation**
  - [x] 7.1 DONE+DEPLOYED. Async SFN: `CheckResults` injects `use_batch_agg`, `ChooseAggregationCompute`
    routes to `SubmitBatchAggregation` (`:::batch:submitJob.sync`, RTSTPS=on) or keeps the CodeBuild
    poll-loop. `sfn_batch` IAM extended to the agg queue+jobdef.
    - _Requirements: 2.3_
  - [x] 7.2 DONE+DEPLOYED (code). Sync SFN: `StopReceiver` -> `InjectAggFlag` -> `ChooseAggregationCompute`
    -> `SubmitBatchAggregation` (RTSTPS=off, OUTPUT_BUCKET=sync_output). New sync `sfn_batch` IAM.
    **NOT yet runtime-validated — needs a real sync contact (contact-7 is async).**
    - _Requirements: 2.2, 2.3_
  - [x] 7.3 ASYNC VALIDATED on contact-7. Full run SUCCEEDED end-to-end; state path:
    CheckResults -> ChooseAggregationCompute -> **SubmitBatchAggregation** -> StartVisualization ->
    PipelineSucceeded (StartAggregationBuild/CodeBuild NOT entered). Batch aggregation job ran ~21 min
    on-demand, produced CSPP SDR/GEO `.h5` set. Required two fixes found via live runs: (1) SFN
    TimeoutSeconds 5400->10800 (fan-out+Batch-agg exceeds 90 min); (2) SG port-80 egress for CSPP's
    HTTP ancillary LUT fetches from jpssdb.ssec.wisc.edu (PERF-1 cache hit but residual HTTP checks
    remain). Cold-cache run NOT separately forced (cache was warm); **sync-contact validation still
    owed before task 8 retires the sync CSPP project.**
    - _Requirements: 2.4, 9.1, 9.3, 9.4_

- [x] 8. **Retire aggregation CodeBuild (shared project now safe; sync gated on sync validation)**
  - **ASYNC DONE+DEPLOYED (task 8 partial).** Removed `aws_codebuild_project.sdr_pipeline` (shared
    fan-out+async-agg project), its log group, `aws_iam_role.codebuild`+policy, and the
    `InvokeCodeBuild` sfn IAM statement. Async SFN collapsed: `CheckResults -> SubmitBatchAggregation`
    direct; removed `ChooseAggregationCompute`, `StartAggregationBuild`, `WaitForAggregation`,
    `CheckAggregationBuild`, `EvaluateAggregation`, `MarkAggregationFailed`. Apply: 4 destroyed,
    3 changed, 0 added. Verified: shared project GONE; image-build CI, lut_cache_refresh, ECR repo,
    sync module ALL untouched.
  - **HELD:** `groundstation-noaa20-sync-aggregation` CodeBuild + its sync SFN poll-loop — retire
    only after a real sync contact validates `RTSTPS=off`.
  - **FLAGGED (out of scope):** `satdump-noaa20-processing` — the old out-of-IaC CodeBuild project
    (OE-1) is now dead (fan-out on Batch) but not in Terraform; needs separate deliberate cleanup.
    - _Requirements: 7.2, 7.4, 7.5_

---

## Stage 3 — Visualization render → Batch on-demand + NASA bug fixes

- [x] 9. **NASA render bug fixes — test-first (`python` subagent)**
  - [x] 9.1 DONE. First `scripts/viirs/` test coverage added (`tests/test_nasa_render_bugs.py`,
    12 tests, synthetic arrays/filenames). All 12 FAILED pre-fix, PASS post-fix.
  - [x] 9.2 DONE (Bug #1). `visualize_nasa.py`: added `_parse_granule_token` (regex `tHHMMSSS`) +
    `_reconcile_granule_sets` (token intersection across band+geo lists); wired into `main()` for
    both True Color (i1∩i2∩i3∩igeo) and Thermal (m15∩mgeo); drops orphan granules with a log; bbox
    from matched set only.
  - [x] 9.3 DONE (Bug #2). `cartopy_renderer.py`: added `_sanitise_thermal_coords` masking
    non-finite lat/lon (+band) before `pcolormesh`; existing all-NaN guard kept. (Tested via the
    extracted helper — cartopy not importable in venv.)
  - [x] 9.4 DONE (Bug #3). Single-stretch fix: orchestrator now `destripe(rawIN)` (not
    `destripe(contrast_stretch(IN))`); `destripe` uses `np.ma.median` preserving mask so masked
    pixels don't corrupt per-detector medians; `assemble_true_color` does the single stretch+gamma.
    Each band stretched exactly once. Also retired the task-8-stale `test_sdr_pipeline_stub_exits_nonzero`
    (its CodeBuild project was deleted). Full suite: 287 passed, 5 pre-existing skips, 0 failures.
    - _Requirements: 4.1-4.4, 5.1-5.3, 6.1-6.3_

- [ ] 10. **Render entrypoint relocation + job definition + Lambda trigger swap**
  - [ ] 10.1 Author `render_entrypoint.sh` baked into the render image, lifting the ~100+ lines
    of stage logic from `SATDUMP_BUILDSPEC` + `NASA_BUILDSPEC` (selective `s3 sync --include`
    globs, per-chunk flatten `cp -n` loop, output sync, `ProductsWritten` post-sync metric,
    `VisualizationFailed`-on-failure metric + SNS), `RENDER_BRANCH=satdump|nasa` selects branch.
    Add `render` job definition on the on-demand CE. (`terraform` + `python` entrypoint.)
    - _Requirements: 3.2, 3.3, 3.4, 3.5_
  - [ ] 10.2 `viirs-orchestrator` Lambda: replace `codebuild.start_build(buildspecOverride=...)`
    with `batch.submit_job(..., RENDER_BRANCH=...)` **behind an env flag** (CodeBuild path kept
    until 12 passes); `_lookup_acquisition` stays in the Lambda and feeds a param; preserve
    products-survive-render-failure. (`python` subagent.)
    - _Requirements: 3.1, 3.4_
  - [ ] 10.3 IAM for the render job role (S3 write `products/*`, KMS, ECR, logs,
    namespace-conditioned metrics) + a CloudWatch alarm consuming `VisualizationFailed` / Batch
    `FAILED` render jobs → shared SNS (the explicit watcher).
    - _Requirements: 3.4, 8.1, 8.2_

- [ ] 11. **Checkpoint — validate + security gate (Stage 3)**
  - plan-only + Checkov; HIGH/CRITICAL block. Run the full `scripts/viirs/` test suite green.
    - _Requirements: 8.3_

- [ ] 12. **Both-branch contact-7 render validation**
  - Operator run: render both branches on contact-7 → assert `.png` + `.geotiff` + `.json`
    produced for each; NASA branch now produces thermal PNG + GeoTIFF (bug #2) with intersection
    bbox (bug #1) and no banding (bug #3). `verify_pass.py` reconciliation passes.
    - _Requirements: 3.3, 3.6, 4.1, 5.2, 6.1, 9.2, 9.4_

- [ ] 13. **Retire visualization CodeBuild**
  - Remove `groundstation-noaa20-viirs-visualization` + IAM; keep image-build CI; update
    `docs/ARCHITECTURE.md`.
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

---

## Finalisation

- [ ] 14. **Full end-to-end contact-7 validation (all stages on Batch)**
  - Operator run: contact-7 end-to-end through fan-out → aggregation → render, both branches, to
    png/geotiff/json. `verify_pass.py` passes. IF a stage could not be validated on contact-7
    THEN record the justification in `docs/CONTACTS.md` and book a new pass (COST-1 guardrail).
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [ ] 15. **Final checkpoint — security + review + docs**
  - [ ] 15.1 `security-scanner` Checkov on the whole `infra/`; confirm no net-new HIGH/CRITICAL;
    confirm `terraform plan` shows **no runtime CodeBuild** (image-build CI only).
    - _Requirements: 7.3, 8.3_
  - [ ] 15.2 Update ADR-001 (measured fan-out result) + ADR-002 (status Accepted, any Fargate
    fallbacks); replace the CodeBuild pipeline diagram with the all-Batch diagram (both branches,
    to png/geotiff/json) in `docs/ARCHITECTURE.md` + `docs/SYNC_PIPELINE.md`. (Orchestrator.)
    - _Requirements: 10.1, 10.2, 10.3, 10.4_
  - [ ] 15.3 Tear down the leftover `oneoff-nasa-render` ECS cluster (cluster, task def, roles
    `oneoff-nasa-render-exec`/`-task`, SG `sg-0eaa0ef333d4bfdc3`, log group `/ecs/oneoff-nasa-render`)
    now that render is codified on Batch. (Operator / `terraform`.)
    - _Requirements: 7.3_

## Notes

- `*` tasks are optional (test-only), but 9.x bug-fix tests are **required** (Req 4–6 mandate
  test-first) — only 9.1 (harness scaffolding) is marked optional.
- Operator-run tasks (3.2, 3.4, 5.x apply, 7.3, 12, 14, 15.3) incur real spend / touch the
  account; the agent prepares and plans, the operator applies after `aws sso login`.
- A new paid pass is a last resort (Req 9.3) — contact-7 is warm and, for the NASA render, is
  the ideal mismatch input for bug #1.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2"] },
    { "id": 2, "tasks": ["3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5"] },
    { "id": 4, "tasks": ["4"] },
    { "id": 5, "tasks": ["5.1", "5.2"] },
    { "id": 6, "tasks": ["6"] },
    { "id": 7, "tasks": ["7.1", "7.2", "7.3"] },
    { "id": 8, "tasks": ["8"] },
    { "id": 9, "tasks": ["9.1", "9.2", "9.3", "9.4"] },
    { "id": 10, "tasks": ["10.1", "10.2", "10.3"] },
    { "id": 11, "tasks": ["11"] },
    { "id": 12, "tasks": ["12"] },
    { "id": 13, "tasks": ["13"] },
    { "id": 14, "tasks": ["14"] },
    { "id": 15, "tasks": ["15.1", "15.2", "15.3"] }
  ]
}
```
