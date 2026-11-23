# Sync pipeline dry run plan

Dry run for the synchronous (demod/decode) pipeline, following the rule in
[`delegation-and-safety.md`](../.kiro/steering/delegation-and-safety.md):

> A dry run before a paid contact exercises the REAL live path: the same triggers,
> the same principals (invoke the real Lambda / state machine with a test event;
> never substitute operator/admin credentials), the same instance state at start
> (stopped), the same network path. Every deviation is listed BEFORE the dry run,
> with how that gap is validated another way.

**Execute only after the plan from branch `fix/sync-contact8-failures` is applied**
(4 changes plus the 4 network canary resources; changes: `module.security.aws_iam_role_policy.groundstation`,
`module.sync_pipeline[0].aws_iam_role_policy.receiver_arm_lambda`,
`module.sync_pipeline[0].aws_lambda_function.receiver_arm`,
`module.sync_pipeline[0].aws_security_group.receiver`). Do not run on a stale
deployment -- the three Contact #8 fixes (DFEG role EC2 permissions, Lambda role KMS
permissions, receiver security group rules) must all be live.

## Why this plan exists

Contact #8 (2026-11-22) failed on two independent configuration faults (a third, the
security group, was found afterwards while preparing this dry run) that the
prior dry run (2026-11-20) could not see, because that dry run ran with operator
credentials instead of the real Lambda principal. See `docs/CONTACTS.md` Contact #8
outcome and the deviation record added to that entry.

This plan corrects both gaps: the real Lambda is invoked (not an operator SSM command),
and the DFEG IAM fix is validated via policy simulation before the dry run runs.

## Deviations from live conditions

Read and act on this table BEFORE running any step.

