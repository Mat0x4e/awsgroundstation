# Clean Compute Pipeline Migration — Execution Log

The running record of the whole-pipeline migration off AWS CodeBuild onto AWS Batch, executing
**ADR-002** (in [`ARCHITECTURE_REFINEMENT.md`](ARCHITECTURE_REFINEMENT.md)) via the spec
[`.kiro/specs/clean-compute-pipeline/`](../.kiro/specs/clean-compute-pipeline/).

This document is the **honest-runbook** record for the migration: what was done, what broke, and
why — so a future operator (or agent) can reconstruct the reasoning, not just the end state. It
complements [`ARCHITECTURE.md`](ARCHITECTURE.md) (async as-built) and
[`SYNC_PIPELINE.md`](SYNC_PIPELINE.md) (sync as-built), which are updated per stage as each
CodeBuild project is retired.

## Strategy (from ADR-002)

- **Strangler, committed to EC2 Spot, Fargate as per-stage fallback.** Each stage stands up on
  Batch alongside its CodeBuild equivalent, is proven on contact-7 warm data, then the CodeBuild
  usage is retired — in dependency order: **fan-out -> aggregation -> render**.
- **Target compute:** fan-out -> Batch EC2 **Spot** (idempotent + Map-tolerated); aggregation and
  render -> Batch EC2 **on-demand** (single long jobs). Live-demod RT-STPS stays on the receiver
  EC2 (not batch).
- **CodeBuild surface (verified in `step_functions.tf`):** NOT four separable projects. Fan-out
  and async aggregation share ONE project (`aws_codebuild_project.sdr_pipeline`); sync CSPP is a
  second project; viirs render is a third. Image-build CI is kept.

## Account / region / key facts

| Item | Value |
|---|---|
| Account | `471112743408` |
| Region | `eu-central-1` |
| SDR ECR image | `groundstation-noaa20-sdr-pipeline` — **~20 GiB compressed** (22,332,403,301 bytes) |
| Reception bucket | `aws-groundstation-demo-reception-471112743408` |
| Output bucket | `groundstation-noaa20-sdr-output-471112743408` |
| Pipeline KMS CMK | `70451aac-a58c-4a93-be24-4587cd55a795` |
| Contact-7 (warm test data) | `dcef1518-c454-401f-bdcd-dc2e73fe89a5`, `2026/10/17`, Oregon |
| Org SCP (the blocker) | `p-eb2chkmi` (org `o-dri8v6mcv7`) |

---

## Stage 1 — Chunk fan-out -> AWS Batch EC2 Spot

### Status: fan-out VALIDATED end-to-end; CodeBuild branch RETIRED from Map (task 4, plan-only)

### What was deployed

Module `infra/modules/batch_compute/` (gated by root var `enable_batch_compute`, default
**false**), wired in `infra/main.tf` alongside the CodeBuild fan-out (strangler — both coexist):

| Resource | Detail |
|---|---|
| Spot compute environment | MANAGED, EC2 SPOT, `SPOT_CAPACITY_OPTIMIZED`, `c6i.2xlarge`+`m6i.2xlarge`, `min_vcpus=0`, `max_vcpus=216` (27 chunks x 8 vCPU — removes the CodeBuild concurrency throttle) |
| Launch template | `/dev/xvda` root EBS `encrypted=true` (aws/ebs managed key), gp3, **200 GiB**, IMDSv2 required; `Owner` tag on instance+volume; `update_default_version=true` |
| CE launch-template version | `aws_launch_template.fanout.latest_version` (numeric — forces CE re-resolution on LT change) + `create_before_destroy` + `compute_environment_name_prefix` |
| Job queue | `groundstation-noaa20-fanout-queue` |
| Chunk job definition | existing ECR image; entrypoint = CodeBuild buildspec steps (download pcap -> `iq_extract.py` -> `satdump_process.sh` -> upload to `contacts/`); idempotent prefix-clear before `s3 sync`; `retry_strategy` attempts=2, `evaluate_on_exit` Host-EC2*->RETRY / *->EXIT |
| IAM | 5 least-privilege roles (service, spot-fleet, instance+profile, chunk-job, execution); S3 write scoped to `contacts/*`; `PutMetricData` namespace-conditioned to `SDRPipeline`; Checkov clean (53 passed / 0 failed) |

### Canary validation (2026-10-20)

Before wiring the live Step Functions state machine, a **single-chunk canary** was run directly
against the Batch queue to de-risk the live-SFN edit. This proved the Batch Spot path end-to-end
and surfaced six launch-blockers that `terraform validate` and `terraform plan` cannot catch.

