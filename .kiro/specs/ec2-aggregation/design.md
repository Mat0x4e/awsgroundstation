# Design Document: EC2-Based Aggregation for NOAA-20 SDR Pipeline

## Overview

Replace the CodeBuild-based final aggregation step with an EC2 instance (r6i.xlarge) that has CSPP SDR and its LUT database (SDR_4_1_DB) pre-installed on persistent EBS storage. The current pipeline uses a Docker container in CodeBuild for aggregation, but CSPP SDR requires a persistent filesystem with an initialized LUT database that cannot be efficiently recreated in an ephemeral container on every run.

The EC2 instance is started on-demand by a Trigger Lambda, receives work via SSM Run Command, executes the full aggregation chain (CADU concatenation → RT-STPS → CSPP SDR → S3 upload), then stops itself to minimize cost. The Step Functions state machine is modified to replace the `FinalAggregation` CodeBuild state with a Lambda invocation + SSM polling loop.

### Key Design Decisions

1. **EC2 over CodeBuild for aggregation** — CSPP SDR's LUT database (SDR_4_1_DB) is ~15 GB and requires a one-time initialization step (`cspp_sdr_setup.sh`). Rebuilding it in a Docker container on every run adds 10+ minutes and is fragile. A persistent EBS volume eliminates this.

2. **SSM Run Command over SSH** — No SSH keys to manage, no security group inbound rules needed. SSM integrates natively with IAM for authorization and CloudWatch for logging.

3. **Self-stop pattern** — The instance stops itself after processing (success or failure), ensuring cost is minimized. A 30-minute timeout acts as a safety net against hung processes.

4. **Trigger Lambda pattern** — A Lambda function handles the EC2 start + SSM send command orchestration. This keeps the Step Functions state machine simple (single Lambda invoke + polling loop) and allows the Lambda to handle retries on instance start.

5. **Per-chunk processing unchanged** — Only the aggregation step changes. Per-chunk CodeBuild processing (IQ extraction + SatDump) remains identical.

## Architecture

```mermaid
flowchart TD
    A[Step Functions: SDR Pipeline] -->|ParallelProcessing complete| B[Trigger Lambda]
    B -->|1. StartInstances| C[EC2: Aggregation Instance<br/>r6i.xlarge, 32 GB RAM<br/>EBS 100 GB gp3]
    B -->|2. Wait for Running| C
    B -->|3. SendCommand| D[SSM Run Command]
    D -->|Execute| E[Aggregation Script<br/>/opt/scripts/aggregation.sh]

    subgraph EC2 Instance — Aggregation
        E --> E1[Download .cadu from S3]
        E1 --> E2[Concatenate → combined.cadu]
        E2 --> E3[RT-STPS jpss1.xml<br/>PnEncoded=false]
        E3 --> E4{VIIRS RDR<br/>produced?}
        E4 -->|Yes| E5[CSPP SDR viirs_sdr.sh]
        E4 -->|No| E6[Log warning, skip CSPP]
        E5 --> E7[Upload SDR + GEO to S3]
        E6 --> E7
        E7 --> E8[Upload RDR to S3]
        E8 --> E9[Self-stop: shutdown -h now]
    end

    A -->|Poll SSM status<br/>30s intervals| F{SSM Command<br/>Status?}
    F -->|InProgress| G[Wait 30s]
    G --> F
    F -->|Success| H[PipelineSucceeded]
    F -->|Failed/TimedOut| I[AggregationFailure → SNS]

    C -.->|Instance Profile| J[IAM Role:<br/>S3 + KMS + SSM]
```

### Position in Existing Infrastructure

| Existing Resource | Usage |
|---|---|
| `aws_sfn_state_machine.sdr_pipeline` | Modified: replaces `FinalAggregation` CodeBuild state with Lambda + SSM polling |
| `aws_s3_bucket.sdr_output` | Unchanged: aggregation reads .cadu, writes SDR/RDR/GEO outputs |
| `var.kms_key_arn` | Unchanged: used for S3 SSE-KMS encryption |
| `var.sns_topic_arn` | Unchanged: failure notifications |
| Per-chunk CodeBuild project | Unchanged: continues processing IQ extraction + SatDump |

### New Resources