| # | Deviation | How validated | Status |
|---|-----------|--------------|--------|
| 1 | Fake contact ID (`dryrun-0000-0000-0000-000000000001`). Both `handler.py` and the state machine use it only as a path string -- no Ground Station API lookup on the contact ID in either component. | None needed: the ID is a passthrough argument to `sync_receiver_arm.sh` and to `States.Format` path strings in the state machine. Confirmed by reading `lambdas/receiver_arm/handler.py` and `infra/modules/sync_pipeline/step_functions.tf`. | Acceptable |
| 2 | No satellite UDP stream. Replaced by the network canary (Step 1b): the temporary `groundstation-noaa20-sync-network-canary` Lambda, in the receiver subnet with the receiver security group, sends 100 test UDP datagrams to the receiver ENI private IP on port 55888 while the listener is armed by the REAL `receiver_arm` Lambda. Its datagrams follow the same SG rules as the Ground Station dataflow endpoint ENI. | Step 1b: listener counters (`packets_received = 100`) and `raw/raw_capture.bin` (about 100000 bytes) in S3 after the real finish path. The canary carries no satellite data, so the packet format stays unvalidated (see "What this dry run does NOT validate"; REL-5 covers it). | Network path validated by the canary; packet format open until first real contact |
| 6 | Security group (third Contact #8 fault): the rules were the Ground Station agent pattern (prefix-list ingress, no UDP egress), wrong for a dataflow endpoint ENI. Fixed by commit `cae5ccf`: self-referencing UDP ingress and egress on port 55888 on the shared SG (`infra/modules/sync_pipeline/ec2.tf`). | Read against https://docs.aws.amazon.com/ground-station/latest/ug/dataflows.vpc-configuration.html, then exercised by the canary (Step 1b): it uses the same SG, so its datagrams only arrive if the self-referencing UDP rules work. | Validated by Step 1b (SG rules only, not the Ground Station ENI itself) |
| 3 | DFEG ENI creation by Ground Station (at contact time only -- no dry-run mechanism can trigger it). The IAM fix grants the 7 required EC2 actions to the Ground Station role. | `simulate-principal-policy` on the updated `groundstation-noaa20-demo-groundstation-role` for `ec2:CreateNetworkInterface`, `ec2:DeleteNetworkInterface`, `ec2:CreateNetworkInterfacePermission`, `ec2:DeleteNetworkInterfacePermission`, `ec2:DescribeSubnets`, `ec2:DescribeVpcs`, `ec2:DescribeSecurityGroups` -- all `allowed`, including region condition (prior step, branch `fix/sync-contact8-failures`). | Pre-validated via simulation; IAM cannot be exercised further offline |
| 4 | EC2 start KMS path. `StartInstances` on `i-06a51f5b29e26aeaa` requires the receiver_arm Lambda role to call `kms:CreateGrant` etc. via `ec2.eu-central-1.amazonaws.com` with `kms:GrantIsForAWSResource=true`, because the root volume is encrypted with the pipeline CMK (`70451aac-...`). The fix adds those permissions to the Lambda role. | `simulate-custom-policy` on the updated `receiver_arm_lambda` policy for `kms:CreateGrant`, `kms:Decrypt`, `kms:GenerateDataKeyWithoutPlaintext`, `kms:DescribeKey` with `kms:ViaService=ec2.eu-central-1.amazonaws.com` and `kms:GrantIsForAWSResource=true` -- all `allowed` (prior step). The key policy allows `kms:*` for the account root, which delegates to IAM -- no key policy change needed. The simulation evaluates identity policies only; key policy evaluation and the actual EC2-to-KMS runtime call are exercised live in Step 1 when the Lambda calls `start_instances`. | Exercised live in Step 1 |
| 5 | Canary payload is not real satellite data. With `"vita49": true` (Step 1b) the canary frames each datagram as VITA-49 Signal Data (28-byte header, so our own assumption of the layout), and the listener extracts a non-empty `combined.cadu`. The CADU bytes are filler, not PN-encoded CCSDS frames, so RT-STPS finds no frames and produces no RDR. The finish script uploads both `raw/raw_capture.bin` and `cadu/combined.cadu`, the real state machine runs `SubmitBatchAggregation`, and the job fails on the sync zero-RDR strict check. The execution ends via `AggregationFailure` + SNS (`FailExecution`). | Expected: SNS "Sync Pipeline - Aggregation Failed" and status `FAILED`. This is the correct end state for the dry run -- not a pipeline error. It exercises FinishReceiver, the CADU upload, Batch submission and the strict check. | Expected / acceptable |
| 7 | EventBridge triggers bypassed by direct invocation. Steps 1 and 2 invoke the Lambda and the state machine directly, so the PREPASS and COMPLETED rules and their input transformers do not fire. | Validated by the orchestrator on 2026-11-23: (a) PREPASS rule proven live on contact #8 (`receiver_arm` invoked 10:54:35 UTC = PREPASS start). (b) COMPLETED rule `groundstation-noaa20-contact-completed-sync`: `aws events test-event-pattern` with the real contact #8 event (status set to COMPLETED) returns True; it returns False for `contact-completed-sdr` and `contact-prepass-sync`. Input paths: `contact_id=$.detail.contactId`, `contact_time=$.time`, `bucket` = sync output bucket. (c) `simulate-principal-policy` for role `groundstation-noaa20-sync-eventbridge`, `states:StartExecution` on the sync state machine: allowed. | Validated (rule match and permission); not exercised end to end until the live contact |

**Note on the IAM simulations:** the simulator evaluates identity policies only. It does
not confirm that Ground Station or EC2 succeed at runtime, and it does not evaluate the
KMS key policy. The key policy was read manually and confirmed to have `kms:*` for the
account root, which is sufficient (prior step "Not verified" note covers this limit).

## Prerequisites

Confirm all of the following before starting Step 1.

**1. Plan applied and confirmed clean.**

After the orchestrating session applies the plan, re-run to confirm 0 changes:

```powershell
terraform -chdir=infra plan -var-file=terraform.tfvars 2>&1 | Select-String "No changes"
```

**2. Instance stopped.**

```powershell
aws ec2 describe-instances `
  --instance-ids i-06a51f5b29e26aeaa `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "Reservations[0].Instances[0].State.Name" --output text
```

Expected: `stopped`.

**3. Receiver prerequisites installed.**

The three `/opt/scripts` files must be present and executable on the instance
(see `SYNC_PIPELINE.md` "Manual prerequisites"). Check via the `OptDriftDetected`
CloudWatch alarm (OK = files present and unchanged) or run a one-off SSM command:

```powershell
aws ssm send-command `
  --instance-ids i-06a51f5b29e26aeaa `
  --document-name AWS-RunShellScript `
  --parameters 'commands=["ls -la /opt/scripts/sync_receiver_*.{sh,py} 2>&1"]' `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --output text --query "Command.CommandId"
```

Then `get-command-invocation` on that command ID and confirm all three files appear
with executable permissions.

**4. Alarms in OK state and SNS subscription confirmed.**

The sync pipeline alarms (`ReceiverReadyFailure`,
`groundstation-noaa20-sync-receiver-arm-errors`, the Step Functions failure alarm)
must be `OK` and their SNS topic must have a confirmed email subscription (you should
receive the AggregationFailure notification in Step 2).

**5. Capture Terraform outputs.**

```powershell
$BUCKET  = terraform -chdir=infra output -raw sync_pipeline_output_bucket_name
$SFN_ARN = terraform -chdir=infra output -raw sync_pipeline_state_machine_arn

Write-Host "Bucket  : $BUCKET"
Write-Host "SFN ARN : $SFN_ARN"
```

## Step 1 -- arm the receiver (PREPASS path)

Invoke the real `groundstation-noaa20-sync-receiver-arm` Lambda with an event shaped
exactly as the EventBridge PREPASS rule produces it.

### Event shape derivation

The raw Ground Station Contact State Change event for PREPASS (derived from the
Contact #8 event in message #11, substituting `contactStatus = "PREPASS"` and the
sync mission profile ARN):

```json
{
  "version": "0",
  "id": "00000000-0000-0000-0000-000000000000",
  "detail-type": "Ground Station Contact State Change",
  "source": "aws.groundstation",
  "account": "471112743408",
  "time": "2026-11-25T10:00:00Z",
  "region": "eu-central-1",
  "resources": [
    "arn:aws:groundstation:eu-central-1:471112743408:contact/dryrun-0000-0000-0000-000000000001"
  ],
  "detail": {
    "contactId": "dryrun-0000-0000-0000-000000000001",
    "groundstationId": "Stockholm 1",
    "missionProfileArn": "arn:aws:groundstation:eu-central-1:471112743408:mission-profile/2e719f99-35f5-4720-b974-ba177036c1ee",
    "satelliteArn": "arn:aws:groundstation::471112743408:satellite/33f035e1-73f7-47a5-9df8-fbc48636dca8",
    "contactStatus": "PREPASS"
  }
}
```

The `input_transformer` in `infra/modules/sync_pipeline/eventbridge.tf` (rule
`contact_prepass`, target `arm_receiver`) extracts `$.detail.contactId` as
`contact_id` and `$.time` as `contact_time`, then injects the static bucket.
The Lambda therefore receives:

```json
{
  "contact_id": "dryrun-0000-0000-0000-000000000001",
  "contact_time": "2026-11-25T10:00:00Z",
  "bucket": "<sync_pipeline_output_bucket_name>"
}
```

### Commands

```powershell
# Set $BUCKET from Prerequisites step 5 first.
# Update $CONTACT_TIME to the current date/time (must be a valid ISO-8601 timestamp).

$CONTACT_ID   = "dryrun-0000-0000-0000-000000000001"
$CONTACT_TIME = "2026-11-25T10:00:00Z"

$ARM_PAYLOAD = @{
  contact_id   = $CONTACT_ID
  contact_time = $CONTACT_TIME
  bucket       = $BUCKET
} | ConvertTo-Json -Compress

$ARM_PAYLOAD | Out-File -FilePath "$env:TEMP\dryrun_arm_event.json" -Encoding utf8

aws lambda invoke `
  --function-name groundstation-noaa20-sync-receiver-arm `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --cli-binary-format raw-in-base64-out `
  --payload "file://$env:TEMP\dryrun_arm_event.json" `
  "$env:TEMP\dryrun_arm_response.json"

Get-Content "$env:TEMP\dryrun_arm_response.json"
```

### Expected outcome

The Lambda has a 420 s timeout (plenty for the dry run). Expected behaviour:

| Lambda action | Log line | Expected value |
|--------------|----------|---------------|
| Start | `receiver_arm_start` | `instance_id = i-06a51f5b29e26aeaa` |
| EC2 start | `instance_start_requested` | (no error) |
| State poll | `instance_state_poll` | `state = running` within ~60 s |
| SSM poll | `ssm_online_poll` | `ping_status = Online` within ~120 s |
| SSM command | `ssm_command_sent` | `command_line = /opt/scripts/sync_receiver_arm.sh ...` |
| Return | `receiver_arm_complete` | `command_id = <id>` |

The response file must have no `FunctionError` key. If it does, stop here and check
CloudWatch log group `/aws/lambda/groundstation-noaa20-sync-receiver-arm` -- the most
likely cause is a KMS gap not covered by the identity-policy simulation (key policy
evaluation or an EC2-to-KMS runtime condition the simulator was not given).

**This step exercises the KMS fix from Contact #8** (deviation 4). A successful
Lambda return -- instance running, SSM command sent -- confirms the fix is live.

### Verify the listener is bound (optional but recommended)

```powershell
# Replace <COMMAND_ID> with the command_id from the Lambda response.
$COMMAND_ID = (Get-Content "$env:TEMP\dryrun_arm_response.json" | ConvertFrom-Json).command_id

# Check the arm SSM command status:
aws ssm get-command-invocation `
  --command-id $COMMAND_ID `
  --instance-id i-06a51f5b29e26aeaa `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "[StatusDetails, StandardOutputContent]" --output text

# Confirm the listener socket is bound:
$VERIFY_ID = (aws ssm send-command `
  --instance-ids i-06a51f5b29e26aeaa `
  --document-name AWS-RunShellScript `
  --parameters 'commands=["ss -ulpn | grep 55888 || echo NOT_BOUND"]' `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --output json | ConvertFrom-Json).Command.CommandId

Start-Sleep -Seconds 10

aws ssm get-command-invocation `
  --command-id $VERIFY_ID `
  --instance-id i-06a51f5b29e26aeaa `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "StandardOutputContent" --output text
```

Expected: a line containing `:55888` or `0.0.0.0:55888`, status `Success`.

## Step 1b -- network canary (replaces the missing satellite stream)

Runs right after Step 1, while the listener armed by the real `receiver_arm` Lambda is
bound. Requires `enable_network_canary = true` (set in `infra/terraform.tfvars`) and the
canary resources applied. The canary is TEMPORARY: set `enable_network_canary = false`
once this check has passed.

The canary Lambda (`lambdas/network_canary/handler.py`) runs in the receiver subnet with
the receiver security group. Defaults for `target_ip` (receiver ENI private IP) and
`port` (55888) come from its environment, so the event can be minimal. Each datagram is
`GSCANARY` + a 4-byte big-endian sequence number, zero-padded to `payload_size`.

With `"vita49": true` the datagram is framed as VITA-49 Signal Data so the listener
extracts a payload: `payload_size` is then the total datagram length (rounded down to a
multiple of 4, minimum 40) and each datagram carries `payload_size - 28` payload bytes.
The response reports `expected_listener_payload_bytes` = `sent x (payload_size - 28)`
(0 when `vita49` is false). This step uses `vita49: true`, so 100 x (1000 - 28) = 97200.

```powershell
$CANARY_FN = terraform -chdir=infra output -raw sync_pipeline_network_canary_function_name
# Expected: groundstation-noaa20-sync-network-canary

@{ count = 100; payload_size = 1000; pacing = 0.001; vita49 = $true } | ConvertTo-Json -Compress |
  Out-File -FilePath "$env:TEMP\dryrun_canary_event.json" -Encoding ascii

aws lambda invoke `
  --function-name $CANARY_FN `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --cli-binary-format raw-in-base64-out `
  --payload "file://$env:TEMP\dryrun_canary_event.json" `
  "$env:TEMP\dryrun_canary_response.json"

Get-Content "$env:TEMP\dryrun_canary_response.json"
```

Expected response: `sent = 100`, `failed = 0`, `first_seq = 0`, `last_seq = 99`, `errors`
empty, `expected_listener_payload_bytes = 97200`, no `FunctionError`. UDP is fire-and-forget: `sent = 100` proves only that the
sockets accepted the datagrams. Delivery is proved by the receiver side, below.

### Verify receipt on the receiver (listener counters)

The listener logs `listener_stopped` with `packets_received` when finish stops it
(and `listener_stats` every 30 s). Wait about 35 s after the canary, then:

```powershell
$LOG_ID = (aws ssm send-command `
  --instance-ids i-06a51f5b29e26aeaa `
  --document-name AWS-RunShellScript `
  --parameters 'commands=["grep -E first_packet_received|listener_stats /var/log/sync_receiver_listen.log | tail -n 5"]' `
  --region eu-central-1 --profile AWSAdminAccess-471112743408 `
  --output json | ConvertFrom-Json).Command.CommandId