**Result (canary v6, job `ea0795c6-...`):** SUCCEEDED in ~23 min (RUNNING 13:58 -> SUCCEEDED
14:21 UTC) — within the CodeBuild per-chunk baseline (8.7 / 23.3 / 38.9 min min/median/max).
Produced to `contacts/2026/10/17/dcef1518.../satdump/chunk_canary/`:
- `npp_hrd.cadu` — **56 MB** (the decoded CADU frame stream; the essential chunk output)
- **415 SatDump composite objects** — OMPS (339), VIIRS (36), ATMS (33), VIIRS-DNB (4)
- `dataset.json`, `satdump.log`
All KMS-encrypted (SSE-KMS, pipeline CMK) — byte/structure-comparable to a CodeBuild chunk.
CE scaled back to `desiredvCpus=0` afterward (no lingering Spot cost). Canary output then deleted.

### The six launch-blockers (all fixed; full detail backported to `.kiro/steering/aws-knowledge.md`)

A compute environment reporting `VALID/ENABLED/Healthy` proves only that the CONFIG was accepted
— NOT that an instance can launch, encrypt, pull the image, and run. The canary found, in order:

1. **Org SCP `DenyEC2ResourceWithoutOwnerTag`** — requires an `Owner` request-tag on
   instance/volume creation. Symptom: job stuck `RUNNABLE`, `desiredvCpus>0`, zero instances;
   CloudTrail `RunInstances` -> `Client.UnauthorizedOperation ... explicit deny in SCP`. Decoded
   with `aws sts decode-authorization-message`. Fix: launch-template `tag_specifications` apply
   `Owner` to both `instance` and `volume`.
2. **Encrypted root volume required** — the ECS-optimised AL2023 AMI root volume is
   `Encrypted:false` and account EBS-encryption-by-default is off; SCP denies `RunInstances` on
   `volume/*`. Fix: launch-template `ebs { encrypted = true }`.
3. **Customer CMK fails on Spot EBS** — instances launched then self-terminated with
   `Client.InvalidKMSKey.InvalidState` (on an ENABLED key). Root cause: with EC2 Spot, the EBS
   volume is created by the EC2 Spot service-linked role (AWS-owned account) which cannot use our
   CMK without a key-policy grant. Fix: use `encrypted=true` with NO `kms_key_id` -> `aws/ebs`
   managed key (the root volume is ephemeral scratch; the science product is CMK-encrypted in S3).
4. **Launch-template version pinning** — after fixing the LT, the CE kept launching the OLD
   template content. `$Latest`/`$Default` are static strings that don't force CE re-resolution on
   an in-place LT update. Fix: `update_default_version=true` on the LT + CE
   `version = aws_launch_template.fanout.latest_version` (numeric -> CE replacement -> re-resolve).
   Note: applying a SAVED plan with `latest_version` throws `Provider produced inconsistent final
   plan` — re-run `apply` without the stale `-out` file.
5. **Root volume too small for the decompressed image** — job reached `STARTING`, container
   `PENDING` ~20-40 min, then `CannotPullContainerError: ... no space left on device`. The ~20 GiB
   compressed image decompresses well past 60 GiB (CSPP `SDR_4_1/anc` ancillary tiles). Fix:
   root volume 60 -> **200 GiB** gp3.
6. **Batch `Ref::` substitution works in `command`, NOT `environment[].value`** — the container
   ran but failed with the literal string `Invalid bucket name "Ref::input_bucket"`. Fix: pass
   per-item values via `ContainerOverrides.Environment[]` at submit time (exactly how the SFN
   `:::batch:submitJob.sync` Map passes them), not via job-def `Ref::` env vars. **This is the key
   design input for task 3.1.**

### Terraform operational notes (this stage)

- **CE replacement deadlock:** adding a launch template forces CE replacement; Terraform tried to
  DELETE the old CE while the job queue still referenced it -> `Cannot delete, found existing
  JobQueue relationship`. Fixed permanently with `compute_environment_name_prefix` +
  `lifecycle { create_before_destroy = true }` so the new CE is created and the queue re-pointed
  before the old CE is destroyed.
- **Non-ASCII in `description` fields** failed the first apply (`em-dash` U+2014) — AWS IAM/EC2
  reject non-ASCII; `terraform validate` does not catch it. All module files sanitised to ASCII.
- Both gotchas backported to `.kiro/steering/aws-knowledge.md`.

