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

# ─────────────────────────────────────────────
# Docker image build project
# ─────────────────────────────────────────────
#
# Builds the viirs-visualization Docker image from the GitHub repository
# and pushes it to ECR. Run this project once after initial deployment,
# and whenever the Dockerfile or scripts/ change.
#
# Trigger manually via the AWS Console or:
#   aws codebuild start-build --project-name <project-name>-viirs-docker-build

resource "aws_codebuild_project" "viirs_docker_build" {
  name          = "${var.project_name}-viirs-docker-build"
  description   = "Builds the viirs-visualization Docker image from GitHub and pushes to ECR"
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
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true # required for docker build

    environment_variable {
      name  = "ECR_REPO_URL"
      value = aws_ecr_repository.visualization.repository_url
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.account_id
      type  = "PLAINTEXT"
    }
  }

  source {
    type            = "GITHUB"
    location        = "https://github.com/Mat0x4e/awsgroundstation.git"
    git_clone_depth = 1
    buildspec       = <<-EOT
      version: 0.2
      phases:
        pre_build:
          commands:
            - echo Logging in to ECR...
            - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com
        build:
          commands:
            - echo Building Docker image...
            - docker build -t $ECR_REPO_URL:latest -f docker/viirs-visualization/Dockerfile .
        post_build:
          commands:
            - echo Pushing to ECR...
            - docker push $ECR_REPO_URL:latest
            - echo Done.
    EOT
  }

  source_version = "main"

  # Encrypt build logs with the project KMS CMK.
  encryption_key = var.kms_key_arn

  logs_config {
    cloudwatch_logs {
      group_name  = "/aws/codebuild/${var.project_name}-viirs-docker-build"
      stream_name = "build"
      status      = "ENABLED"
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  tags = var.tags
}
