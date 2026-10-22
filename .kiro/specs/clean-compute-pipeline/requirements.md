# Requirements: Clean Compute Pipeline — Whole-Pipeline Migration off CodeBuild

## Introduction

This spec executes **ADR-002** (in [`docs/ARCHITECTURE_REFINEMENT.md`](../../../docs/ARCHITECTURE_REFINEMENT.md)):
migrate **every runtime compute stage** of the NOAA-20 SDR pipeline — both the async DigIF
branch and the sync demod branch, end-to-end to `.png` + `.geotiff` + `.json` — off AWS
CodeBuild onto **AWS Batch**, and fix the three silent NASA render-correctness defects as part
of the render-stage migration.

It supersedes the fan-out-only direction of ADR-001 by widening it to aggregation and
visualization render. The `compute-bakeoff-batch-spot` spec's Spot measurement is folded in as
the **first strangler step** (it is both the ADR measurement and the fan-out acceptance test),
not a separate pre-gate.

**Execution model: strangler, committed to EC2 Spot, Fargate as per-stage fallback.** Each
stage stands up on Batch alongside its CodeBuild equivalent, is proven on **contact-7's
still-warm S3 data** (`dcef1518-c454-401f-bdcd-dc2e73fe89a5`, `2026/10/17`, Oregon), then the
CodeBuild project is retired. A new paid pass is booked only if a stage cannot be validated on
warm data.

**Non-negotiables carried from steering:**
- All `*.tf` changes are delegated to the `terraform` subagent; all `*.py` to the `python`
  subagents. The orchestrator never edits infra or Python directly.
- Checkov HIGH/CRITICAL findings block a task (`enable_compute_bakeoff` pattern continues).
- `scripts/viirs/` has **zero tests today**; the render-correctness work adds the first tests
  and every bug fix is gated by a test that fails against the broken code first.
- S3 is the source of truth; outputs are byte/structure-comparable across the platform swap.

## Scope boundary

| In scope | Out of scope |
|---|---|
| Chunk fan-out → AWS Batch on EC2 Spot | Live-demod RT-STPS (stays on receiver EC2 — not batch) |
| Aggregation (async + sync, converged) → AWS Batch on-demand, PERF-1 cache | The ECR `sdr-pipeline` image contents (unchanged) |
| Visualization render (both branches) → AWS Batch on-demand | Ground Station booking/feasibility/scheduler logic |
| Fix 3 NASA render bugs + first `scripts/viirs/` tests | New satellite / mission profile support |
| Retire runtime CodeBuild (shared `sdr_pipeline` + sync CSPP + viirs render) | Cost Explorer dashboards beyond the measurement |
| Validate every stage on contact-7 warm data | CSPP/SatDump algorithm changes |

---

## Requirement 1 — Chunk fan-out runs on AWS Batch on EC2 Spot

**User story:** As an architect, I want the chunk fan-out to run on AWS Batch on EC2 Spot
instead of CodeBuild, so the pipeline uses a batch-native engine with a raisable concurrency
quota and Spot economics for idempotent work.

#### Acceptance Criteria
1. WHEN a contact's chunks are processed THEN they SHALL run as AWS Batch array jobs on an EC2
   Spot managed compute environment, not as CodeBuild builds.
2. WHEN the Batch job runs THEN it SHALL use the existing `${project_name}-sdr-pipeline` ECR
   image and the same per-chunk entrypoint (pcap→cs8 via `iq_extract.py` → SatDump via
   `satdump_process.sh` → upload) the CodeBuild buildspec used.
3. WHEN the compute environment is sized THEN `max_vcpus` SHALL allow all chunks of a typical
   contact (≤27) to run concurrently, removing the CodeBuild account-build-cap throttle.
4. WHEN a Spot instance is reclaimed mid-chunk THEN the chunk SHALL be retried (Batch
   `retryStrategy`), relying on chunk idempotence and the Map's existing 100% failure tolerance.
