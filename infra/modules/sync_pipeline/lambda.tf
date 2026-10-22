# lambda.tf — Receiver Arm Trigger Lambda, IAM role, and CloudWatch Log Group
#
# Unlike modules/sdr_pipeline's Trigger Lambda (invoked post-hoc by Step
# Functions once data already sits in S3), this Lambda is invoked directly by
# an EventBridge PREPASS rule (see eventbridge.tf) BEFORE AOS: it starts the
# receiver EC2 if stopped, waits for running state, then issues an SSM Run
# Command that arms the live-UDP listener in the background so it's ready the
# moment Ground Station starts streaming. There is no S3 buffer in this path,
# so a late start loses the pass outright.
#
# The corresponding "finish" step (stop the UDP listener, run RT-STPS, upload
# RDR to S3) is invoked directly by Step Functions via SSM SDK integration
# (see step_functions.tf and iam.tf's sfn role) rather than through a second
# Lambda, since Step Functions can call ssm:sendCommand natively.

###############################################################################
# Package the Lambda handler into a zip archive
###############################################################################

data "archive_file" "receiver_arm" {
  type        = "zip"
  source_dir  = "${path.module}/../../../lambdas/receiver_arm"
  output_path = "${path.module}/../../../.build/receiver_arm.zip"
}

###############################################################################
# IAM Role for the Receiver Arm Lambda
###############################################################################

resource "aws_iam_role" "receiver_arm_lambda" {
  name        = "${var.project_name}-sync-receiver-arm"
  description = "Allows the Receiver Arm Lambda to start the receiver EC2 instance and issue SSM commands"

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
    Name    = "${var.project_name}-sync-receiver-arm"
    Service = "sync-pipeline"
  })
}

resource "aws_iam_role_policy" "receiver_arm_lambda" {
  name = "${var.project_name}-sync-receiver-arm-policy"
  role = aws_iam_role.receiver_arm_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # EC2 — start and describe the receiver instance
      {
        Sid      = "EC2StartInstance"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances"]
        Resource = "arn:aws:ec2:${data.aws_region.current.id}:${var.account_id}:instance/${aws_instance.receiver.id}"
      },
      # ec2:DescribeInstances does not support resource-level scoping
      # checkov:skip=CKV_AWS_355: ec2:DescribeInstances cannot be scoped to a specific resource ARN
      {
        Sid      = "EC2DescribeInstances"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      # SSM — send commands to the receiver instance only
      {
        Sid    = "SSMSendCommand"
        Effect = "Allow"
        Action = ["ssm:SendCommand"]
        Resource = [
          "arn:aws:ec2:${data.aws_region.current.id}:${var.account_id}:instance/${aws_instance.receiver.id}",
          "arn:aws:ssm:${data.aws_region.current.id}::document/AWS-RunShellScript",
        ]
      },
      # ssm:DescribeInstanceInformation does not support resource-level
      # scoping -- used by _wait_for_ssm_online to confirm the SSM agent has
      # registered before arming, since EC2 "running" alone isn't sufficient.
      # checkov:skip=CKV_AWS_355: ssm:DescribeInstanceInformation cannot be scoped to a specific resource ARN
      {
        Sid      = "SSMDescribeInstanceInformation"
        Effect   = "Allow"
        Action   = ["ssm:DescribeInstanceInformation"]
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
        Resource = "${aws_cloudwatch_log_group.receiver_arm.arn}:*"
      },
    ]
  })
}

###############################################################################
# CloudWatch Log Group — 14-day retention
###############################################################################

resource "aws_cloudwatch_log_group" "receiver_arm" {
  name              = "/aws/lambda/${var.project_name}-sync-receiver-arm"
  retention_in_days = 14

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-arm"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Lambda Function
###############################################################################

resource "aws_lambda_function" "receiver_arm" {
  function_name = "${var.project_name}-sync-receiver-arm"
  description   = "Starts the sync receiver EC2 and arms its live-UDP listener before AOS"

  filename         = data.archive_file.receiver_arm.output_path
  source_code_hash = data.archive_file.receiver_arm.output_base64sha256

  role        = aws_iam_role.receiver_arm_lambda.arn
  runtime     = "python3.12"
  handler     = "handler.lambda_handler"
  memory_size = 256

  # 420s to cover the handler's worst-case polling budget: up to 240s
  # waiting for EC2 running state, plus up to 120s waiting for the SSM agent
  # to register Online, plus margin for send_command -- see
  # variables.tf's contact_pre_pass_duration_seconds for the matching
  # pre-pass lead time this is sized against.
  timeout = 420

  environment {
    variables = {
      RECEIVER_INSTANCE_ID = aws_instance.receiver.id
      RECEIVER_UDP_PORT    = tostring(var.receiver_udp_port)
    }
  }

  depends_on = [aws_cloudwatch_log_group.receiver_arm]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-arm"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Resource-based permission -- allows the eventbridge.tf PREPASS rule to
# invoke this Lambda directly, without a dedicated IAM role (EventBridge ->
# Lambda targets use a Lambda resource policy, not an assumed role).
###############################################################################

resource "aws_lambda_permission" "receiver_arm_eventbridge" {
  statement_id  = "AllowEventBridgePrepass"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.receiver_arm.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.contact_prepass.arn
}