### Full-pipeline fan-out validation THROUGH the SFN (task 3.2/3.3, 2026-10-20)

After 3.1 deployed, the live `sdr_pipeline` Step Functions state machine was triggered for
contact-7 (`enable_batch_compute=true` -> `use_batch_fanout=true`), fanning out all 25 chunks to
Batch Spot via the new `SubmitBatchChunk` path. Validated the whole fan-out at scale, not just
the single canary chunk.

**Idempotence gotcha:** the first trigger short-circuited `CheckProcessingMarker -> SUCCEEDED`
because the CodeBuild baseline run (2026-10-17) had left a `.processing` marker at
`contacts/{contact_id}/.processing`. Removed the marker, re-triggered. (The marker is keyed by
contact_id only, not date.)

**Result:**
- Map submitted all 25 chunks; **19 ran concurrently** (the Map `MaxConcurrency`) on 19 Spot
  instances simultaneously -- the concurrency win over CodeBuild's throttled account build cap,
  demonstrated live. Second wave of 6 ran as slots freed.
- Image pull ~13 min per fresh instance (the 20 GiB tax), then ~23 min processing each.
- **25/25 chunks produced `npp_hrd.cadu` (~56 MB each)** + 10,375 total SatDump composite objects
  (4.9 GB) to the `contacts/` prefix -- byte-comparable to the CodeBuild baseline per-chunk CADU.
- **Spot-reclaim retry exercised live:** `chunk-10` had both attempts' instances reclaimed
  (`Host EC2 ... terminated` x2) -> job tallied FAILED, but `ToleratedFailurePercentage=100` kept
  the Map green and chunk_10 still has its CADU in S3. Tuning note: raising the job-def
  `retryStrategy` attempts from 2 to 3 would absorb a double-reclaim; 2 was enough here because
  the Map tolerates the single loss.
- Map exited cleanly; execution handed off to aggregation (still CodeBuild -- Stage 2 waypoint).

**Conclusion: Stage 1 fan-out on Batch EC2 Spot is validated end-to-end through the production
state machine.** The CodeBuild fan-out branch remains in the definition (strangler) and is
retired in task 4 only after this validation -- which has now passed.

### Task 4 complete (plan-only) — CodeBuild branch retired from the Map

The `ChooseCompute` Choice gate and the five CodeBuild fan-out states (`StartCodeBuild`,
`WaitForBuild`, `CheckBuildStatus`, `EvaluateBuildStatus`, and the `ChooseCompute` router) have
been removed from the `ParallelProcessing` `ItemProcessor`. `StartAt` is now
`"SubmitBatchChunk"` directly. The `"use_batch"` field is removed from `ItemSelector`. The
`sfn_batch` IAM policy (`count = 1`, was `count = var.use_batch_fanout ? 1 : 0`) is now
unconditional.

**Shared resources NOT touched:** `aws_codebuild_project.sdr_pipeline`, `aws_iam_role.codebuild`,
`StartAggregationBuild` and its poll loop (`WaitForAggregation`, `CheckAggregationBuild`,
`EvaluateAggregation`) — all remain for Stage 2 aggregation retirement.

**Coupling note:** the SFN Map now routes ALL chunks to `SubmitBatchChunk` unconditionally. If
the pipeline is ever triggered with `enable_batch_compute=false` (which keeps the Batch CE/queue
absent), the `SubmitBatchChunk` state will fail at runtime because the queue ARN is an empty
string. The `enable_batch_compute` flag must remain `true` for any live contact run from this
point forward. Resolving this coupling is deferred to Stage 2 cleanup.

`terraform plan -var "enable_batch_compute=true"` shows: 0 to add, 2 to change, 0 to destroy.
The 2 changes are `aws_sfn_state_machine.sdr_pipeline` (Map body) and
`aws_batch_job_definition.chunk` (pre-existing parameter drift, unrelated to this task).
`aws_codebuild_project.sdr_pipeline` and its IAM role are unchanged.

### Next: Stage 2

---

## Stage 2 — Aggregation (async + sync CSPP) -> Batch on-demand

### Status: in progress (task 5 — on-demand CE + aggregation job definition)

### Design (converged, from reading both buildspecs + scripts/aggregation.sh)

One `aggregation` Batch job definition on a NEW on-demand compute environment, driven by an
`RTSTPS=on|off` parameter, replacing BOTH CodeBuild aggregation projects:

