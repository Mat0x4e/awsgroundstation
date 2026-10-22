# codebuild_image.tf — CodeBuild project for the SDR pipeline Docker image build
#
# Builds the SatDump/RT-STPS/CSPP Docker image from the GitHub repository and
# pushes it to the sdr_pipeline ECR repository. Mirrors the pattern used by
# viirs_visualization/codebuild.tf (viirs_docker_build).
#
# Requirement 1 (OE-1 / SEC-3): codifies the out-of-band "satdump-noaa20-processing"
# project so that terraform apply can create and repair it.
#
# ─── IMPORT — adopting the existing deployed project (run once) ──────────────
#
# (a) ADOPTION: This resource codifies the project previously created out-of-band
#     as "satdump-noaa20-processing". Importing it brings the existing project
#     under Terraform management — no parallel project is created alongside it.
#
# (b) FIRST-APPLY REPLACEMENT — EXPECTED, ONE-TIME: CodeBuild project names are
#     immutable. Renaming from "satdump-noaa20-processing" to the IaC-convention
#     name "${var.project_name}-sdr-docker-build" (= "groundstation-noaa20-sdr-
#     docker-build") forces destroy+recreate on the first apply after import.
#     This is the accepted outcome — the old project is torn down and the new one
#     is created; the project is immediately usable afterwards.
#
# (c) REPLACEMENT IS SAFE: The project has NO_ARTIFACTS and an idempotent purpose
#     — it only clones the GitHub repo, builds the SDR Docker image, and pushes
#     it to ECR. No build history, artifact archives, or caches are stored in the
#     project itself. The ECR image is the sole durable artefact; it is completely
#     unaffected by the rename. Running the project once after recreate restores
#     full functionality.
#
# (d) NOTHING IS LOST: The hand-added S3 docker-build/* read that existed on the
#     out-of-band "satdump-noaa20-processing" role is fully codified in iam.tf
#     (aws_iam_role_policy.codebuild_docker_build, Sids S3GetDockerBuildArtifacts
#     and S3ListDockerBuildPrefix). The new role is created before the project is
#     destroyed; no access gap occurs during the replacement.
#
# ── How to import ─────────────────────────────────────────────────────────────
#
# Option A (preferred — TF ≥ 1.5 native import block; uncomment the block below):
#   1. Uncomment the `import { ... }` block below.
#   2. terraform -chdir=infra plan
#      → expect: 1 to import (satdump-noaa20-processing) + 1 to replace
#   3. terraform -chdir=infra apply   ← imports AND replaces in one step
#   4. Re-comment the import block after apply completes (keeps the file clean).
#
# Option B (CLI — equivalent result):
#   terraform -chdir=infra import \
#     'module.sdr_pipeline[0].aws_codebuild_project.sdr_docker_build' \
#     satdump-noaa20-processing
#   Then: terraform -chdir=infra plan / apply.
#
# ─────────────────────────────────────────────────────────────────────────────

# import {
#   to = aws_codebuild_project.sdr_docker_build
#   id = "satdump-noaa20-processing"
# }

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
