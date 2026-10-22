# iam.tf - Least-privilege IAM for the production Batch compute module (Req 8).
#
# Mirrors compute_bakeoff/iam.tf with production-scope changes:
#   - S3 writes scoped to contacts/* (not bakeoff/*) - production output prefix
#   - cloudwatch:PutMetricData conditioned on namespace=SDRPipeline (Req 8.2)
#     compute_bakeoff deliberately has NO PutMetricData - this is a new statement
#   - DeleteObject added to the job role for idempotent prefix clear (aws s3 rm)
#   - ecr:GetAuthorizationToken on Resource:"*" is the sole un-scopeable action
#
# Role inventory:
#   1. batch_service      - service=batch.amazonaws.com, AWSBatchServiceRole managed policy
#   2. spot_fleet         - service=spotfleet.amazonaws.com, AmazonEC2SpotFleetTaggingRole
#   3. batch_instance     - service=ec2.amazonaws.com, AmazonEC2ContainerServiceforEC2Role
#                           + aws_iam_instance_profile.batch_instance
#   4. batch_chunk_job    - task/job role assumed by the running chunk container
#   5. batch_execution    - ECS task execution role (ECR pull + logs, pre-container phase)

###############################################################################
# 1. Batch Service Role
###############################################################################

resource "aws_iam_role" "batch_service" {
  name        = "${var.project_name}-batch-service"
  description = "Allows AWS Batch to call EC2, ECS, and CloudWatch on behalf of the compute environments in this module"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "batch.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.account_id
          }
        }
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-service"
  })
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

###############################################################################
# 2. Spot Fleet Role
###############################################################################
# Required in the compute_resources block for SPOT type to satisfy Checkov CKV_AWS_97.
# SPOT_CAPACITY_OPTIMIZED uses EC2 Fleet internally (not Spot Fleet), so this role
# is not exercised at runtime. Its presence satisfies the provider schema constraint.

resource "aws_iam_role" "spot_fleet" {
  name        = "${var.project_name}-batch-spot-fleet"
  description = "Spot Fleet IAM role - provider v5 schema requirement for SPOT CE type (not exercised at runtime with SPOT_CAPACITY_OPTIMIZED)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "spotfleet.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-spot-fleet"
  })
}

resource "aws_iam_role_policy_attachment" "spot_fleet" {
  role       = aws_iam_role.spot_fleet.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

###############################################################################
# 3. EC2 Instance Role + Instance Profile
###############################################################################
# The ECS agent on each Batch EC2 instance uses this role to register with ECS,
# pull images, and route container logs to CloudWatch.

resource "aws_iam_role" "batch_instance" {
  name        = "${var.project_name}-batch-instance"
  description = "EC2 instance role for Batch compute environments - ECS agent registration, ECR image pull, CloudWatch log routing"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # No SourceAccount condition on EC2 instance roles: ec2.amazonaws.com does
        # not support the aws:SourceAccount condition key and the trust would fail.
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-instance"
  })
}

# AmazonEC2ContainerServiceforEC2Role is the AWS-managed policy for EC2 instances
# in a Batch/ECS compute environment. Minimum required for the ECS agent to register,
# pull images, and send logs.
resource "aws_iam_role_policy_attachment" "batch_instance" {
  role       = aws_iam_role.batch_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch_instance" {
  name = "${var.project_name}-batch-instance"
  role = aws_iam_role.batch_instance.name

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-instance"
  })
}

###############################################################################
# 4. Chunk Job Role (container task role)
###############################################################################
# Assumed by the running SatDump container. Scoped precisely:
#   - S3 GetObject on the reception bucket (read input .pcap chunks)
#   - S3 ListBucket on the reception bucket (conditioned to year=*/* prefix)
#   - S3 PutObject/GetObject/DeleteObject on output_bucket/contacts/* ONLY
#       PutObject - write chunk outputs
#       GetObject - aws s3 sync reads before writing
#       DeleteObject - aws s3 rm --recursive for idempotent prefix clear (Req 1.8)
#   - S3 ListBucket on output bucket conditioned to contacts/ prefix
#   - KMS Encrypt/Decrypt/GenerateDataKey on the pipeline key
#   - ECR pull on the sdr-pipeline repository only
#   - ecr:GetAuthorizationToken on Resource:"*" (AWS service constraint - see comment)
#   - logs:Create*/PutLogEvents on this module's fanout log group only
#   - cloudwatch:PutMetricData conditioned on namespace=SDRPipeline (Req 8.2, SEC-1)