| Aspect | async (`RTSTPS=on`) | sync (`RTSTPS=off`) |
|---|---|---|
| Source | `.cadu` from `contacts/.../satdump/` | pre-made RDR from `contacts/.../rdr/` |
| Steps | concat CADU -> RT-STPS (PnEncoded=false munge) -> RDR -> CSPP -> SDR | CSPP directly on the RDR -> SDR |
| Replaces | `aggregation.yml` + `scripts/aggregation.sh` (async `StartAggregationBuild`) | `aggregation_sync.yml` (sync SFN `StartAggregationBuild`) |

**Common to both (the convergence):**
- Extract J01 shipped LUTs from `s3://OUTPUT/software/CSPP_SDR_V4.1_static_luts_j01.tar.gz`.
- **PERF-1 cache fast-path for BOTH branches** (`s3://OUTPUT/lut-cache/SDR_4_1_DB/`) with the
  `sdr_luts.sh` internet fallback KEPT. This FIXES the sync-branch gap: `aggregation_sync.yml`
  always ran `sdr_luts.sh` (no cache); the converged entrypoint gives sync the same fast-path.
- Upload SDR/GEO HDF5 to `contacts/.../sdr/` with SSE-KMS; async also uploads RDR to `.../rdr/`.

**Batch adaptations vs the EC2/CodeBuild versions:**
- `SELF_STOP=0` always (no `shutdown -h` -- the container is torn down by Batch).
- No watchdog/self-stop (Batch job timeout governs).
- Scratch in `/var/tmp` (CSPP needs real disk, not the tmpfs `/tmp`); the 200 GiB root volume
  (sized in Stage 1 for the image) comfortably holds CSPP's multi-GB SDR scratch.
- Entrypoint pulled from `s3://OUTPUT/software/aggregation_entrypoint.sh` (mirrors the existing
  no-image-rebuild deploy property of `aggregation.yml`).
- **On-demand, not Spot** -- one long non-restartable job; a mid-run reclaim wastes the whole run.

### Compute
New on-demand CE in `infra/modules/batch_compute/` (same `c6i/m6i.2xlarge` family, `min_vcpus=0`,
reuses the 200 GiB encrypted-LT + Owner-tag pattern proven in Stage 1), its own job queue, and
the `aggregation` job definition. IAM: aggregation job role needs S3 read/write on `contacts/*`
+ `software/*` + `lut-cache/*`, KMS, ECR, logs, namespace-conditioned metrics.

### Validation
- Async on contact-7 (replay) -> same CSPP SDR/GEO file set as the baseline.
- Cold-cache run (emptied `lut-cache/`) -> exercises the `sdr_luts.sh` fallback.
- **Sync branch needs a real sync contact** (contact-7 is async-only) -- recorded gap; the sync
  CSPP project is retired (task 8) only after a sync contact validates `RTSTPS=off`.

---

## Stage 3 — Visualization render -> Batch on-demand (+ 3 NASA render bug fixes)

Not started. See spec tasks 9-13.


### Stage 2 validation gotcha: SFN execution timeout too tight for Batch fan-out + Batch aggregation

The first Stage 2 async validation run (`stage2-agg-val-c7`, 2026-10-21) **TIMED_OUT at exactly
5400s** (90 min): 24/25 Map iterations succeeded, one straggler chunk was still processing, and
the state machine's top-level `TimeoutSeconds = 5400` killed the execution before the Map
finished -- so `SubmitBatchAggregation` never ran.

Root cause: the 5400s timeout was sized for the CodeBuild era. Batch fan-out adds a per-fresh-Spot-
instance ~20 GiB image pull (~13 min) on top of ~23 min processing; 25 chunks / MaxConcurrency 19
= two waves ~70-75 min worst case (cold image), and the NEW Batch aggregation adds ~25-40 min. A
single slow straggler chunk pushed the total past 90 min. (Note: the FIRST full run on 2026-10-20
fit inside 90 min only because its timing happened to land right; it is not reliable.)

Fix: raised `sdr_pipeline` SFN `TimeoutSeconds` 5400 -> **10800** (180 min) for comfortable
headroom over the ~110-115 min realistic worst case. This is NOT a Batch failure -- the fan-out
and aggregation both work; the orchestration window was simply too small once fan-out moved to
Batch's per-instance image-pull model.

Also flagged (sync path, not yet changed): the sync SFN has a top-level 5400s AND an inner
per-state 1800s (30 min) timeout on its aggregation state -- the inner 1800s is tight against a
~40-min cold-cache `sdr_luts.sh` fallback. To be reviewed before the sync branch is validated
(task 7.3 sync / task 8).


### Stage 2 validation gotcha #2: CSPP needs HTTP port 80 egress (SG only had 443)

