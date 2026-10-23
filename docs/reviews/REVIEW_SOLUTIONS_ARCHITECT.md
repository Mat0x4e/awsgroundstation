# Solutions Architect Review -- NOAA-20 DigIF -> VIIRS L1 Demonstrator

Reviewer lens: Enterprise & Solutions Architecture -- architecture adequacy,
architecture documentation quality, and AWS Well-Architected alignment.
Repo root: /projects/sandbox/awsgroundstation  (branch: docs/post-batch-accuracy-review)
Scope basis: every material claim below is grounded in the Terraform and handler
code, not only the prose docs. Evidence paths are cited inline.

---

## Executive summary

This is a strong, unusually honest demonstrator architecture. The decomposition is
sound, the module boundaries are clean, and the recently completed CodeBuild -> AWS
Batch migration (ADR-001/ADR-002, spec `clean-compute-pipeline`) was executed as a
textbook strangler: flag-gated per stage, validated on warm contact-7 data, with the
legacy CodeBuild path retained as a fallback. The IAM is genuinely least-privilege
(per-job task roles, namespace-conditioned `PutMetricData`, prefix-scoped S3, each
wildcard justified in-code). The documentation is a differentiator: the ADRs are real
ADRs (context / measured inputs / options / decision / consequences / status), the
architecture docs are traceable to the actual resources, and a live-learned gotchas
file (`.kiro/steering/aws-knowledge.md`) captures five Batch-on-Spot launch blockers
and the CloudWatch metric-math alarm timing trap that `terraform validate` cannot catch.

The project's own `ARCHITECTURE_REFINEMENT.md` already enumerates most of the real
gaps and -- importantly -- distinguishes demonstrator-appropriate tradeoffs (single
region, single shared CMK, single-AZ receiver, `ToleratedFailurePercentage=100`,
account-wide budget) from genuine debt, and has since closed several (REL-1 alarms,
REL-2/3 visibility metrics, REL-6 fixed ENI, OE-2 drift detection). The largest residual
risk is not a design flaw but an operational-excellence gap the docs name explicitly:
load-bearing software (`/opt/rt-stps`, `/opt/SDR_4_1`, the sync receiver's patched
`jpss1.xml`) is hand-installed on EC2 outside IaC, with automated reconstruction
deliberately DEFERRED to drift-detection only. For a demonstrator this is an acceptable,
well-documented tradeoff; for production it is the first thing to codify.

Overall: the architecture is adequate-to-strong for its stated purpose, the
documentation is production-grade, and the known gaps are known, ranked, and mostly
either closed or consciously deferred.

---

## Architecture adequacy

### Decomposition and module boundaries -- sound

The root module (`infra/main.tf`) composes eleven feature modules behind boolean
feature flags (`enable_sdr_pipeline`, `enable_sync_pipeline`, `enable_batch_compute`,
`enable_processing_pipeline`, `ground_station_enabled`, `enable_compute_bakeoff`).
Boundaries are clean and follow responsibility seams:

- `security/` owns the cross-cutting primitives (KMS CMK, SNS, CloudTrail, the
  per-consumer IAM roles, the budget guardrail) and exports them as outputs.
- `s3_delivery/` owns the reception bucket contract (the `aws-groundstation-*`
  name prefix required by Ground Station S3 Data Delivery).
- `sdr_pipeline/` owns the async DigIF orchestration (SFN, EC2 aggregation
  instance, Lambdas, LUT cache, drift check); `sync_pipeline/` owns the live
  demod/decode path; `viirs_visualization/` owns the render image + orchestration.
- `batch_compute/` owns the runtime compute (Spot fan-out CE, on-demand
  aggregation/render CE, job queues, job definitions, five+ IAM roles).
- `observability/` owns dashboards, GS contact-state alarms, and the single
  bucket-notification configuration (correctly centralised -- see below).

The separation of `compute_bakeoff/` (disposable experiment, `count=0` by default,
documented teardown command in `variables.tf`) from `batch_compute/` (long-lived
production) is good discipline: the throwaway scaffold is not confused with the
promoted module, and `batch_compute/main.tf` documents the deltas from the bakeoff
(production `contacts/` prefix, Spot `retryStrategy`, idempotent prefix clear,
namespace-conditioned metrics).

One genuinely elegant boundary decision: the single-owner rule for
`aws_s3_bucket_notification` is enforced and documented in
`observability/main.tf` -- "S3 allows exactly ONE notification configuration per
bucket ... declaring the resource twice ... silently drops the other." This is a
real incident scar (contact `ba2c5446`, 43.1 GiB landed unprocessed) turned into a
codified constraint.

