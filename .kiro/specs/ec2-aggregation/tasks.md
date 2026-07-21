# Implementation Plan: EC2-Based Aggregation for NOAA-20 SDR Pipeline

## Overview

Replace the CodeBuild-based final aggregation step in `modules/sdr_pipeline/` with an EC2 instance (r6i.xlarge) triggered via SSM Run Command. The implementation adds Terraform resources for the EC2 instance, IAM roles, and Trigger Lambda, modifies the Step Functions state machine to use a Lambda + SSM polling loop, and creates the aggregation shell script for the instance.

## Tasks

- [x] 1. Provision EC2 Aggregation Instance and IAM
  - [x] 1.1 Create Terraform EC2 instance resource and instance profile
    - Add `ec2.tf` in `modules/sdr_pipeline/` with:
      - `aws_instance` resource (r6i.xlarge, Amazon Linux 2023 AMI, 100 GB gp3 EBS, launched in stopped state)
      - `aws_iam_instance_profile` and `aws_iam_role` for the EC2 instance
      - IAM policy for S3 read/write on SDR_Output_Bucket, KMS Encrypt/Decrypt/GenerateDataKey
      - `AmazonSSMManagedInstanceCore` managed policy attachment
      - Security group with no inbound rules (SSM uses outbound HTTPS only)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3_

  - [x] 1.2 Create Trigger Lambda IAM role and permissions
    - Add IAM role for the Trigger Lambda with:
      - `ec2:StartInstances` and `ec2:DescribeInstances` scoped to the aggregation instance
      - `ssm:SendCommand` and `ssm:GetCommandInvocation` scoped to the instance and `AWS-RunShellScript` document
      - Basic Lambda execution role (CloudWatch Logs)
    - _Requirements: 2.4, 2.5_

  - [x] 1.3 Update Step Functions IAM role
    - Add `lambda:InvokeFunction` permission on the Trigger Lambda ARN
    - Add `ssm:GetCommandInvocation` permission (for the `CheckSSMStatus` SDK integration state)
    - _Requirements: 2.6_

- [x] 2. Implement Trigger Lambda
  - [x] 2.1 Create Trigger Lambda function code
    - Create `lambdas/aggregation_trigger/handler.py` with:
      - Start EC2 instance via `ec2:StartInstances`
      - Poll `ec2:DescribeInstances` until instance reaches `running` state (max 60s, 5s intervals)
      - Issue `ssm:SendCommand` with `AWS-RunShellScript` document, passing aggregation script path and parameters
      - Return `command_id` and `instance_id`
      - Error handling: raise `RuntimeError` if instance doesn't reach running state
    - Environment variable: `AGGREGATION_INSTANCE_ID`
    - Runtime: Python 3.12, 256 MB memory, 120s timeout
    - _Requirements: 3.2, 3.3_

  - [x] 2.2 Create Terraform Lambda resource
    - Add `lambda.tf` in `modules/sdr_pipeline/` (or extend existing file) with:
      - `aws_lambda_function` resource for the Trigger Lambda
      - `data "archive_file"` for packaging the handler
      - CloudWatch Log Group with appropriate retention
      - Environment variable referencing the EC2 instance ID
    - _Requirements: 3.2, 3.3, 7.2_

  - [ ]* 2.3 Write unit tests for Trigger Lambda
    - Test with moto:
      - Instance starts successfully and SSM command is sent
      - Instance fails to reach running state within timeout (raises RuntimeError)
      - SSM SendCommand returns command_id correctly
    - _Requirements: 3.2, 3.3_

- [x] 3. Checkpoint — Validate Terraform and IAM
  - Ensure all tests pass, ask the user if questions arise.
  - Run `terraform validate` and `terraform plan` to verify resource definitions
  - Verify IAM policies follow least-privilege (no `*` actions on sensitive services)