Start-Sleep -Seconds 10

aws ssm get-command-invocation --command-id $LOG_ID `
  --instance-id i-06a51f5b29e26aeaa --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "StandardOutputContent" --output text
```

Expected: a `first_packet_received` line and a `listener_stats` line with
`packets_received = 100`. With `vita49: true`, `bytes_written` equals the canary's
`expected_listener_payload_bytes` (97200).

If `packets_received` stays 0 while the canary reports `sent = 100`, the SG path is still
broken (or the canary targeted a stale IP): STOP, do not book the contact.

### After Step 2 (real finish path): S3 evidence

Step 2 runs the real `FinishReceiver` path. Afterwards:

```powershell
aws s3 ls "s3://$BUCKET/contacts/" --recursive `
  --region eu-central-1 --profile AWSAdminAccess-471112743408 | Select-String $CONTACT_ID
```

| Object | Expected | Why |
|--------|----------|-----|
| `.../raw/raw_capture.bin` | present, about 100000 bytes (100 x 1000) | The raw tee writes every UDP payload, whatever its content. Proves ENI -> listener -> tee -> finish upload (KMS) end to end. |
| `.../cadu/combined.cadu` | present, non-empty, size = the canary's `expected_listener_payload_bytes` (97200) | The `vita49: true` datagrams carry a Signal Data header, so the listener extracts the payload. |
| `.../logs/sync_receiver_finish.log` | present | Uploaded by the finish script on exit. |

The VITA-49 framing is our own assumption of the layout, so a non-empty `combined.cadu`
proves the listener/finish/upload mechanics, not the real satellite format (see "What
this dry run does NOT validate"). The payload is filler, not PN-encoded CCSDS frames, so
Step 2 reaches `SubmitBatchAggregation` and ends in `AggregationFailure`. If it ends in
`ReceiverFailure` instead, the finish path is broken: investigate before booking.
Check `raw_capture.bin` size:

```powershell
aws s3api head-object --bucket $BUCKET `
  --key "contacts/<date>/$CONTACT_ID/raw/raw_capture.bin" `
  --region eu-central-1 --profile AWSAdminAccess-471112743408 `
  --query ContentLength
```