The second async validation run (180-min timeout) got PAST fan-out (25/25 chunks) and the Batch
aggregation job STARTED, ran RT-STPS (5 RDR files), and launched CSPP `viirs_sdr.sh` -- but then
hung and hit the 60-min job timeout (SIGTERM, exit 143). The EXIT-trap uploaded the logs to S3
(CloudWatch had ZERO log streams -- the awslogs driver never created one, so read the S3 logs).

Root cause from `cspp_sdr.log`:
```
ancillary_and_luts.py: check j01 lut http://jpssdb.ssec.wisc.edu/cspp_luts_v_7_0/VIIRS-SDR-F-PREDICTED-LUT_j01_.*?bin
http_wrap.py:125: Not OK curl --compressed http://jpssdb.ssec.wisc.edu/cspp_luts_v_7_0/
curl: (28) Failed to connect to jpssdb.ssec.wisc.edu port 80 after 132867 ms: Connection timed out
```
CSPP's `viirs_sdr.sh` does residual DYNAMIC ancillary/LUT checks against
`http://jpssdb.ssec.wisc.edu` on **port 80 (HTTP)** at SDR time -- even with the PERF-1 static
LUT DB cache staged (the cache hit was confirmed in the log). The Batch SG `fanout` only allowed
**443** egress, so every port-80 fetch timed out at ~132s and CSPP never finished.

This is exactly why aggregation originally ran in CodeBuild: CodeBuild has unrestricted egress
(80 + 443). The PERF-1 cache reduces but does NOT eliminate CSPP's HTTP fetches.

Fix: added a port-80 egress rule to `aws_security_group.fanout` (0.0.0.0/0, with a CKV_AWS_382
checkov:skip justified by the HTTP-only external SSEC LUT host). The 443 rule is unchanged. Port
80 is harmless for the fan-out SatDump chunk jobs (they do not use it). A future hardening option
is to cache ALL dynamic ancillary in S3 and drop port 80, but that is deferred.

Note: this means the aggregation job's effective worst-case runtime includes these HTTP fetches;
once port 80 works they complete quickly, but monitor the first post-fix run's duration vs the
3600s job timeout.


### Task 8 (async retirement) — DONE 2026-10-21

With async aggregation validated on Batch, the shared CodeBuild project was retired:
- **Destroyed:** `aws_codebuild_project.sdr_pipeline` (the SHARED fan-out + async-aggregation
  project), its `/aws/codebuild/...-sdr-pipeline` log group, `aws_iam_role.codebuild` + policy, and
  the `InvokeCodeBuild` statement on the SFN role (last reference to the dead project). Apply: 4
  destroyed, 3 changed, 0 added.
- **Async SFN collapsed to pure Batch:** `CheckResults -> SubmitBatchAggregation` direct; removed
  `ChooseAggregationCompute`, `StartAggregationBuild`, `WaitForAggregation`, `CheckAggregationBuild`,
  `EvaluateAggregation`, `MarkAggregationFailed`. Verified in the live definition.
- **Untouched (confirmed):** image-build CI (`sdr-docker-build`, `viirs-docker-build`),
  `lut-cache-refresh` (PERF-1), the ECR repo, and the entire `sync_pipeline` module.

**HELD:** `groundstation-noaa20-sync-aggregation` CodeBuild + its sync SFN poll-loop — retired only
after a real sync contact validates `RTSTPS=off` (contact-7 is async DigIF and cannot exercise it).

**FLAGGED for separate cleanup (out of this spec's scope):** `satdump-noaa20-processing` — the old
out-of-IaC CodeBuild project (OE-1 finding) still exists in the account but not in Terraform. It is
now dead (fan-out runs on Batch). It needs a deliberate manual/console removal since Terraform does
not manage it.

### State after task 8

| Stage | Compute | Status |
|---|---|---|
| Chunk fan-out | Batch EC2 Spot | DONE, validated |
| Aggregation (async) | Batch EC2 on-demand | DONE, validated |
| Aggregation (sync) | Batch EC2 on-demand (wired, RTSTPS=off) | deployed, NOT yet validated (needs sync contact); CodeBuild held as fallback |
| Visualization render | CodeBuild (`viirs-visualization`) | NOT migrated — Stage 3 |
| Live-demod RT-STPS | Receiver EC2 | unchanged (not batch) |

Runtime CodeBuild remaining: `viirs-visualization` (render, Stage 3) + `sync-aggregation` (held).
Image-build CI CodeBuild: kept (not runtime).