### Topology: Step Functions + Batch + Lambda -- appropriate vs alternatives

The async topology is: EventBridge (GS Contact State Change) -> Step Functions
(`STANDARD`) -> Map of `:::batch:submitJob.sync` chunk jobs on Spot -> on-demand
Batch aggregation -> Lambda orchestrator -> on-demand Batch render. This is the
right shape, and the choice is explicitly reasoned in ADR-001 against the real
alternatives:

- CodeBuild (status quo) was a CI/CD build service used as a batch engine; the
  measured bottleneck was the account concurrent-build cap, not the workload
  (ADR-001, contact #5 rehearsal). Correctly retired.
- ECS/Fargate and Batch-on-Fargate were evaluated and kept as the documented
  fallback if `eu-central-1` Spot capacity for the `c6i/m6i.2xlarge` shape proved
  unreliable (Requirement 1.7, `clean-compute-pipeline`). It did not, so EC2 Spot
  was adopted.
- Pure EC2 was the original intent and remains viable post-PERF-1 (LUT cache) but
  does not model "N idempotent container runs" natively.

The compute placement is well matched to workload shape: **Spot** for the idempotent
fan-out (`SPOT_CAPACITY_OPTIMIZED`, `batch_compute/main.tf`), **on-demand** for the
long (~25-40 min) aggregation and the single render job, which must not lose a mid-run
Spot reclaim. Render correctly **reuses** the on-demand aggregation queue (it is a
single job, not a fan-out) -- documented in `infra/main.tf` locals.

Notably, the Map uses one `submitJob.sync` **per item** rather than a Batch **array
job** -- a deliberate, documented decision (Requirement 1.5): an array job does not
compose with per-item Map semantics. This is the correct call and shows the team
understood the integration rather than reaching for the obvious-but-wrong primitive.

### Strangler migration -- well executed

The three cutover mechanics are all present and correct:

1. **Flag-gating.** `enable_batch_compute` gates the whole Batch family;
   `sdr_pipeline` receives `use_batch_fanout` / `use_batch_aggregation` and
   `viirs_visualization` receives `render_compute = "batch"|"codebuild"`. The
   Lambda honours `RENDER_COMPUTE` with the CodeBuild path "exactly as-is"
   (`lambdas/viirs_visualizer/handler.py`, `_submit_batch` vs `_submit_codebuild`).
2. **Deterministic-ARN cycle breaks.** `infra/main.tf` locals construct the Batch
   queue/job-def ARNs by pure string interpolation (`var.*`, `data.*`) rather than
   reading `module.batch_compute` outputs -- because `batch_compute` already reads
   `sdr_pipeline`/`viirs_visualization` outputs (ECR URL, buckets), so a back-
   reference would close a module cycle. The reasoning is documented at length and
   the job-def ARN is intentionally **unversioned** so `submitJob` resolves the
   latest ACTIVE revision at runtime (avoids Terraform churn). This is a subtle,
   correct piece of Terraform architecture.
3. **Fallbacks retained.** The CodeBuild buildspecs survive as string constants in
   the Lambda with explicit "RETAINED FOR THE LEGACY CODEBUILD STRANGLER PATH ONLY"
   banners and a `test_buildspec_contract.py` guard; `ARCHITECTURE.md` records they
   are retired only after the Batch path is proven over several real contacts (task 13).

Residual migration risk, correctly documented in `aws-knowledge.md`: a container
Batch job runs **image-baked** code, so the image must be rebuilt from the branch that
holds the new code (`sourceVersion = main`). The "No such file or directory" render
failure on contact `dcef1518` is captured as a live lesson, with the ordering hazard
(flip applied while `:latest` is stale) and the mitigation spelled out. This is a real
coupling (image-build source-ref -> `main`) flagged under Risks below.

Adequacy verdict: **Strong for a demonstrator.** The decomposition, topology, compute
placement, and strangler execution are all defensible with evidence, and the one-way
dependency direction (`batch_compute -> viirs_visualization`, acyclic) is reasoned
explicitly rather than stumbled into.

---

## Well-Architected scorecard

| Pillar | Rating | Key strengths (evidence) | Key gaps |
|---|---|---|---|
| Operational Excellence | Adequate | Whole loop is IaC + flag-gated strangler (`infra/main.tf`); SSM State Manager drift detection on both EC2 instances (`sdr_pipeline/ssm_drift_check.tf`, `sync_pipeline/ssm_drift_check.tf`) with `OptDriftDetected` alarms; live-learned gotchas codified as steering (`aws-knowledge.md`); honest runbooks (`ARCHITECTURE.md`, `CONTACTS.md`). | Hand-installed `/opt/rt-stps` + `/opt/SDR_4_1` + sync `jpss1.xml` outside IaC; automated reconstruction DEFERRED (detection only). SDR image-build pinned to `main` is a deploy-ordering hazard. |
| Security | Strong | Per-job task + execution roles (`batch_compute/iam.tf`); `cloudwatch:PutMetricData` conditioned on `namespace=SDRPipeline`/`SyncPipeline`; S3 prefix-scoped (`contacts/*`, `products/*`, `software/*`, `lut-cache/*`); every `Resource:"*"` justified in-code (`ecr:GetAuthorizationToken`, metric namespace); KMS CMK rotation on, least-privilege per-service-principal key policy; all buckets block public access + SSE-KMS + versioning; CloudTrail bucket hardened (no `force_destroy`, versioning on); IMDSv2 required, SG no-ingress/egress-443. | Single shared CMK across all domains (documented tradeoff). CloudTrail single-region, management-events-only (documented, SEC-4). Default-VPC/subnet usage (no dedicated network). |
| Reliability | Adequate | SFN idempotence via `.processing` marker; chunk `retryStrategy` with `evaluateOnExit` (Host EC2* -> RETRY, * -> EXIT) + idempotent prefix clear; SFN task `Retry`/`Catch` throughout; aggregation on-demand (no Spot reclaim) with 60-min timeout; alarm parity achieved (`sdr_pipeline/monitoring.tf` now mirrors sync); swallowed-viz-failure made observable (`VisualizationFailed` metric + SNS); all-chunks-failed alarm; sync REL-6 fixed-IP secondary ENI; EC2 auto-recovery alarm. | Sync path has no S3 buffer: a missed arm loses the pass (REL-5, mitigated by PREPASS readiness alarm, not eliminated). Single-AZ receiver, single aggregation instance (documented tradeoffs). No cross-region/remote-state DR. |
| Performance Efficiency | Strong | Spot fan-out at up to 19 concurrent removes the CodeBuild build-cap throttle (measured contact-7: ChunksSucceeded=50, 0 failures); PERF-1 S3 LUT cache removes ~10 min `sdr_luts.sh` hot-path fetch and the run-time internet dependency; 200 GiB root sized for the decompressed ~20 GiB image (measured, not guessed); SFN 180-min timeout sized against measured worst case. | Chunk instance types fixed to two `2xlarge` shapes (fine, but no right-sizing study per chunk profile). Aggregation is a single serial job. |
| Cost Optimization | Strong | Scheduler cron DISABLED after it booked two unintended paid contacts (`contact_scheduler/main.tf`, `state = "DISABLED"`); AWS Budgets guardrail at 80/100/forecast thresholds -> SNS (`security/budget.tf`); Spot for idempotent fan-out (~$1-2/contact vs ~490 CodeBuild build-min); S3 lifecycle to DEEP_ARCHIVE at 30d; Bucket Keys on SSE-KMS; ephemeral instances (`min_vcpus=0`). Honest framing: compute is dwarfed by ~$110 antenna time. | Account-wide (not GS-service-scoped) budget by choice (ready-to-enable `cost_filter` committed commented). No approval gate in front of `reserve-contact` (COST-1 optional). |
| Sustainability | Adequate | Scale-to-zero compute (`min_vcpus=0` on both CEs); Spot uses spare capacity; on-demand instances live only minutes per contact and are deleted on termination; S3 tiering to DEEP_ARCHIVE reduces storage footprint; no idle always-on compute in the async path. | Not an explicit design axis; DigIF ships ~30x the demodulated bitstream (cross-region transfer + processing energy) -- the sync path exists to attack this but is blocked on onboarding. Region (`eu-central-1`) not selected on carbon grounds. |

### Pillar notes (evidence detail)

**Operational Excellence -- Adequate.** The strongest OE asset is cultural: incidents
become codified constraints (the one-bucket-notification rule; the per-contact-not-
per-object trigger after the ~6,000-build incident; the metric-from-buildspec-not-Lambda
fix). Drift detection (`ssm_drift_check.tf`) is a pragmatic OE-2 compromise: it runs on
instance start (which precedes every contact) so drift surfaces before AOS, and emits
`OptDriftDetected` to a CloudWatch alarm. The gap is that detection is not repair -- a
detected-missing `/opt/*` still needs weeks-of-manual-work restoration, deliberately
DEFERRED because "re-encoding it blind risks silent wrongness that only surfaces during
a pass." That reasoning is sound for a demonstrator and explicit in the code comment.

**Security -- Strong.** `batch_compute/iam.tf` is the standout: eight roles, each
scoped to exactly what its entrypoint calls, with the chunk job able to write only
`output_bucket/contacts/*`, the render job only `products/*`, and `DeleteObject` added
only where idempotent retry needs it. Every unavoidable `Resource:"*"`
(`ecr:GetAuthorizationToken`, `cloudwatch:PutMetricData`) carries an in-code
justification, and `PutMetricData` is namespace-conditioned everywhere except the
`processor_lambda` (which `ARCHITECTURE_REFINEMENT.md` SEC-1 flagged and has since been
brought into line -- the condition is present in `security/main.tf`). The KMS key policy
grants each service principal (GroundStation, CloudTrail, EventBridge, CloudWatch Logs
with an encryption-context ArnLike condition, CloudWatch Alarms + Budgets with
SourceAccount conditions) only the actions it needs. The single shared CMK is the one
deliberate blast-radius tradeoff, documented with a clear "revisit when" trigger.

**Reliability -- Adequate.** The async path is genuinely robust: idempotent via the
`.processing` marker, replayable from S3, partial-tolerant with the failure made
visible. The honest caveat is reproduced verbatim in `ARCHITECTURE.md` -- "A green
execution does not mean 22 good chunks ... does not mean imagery either" -- and both
blind spots now have metrics/alarms (`ChunksSucceeded/Failed`, `VisualizationFailed`).
The `chunks_all_failed` alarm is a highlight of engineering rigour: the metric-math
`IF(m1>0 AND m2==0,1,0)` with `period=3600` is the direct fix for the sparse-log-metric
timing trap documented in `aws-knowledge.md`, with the live-run failure
(period=900 missed a 22-chunk burst) and root cause captured in the resource comment.
The sync path is the highest residual risk by design (no S3 buffer); it is mitigated
(REL-6 ENI, REL-5 PREPASS readiness alarm) but the single-point-of-failure remains, and
the path is blocked on satellite onboarding anyway.

**Performance Efficiency -- Strong.** The migration's measured win (contact-7:
50/50 chunks, ~19 concurrent Spot) is the proof point; the EBS sizing (200 GiB for the
decompressed image, derived from `ecr describe-images` bytes) and the LUT cache
(PERF-1) show performance decisions made from measurement, not assumption.

