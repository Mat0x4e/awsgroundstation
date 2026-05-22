data "archive_file" "contact_scheduler" {
  type        = "zip"
  source_file = "${path.module}/../../lambdas/contact_scheduler/handler.py"
  output_path = "${path.module}/../../.build/contact_scheduler.zip"
}

resource "aws_lambda_function" "contact_scheduler" {
  function_name    = "${var.project_name}-${var.environment}-contact-scheduler"
  role             = var.lambda_role_arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.contact_scheduler.output_path
  source_code_hash = data.archive_file.contact_scheduler.output_base64sha256

  environment {
    variables = {
      MISSION_PROFILE_ARN       = var.mission_profile_arn
      SATELLITE_ARN             = var.satellite_arn
      SNS_TOPIC_ARN             = var.sns_topic_arn
      MINIMUM_ELEVATION_DEGREES = tostring(var.minimum_elevation_degrees)
    }
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-contact-scheduler"
  })
}

resource "aws_cloudwatch_log_group" "contact_scheduler" {
  name              = "/aws/lambda/${aws_lambda_function.contact_scheduler.function_name}"
  retention_in_days = 90

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-scheduler-logs"
  })
}

resource "aws_cloudwatch_event_rule" "scheduler_cron" {
  name                = "${var.project_name}-${var.environment}-scheduler-cron"
  description         = "Trigger contact scheduler at 06:00 UTC on weekdays"
  schedule_expression = "cron(0 6 ? * MON-FRI *)"

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-scheduler-cron"
  })
}

resource "aws_cloudwatch_event_target" "scheduler_lambda" {
  rule = aws_cloudwatch_event_rule.scheduler_cron.name
  arn  = aws_lambda_function.contact_scheduler.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.contact_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scheduler_cron.arn
}
