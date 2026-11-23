# network_canary.tf - TEMPORARY network canary Lambda (see enable_network_canary)
#
# TEMPORARY BY DESIGN: this Lambda exists only to validate the Ground Station
# dataflow ENI -> receiver UDP path before a paid contact. It runs IN the
# receiver's subnet (same subnet as the dataflow endpoint) WITH the receiver
# security group, so its datagrams follow the same rules as the Ground Station
# ENI. It therefore depends on the self-referencing UDP egress/ingress rules on
# aws_security_group.receiver: the canary validates exactly that rule.
# Remove it (set enable_network_canary = false) once the path is proven.
#
# Every resource uses count on var.enable_network_canary. The function needs
# no internet access: it only sends UDP to the receiver's private IP.

###############################################################################
# Package the handler
###############################################################################

data "archive_file" "network_canary" {
  count = var.enable_network_canary ? 1 : 0

  type        = "zip"
  source_dir  = "${path.module}/../../../lambdas/network_canary"
  output_path = "${path.module}/../../../.build/network_canary.zip"

  # Fixed mode and no bytecode keep the zip hash stable across runners.
  output_file_mode = "0644"
  excludes         = ["__pycache__", "__pycache__/*"]
}

###############################################################################
# IAM role: trust for Lambda, VPC ENI management, CloudWatch Logs
###############################################################################

resource "aws_iam_role" "network_canary" {
  count = var.enable_network_canary ? 1 : 0

  name        = "${var.project_name}-sync-network-canary"
  description = "Temporary network canary Lambda: sends test UDP datagrams to the receiver ENI"

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
    Name    = "${var.project_name}-sync-network-canary"
    Service = "sync-pipeline"
  })
}

# AWS-managed policy: ec2 network interface create/describe/delete (needed by
# VPC-attached Lambdas; these actions cannot be resource-scoped) plus logs.
resource "aws_iam_role_policy_attachment" "network_canary_vpc" {
  count = var.enable_network_canary ? 1 : 0

  role       = aws_iam_role.network_canary[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

###############################################################################
# CloudWatch Log Group
###############################################################################

# checkov:skip=CKV_AWS_158: temporary canary logs hold no sensitive data; matches the other sync log groups (no CMK, the key policy is not opened to CloudWatch Logs)
resource "aws_cloudwatch_log_group" "network_canary" {
  count = var.enable_network_canary ? 1 : 0

  name              = "/aws/lambda/${var.project_name}-sync-network-canary"
  retention_in_days = 14

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-network-canary"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Lambda function (in the receiver subnet, with the receiver security group)
###############################################################################

# checkov:skip=CKV_AWS_115: temporary canary, no reserved concurrency needed (invoked manually a few times)
# checkov:skip=CKV_AWS_116: temporary canary invoked synchronously by hand; a DLQ adds nothing
# checkov:skip=CKV_AWS_50: X-Ray tracing not needed for a temporary UDP send test
# checkov:skip=CKV_AWS_272: code signing not warranted for a temporary canary
# checkov:skip=CKV_AWS_173: environment variables hold only a private IP and a port, no secrets
resource "aws_lambda_function" "network_canary" {
  count = var.enable_network_canary ? 1 : 0

  function_name = "${var.project_name}-sync-network-canary"
  description   = "Temporary canary: sends test UDP datagrams to the receiver ENI from its own subnet and SG"

  filename         = data.archive_file.network_canary[0].output_path
  source_code_hash = data.archive_file.network_canary[0].output_base64sha256

  role        = aws_iam_role.network_canary[0].arn
  runtime     = "python3.12"
  handler     = "handler.handler"
  memory_size = 128
  timeout     = 60

  # Same subnet and security group as the receiver ENI, so the traffic mirrors
  # the Ground Station dataflow endpoint ENI path.
  vpc_config {
    subnet_ids         = [data.aws_subnets.default.ids[0]]
    security_group_ids = [aws_security_group.receiver.id]
  }

  environment {
    variables = {
      CANARY_TARGET_IP = aws_network_interface.receiver.private_ip
      CANARY_PORT      = tostring(var.receiver_udp_port)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.network_canary,
    aws_iam_role_policy_attachment.network_canary_vpc,
  ]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-network-canary"
    Service = "sync-pipeline"
  })
}
