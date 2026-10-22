# main.tf — AWS Batch-on-EC2-Spot compute environment for the SDR chunk fan-out bakeoff.
#
# Gated behind enable_compute_bakeoff (default false) in the root module.
# All resources are prefixed "bakeoff-" and tagged Objective=compute-bakeoff / Disposable=true.
# The per-chunk command is identical to the CodeBuild buildspec in step_functions.tf, with
# outputs routed to the bakeoff/ throwaway prefix instead of the production contacts/ tree.
#
# NEVER apply this module in isolation — operators apply via the root module after reading
# the cost and teardown notes in .kiro/specs/compute-bakeoff-batch-spot/.

###############################################################################
# Data sources — region, default VPC and subnets
# Mirrors the pattern in sdr_pipeline/ec2.tf
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
  bakeoff_prefix         = "bakeoff-${var.project_name}"
  bakeoff_log_group_name = "/aws/batch/bakeoff-${var.project_name}"

  # ECR repository ARN — derived from the naming convention used by the sdr_pipeline module.
  # That module always names its ECR repo "${project_name}-sdr-pipeline".
  ecr_repo_arn = "arn:aws:ecr:${data.aws_region.current.id}:${var.account_id}:repository/${var.project_name}-sdr-pipeline"

  # Bakeoff-specific tags merged on top of caller-supplied common tags
  bakeoff_tags = merge(var.tags, {
    Objective  = "compute-bakeoff"
    Disposable = "true"
  })

  # Per-chunk entrypoint — mirrors the CodeBuild buildspec injected by step_functions.tf
  # (the BuildspecOverride in StartCodeBuild), adapted for Batch:
  #   - Same three-step pipeline: S3 download → IQ extraction → SatDump
  #   - Same upload commands, but output goes to bakeoff/ prefix (never contacts/)
  #   - All S3/KMS env vars are injected via Batch Ref:: parameter substitution
  # Uses plain $VAR (no braces) to avoid Terraform template interpolation conflicts.
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
    "echo '[Batch] Step 2b - Uploading SatDump outputs to S3 (bakeoff/ throwaway prefix)'",
    "aws s3 sync /tmp/output/satdump/ \"s3://$OUTPUT_BUCKET/bakeoff/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/chunk_$CHUNK_ID/\" --sse aws:kms --sse-kms-key-id \"$KMS_KEY_ID\"",
    "aws s3 cp /tmp/output/satdump/dataset.json \"s3://$OUTPUT_BUCKET/bakeoff/contacts/$CONTACT_DATE/$CONTACT_ID/chunks/chunk_$CHUNK_ID/dataset.json\" --sse aws:kms --sse-kms-key-id \"$KMS_KEY_ID\" 2>/dev/null || true",
    "echo '[Batch] Chunk processing complete'",
  ])
}

###############################################################################
# CloudWatch Log Group
###############################################################################

resource "aws_cloudwatch_log_group" "bakeoff" {
  # checkov:skip=CKV_AWS_338: 30-day retention is deliberate — this is a throwaway
  # experiment log group (Disposable=true). The permanent science record lives in S3;
  # short-lived execution logs do not need 365-day retention.
  name              = local.bakeoff_log_group_name
  retention_in_days = 30
  kms_key_id        = var.kms_key_arn

  tags = merge(local.bakeoff_tags, {
    Name = local.bakeoff_log_group_name
  })
}

###############################################################################
# Batch Compute Environment — MANAGED, EC2 SPOT
###############################################################################