**Cost Optimization -- Strong.** The disabled scheduler + budget + Spot combination is
exactly right for a paid-per-contact workload, and the docs are refreshingly honest that
the compute optimisation is operationally motivated (throttle, tool-fit) rather than
dollar-motivated, since antenna time dominates.

**Sustainability -- Adequate.** Scale-to-zero and short-lived Spot are the right
instincts; the pillar is not an explicit design axis, which is appropriate for a
demonstrator but worth naming.

---

## Documentation adequacy

**The ADRs are real ADRs.** `ARCHITECTURE_REFINEMENT.md` contains ADR-001 and ADR-002
with full structure: Status (with dates and accepted/implemented transitions), Context,
measured-inputs tables (sourced to `CONTACTS.md` and the Terraform), options matrices
with fit/cost/change columns, an explicit Decision, Consequences (positive/negative/
neutral), and a "what this ADR deliberately does NOT do" boundary. ADR-001's acceptance
note ties the decision to a specific verified run (contact-7, `dcef1518`). This is
better ADR hygiene than most production codebases.

**Traceability requirements -> design -> deployed is present and bidirectional.** The
specs under `.kiro/specs/` follow a `requirements.md` / `design.md` / `tasks.md`
triad (verified for `architecture-refinement` and `clean-compute-pipeline`).
Requirement IDs propagate into the code: `batch_compute/iam.tf` cites "Req 8.2, SEC-1"
for the namespace-conditioned metric; `main.tf` chunk entrypoint cites "Req 1.8" for the
idempotent prefix clear; `monitoring.tf` cites "R3-AC1", "R4-AC1", "REL-3". The reverse
direction holds too: `ARCHITECTURE.md` and `ARCHITECTURE_REFINEMENT.md` cite specific
files/resources for each finding. An incoming architect can walk from an acceptance
criterion to the resource that satisfies it and back.

