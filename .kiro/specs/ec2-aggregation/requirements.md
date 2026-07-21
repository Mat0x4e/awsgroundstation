# Requirements Document

## Introduction

Replace the CodeBuild-based aggregation step in the NOAA-20 SDR pipeline with an EC2-based approach. CSPP SDR requires a persistent filesystem with an initialized LUT database (SDR_4_1_DB) that cannot be created in an ephemeral Docker container. An EC2 instance with persistent EBS storage will have CSPP SDR properly installed and initialized (one-time manual setup). Step Functions will trigger the EC2 instance via SSM Run Command, the instance runs the full aggregation chain (CADU concatenation → RT-STPS → CSPP SDR), uploads results to S3, then stops to minimize cost.

## Glossary

- **Aggregation_Instance**: The EC2 instance (r6i.xlarge, 32 GB RAM) in eu-central-1 that runs CSPP SDR processing
- **Pipeline_State_Machine**: The existing Step Functions state machine `groundstation-noaa20-sdr-pipeline` that orchestrates per-chunk and aggregation processing
- **Trigger_Lambda**: A Lambda function that starts the Aggregation_Instance and issues an SSM Run Command to execute the aggregation script
- **Aggregation_Script**: A shell script on the Aggregation_Instance that performs CADU concatenation, RT-STPS, CSPP SDR, and S3 upload
- **SDR_Output_Bucket**: The S3 bucket `groundstation-noaa20-sdr-output-471112743408` used for pipeline input/output
- **KMS_Key**: The KMS key `70451aac-a58c-4a93-be24-4587cd55a795` used for S3 server-side encryption
- **RT-STPS**: Real-Time Software Telemetry Processing System — converts raw CADU frames into RDR HDF5 instrument packets
- **CSPP_SDR**: Community Satellite Processing Package — calibrates RDR into SDR + GEO HDF5 with per-pixel geolocation
- **CADU**: Channel Access Data Unit — 1024-byte telemetry frames output by SatDump
- **SSM_Run_Command**: AWS Systems Manager Run Command — executes commands on managed EC2 instances without SSH

## Requirements

### Requirement 1: EC2 Instance Provisioning

**User Story:** As a pipeline operator, I want an EC2 instance with sufficient resources and persistent storage, so that CSPP SDR can run with its initialized LUT database.

#### Acceptance Criteria

1. THE Aggregation_Instance SHALL be an r6i.xlarge (4 vCPU, 32 GB RAM) in eu-central-1
2. THE Aggregation_Instance SHALL have an EBS gp3 root volume of at least 100 GB with CSPP SDR, RT-STPS, and the SDR_4_1_DB pre-installed
3. THE Aggregation_Instance SHALL be launched in a stopped state and remain stopped when not processing
4. THE Aggregation_Instance SHALL have the SSM Agent installed and running to accept Run Commands
5. THE Aggregation_Instance SHALL use Amazon Linux 2023 as the operating system

### Requirement 2: IAM Permissions

**User Story:** As a security engineer, I want the EC2 instance to have least-privilege IAM permissions, so that it can only access the resources required for aggregation.

#### Acceptance Criteria

1. THE Aggregation_Instance SHALL have an instance profile with an IAM role granting S3 read and write access to the SDR_Output_Bucket
2. THE Aggregation_Instance SHALL have an IAM role granting KMS Encrypt and Decrypt permissions on the KMS_Key
3. THE Aggregation_Instance SHALL have an IAM role granting SSM managed instance core permissions (AmazonSSMManagedInstanceCore)
4. THE Trigger_Lambda SHALL have an IAM role granting ec2:StartInstances and ec2:DescribeInstances on the Aggregation_Instance
5. THE Trigger_Lambda SHALL have an IAM role granting ssm:SendCommand and ssm:GetCommandInvocation permissions
6. THE Pipeline_State_Machine SHALL have an IAM role granting lambda:InvokeFunction on the Trigger_Lambda

### Requirement 3: Step Functions Integration

**User Story:** As a pipeline operator, I want Step Functions to trigger the EC2 aggregation instead of CodeBuild, so that CSPP SDR runs on persistent storage with its initialized database.

#### Acceptance Criteria

