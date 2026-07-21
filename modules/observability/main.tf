data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# SNS topic for contact failure notifications
resource "aws_sns_topic" "contact_failures" {
  name              = "${var.project_name}-${var.environment}-contact-failures"
  kms_master_key_id = var.kms_key_id

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-contact-failures"
  })
}

# EventBridge rule for Ground Station contact state changes (FAILED, FAILED_TO_SCHEDULE)
resource "aws_cloudwatch_event_rule" "contact_state_change" {
  name        = "${var.project_name}-${var.environment}-contact-state-change"
  description = "Capture Ground Station contact state changes for failures"

  event_pattern = jsonencode({
    source      = ["aws.groundstation"]
    detail-type = ["Ground Station Contact State Change"]
    detail = {
      contactStatus = ["FAILED", "FAILED_TO_SCHEDULE"]
    }
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-contact-state-change"
  })
}

resource "aws_cloudwatch_event_target" "contact_failure_sns" {
  rule = aws_cloudwatch_event_rule.contact_state_change.name
  arn  = aws_sns_topic.contact_failures.arn
}

# SNS topic policy to allow EventBridge and S3 to publish
resource "aws_sns_topic_policy" "contact_failures" {
  arn = aws_sns_topic.contact_failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEventBridgePublish"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.contact_failures.arn
      },
      {
        Sid    = "AllowS3Publish"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.contact_failures.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:s3:::aws-groundstation-*"
          }
        }
      }
    ]
  })
}

# CloudWatch metric alarm for FAILED contact status
resource "aws_cloudwatch_metric_alarm" "contact_failed" {
  alarm_name          = "${var.project_name}-${var.environment}-contact-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ContactStatus"
  namespace           = "AWS/GroundStation"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Alert when a Ground Station contact fails"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Status = "FAILED"
  }

  alarm_actions = [aws_sns_topic.contact_failures.arn]

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-contact-failed-alarm"
  })
}

# CloudWatch metric alarm for FAILED_TO_SCHEDULE contact status
resource "aws_cloudwatch_metric_alarm" "contact_failed_to_schedule" {
  alarm_name          = "${var.project_name}-${var.environment}-contact-failed-to-schedule"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ContactStatus"
  namespace           = "AWS/GroundStation"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Alert when a Ground Station contact fails to schedule"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Status = "FAILED_TO_SCHEDULE"
  }

  alarm_actions = [aws_sns_topic.contact_failures.arn]

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-contact-failed-to-schedule-alarm"
  })
}

# CloudWatch Log Group for observability
resource "aws_cloudwatch_log_group" "observability" {
  name              = "/groundstation/${var.project_name}-${var.environment}/observability"
  retention_in_days = 90

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-observability-logs"
  })
}

# CloudWatch Dashboard
resource "aws_cloudwatch_dashboard" "groundstation" {
  dashboard_name = "${var.project_name}-${var.environment}-dashboard"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Contacts Planned"
          metrics = [["AWS/GroundStation", "ContactStatus", "Status", "SCHEDULED"]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Contacts Active"
          metrics = [["AWS/GroundStation", "ContactStatus", "Status", "PASS"]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Contacts Completed"
          metrics = [["AWS/GroundStation", "ContactStatus", "Status", "COMPLETED"]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Contacts Failed"
          metrics = [["AWS/GroundStation", "ContactStatus", "Status", "FAILED"]]
          period  = 300
          stat    = "Sum"
          region  = data.aws_region.current.id
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Data Volume Received"
          metrics = [["AWS/S3", "BucketSizeBytes", "BucketName", var.reception_bucket_name, "StorageType", "StandardStorage"]]
          period  = 86400
          stat    = "Average"
          region  = data.aws_region.current.id
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title = "Contact Success Rate"
          metrics = [
            ["AWS/GroundStation", "ContactStatus", "Status", "COMPLETED"],
            ["AWS/GroundStation", "ContactStatus", "Status", "FAILED"]
          ]
          period = 3600
          stat   = "Sum"
          region = data.aws_region.current.id
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 18
        width  = 24
        height = 6
        properties = {
          title = "Estimated Cost (USD)"
          metrics = [
            [{ "expression" = "m1 * 100", "label" = "Estimated Cost (USD) — ~$100/contact (10 min × $10/min)", "id" = "cost" }],
            ["AWS/GroundStation", "ContactStatus", "Status", "COMPLETED", { "id" = "m1", "visible" = false, "stat" = "Sum" }]
          ]
          period = 86400
          stat   = "Sum"
          region = data.aws_region.current.id
          view   = "timeSeries"
          yAxis = {
            left = {
              label     = "USD"
              showUnits = false
            }
          }
        }
      }
    ]
  })
}


# EventBridge rule for Ground Station contact COMPLETED (notify operator when data arrives)
resource "aws_cloudwatch_event_rule" "contact_completed" {
  name        = "${var.project_name}-${var.environment}-contact-completed"
  description = "Notify when a Ground Station contact completes successfully"

  event_pattern = jsonencode({
    source      = ["aws.groundstation"]
    detail-type = ["Ground Station Contact State Change"]
    detail = {
      contactStatus = ["COMPLETED"]
    }
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-contact-completed"
  })
}

resource "aws_cloudwatch_event_target" "contact_completed_sns" {
  rule = aws_cloudwatch_event_rule.contact_completed.name
  arn  = aws_sns_topic.contact_failures.arn

  input_transformer {
    input_paths = {
      contactId     = "$.detail.contactId"
      status        = "$.detail.contactStatus"
      groundStation = "$.detail.groundStation"
    }
    input_template = "\"Ground Station contact <contactId> COMPLETED on <groundStation>. Data has been delivered to S3.\""
  }
}

# S3 event notification for new objects in the reception bucket → SNS
resource "aws_s3_bucket_notification" "reception_notify" {
  bucket = var.reception_bucket_name

  topic {
    topic_arn     = aws_sns_topic.contact_failures.arn
    events        = ["s3:ObjectCreated:*"]
    filter_suffix = ".pcap"
  }

  depends_on = [aws_sns_topic_policy.contact_failures]
}