**Diagrams are accurate and at the right abstraction levels.** There are container/
sequence-level Mermaid diagrams in `ARCHITECTURE.md` (the "one contact end to end"
sequence, the processing-chain flow), diagram-as-code sources
(`docs/diagrams/architecture.yaml`, `architecture-refinement.yaml`, rendered via awsdac),
and a detailed component-level ASCII diagram (`docs/Asciiart-archi.txt`) that shows BOTH
the retired CodeBuild topology and the current all-Batch topology side by side, with the
Spot-vs-on-demand CE split, the shared-vs-dedicated image distinction, and the
RT-STPS-stays-on-EC2 note. The pre-migration diagram is explicitly labelled
"RETIRED 2026-10-22 ... Kept for history," so a reader cannot mistake it for current.

**What an incoming architect could reconstruct -- and could not.** Reconstructable from
the repo alone: the entire async DigIF path, the strangler mechanics, the IAM model, the
cost model, the accepted tradeoffs (with revisit triggers), and the failure history. NOT
reconstructable from the repo alone (and correctly flagged as such): the hand-installed
`/opt/rt-stps` and `/opt/SDR_4_1` contents and the sync receiver's patched `jpss1.xml`
-- these exist in no Terraform, Dockerfile, or `user_data`, and the drift check only
asserts presence/non-emptiness, not correctness. This is the single documentation/
reproducibility gap, and the docs name it repeatedly rather than hiding it.

