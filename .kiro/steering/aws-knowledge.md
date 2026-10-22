---
inclusion: always
---

# AWS Service Knowledge — Ground Station SDR Pipeline

Project-local learnings about AWS service behaviour, constraints, and gotchas
encountered during live pipeline runs. Supplement to global AWS knowledge.

---

## CloudWatch — Metric-Math Alarms Over Sparse Log-Metric-Filter Metrics Can Miss Transient Bursts with Short Periods

**Symptom**: A CloudWatch metric-math alarm (`IF(m1 > 0 AND m2 == 0, 1, 0)`) stays OK
even though the burst condition was demonstrably true. CloudWatch StateReason reads:
`"Threshold Crossed: 1 datapoint [0.0 (<boundary-time>)] was not greater than the threshold"` —
the boundary time is the START of the period AFTER the burst, not the burst period itself.

**Live evidence (pipeline rerun)**:
- 22 chunks failed; `ChunksFailed` summed to 44 in window 15:51–15:59 UTC;
  `ChunksSucceeded=0` throughout. `AllChunksFailed` expression was true.
- Alarm evaluated the 14:00:00 bucket (value 0.0) and missed the 13:45 bucket (value 44).

**Root cause — two effects compound**:

1. **Log metric filter ingestion latency** (~1–5 min): the real m1=44 datapoint for the burst
   period has not been committed to CloudWatch when the alarm evaluates at the period boundary.

2. **Synthetic-zero timing with `default_value=0`**: `default_value=0` causes CloudWatch to
   emit a 0 for the new period immediately. At the period boundary, the fresh synthetic-zero
   is the "most recent complete period"; the latency-delayed burst data loses the race.

With period=900 (15-min buckets) these two effects combine: burst data is ≤5 min late,
the new 15-min period has an immediate 0.0, and CloudWatch takes the 0.0.

**❌ INCORRECT — short period with `default_value=0` and a transient one-bucket burst:**
```hcl
metric_query {
  id    = "m1"
  metric {
    stat   = "Sum"
    period = 900  # burst lasts 8 min; log filter latency can make data arrive late
  }
}
# evaluation_periods = 1
# CloudWatch may pick the adjacent synthetic-zero bucket instead of the burst bucket
```

**✅ CORRECT — period wide enough that the burst is fully contained and latency is absorbed:**
```hcl
metric_query {
  id    = "m1"
  metric {
    stat   = "Sum"
    period = 3600  # 60-min bucket: 8-min burst always lands inside; 5-min latency is negligible
  }
}
# evaluation_periods = 1
# At evaluation time the 60-min bucket is the last COMPLETE period;
# the next default_value=0 datapoint belongs to the [now, now+3600) bucket which is still open.
```

**Rule**: When using metric-math alarms on **sparse log-metric-filter metrics** (metrics that
only emit data when log events match, padded with `default_value=0`), set the period to span
the entire expected burst duration **plus** at least 2× the metric filter ingestion latency.
For log metric filters: burst duration (minutes) + 10-15 min latency headroom → use the next
standard CloudWatch period that comfortably exceeds that sum.

For a fan-out burst of ≤10 min: period=3600 is safe. For longer bursts, period=3600 may
not be sufficient — consider a dedicated single-metric filter on a terminal decision state
(Option A) instead.

**Key constraint**: `terraform validate` cannot test this. Schema validation passes
regardless of period length. The only way to validate alarm evaluation logic is to run
against real (or injected) metric data. Always verify alarm behavior with a live test or
metric injection before considering an alarm spec complete.

**Trade-off of wider period**: detection latency increases proportionally (≤60 min for
period=3600 vs. ≤15 min for period=900). Acceptable for batch/satellite-contact pipelines
where the execution has already completed by the time the alarm fires.

---

## CodeBuild — Metrics That Measure Build Output Must Be Emitted From the Buildspec, Not From the Dispatching Lambda

**Symptom**: A metric (e.g. `ProductsWritten`) is always published with value 0 even
though the pipeline produces the expected output and S3 shows the correct files.
Lambda logs say: `"ProductsWritten metric published: count=0"` at the moment of
CodeBuild dispatch.