| Resource | Purpose |
|---|---|
| EC2 Instance (r6i.xlarge) | Runs CSPP SDR with persistent LUT database |
| EBS Volume (100 GB gp3) | Stores CSPP SDR, RT-STPS, SDR_4_1_DB |
| IAM Instance Profile + Role | EC2 access to S3, KMS, SSM |
| Lambda Function (Trigger) | Starts EC2, issues SSM Run Command |
| IAM Role (Trigger Lambda) | ec2:StartInstances, ssm:SendCommand |
| CloudWatch Log Group | Trigger Lambda logs |

## Components and Interfaces

### 1. Aggregation EC2 Instance

**Instance Type**: r6i.xlarge (4 vCPU, 32 GB RAM) — CSPP SDR requires 16+ GB RAM for VIIRS processing.

**AMI**: Amazon Linux 2023 (latest, region eu-central-1)

**EBS**: 100 GB gp3 root volume with:
- `/opt/rt-stps/` — RT-STPS 7.0 installation
- `/opt/SDR_4_1/` — CSPP SDR 4.1.1 + initialized LUT database (SDR_4_1_DB)
- `/opt/scripts/` — Aggregation script and utilities
- SSM Agent pre-installed (included in Amazon Linux 2023)

**Lifecycle**: Launched in stopped state. Started by Trigger Lambda. Stops itself after processing via `shutdown -h now`.

**Network**: Default VPC, public subnet (needs internet for S3 access via public endpoint or VPC endpoint). No inbound security group rules needed (SSM uses outbound HTTPS).

### 2. Trigger Lambda

**Runtime**: Python 3.12  
**Memory**: 256 MB  
**Timeout**: 120 seconds (covers EC2 start time + SSM send)

**Interface**:
```json
{
  "Input": {
    "contact_id": "string",
    "contact_date": "YYYY-MM-DD",
    "bucket": "string"
  },
  "Output": {
    "command_id": "string",
    "instance_id": "string"
  }
}
```

**Logic**:
1. Start the Aggregation Instance (`ec2:StartInstances`)
2. Wait for instance to reach `running` state (poll `ec2:DescribeInstances`, max 60s)
3. Send SSM Run Command with aggregation script parameters
4. Return `command_id` and `instance_id` to Step Functions

```python
import boto3
import time
import os

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")

INSTANCE_ID = os.environ["AGGREGATION_INSTANCE_ID"]
SCRIPT_PATH = "/opt/scripts/aggregation.sh"
SSM_TIMEOUT = 1800  # 30 minutes

def handler(event, context):
    contact_id = event["contact_id"]
    contact_date = event["contact_date"]
    bucket = event["bucket"]

    # Start instance
    ec2.start_instances(InstanceIds=[INSTANCE_ID])

    # Wait for running state (max 60s)
    for _ in range(12):
        resp = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
        state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        if state == "running":
            break
        time.sleep(5)
    else:
        raise RuntimeError(f"Instance {INSTANCE_ID} did not reach running state")

    # Send SSM command
    response = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                f"{SCRIPT_PATH} {bucket} {contact_id} {contact_date}"
            ],
            "executionTimeout": [str(SSM_TIMEOUT)],
        },
        TimeoutSeconds=SSM_TIMEOUT,
        Comment=f"Aggregation for contact {contact_id}",
    )

    return {
        "command_id": response["Command"]["CommandId"],
        "instance_id": INSTANCE_ID,
    }
```

### 3. Aggregation Script

**Location**: `/opt/scripts/aggregation.sh` on the EC2 instance  
**Execution**: Via SSM Run Command  
**Arguments**: `$1=bucket`, `$2=contact_id`, `$3=contact_date`

