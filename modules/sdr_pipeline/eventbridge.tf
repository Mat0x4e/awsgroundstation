# eventbridge.tf — trigger for the SDR pipeline
# Requirements: 6.1 (automatic trigger), 6.6 (idempotence)
#
# The pipeline is triggered ONCE PER CONTACT, on the Ground Station contact
# reaching COMPLETED — not per uploaded .pcap object.
#
# The original design (.kiro/specs/noaa20-cadu-to-tiff/design.md §38) triggered
# directly from S3 ObjectCreated. That cannot work for this pipeline, for two
# independent reasons, both observed on contact ba2c5446 (2026-08-31):
#
#   1. Shape. The S3 rule emitted {bucket, key, contact_id: <the whole S3 key>}.
#      The state machine consumes {contact_id, bucket, chunks[], contact_date};
#      ParallelProcessing reads ItemsPath "$.chunks" and the Map's ItemSelector
#      reads $$.Execution.Input.contact_date. Feeding it the S3 rule's payload
#      fails immediately with States.ReferencePathConflict:
#      'Unable to apply step "chunks" to input {...}'.
#
#   2. Timing. A contact delivers one .pcap roughly every 30 s — 22 objects over
#      ~10 minutes for a 10-minute pass. An execution started by the first object
#      (~28 s after AOS) cannot know the full chunk list; the last object lands
#      ~19 s after LOS. Per-object triggering also starts one execution per
#      object, so a single pass would fire 22 of them.
#
# Ground Station Contact State Change fires once, after the contact ends, when
# every chunk has been delivered. That is the correct trigger granularity.

###############################################################################
# EventBridge Rule — Ground Station contact COMPLETED
###############################################################################

resource "aws_cloudwatch_event_rule" "contact_completed" {
  name        = "${var.project_name}-contact-completed-sdr"
  description = "Starts the SDR pipeline when a Ground Station contact completes and all .pcap chunks are delivered"

  event_pattern = jsonencode({
    source        = ["aws.groundstation"]
    "detail-type" = ["Ground Station Contact State Change"]
    detail = {
      contactStatus = ["COMPLETED"]
    }
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-contact-completed-sdr"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# EventBridge Target — Step Functions state machine
#
# The event carries contactId and the event time; the reception bucket and
# satellite id are constants for this deployment. contact_date is NOT derived
# here: input transformers substitute whole values and cannot reformat
# "2026-09-06T11:57:59Z" into "2026/09/06". The state machine's DeriveContactDate
# state does that with States.StringSplit / States.Format.
###############################################################################

resource "aws_cloudwatch_event_target" "start_sdr_pipeline" {
  rule     = aws_cloudwatch_event_rule.contact_completed.name
  arn      = local.sfn_arn
  role_arn = aws_iam_role.eventbridge.arn

  input_transformer {
    input_paths = {
      contact_id   = "$.detail.contactId"
      contact_time = "$.time"
    }

    input_template = <<-EOT
      {
        "contact_id": <contact_id>,
        "contact_time": <contact_time>,
        "bucket": "${var.input_bucket_name}",
        "satellite_id": "${var.satellite_id}"
      }
    EOT
  }

  # Idempotence is the .processing marker in CheckProcessingMarker, not the
  # execution name: an EventBridge target cannot set a Step Functions execution
  # name, so Step Functions generates one. COMPLETED fires once per contact, so
  # duplicates are only possible on an EventBridge redelivery.
}

###############################################################################
# EventBridge Rule — S3 .pcap uploaded (DISABLED, superseded)
#
# Kept, disabled, as the record of the superseded trigger. Deleting it would
# lose the link between the design doc's per-object decision and why it was
# abandoned. Do not re-enable without also giving the state machine a way to
# wait for all chunks and to build the chunks[] array itself.
###############################################################################

resource "aws_cloudwatch_event_rule" "pcap_uploaded" {
  name        = "${var.project_name}-pcap-uploaded"
  description = "SUPERSEDED by ${var.project_name}-contact-completed-sdr — per-object triggering cannot assemble a whole contact"
  state       = "DISABLED"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
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
# Local — State machine ARN
###############################################################################

locals {
  sfn_arn = aws_sfn_state_machine.sdr_pipeline.arn
}