1. WHEN the ParallelProcessing Map state completes, THE Pipeline_State_Machine SHALL invoke the Trigger_Lambda instead of starting a CodeBuild aggregation build
2. THE Trigger_Lambda SHALL start the Aggregation_Instance and wait until the instance reaches the running state
3. WHEN the Aggregation_Instance is running, THE Trigger_Lambda SHALL issue an SSM Run Command executing the Aggregation_Script
4. THE Pipeline_State_Machine SHALL poll for SSM command completion using a Wait-CheckStatus loop with 30-second intervals
5. WHEN the SSM command status is "Success" and the command invocation has completed, THE Pipeline_State_Machine SHALL transition to PipelineSucceeded
6. IF the SSM command status is "Failed" or "TimedOut", THEN THE Pipeline_State_Machine SHALL transition to AggregationFailure

### Requirement 4: Aggregation Script Execution

**User Story:** As a pipeline operator, I want the aggregation script to download CADU files, run RT-STPS and CSPP SDR, and upload results, so that SDR and GEO HDF5 products are available in S3.

#### Acceptance Criteria

1. WHEN the Aggregation_Script executes, THE Aggregation_Instance SHALL download all .cadu files from s3://SDR_Output_Bucket/contacts/{contact_date}/{contact_id}/satdump/
2. THE Aggregation_Script SHALL concatenate all downloaded .cadu files in sorted order into a single combined.cadu file
3. THE Aggregation_Script SHALL configure RT-STPS with PnEncoded="false" and the pn link node removed from jpss1.xml before execution
4. WHEN combined.cadu is ready, THE Aggregation_Script SHALL execute RT-STPS with jpss1.xml configuration from the /opt/rt-stps directory, outputting RDR HDF5 to /opt/data
5. WHEN RT-STPS produces a VIIRS RDR file, THE Aggregation_Script SHALL execute CSPP SDR viirs_sdr.sh with the VIIRS RDR as input, and IF CSPP SDR fails to execute or crashes, THEN THE Aggregation_Script SHALL fail the entire aggregation process
6. THE Aggregation_Script SHALL upload all SDR and GEO HDF5 files to s3://SDR_Output_Bucket/contacts/{contact_date}/{contact_id}/sdr/ with KMS encryption
7. THE Aggregation_Script SHALL upload all RDR HDF5 files to s3://SDR_Output_Bucket/contacts/{contact_date}/{contact_id}/rdr/ with KMS encryption
8. IF RT-STPS produces no VIIRS RDR file, THEN THE Aggregation_Script SHALL log a warning and skip the CSPP SDR step

### Requirement 5: Instance Lifecycle Management

**User Story:** As a cost-conscious operator, I want the EC2 instance to stop automatically after processing, so that costs are minimized when the pipeline is idle.

#### Acceptance Criteria

1. WHEN the Aggregation_Script completes successfully, THE Aggregation_Instance SHALL stop itself using a shutdown command
2. WHEN the Aggregation_Script fails, THE Aggregation_Instance SHALL stop itself using a shutdown command
3. THE Aggregation_Instance SHALL have a maximum execution timeout of 30 minutes, after which it stops regardless of script status
4. WHILE the Aggregation_Instance is stopped, THE Aggregation_Instance SHALL incur only EBS storage costs

### Requirement 6: Per-Chunk Processing Unchanged

**User Story:** As a pipeline operator, I want the per-chunk CodeBuild processing to remain unchanged, so that IQ extraction and SatDump continue working as before.

#### Acceptance Criteria

1. THE Pipeline_State_Machine SHALL continue using CodeBuild for per-chunk processing (IQ extraction + SatDump)
2. THE Pipeline_State_Machine SHALL pass the same input parameters (contact_id, bucket, chunks, contact_date) to the per-chunk Map state as before
3. THE per-chunk CodeBuild builds SHALL continue uploading .cadu files and SatDump outputs to the same S3 paths as before

### Requirement 7: Observability

**User Story:** As a pipeline operator, I want visibility into the aggregation step, so that I can diagnose failures and monitor processing time.

#### Acceptance Criteria

1. THE Aggregation_Script SHALL write structured log output to /var/log/aggregation.log on the instance
2. WHEN the SSM Run Command completes successfully, THE Trigger_Lambda SHALL retrieve and log the command output to CloudWatch
3. IF the aggregation fails, THEN THE Pipeline_State_Machine SHALL publish a failure notification via SNS including the contact_id and error details