- [x] 4. Create Aggregation Script
  - [x] 4.1 Write the aggregation shell script
    - Create `scripts/aggregation.sh` (to be deployed to `/opt/scripts/aggregation.sh` on the instance):
      - Accept arguments: `$1=bucket`, `$2=contact_id`, `$3=contact_date`
      - `set -euo pipefail` for strict error handling
      - EXIT trap to run `shutdown -h now` on any exit (success or failure)
      - Step 1: Download `.cadu` files from `s3://{bucket}/contacts/{date}/{id}/satdump/`
      - Step 2: Concatenate in sorted order → `combined.cadu`
      - Step 3: Configure RT-STPS (PnEncoded=false, remove pn link node), execute `batch.sh`
      - Step 4: If VIIRS RDR exists, run `viirs_sdr.sh`; if not, log warning and skip
      - Step 5: Upload SDR/GEO HDF5 to `sdr/` prefix and RDR HDF5 to `rdr/` prefix with KMS encryption
      - Structured logging to `/var/log/aggregation.log`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 5.1, 5.2, 7.1_

  - [x] 4.2 Add instance timeout safety net
    - In the aggregation script or as a systemd timer:
      - 30-minute maximum execution timeout (SSM `executionTimeout` parameter)
      - Instance self-stops regardless of script status after timeout
    - _Requirements: 5.3_

- [x] 5. Modify Step Functions State Machine
  - [x] 5.1 Replace CodeBuild aggregation states with Lambda + SSM polling
    - Modify `modules/sdr_pipeline/step_functions.tf`:
      - Replace `FinalAggregation` CodeBuild state with Lambda invoke targeting Trigger Lambda
      - Add `WaitForSSM` state (Wait 30 seconds)
      - Add `CheckSSMStatus` state (SDK integration: `ssm:getCommandInvocation`)
      - Add `EvaluateSSMStatus` Choice state:
        - `InProgress` / `Pending` → loop back to `WaitForSSM`
        - `Success` → `PipelineSucceeded`
        - Default (Failed/TimedOut) → `AggregationFailure`
      - Catch block on `FinalAggregation` for `States.ALL` → `AggregationFailure`
      - Retry on `CheckSSMStatus` for `States.TaskFailed` (3 attempts, 10s interval, 1.5x backoff)
    - Pass `contact_id`, `contact_date`, `bucket` as payload to the Trigger Lambda
    - _Requirements: 3.1, 3.4, 3.5, 3.6_

  - [x] 5.2 Verify per-chunk processing states are unchanged
    - Confirm the ParallelProcessing Map state and per-chunk CodeBuild states remain intact
    - Ensure same input parameters (contact_id, bucket, chunks, contact_date) flow to per-chunk Map
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 6. Checkpoint — Full Terraform Plan Validation
  - Ensure all tests pass, ask the user if questions arise.
  - Run `terraform validate` and `terraform plan`
  - Verify the state machine JSON structure is valid
  - Confirm no unintended changes to per-chunk CodeBuild resources

- [x] 7. Observability and Failure Handling
  - [x] 7.1 Add SNS failure notification to AggregationFailure state
    - Ensure the existing `AggregationFailure` state publishes to SNS with `contact_id` and error details
    - Verify SNS topic ARN is referenced correctly in the state machine definition
    - _Requirements: 7.3_

  - [x] 7.2 Configure CloudWatch logging for Trigger Lambda
    - Ensure CloudWatch Log Group exists with appropriate retention
    - Trigger Lambda retrieves and logs SSM command output on completion
    - _Requirements: 7.2_

- [x] 8. Terraform Variables and Outputs
  - [x] 8.1 Add module variables and outputs for new resources
    - Add variables for: AMI ID (or data source for latest Amazon Linux 2023), subnet ID, KMS key ARN, SNS topic ARN
    - Add outputs for: EC2 instance ID, Trigger Lambda ARN, Trigger Lambda function name
    - Wire outputs from `modules/sdr_pipeline/` to root module if needed
    - _Requirements: 1.1, 1.5_

- [x] 9. Final Checkpoint — End-to-End Validation
  - Ensure all tests pass, ask the user if questions arise.
  - Run `terraform validate` and `terraform plan` for the complete configuration
  - Verify IAM least-privilege across all new roles
  - Confirm instance launches in stopped state
  - Confirm aggregation script self-stops on both success and failure paths

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The EC2 instance requires one-time manual setup after provisioning (CSPP SDR installation, LUT database initialization) — this is outside the scope of automated tasks
- Per-chunk CodeBuild processing (IQ extraction + SatDump) is explicitly unchanged
- The design states property-based testing is not applicable — correctness is validated through Terraform plan tests and integration tests
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1", "4.1"] },
    { "id": 2, "tasks": ["2.2", "4.2"] },
    { "id": 3, "tasks": ["2.3", "5.1"] },
    { "id": 4, "tasks": ["5.2", "7.1", "7.2"] },
    { "id": 5, "tasks": ["8.1"] }
  ]
}
```
