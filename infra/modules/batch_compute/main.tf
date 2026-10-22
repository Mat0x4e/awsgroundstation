# main.tf - Production AWS Batch compute module for the SDR chunk fan-out (Stage 1).
#
# Promoted from infra/modules/compute_bakeoff (throwaway scaffold) - this is a rewrite,
# not a copy. Key differences from compute_bakeoff:
#   1. Production contacts/ prefix (never bakeoff/)
#   2. Spot retryStrategy with evaluateOnExit (compute_bakeoff has NONE - new requirement)
#   3. Idempotent retry: aws s3 rm --recursive clears the chunk prefix before s3 sync
#      so a Spot-interrupted partial upload leaves no stale objects (Req 1.8)
#   4. cloudwatch:PutMetricData conditioned on namespace=SDRPipeline (bakeoff has NONE)
#   5. No Disposable=true tag; production tagging uses caller-supplied tags only
#
# Wave 0 (tasks 1.1, 1.2): Spot CE + fan-out job queue + chunk job definition.
# On-demand CE, aggregation job definition, and render job definition are added in
# later waves (tasks 5 and 10) - the module structure is designed to accommodate them.
#
# NEVER run terraform apply directly - all applies go through the CI/CD pipeline
# after the operator runs: aws sso login && terraform plan

###############################################################################
# Data sources - region, default VPC and subnets
# Mirrors the pattern in sdr_pipeline/ec2.tf and compute_bakeoff/main.tf
###############################################################################

data "aws_region" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

###############################################################################
# Locals
###############################################################################

locals {
  fanout_log_group_name      = "/aws/batch/${var.project_name}-fanout"
  aggregation_log_group_name = "/aws/batch/${var.project_name}-aggregation"

  # ECR repository ARN - derived from the naming convention in the sdr_pipeline module.
  # That module always creates the repo as "${project_name}-sdr-pipeline".
  ecr_repo_arn = "arn:aws:ecr:${data.aws_region.current.id}:${var.account_id}:repository/${var.project_name}-sdr-pipeline"

  # Aggregation entrypoint bootstrap command.
  # Pulls aggregation_entrypoint.sh from S3 at job start, then execs it. This
  # mirrors the same no-image-rebuild deploy pattern used by aggregation.yml
  # (which pulls aggregation.sh from s3://OUTPUT/software/aggregation.sh).
  # The real env var values (OUTPUT_BUCKET, CONTACT_ID, etc.) are injected at
  # submit time via SFN ContainerOverrides.Environment (canary blocker #6 --
  # Ref:: substitution does NOT work in environment[], only in command[]).
  # Using $OUTPUT_BUCKET (no braces) -- Terraform only interpolates ${...}.
  aggregation_fetch_cmd = join("\n", [
    "set -euo pipefail",
    "aws s3 cp \"s3://$OUTPUT_BUCKET/software/aggregation_entrypoint.sh\" /tmp/aggregation_entrypoint.sh",
    "chmod +x /tmp/aggregation_entrypoint.sh",
    "exec /tmp/aggregation_entrypoint.sh",
  ])

  # Per-chunk entrypoint - mirrors the production CodeBuild buildspec injected by
  # step_functions.tf (the BuildspecOverride in StartCodeBuild), adapted for Batch.
  #
  # Three steps match the CodeBuild baseline exactly:
  #   download pcap -> iq_extract.py -> satdump_process.sh -> upload
  #
  # DIFFERENCE 1 - output prefix: contacts/ (production), never bakeoff/contacts/
  # DIFFERENCE 2 - idempotent retry (Req 1.8): aws s3 rm --recursive clears the
  #   destination chunk prefix BEFORE s3 sync so a Spot-retry after a partial upload
  #   cannot leave stale objects. The "sync is additive" gap is closed here, not assumed.
  # DIFFERENCE 3 - Spot retryStrategy: see the retry_strategy block on the job def.
  #
  # Uses plain $VAR (no ${...}) to avoid Terraform template interpolation conflicts.
  chunk_entrypoint = join("\n", [
    "set -euo pipefail",
    "echo '[Batch] Downloading chunk from S3...'",
    "aws s3 cp \"s3://$INPUT_BUCKET/$INPUT_KEY\" /tmp/input.pcap",
    "mkdir -p /tmp/output/iq /tmp/output/satdump",
    "echo '[Batch] Step 1 - IQ Extraction'",
    "python3 /opt/scripts/iq_extract.py /tmp/input.pcap /tmp/output/iq/baseband.cs8",
    "echo '[Batch] Step 2 - SatDump (npp_hrd pipeline)'",
    "/opt/scripts/satdump_process.sh /tmp/output/iq/baseband.cs8 /tmp/output/satdump",
    "find /tmp/output/ -name '*.cadu' -ls",
    "ls -la /tmp/output/satdump/",
    "echo '[Batch] Step 2b - Clearing destination chunk prefix before upload (idempotent retry, Req 1.8)'",
    "aws s3 rm --recursive \"s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/chunk_$CHUNK_ID/\" 2>/dev/null || true",
    "echo '[Batch] Step 2c - Uploading SatDump outputs to S3 (production contacts/ prefix)'",
    "aws s3 sync /tmp/output/satdump/ \"s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/chunk_$CHUNK_ID/\" --sse aws:kms --sse-kms-key-id \"$KMS_KEY_ID\"",
    "aws s3 cp /tmp/output/satdump/dataset.json \"s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/chunks/chunk_$CHUNK_ID/dataset.json\" --sse aws:kms --sse-kms-key-id \"$KMS_KEY_ID\" 2>/dev/null || true",
    "echo '[Batch] Chunk processing complete'",
  ])
}

