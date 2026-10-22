# Design: Clean Compute Pipeline — Whole-Pipeline Migration off CodeBuild

## Overview

Execute ADR-002: move every runtime compute stage of the NOAA-20 SDR pipeline onto AWS Batch,
retire the runtime CodeBuild usage (three resources — see the correction below; fan-out and
async aggregation share one project), and fix the three silent NASA render defects with
first-ever tests on `scripts/viirs/`. The ECR `sdr-pipeline` image is unchanged — every stage
already runs it. The migration is a **strangler**: each stage stands up on Batch alongside its
CodeBuild equivalent, is proven on contact-7 warm data, then the CodeBuild project is retired,
in dependency order (fan-out → aggregation → render).

> **Image-build CI is kept.** Only the *runtime* CodeBuild usage is retired. The project that
> builds and pushes the ECR image remains as CI (it is not a runtime stage).

> **CRITICAL CORRECTION (from WA review, verified against `step_functions.tf`).** There are
> **not** four cleanly separable runtime CodeBuild projects. The fan-out Map body
> (`StartCodeBuild`) **and** the async aggregation step (`StartAggregationBuild`) both target
> the **same** resource `aws_codebuild_project.sdr_pipeline` with two different inline
> buildspecs. So fan-out and async aggregation are **one shared project + one shared IAM role**.
> The runtime CodeBuild surface is really: (1) the shared `sdr_pipeline` project used by BOTH
> fan-out and async aggregation, (2) the sync CSPP project `groundstation-noaa20-sync-aggregation`,
> (3) the viirs render project `groundstation-noaa20-viirs-visualization`. The shared project
> and its role can be deleted **only after both fan-out and async aggregation are off it**
> (end of Stage 2), never at the end of Stage 1.

> **CRITICAL CORRECTION (SFN integration).** The async fan-out does **not** use
> `:::batch:submitJob.sync` today and does not use `.sync` CodeBuild either — the Map body is a
> 6-state poll loop (`StartCodeBuild` via `aws-sdk:codebuild:startBuild` → `WaitForBuild` →
> `CheckBuildStatus` → `EvaluateBuildStatus` → `BuildSucceeded`/`MarkChunkFailed`). The
> migration keeps the **per-item `Map`** (it carries `ItemSelector`, `chunk_id`, failure
> accounting, and sits downstream of the `.processing` idempotence marker) and submits **one
> single-container Batch job per item** via `:::batch:submitJob.sync` — which collapses the
> 6-state poll loop into one synchronous task. We do **NOT** use a Batch *array* job: an array
> job is one `SubmitJob` returning one terminal state for N children and does not compose with
> the per-item Map semantics (`ToleratedFailurePercentage`, per-item retry/catch).

## Target topology (all-Batch runtime)

```
                 ┌──────────────────────── AWS Batch (one family) ────────────────────────┐
  S3 reception   │  Spot CE  ──►  fan-out queue   ──►  chunk array jobs  (SatDump)         │
  (DigIF chunks) │  OnDem CE ──►  agg/render queue ─►  aggregation job   (RT-STPS + CSPP)  │
                 │                                 └►  render job        (SatDump | NASA)  │
                 └──────────────────────────────────────────────────────────────────────┘
       Step Functions  :::batch:submitJob.sync   │   Lambda trigger → SubmitJob (render)
       Receiver EC2 (live UDP demod, RT-STPS)  — UNCHANGED, not batch —
```

Two compute environments share one Batch family:
- **Spot CE** (`c6i/m6i.2xlarge`, `SPOT_CAPACITY_OPTIMIZED`) — fan-out array jobs only.
- **On-demand CE** (same instance family) — aggregation + render jobs (single long jobs).

One job queue per CE. Three job definitions: `chunk` (Spot), `aggregation` (on-demand),
`render` (on-demand). The `compute_bakeoff` module is the proven scaffold for the Spot CE +
`chunk` job definition; this spec promotes that pattern from throwaway to production and adds
the on-demand CE + two job definitions.

## Why this shape