Documentation verdict: **Production-grade.** The only improvement would be to lift the
`/opt/*` install procedure itself into version control (even as a documented runbook with
vendor tarball provenance), so detection is backed by a repair path.

---

## Risks and anti-patterns

Grouped by severity, distinguishing genuine gaps from documented demonstrator tradeoffs.

### Genuine gaps (would block production)

1. **Hand-installed `/opt/*` software outside IaC (OE-2).** `/opt/rt-stps`,
   `/opt/SDR_4_1` (aggregation EC2) and the sync receiver's `jpss1.xml` are installed by
   hand; `aws_instance.aggregation` and `aws_instance.receiver` carry
   `lifecycle { ignore_changes = [ami, root_block_device] }` precisely so Terraform does
   not destroy them. Reconstruction is DEFERRED; only drift DETECTION exists
   (`ssm_drift_check.tf`). Risk: instance loss = weeks of manual rebuild; no RPO. For a
   demonstrator this is a documented, accepted compromise; for production it is priority 1.

2. **SDR/VIIRS image-build source-ref coupled to `main` (deploy-ordering hazard).** Both
   image-build CodeBuild projects build from GitHub `sourceVersion = main`
   (`ARCHITECTURE.md`, `aws-knowledge.md`). A Batch job runs image-baked code, so code on
   a feature branch is invisible until merged + rebuilt; worse, if the orchestrator flag
   is flipped to `batch` while `:latest` is stale, a real contact runs the wrong code.
   Captured as a live lesson (contact `dcef1518`), with mitigations documented but not
   enforced in automation.

### Lower-severity / partially mitigated

3. **Sync path has no S3 buffer (REL-5).** A missed pre-AOS arm loses the pass outright;
   arming is fire-and-forget. Mitigated by the REL-6 fixed-IP ENI and the REL-5 PREPASS
   `ReceiverReadyFailure` readiness alarm, but the architectural single-point-of-failure
   remains. The path is currently un-flyable (onboarding blocked), so the operational
   exposure is zero today.

4. **No remote-state DR.** Terraform state is in a single-region S3 bucket with
   `use_lockfile=true` (`backend.tf`); versioning is on, but there is no cross-region
   replication of state and no documented state-recovery runbook. Low likelihood, high
   blast radius if the state bucket is lost.

5. **Default VPC / default subnets.** All compute uses `data.aws_vpc.default` and
   `data.aws_subnets.default` (`batch_compute/main.tf`, both `ec2.tf`). Acceptable for a
   demonstrator; a production deployment would want a dedicated VPC with private subnets
   + VPC endpoints (removing the `0.0.0.0/0` egress).

### Documented demonstrator tradeoffs (NOT gaps -- consciously chosen, with revisit triggers)

- **Single shared KMS CMK** across S3/ECR/CloudTrail/SNS/Logs/EBS and both pipelines
  (`security/main.tf`). Operational simplicity over blast-radius isolation; rotation on,
  least-privilege key policy. Revisit when a compliance boundary or second tenant appears.
- **Single region, no cross-region DR** (`checkov:skip=CKV_AWS_144` on output buckets).
  S3 is a replayable cache; the system of record is Ground Station / Partner Central.