###############################################################################
# CloudWatch Log Group - fan-out chunk jobs
###############################################################################

resource "aws_cloudwatch_log_group" "fanout" {
  # checkov:skip=CKV_AWS_338: 90-day retention is deliberate for job execution logs.
  # The permanent science record (CADU frames, composites) lives in S3 with versioning.
  # Batch job execution traces do not require the 365-day policy threshold - they are
  # operational logs, not the data product.
  name              = local.fanout_log_group_name
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name = local.fanout_log_group_name
  })
}

###############################################################################
# Security Group - egress HTTPS only, no ingress
###############################################################################
# Batch job containers pull from ECR, write to S3, and push logs to CloudWatch -
# all over port 443. No inbound connections are required.

resource "aws_security_group" "fanout" {
  name        = "${var.project_name}-fanout-sg"
  description = "Batch fan-out compute instances - no inbound; HTTPS (443) egress only for ECR, S3, and CloudWatch Logs"
  vpc_id      = data.aws_vpc.default.id

  # No ingress rules - Batch job containers do not accept inbound connections

  egress {
    description = "HTTPS outbound for ECR image pull, S3 data access, and CloudWatch Logs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Port 80 (HTTP) outbound: CSPP viirs_sdr.sh fetches dynamic ancillary data and LUT
  # files from jpssdb.ssec.wisc.edu (CIMSS/SSEC) over plain HTTP. That host does not
  # support HTTPS. The PERF-1 S3 LUT cache reduces but does not eliminate these fetches
  # (residual ancillary checks still hit port 80 at SDR processing time). The fan-out
  # Spot CE shares this SG but SatDump does not use port 80, so the rule is harmless for
  # chunk jobs. CodeBuild (the previous Stage 2 runtime) had unrestricted egress; this
  # rule restores parity for the aggregation on-demand CE only.
  # checkov:skip=CKV_AWS_382: CSPP viirs_sdr.sh performs dynamic ancillary and LUT
  # fetches from jpssdb.ssec.wisc.edu (CIMSS/SSEC) over HTTP port 80. This host is
  # a well-known scientific data server that does not support HTTPS. The PERF-1 S3
  # LUT cache reduces but does not eliminate these fetches (residual ancillary checks
  # still hit port 80 at SDR processing time). CodeBuild - the previous Stage 2
  # runtime - had unrestricted egress (80 + 443); this rule restores that parity for
  # the Batch aggregation on-demand CE that shares this security group. Port-specific
  # (80 only), outbound only; no ingress is opened. The fan-out Spot CE also uses this
  # SG but SatDump does not use port 80, so the rule is harmless for chunk jobs.
  egress {
    description = "HTTP outbound for CSPP dynamic ancillary and LUT fetches from jpssdb.ssec.wisc.edu (port 80 only, CIMSS/SSEC scientific data host)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-fanout-sg"
  })
}

