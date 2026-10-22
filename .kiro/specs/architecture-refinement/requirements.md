# Requirements: Architecture Refinement (Well-Architected Delta)

## Introduction

This spec closes the gap between the deployed protogroundstation pipeline and a
Well-Architected target, as laid out in [`docs/ARCHITECTURE_REFINEMENT.md`](../../../docs/ARCHITECTURE_REFINEMENT.md).
Every requirement maps to a confirmed finding in the current Terraform/handler code. The two
organising themes are **drift from IaC** (resources `terraform apply` cannot create or repair)
and **success inferred, not observed** (green executions that prove neither good chunks nor
imagery, and a pipeline that emits no failure signal).

Each requirement is independently implementable and independently verifiable. Work runs
through the generic well-architected delta workflow
([`.kiro/steering/wa-delta-workflow.md`](../../steering/wa-delta-workflow.md)) with four
agentic quality gates.

Scope note: this is a **demonstrator**. Some findings are resolved by *documenting an accepted
tradeoff* rather than engineering it away (single-region, single CMK, single-AZ receiver).
Those are explicit acceptance criteria, not hand-waving.

---

## Requirement 1 — Codify the SDR-image build project (OE-1 / SEC-3)

**User story:** As an operator, I want the SDR Docker-image CodeBuild project under Terraform,
so that `terraform apply` creates and repairs it and its privileged role is reviewable, rather
than existing out-of-band with a hand-edited S3 policy.

#### Acceptance Criteria
1. WHEN `enable_sdr_pipeline = true` THEN the `sdr_pipeline` module SHALL define a CodeBuild
   project that builds and pushes the SDR image to ECR, mirroring the codified pattern already
   used by `viirs_visualization/codebuild.tf`.
2. WHEN the project's IAM role is reviewed THEN its S3 access to the `docker-build/*` prefix
   SHALL be expressed in Terraform (not hand-added), scoped to `GetObject` + `ListBucket`.
3. WHEN `terraform plan` is run after import THEN it SHALL show no destructive diff against the
   existing out-of-band project (import, do not recreate).
4. WHEN Checkov runs THEN the privileged-mode finding SHALL be either resolved or documented
   with a `checkov:skip` and justification.

---

## Requirement 2 — Emit a real "products written" success signal (REL-4)

**User story:** As an operator, I want an explicit, queryable signal that imagery was produced
for a contact, so that I do not have to inspect the `products/` S3 prefix by hand to know
whether a run truly succeeded.

#### Acceptance Criteria
1. WHEN a contact's visualization step completes THEN the system SHALL publish a `ProductsWritten`
   CloudWatch metric equal to the count of product objects under `products/{date}/{contact}/`.
2. IF the count is zero THEN an alarm SHALL raise to the shared SNS topic.
3. WHEN the metric is published THEN it SHALL be in the `SDRPipeline` namespace, matching the
   namespace already permitted by IAM.
4. WHEN verified THEN the metric SHALL be confirmed non-empty for at least one real or simulated
   contact (closing the "namespace may be empty" gap).

---

## Requirement 3 — Alarm parity for the async pipeline (REL-1)

**User story:** As an operator, I want the async SDR pipeline to alarm on failure the way the
sync pipeline already does, so that a failed execution notifies me instead of passing silently.

#### Acceptance Criteria
1. WHEN the SDR state machine reports `ExecutionsFailed` THEN a CloudWatch alarm SHALL publish to
   the shared SNS topic.
2. WHEN the orchestrator or aggregation-trigger Lambda errors THEN a Lambda-error alarm SHALL
   publish to the shared SNS topic.
3. WHEN the module is applied THEN these alarms SHALL live in a new `sdr_pipeline/monitoring.tf`,
   consistent with `sync_pipeline/monitoring.tf`.

---

## Requirement 4 — Make swallowed visualization failure observable (REL-2)

**User story:** As an operator, I want a systematically broken visualization step to raise a
signal even though the pipeline is designed to report success, so that a persistent render
defect cannot hide behind a green execution.

#### Acceptance Criteria
1. WHEN `StartVisualization` takes its `States.ALL` catch path THEN the system SHALL publish a
   `VisualizationFailed` metric and an SNS notification.
2. WHEN the catch path is taken THEN the pipeline SHALL still report `SUCCEEDED` (products must
   survive a render failure — unchanged behaviour).
3. WHEN `VisualizationFailed` exceeds a threshold over a window THEN an alarm SHALL raise.

---

## Requirement 5 — Remove the `/opt/*` silent-destruction failure mode (OE-2)

**User story:** As an operator, I want the hand-installed `/opt/rt-stps` and `/opt/SDR_4_1`
software to be reproducible or drift-detectable, so that replacing an EC2 volume does not
silently destroy the pipeline's dependencies.

#### Acceptance Criteria
1. WHEN the receiver/aggregation instance software must be reconstructed THEN there SHALL be a
   codified mechanism (SSM Document / State Manager association / user_data reconstructing from
   the S3 vendor tarballs) that does so.
2. IF full reconstruction is deferred THEN an SSM State Manager association SHALL at minimum
   **detect drift** on the `/opt/*` paths and report it.
3. WHEN the mechanism is documented THEN `docs/ARCHITECTURE.md`'s "exists in no Terraform"
   constraint SHALL be updated to reflect the new reality.