Expected: `100000`. Cleanup: also delete `$env:TEMP\dryrun_canary_event.json` and
`dryrun_canary_response.json`.
## Step 2 -- drive the post-contact path (COMPLETED path)

Start the real state machine with an event shaped exactly as the EventBridge COMPLETED
rule produces it.

### Event shape derivation

The `input_transformer` for the COMPLETED rule (target `start_sync_pipeline` in
`eventbridge.tf`) extracts the same `contact_id` and `contact_time`, then injects
the static bucket and satellite ID. The state machine therefore receives:

```json
{
  "contact_id": "dryrun-0000-0000-0000-000000000001",
  "contact_time": "2026-11-25T10:00:00Z",
  "bucket": "<sync_pipeline_output_bucket_name>",
  "satellite_id": "33f035e1-73f7-47a5-9df8-fbc48636dca8"
}
```

### Commands

```powershell
# Set $BUCKET and $SFN_ARN from Prerequisites step 5 first.
# Keep $CONTACT_ID and $CONTACT_TIME the same as Step 1.

$SFN_INPUT = @{
  contact_id   = $CONTACT_ID
  contact_time = $CONTACT_TIME
  bucket       = $BUCKET
  satellite_id = "33f035e1-73f7-47a5-9df8-fbc48636dca8"
} | ConvertTo-Json -Compress

$EXECUTION_ARN = (aws stepfunctions start-execution `
  --state-machine-arn $SFN_ARN `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --input $SFN_INPUT `
  --output json | ConvertFrom-Json).executionArn