- **Spot for fan-out only.** Chunks are idempotent and the Map tolerates 100% failure → a Spot
  reclaim costs one cheap retry. Aggregation and render are single long jobs where a mid-run
  reclaim wastes the whole run → on-demand.
- **One image, parameter-selected branch.** The render job replaces the `SATDUMP_BUILDSPEC` /
  `NASA_BUILDSPEC` string-constant override with a job parameter (`RENDER_BRANCH=satdump|nasa`).
- **Lambda keeps only the trigger.** The `viirs-orchestrator` Lambda swaps `codebuild.start_build`
  for `batch.submit_job`; it stays fire-and-forget at the trigger but the Batch job's terminal
  state is now observable (REL-2 signal preserved and strengthened).
- **Aggregation convergence.** Async and sync aggregation run the identical CSPP+RT-STPS work;
  sync currently lacks the PERF-1 LUT cache. One `aggregation` job definition, PERF-1-cached,
  serves both — the sync finish step and the async state machine both `SubmitJob` to it.

## Stage-by-stage design

### Stage 1 — Chunk fan-out → Batch EC2 Spot  (Req 1)

| Aspect | Design |
|---|---|
| Compute | Promote `infra/modules/compute_bakeoff` Spot CE pattern to a production module (`infra/modules/batch_compute/`). **This is a rewrite, not a copy:** the bakeoff module is a single throwaway job writing to `bakeoff/contacts/...`; production needs the `contacts/` prefix, the `ProductsWritten`/metric path (bakeoff has NONE), the on-demand CE + 2 extra job defs (bakeoff has none), and a real Spot `retryStrategy` (bakeoff has none). |
| Job def | `chunk` — existing ECR image, same entrypoint (`iq_extract.py` → `satdump_process.sh` → upload), `resourceRequirements` 8 vCPU / 16 GiB. Entry clears the chunk prefix before `aws s3 sync` (or syncs with `--delete`) so a Spot-retry after a partial upload cannot leave stale objects — closes the "sync is additive" gap the WA review flagged. |
| Spot retry (EXPLICIT) | Batch job-definition `retryStrategy { attempts = 2, evaluateOnExit = [{ onStatusReason = "Host EC2*", action = "RETRY" }, { onReason = "*", action = "EXIT" }] }` so a Spot reclaim (`Host EC2 was terminated`) retries but a genuine processing failure exits. **This does not exist in `compute_bakeoff` and must be authored.** |
| Concurrency | `max_vcpus` ≥ 27×8 so all chunks run at once (removes CodeBuild cap) |
| Orchestration (EXPLICIT, additive) | Keep the per-item `Map`. Add a **Choice gate** at the top of the Map body (`UseBatch` flag from execution input / SSM) that routes to either the existing `StartCodeBuild` 6-state poll loop (unchanged) **or** a new single `SubmitBatchChunk` state (`:::batch:submitJob.sync`, one non-array job per item). The CodeBuild body stays wired until Stage-1 validation passes — honouring the strangler. Only Task 4 removes the CodeBuild branch. |
| Validation | Replay contact-7 chunks; diff per-chunk CADU + composite object count/size vs CodeBuild baseline |
| Fallback | If `eu-central-1` Spot thin → flip CE `type` to Fargate (job-def `platform_capabilities`); record in ADR-002 |

The compute-bakeoff Spot run (if not already executed) is **strangler step 1a** and doubles as
the ADR-001 measurement. Fault-injection note: the Spot `retryStrategy` is validated by forcing
a reclaim (or an injected `evaluateOnExit` test), because a warm contact-7 replay will not
naturally interrupt.

### Stage 2 — Aggregation → Batch on-demand  (Req 2)

> **CORRECTION (from WA review).** Async and sync aggregation are **not** "identical CSPP+RT-STPS
> work." Async (`buildspecs/aggregation.yml`) reads `.cadu` from reception, runs **RT-STPS +
> CSPP**, and already has the PERF-1 cache branch. Sync (`buildspecs/aggregation_sync.yml`) reads
> **pre-made RDR** (RT-STPS already ran on the live receiver EC2 with unmodified `jpss1.xml`),
> runs **CSPP-only**, and has **no** cache branch (always `sdr_luts.sh`). They converge on the
> **CSPP stage**, not the whole job. The sync RDR is produced differently upstream, so this is a
> redesign with a mode switch, not a wiring swap.

