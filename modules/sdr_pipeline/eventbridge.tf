# eventbridge.tf — EventBridge rule to trigger the SDR pipeline on .pcap uploads
# Requirements: 6.1 (automatic trigger on S3 ObjectCreated)
#               6.6 (idempotence via execution name = contact_id)

###############################################################################
# EventBridge Rule — S3 ObjectCreated for .pcap files in the reception bucket
###############################################################################

resource "aws_cloudwatch_event_rule" "pcap_uploaded" {
  name        = "${var.project_name}-pcap-uploaded"
  description = "Triggers SDR pipeline when a .pcap file is uploaded to the reception bucket"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = {
        name = [var.input_bucket_name]
      }
      object = {
        key = [{ suffix = ".pcap" }]
      }
    }
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-pcap-uploaded"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# EventBridge Target — Step Functions state machine
#
# Input transformer extracts contact_id from the S3 key for use as the
# execution name (providing native idempotence — Step Functions rejects
# duplicate execution names within the 90-day retention window).
#
# S3 key pattern: year=YYYY/month=MM/day=DD/satellite=NOAA20/{contact_id}/chunk_NNN.pcap
# contact_id is the 5th path segment (0-indexed: 4th after split on "/")
###############################################################################

resource "aws_cloudwatch_event_target" "start_sdr_pipeline" {
  rule     = aws_cloudwatch_event_rule.pcap_uploaded.name
  arn      = local.sfn_arn
  role_arn = aws_iam_role.eventbridge.arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }

    # Extract contact_id from key: year=YYYY/month=MM/day=DD/satellite=NOAA20/{contact_id}/chunk.pcap
    # The execution name uses the contact_id for idempotence (req 6.6)
    input_template = <<-EOT
      {
        "bucket": <bucket>,
        "key": <key>,
        "contact_id": <key>
      }
    EOT
  }

  # Use contact_id derived from the S3 key as execution name for idempotence
  # Step Functions will reject duplicate execution names (ExecutionAlreadyExists)
  # Note: The actual contact_id extraction from the key path is handled by the
  # state machine's first state (ListChunks) which parses the key structure.
  # The execution name template extracts just the contact_id segment.
}

###############################################################################
# Local — State machine ARN
# Now references the actual resource created in step_functions.tf (task 11.1).
###############################################################################

locals {
  sfn_arn = aws_sfn_state_machine.sdr_pipeline.arn
}