Write-Host "Execution ARN: $EXECUTION_ARN"
```

Poll status (repeat until `FAILED` or `SUCCEEDED`):

```powershell
aws stepfunctions describe-execution `
  --execution-arn $EXECUTION_ARN `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "[status, stopDate, currentTaskInput]" --output text
```

### Expected state machine progression

| State | Expected outcome | Notes |
|-------|-----------------|-------|
| `DeriveDateOnly`, `DeriveDateParts`, `BuildContactDate` | Pass through instantly | Date string derivation only |
| `WaitForDelivery` | 30-second pause | |
| `FinishReceiver` | SSM command issued to `sync_receiver_finish.sh <bucket> dryrun-... <date>` on `i-06a51f5b29e26aeaa`; listener stops; `raw/raw_capture.bin` and non-empty `cadu/combined.cadu` uploaded to S3 | Must exit 0. A failure here (`ReceiverFailure`) is NOT acceptable in this dry run |
| `CheckFinishStatus` / `EvaluateFinishStatus` | Poll SSM status -> `Success` -> `StopReceiver` | |
| `StopReceiver` | EC2 stop requested; instance returns to `stopped` | |
| `SubmitBatchAggregation` | Batch job submitted with `CADU_PREFIX=cadu, PN_ENCODED=true, OUTPUT_BUCKET=<bucket>`; RT-STPS finds no frames in the filler payload and the job FAILS on the sync zero-RDR strict check | Expected failure (deviation 5) |
| `AggregationFailure` | SNS notification fires, subject: "Sync Pipeline - Aggregation Failed" | Check inbox |
| `FailExecution` | Execution reaches status `FAILED` | Correct dry-run end state |

The `FAILED` execution status is **the correct end state for the dry run** -- the
structural path was exercised successfully even though the execution fails at
aggregation.

## Verification by evidence

Do not accept exit codes or execution status alone. Check each item:

```powershell
# 1. Instance stopped
aws ec2 describe-instances `
  --instance-ids i-06a51f5b29e26aeaa `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "Reservations[0].Instances[0].State.Name" --output text
