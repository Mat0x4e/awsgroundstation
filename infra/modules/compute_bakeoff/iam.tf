# iam.tf — Least-privilege IAM for the Batch bakeoff module.
#
# Mirrors the pattern in sdr_pipeline/iam.tf (the CodeBuild role is the template)
# with these deliberate restrictions:
#   - S3 writes are scoped to the bakeoff/* prefix only (never the full output bucket)
#   - No cloudwatch:PutMetricData (the harness measures cost client-side via Cost Explorer)
#   - Logs use three write actions only — never logs:*
#   - ecr:GetAuthorizationToken on Resource:"*" is the sole un-scopeable action,
#     commented with an explanation of the AWS service constraint
#
# Role inventory:
#   1. batch_service   — service=batch.amazonaws.com, AWSBatchServiceRole managed policy
#   2. batch_instance  — service=ec2.amazonaws.com, AmazonEC2ContainerServiceforEC2Role
#                        + aws_iam_instance_profile.batch_instance
#   3. spot_fleet      — service=spotfleet.amazonaws.com, AmazonEC2SpotFleetTaggingRole
#   4. batch_job       — task/job role assumed by the running container
#   5. batch_execution — ECS task execution role (ECR pull + logs before container starts)
#   6. aws_security_group.bakeoff — egress 443 only, no ingress

###############################################################################
# 1. Batch Service Role
###############################################################################

resource "aws_iam_role" "batch_service" {
  name        = "${local.bakeoff_prefix}-batch-service"
  description = "Allows AWS Batch to call EC2, ECS, and CloudWatch on behalf of the bakeoff compute environment"

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

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-batch-service"
  })
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

###############################################################################
# 2. EC2 Instance Role + Instance Profile
###############################################################################

resource "aws_iam_role" "batch_instance" {
  name        = "${local.bakeoff_prefix}-instance"
  description = "EC2 instance role for the bakeoff Batch CE — allows the ECS agent to register, pull images, and route logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # No SourceAccount condition on EC2 instance roles — the EC2 service does
        # not support it and the trust would fail to validate.
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-instance"
  })
}

# AmazonEC2ContainerServiceforEC2Role is the AWS-managed policy for EC2 instances
# in a Batch/ECS compute environment. It is the minimum required for the ECS agent
# to register the instance, pull images, and send logs.
resource "aws_iam_role_policy_attachment" "batch_instance" {
  role       = aws_iam_role.batch_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch_instance" {
  name = "${local.bakeoff_prefix}-instance"
  role = aws_iam_role.batch_instance.name

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-instance"
  })
}

###############################################################################
# 3. Spot Fleet Role
###############################################################################
# Required in the compute_resources block for SPOT type in provider v5 to satisfy
# Checkov CKV_AWS_97. For SPOT_CAPACITY_OPTIMIZED strategy, AWS internally uses
# EC2 Fleet (not Spot Fleet), so this role is not exercised at runtime. Its presence
# satisfies the Terraform/Checkov schema constraint without introducing permissions risk.

resource "aws_iam_role" "spot_fleet" {
  name        = "${local.bakeoff_prefix}-spot-fleet"
  description = "Spot Fleet IAM role for the bakeoff Batch CE — provider v5 schema requirement for SPOT type (not exercised at runtime with SPOT_CAPACITY_OPTIMIZED)"

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

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-spot-fleet"
  })
}

resource "aws_iam_role_policy_attachment" "spot_fleet" {
  role       = aws_iam_role.spot_fleet.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

###############################################################################
# 4. Job Role (container task role)
###############################################################################
# Assumed by the running SatDump container. Scoped precisely:
#   - S3 GetObject/ListBucket on the reception bucket (read input chunks)
#   - S3 PutObject/GetObject on output_bucket/bakeoff/* ONLY (write throwaway outputs)
#   - S3 ListBucket on the output bucket conditioned to the bakeoff/ prefix
#   - KMS Encrypt/Decrypt/GenerateDataKey on var.kms_key_arn
#   - ECR pull on the sdr-pipeline repository ARN only
#   - ecr:GetAuthorizationToken on Resource:"*" (AWS constraint — see comment below)
#   - logs:CreateLogGroup/CreateLogStream/PutLogEvents on the bakeoff log group only

resource "aws_iam_role" "batch_job" {
  name        = "${local.bakeoff_prefix}-job"
  description = "Task role for bakeoff Batch jobs — S3 read reception, S3 write bakeoff/* only, ECR pull sdr repo, KMS, logs"

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

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-job"
  })
}

