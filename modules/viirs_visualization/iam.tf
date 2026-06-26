# iam.tf — IAM roles and policies for the VIIRS visualization pipeline
#
# Resources:
#   - aws_iam_role.lambda_execution         — Lambda orchestrator execution role
#   - aws_iam_policy.lambda_execution       — Inline policy for Lambda (least-privilege)
#   - aws_iam_role_policy_attachment.lambda — Attaches policy to Lambda role
#   - aws_iam_role.codebuild_service        — CodeBuild service role
#   - aws_iam_policy.codebuild_service      — Inline policy for CodeBuild (least-privilege)
#   - aws_iam_role_policy_attachment.codebuild — Attaches policy to CodeBuild role
#
# Satisfies: Requirements 11.3, 11.4

# ─────────────────────────────────────────────
# Lambda execution role
# ─────────────────────────────────────────────

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
    Statement = [
      # CloudWatch Logs — write logs for this Lambda function
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
      # CodeBuild — start visualization build jobs
      {
        Sid      = "StartCodeBuild"
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = "arn:aws:codebuild:*:${var.account_id}:project/${var.project_name}-viirs-*"
      },
      # S3 — list and read objects from the SDR output bucket
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
      # KMS — decrypt objects encrypted with the project KMS key
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = var.kms_key_arn
      },
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "lambda_execution" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_execution.arn
}

# ─────────────────────────────────────────────
# CodeBuild service role
# ─────────────────────────────────────────────

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
      # S3 — read inputs and write rendered products to the SDR output bucket
      {
        Sid    = "S3ReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${var.sdr_output_bucket_arn}/*"
      },
      # KMS — encrypt/decrypt S3 objects written with SSE-KMS
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
      # CloudWatch Logs — write CodeBuild build logs
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = "logs:*"
        Resource = "arn:aws:logs:*:${var.account_id}:log-group:/aws/codebuild/${var.project_name}-viirs-*:*"
      },
      # ECR — pull the visualization Docker image
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
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "codebuild_service" {
  role       = aws_iam_role.codebuild_service.name
  policy_arn = aws_iam_policy.codebuild_service.arn
}
