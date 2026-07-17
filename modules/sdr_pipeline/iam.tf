# iam.tf — IAM roles for the SDR pipeline module
# Three dedicated roles following least-privilege principles (req 8.2):
#   1. codebuild_role  — assumed by CodeBuild for chunk processing
#   2. sfn_role        — assumed by Step Functions for pipeline orchestration
#   3. eventbridge_role — assumed by EventBridge to trigger the state machine

data "aws_region" "current" {}

###############################################################################
# 1. CodeBuild Role
###############################################################################

resource "aws_iam_role" "codebuild" {
  name        = "${var.project_name}-codebuild"
  description = "Allows CodeBuild to read/write S3, decrypt with KMS, write CloudWatch Logs, and pull from ECR"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "codebuild.amazonaws.com" }
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
    Name    = "${var.project_name}-codebuild"
    Service = "sdr-pipeline"
  })
}

resource "aws_iam_role_policy" "codebuild" {
  name = "${var.project_name}-codebuild-policy"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — read source (input) bucket
      {
        Sid      = "ReadSourceBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.input_bucket_name}/*"
      },
      {
        Sid      = "ListSourceBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.input_bucket_name}"
      },
      # S3 — full access on output bucket (write processed files, list, clean up)
      {
        Sid    = "WriteOutputBucket"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
        ]
        Resource = "${aws_s3_bucket.sdr_output.arn}/*"
      },
      {
        Sid      = "ListOutputBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sdr_output.arn
      },
      # KMS — encrypt / decrypt data keys for S3 SSE-KMS and ECR
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
      # CloudWatch Logs — write build logs (req 8.6: includes permission errors with full detail)
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${var.account_id}:log-group:/aws/codebuild/${var.project_name}-*:*"
      },
      # CloudWatch Metrics — publish pipeline metrics under the SDRPipeline namespace
      {
        Sid      = "PutMetricData"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "SDRPipeline"
          }
        }
      },
      # ECR — pull the SDR pipeline container image
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
        ]
        Resource = aws_ecr_repository.sdr_pipeline.arn
      },
      # ECR — GetAuthorizationToken is a global action (no resource ARN)
      {
        Sid      = "ECRAuthToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
    ]
  })
}

###############################################################################
# 2. Step Functions Role
###############################################################################

resource "aws_iam_role" "sfn" {
  name        = "${var.project_name}-sfn"
  description = "Allows Step Functions to invoke CodeBuild, read/write S3 markers, publish SNS, and write execution logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "states.amazonaws.com" }
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
    Name    = "${var.project_name}-sfn"
    Service = "sdr-pipeline"
  })
}

resource "aws_iam_role_policy" "sfn" {
  name = "${var.project_name}-sfn-policy"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # CodeBuild — start and poll the SDR pipeline build project
      {
        Sid    = "InvokeCodeBuild"
        Effect = "Allow"
        Action = [
          "codebuild:StartBuild",
          "codebuild:BatchGetBuilds",
        ]
        Resource = aws_codebuild_project.sdr_pipeline.arn
      },
      # S3 — read/write/delete processing markers on the output bucket
      {
        Sid    = "S3ProcessingMarkers"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${aws_s3_bucket.sdr_output.arn}/*"
      },
      {
        Sid      = "S3ListOutputBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sdr_output.arn
      },
      # SNS — publish pipeline completion / failure notifications
      {
        Sid      = "SNSPublish"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_topic_arn
      },
      # KMS — encrypt/decrypt for S3 SSE-KMS operations
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
      # CloudWatch Logs — scoped write actions for the SFN log group (CKV_AWS_355/CKV_AWS_290)
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${var.account_id}:log-group:/aws/states/${var.project_name}-*:*"
      },
      # CloudWatch Logs — log delivery management actions require Resource: * per AWS docs
      # (CreateLogDelivery, GetLogDelivery, etc. are not resource-scopeable).
      # checkov:skip=CKV_AWS_355: Log delivery management actions cannot be scoped to a specific resource
      # checkov:skip=CKV_AWS_290: Log delivery management actions cannot be scoped to a specific resource
      {
        Sid    = "CloudWatchLogsDelivery"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      # Lambda — invoke the aggregation Trigger Lambda from the FinalAggregation state
      {
        Sid      = "InvokeTriggerLambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.aggregation_trigger.arn
      },
      # SSM — poll command status from the CheckSSMStatus SDK integration state.
      # GetCommandInvocation does not support resource-level scoping (command IDs are
      # not ARNs), so Resource: * is required here.
      # checkov:skip=CKV_AWS_355: ssm:GetCommandInvocation cannot be scoped to a specific resource ARN
      {
        Sid      = "SSMGetCommandInvocation"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
    ]
  })
}

###############################################################################
# 3. EventBridge Role
###############################################################################

resource "aws_iam_role" "eventbridge" {
  name        = "${var.project_name}-eventbridge"
  description = "Allows EventBridge to start the SDR pipeline Step Functions state machine only"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
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
    Name    = "${var.project_name}-eventbridge"
    Service = "sdr-pipeline"
  })
}

resource "aws_iam_role_policy" "eventbridge" {
  name = "${var.project_name}-eventbridge-policy"
  role = aws_iam_role.eventbridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Scoped to the single SDR pipeline state machine — no wildcard
      {
        Sid      = "StartStateMachine"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = aws_sfn_state_machine.sdr_pipeline.arn
      },
    ]
  })
}