###############################################################################
# EC2 Launch Template - encrypted root EBS volume (SCP compliance)
###############################################################################
# Context: The org SCP p-eb2chkmi explicitly DENIES ec2:RunInstances on
# arn:aws:ec2:REGION:ACCOUNT:volume/* when the root EBS volume is unencrypted.
# The Batch-generated default launch template has NO BlockDeviceMappings, so it
# inherits the ECS-optimised AL2023 AMI default (Encrypted: false, /dev/xvda,
# gp3, 30 GiB). Account-level EBS encryption-by-default is also false, so the
# SCP blocked every Batch instance launch (CloudTrail: Client.UnauthorizedOperation
# explicit deny in SCP p-eb2chkmi).
#
# This template overrides ONLY the block device mapping - image_id is intentionally
# absent. Batch merges its managed ECS-optimised AMI with this template's device
# mapping at launch time; Batch injects the AMI ID it manages.
#
# KMS key choice: aws/ebs (AWS-managed) rather than the pipeline CMK.
# The root volume holds only ephemeral /tmp scratch (pcap, cs8, CADU) deleted on
# termination. The permanent science product is written to S3 under the pipeline CMK.
# With EC2 Spot, EBS volumes are created by the EC2 Spot service-linked role (SLR),
# which runs in an AWS-owned account. Granting the EC2 Spot SLR access to our CMK
# requires a key-policy change and is fragile; the aws/ebs key is always accessible
# to the Spot SLR without any grant, eliminating the kms:InvalidState termination
# (Client.InvalidKMSKey.InvalidState observed on 5 live Spot instances). The SCP
# requires encryption (satisfied by aws/ebs) and the Owner tag (satisfied by
# tag_specifications) - it does NOT require a specific customer CMK. Using aws/ebs
# for a throwaway volume is the least-privilege, operationally correct choice.
#
# Volume size: 200 GiB.
# Root cause of CannotPullContainerError "no space left on device" (April 2026 canary):
# the SDR ECR image is ~20 GiB COMPRESSED (22,332,403,301 bytes per ecr describe-images).
# Docker decompresses all layers onto the root volume at container pull time; combined
# with the large CSPP ancillary tile tree (SDR_4_1/anc/static/ecotiles/...), the
# on-disk image footprint far exceeds the compressed size. The prior 60 GiB estimate
# only accounted for /tmp scratch and completely missed the decompressed image layers.
#
# Measured breakdown:
#   Decompressed SDR image (SatDump + RT-STPS + CSPP + SDR_4_1/anc tiles): >60 GiB
#   OS + ECS agent:  ~10 GiB
#   Per-chunk /tmp:   ~6 GiB (pcap 2.1 + cs8 2.0 + SatDump out 1-2)
#   Total:           ~76+ GiB measured minimum, with headroom needed for layer staging
#
# 200 GiB gp3 provides comfortable headroom above the measured footprint and avoids
# the layer-registration failure. Instances are ephemeral (minutes per chunk, deleted
# on termination), so the real cost impact is negligible despite the $0.08/GiB-month
# gp3 rate ($16/month per instance-month at 200 GiB, but never billed for a full month).

