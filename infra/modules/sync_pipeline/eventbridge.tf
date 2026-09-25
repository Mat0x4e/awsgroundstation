# eventbridge.tf — triggers for the synchronous pipeline
#
# Two Ground Station Contact State Change triggers, at two different phases of
# the SAME contact — the async pipeline (modules/sdr_pipeline) only needs one:
#
#   1. PREPASS -> receiver_arm Lambda directly. Ground Station enters PREPASS
#      some lead time before AOS (var.contact_pre_pass_duration_seconds); the
#      receiver EC2 must be running and its UDP listener bound before AOS, or
#      the pass is lost outright (no S3 buffer in this path, unlike the async
#      pipeline). Target is the Lambda directly (via aws_lambda_permission in
#      lambda.tf), not the state machine -- there's nothing to orchestrate yet.
#
#   2. COMPLETED -> sync_pipeline state machine. Same granularity lesson as
#      modules/sdr_pipeline/eventbridge.tf: triggering per-object caused a real
#      incident (~6,000 CodeBuild builds in a morning, 2026-09-01). This
#      pipeline has no per-object triggering to begin with (the receiver
#      writes one CADU capture file per contact), but the principle still
#      applies -- orchestration starts once, at contact granularity.
#
# Both rules filter on detail.missionProfileArn, scoped to THIS module's
# mission profile. Without that, two mission profiles exist for the same
# satellite (this one, and modules/mission_profile's async DigIF profile) and
# an unscoped "contactStatus: COMPLETED" rule fires on BOTH pipelines'
# contacts -- modules/sdr_pipeline/eventbridge.tf predates this second
# mission profile and does NOT have this filter, so it will spuriously fire
# (and publish a false "no chunks found" SNS failure) on every sync pipeline
# contact. That is a known, accepted gap in the existing module -- it is not
# modified here per the fully-separate-pipeline requirement -- but the new
# rules below must not repeat the mistake in the other direction.

###############################################################################
# EventBridge Rule — Ground Station contact PREPASS
###############################################################################

resource "aws_cloudwatch_event_rule" "contact_prepass" {
  name        = "${var.project_name}-contact-prepass-sync"
  description = "Arms the sync receiver before AOS when a Ground Station contact enters PREPASS"

  event_pattern = jsonencode({
    source        = ["aws.groundstation"]
    "detail-type" = ["Ground Station Contact State Change"]
    detail = {
      contactStatus     = ["PREPASS"]
      missionProfileArn = [awscc_groundstation_mission_profile.noaa20_sync.arn]
    }
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-contact-prepass-sync"
    Service = "sync-pipeline"
  })
}

resource "aws_cloudwatch_event_target" "arm_receiver" {
  rule = aws_cloudwatch_event_rule.contact_prepass.name
  arn  = aws_lambda_function.receiver_arm.arn

  input_transformer {
    input_paths = {
      contact_id   = "$.detail.contactId"
      contact_time = "$.time"
    }

    input_template = <<-EOT
      {
        "contact_id": <contact_id>,
        "contact_time": <contact_time>,
        "bucket": "${aws_s3_bucket.sync_output.id}"
      }
    EOT
  }

  # Permission for EventBridge to invoke this Lambda target lives in
  # lambda.tf (aws_lambda_permission.receiver_arm_eventbridge) -- EventBridge
  # -> Lambda targets use a Lambda resource policy, not role_arn.
}

###############################################################################
# EventBridge Rule — Ground Station contact COMPLETED
###############################################################################

resource "aws_cloudwatch_event_rule" "contact_completed" {
  name        = "${var.project_name}-contact-completed-sync"
  description = "Starts the sync pipeline state machine when a Ground Station contact completes"

  event_pattern = jsonencode({
    source        = ["aws.groundstation"]
    "detail-type" = ["Ground Station Contact State Change"]
    detail = {
      contactStatus     = ["COMPLETED"]
      missionProfileArn = [awscc_groundstation_mission_profile.noaa20_sync.arn]
    }
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-contact-completed-sync"
    Service = "sync-pipeline"
  })
}

resource "aws_cloudwatch_event_target" "start_sync_pipeline" {
  rule     = aws_cloudwatch_event_rule.contact_completed.name
  arn      = aws_sfn_state_machine.sync_pipeline.arn
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
        "bucket": "${aws_s3_bucket.sync_output.id}",
        "satellite_id": "${var.satellite_id}"
      }
    EOT
  }
}
