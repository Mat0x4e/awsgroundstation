data "aws_caller_identity" "current" {}

# S3 output bucket for processed data
resource "aws_s3_bucket" "output" {
  bucket = "${var.project_name}-${var.environment}-output-${data.aws_caller_identity.current.account_id}"

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-output"
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "output" {
  bucket = aws_s3_bucket.output.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "output" {
  bucket = aws_s3_bucket.output.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SQS Dead Letter Queue
resource "aws_sqs_queue" "dlq" {
  name              = "${var.project_name}-${var.environment}-processor-dlq"
  kms_master_key_id = var.kms_key_id

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-processor-dlq"
  })
}

# SQS Queue for S3 notifications
resource "aws_sqs_queue" "processing" {
  name                       = "${var.project_name}-${var.environment}-processing"
  kms_master_key_id          = var.kms_key_id
  visibility_timeout_seconds = 360

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-processing"
  })
}

# SQS Queue policy to allow S3 to send notifications
resource "aws_sqs_queue_policy" "processing" {
  queue_url = aws_sqs_queue.processing.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowS3Notification"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.processing.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = var.reception_bucket_arn
          }
        }
      }
    ]
  })
}

# S3 bucket notification to SQS for .cadu objects -- REMOVED DELIBERATELY.
#
# S3 supports one notification configuration per bucket, and
# aws_s3_bucket_notification manages the whole thing. This resource and
# modules/observability/main.tf both targeted the reception bucket, so whichever
# applied last silently erased the other. That is how the bucket ended up with
# only an SNS topic and no eventbridge = true, leaving the SDR pipeline with no
# trigger at all (contact ba2c5446, 2026-08-31).
#
# The single owner is now aws_s3_bucket_notification.reception_notify in
# modules/observability. If this pipeline is ever re-enabled
# (enable_processing_pipeline = true, currently false), add a queue block THERE
# and pass aws_sqs_queue.processing.arn in from the root module -- do not
# reintroduce a second resource for this bucket.

# Lambda function for data processing
data "archive_file" "data_processor" {
  type        = "zip"
  source_file = "${path.module}/../../../lambdas/data_processor/handler.py"
  output_path = "${path.module}/../../../.build/data_processor.zip"
}

resource "aws_lambda_function" "data_processor" {
  function_name    = "${var.project_name}-${var.environment}-data-processor"
  role             = var.lambda_role_arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 300
  memory_size      = 512
  filename         = data.archive_file.data_processor.output_path
  source_code_hash = data.archive_file.data_processor.output_base64sha256

  environment {
    variables = {
      OUTPUT_BUCKET_NAME = aws_s3_bucket.output.id
      PROJECT_NAME       = var.project_name
    }
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-data-processor"
  })
}

resource "aws_cloudwatch_log_group" "data_processor" {
  name              = "/aws/lambda/${aws_lambda_function.data_processor.function_name}"
  retention_in_days = 90

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-processor-logs"
  })
}

# Lambda event source mapping from SQS
resource "aws_lambda_event_source_mapping" "sqs_to_lambda" {
  event_source_arn = aws_sqs_queue.processing.arn
  function_name    = aws_lambda_function.data_processor.arn
  batch_size       = 10
  enabled          = true
}

# SNS topic for failure notifications
resource "aws_sns_topic" "processing_failures" {
  name              = "${var.project_name}-${var.environment}-processing-failures"
  kms_master_key_id = var.kms_key_id

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-processing-failures"
  })
}

# CloudWatch alarm on DLQ messages visible
resource "aws_cloudwatch_metric_alarm" "dlq_messages" {
  alarm_name          = "${var.project_name}-${var.environment}-dlq-messages"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Alert when messages appear in the processing DLQ"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }

  alarm_actions = [aws_sns_topic.processing_failures.arn]

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-dlq-alarm"
  })
}