resource "aws_iam_role" "batch_chunk_job" {
  name        = "${var.project_name}-batch-chunk-job"
  description = "Task role for chunk Batch jobs - S3 read reception, write contacts/* only, ECR pull, KMS, logs, namespace-conditioned metrics"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.account_id
          }
        }
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-chunk-job"
  })
}

resource "aws_iam_role_policy" "batch_chunk_job" {
  name = "${var.project_name}-batch-chunk-job-policy"
  role = aws_iam_role.batch_chunk_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 - read input .pcap chunks from the reception bucket (read-only)
      {
        Sid      = "S3ReadReceptionBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.reception_bucket_arn}/*"
      },
      {
        Sid      = "S3ListReceptionBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.reception_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["year=*/*"]
          }
        }
      },
      # S3 - write chunk outputs to the production contacts/ prefix ONLY.
      # PutObject  - upload chunk results (satdump outputs, dataset.json)
      # GetObject  - aws s3 sync reads existing objects before writing
      # DeleteObject - aws s3 rm --recursive for idempotent prefix clear before retry (Req 1.8)
      # The bakeoff/ prefix and all other prefixes are explicitly excluded.
      {
        Sid    = "S3WriteContactsPrefix"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
        ]
        Resource = "${var.output_bucket_arn}/contacts/*"
      },
      # s3:ListBucket is a bucket-level action (Resource = bucket ARN, not object ARN).
      # Conditioned to contacts/ so it cannot enumerate keys outside the production prefix.
      {
        Sid      = "S3ListOutputBucketContactsPrefix"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.output_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["contacts/*"]
          }
        }
      },
      # KMS - encrypt/decrypt data keys for S3 SSE-KMS on both reception and output buckets
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = var.kms_key_arn
      },
      # ECR - pull the SDR pipeline image from the specific repository only
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = local.ecr_repo_arn
      },
      # ECR - GetAuthorizationToken cannot be scoped to a specific repository ARN.
      # AWS service constraint: this action authenticates the Docker client to the
      # registry endpoint (account-level), not a specific repository. Resource:"*"
      # is the only valid value the IAM policy evaluator accepts for this action.
      {
        Sid      = "ECRAuthTokenUnScopeable"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # CloudWatch Logs - three write actions, scoped to the fanout log group only.
      # No logs:* wildcard. The :* suffix covers all log streams under the group ARN.
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.fanout.arn}:*"
      },
      # CloudWatch Metrics - emit chunk metrics (e.g. ChunksSucceeded, ChunksFailed).
      # Conditioned on namespace=SDRPipeline so this grant cannot write to any other
      # namespace (Req 8.2, SEC-1 parity - compute_bakeoff has NO PutMetricData).
      # Resource:"*" is required - cloudwatch:PutMetricData has no resource ARN scope.
      {
        Sid      = "CloudWatchPutMetricDataScopedNamespace"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "SDRPipeline"
          }
        }
      },
    ]
  })
}

###############################################################################
# 5. Execution Role (ECS task execution role)
###############################################################################
# Used by the ECS agent on the EC2 instance to pull the image from ECR and route
# container logs to CloudWatch before the container process starts. Minimal scope only.

resource "aws_iam_role" "batch_execution" {
  name        = "${var.project_name}-batch-execution"
  description = "ECS task execution role for Batch jobs - ECR pull and CloudWatch Logs write only (pre-container phase)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.account_id
          }
        }
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-execution"
  })
}

resource "aws_iam_role_policy" "batch_execution" {
  name = "${var.project_name}-batch-execution-policy"
  role = aws_iam_role.batch_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ECR - pull the SDR pipeline image from the specific repository only
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = local.ecr_repo_arn
      },
      # ECR - GetAuthorizationToken cannot be scoped to a specific repository ARN
      # (AWS service constraint - authenticates to the registry endpoint, not a repo)
      {
        Sid      = "ECRAuthTokenUnScopeable"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # CloudWatch Logs - write container start-up logs; three actions, never logs:*
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.fanout.arn}:*"
      },
    ]
  })
}

###############################################################################
# 6. Aggregation Job Role (container task role) - Stage 2
###############################################################################
# Assumed by the running aggregation container (RT-STPS + CSPP). Scope:
#   - S3 read+write on output_bucket/contacts/* (cadu download, rdr/sdr/log upload)
#   - S3 read on output_bucket/software/* (entrypoint + J01 LUT tarball)
#   - S3 read on output_bucket/lut-cache/* (PERF-1 SDR_4_1_DB cache fast-path)
#   - S3 ListBucket on output bucket conditioned to those three prefixes
#   - S3 GetObject + ListBucket on reception bucket (RECEPTION_BUCKET parity)
#   - KMS Encrypt/Decrypt/GenerateDataKey on the pipeline key
#   - ECR pull on the sdr-pipeline repository + GetAuthorizationToken on *
#   - logs:Create*/PutLogEvents on the aggregation log group only
# No PutMetricData: the aggregation entrypoint does not emit custom metrics.

resource "aws_iam_role" "batch_aggregation_job" {
  name        = "${var.project_name}-batch-agg-job"
  description = "Task role for aggregation Batch jobs - S3 rw contacts/*, ro software/*, ro lut-cache/*, ECR pull, KMS, logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.account_id
          }
        }
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-batch-agg-job"
  })
}

