# lut_cache.tf — CSPP ancillary/LUT cache + scheduled refresh job (PERF-1)
#
# Caches the CSPP SDR_4_1_DB ancillary/LUT set in S3 and refreshes it on a
# weekly schedule. This eliminates the ~10-min per-contact internet fetch
# (sdr_luts.sh from jpssdb.ssec.wisc.edu) that is the sole reason aggregation
# requires internet egress and cannot run on the EC2 instance.
#
# Cache layout — prefix convention on aws_s3_bucket.sdr_output (no new bucket):
#
#   lut-cache/SDR_4_1_DB/
#   └── mirrors ${CSPP_HOME}/anc/static/SDR_4_1_DB/ on disk, making the
#       aggregation sync command symmetric:
#         aws s3 sync s3://<bucket>/lut-cache/SDR_4_1_DB/ \
#                     $CSPP_HOME/anc/static/SDR_4_1_DB/
#
#   The lut-cache/ top-level prefix gives a clean, unambiguous IAM scope
#   (lut-cache/*) without interfering with products/, software/, or any
#   other existing prefix on the bucket.
#
# Acceptance criteria (Requirement 8):
#   8.1 — aggregation stages from S3 (fast path in aggregation.yml)
#   8.2 — refresh is scheduled, not per contact (aws_cloudwatch_event_rule below)
#   8.3 — wall-clock reduction: skips sdr_luts.sh (~10 min) on cache hit

###############################################################################
# CloudWatch log group for lut_cache_refresh builds
###############################################################################

resource "aws_cloudwatch_log_group" "lut_cache_refresh" {
  name = "/aws/codebuild/${var.project_name}-lut-cache-refresh"
  # checkov:skip=CKV_AWS_338: 90-day retention is sufficient for LUT cache refresh logs —
  # the S3 LUT cache is the permanent artefact; build logs are debug/audit only
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.project_name}-lut-cache-refresh-logs"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# IAM role for the lut_cache_refresh CodeBuild project
###############################################################################
#
# Least-privilege scoping (security self-check):
#   S3  — GetObject on software/* (J01 tarball read) only
#         PutObject/GetObject on lut-cache/* (cache write + verify) only
#         ListBucket conditioned to the two active prefixes — no whole-bucket write
#   ECR — pull only (GetDownloadUrlForLayer + Batch*) on the sdr_pipeline repo
#   KMS — Encrypt/Decrypt/GenerateDataKey on the project CMK ARN (not *)
#   CWL — three write actions scoped to the refresh log group ARN (not logs:*)
#   ECR GetAuthorizationToken — Resource: * (AWS service constraint, commented)

resource "aws_iam_role" "lut_cache_refresh" {
  name        = "${var.project_name}-lut-cache-refresh"
  description = "Least-privilege role for the CSPP LUT cache refresh CodeBuild project (PERF-1)"

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
    Name    = "${var.project_name}-lut-cache-refresh"
    Service = "sdr-pipeline"
  })
}