5. WHEN the state machine orchestrates fan-out THEN it SHALL keep the per-item `Map` and route
   each item (via an additive Choice flag) to either the existing CodeBuild poll-loop branch
   **or** a new single-container Batch job submitted with `:::batch:submitJob.sync` — **not** a
   Batch array job (an array job does not compose with the per-item Map semantics). Outputs use
   the same `contacts/` prefix structure. The CodeBuild branch is removed only after validation
   (strangler), not in the same change.
6. WHEN the fan-out is validated THEN it SHALL be proven on contact-7 warm data with
   byte/structure-comparable per-chunk outputs (CADU + composites) versus the CodeBuild baseline,
   AND the Spot `retryStrategy`/`evaluateOnExit` SHALL be validated by fault injection (a warm
   replay does not interrupt), AND the concurrency win SHALL be measured on a high-chunk-count run.
7. IF `eu-central-1` Spot capacity for the `c6i/m6i.2xlarge` shape is unreliable during
   validation THEN the stage SHALL fall back to Batch-on-Fargate (job-definition/CE change,
   same image) and the fallback SHALL be recorded in ADR-002.
8. WHEN a chunk is retried after a partial upload THEN the entrypoint SHALL clear the chunk
   prefix before `aws s3 sync` (or sync with `--delete`) so a Spot-retry cannot leave stale
   objects — the idempotence claim is enforced, not assumed.

---

## Requirement 2 — Aggregation runs on AWS Batch on-demand; CSPP stage converged across branches

**User story:** As an architect, I want aggregation to run as AWS Batch on-demand jobs that
share one cached CSPP stage across the async and sync branches, so there is no run-time internet
dependency on the hot path while respecting that the two branches enter aggregation differently.

> **Note:** async (`aggregation.yml`) reads `.cadu` and runs **RT-STPS + CSPP**; sync
> (`aggregation_sync.yml`) reads pre-made **RDR** and runs **CSPP-only**. They converge on the
> CSPP stage, selected by a job parameter (`RTSTPS=on|off`), not on one monolithic job.

#### Acceptance Criteria
1. WHEN aggregation runs THEN it SHALL execute as an AWS Batch **on-demand** job (not CodeBuild),
   using the existing ECR image and an `aggregation_entrypoint.sh` baked into the image that
   ports the `aggregation.yml` logic (preserving the S3-pulled `aggregation.sh` indirection,
   `/opt/data` cleanup, and EXIT-trap log upload), parameterised by `RTSTPS=on|off`.
2. WHEN aggregation needs LUTs/ancillary data THEN it SHALL use the PERF-1 S3 cache fast-path
   (`lut-cache/SDR_4_1_DB/`) when present but SHALL **keep** the `sdr_luts.sh` internet fallback
   for a cold cache (the sync branch has never had the cache and its first Batch run is cold by
   definition). The on-demand CE egress posture SHALL be stated: 443 egress permitted so the
   fallback works; NOT hardened to VPC-endpoints-only in this spec.
3. WHEN aggregation is dispatched THEN the async state machine (`RTSTPS=on`) and the **sync SFN's
   `StartAggregationBuild` state** (`RTSTPS=off`; this is the sync SFN state that today dispatches
   the sync CSPP CodeBuild, not the receiver-side `FinishReceiver` RT-STPS step) SHALL both
   target the same Batch job definition via additive flag-gated branches (CodeBuild kept until
   validated).
4. WHEN async aggregation is validated THEN it SHALL be proven on contact-7 warm data producing
   the same CSPP SDR/GEO file set (38 files for contact-7) as the CodeBuild baseline. WHEN the
   sync CSPP convergence is validated THEN it SHALL be proven on a real sync contact or a
   replayed past sync RDR set — **contact-7 is async and cannot validate the sync path**.