| Aspect | Design |
|---|---|
| Compute | On-demand CE (same instance family), `min_vcpus = 0` (no idle EC2), separate agg/render queue |
| Job def | `aggregation` — existing image; one entrypoint script `aggregation_entrypoint.sh` baked into the image, parameterised by `RTSTPS=on\|off`: `on` = async (CADU→RT-STPS→CSPP), `off` = sync (RDR→CSPP). Both share the cached CSPP stage. |
| LUT cache (SAFE) | PERF-1 fast-path (`lut-cache/SDR_4_1_DB/`) used when present, but the `sdr_luts.sh` **internet fallback is KEPT** (cache-hit/miss branch, as `aggregation.yml` already does). Dropping the fallback on a cold cache would hard-fail aggregation — the sync branch has **never** had the cache, so its first Batch run is a guaranteed cold path. Egress posture is stated explicitly: the on-demand CE SG permits 443 egress so the fallback works; it is NOT hardened to VPC-endpoints-only in this spec. |
| Entrypoint port | `aggregation.yml` is phase-shaped (pre_build: LUT stage + tarball extract + cache branch; build: `aggregation.sh` pulled from S3; post_build: log tail + EXIT-trap upload). Porting to one `bash -c` must preserve: the S3-pulled `aggregation.sh` indirection, `/opt/data` cross-run cleanup, and the EXIT-trap log upload. Tracked as its own sub-task, not a one-liner. |
| Orchestration | Async: SFN `StartAggregationBuild` → additive Choice → `:::batch:submitJob.sync` (`RTSTPS=on`), CodeBuild branch kept until validated. Sync: the **sync SFN's `StartAggregationBuild` state** (which dispatches `sync_aggregation` CodeBuild today, NOT the `FinishReceiver` SSM/RT-STPS step) → `submit_job` (`RTSTPS=off`). |
| Validation | Async on contact-7 (same 38 CSPP SDR/GEO files vs baseline). **Sync path cannot be validated on contact-7 (it is an async contact)** — sync convergence requires a real sync contact or a replay of a past sync RDR set; recorded as a validation gap to close before retiring the sync project (Req 9.3). |
| Spot? | No — one long non-restartable job → on-demand. |

### Stage 3 — Visualization render → Batch on-demand, both branches, 3 bug fixes  (Req 3–6)

> **CORRECTION (from WA review).** The buildspecs `SATDUMP_BUILDSPEC` / `NASA_BUILDSPEC` in
> `handler.py` **are the stage**, not a thin wrapper: ~100+ lines each carrying the selective
> `aws s3 sync --include` globs, the per-chunk flatten loop (`cp -n` lowest-chunk-wins), the
> `__CONTACT_TIME_ARG__` / `__PASS_DURATION_ARG__` string substitution the Lambda does at
> dispatch, the output sync, and the `ProductsWritten` post-sync metric. "Lambda keeps only the
> trigger" is only true if **all of that moves into an image entrypoint** (`render_entrypoint.sh`,
> two branches), with the Lambda passing params (`RENDER_BRANCH`, `CONTACT_TIME`,
> `CHUNK_DURATION_S`, `ENABLE_GEOTIFF`). The acquisition-time lookup (`_lookup_acquisition`,
> reading `dataset.json` cadence) stays Python in the Lambda and feeds a param. This relocation
> is a first-class task, not a side effect.

