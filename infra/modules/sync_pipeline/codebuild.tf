# codebuild.tf — CodeBuild project for the sync pipeline's CSPP-only
# aggregation build.
#
# Unlike modules/sdr_pipeline's aggregation build (which runs BOTH RT-STPS and
# CSPP inline -- see scripts/aggregation.sh), this project starts from an RDR
# that the receiver EC2 already produced (scripts/sync_receiver_finish.sh runs
# RT-STPS locally, right after LOS, with PnEncoded="true" kept since the
# demod/decode UncodedFramesEgress output is not PN-derandomized). This build
# only needs CSPP's internet-dependent LUT staging step, same reasoning as the
# existing pipeline: CSPP's sdr_luts.sh fetch needs egress the receiver EC2's
# security group deliberately does not have (UDP ingress / HTTPS-SSM egress
# only -- see ec2.tf).
#
# Reuses the SAME ECR image as modules/sdr_pipeline (var.sdr_pipeline_ecr_repository_url)
# rather than building a second one -- it already carries CSPP 4.1.1, the J01
# straylight LUTs, and RT-STPS (unused here, but harmless).

resource "aws_cloudwatch_log_group" "codebuild" {
  name = "/aws/codebuild/${var.project_name}-sync-aggregation"
  # checkov:skip=CKV_AWS_338: 90-day retention is sufficient for pipeline debug logs —
  # satellite contact data is the permanent record (stored in S3 with lifecycle policies)
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-aggregation-logs"
    Service = "sync-pipeline"
  })
}

resource "aws_codebuild_project" "sync_aggregation" {
  name         = "${var.project_name}-sync-aggregation"
  description  = "Synchronous pipeline CSPP-only aggregation — RDR (from receiver EC2) to SDR+GEO"
  service_role = aws_iam_role.codebuild.arn

  # Matches modules/sdr_pipeline's aggregation timeout: sdr_luts.sh alone can
  # run ~10 min, CSPP viirs_sdr.sh itself can take 60-90 min for a full pass.
  build_timeout  = 180
  queued_timeout = 30

  source {
    type      = "NO_SOURCE"
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - echo \"Buildspec overridden by Step Functions at runtime\"\n"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_2XLARGE"
    image                       = "${var.sdr_pipeline_ecr_repository_url}:latest"
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
    Name    = "${var.project_name}-sync-aggregation"
    Service = "sync-pipeline"
  })
}