resource "aws_iam_role_policy" "batch_aggregation_job" {
  name = "${var.project_name}-batch-agg-job-policy"
  role = aws_iam_role.batch_aggregation_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 - read+write on contacts/* (download cadu, upload rdr/sdr/logs)
      # GetObject    - download .cadu files (async) or .h5 RDR (sync)
      # PutObject    - upload rdr/sdr/logs
      # DeleteObject - future idempotent retry safety (mirrors chunk role pattern)
      {
        Sid    = "S3ReadWriteContactsPrefix"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${var.output_bucket_arn}/contacts/*"
      },
      # S3 - read the aggregation entrypoint script and J01 LUT tarball
      {
        Sid      = "S3ReadSoftwarePrefix"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.output_bucket_arn}/software/*"
      },
      # S3 - read the PERF-1 LUT DB cache (SDR_4_1_DB/)
      {
        Sid      = "S3ReadLutCachePrefix"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.output_bucket_arn}/lut-cache/*"
      },
      # S3 ListBucket - bucket-level action scoped to the three permitted prefixes
      {
        Sid      = "S3ListOutputBucketPermittedPrefixes"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.output_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = [
              "contacts/*",
              "software/*",
              "lut-cache/*",
            ]
          }
        }
      },
      # S3 - read the reception bucket (RECEPTION_BUCKET parity; not used by
      # the converged entrypoint but provided to match the async SFN override)
      {
        Sid      = "S3ReadReceptionBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.reception_bucket_arn}/*"
      },
      {
        Sid      = "S3ListReceptionBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.reception_bucket_arn
      },
      # KMS - encrypt/decrypt data keys for S3 SSE-KMS uploads
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = var.kms_key_arn
      },
      # ECR - pull the SDR pipeline image
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = local.ecr_repo_arn
      },
      # ECR - GetAuthorizationToken cannot be scoped to a specific repository ARN
      # (AWS service constraint -- authenticates to the registry endpoint, not a repo)
      {
        Sid      = "ECRAuthTokenUnScopeable"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # CloudWatch Logs - scoped to the aggregation log group only
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.aggregation.arn}:*"
      },
    ]
  })
}
