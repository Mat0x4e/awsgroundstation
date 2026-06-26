# eventbridge.tf — EventBridge rule triggering the VIIRS orchestrator Lambda
#
# Captures S3 ObjectCreated events from the SDR output bucket for:
#   - manifest.json files  (contacts/{date}/{id}/manifest.json)
#   - SatDump composite PNGs matching viirs_rgb_* or viirs_*_Thermal_IR_* patterns
#
# Requirements: 11.1, 11.2
#
# NOTE: The SDR output bucket must have EventBridge notifications enabled.
# This is set via aws_s3_bucket_notification with eventbridge = true on the
# bucket resource (owned by the sdr_pipeline module). Ensure that module
# enables S3 → EventBridge integration before this rule can receive events.

# ─────────────────────────────────────────────────────────────────────────────
# EventBridge rule — ObjectCreated on manifest.json or composite PNGs
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "viirs_trigger" {
  name        = "${var.project_name}-viirs-trigger"
  description = "Triggers VIIRS visualization orchestrator when manifest.json or composite PNGs land in the SDR output bucket"

  # S3 sends ObjectCreated events to the default EventBridge bus when bucket
  # notifications are configured with eventbridge = true.
  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = {
        name = [var.sdr_output_bucket_name]
      }
      object = {
        # Match manifest.json or any PNG file deposited under contacts/
        # EventBridge does not support wildcards in suffix filters, so we
        # match the two relevant extensions and rely on the Lambda to
        # further filter by filename pattern before submitting CodeBuild.
        key = [
          { suffix = "/manifest.json" },
          { suffix = ".png" }
        ]
      }
    }
  })

  tags = var.tags
}

# ─────────────────────────────────────────────────────────────────────────────
# EventBridge target — Lambda orchestrator with input transformer
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_event_target" "viirs_lambda" {
  rule = aws_cloudwatch_event_rule.viirs_trigger.name
  arn  = aws_lambda_function.viirs_orchestrator.arn

  # Input transformer: extract the S3 bucket name and object key from the
  # EventBridge event and pass them to the Lambda as a structured payload.
  # The Lambda uses these to locate the contact folder and detect the
  # visualization path (SatDump vs NASA).
  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }
    # Lambda event shape: { "bucket": "<name>", "key": "<prefix/file>" }
    input_template = "{\"bucket\": <bucket>, \"key\": <key>}"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Lambda resource-based policy — allow EventBridge to invoke the orchestrator
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_lambda_permission" "eventbridge_invoke_viirs" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.viirs_orchestrator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.viirs_trigger.arn
}