5. WHEN a single-run failure occurs THEN on-demand (not Spot) SHALL be used, because the
   aggregation job is long and not cheaply restartable mid-run; the CE SHALL set `min_vcpus = 0`
   so no idle EC2 runs between contacts.

---

## Requirement 3 — Visualization render runs on AWS Batch on-demand; Lambda keeps only the trigger

**User story:** As an operator, I want the visualization render (both SatDump and NASA branches,
producing `.png` + `.geotiff` + `.json`) to run as an AWS Batch on-demand job, with the
visualizer Lambda reduced to a trigger, so the tangled fire-and-forget Lambda→CodeBuild
dispatch is replaced by an observable batch job.

#### Acceptance Criteria
1. WHEN visualization is triggered THEN the `viirs-orchestrator` Lambda SHALL submit an AWS
   Batch job (`SubmitJob`) instead of calling CodeBuild `start-build` with a `buildspecOverride`,
   **behind an env flag** so the CodeBuild path stays available until render validates (strangler).
2. WHEN the render job runs THEN its stage logic SHALL live in an `render_entrypoint.sh` baked
   into the render image (carrying the selective `s3 sync --include` globs, the per-chunk flatten
   loop, output sync, and metric emission lifted from the `SATDUMP_BUILDSPEC` / `NASA_BUILDSPEC`
   string constants), with the branch selected by a job parameter (`RENDER_BRANCH=satdump|nasa`)
   — "Lambda keeps only the trigger" requires this relocation, which is an explicit task, not a
   side effect. The acquisition-time lookup (`_lookup_acquisition`) MAY stay in the Lambda and
   feed a param.
3. WHEN either branch completes THEN it SHALL produce the full output triple — `.png`,
   `.geotiff`, and `.json` metadata — uploaded to the `products/` prefix.
4. WHEN a render fails THEN the failure SHALL be observable AND consumed: the job emits a
   `VisualizationFailed` metric + SNS, and a CloudWatch alarm on that metric (and on Batch
   `FAILED` render jobs) is the explicit watcher — a fire-and-forget submit without a watcher is
   not acceptable. Products still survive a render failure (REL-2 behaviour preserved).
5. WHEN the `ProductsWritten` metric is emitted THEN it SHALL come from `render_entrypoint.sh`
   after the S3 upload (the post_build pattern from REL-4), reflecting actual uploaded files.
6. WHEN render is validated THEN both branches SHALL be proven on contact-7 warm data.

---

## Requirement 4 — NASA render granule-count mismatch is fixed, with tests

**User story:** As a user, I want the NASA render to handle the common case where band and
geolocation granule counts differ, so the render does not crash or silently mis-align.

#### Acceptance Criteria
1. WHEN `visualize_nasa.py` discovers HDF5 inputs THEN it SHALL **introduce granule-token
   parsing** (parse the granule-start token, e.g. `tHHMMSSS`, from each CSPP filename —
   `_find_hdf5_files` has none today, it only sorts paths) and reconcile the band-token set
   against the geolocation-token set so that only granules present in **both** are rendered
   (contact-7 has SVI/SVM for one granule but GITCO/GMTCO for five — the mismatch that produced
   the `(29491200,) (49152000,)` broadcast error). This is new logic, not a tweak.
2. WHEN tokens mismatch THEN the code SHALL select the intersection (matched granules) rather
   than broadcasting mismatched arrays, and SHALL log which granules were dropped and why.
3. WHEN the bounding box is computed THEN it SHALL reflect only the matched granules, not the
   over-wide 4525 km / lat 30–70 box produced by the unreconciled set.
4. WHEN this fix lands THEN a test on `scripts/viirs/` SHALL reproduce the mismatch case (1 band
   granule + 5 geo granules, realistic CSPP names) and FAIL against the current (unreconciled)
   code, then PASS after the fix.

---

## Requirement 5 — NASA thermal render NaN-coordinate crash is fixed, with tests

