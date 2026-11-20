# iam.tf â€” IAM roles and policies for the VIIRS visualization pipeline
#
# Resources:
#   - aws_iam_role.lambda_execution         â€” Lambda orchestrator execution role
#   - aws_iam_policy.lambda_execution       â€” Inline policy for Lambda (least-privilege)
#   - aws_iam_role_policy_attachment.lambda â€” Attaches policy to Lambda role
#   - aws_iam_role.codebuild_service        â€” CodeBuild service role
#   - aws_iam_policy.codebuild_service      â€” Inline policy for CodeBuild (least-privilege)
#   - aws_iam_role_policy_attachment.codebuild â€” Attaches policy to CodeBuild role
#
# Satisfies: Requirements 11.3, 11.4

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Lambda execution role
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

resource "aws_iam_role" "lambda_execution" {
  name = "${var.project_name}-viirs-lambda-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowLambdaAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_policy" "lambda_execution" {
  name        = "${var.project_name}-viirs-lambda-execution"
  description = "Least-privilege policy for the VIIRS visualization Lambda orchestrator"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # CloudWatch Logs â€” write logs for this Lambda function
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:*:${var.account_id}:log-group:/aws/lambda/${var.project_name}-viirs-*:*"
      },
      # CodeBuild â€” start visualization build jobs
      {
        Sid      = "StartCodeBuild"
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = "arn:aws:codebuild:*:${var.account_id}:project/${var.project_name}-viirs-*"
      },
      # Batch - submit the render job (Stage 3b). Scoped to BOTH the shared
      # on-demand aggregation queue (render reuses project_name-agg-queue) and
      # the unversioned render job definition. Region uses the * wildcard to
      # match this file's existing ARN convention (no region var in this module).
      {
        Sid    = "SubmitRenderBatchJob"
        Effect = "Allow"
        Action = "batch:SubmitJob"
        Resource = [
          "arn:aws:batch:*:${var.account_id}:job-queue/${var.project_name}-agg-queue",
          "arn:aws:batch:*:${var.account_id}:job-definition/${var.project_name}-render",
        ]
      },
      # IAM PassRole is required so the Lambda can submit a job whose job
      # definition references these task/execution roles. The PassedToService
      # condition limits the grant to ECS tasks (Batch on EC2 uses the ECS task
      # model, matching the render job roles' trust policy principal).
      {
        Sid    = "PassRenderJobRoles"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          "arn:aws:iam::${var.account_id}:role/${var.project_name}-batch-render-job",
          "arn:aws:iam::${var.account_id}:role/${var.project_name}-batch-render-execution",
        ]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      },
      # S3 â€” list and read objects from the SDR output bucket
      {
        Sid      = "S3ListBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.sdr_output_bucket_arn
      },
      {
        Sid      = "S3GetObject"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${var.sdr_output_bucket_arn}/*"
      },
      # KMS â€” decrypt objects encrypted with the project KMS key
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = var.kms_key_arn
      },
      # CloudWatch Metrics â€” publish ProductsWritten and other pipeline metrics.
      # Resource = "*" is the only valid value: cloudwatch:PutMetricData has no
      # resource-level constraint in IAM.  The namespace condition below limits
      # this permission to the SDRPipeline namespace exclusively, making the
      # effective scope equivalent to a resource-scoped statement.
      {
        Sid      = "CloudWatchPutMetricData"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*" # PutMetricData is un-scopeable by resource; scoped by namespace condition
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "SDRPipeline"
          }
        }
      },
      ],
      # Sync bucket: the sync state machine passes the sync bucket in the
      # StartVisualization event, and the orchestrator lists/reads the contact
      # prefix there. Prefix-scoped (contacts/* only), no write. Omitted when
      # the sync pipeline is disabled.
      [for s in [
        {
          Sid      = "S3ListSyncBucketContactsPrefix"
          Effect   = "Allow"
          Action   = "s3:ListBucket"
          Resource = var.sync_output_bucket_arn
          Condition = {
            StringLike = {
              "s3:prefix" = ["contacts/*"]
            }
          }
        },
        {
          Sid      = "S3GetSyncContactsPrefix"
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = "${var.sync_output_bucket_arn}/contacts/*"
        },
      ] : s if var.sync_output_bucket_arn != ""],
    )
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "lambda_execution" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_execution.arn
}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CodeBuild service role
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

resource "aws_iam_role" "codebuild_service" {
  name = "${var.project_name}-viirs-codebuild-service"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCodeBuildAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "codebuild.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_policy" "codebuild_service" {
  name        = "${var.project_name}-viirs-codebuild-service"
  description = "Least-privilege policy for the VIIRS visualization CodeBuild project"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 â€” read inputs and write rendered products to the SDR output bucket
      {
        Sid      = "S3ListBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.sdr_output_bucket_arn
      },
      {
        Sid    = "S3ReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${var.sdr_output_bucket_arn}/*"
      },
      # KMS â€” encrypt/decrypt S3 objects written with SSE-KMS
      {
        Sid    = "KMSEncryptDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = var.kms_key_arn
      },
      # CloudWatch Logs â€” write CodeBuild build logs
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:*:${var.account_id}:log-group:/aws/codebuild/${var.project_name}-viirs-*:*"
      },
      # ECR â€” pull the visualization Docker image
      {
        Sid      = "ECRAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # GetAuthorizationToken has no resource-level constraint
      },
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = "arn:aws:ecr:*:${var.account_id}:repository/${var.project_name}-viirs-*"
      },
      # ECR â€” push the visualization Docker image (docker build + push)
      {
        Sid    = "ECRPushImage"
        Effect = "Allow"
        Action = [
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = "arn:aws:ecr:*:${var.account_id}:repository/${var.project_name}-viirs-*"
      },
      # CloudWatch Metrics â€” publish ProductsWritten from the buildspec post_build phase.
      # Resource = "*" is the only valid value: cloudwatch:PutMetricData has no
      # resource-level constraint in IAM.  The namespace condition below limits
      # this permission to the SDRPipeline namespace exclusively, making the
      # effective scope equivalent to a resource-scoped statement.
      {
        Sid      = "CloudWatchPutMetricData"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*" # PutMetricData is un-scopeable by resource; scoped by namespace condition
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "SDRPipeline"
          }
        }
      },
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "codebuild_service" {
  role       = aws_iam_role.codebuild_service.name
  policy_arn = aws_iam_policy.codebuild_service.arn
}
