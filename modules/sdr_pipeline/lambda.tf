# lambda.tf — Aggregation Trigger Lambda, IAM role, and CloudWatch Log Group
# Implements Requirements 3.2, 3.3, and 7.2 of the ec2-aggregation spec.
#
# The Trigger Lambda starts the aggregation EC2 instance, waits for running
# state, and issues an SSM Run Command to begin the aggregation script.
# Step Functions invokes this Lambda as the FinalAggregation state.

###############################################################################
# Package the Lambda handler into a zip archive
###############################################################################

data "archive_file" "aggregation_trigger" {
  type        = "zip"
  source_dir  = "${path.module}/../../lambdas/aggregation_trigger"
  output_path = "${path.module}/../../.build/aggregation_trigger.zip"
}

###############################################################################
# IAM Role for the Trigger Lambda (task 1.2)
###############################################################################

resource "aws_iam_role" "aggregation_trigger_lambda" {
  name        = "${var.project_name}-aggregation-trigger"
  description = "Allows the Trigger Lambda to start the aggregation EC2 instance and issue SSM commands"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-trigger"
    Service = "sdr-pipeline"
  })
}

# Least-privilege policy: EC2 start/describe + SSM send/get scoped to the instance
resource "aws_iam_role_policy" "aggregation_trigger_lambda" {
  name = "${var.project_name}-aggregation-trigger-policy"
  role = aws_iam_role.aggregation_trigger_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # EC2 — start and describe the aggregation instance
      {
        Sid      = "EC2StartInstance"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances"]
        Resource = "arn:aws:ec2:${data.aws_region.current.id}:${var.account_id}:instance/${aws_instance.aggregation.id}"
      },
      # ec2:DescribeInstances does not support resource-level scoping
      # checkov:skip=CKV_AWS_355: ec2:DescribeInstances cannot be scoped to a specific resource ARN
      {
        Sid      = "EC2DescribeInstances"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      # SSM — send commands to the aggregation instance only
      {
        Sid    = "SSMSendCommand"
        Effect = "Allow"
        Action = ["ssm:SendCommand"]
        Resource = [
          "arn:aws:ec2:${data.aws_region.current.id}:${var.account_id}:instance/${aws_instance.aggregation.id}",
          "arn:aws:ssm:${data.aws_region.current.id}::document/AWS-RunShellScript",
        ]
      },
      # SSM — poll command status; GetCommandInvocation does not support resource-level scoping
      # checkov:skip=CKV_AWS_355: ssm:GetCommandInvocation cannot be scoped to a specific resource ARN
      {
        Sid      = "SSMGetCommandInvocation"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
      # CloudWatch Logs — write Lambda execution logs
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.aggregation_trigger.arn}:*"
      },
    ]
  })
}

###############################################################################
# CloudWatch Log Group — 14-day retention
###############################################################################

resource "aws_cloudwatch_log_group" "aggregation_trigger" {
  name              = "/aws/lambda/${var.project_name}-aggregation-trigger"
  retention_in_days = 14

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-trigger"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# Lambda Function
###############################################################################

resource "aws_lambda_function" "aggregation_trigger" {
  function_name = "${var.project_name}-aggregation-trigger"
  description   = "Starts EC2 aggregation instance and issues SSM Run Command for NOAA-20 SDR pipeline"

  filename         = data.archive_file.aggregation_trigger.output_path
  source_code_hash = data.archive_file.aggregation_trigger.output_base64sha256

  role        = aws_iam_role.aggregation_trigger_lambda.arn
  runtime     = "python3.12"
  handler     = "handler.lambda_handler"
  memory_size = 256
  timeout     = 120

  environment {
    variables = {
      AGGREGATION_INSTANCE_ID = aws_instance.aggregation.id
    }
  }

  # Ensure log group exists before Lambda creates its first log stream
  depends_on = [aws_cloudwatch_log_group.aggregation_trigger]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-trigger"
    Service = "sdr-pipeline"
  })
}
