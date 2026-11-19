# codebuild_image.tf — CodeBuild project for the SDR pipeline Docker image build
#
# Builds the SatDump/RT-STPS/CSPP Docker image from the GitHub repository and
# pushes it to the sdr_pipeline ECR repository. Mirrors the pattern used by
# viirs_visualization/codebuild.tf (viirs_docker_build).
#
# This project (aws_codebuild_project.sdr_docker_build =>
# groundstation-noaa20-sdr-docker-build) builds the SDR Docker image and pushes
# it to the sdr_pipeline ECR repo.
#
# Historical note: an out-of-band project "satdump-noaa20-processing" (and its
# role "codebuild-satdump-role") predated this IaC project. Rather than
# import+rename it, this IaC project was created fresh; the out-of-band project
# became dead once the chunk fan-out moved to AWS Batch and was deleted from the
# account on 2026-10-22. No import is needed or possible now.

# ─────────────────────────────────────────────────────────────────────────────
# CloudWatch log group
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "codebuild_docker_build" {
  name = "/aws/codebuild/${var.project_name}-sdr-docker-build"
  # checkov:skip=CKV_AWS_338: 90-day retention is sufficient for Docker image build logs —
  # the ECR image is the permanent artefact; build logs are debug/audit only
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-docker-build-logs"
    Service = "sdr-pipeline"
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Docker image build project
# ─────────────────────────────────────────────────────────────────────────────
#
# Builds the SDR pipeline Docker image from the GitHub repository, downloads
# vendor build artefacts from the S3 docker-build/ prefix, and pushes the
# image to ECR. Run once after initial deployment and whenever the Dockerfile
# or docker/ scripts change.
#
# Trigger manually via the AWS Console or:
#   aws codebuild start-build --project-name ${var.project_name}-sdr-docker-build
#
# Override the branch for pre-merge testing:
#   aws codebuild start-build \
#     --project-name ${var.project_name}-sdr-docker-build \
#     --environment-variables-override \
#       name=REPO_BRANCH,value=feature/my-branch,type=PLAINTEXT

resource "aws_codebuild_project" "sdr_docker_build" {
  # checkov:skip=CKV_AWS_316: privileged_mode is required for docker build — this project
  # exists solely to build the SDR Docker image; it does not run pipeline workloads
  name          = "${var.project_name}-sdr-docker-build"
  description   = "Builds the SDR pipeline Docker image from GitHub and pushes to ECR"
  build_timeout = 30
  service_role  = aws_iam_role.codebuild_docker_build.arn

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
      name  = "ECR_REPO"
      value = aws_ecr_repository.sdr_pipeline.repository_url
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "S3_BUILD_PREFIX"
      value = "s3://${aws_s3_bucket.sdr_output.id}/docker-build"
      type  = "PLAINTEXT"
    }

    # Branch to build from. Override at start-build to test a change before it
    # reaches main — the image is what runs SatDump, so a Dockerfile change is
    # only real once this project has run against it.
    environment_variable {
      name  = "REPO_BRANCH"
      value = "main"
      type  = "PLAINTEXT"
    }

    environment_variable {
      name  = "ACCOUNT_ID"
      value = var.account_id
      type  = "PLAINTEXT"
    }
  }

  # GITHUB source: the repository is cloned during the build phase (not by CodeBuild's
  # built-in source download). The inline buildspec drives the full build sequence:
  #   pre_build  — ECR login + S3 sync of vendor artefacts (SatDump, RT-STPS, CSPP)
  #   build      — git clone, assemble build context, docker build
  #   post_build — docker push
  #
  # Inline rather than file() to keep env-var references ($ECR_REPO etc.) generic
  # — the equivalent file buildspecs/docker_build_sdr.yml contains hardcoded
  # account-specific values; this version uses project-level env vars instead.
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
            - echo Downloading build artefacts from S3...
            - mkdir -p /tmp/docker-build
            - aws s3 sync $S3_BUILD_PREFIX /tmp/docker-build/
            - echo Downloaded files
            - ls -la /tmp/docker-build/
        build:
          commands:
            - echo Cloning repo for Dockerfile and scripts...
            - git clone --depth 1 --branch "$REPO_BRANCH" https://github.com/Mat0x4e/awsgroundstation.git /tmp/repo
            - echo "Building from branch $REPO_BRANCH at $(git -C /tmp/repo rev-parse --short HEAD)"
            - echo Preparing build context...
            - cp /tmp/repo/docker/sdr-pipeline/* /tmp/docker-build/
            - cp -r /tmp/repo/scripts /tmp/docker-build/scripts
            - echo Building Docker image...
            - docker build -t $ECR_REPO:latest -f /tmp/docker-build/Dockerfile /tmp/docker-build/
        post_build:
          commands:
            - echo Pushing to ECR...
            - docker push $ECR_REPO:latest
            - echo Done - image pushed as $ECR_REPO:latest
    EOT
  }

  source_version = "main"

  # Encrypt build logs with the project KMS CMK.
  encryption_key = var.kms_key_arn

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.codebuild_docker_build.name
      stream_name = "build"
      status      = "ENABLED"
    }

    s3_logs {
      status = "DISABLED"
    }
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-docker-build"
    Service = "sdr-pipeline"
  })
}
