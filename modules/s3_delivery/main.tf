data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# S3 bucket for CADU reception data
resource "aws_s3_bucket" "reception" {
  bucket = "${var.project_name}-${var.environment}-reception-${data.aws_caller_identity.current.account_id}"

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-reception"
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reception" {
  bucket = aws_s3_bucket.reception.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "reception" {
  bucket = aws_s3_bucket.reception.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "reception" {
  bucket = aws_s3_bucket.reception.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reception" {
  bucket = aws_s3_bucket.reception.id

  rule {
    id     = "transition-to-glacier-deep-archive"
    status = "Enabled"

    filter {}

    transition {
      days          = 30
      storage_class = "DEEP_ARCHIVE"
    }
  }
}

resource "aws_s3_bucket_policy" "reception" {
  bucket = aws_s3_bucket.reception.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowGroundStationPutObject"
        Effect = "Allow"
        Principal = {
          Service = "groundstation.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.reception.arn}/year=*/month=*/day=*/contact-id=*/*"
      },
      {
        Sid    = "AllowGroundStationGetBucketLocation"
        Effect = "Allow"
        Principal = {
          Service = "groundstation.amazonaws.com"
        }
        Action   = "s3:GetBucketLocation"
        Resource = aws_s3_bucket.reception.arn
      }
    ]
  })
}

# Server access logging bucket
resource "aws_s3_bucket" "logging" {
  bucket = "${var.project_name}-${var.environment}-access-logs-${data.aws_caller_identity.current.account_id}"

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-access-logs"
  })
}

resource "aws_s3_bucket_public_access_block" "logging" {
  bucket = aws_s3_bucket.logging.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logging" {
  bucket = aws_s3_bucket.logging.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logging" {
  bucket = aws_s3_bucket.logging.id

  rule {
    id     = "expire-old-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }
  }
}

resource "aws_s3_bucket_logging" "reception" {
  bucket = aws_s3_bucket.reception.id

  target_bucket = aws_s3_bucket.logging.id
  target_prefix = "reception-access-logs/"
}

# CloudWatch alarm monitoring S3 bucket errors
resource "aws_cloudwatch_metric_alarm" "s3_errors" {
  alarm_name          = "${var.project_name}-${var.environment}-s3-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "4xxErrors"
  namespace           = "AWS/S3"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "Alert when S3 bucket experiences elevated 4xx errors"
  treat_missing_data  = "notBreaching"

  dimensions = {
    BucketName = aws_s3_bucket.reception.id
    FilterId   = "AllMetrics"
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-s3-errors"
  })
}
