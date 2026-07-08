# -----------------------------------------------------------------------------
# CodeBuild Project — SDR Pipeline Chunk Processing
# Requirements: 7.3, 7.4
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "codebuild" {
  name = "/aws/codebuild/${var.project_name}-sdr-pipeline"
  # checkov:skip=CKV_AWS_338: 90-day retention is sufficient for pipeline debug logs —
  # satellite contact data is the permanent record (stored in S3 with lifecycle policies)
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-pipeline-logs"
    Service = "sdr-pipeline"
  })
}

resource "aws_codebuild_project" "sdr_pipeline" {
  name           = "${var.project_name}-sdr-pipeline"
  description    = "SDR pipeline chunk processing — DigIF to SDR+GEO via SatDump, RT-STPS, CSPP"
  service_role   = aws_iam_role.codebuild.arn
  build_timeout  = 90
  queued_timeout = 30

  source {
    type      = "NO_SOURCE"
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - echo \"Buildspec overridden by Step Functions at runtime\"\n"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_LARGE"
    image                       = "${aws_ecr_repository.sdr_pipeline.repository_url}:latest"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "SERVICE_ROLE"
    privileged_mode             = false
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
      status     = "ENABLED"
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-pipeline"
    Service = "sdr-pipeline"
  })
}