```bash
#!/bin/bash
set -euo pipefail

BUCKET="$1"
CONTACT_ID="$2"
CONTACT_DATE="$3"

LOG_FILE="/var/log/aggregation.log"
KMS_KEY_ID="70451aac-a58c-4a93-be24-4587cd55a795"
WORK_DIR="/tmp/aggregation"
RTSTPS_HOME="/opt/rt-stps"
CSPP_HOME="/opt/SDR_4_1"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$1] $2" | tee -a "$LOG_FILE"; }

# Ensure self-stop on exit (success or failure)
trap 'log "INFO" "Stopping instance..."; shutdown -h now' EXIT

# Setup
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"/{cadu,rdr,sdr}
log "INFO" "Starting aggregation for contact=$CONTACT_ID date=$CONTACT_DATE"

# Step 1: Download .cadu files
log "INFO" "Downloading .cadu files from s3://$BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/"
aws s3 sync "s3://$BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/" "$WORK_DIR/cadu/" \
    --exclude '*' --include '*.cadu'
CADU_COUNT=$(find "$WORK_DIR/cadu" -name '*.cadu' | wc -l)
log "INFO" "Downloaded $CADU_COUNT .cadu files"

# Step 2: Concatenate in sorted order
log "INFO" "Concatenating .cadu files..."
find "$WORK_DIR/cadu" -name '*.cadu' | sort | xargs cat > "$WORK_DIR/combined.cadu"
log "INFO" "Combined CADU size: $(du -h "$WORK_DIR/combined.cadu" | cut -f1)"

# Step 3: Configure and run RT-STPS
log "INFO" "Configuring RT-STPS (PnEncoded=false, removing pn link)..."
cp "$RTSTPS_HOME/config/jpss1.xml" "$WORK_DIR/jpss1.xml"
sed -i 's/PnEncoded="true"/PnEncoded="false"/' "$WORK_DIR/jpss1.xml"
sed -i '/from="pn" to="reed_solomon"/d' "$WORK_DIR/jpss1.xml"
sed -i 's|from="frame_sync" to="pn"|from="frame_sync" to="reed_solomon"|' "$WORK_DIR/jpss1.xml"

log "INFO" "Running RT-STPS..."
mkdir -p /opt/data
cd "$RTSTPS_HOME" && bin/batch.sh "$WORK_DIR/jpss1.xml" "$WORK_DIR/combined.cadu"
RDR_COUNT=$(find /opt/data -name '*.h5' | wc -l)
log "INFO" "RT-STPS produced $RDR_COUNT RDR files"

# Step 4: CSPP SDR (only if VIIRS RDR exists)
VIIRS_RDR=$(find /opt/data -name 'RNSCA-RVIRS*.h5' | head -1)
if [ -n "$VIIRS_RDR" ]; then
    log "INFO" "Running CSPP SDR on $VIIRS_RDR..."
    export CSPP_SDR_HOME="$CSPP_HOME" CSPP_RT_HOME="$CSPP_HOME"
    "$CSPP_HOME/bin/viirs_sdr.sh" --work-dir "$WORK_DIR/sdr" "$VIIRS_RDR"
    SDR_COUNT=$(find "$WORK_DIR/sdr" -name 'SV*.h5' -o -name 'G*.h5' | wc -l)
    log "INFO" "CSPP SDR produced $SDR_COUNT SDR/GEO files"
else
    log "WARN" "No VIIRS RDR file produced by RT-STPS — skipping CSPP SDR"
fi

# Step 5: Upload results to S3
log "INFO" "Uploading SDR/GEO files to S3..."
if [ -d "$WORK_DIR/sdr" ] && [ "$(find "$WORK_DIR/sdr" -name '*.h5' | head -1)" ]; then
    aws s3 sync "$WORK_DIR/sdr/" "s3://$BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/sdr/" \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
    log "INFO" "SDR/GEO upload complete"
fi

log "INFO" "Uploading RDR files to S3..."
if [ "$(find /opt/data -name '*.h5' | head -1)" ]; then
    aws s3 sync /opt/data/ "s3://$BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/rdr/" \
        --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID" --exclude 'defs/*'
    log "INFO" "RDR upload complete"
fi

log "INFO" "Aggregation complete for contact=$CONTACT_ID"
# EXIT trap will stop the instance
```

### 4. Modified Step Functions State Machine

The `FinalAggregation` → `WaitForAggregation` → `CheckAggregationStatus` → `EvaluateAggregationStatus` CodeBuild states are replaced with:

```
FinalAggregation (Lambda invoke) → WaitForSSM (Wait 30s) → CheckSSMStatus (Lambda/SDK) → EvaluateSSMStatus (Choice)
```

**New states replacing the CodeBuild aggregation**:

```json
{
  "FinalAggregation": {
    "Type": "Task",
    "Resource": "arn:aws:states:::lambda:invoke",
    "Parameters": {
      "FunctionName": "${trigger_lambda_arn}",
      "Payload": {
        "contact_id.$": "$.contact_id",
        "contact_date.$": "$.contact_date",
        "bucket": "${output_bucket_id}"
      }
    },
    "ResultSelector": {
      "command_id.$": "$.Payload.command_id",
      "instance_id.$": "$.Payload.instance_id"
    },
    "ResultPath": "$.ssm",
    "Catch": [{
      "ErrorEquals": ["States.ALL"],
      "Next": "AggregationFailure",
      "ResultPath": "$.error"
    }],
    "Next": "WaitForSSM"
  },

  "WaitForSSM": {
    "Type": "Wait",
    "Seconds": 30,
    "Next": "CheckSSMStatus"
  },

  "CheckSSMStatus": {
    "Type": "Task",
    "Resource": "arn:aws:states:::aws-sdk:ssm:getCommandInvocation",
    "Parameters": {
      "CommandId.$": "$.ssm.command_id",
      "InstanceId.$": "$.ssm.instance_id"
    },
    "ResultSelector": {
      "status.$": "$.Status",
      "status_details.$": "$.StatusDetails"
    },
    "ResultPath": "$.ssm_poll",
    "Retry": [{
      "ErrorEquals": ["States.TaskFailed"],
      "IntervalSeconds": 10,
      "MaxAttempts": 3,
      "BackoffRate": 1.5
    }],
    "Next": "EvaluateSSMStatus"
  },

  "EvaluateSSMStatus": {
    "Type": "Choice",
    "Choices": [
      {
        "Variable": "$.ssm_poll.status",
        "StringEquals": "InProgress",
        "Next": "WaitForSSM"
      },
      {
        "Variable": "$.ssm_poll.status",
        "StringEquals": "Pending",
        "Next": "WaitForSSM"
      },
      {
        "Variable": "$.ssm_poll.status",
        "StringEquals": "Success",
        "Next": "PipelineSucceeded"
      }
    ],
    "Default": "AggregationFailure"
  }
}
```

### 5. IAM Roles

#### EC2 Instance Role (`groundstation-noaa20-aggregation-ec2`)

```hcl
{
  "Statement": [
    {
      "Sid": "S3ReadWriteOutputBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::groundstation-noaa20-sdr-output-471112743408",
        "arn:aws:s3:::groundstation-noaa20-sdr-output-471112743408/*"
      ]
    },
    {
      "Sid": "KMSAccess",
      "Effect": "Allow",
      "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": "arn:aws:kms:eu-central-1:471112743408:key/70451aac-a58c-4a93-be24-4587cd55a795"
    }
  ]
}
```

Plus `AmazonSSMManagedInstanceCore` managed policy attachment.

#### Trigger Lambda Role (`groundstation-noaa20-aggregation-trigger`)

```hcl
{
  "Statement": [
    {
      "Sid": "EC2StartDescribe",
      "Effect": "Allow",
      "Action": ["ec2:StartInstances", "ec2:DescribeInstances"],
      "Resource": "arn:aws:ec2:eu-central-1:471112743408:instance/<instance-id>"
    },
    {
      "Sid": "EC2DescribeGlobal",
      "Effect": "Allow",
      "Action": ["ec2:DescribeInstances"],
      "Resource": "*"
    },
    {
      "Sid": "SSMSendCommand",
      "Effect": "Allow",
      "Action": ["ssm:SendCommand", "ssm:GetCommandInvocation"],
      "Resource": [
        "arn:aws:ec2:eu-central-1:471112743408:instance/<instance-id>",
        "arn:aws:ssm:eu-central-1::document/AWS-RunShellScript"
      ]
    }
  ]
}
```

#### Step Functions Role Update

Add to existing `aws_iam_role_policy.sfn`:

```hcl
{
  "Sid": "InvokeTriggerLambda",
  "Effect": "Allow",
  "Action": ["lambda:InvokeFunction"],
  "Resource": "<trigger_lambda_arn>"
},
{
  "Sid": "SSMGetCommandInvocation",
  "Effect": "Allow",
  "Action": ["ssm:GetCommandInvocation"],
  "Resource": "*"
}
```

