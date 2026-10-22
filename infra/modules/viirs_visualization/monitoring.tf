# monitoring.tf — CloudWatch alarms for the VIIRS visualization module.
#
# REL-2 / REL-4 target: surface the "degraded-but-green" signal — a contact
# that completed without error at the Step Functions level but produced no
# imagery — before it is silently missed by the ops team.
#
# Satisfies: architecture-refinement spec Task 2.3 (REL-4 acceptance 2.2)

###############################################################################
# ProductsWritten == 0 — visualization produced no imagery in a contact window
###############################################################################

# The ProductsWritten metric is emitted by the visualization CodeBuild buildspec's
# post_build phase, after `aws s3 sync` uploads products to S3 (namespace SDRPipeline,
# dimension ContactId, Unit Count).  It therefore reflects the actual uploaded file
# count rather than the (always-empty) products/ prefix at Lambda dispatch time.
#
# DIMENSION DECISION — no ContactId dimension on this alarm:
#   This alarm intentionally omits the ContactId dimension so it aggregates
#   across all contacts within each evaluation period.  That gives a coarse
#   "pipeline produced nothing at all in the last 5 minutes" signal, which is
#   the correct ops-level indicator (REL-4).  Per-contact pass-level alerting
#   (e.g. "contact X produced 0 images") is better expressed via metric math
#   or an EventBridge rule on Step Functions execution history — deferred as a
#   future enhancement.  The aggregate-no-dimension approach fires exactly
#   when Sum(ProductsWritten) == 0 over a window that covered at least one
#   contact dispatch, i.e. the metric was published but no products resulted.
#
# treat_missing_data = "notBreaching":
#   ProductsWritten is only emitted when a contact is dispatched; there is no
#   metric data between passes.  Without this setting the alarm would breach
#   continuously whenever no contact is in progress, generating noise.

resource "aws_cloudwatch_metric_alarm" "products_written_zero" {
  alarm_name        = "${var.project_name}-viirs-products-written-zero"
  alarm_description = "VIIRS visualization produced zero products in the last 5-minute window — possible imagery-generation failure (REL-4)"

  namespace   = "SDRPipeline"
  metric_name = "ProductsWritten"
  statistic   = "Sum"

  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"

  # No dimensions block: alarm aggregates across all ContactId values.
  # See DIMENSION DECISION comment above.

  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-viirs-products-written-zero"
    Service = "viirs-visualization"
  })
}
