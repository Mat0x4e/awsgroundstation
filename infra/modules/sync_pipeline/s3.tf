# ─────────────────────────────────────────────────────────────
# Logging bucket (SSE-S3 — S3 access logging does not support KMS)
# ─────────────────────────────────────────────────────────────

# checkov:skip=CKV2_AWS_62: Event notifications not needed on this bucket —
# this pipeline is triggered by Ground Station contact-state EventBridge
# events and direct Step Functions invocation, not S3 events (see eventbridge.tf)
# checkov:skip=CKV_AWS_144: Single-region deployment by design — satellite data
# is processed in eu-central-1 only; cross-region replication adds unnecessary cost
# checkov:skip=CKV_AWS_145: S3 access logging does not support KMS-SSE as destination
# encryption — AES256 (SSE-S3) is the required encryption for log delivery buckets
# checkov:skip=CKV_AWS_21: Versioning not needed on access logs bucket —
# logs are append-only and expire after 90 days via lifecycle rule
resource "aws_s3_bucket" "sync_output_logs" {
  bucket        = "${var.project_name}-sync-output-logs-${var.account_id}"
  force_destroy = false

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-output-logs-${var.account_id}"
    Service = "sync-pipeline"
  })
}

resource "aws_s3_bucket_public_access_block" "sync_output_logs" {
  bucket = aws_s3_bucket.sync_output_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "sync_output_logs" {
  bucket = aws_s3_bucket.sync_output_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Allow S3 log delivery service to write access logs to this bucket
resource "aws_s3_bucket_policy" "sync_output_logs" {
  bucket = aws_s3_bucket.sync_output_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3LogDeliveryWrite"
        Effect = "Allow"
        Principal = {
          Service = "logging.s3.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.sync_output_logs.arn}/*"
        Condition = {
          ArnLike = {
            "aws:SourceArn" = aws_s3_bucket.sync_output.arn
          }
        }
      },
      {
        Sid    = "DenyNonTLS"
        Effect = "Deny"
        Principal = {
          AWS = "*"
        }
        Action = "s3:*"
        Resource = [
          aws_s3_bucket.sync_output_logs.arn,
          "${aws_s3_bucket.sync_output_logs.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })
}

# Lifecycle rule for access logs — expire after 90 days (sufficient for audit/debug)
resource "aws_s3_bucket_lifecycle_configuration" "sync_output_logs" {
  bucket = aws_s3_bucket.sync_output_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ─────────────────────────────────────────────────────────────
# Sync pipeline output bucket
#
# Written to by the receiver EC2 (raw CADU + RT-STPS RDR HDF5) and by
# codebuild.tf's aggregation project (SDR/GEO HDF5, then products/ from the
# reused viirs-orchestrator Lambda). Same layout convention as
# modules/sdr_pipeline's bucket: contacts/{date}/{contact_id}/{cadu,rdr,sdr}/
# and products/{date}/{contact_id}/.
# ─────────────────────────────────────────────────────────────

# checkov:skip=CKV2_AWS_62: Event notifications not needed on this bucket —
# this pipeline is triggered by Ground Station contact-state EventBridge
# events and direct Step Functions invocation, not S3 events (see eventbridge.tf)
# checkov:skip=CKV_AWS_144: Single-region deployment by design — satellite data
# is processed in eu-central-1 only; cross-region replication adds unnecessary cost
resource "aws_s3_bucket" "sync_output" {
  bucket        = "${var.project_name}-sync-output-${var.account_id}"
  force_destroy = false

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-output-${var.account_id}"
    Service = "sync-pipeline"
  })
}

resource "aws_s3_bucket_versioning" "sync_output" {
  bucket = aws_s3_bucket.sync_output.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "sync_output" {
  bucket = aws_s3_bucket.sync_output.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "sync_output" {
  bucket = aws_s3_bucket.sync_output.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "sync_output" {
  bucket        = aws_s3_bucket.sync_output.id
  target_bucket = aws_s3_bucket.sync_output_logs.id
  target_prefix = "s3-access-logs/"
}

resource "aws_s3_bucket_lifecycle_configuration" "sync_output" {
  bucket = aws_s3_bucket.sync_output.id

  rule {
    id     = "transition-to-ia-and-glacier"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 365
      storage_class = "GLACIER"
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "sync_output" {
  bucket = aws_s3_bucket.sync_output.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DenyNonTLS"
        Effect = "Deny"
        Principal = {
          AWS = "*"
        }
        Action = "s3:*"
        Resource = [
          aws_s3_bucket.sync_output.arn,
          "${aws_s3_bucket.sync_output.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Sync output bucket -- deliberately NO EventBridge notification, same lesson
# as modules/sdr_pipeline/s3.tf (see that file's comment for the incident this
# avoids). This pipeline's orchestration is contact-level, not object-level:
# eventbridge.tf triggers off aws.groundstation Contact State Change, and
# step_functions.tf invokes the aggregation CodeBuild project and the reused
# viirs-orchestrator Lambda directly.
# ---------------------------------------------------------------------------