## Data Models

### S3 Object Layout (Aggregation Outputs)

```
groundstation-noaa20-sdr-output-471112743408/
  contacts/
    {contact_date}/
      {contact_id}/
        satdump/                          # Input: per-chunk .cadu files
          chunk_000/
            *.cadu
          chunk_001/
            *.cadu
          ...
        rdr/                              # Output: RT-STPS RDR HDF5
          RNSCA-RVIRS_*.h5                # VIIRS RDR
          RNSCA-RATMS_*.h5                # ATMS RDR (if present)
          RNSCA-RCRIS_*.h5                # CrIS RDR (if present)
        sdr/                              # Output: CSPP SDR + GEO HDF5
          SVI01_j01_d{date}_t{time}_*.h5  # I-band SDR
          SVM01_j01_d{date}_t{time}_*.h5  # M-band SDR
          SVDNB_j01_d{date}_t{time}_*.h5  # DNB SDR
          GIGTO_j01_d{date}_t{time}_*.h5  # I-band GEO
          GMODO_j01_d{date}_t{time}_*.h5  # M-band GEO
          GDNBO_j01_d{date}_t{time}_*.h5  # DNB GEO
```

### Trigger Lambda Event Schema

```json
{
  "contact_id": "abc123-def456",
  "contact_date": "2026-01-15",
  "bucket": "groundstation-noaa20-sdr-output-471112743408"
}
```

### SSM Command Output Schema

The Aggregation Script writes structured logs to `/var/log/aggregation.log`:

```
2026-01-15T14:30:00Z [INFO] Starting aggregation for contact=abc123 date=2026-01-15
2026-01-15T14:30:05Z [INFO] Downloaded 19 .cadu files
2026-01-15T14:30:10Z [INFO] Combined CADU size: 2.1G
2026-01-15T14:32:15Z [INFO] RT-STPS produced 3 RDR files
2026-01-15T14:37:20Z [INFO] CSPP SDR produced 42 SDR/GEO files
2026-01-15T14:38:00Z [INFO] SDR/GEO upload complete
2026-01-15T14:38:30Z [INFO] RDR upload complete
2026-01-15T14:38:30Z [INFO] Aggregation complete for contact=abc123
2026-01-15T14:38:31Z [INFO] Stopping instance...
```

### Instance Configuration

| Parameter | Value |
|---|---|
| Instance Type | r6i.xlarge |
| vCPU | 4 |
| RAM | 32 GB |
| EBS | 100 GB gp3 (3000 IOPS, 125 MB/s) |
| AMI | Amazon Linux 2023 |
| Region | eu-central-1 |
| Default State | Stopped |
| Max Execution Time | 30 minutes |

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

This feature is not suitable for property-based testing. The implementation consists of Terraform IaC (declarative EC2, IAM, and Lambda resource definitions), shell scripting (a linear aggregation pipeline), and AWS service integration (SSM Run Command, S3 sync, Step Functions polling). There are no pure functions with a meaningful input space suitable for PBT.

Correctness is verified through integration tests and operational invariants:

### Property 1: Instance self-stop after execution

*For any* aggregation execution (success or failure), the EC2 instance SHALL transition to `stopped` state within 5 minutes of script exit, guaranteed by the bash EXIT trap invoking `shutdown -h now`.

**Validates: Requirements 5.1, 5.2**

### Property 2: KMS encryption on all S3 uploads

*For any* object written to the `sdr/` or `rdr/` S3 prefixes during aggregation, the object SHALL be encrypted with SSE-KMS using the designated KMS key ARN.

**Validates: Requirements 4.6, 4.7**

### Property 3: SSM polling loop termination

*For any* Step Functions execution that reaches the SSM polling loop, the loop SHALL terminate in finite time — either via `Success` leading to `PipelineSucceeded`, or via `Failed`/`TimedOut`/default leading to `AggregationFailure`. No infinite loop is possible given the 30-minute SSM command timeout.

**Validates: Requirements 3.5, 3.6, 5.3**

## Error Handling

### Trigger Lambda Failures