# Expected: stopped

# 2. Execution status FAILED
aws stepfunctions describe-execution `
  --execution-arn $EXECUTION_ARN `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 `
  --query "status" --output text
# Expected: FAILED

# 3. Test prefix exists in S3 (empty/partial capture uploaded by finish script)
aws s3 ls "s3://$BUCKET/contacts/" --recursive `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408 | Select-String $CONTACT_ID
# Expected: at least raw/ and/or cadu/ objects under the dryrun contact prefix
```

SNS email expected: "Sync Pipeline - Aggregation Failed". No action required. A "Sync
Pipeline - Receiver Handoff Failed" email means FinishReceiver failed: investigate.

CloudWatch log group `/aws/lambda/groundstation-noaa20-sync-receiver-arm` must show
the full arm sequence (see Step 1 expected outcome table).

## Cleanup

```powershell
# Remove the test S3 prefix
aws s3 rm "s3://$BUCKET/contacts/" --recursive `
  --include "*/$CONTACT_ID/*" `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408

# Remove temp files
Remove-Item "$env:TEMP\dryrun_arm_event.json"  -ErrorAction SilentlyContinue
Remove-Item "$env:TEMP\dryrun_arm_response.json" -ErrorAction SilentlyContinue
```

If the state machine failed before `StopReceiver` (e.g., at `ReceiverFailure`), confirm
the instance is stopped and stop it manually if needed:

```powershell
aws ec2 stop-instances `
  --instance-ids i-06a51f5b29e26aeaa `
  --region eu-central-1 `
  --profile AWSAdminAccess-471112743408
```

## What this dry run does NOT validate

- **Real satellite packet format (VITA-49 Extension layout)** (deviation 2): the canary
  frames its datagrams per our own assumption of the layout, so it proves transport and
  listener mechanics only. The real Ground Station demod/decode egress layout is unknown
  until a real contact delivers a raw UDP stream. REL-5 (packet-format validation gate in
  `SYNC_PIPELINE.md`) remains open until the first real contact produces
  `raw/raw_capture.bin` in S3 and that file is inspected against the onboarding-provided
  VITA-49 spec.
- **Ground Station-side ENI creation** (deviation 3, and the Ground Station side of the
  SG fix): the IAM policy is confirmed correct by simulation. Ground Station's actual
  managed-ENI creation at contact time is not exercisable offline.
- **Transport eu-north-1 -> eu-central-1**: the Stockholm antenna to our eu-central-1 ENI
  leg is inside Ground Station and cannot be exercised; the canary starts inside our own
  subnet.
- **CSPP and visualization on sync data**: the filler payload yields no RDR, so the
  downstream CSPP and render stages never run. Validating them needs a replay of real
  PN-encoded frames (about 160 MB per VIIRS granule), which is out of scope.
- **Ground Station side behavior**: IAM simulation covers identity policies only --
  it does not prove that Ground Station, EC2, or KMS succeed at runtime.