resource "aws_launch_template" "fanout" {
  name        = "${var.project_name}-fanout-lt"
  description = "Batch Spot CE launch template - aws/ebs encrypted root volume for SCP p-eb2chkmi compliance; IMDSv2 required"

  # Root volume override: encrypt with aws/ebs (AWS-managed key), gp3, 200 GiB.
  # No kms_key_id - encrypted=true without a kms_key_id uses the account aws/ebs key,
  # which the EC2 Spot SLR can always use without an explicit key-policy grant.
  # device_name /dev/xvda matches the ECS-optimised AL2023 AMI root device (confirmed).
  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      encrypted             = true
      volume_type           = "gp3"
      volume_size           = 200
      delete_on_termination = true
    }
  }

  # IMDSv2 - require session-oriented tokens; hop_limit=1 prevents container escapes
  # to the instance metadata endpoint (CKV_AWS_79).
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  # Tag instances and root volumes at launch time for cost attribution and audit trail.
  tag_specifications {
    resource_type = "instance"
    tags = merge(var.tags, {
      Name = "${var.project_name}-fanout-instance"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(var.tags, {
      Name = "${var.project_name}-fanout-volume"
    })
  }

  # Advance the LT's DEFAULT version to the newest version on every Terraform change.
  # Combined with version = aws_launch_template.fanout.latest_version on the CE,
  # this ensures the CE always launches with the current Terraform-managed content.
  update_default_version = true

  tags = merge(var.tags, {
    Name = "${var.project_name}-fanout-lt"
  })
}

###############################################################################
# Batch Compute Environment - MANAGED, EC2 SPOT (fan-out only)
###############################################################################

resource "aws_batch_compute_environment" "fanout_spot" {
  # compute_environment_name_prefix + create_before_destroy: standard pattern for
  # safe CE replacement. Batch refuses to delete a CE that a job queue still
  # references, so a static name causes "Cannot delete, found existing JobQueue
  # relationship" whenever any attribute forces CE replacement (e.g. launch_template).
  # The prefix form lets Terraform create a new CE (with a fresh unique suffix)
  # before touching the old one; the job queue is then updated to the new CE ARN
  # in the same apply, and only after that is the old CE deleted.
  compute_environment_name_prefix = "${var.project_name}-fanout-ce-"
  type                            = "MANAGED"
  state                           = "ENABLED"
  # Explicit service role - not the AWS-managed default, for auditability
  service_role = aws_iam_role.batch_service.arn

  compute_resources {
    type = "SPOT"
    # SPOT_CAPACITY_OPTIMIZED: Batch selects the instance pool with the most
    # available Spot capacity, minimising interruption probability. Recommended by
    # ADR-002 for idempotent fan-out workloads.
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"

    # c6i.2xlarge (8 vCPU / 16 GiB) and m6i.2xlarge (8 vCPU / 32 GiB) match
    # CodeBuild BUILD_GENERAL1_2XLARGE for a like-for-like migration.
    # Two instance types give Batch more pools to draw Spot capacity from.
    instance_type = ["c6i.2xlarge", "m6i.2xlarge"]

    min_vcpus = 0
    max_vcpus = var.max_vcpus

    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.fanout.id]
    # instance_role must be the instance profile ARN (not the IAM role ARN)
    instance_role = aws_iam_instance_profile.batch_instance.arn

    # spot_iam_fleet_role: present for provider v5 / Checkov CKV_AWS_97 compliance.
    # SPOT_CAPACITY_OPTIMIZED uses EC2 Fleet internally (not Spot Fleet), so this
    # role is not exercised at runtime. Its presence satisfies the schema constraint
    # without introducing permissions risk.
    spot_iam_fleet_role = aws_iam_role.spot_fleet.arn

    # Wire in the launch template that forces aws/ebs-encrypted root EBS (SCP compliance).
    # version = aws_launch_template.fanout.latest_version pins the CE to the actual numeric
    # version rather than the symbolic "$Latest" or "$Default" strings.
    #
    # Why not "$Default" + update_default_version = true?
    # "$Default" is a static string - Terraform sees no attribute diff when the underlying
    # default version number advances (v2->v3), so CE replacement never triggers and Batch
    # keeps launching the old version. A numeric latest_version changes on every LT update,
    # produces a CE attribute diff, and (via create_before_destroy + name_prefix) forces CE
    # replacement so Batch re-resolves the LT version on every Terraform-managed LT change.
    #
    # update_default_version = true is still set on the LT so that $Default and $Latest both
    # point at the current content, keeping manual AWS Console launches consistent.
    launch_template {
      launch_template_id = aws_launch_template.fanout.id
      version            = aws_launch_template.fanout.latest_version
    }

    # Tag the EC2 instances for cost attribution
    tags = merge(var.tags, {
      Name = "${var.project_name}-fanout-instance"
    })
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-fanout-ce"
  })

  # Ensure managed policies are attached before Batch tries to use the roles
  depends_on = [
    aws_iam_role_policy_attachment.batch_service,
    aws_iam_role_policy_attachment.batch_instance,
    aws_iam_role_policy_attachment.spot_fleet,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Batch Job Queue - Spot fan-out
###############################################################################

resource "aws_batch_job_queue" "fanout" {
  name     = "${var.project_name}-fanout-queue"
  state    = "ENABLED"
  priority = 1

  # compute_environment_order is the provider v5 recommended form
  # (the deprecated compute_environments list form is avoided)
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.fanout_spot.arn
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-fanout-queue"
  })
}

###############################################################################
# Batch Job Definition - chunk (EC2 Spot, per-chunk fan-out)
###############################################################################