| Aspect | Design |
|---|---|
| Compute | On-demand CE / agg-render queue |
| Entrypoint | `render_entrypoint.sh` baked into the render image, carrying the stage/flatten/sync/metric logic lifted verbatim from both buildspec constants; `RENDER_BRANCH=satdump\|nasa` selects the branch |
| Job def | `render` — render image + `scripts/viirs/`; params replace the inline `buildspecOverride` |
| Trigger | `viirs-orchestrator` Lambda: `batch.submit_job` replaces `codebuild.start_build(buildspecOverride=...)`, **behind an env flag** so the CodeBuild path stays one toggle away until render validates (strangler); `_lookup_acquisition` unchanged |
| Outputs | `.png` + `.geotiff` + `.json` to `products/{date}/{contact}/` |
| Observability | `ProductsWritten` emitted from `render_entrypoint.sh` after `s3 sync` (REL-4 pattern); on failure the job emits `VisualizationFailed` metric **and** publishes SNS — **and the Batch job's terminal FAILED state is consumed**: the Lambda submits with `:::batch:submitJob.sync` is NOT used (keeps products-survive-render-failure), so a CloudWatch alarm on `VisualizationFailed`/Batch `FAILED` jobs is the consumer (names the gap the WA review flagged: a fire-and-forget submit needs an explicit watcher) |
| Bug #1 (Req 4) | `visualize_nasa.py _find_hdf5_files`: **introduce granule-token parsing** (parse the `tHHMMSSS` / granule-start token from each CSPP filename, e.g. `SVI01_j01_dYYYYMMDD_tHHMMSSS_...h5`) — none exists today; `_find_hdf5_files` only sorts paths. Then intersect band-token-set ∩ geo-token-set, render only matched granules, compute bbox from matched set, log dropped granules. This is NEW logic, not a tweak. |
| Bug #2 (Req 5) | `cartopy_renderer.py render_nasa` thermal branch: mask non-finite lat/lon before `pcolormesh` (diagnosis + fix both confirmed correct by review) |
| Bug #3 (Req 6) | `image_renderer.py` + `visualize_nasa.py`: **the real bug is a double-stretch**, not just order. Today: `destripe(contrast_stretch(i1))` in the orchestrator, then `assemble_true_color` **stretches AND gammas again** internally (`gamma_correct(contrast_stretch(i1))`). Fix: destripe **raw** reflectance (mask preserved), remove the orchestrator's redundant `contrast_stretch`, and let `assemble_true_color` do the single stretch — so per-detector medians are computed on real radiance and the data is stretched exactly once. A naive "reorder" alone leaves the double-stretch and does not fix banding. |

## NASA render bug fixes — test-first (Req 4–6)

`scripts/viirs/` has **zero tests today**. Each fix is gated by a test that FAILS against the
broken code first:

| Bug | Test (fails first) | Fix |
|---|---|---|
| #1 granule mismatch | Build HDF5 fixtures with 1 band granule + 5 geo granules, realistic CSPP names → assert token-intersection chosen + no broadcast error; reproduces `(29491200,) (49152000,)` | **introduce** granule-token parse in `_find_hdf5_files`, intersect band∩geo tokens |
| #2 thermal NaN | Feed lat/lon with NaN/inf → assert pcolormesh not called with non-finite (or render succeeds) | mask non-finite before pcolormesh |
| #3 double-stretch banding | Image where stretch-then-destripe-then-stretch corrupts per-detector medians → assert single-stretch path produces correct medians | destripe raw, remove redundant orchestrator stretch, single stretch in `assemble_true_color` |

Tests delegated to the `python` subagent (new logic, Hypothesis where it fits), using the
project's existing pytest + moto + hypothesis setup. Fixtures kept minimal/synthetic — no real
HDF5 granule shipped in-repo.

## IAM / security  (Req 8)

Mirror the `compute_bakeoff` least-privilege roles (service, instance, job, execution), scoped
to production prefixes:
- S3 read on reception bucket; S3 write on `contacts/*` + `products/*`.
- ECR pull on `${project_name}-sdr-pipeline`.
- KMS on the pipeline key; `cloudwatch:PutMetricData` conditioned on `namespace=SDRPipeline`.
- logs write on each stage log group.
Checkov gate: no net-new HIGH/CRITICAL; Spot CE carries the already-proven `spot_iam_fleet_role`
+ `checkov:skip` justifications from `compute_bakeoff`.

## CodeBuild retirement  (Req 7)

Strangler order; a project/branch is removed only after its Batch replacement passes
validation. **The shared `sdr_pipeline` project and its IAM role survive until BOTH fan-out and
async aggregation are off it** (end of Stage 2) — nothing is "orphaned" by Stage 1 alone:
1. fan-out proven (Stage 1) → remove the **CodeBuild branch from the Map body** only; the
   shared `sdr_pipeline` project + role stay (async aggregation still uses them).