| Failure | Handling |
|---|---|
| EC2 instance fails to start | Lambda raises `RuntimeError` after 60s timeout → Step Functions catches, transitions to `AggregationFailure` |
| EC2 instance in `terminated` state | Lambda raises error immediately → `AggregationFailure` |
| SSM SendCommand fails | Lambda raises error → `AggregationFailure` |
| Lambda timeout (120s) | Step Functions catches `States.Timeout` → `AggregationFailure` |

### Aggregation Script Failures

| Failure | Handling |
|---|---|
| S3 download fails | `set -e` causes immediate exit → SSM reports `Failed` → `AggregationFailure` |
| RT-STPS crashes | `set -e` causes immediate exit → instance self-stops via trap |
| CSPP SDR crashes | `set -e` causes immediate exit → instance self-stops via trap |
| No VIIRS RDR produced | Script logs warning, skips CSPP, uploads RDR only → SSM reports `Success` |
| S3 upload fails | `set -e` causes immediate exit → instance self-stops via trap |
| 30-minute timeout | SSM kills the process → reports `TimedOut` → instance self-stops via trap |

### Instance Self-Stop Guarantee

The aggregation script uses an `EXIT` trap to ensure `shutdown -h now` runs regardless of how the script exits:
- Normal completion → trap fires → shutdown
- `set -e` failure → trap fires → shutdown
- SSM timeout kill → OS eventually shuts down (SSM timeout sends SIGTERM, then SIGKILL)

As a safety net, a CloudWatch alarm can be configured to stop the instance if it has been running for more than 35 minutes (covers the 30-minute script timeout + buffer).

### SNS Failure Notification

The existing `AggregationFailure` state in Step Functions publishes to SNS with the `contact_id` and error details, unchanged from the current implementation.

## Testing Strategy

### Why Property-Based Testing Does Not Apply

This feature is primarily Infrastructure as Code (Terraform) and shell scripting with AWS service integration:
- EC2 provisioning is declarative Terraform configuration
- IAM policies are static JSON documents
- The aggregation script is a linear shell pipeline
- Step Functions states are JSON configuration
- SSM integration is AWS service wiring

There are no pure functions with varied input spaces suitable for property-based testing. The correct testing approaches are:

### Unit Tests (Example-Based)

1. **Trigger Lambda logic** — Test with moto:
   - Instance starts successfully and SSM command is sent
   - Instance fails to reach running state within timeout
   - SSM SendCommand returns command_id correctly
   - Error handling for terminated instance state

2. **Step Functions state machine definition** — Validate JSON structure:
   - `FinalAggregation` state invokes correct Lambda ARN
   - `EvaluateSSMStatus` choice routes correctly for each status value
   - Polling loop has correct Wait duration

### Integration Tests

1. **Aggregation script** — Run against test .cadu files on a real EC2 instance:
   - Downloads .cadu files from test S3 prefix
   - Concatenation produces correct combined file size
   - RT-STPS produces expected RDR output
   - CSPP SDR produces SDR/GEO files
   - S3 upload uses KMS encryption
   - Instance stops after completion

2. **End-to-end pipeline** — Start a full pipeline execution:
   - Step Functions invokes Trigger Lambda
   - EC2 instance starts and SSM command executes
   - Polling loop correctly detects completion
   - Pipeline reaches `PipelineSucceeded` state

### Terraform Plan Tests

1. **IAM policy validation** — Verify least-privilege:
   - EC2 role has only S3, KMS, and SSM permissions
   - Trigger Lambda role has only ec2:Start, ec2:Describe, ssm:Send, ssm:Get
   - Step Functions role has lambda:InvokeFunction on trigger Lambda

2. **Resource configuration** — Verify instance spec:
   - Instance type is r6i.xlarge
   - EBS volume is 100 GB gp3
   - Instance has SSM managed policy attached
   - Instance is in eu-central-1

3. **Security** — Checkov validation:
   - No public IP unless required for S3 access (use VPC endpoint if possible)
   - EBS encryption enabled
   - Security group has no unnecessary inbound rules

### Smoke Tests

1. **SSM connectivity** — Verify the instance appears in SSM managed instances list after start
2. **CSPP SDR initialization** — Verify `/opt/SDR_4_1/anc/static/` exists and contains LUT files
3. **RT-STPS installation** — Verify `/opt/rt-stps/bin/batch.sh` is executable