**User story:** As a user, I want the NASA thermal PNG to render, so the thermal branch produces
output instead of silently failing on non-finite coordinates.

#### Acceptance Criteria
1. WHEN `cartopy_renderer.py render_nasa` renders the thermal (`is_thermal`) branch THEN it
   SHALL guard against non-finite lat/lon before passing coordinates to `pcolormesh` (which
   forbids NaN/inf coordinates), masking or dropping non-finite cells.
2. WHEN the thermal branch runs on real data THEN it SHALL produce a thermal PNG (the branch
   currently never renders).
3. WHEN this fix lands THEN a test SHALL feed lat/lon arrays containing NaN/inf, FAIL against
   the current unguarded code (via the pcolormesh contract), then PASS after the guard.

---

## Requirement 6 — NASA true-color double-stretch banding is fixed, with tests

**User story:** As a user, I want destriping to run on real radiance and the image stretched
exactly once, so the per-detector correction removes banding instead of creating it.

> **Corrected root cause (WA review):** the bug is a **double-stretch**, not merely order.
> Today the orchestrator does `destripe(contrast_stretch(i1))` and then `assemble_true_color`
> stretches **and** gamma-corrects **again** internally (`gamma_correct(contrast_stretch(i1))`).
> Destriping therefore runs on stretched data, and the data is stretched twice.

#### Acceptance Criteria
1. WHEN `visualize_nasa.py` / `image_renderer.py` processes a NASA true-color image THEN
   `destripe` SHALL run on **raw** reflectance (mask preserved), the orchestrator's redundant
   `contrast_stretch` SHALL be removed, and `assemble_true_color` SHALL perform the single
   stretch — so per-detector medians are computed on real radiance and the data is stretched
   exactly once.
2. WHEN the corrected path runs THEN per-detector medians SHALL be computed from unmasked source
   pixels only, and the mask SHALL be reapplied correctly after destriping (destripe returns a
   plain ndarray today).
3. WHEN this fix lands THEN a test SHALL construct an image whose per-detector medians are
   corrupted by the current stretch→destripe→stretch path, FAIL against the current code, then
   PASS after the single-stretch fix.

---

## Requirement 7 — Runtime CodeBuild is retired (respecting the shared project)

**User story:** As a maintainer, I want CodeBuild removed from the runtime pipeline once each
stage is proven on Batch, so the "build tool as batch engine" mismatch is gone — without
deleting a project another un-migrated stage still depends on.

> **Critical fact (verified in `step_functions.tf`):** the fan-out Map body (`StartCodeBuild`)
> and the async aggregation step (`StartAggregationBuild`) both use the **same**
> `aws_codebuild_project.sdr_pipeline` + its IAM role. The runtime CodeBuild surface is: (1) the
> shared `sdr_pipeline` project (fan-out **and** async aggregation), (2) sync CSPP
> `groundstation-noaa20-sync-aggregation`, (3) viirs render `groundstation-noaa20-viirs-visualization`.

#### Acceptance Criteria
1. WHEN fan-out is validated on Batch (Stage 1) THEN only the **CodeBuild branch in the Map
   body** SHALL be removed — the shared `sdr_pipeline` project and its IAM role SHALL remain,
   because async aggregation still uses them. Nothing is "orphaned" by Stage 1 alone.
2. WHEN async aggregation is validated on Batch (Stage 2) THEN the shared `sdr_pipeline`
   CodeBuild project **and** its IAM role SHALL be removed (both consumers are now off it). The
   sync CSPP project SHALL be removed **only after the sync path is validated on a real sync
   contact** (not contact-7).
3. WHEN render is validated on Batch (Stage 3) THEN `groundstation-noaa20-viirs-visualization`
   and its IAM role SHALL be removed.
4. WHEN all runtime stages are migrated THEN `terraform plan` SHALL show no runtime CodeBuild
   (image-build CI only), with no orphaned IAM roles, and `docs/ARCHITECTURE.md` +
   `docs/SYNC_PIPELINE.md` SHALL be updated to the Batch topology per stage.