resource "aws_batch_job_definition" "chunk" {
  name                  = "${var.project_name}-chunk"
  type                  = "container"
  platform_capabilities = ["EC2"]

  # Default parameter values - overridden at submit time by the SFN Map.
  # Ref:: substitution resolves these into the environment[] block at runtime.
  parameters = {
    chunk_key     = ""
    input_bucket  = ""
    output_bucket = ""
    contact_id    = ""
    contact_date  = ""
    chunk_id      = "0"
    kms_key_id    = ""
  }

  container_properties = jsonencode({
    image = var.sdr_ecr_image_url

    # resourceRequirements is the current-generation approach.
    # 8 vCPU / 16 GiB matches BUILD_GENERAL1_2XLARGE for a like-for-like comparison.
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "16384" },
    ]

    # Three-step pipeline: download -> IQ extract -> SatDump -> upload (production prefix)
    command = ["/bin/bash", "-c", local.chunk_entrypoint]

    # Batch Ref:: parameter substitution - values supplied by the SFN per job
    environment = [
      { name = "INPUT_BUCKET", value = "Ref::input_bucket" },
      { name = "INPUT_KEY", value = "Ref::chunk_key" },
      { name = "OUTPUT_BUCKET", value = "Ref::output_bucket" },
      { name = "CONTACT_ID", value = "Ref::contact_id" },
      { name = "CONTACT_DATE", value = "Ref::contact_date" },
      { name = "CHUNK_ID", value = "Ref::chunk_id" },
      { name = "KMS_KEY_ID", value = "Ref::kms_key_id" },
    ]

    jobRoleArn       = aws_iam_role.batch_chunk_job.arn
    executionRoleArn = aws_iam_role.batch_execution.arn

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.fanout.name
        "awslogs-region"        = data.aws_region.current.id
        "awslogs-stream-prefix" = "chunk"
      }
    }
  })

  # Spot retryStrategy (Req 1.4) - this does NOT exist in compute_bakeoff; it is new.
  #
  # evaluate_on_exit rules are evaluated in order; first match wins:
  #   1. on_status_reason = "Host EC2*" matches the Spot instance reclaim event
  #      ("Host EC2 (i-xxxxx) terminated"). Action: RETRY - up to (attempts-1) times.
  #   2. on_reason = "*" matches every other failure (processing error, OOM, etc.).
  #      Action: EXIT - do not waste a retry on an unrecoverable error.
  #
  # Combined with the idempotent prefix clear in chunk_entrypoint, a Spot-retry
  # after a partial upload starts from a clean slate (Req 1.4 + Req 1.8).
  retry_strategy {
    attempts = 2

    evaluate_on_exit {
      on_status_reason = "Host EC2*"
      action           = "RETRY"
    }

    evaluate_on_exit {
      on_reason = "*"
      action    = "EXIT"
    }
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-chunk"
  })
}

###############################################################################
# CloudWatch Log Group - aggregation jobs (Stage 2, on-demand CE)
###############################################################################

resource "aws_cloudwatch_log_group" "aggregation" {
  # checkov:skip=CKV_AWS_338: 90-day retention is deliberate for job execution logs.
  # Aggregation job traces are operational logs; the science products (SDR/GEO HDF5)
  # are the permanent record, stored in S3 with versioning.
  name              = local.aggregation_log_group_name
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name = local.aggregation_log_group_name
  })
}

###############################################################################
# Batch Compute Environment - MANAGED, EC2 ON-DEMAND (aggregation only)
###############################################################################
# On-demand (not Spot): the aggregation job runs RT-STPS -> CSPP for ~25-40 min.
# A mid-run Spot reclaim wastes the entire run; on-demand removes that risk.
#
# Shares the proven Stage 1 patterns:
#   - same launch template (200 GiB aws/ebs root, IMDSv2, Owner tag)
#   - same security group (HTTPS 443 egress for ECR, S3, CloudWatch)
#   - same instance role + service role
#   - compute_environment_name_prefix + create_before_destroy for safe CE replacement
#
# No spot_iam_fleet_role: that role is only required for SPOT CE type.