resource "aws_iam_role_policy" "lut_cache_refresh" {
  name = "${var.project_name}-lut-cache-refresh-policy"
  role = aws_iam_role.lut_cache_refresh.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — read the J01 shipped_luts tarball from software/ prefix only
      {
        Sid      = "S3ReadSoftwareArtifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.sdr_output.arn}/software/*"
      },
      # S3 — write and verify the LUT cache under lut-cache/ prefix only
      # (no whole-bucket write wildcard — PutObject is scoped to the cache prefix)
      {
        Sid    = "S3WriteLUTCache"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
        ]
        Resource = "${aws_s3_bucket.sdr_output.arn}/lut-cache/*"
      },
      # S3 — ListBucket scoped to the two active prefixes; no unrestricted list
      {
        Sid      = "S3ListOutputBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sdr_output.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["software/*", "lut-cache/*"]
          }
        }
      },
      # KMS — encrypt/decrypt for S3 SSE-KMS operations; scoped to the CMK ARN
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
      # CloudWatch Logs — three write actions only; scoped to the refresh log group
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.lut_cache_refresh.arn}:*"
      },
      # ECR — pull the sdr-pipeline image (carries CSPP 4.1.1 + sdr_luts.sh)
      # Pull only: no push/upload actions needed for the cache refresh job
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = aws_ecr_repository.sdr_pipeline.arn
      },
      # ECR — GetAuthorizationToken cannot be scoped to a specific resource ARN.
      # (AWS service constraint: the action targets the registry endpoint, not a
      # repository ARN.) Resource: * is the only valid value for this action.
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
# CodeBuild project — lut_cache_refresh
###############################################################################
#
# Runs sdr_luts.sh once and syncs the result to s3://<output-bucket>/lut-cache/
# SDR_4_1_DB/. Uses the sdr-pipeline ECR image (CSPP 4.1.1 + sdr_luts.sh).
# The image still needs internet egress here — this is the intentional trade:
# one internet fetch per week instead of one per contact.
#
# Mirrors the shape of aws_codebuild_project.sdr_docker_build (codebuild_image.tf):
#   - NO_ARTIFACTS, NO_CACHE
#   - ECR image + SERVICE_ROLE credentials
#   - Inline buildspec (NO_SOURCE)
#   - encryption_key = var.kms_key_arn
#
# privileged_mode = false (no Docker builds needed)
# build_timeout  = 60 min (sdr_luts.sh allotted 40 min + extraction overhead)

resource "aws_codebuild_project" "lut_cache_refresh" {
  name          = "${var.project_name}-lut-cache-refresh"
  description   = "Refreshes the CSPP LUT/ancillary DB in S3 weekly — removes internet fetch from aggregation hot path (PERF-1)"
  build_timeout = 60
  service_role  = aws_iam_role.lut_cache_refresh.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  cache {
    type = "NO_CACHE"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    image                       = "${aws_ecr_repository.sdr_pipeline.repository_url}:latest"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "SERVICE_ROLE"
    privileged_mode             = false

    environment_variable {
      name  = "OUTPUT_BUCKET"
      value = aws_s3_bucket.sdr_output.id
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "KMS_KEY_ID"
      value = var.kms_key_arn
      type  = "PLAINTEXT"
    }

    # CSPP_HOME and RTSTPS_HOME match the paths baked into the sdr-pipeline
    # image and the values set in aggregation.yml env block.
    environment_variable {
      name  = "CSPP_HOME"
      value = "/opt/SDR_4_1"
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "RTSTPS_HOME"
      value = "/opt/rt-stps"
      type  = "PLAINTEXT"
    }
  }

  # Inline buildspec (NO_SOURCE) — mirrors codebuild_image.tf pattern.
  # Shell variables ($OUTPUT_BUCKET etc.) are CodeBuild env vars at runtime;
  # they are NOT Terraform interpolations — $VAR without braces passes through.
  source {
    type      = "NO_SOURCE"
    buildspec = <<-EOT
      version: 0.2
      phases:
        pre_build:
          commands:
            - echo "PERF-1 LUT cache refresh -- $(date -u)"
            # J01 shipped LUTs -- same extraction as aggregation.yml pre_build.
            # The sdr-pipeline image carries straylight and ecotiles but NOT
            # shipped_luts; the tarball must be extracted before sdr_luts.sh runs.
            - aws s3 cp "s3://$OUTPUT_BUCKET/software/CSPP_SDR_V4.1_static_luts_j01.tar.gz" /tmp/j01luts.tgz
            - tar xzf /tmp/j01luts.tgz -C /opt/ && rm -f /tmp/j01luts.tgz
            - test -d "$CSPP_HOME/anc/static/shipped_luts/j01" || { echo "shipped_luts/j01 missing after extract"; exit 1; }
        build:
          commands:
            # Intentional internet fetch -- once per week, not per contact (PERF-1).
            - echo "Fetching LUT DB from jpssdb.ssec.wisc.edu (weekly refresh)"
            - cd "$CSPP_HOME" && timeout 2400 ./bin/sdr_luts.sh > /tmp/luts.log 2>&1; echo "sdr_luts exit $?"
            - tail -10 /tmp/luts.log
            - test -e "$CSPP_HOME/anc/static/SDR_4_1_DB/package" || { echo "LUT DB absent after sdr_luts.sh"; tail -40 /tmp/luts.log; exit 1; }
            # Upload the populated DB tree to the S3 LUT cache prefix.
            # Aggregation jobs stage from here on the fast path.
            - echo "Syncing LUT DB to s3://$OUTPUT_BUCKET/lut-cache/SDR_4_1_DB/"
            - aws s3 sync "$CSPP_HOME/anc/static/SDR_4_1_DB/" "s3://$OUTPUT_BUCKET/lut-cache/SDR_4_1_DB/" --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"
        post_build:
          commands:
            - echo "LUT cache refresh complete -- exit $CODEBUILD_BUILD_SUCCEEDING"
    EOT
  }

  encryption_key = var.kms_key_arn

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.lut_cache_refresh.name
      stream_name = "refresh"
      status      = "ENABLED"
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-lut-cache-refresh"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# EventBridge schedule — weekly trigger for lut_cache_refresh
###############################################################################
#
# Schedule choice: rate(7 days)
#   The CSPP ancillary/calibration LUT DB (jpssdb.ssec.wisc.edu) changes
#   infrequently — calibration updates are versioned releases, not daily feeds.
#   Weekly cadence keeps the cache fresh well within any upstream update cycle.
#   Use cron(0 2 ? * SUN *) instead if a specific maintenance window is needed.

resource "aws_cloudwatch_event_rule" "lut_cache_refresh_schedule" {
  name                = "${var.project_name}-lut-cache-refresh-schedule"
  description         = "Weekly trigger for the CSPP LUT cache refresh job (PERF-1)"
  schedule_expression = "rate(7 days)"

  tags = merge(var.tags, {
    Name    = "${var.project_name}-lut-cache-refresh-schedule"
    Service = "sdr-pipeline"
  })
}

resource "aws_cloudwatch_event_target" "lut_cache_refresh_schedule" {
  rule     = aws_cloudwatch_event_rule.lut_cache_refresh_schedule.name
  arn      = aws_codebuild_project.lut_cache_refresh.arn
  role_arn = aws_iam_role.eventbridge_lut_refresh.arn
}

###############################################################################
# IAM role — EventBridge → CodeBuild (lut_cache_refresh only)
###############################################################################
#
# Mirrors the pattern of aws_iam_role.eventbridge (iam.tf) but targets
# CodeBuild instead of Step Functions.
# codebuild:StartBuild is scoped to the single lut_cache_refresh project ARN —
# no wildcard on the action and no wildcard on the resource.

resource "aws_iam_role" "eventbridge_lut_refresh" {
  name        = "${var.project_name}-eb-lut-refresh"
  description = "Allows EventBridge to start the LUT cache refresh CodeBuild project only (PERF-1)"

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
    Name    = "${var.project_name}-eb-lut-refresh"
    Service = "sdr-pipeline"
  })
}

resource "aws_iam_role_policy" "eventbridge_lut_refresh" {
  name = "${var.project_name}-eb-lut-refresh-policy"
  role = aws_iam_role.eventbridge_lut_refresh.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Scoped to the single lut_cache_refresh project ARN — no wildcard
      {
        Sid      = "StartLUTCacheRefresh"
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild"]
        Resource = aws_codebuild_project.lut_cache_refresh.arn
      },
    ]
  })
}