resource "aws_iam_role_policy" "batch_job" {
  name = "${local.bakeoff_prefix}-job-policy"
  role = aws_iam_role.batch_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — read input .pcap chunks from the reception bucket (read-only)
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
      # S3 — write bakeoff outputs to the throwaway bakeoff/* prefix ONLY.
      # The production contacts/ tree is explicitly excluded (different prefix).
      # GetObject is included because `aws s3 sync` reads existing objects before overwriting.
      {
        Sid    = "S3WriteBakeoffPrefix"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
        ]
        Resource = "${var.output_bucket_arn}/bakeoff/*"
      },
      # s3:ListBucket is a bucket-level action (Resource = bucket ARN, not object ARN).
      # Conditioned to the bakeoff/ prefix so it cannot enumerate production keys.
      {
        Sid      = "S3ListOutputBucketBakeoffPrefix"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.output_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["bakeoff/*"]
          }
        }
      },
      # KMS — encrypt/decrypt data keys for S3 SSE-KMS on both the reception and output buckets
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
      # ECR — pull the SDR pipeline image from the specific repository only
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
      # ECR — GetAuthorizationToken cannot be scoped to a specific repository ARN.
      # AWS service constraint: the action authenticates the Docker client to the
      # registry endpoint (account-level), not a specific repository. Resource: "*"
      # is the only valid value accepted by the IAM policy evaluator for this action.
      {
        Sid      = "ECRAuthTokenUnScopeable"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # CloudWatch Logs — three write actions scoped to the bakeoff log group only.
      # No logs:* wildcard. The :* suffix covers all log streams under the group ARN.
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.bakeoff.arn}:*"
      },
    ]
  })
}

###############################################################################
# 5. Execution Role (ECS task execution role)
###############################################################################
# Used by the ECS agent on the EC2 instance to pull the image from ECR and route
# container logs to CloudWatch before the container process starts. Minimal scope:
# ECR pull on the sdr-pipeline repo + logs write on the bakeoff log group only.

resource "aws_iam_role" "batch_execution" {
  name        = "${local.bakeoff_prefix}-execution"
  description = "ECS task execution role for bakeoff Batch jobs — ECR pull and CloudWatch Logs write only (pre-container phase)"

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

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-execution"
  })
}

resource "aws_iam_role_policy" "batch_execution" {
  name = "${local.bakeoff_prefix}-execution-policy"
  role = aws_iam_role.batch_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ECR — pull the SDR pipeline image (scoped to the specific repository)
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
      # ECR — GetAuthorizationToken cannot be scoped to a specific repository ARN
      # (AWS service constraint — authenticates to the registry endpoint, not a repo).
      {
        Sid      = "ECRAuthTokenUnScopeable"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      # CloudWatch Logs — write container logs; three actions, not logs:*
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.bakeoff.arn}:*"
      },
    ]
  })
}

###############################################################################
# 6. Security Group — egress HTTPS only, no ingress
###############################################################################
# Batch jobs pull from ECR, write to S3, and push logs to CloudWatch — all over
# port 443. No inbound connections are required; Batch workers are not servers.

resource "aws_security_group" "bakeoff" {
  name        = "${local.bakeoff_prefix}-sg"
  description = "Batch bakeoff compute instances — no inbound; HTTPS (443) egress only for ECR, S3, and CloudWatch Logs"
  vpc_id      = data.aws_vpc.default.id

  # No ingress rules — Batch job containers do not accept inbound connections

  egress {
    description = "HTTPS outbound for ECR image pull, S3 data access, and CloudWatch Logs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.bakeoff_tags, {
    Name = "${local.bakeoff_prefix}-sg"
  })
}
