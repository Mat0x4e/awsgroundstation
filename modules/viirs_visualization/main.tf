# main.tf — Lambda orchestrator function and CloudWatch log groups
#
# Resources:
#   - data.archive_file.viirs_orchestrator     — packages Lambda source into a zip
#   - aws_lambda_function.viirs_orchestrator   — orchestrator (Python 3.12, 512 MB, 60s)
#   - aws_cloudwatch_log_group.lambda          — /aws/lambda/…, 90-day retention, KMS
#   - aws_cloudwatch_log_group.codebuild       — /aws/codebuild/…, 90-day retention, KMS
#
# Satisfies: Requirements 11.3, 11.5

# ─────────────────────────────────────────────
# Lambda deployment package
# ─────────────────────────────────────────────

data "archive_file" "viirs_orchestrator" {
  type        = "zip"
  source_dir  = "${path.module}/../../lambdas/viirs_visualizer"
  output_path = "${path.module}/../../.build/viirs_orchestrator.zip"
}

# ─────────────────────────────────────────────
# Lambda function
# ─────────────────────────────────────────────

resource "aws_lambda_function" "viirs_orchestrator" {
  function_name = "${var.project_name}-viirs-orchestrator"
  description   = "VIIRS visualization orchestrator — detects path and submits CodeBuild job"

  filename         = data.archive_file.viirs_orchestrator.output_path
  source_code_hash = data.archive_file.viirs_orchestrator.output_base64sha256

  runtime = "python3.12"
  handler = "handler.lambda_handler"

  memory_size = 512
  timeout     = 60

  role = aws_iam_role.lambda_execution.arn

  environment {
    variables = {
      INPUT_BUCKET      = var.sdr_output_bucket_name
      CODEBUILD_PROJECT = aws_codebuild_project.viirs_visualization.name
      ENABLE_GEOTIFF    = tostring(var.enable_geotiff)
    }
  }

  # Encrypt the deployment package at rest with the project KMS CMK.
  kms_key_arn = var.kms_key_arn

  depends_on = [
    aws_cloudwatch_log_group.lambda,
  ]

  tags = var.tags
}

# ─────────────────────────────────────────────
# CloudWatch Log Groups — 90-day retention, KMS encryption
# ─────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-viirs-orchestrator"
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${var.project_name}-viirs-visualization"
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = var.tags
}