2. async aggregation proven (Stage 2) → now remove the shared `sdr_pipeline` CodeBuild project +
   its IAM role (both fan-out and async aggregation are off it); remove the sync CSPP project
   `groundstation-noaa20-sync-aggregation` + role **only after the sync path is validated on a
   real sync contact** (not contact-7).
3. render proven (Stage 3) → remove `groundstation-noaa20-viirs-visualization` + role.

Each removal drops the project **and** its IAM role/policy. Image-build CI is kept. Final
`terraform plan` shows no runtime CodeBuild. `docs/ARCHITECTURE.md` + `docs/SYNC_PIPELINE.md`
updated per stage.

## Validation on contact-7  (Req 9)

Contact-7 SDR data is still warm in S3 (`dcef1518-c454-401f-bdcd-dc2e73fe89a5`, `2026/10/17`,
STANDARD tier). Each stage replays it and diffs against the known-good CodeBuild baseline.
`verify_pass.py` (S3-truth + metric corroboration) runs the end-to-end check. A new paid pass is
booked only if contact-7 cannot exercise a stage, justification recorded in `docs/CONTACTS.md`
first (COST-1 guardrail).

> **Note on contact-7 NASA data:** SVI/SVM bands exist for one granule (`t2049455`); GITCO/GMTCO
> for five. This is precisely the mismatch bug #1 fixes — so contact-7 is the ideal validation
> input for the NASA render fix, not a limitation.

## Validation sufficiency — contact-7 is necessary but NOT sufficient  (Req 9, WA must-fix #8)

A single warm **async** contact (contact-7) does not exercise several failure modes. Each is
called out with how it IS covered, so no CodeBuild project is retired on an untested path:

| Failure mode contact-7 does NOT exercise | How it is covered before retirement |
|---|---|
| Spot reclaim / `retryStrategy` (warm replay won't interrupt) | Fault-injection: force a reclaim or an injected `evaluateOnExit` test on the `chunk` job def |
| CodeBuild concurrency ceiling (the thing we're removing) | The bakeoff/Stage-1 run must use a high-chunk-count contact with `max_vcpus` set to run all at once, and record wall-clock vs the throttled CodeBuild baseline |
| Cold LUT cache → `sdr_luts.sh` fallback | Explicitly test one aggregation run with the cache prefix emptied; the sync branch's first Batch run is cold by definition |
| Sync branch end-to-end (contact-7 is async) | Validate on a real sync contact or a replayed past sync RDR set; sync CSPP project retired only after this |
| Midnight-straddling contact (SFN already flags unhandled) | Out of scope for this spec; recorded as a pre-existing gap, not introduced here |

A new paid pass is booked only if a path cannot be validated on warm/replayed data, with the
justification recorded in `docs/CONTACTS.md` first (COST-1 guardrail).

## Workflow / gates

Runs under the Well-Architected Delta Workflow
([`.kiro/steering/wa-delta-workflow.md`](../../steering/wa-delta-workflow.md)):
implement → validate → security-gate → review per stage. Terraform → `terraform` subagent
(plan-only from the agent; operator applies). Python → `python` subagents, moto/hypothesis.
The agent never edits `*.tf` / `*.py` directly.

## Risks

- **Spot capacity** for `c6i/m6i.2xlarge` in `eu-central-1` — mitigated by the documented
  per-stage Fargate fallback (same image, CE/job-def change).
- **Aggregation entrypoint port** — `buildspecs/aggregation.yml` is CodeBuild-phase-shaped;
  porting to a single Batch entrypoint script risks missing an env/phase nuance → validated by
  the 38-file diff on contact-7.
- **Render trigger change** touches the live `viirs-orchestrator` Lambda — gated by keeping the
  CodeBuild path until the Batch render passes contact-7 (strangler).
- **`scripts/viirs/` has no test harness today** — first tests may surface latent coupling;
  kept synthetic/minimal to avoid shipping real granules.