resource "aws_batch_compute_environment" "aggregation_ondemand" {
  compute_environment_name_prefix = "${var.project_name}-agg-ce-"
  type                            = "MANAGED"
  state                           = "ENABLED"
  service_role                    = aws_iam_role.batch_service.arn

  compute_resources {
    type = "EC2"
    # BEST_FIT_PROGRESSIVE: selects the lowest-cost instance type with available
    # on-demand capacity, scaling up to more expensive types as needed.
    # Suitable for a single long-running job (not a fan-out of many short jobs).
    allocation_strategy = "BEST_FIT_PROGRESSIVE"

    # Same instance family as the fan-out CE for consistent image pull behaviour
    # (the 200 GiB root volume / decompressed image footprint is the same).
    instance_type = ["c6i.2xlarge", "m6i.2xlarge"]

    min_vcpus = 0
    max_vcpus = var.aggregation_max_vcpus

    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.fanout.id]
    instance_role      = aws_iam_instance_profile.batch_instance.arn

    # Reuse the fan-out launch template: aws/ebs-encrypted 200 GiB root (SCP
    # p-eb2chkmi compliance + space for decompressed SDR image + CSPP scratch),
    # IMDSv2, Owner tag. No second launch template needed -- same requirements.
    # Numeric latest_version forces CE replacement on every LT update (see the
    # fan-out CE comment for the "$Default" vs latest_version reasoning).
    launch_template {
      launch_template_id = aws_launch_template.fanout.id
      version            = aws_launch_template.fanout.latest_version
    }

    tags = merge(var.tags, {
      Name = "${var.project_name}-agg-instance"
    })
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-agg-ce"
  })

  depends_on = [
    aws_iam_role_policy_attachment.batch_service,
    aws_iam_role_policy_attachment.batch_instance,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Batch Job Queue - on-demand aggregation
###############################################################################

resource "aws_batch_job_queue" "aggregation" {
  name     = "${var.project_name}-agg-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.aggregation_ondemand.arn
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-agg-queue"
  })
}

###############################################################################
# Batch Job Definition - aggregation (on-demand EC2)
###############################################################################
# Converged aggregation job: replaces BOTH aggregation.yml (async, RTSTPS=on)
# and aggregation_sync.yml (sync, RTSTPS=off). The RTSTPS env var selects the
# branch; all other env vars are injected at submit time by the SFN via
# ContainerOverrides.Environment (canary blocker #6: Ref:: does not work in
# environment[], only in command[]).
#
# Entrypoint bootstrap: fetches aggregation_entrypoint.sh from S3 at job start,
# then execs it. Mirrors the aggregation.yml pattern (aggregation.sh from S3),
# so a script update takes effect without rebuilding the ECR image.

resource "aws_batch_job_definition" "aggregation" {
  name                  = "${var.project_name}-aggregation"
  type                  = "container"
  platform_capabilities = ["EC2"]

  container_properties = jsonencode({
    image = var.sdr_ecr_image_url

    # 8 vCPU / 16 GiB: matches the fan-out chunk job and the CodeBuild
    # BUILD_GENERAL1_2XLARGE baseline. CSPP viirs_sdr.sh is called with -p 4
    # (4 parallel processors); 8 vCPU leaves headroom for RT-STPS + overhead.
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "16384" },
    ]

    # Three-step bootstrap: download entrypoint from S3, make executable, exec.
    # OUTPUT_BUCKET is supplied via ContainerOverrides.Environment at submit time.
    command = ["/bin/bash", "-c", local.aggregation_fetch_cmd]

    # Env vars declared with empty-string defaults for documentation purposes.
    # Real values are injected by the SFN ContainerOverrides.Environment at
    # submit time -- Ref:: substitution is only valid in command[], not here.
    environment = [
      { name = "RTSTPS", value = "" },
      { name = "OUTPUT_BUCKET", value = "" },
      { name = "RECEPTION_BUCKET", value = "" },
      { name = "CONTACT_ID", value = "" },
      { name = "CONTACT_DATE", value = "" },
      { name = "KMS_KEY_ID", value = "" },
    ]

    jobRoleArn       = aws_iam_role.batch_aggregation_job.arn
    executionRoleArn = aws_iam_role.batch_execution.arn

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.aggregation.name
        "awslogs-region"        = data.aws_region.current.id
        "awslogs-stream-prefix" = "aggregation"
      }
    }
  })

  # On-demand: not subject to Spot reclaim. A single attempt is correct --
  # aggregation is not cheap to retry (RT-STPS + CSPP can take 25-40 min).
  retry_strategy {
    attempts = 1
  }

  # 3600 s = 60 min: generous headroom over the ~25-min fast-path (PERF-1 cache
  # hit) and the ~40-min worst-case (PERF-1 cache miss + sdr_luts.sh fallback).
  timeout {
    attempt_duration_seconds = 3600
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-aggregation"
  })
}