- **Single-AZ sync receiver** (`subnets.ids[0]`). ENI pinning + readiness check address
  the realistic failure modes; multi-AZ would not help a pre-positioned per-contact receiver.
- **`ToleratedFailurePercentage = 100`** on the chunk Map. Partial results are useful;
  degradation made visible via `ChunksSucceeded/Failed` + the all-failed alarm.
- **Account-wide budget** rather than GS-service-scoped (a working ceiling beats a
  mis-scoped one; ready-to-enable `cost_filter` committed commented).
- **Disabled scheduler cron.** Booking a paid contact is a deliberate human act after the
  cron booked two unintended contacts. Backed by the budget guardrail.
- **CloudTrail single-region, management-events-only** (SEC-4). Named as an accepted
  demonstrator choice; the trail bucket was hardened (versioning on, `force_destroy` dropped).
- **Provider pinning** `aws ~> 5.0`, `awscc ~> 1.0`, `archive ~> 2.0`,
  `required_version >= 1.5` (`versions.tf`). Pragmatic pessimistic pinning; reproducibility
  is backed by `.terraform.lock.hcl`. Minor: `>= 1.5` is a floor, not a ceiling -- the
  S3-native `use_lockfile` needs >= 1.10 (noted in `backend.tf`), so the effective floor is
  higher than `versions.tf` states. Worth tightening to `>= 1.10`.

---

## Production-hardening roadmap (ranked)

Ranked by blast radius x likelihood, aligned to the project's own prioritisation in
`ARCHITECTURE_REFINEMENT.md` and extended with the SA-lens items above.

1. **Codify the `/opt/*` software reconstruction (close OE-2 fully).** Move the RT-STPS /
   CSPP / SDR_4_1_DB install from hand-work into a reproducible, version-controlled path:
   an Image Builder recipe or an SSM Document that reconstructs `/opt/*` from vendor
   tarballs staged in S3 (with checksums), validated against one real contact before the
   DEFERRED flag is removed. This converts the drift *detection* already in place into a
   drift *repair* capability and removes the only non-reconstructable part of the system.

2. **Make the image-build deploy safe and ordered.** Pin image-build `sourceVersion` to an
   immutable tag per release (not the moving `main`), gate the `RENDER_COMPUTE`/`use_batch_*`
   flips behind a check that `:latest` was rebuilt from the intended ref (verify ECR
   `imagePushedAt` advanced), and add a post-deploy canary that verifies product/metric
   evidence (not exit code) before declaring a stage live. Encodes the `aws-knowledge.md`
   lesson as automation.

3. **Give the sync path a replayable buffer and a real readiness gate (REL-5).** Add a
   cheap raw-capture tee to S3 so a decode failure is replayable, and treat first-contact
   packet-format validation as a gate before relying on the path. This is the highest-risk
   design element and must be de-risked before the sync path is ever promoted from
   experimental -- sequence it with the onboarding unblock.

4. **Introduce a dedicated VPC with private subnets + VPC endpoints, and remote-state DR.**
   Replace default-VPC usage with a purpose-built network (private subnets, S3/ECR/Logs/KMS
   gateway + interface endpoints) to eliminate the `0.0.0.0/0` egress and the HTTP-80 CSPP
   exception at the network edge; add cross-region replication + a documented recovery
   runbook for the Terraform state bucket.

5. **Decide the per-domain KMS and audit posture, and tighten provider floor.** For any
   production or multi-tenant use, split the single CMK along a compliance boundary (S3/data
   vs logs/audit at minimum) and make CloudTrail multi-region with data-event selectors on
   the data buckets so the audit answers "who did what to the data," not just the management
   API. Tighten `required_version` to `>= 1.10` to match the S3-native lock requirement.

Each item is additive and independently shippable; none require re-architecting the
async pipeline, which is production-shaped already.

---

## Closing assessment

For its stated purpose -- a NOAA-20 DigIF -> VIIRS L1 demonstrator -- the architecture is
adequate-to-strong, the Well-Architected posture is Strong on Security/Performance/Cost and
Adequate on Operational Excellence/Reliability/Sustainability, and the documentation is
production-grade and traceable. The team's habit of turning incidents into codified
constraints and recording tradeoffs with explicit revisit triggers is the project's
defining strength. The known gaps (hand-installed `/opt/*`, image-build `main` coupling,
sync-path SPOF) are known, ranked, and either consciously deferred or already mitigated --
which is exactly what a reviewer wants to find.