5. WHEN a project/branch is retired THEN it SHALL be retired **only after** its Batch
   replacement passed validation (strangler ordering: fan-out branch → shared project after
   async agg → sync CSPP after sync validation → render).

---

## Requirement 8 — Least privilege and security parity on all new Batch resources

**User story:** As a security reviewer, I want every new Batch role/policy to be least-privilege
and Checkov-clean, so the migration does not regress the Well-Architected security posture.

#### Acceptance Criteria
1. WHEN Batch IAM roles are created (service, instance, job, execution) THEN each SHALL be
   scoped to the minimum: S3 read on reception bucket, S3 write on the relevant output prefix,
   ECR pull on the sdr image repo, KMS on the pipeline key, logs write on the stage log group.
2. WHEN `cloudwatch:PutMetricData` is granted to a Batch job role THEN it SHALL be conditioned
   on `cloudwatch:namespace = SDRPipeline` (parity with REL-4 / SEC-1).
3. WHEN `terraform` validates the new module(s) THEN `checkov -d . --quiet` SHALL report no net
   new HIGH/CRITICAL findings; any accepted LOW/MEDIUM SHALL carry a `checkov:skip` with
   justification.
4. WHEN Batch instances run THEN they SHALL use an instance profile (not long-lived keys) and
   the pipeline KMS key for any encrypted I/O.

---

## Requirement 9 — Validation on contact-7 warm data, new pass only if necessary

**User story:** As an owner, I want each migrated stage proven on the still-warm contact-7 data
before any new paid pass, so the migration is validated at zero additional antenna cost where
possible.

#### Acceptance Criteria
1. WHEN a stage is migrated THEN it SHALL be validated by replaying contact-7's S3 data
   (`dcef1518-c454-401f-bdcd-dc2e73fe89a5`) through the new Batch stage and comparing outputs to
   the known-good CodeBuild baseline for that contact.
2. WHEN the end-to-end pipeline is assembled THEN it SHALL be validated on contact-7 data
   through to `.png` + `.geotiff` + `.json` for both render branches.
3. WHEN a project is retired THEN the failure modes contact-7 does NOT exercise SHALL each be
   covered first: (a) Spot reclaim/`retryStrategy` via fault injection; (b) the concurrency
   ceiling via a high-chunk-count run measured against the throttled CodeBuild baseline; (c) a
   **cold LUT cache** via an emptied-cache aggregation run; (d) the **sync branch** on a real
   sync contact or replayed sync RDR set (contact-7 is async). A new paid pass MAY be booked only
   if a path cannot be validated on warm/replayed data, justification recorded in
   `docs/CONTACTS.md` before booking (COST-1 guardrail).
4. WHEN validation uses `verify_pass.py` THEN the reconciliation (S3 truth + metric
   corroboration) SHALL pass for the Batch-produced outputs.

---

## Requirement 10 — Decision record and runbooks stay true

**User story:** As a maintainer, I want the decision records and runbooks updated as each stage
cuts over, so the docs never drift from the deployed Batch topology.

#### Acceptance Criteria
1. WHEN the fan-out Spot measurement completes THEN its real $/contact + wall-clock + Spot
   interruption count SHALL be appended to ADR-001 (replacing the estimates) and ADR-002's
   status updated.
2. WHEN each stage cuts over THEN `docs/ARCHITECTURE.md` (async) and `docs/SYNC_PIPELINE.md`
   (sync) SHALL be updated to the Batch topology in the same change.
3. WHEN a Fargate fallback is taken for any stage THEN the reason (observed Spot capacity) SHALL
   be recorded in ADR-002.
4. WHEN the spec completes THEN a final ASCII/diagram of the all-Batch pipeline (both branches,
   through to png/geotiff/json) SHALL be in the docs, replacing the CodeBuild diagram.
