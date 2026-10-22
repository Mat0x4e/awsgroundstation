# iam.tf — IAM roles for the SDR pipeline module
# Dedicated roles following least-privilege principles (req 8.2):
#   1. sfn_role                — assumed by Step Functions for pipeline orchestration
#   2. eventbridge_role        — assumed by EventBridge to trigger the state machine
#   3. codebuild_docker_build  — assumed by CodeBuild for Docker image builds (codebuild_image.tf)
#   4. lut_cache_refresh       — assumed by CodeBuild for the weekly LUT cache refresh (lut_cache.tf)
#
# codebuild_role (chunk processing + async aggregation CodeBuild) was retired in
# task 8 (clean-compute-pipeline Stage 2): both fan-out and aggregation now run
# on Batch. See codebuild.tf for the retirement note.

data "aws_region" "current" {}


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

      # ListChunks (s3:listObjectsV2) enumerates this contact's .pcap objects in
      # the reception bucket. Without this the state machine cannot build the
      # chunks[] array and the trigger is inert.
      {
        Sid      = "S3ListReceptionBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.input_bucket_name}"
      },

      # StartVisualization invokes the VIIRS orchestrator once aggregation
      # succeeds. The ARN is built by convention rather than referenced:
      # modules/viirs_visualization already consumes this module's outputs, so a
      # reference back would be a circular module dependency.
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

###############################################################################
# 2a. Step Functions Batch permissions (clean-compute-pipeline Stage 1)
# Always created: fan-out is now Batch-only (ChooseCompute gate removed in
# task 4). The policy was previously gated on var.use_batch_fanout; count=1
# makes it unconditional so the SFN role always carries batch:SubmitJob.
# Kept as a separate resource to avoid restructuring the main sfn policy block.
###############################################################################

resource "aws_iam_role_policy" "sfn_batch" {
  count = 1
  name  = "${var.project_name}-sfn-batch-policy"
  role  = aws_iam_role.sfn.id

  # checkov:skip=CKV_AWS_355: batch:DescribeJobs and batch:TerminateJob cannot
  # be scoped to a specific resource ARN (AWS service constraint -- job IDs are
  # not ARNs at policy evaluation time). Resource: * is the only valid value.
  # checkov:skip=CKV_AWS_290: Same constraint as above.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # batch:SubmitJob -- scoped to the fan-out queue+chunk-jobdef (Stage 1)
      # AND the aggregation queue+jobdef (Stage 2, strangler).
      # compact() drops empty strings so the policy stays valid when either
      # set of aggregation ARNs is not yet provided (enable_batch_compute=false).
      {
        Sid    = "BatchSubmitJob"
        Effect = "Allow"
        Action = ["batch:SubmitJob"]
        Resource = compact([
          var.batch_fanout_job_queue_arn,
          var.batch_chunk_job_definition_arn,
          var.batch_aggregation_job_queue_arn,
          var.batch_aggregation_job_definition_arn,
        ])
      },
      # batch:DescribeJobs and batch:TerminateJob are required by the
      # :::batch:submitJob.sync integration for polling and cleanup.
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
# 4. CodeBuild Docker Image Build Role  (Requirement 1, OE-1 / SEC-3)
###############################################################################
# Dedicated role for aws_codebuild_project.sdr_docker_build.
# Scoped to exactly what the docker build needs:
#   - S3 GetObject on the docker-build/* prefix of the SDR output bucket
#   - S3 ListBucket conditioned to the docker-build/ prefix
#   - ECR push to the sdr_pipeline repository
#   - CloudWatch Logs write (three actions, not logs:*) for the build log group
#   - KMS Decrypt + GenerateDataKey for S3 SSE-KMS objects
#
# This codifies the hand-added S3 read statement that existed on the deployed
# out-of-band "satdump-noaa20-processing" role (acceptance criterion 1.2).

resource "aws_iam_role" "codebuild_docker_build" {
  name        = "${var.project_name}-sdr-docker-build"
  description = "Allows CodeBuild to build and push the SDR Docker image to ECR"

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
    Name    = "${var.project_name}-sdr-docker-build"
    Service = "sdr-pipeline"
  })
}

resource "aws_iam_role_policy" "codebuild_docker_build" {
  name = "${var.project_name}-sdr-docker-build-policy"
  role = aws_iam_role.codebuild_docker_build.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — read Docker build artefacts from the docker-build/ prefix of the SDR
      # output bucket (vendor tarballs: SatDump, RT-STPS, CSPP — staged separately).
      # GetObject scoped to the prefix; ListBucket conditioned to the same prefix.
      {
        Sid      = "S3GetDockerBuildArtifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.sdr_output.arn}/docker-build/*"
      },
      {
        Sid      = "S3ListDockerBuildPrefix"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sdr_output.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["docker-build/*"]
          }
        }
      },
      # KMS — decrypt and generate data keys for S3 SSE-KMS objects in the output bucket
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = var.kms_key_arn
      },
      # CloudWatch Logs — write build logs; three write actions, not logs:*
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        # :* covers all log streams under the docker-build log group
        Resource = "${aws_cloudwatch_log_group.codebuild_docker_build.arn}:*"
      },
      # ECR — push the built image; scoped to the sdr_pipeline repository only
      {
        Sid    = "ECRPushImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
        ]
        Resource = aws_ecr_repository.sdr_pipeline.arn
      },
      # ECR — GetAuthorizationToken cannot be scoped to a specific resource ARN
      # (AWS service constraint: the action operates on the registry endpoint, not a
      # specific repository). Resource: * is the only valid value for this action.
      {
        Sid      = "ECRAuthToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
    ]
  })
}