---

## Requirement 6 — IAM least-privilege corrections (SEC-1, SEC-2)

**User story:** As a security reviewer, I want the two genuine wildcard gaps scoped down, so
that the IAM surface matches the least-privilege pattern used everywhere else.

#### Acceptance Criteria
1. WHEN `processor_lambda`'s `cloudwatch:PutMetricData` statement is reviewed THEN it SHALL carry
   a `cloudwatch:namespace` `StringEquals` condition, matching the sdr/sync pattern.
2. WHEN the `viirs_visualization` CodeBuild log policy is reviewed THEN its `Action` SHALL be the
   three write actions (`CreateLogGroup`, `CreateLogStream`, `PutLogEvents`), not `logs:*`.
3. WHEN Checkov runs after the change THEN it SHALL report no new HIGH/CRITICAL findings.

---

## Requirement 7 — De-risk the synchronous reception path (REL-5, REL-6)

**User story:** As an operator, I want the sync path to fail safely and visibly rather than lose
a paid pass silently, so that I can trust the cheaper demod/decode delivery mode.

#### Acceptance Criteria
1. WHEN the receiver is armed during PREPASS THEN the system SHALL verify the listener bound and
   first packets arrived, and SHALL alarm **before LOS** if not (preventive, not detective).
2. WHEN a contact runs THEN a raw capture SHALL be tee'd to S3 so a decode failure is replayable.
3. WHEN the packet format has not been validated against real traffic THEN this SHALL be treated
   as a gate: the path is not marked flight-ready until a captured sample is verified.
4. WHEN the dataflow endpoint group is defined THEN it SHALL bind to a stable address (fixed-IP
   secondary ENI), so stop/start cannot move the private IP out from under it.

---

## Requirement 8 — Cache CSPP ancillary/LUTs (PERF-1)

**User story:** As an operator, I want the CSPP ancillary/LUT set cached in S3, so that
aggregation does not spend ~10 minutes per contact fetching it and does not depend on reaching
the public internet at run time.

#### Acceptance Criteria
1. WHEN aggregation runs THEN it SHALL stage the ancillary/LUT set from S3 rather than fetching
   from `jpssdb.ssec.wisc.edu` on the hot path.
2. WHEN the cache is refreshed THEN it SHALL be by a scheduled job, not per contact.
3. WHEN measured THEN the aggregation wall-clock SHALL drop by approximately the `sdr_luts.sh`
   fetch time (~10 min).

---

## Requirement 9 — Cost and audit guardrails (COST-1, SEC-4)

**User story:** As an owner, I want a hard guardrail around paid contact reservation and a
hardened audit trail, so that an accidental scheduler enable cannot silently spend and the audit
log is no weaker than the data it audits.

#### Acceptance Criteria
1. WHEN the account's Ground Station spend approaches a threshold THEN a budget alarm SHALL fire.
2. WHEN the CloudTrail log bucket is reviewed THEN it SHALL have versioning enabled and SHALL NOT
   use `force_destroy = true`.
3. WHEN the trail's scope (single-region, management-events-only) is a deliberate demonstrator
   choice THEN it SHALL be documented as an accepted tradeoff alongside the existing no-DR skips.

---

## Requirement 10 — Document accepted tradeoffs and the buildspec contract (REL-7, OE-3, COST-2, PERF-2)

**User story:** As a future maintainer, I want the deliberate tradeoffs and the
buildspec-override contract written down, so that an accepted decision is not mistaken for a bug.

#### Acceptance Criteria
1. WHEN the docs are reviewed THEN single-region/no-DR, single shared CMK, and single-AZ receiver
   SHALL each be recorded as an accepted tradeoff with its rationale.
2. WHEN the visualization buildspec-override contract is reviewed THEN it SHALL be documented
   (NO_SOURCE stub + override from the handler) and guarded by a test asserting the stub cannot
   run standalone.

---

## Requirement 11 — Compute-platform tradeoff study (OPEN-1)

**User story:** As an architect, I want a documented tradeoff study on whether CodeBuild is the
right compute for chunk fan-out and aggregation, so that the single largest cost lever in the
pipeline is a deliberate decision rather than an inherited default.

Context: CodeBuild is a CI/CD build service currently used as a batch compute engine
(490+ build-minutes/contact). It was chosen only because CSPP's `sdr_luts.sh` needed internet
and the EC2 aggregation instance could not reach it. Requirement 8 (PERF-1) removes that
constraint by caching the LUTs in S3 — which reopens the question.

#### Acceptance Criteria
1. WHEN Requirement 8 (PERF-1) is complete THEN the study SHALL be performed (PERF-1 is a
   prerequisite — it de-risks every non-CodeBuild option).
2. WHEN the study is written THEN it SHALL compare at minimum: CodeBuild (status quo), AWS Batch
   on Fargate, AWS Batch on EC2 Spot, ECS/Fargate via Step Functions, and EC2 + LUT cache — each
   with measured or estimated per-contact cost and wall-clock, and a cost-of-change.
3. WHEN the study concludes THEN it SHALL record a recommendation and a decision (keep or
   migrate) as an ADR-style entry in `docs/ARCHITECTURE_REFINEMENT.md`.
4. This requirement produces a **decision document**, not an implementation. Any migration that
   follows is a separate spec.
