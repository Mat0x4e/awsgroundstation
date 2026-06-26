# codebuild.tf — CodeBuild project with dynamic buildspec selection
#
# A single project covers both visualization paths (SatDump and NASA).
# The Lambda orchestrator selects the appropriate buildspec at start_build()
# time via the overrideSpec parameter — no buildspec is hardcoded here.
#
# Source type is NO_SOURCE: the buildspec pre_build phase downloads inputs
# directly from S3, so CodeBuild does not need a source repository.

resource "aws_codebuild_project" "viirs_visualization" {
  name          = "${var.project_name}-viirs-visualization"
  description   = "VIIRS visualization pipeline — SatDump and NASA paths (buildspec selected dynamically by Lambda)"
  build_timeout = 15
  service_role  = aws_iam_role.codebuild_service.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  cache {
    type = "NO_CACHE"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    image                       = aws_ecr_repository.visualization.repository_url
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "SERVICE_ROLE"
    privileged_mode             = false

    # Static environment variables — set at project creation time.
    # Dynamic overrides (INPUT_PREFIX, CONTACT_ID, CONTACT_DATE, VIZ_PATH)
    # are injected by the Lambda at start_build() via environmentVariablesOverride.
    environment_variable {
      name  = "INPUT_BUCKET"
      value = var.sdr_output_bucket_name
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "KMS_KEY_ID"
      value = var.kms_key_id
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "ENABLE_GEOTIFF"
      value = tostring(var.enable_geotiff)
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "ENABLE_DESTRIPE"
      value = tostring(var.enable_destripe)
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "TLE_URL"
      value = var.tle_url
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "TLE_FALLBACK"
      value = var.tle_fallback
      type  = "PLAINTEXT"
    }
  }

  # NO_SOURCE: inputs are downloaded from S3 in the buildspec pre_build phase.
  # The buildspec itself is provided at start_build() time by the Lambda
  # via the overrideSpec field — this placeholder is never used in practice
  # but is required to satisfy the CodeBuild resource schema.
  source {
    type      = "NO_SOURCE"
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - echo 'Buildspec must be provided via overrideSpec at start_build() time'\n      - exit 1\n"
  }

  # Encrypt build artifacts and logs with the project KMS CMK.
  encryption_key = var.kms_key_arn

  logs_config {
    cloudwatch_logs {
      group_name  = "/aws/codebuild/${var.project_name}-viirs-visualization"
      stream_name = "build"
      status      = "ENABLED"
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  tags = var.tags
}