**Live evidence (contact dcef1518, Oregon #7, 2026-10-17)**:
- Pass produced 9/9 products in S3 (confirmed).
- Lambda logged `count=0` at 21:40:35 — the exact moment `start_build()` returned.
- The CodeBuild render ran for ~10-15 minutes AFTER that timestamp.

**Root cause**: The Lambda called `cloudwatch.put_metric_data` immediately after
`codebuild.start_build()`.  `start_build()` is fire-and-forget — it returns a build
ID with no waiting.  The `products/{date}/{contact}/` prefix is empty at that moment;
the build writes to it minutes later.  The metric therefore always measured an empty
prefix → always published 0.

**Why unit tests missed it**: The moto test seeded the bucket with products BEFORE
calling `handle()`, then checked that `put_metric_data` was called.  It never
simulated the real lifecycle (dispatch first, products written later), so it passed
against the buggy code.

**❌ INCORRECT — measuring output from the dispatching Lambda:**
```python
# In Lambda handle():
build_id = self._submit_codebuild(...)   # returns immediately; build not done
self._publish_products_written_metric(bucket, date, contact_id)  # always 0
```

**✅ CORRECT — emit the metric from the buildspec post_build, after the upload:**
```yaml
# In the CodeBuild buildspec post_build:
- aws s3 sync /tmp/output/ "s3://${BUCKET}/products/${DATE}/${CONTACT_ID}/" ...
# Metric comes AFTER the sync — reflects real uploaded file count:
- PRODUCT_COUNT=$(aws s3 ls "s3://${BUCKET}/products/${DATE}/${CONTACT_ID}/" --recursive 2>/dev/null | wc -l | tr -d ' ')
- aws cloudwatch put-metric-data --namespace SDRPipeline --metric-name ProductsWritten --unit Count --value "${PRODUCT_COUNT}" --dimensions "Name=ContactId,Value=${CONTACT_ID}" || echo "WARNING:ProductsWritten metric publish failed (non-fatal)"
```

**IAM implication**: The buildspec runs under the CodeBuild service role, not the
Lambda execution role.  If the metric emission is moved to the buildspec, add
`cloudwatch:PutMetricData` (conditioned on `cloudwatch:namespace = SDRPipeline`) to
the **CodeBuild service role** policy — the Lambda execution role already has it but
that permission is no longer exercised for this metric.

**Verify window**: If a post-pass verification tool (`verify_pass.py`) uses an
execution-derived window of `exec_stop + N minutes`, N must be large enough to cover
the full build duration plus latency.  With the Lambda approach N=15 min was barely
sufficient; with the buildspec approach the build adds ~10-15 min → use N≥30 min.
The test asserting the window end must be updated to match.

**Test pattern for async build output metrics:**
```python
# ❌ INCORRECT — seeds products BEFORE handle(), encodes wrong lifecycle:
def test_old():
    self._seed_products(s3, count=3)  # products exist at dispatch time → wrong
    result = orchestrator.handle(event)
    mock_cw.put_metric_data.assert_called_once()  # passes against buggy code

# ✅ CORRECT — regression gate: Lambda must NOT call put_metric_data:
def test_regression():
    # No products seeded — correct state at dispatch time
    result = orchestrator.handle(event)
    mock_cw.put_metric_data.assert_not_called()  # FAILS against old buggy code

# ✅ CORRECT — structural contract: buildspec contains metric emission after sync:
def test_buildspec_contract():
    spec = handler.SATDUMP_BUILDSPEC
    upload_pos = spec.find("aws s3 sync /tmp/output/")
    metric_pos = spec.find("put-metric-data")
    assert metric_pos > upload_pos  # metric emission follows upload, not before
```


---

## IAM & EC2 — `description` Fields Reject Non-ASCII; `terraform validate` Will NOT Catch It

**Symptom**: `terraform validate` passes and the plan is clean, but `terraform apply` fails at
resource-creation time with, for IAM roles:
```
api error ValidationError: 1 validation error detected: Value at 'description' failed to
satisfy constraint: Member must satisfy regular expression pattern:
[\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*
```
and for EC2 security groups:
```
api error InvalidParameterValue: Value (...) for parameter GroupDescription is invalid.
Character sets beyond ASCII are not supported.
```

**Root cause**: Prose written with a word-processor / LLM-generated comments often contains
**em-dash `—` (U+2014)**, en-dash `–` (U+2013), smart quotes, `×` (U+00D7), arrows `→`, or an
ellipsis `…`. When these characters leak into a `description =` **argument value** (not just a
comment), AWS rejects them:
- **IAM** `CreateRole` allows `\u0020-\u007E` + `\u00A1-\u00FF` (so Latin-1 like `×` is OK for
  IAM but the em-dash `\u2014` is NOT).
- **EC2** `CreateSecurityGroup` `GroupDescription` is **stricter — ASCII only** (`\u0020-\u007E`),
  so even a Latin-1 character fails.

`terraform validate` only checks HCL schema, not AWS API string constraints, so this passes
validation and only fails on the live `apply` — a partial-apply hazard (some resources create
before the failing one aborts the run).

**❌ INCORRECT — em-dash in a description argument:**
```hcl
resource "aws_security_group" "fanout" {
  description = "Batch fan-out compute instances — no inbound; HTTPS egress only"  # U+2014 → apply fails
}
resource "aws_iam_role" "spot_fleet" {
  description = "Spot Fleet IAM role — schema requirement for SPOT CE type"        # U+2014 → apply fails
}
```

**✅ CORRECT — ASCII only in every `description`:**
```hcl
resource "aws_security_group" "fanout" {
  description = "Batch fan-out compute instances - no inbound; HTTPS egress only"
}
resource "aws_iam_role" "spot_fleet" {
  description = "Spot Fleet IAM role - schema requirement for SPOT CE type"
}
```

**Rule**: Keep `description` (and `name`/tag) argument values ASCII-only. Before `apply`, grep
the module for non-ASCII — this catches it where `validate` cannot:
```powershell
$env:PYTHONUTF8="1"; Select-String -Path "infra/modules/<mod>/*.tf" -Pattern "[^\u0000-\u007F]"
```
It should return nothing. Safest to keep comments ASCII too, so a later copy-paste of comment
prose into a description cannot reintroduce the problem. Replace `—`/`–`→`-`, `×`→`x`, `→`→`->`,
smart quotes→`"`/`'`, `…`→`...`.


---

## AWS Batch on EC2 Spot — Five Launch-Blocking Gotchas Found by a Live Canary (none caught by `terraform validate`)

A single-chunk canary job on a new Batch-on-Spot compute environment surfaced FIVE
distinct, sequential failures before a job could run — every one invisible to
`terraform validate` and most invisible to a dry plan. Run a canary before wiring Batch into
any orchestrator.

**1. Org SCP requires an `Owner` request-tag on EC2 instance/volume/vpc creation.**
Symptom: job stuck `RUNNABLE` forever, `desiredvCpus>0` but zero instances; CloudTrail
`RunInstances` → `Client.UnauthorizedOperation ... explicit deny in a service control policy`.
Decode with `aws sts decode-authorization-message` (needs admin creds) → statement
`DenyEC2ResourceWithoutOwnerTag`, condition `aws:RequestTag/Owner` must be present.
Fix: the Batch launch template `tag_specifications` for BOTH `instance` and `volume` must carry
the `Owner` tag (merge the common tag set). CE `compute_resources.tags` alone is not enough —
the volume needs it too.

**2. Org SCP / EBS guardrail requires encrypted root volume; AMI default is unencrypted.**
Symptom: `RunInstances` denied on `volume/*`, OR instances launch then self-terminate.
The ECS-optimised AL2023 AMI root volume is `Encrypted:false` and account EBS-encryption-by-default
may be off. Fix: launch template `block_device_mappings` with `ebs { encrypted = true }`.

**3. Customer CMK on a Spot EBS volume → `Client.InvalidKMSKey.InvalidState`.**
Symptom: instances launch (RunInstances SUCCESS) then immediately TERMINATE; check
`describe-instances ... StateReason` → `Client.InvalidKMSKey.InvalidState: The KMS key provided
is in an incorrect state` (even though the key is ENABLED). Root cause: with EC2 Spot, the EBS
volume is created by the EC2 Spot **service-linked role** (in an AWS-owned account) which cannot
use a customer CMK unless the CMK key policy grants it. Fix for a throwaway root volume: use
`encrypted = true` with **no `kms_key_id`** → the `aws/ebs` AWS-managed key, which the Spot SLR
can always use. The root volume is ephemeral scratch; the science product is encrypted with the
CMK in S3. Don't grant the Spot SLR access to the pipeline CMK just for a scratch disk.

**4. Launch-template version pinning — `$Latest`/`$Default` do NOT force CE re-resolution.**
Symptom: you fix the launch template, apply, but the CE keeps launching the OLD template content.
Root cause: AWS Batch resolves the LT version when the CE is created/updated; an in-place LT
update does not re-trigger CE resolution, and `version = "$Latest"`/`"$Default"` are static
strings that produce no Terraform diff when the underlying version number advances. Fix: on the
LT set `update_default_version = true`, and on the CE set
`launch_template { version = aws_launch_template.X.latest_version }` (the NUMERIC attribute).
The number changes on every LT edit → CE attribute diff → CE replacement (pair with
`create_before_destroy` + `compute_environment_name_prefix`) → Batch re-resolves the LT.
Beware: `latest_version` in a computed chain can throw `Provider produced inconsistent final
plan` when applying a SAVED plan file — re-run `terraform apply` WITHOUT the stale `-out` plan
(let it recompute) and it succeeds.

**5. Root volume must fit the DECOMPRESSED image, not the compressed size.**
Symptom: job reaches `STARTING`, container `PENDING` for 20-40 min, then task STOPPED with
`CannotPullContainerError: failed to register layer: write /opt/.../file: no space left on
device`. Root cause: the SDR image is ~20 GiB COMPRESSED; Docker decompresses all layers onto
the root volume at pull time, and a 60 GiB volume (sized only for `/tmp` scratch) overflows. Fix:
size the root volume for decompressed-image + OS + scratch. For the ~20 GiB SDR image → 200 GiB
gp3 (ephemeral, deleted on termination, negligible real cost). Check image size with
`aws ecr describe-images --query 'imageDetails[].imageSizeInBytes'`.

**6. Batch `Ref::` parameter substitution works in `command`, NOT in `environment[].value`.**
Symptom: container runs but fails immediately with the LITERAL string, e.g.
`Invalid bucket name "Ref::input_bucket"`. Root cause: a job definition that wires
`environment = [{ name = "INPUT_BUCKET", value = "Ref::input_bucket" }]` does NOT get the
`Ref::` resolved — `Ref::` placeholders are only substituted inside the `command` array.
Fix: pass the real values as environment variables at submit time via
`containerOverrides.environment` (which is exactly how a Step Functions `submitJob` Map passes
per-item values), rather than relying on `Ref::` in the job-definition `environment`. The SFN
`:::batch:submitJob.sync` integration sets `ContainerOverrides.Environment[]` per item.

**Overall rule**: a Batch compute environment reporting `VALID/ENABLED/Healthy` proves only that
the CONFIG was accepted — it does NOT prove an instance can launch, encrypt, pull the image, and
run. Always run a one-chunk canary end-to-end before wiring Batch into a production orchestrator.


---

## Step Functions + `batch:submitJob.sync` — Needs `events:PutRule` on the Batch Managed Rule, and IAM Propagation Can Fail the Same-Apply SFN Update

**Symptom**: `terraform apply` that (a) creates/updates the SFN execution role's inline policy
AND (b) updates the state-machine definition to use `arn:aws:states:::batch:submitJob.sync` in
the SAME apply fails with:
`UpdateStateMachine ... AccessDeniedException: '<sfn-role>' is not authorized to create managed-rule`
even though the policy granting `events:PutRule` was just created in the same run.

**Root cause (two parts)**:
1. The `.sync` (synchronous) service integrations for Batch (and ECS, etc.) require Step
   Functions to create an **EventBridge managed rule** (`StepFunctionsGetEventsForBatchJobsRule`)
   so it can receive job-state-change events. The SFN role therefore needs
   `events:PutRule`, `events:PutTargets`, `events:DescribeRule` on
   `arn:aws:events:REGION:ACCOUNT:rule/StepFunctionsGetEventsForBatchJobsRule` — this is in
   addition to `batch:SubmitJob` / `batch:DescribeJobs` / `batch:TerminateJob`.
2. Even with the policy correct, IAM is eventually-consistent. When Terraform creates the inline
   policy and then calls `UpdateStateMachine` milliseconds later, the policy may not have
   propagated to the SFN service's authorization check yet, so the update is denied.

**Fix**: the policy was already correct; simply **re-run `terraform apply`** after a short delay
(~15 s). The second apply finds the policy propagated and the `UpdateStateMachine` succeeds. The
first apply is not "wrong" — it created the IAM policy (and any other non-dependent resources);
only the SFN update raced the IAM propagation. This is benign and self-healing on retry.

**IAM policy the SFN role needs for `batch:submitJob.sync`:**
```
batch:SubmitJob     -> job-queue ARN + job-definition ARN (unversioned name form is fine)
batch:DescribeJobs  -> "*"  (not resource-scopeable)
batch:TerminateJob  -> "*"  (not resource-scopeable; required by .sync cleanup)
events:PutRule / PutTargets / DescribeRule
                    -> arn:aws:events:REGION:ACCOUNT:rule/StepFunctionsGetEventsForBatchJobsRule
```

**Rule**: when adding a `.sync` Batch/ECS integration to a Step Functions state machine, grant
the managed-rule EventBridge permissions in the SAME change, and expect that the first apply
after creating those permissions may need one retry due to IAM propagation. Budget for a
re-apply rather than treating the first `AccessDeniedException: not authorized to create
managed-rule` as a code defect.


---

## PowerShell + bash Subshell -- SSO Token Not Inherited by `bash script.sh`

**Symptom**: `aws s3 cp ... --sse aws:kms` fails with
`An error occurred (InvalidToken) when calling the PutObject operation: The provided token is
malformed or otherwise invalid` -- but ONLY when the AWS CLI is invoked from inside a `bash`
subshell (`bash scripts/deploy_foo.sh`). Running the exact same `aws s3 cp` DIRECTLY in
PowerShell succeeds. `aws sts get-caller-identity` works in both.

**Root cause**: On Windows, invoking `bash scripts/foo.sh` from a PowerShell session starts a
subshell that does NOT reliably inherit the PowerShell session's AWS environment / refreshed SSO
credential context. The bash subshell resolves credentials from a stale/incomplete view of the
SSO cache, so a write that requires a fresh token (S3 PutObject, KMS) is rejected with
`InvalidToken`, while a read-only STS call that the stale token still covers appears to work --
masking the real cause.

**Fix**: run the AWS CLI commands DIRECTLY in PowerShell rather than wrapping them in a bash
script, OR ensure the bash subshell is explicitly passed the environment. For a one-shot upload,
the direct PowerShell form is simplest and always picks up the current session:
```powershell
$env:AWS_PROFILE="..."; $env:AWS_REGION="eu-central-1"
aws s3 cp scripts/aggregation_entrypoint.sh "s3://.../software/aggregation_entrypoint.sh" \
  --sse aws:kms --sse-kms-key-id "<cmk>"
```

**Rule**: when a `bash`-wrapped AWS command fails with `InvalidToken` on Windows but
`get-caller-identity` succeeds, do NOT assume the SSO session is broken and force a re-login
loop -- first re-run the SAME command directly in PowerShell. If that works, the problem was the
bash subshell's credential inheritance, not the session. The repo's `scripts/deploy_*.sh` helpers
are fine on Linux/CI but on a Windows operator box prefer the direct PowerShell `aws` invocation.
