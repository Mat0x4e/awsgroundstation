# iam.tf — IAM roles for the synchronous pipeline module
# Two dedicated roles, mirroring modules/sdr_pipeline/iam.tf:
#   1. sfn_role         — assumed by Step Functions for post-contact orchestration
#   2. eventbridge_role — assumed by EventBridge to start the state machine on
#                         contact COMPLETED (the PREPASS arm trigger invokes a
#                         Lambda directly instead -- see lambda.tf's
#                         aws_lambda_permission -- so it needs no IAM role here)
#
# Aggregation runs on the SHARED Batch job (modules/batch_compute), whose job
# role holds the sync bucket permissions -- there is no sync CodeBuild role.

data "aws_region" "current" {}

###############################################################################
# 1. Step Functions Role
###############################################################################

resource "aws_iam_role" "sfn" {
  name        = "${var.project_name}-sync-sfn"
  description = "Allows Step Functions to submit the shared Batch aggregation job, the reused viz Lambda, read/write S3, and publish SNS"

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
    Name    = "${var.project_name}-sync-sfn"
    Service = "sync-pipeline"
  })
}

resource "aws_iam_role_policy" "sfn" {
  name = "${var.project_name}-sync-sfn-policy"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — read/write/delete processing markers and RDR/SDR objects
      {
        Sid    = "S3ProcessingMarkers"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${aws_s3_bucket.sync_output.arn}/*"
      },
      {
        Sid      = "S3ListOutputBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sync_output.arn
      },
      # StartVisualization invokes the existing VIIRS orchestrator once
      # aggregation succeeds -- reused unmodified from modules/sdr_pipeline's
      # counterpart state. ARN built by convention (see that module's iam.tf
      # for the same reasoning re: avoiding a circular module dependency).
      {
        Sid      = "InvokeVisualizationOrchestrator"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${var.account_id}:function:${var.project_name}-viirs-orchestrator"
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
      # CloudWatch Logs — scoped write actions for the SFN log group
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${var.account_id}:log-group:/aws/states/${var.project_name}-sync-*:*"
      },
      # CloudWatch Logs — log delivery management actions require Resource: * per AWS docs
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
      # SSM — send the "finish" command to the receiver EC2 (upload the CADU capture)
      # and poll its completion from the FinishReceiver / CheckSSMStatus states.
      {
        Sid    = "SSMSendCommand"
        Effect = "Allow"
        Action = ["ssm:SendCommand"]
        Resource = [
          "arn:aws:ec2:${data.aws_region.current.id}:${var.account_id}:instance/${aws_instance.receiver.id}",
          "arn:aws:ssm:${data.aws_region.current.id}::document/AWS-RunShellScript",
        ]
      },
      # checkov:skip=CKV_AWS_355: ssm:GetCommandInvocation cannot be scoped to a specific resource ARN
      {
        Sid      = "SSMGetCommandInvocation"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
      # EC2 — stop the receiver once the contact ends and its data has been
      # handed off, so it isn't left running (and billing) between passes.
      {
        Sid      = "StopReceiver"
        Effect   = "Allow"
        Action   = ["ec2:StopInstances"]
        Resource = "arn:aws:ec2:${data.aws_region.current.id}:${var.account_id}:instance/${aws_instance.receiver.id}"
      },
    ]
  })
}

###############################################################################
# 1a. Step Functions Batch permissions (clean-compute-pipeline Stage 2)
# The Batch aggregation path is mandatory for the sync pipeline (see the
# precondition on aws_sfn_state_machine.sync_pipeline). There is no fan-out
# Batch step (unlike sdr_pipeline), so this policy covers the aggregation job only.
###############################################################################

resource "aws_iam_role_policy" "sfn_batch" {
  name = "${var.project_name}-sync-sfn-batch-policy"
  role = aws_iam_role.sfn.id

  # checkov:skip=CKV_AWS_355: batch:DescribeJobs and batch:TerminateJob cannot
  # be scoped to a specific resource ARN (AWS service constraint -- job IDs are
  # not ARNs at policy evaluation time). Resource: * is the only valid value.
  # checkov:skip=CKV_AWS_290: Same constraint as above.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # batch:SubmitJob -- scoped to the aggregation queue and job definition only.
      {
        Sid    = "BatchSubmitJob"
        Effect = "Allow"
        Action = ["batch:SubmitJob"]
        Resource = [
          var.batch_aggregation_job_queue_arn,
          var.batch_aggregation_job_definition_arn,
        ]
      },
      # batch:DescribeJobs and batch:TerminateJob are required by submitJob.sync.
      # Neither action supports resource-level scoping.
      {
        Sid      = "BatchMonitorAndTerminate"
        Effect   = "Allow"
        Action   = ["batch:DescribeJobs", "batch:TerminateJob"]
        Resource = "*"
      },
      # EventBridge -- submitJob.sync creates and manages one well-known managed
      # rule in the execution account. Scoped to the exact rule name.
      {
        Sid    = "EventBridgeBatchManagedRule"
        Effect = "Allow"
        Action = [
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule",
        ]
        Resource = "arn:aws:events:${data.aws_region.current.id}:${var.account_id}:rule/StepFunctionsGetEventsForBatchJobsRule"
      },
    ]
  })
}

###############################################################################
# 2. EventBridge Role — starts the state machine on contact COMPLETED only.
# The PREPASS arm trigger invokes lambda.tf's receiver_arm Lambda directly via
# a resource-based aws_lambda_permission (see eventbridge.tf), not this role.
###############################################################################

resource "aws_iam_role" "eventbridge" {
  name        = "${var.project_name}-sync-eventbridge"
  description = "Allows EventBridge to start the sync pipeline Step Functions state machine only"

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
    Name    = "${var.project_name}-sync-eventbridge"
    Service = "sync-pipeline"
  })
}

resource "aws_iam_role_policy" "eventbridge" {
  name = "${var.project_name}-sync-eventbridge-policy"
  role = aws_iam_role.eventbridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Scoped to the single sync pipeline state machine — no wildcard
      {
        Sid      = "StartStateMachine"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = aws_sfn_state_machine.sync_pipeline.arn
      },
    ]
  })
}