resource "aws_batch_compute_environment" "bakeoff" {
  compute_environment_name = "${local.bakeoff_prefix}-ce"
  type                     = "MANAGED"
  state                    = "ENABLED"
  # Explicit service role so the CE is not silently tied to the AWS-managed default
  service_role = aws_iam_role.batch_service.arn

  compute_resources {
    type = "SPOT"
    # SPOT_CAPACITY_OPTIMIZED: Batch selects the instance pool with the most
    # available Spot capacity, minimising interruption probability. This is the
    # allocation strategy recommended by the ADR for fan-out workloads.
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"

    # c6i.2xlarge (8 vCPU / 16 GiB) and m6i.2xlarge (8 vCPU / 32 GiB) match
    # CodeBuild BUILD_GENERAL1_2XLARGE (8 vCPU / ~16–32 GiB) as closely as
    # practical for a like-for-like comparison.
    instance_type = ["c6i.2xlarge", "m6i.2xlarge"]

    min_vcpus = 0
    max_vcpus = var.max_vcpus

    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.bakeoff.id]
    # instance_role must be the instance profile ARN (not the IAM role ARN)
    instance_role = aws_iam_instance_profile.batch_instance.arn

    # spot_iam_fleet_role: included for provider v5 / Checkov CKV_AWS_97 compliance.
    # For SPOT_CAPACITY_OPTIMIZED, AWS uses EC2 Fleet (not Spot Fleet) internally,
    # so this role is not exercised at runtime — but Checkov flags its absence on
    # any SPOT compute environment.
    spot_iam_fleet_role = aws_iam_role.spot_fleet.arn

    # Tag the EC2 instances themselves for cost attribution
    tags = merge(local.bakeoff_tags, {
      Name = "${local.bakeoff_prefix}-instance"
    })
  }

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-ce"
  })

  # Ensure the managed policies are attached before Batch tries to use the roles
  depends_on = [
    aws_iam_role_policy_attachment.batch_service,
    aws_iam_role_policy_attachment.batch_instance,
    aws_iam_role_policy_attachment.spot_fleet,
  ]
}

###############################################################################
# Batch Job Queue
###############################################################################

resource "aws_batch_job_queue" "bakeoff" {
  name     = "${local.bakeoff_prefix}-queue"
  state    = "ENABLED"
  priority = 1

  # compute_environment_order is the provider v5 recommended form (vs the deprecated
  # compute_environments list)
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.bakeoff.arn
  }

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-queue"
  })
}

###############################################################################
# Batch Job Definition — container, EC2, same per-chunk entrypoint as CodeBuild
###############################################################################

resource "aws_batch_job_definition" "bakeoff_chunk" {
  name = "${local.bakeoff_prefix}-chunk"
  type = "container"
  # Explicit EC2 platform (not Fargate) — must match the MANAGED/SPOT compute environment
  platform_capabilities = ["EC2"]

  # Default parameter values — the harness overrides every field at submit time.
  # Ref:: substitution resolves these into the environment[] block below.
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

    # resourceRequirements is the current-generation approach (vcpus/memory top-level
    # fields are deprecated in the AWS Batch API for EC2 jobs).
    # 8 vCPU / 16 GiB matches BUILD_GENERAL1_2XLARGE for a like-for-like measurement.
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "16384" },
    ]

    # Same three-step pipeline as the CodeBuild buildspec (step_functions.tf StartCodeBuild):
    # S3 download → IQ extraction → SatDump. Output goes to bakeoff/ throwaway prefix.
    command = ["/bin/bash", "-c", local.chunk_entrypoint]

    # Batch Ref:: parameter substitution — values supplied by the harness per job
    environment = [
      { name = "INPUT_BUCKET", value = "Ref::input_bucket" },
      { name = "INPUT_KEY", value = "Ref::chunk_key" },
      { name = "OUTPUT_BUCKET", value = "Ref::output_bucket" },
      { name = "CONTACT_ID", value = "Ref::contact_id" },
      { name = "CONTACT_DATE", value = "Ref::contact_date" },
      { name = "CHUNK_ID", value = "Ref::chunk_id" },
      { name = "KMS_KEY_ID", value = "Ref::kms_key_id" },
    ]

    jobRoleArn       = aws_iam_role.batch_job.arn
    executionRoleArn = aws_iam_role.batch_execution.arn

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.bakeoff.name
        "awslogs-region"        = data.aws_region.current.id
        "awslogs-stream-prefix" = "batch"
      }
    }
  })

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-chunk"
  })
}
