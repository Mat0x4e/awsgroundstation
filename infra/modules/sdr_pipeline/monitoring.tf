# monitoring.tf — CloudWatch alarms for the async SDR pipeline.
# Mirrors the alarm style established in sync_pipeline/monitoring.tf.
# Requirements: R3 (alarm parity) and R4 (observable swallowed viz failure).

###############################################################################
# Task 4.1 — SDR State Machine execution failures (R3-AC1)
###############################################################################

resource "aws_cloudwatch_metric_alarm" "sdr_pipeline_execution_failures" {
  alarm_name          = "${var.project_name}-sdr-pipeline-execution-failures"
  alarm_description   = "SDR pipeline Step Functions execution failed"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.sdr_pipeline.arn
  }

  # The state machine only runs during contacts — no data between runs is normal.
  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-pipeline-execution-failures"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# Task 4.2 — Aggregation Trigger Lambda errors (R3-AC2)
###############################################################################

resource "aws_cloudwatch_metric_alarm" "aggregation_trigger_lambda_errors" {
  alarm_name          = "${var.project_name}-aggregation-trigger-lambda-errors"
  alarm_description   = "Aggregation Trigger Lambda raised an error -- the aggregation EC2 instance may not have started"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    FunctionName = aws_lambda_function.aggregation_trigger.function_name
  }

  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-trigger-lambda-errors"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# Task 5.1 — Observable swallowed visualization failure (R4-AC1, R4-AC3)
#
# Approach: aws_cloudwatch_log_metric_filter on the SFN execution log group
# (aws_cloudwatch_log_group.sfn, level=ALL). When the NotifyVisualizationFailed
# state is entered, Step Functions logs a TaskStateEntered event with
# details.name = "NotifyVisualizationFailed". The metric filter matches that
# JSON field and increments a VisualizationFailed counter (namespace SDRPipeline).
#
# This approach was chosen over a PutMetricData call in the Lambda handler
# because: (a) it requires no new IAM permissions, (b) the SFN log at level=ALL
# is already written unconditionally, and (c) the filter pattern is narrow and
# deterministic. The SNS notification from NotifyVisualizationFailed (added in
# step_functions.tf) satisfies R4-AC1's immediacy requirement; this metric +
# alarm satisfies R4-AC3's threshold-over-window requirement.
###############################################################################

resource "aws_cloudwatch_log_metric_filter" "visualization_failed" {
  name           = "${var.project_name}-sdr-visualization-failed"
  log_group_name = aws_cloudwatch_log_group.sfn.name

  # Step Functions logs TaskStateEntered events with the state name under
  # details.name when level=ALL. Match precisely on the notify state so only
  # an actual catch-path traversal increments the metric.
  pattern = "{ $.details.name = \"NotifyVisualizationFailed\" }"

  metric_transformation {
    name      = "VisualizationFailed"
    namespace = "SDRPipeline"
    value     = "1"
    # Default value 0 ensures the metric exists between contacts so the alarm
    # can evaluate rather than sitting in INSUFFICIENT_DATA.
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "visualization_failed" {
  alarm_name          = "${var.project_name}-sdr-visualization-failed"
  alarm_description   = "SDR pipeline visualization step took its catch path -- render defect may be systematic (products are in S3)"
  namespace           = "SDRPipeline"
  metric_name         = "VisualizationFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-visualization-failed"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# Task 6.1 — /opt/* drift detected on aggregation EC2 (R5-AC2, OE-2)
#
# The SSM State Manager association in ssm_drift_check.tf runs a shell check
# on the aggregation instance once per day (or on each start). It emits
# OptDriftDetected=1 when any critical path (/opt/rt-stps, /opt/rt-stps/bin/batch.sh,
# /opt/SDR_4_1) is absent or empty, and OptDriftDetected=0 when all paths are intact.
#
# This alarm fires when the metric exceeds zero within a 24-hour evaluation window.
# treat_missing_data=notBreaching: the instance is normally stopped between contacts,
# so no data simply means the instance was not running — not drift.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "opt_drift" {
  alarm_name          = "${var.project_name}-aggregation-opt-drift"
  alarm_description   = "Hand-installed /opt/rt-stps or /opt/SDR_4_1 path absent or empty on aggregation EC2 -- manual restoration required before next pass (OE-2)"
  namespace           = "SDRPipeline"
  metric_name         = "OptDriftDetected"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    InstanceId = aws_instance.aggregation.id
  }

  # No data during periods the instance is stopped is not a breach --
  # the check only runs when the instance is running (SSM agent Online).
  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-opt-drift"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# Task 10 — Chunk-level success / failure metrics (REL-3)
#
# Context: the Map state runs with ToleratedFailurePercentage=100 — partial
# results are still useful (aggregation picks up what it can) and the machine
# must not abort because one chunk built badly.  The problem is that a contact
# where every single chunk failed is indistinguishable from a clean contact:
# both produce a green SUCCEEDED execution.  These filters + alarm surface the
# "zero good chunks" case WITHOUT altering the tolerate-100% behaviour.
#
# Filter pattern mirrors the visualization_failed filter exactly.  Step
# Functions logs a TaskStateEntered/StateEntered event for every state entered
# at level=ALL; those events appear in aws_cloudwatch_log_group.sfn with the
# state name under $.details.name.  BuildSucceeded and MarkChunkFailed are the
# two terminal Pass states in the ItemProcessor — matching them is precise and
# requires no new IAM permissions.
###############################################################################

resource "aws_cloudwatch_log_metric_filter" "chunk_succeeded" {
  name           = "${var.project_name}-sdr-chunk-succeeded"
  log_group_name = aws_cloudwatch_log_group.sfn.name

  # Step Functions logs TaskStateEntered events with $.details.name = state name
  # at level=ALL — identical field path to the visualization_failed filter above.
  pattern = "{ $.details.name = \"BuildSucceeded\" }"

  metric_transformation {
    name          = "ChunksSucceeded"
    namespace     = "SDRPipeline"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_log_metric_filter" "chunk_failed" {
  name           = "${var.project_name}-sdr-chunk-failed"
  log_group_name = aws_cloudwatch_log_group.sfn.name

  pattern = "{ $.details.name = \"MarkChunkFailed\" }"

  metric_transformation {
    name          = "ChunksFailed"
    namespace     = "SDRPipeline"
    value         = "1"
    default_value = 0
  }
}

# Alarm form: metric-math IF(m1>0 AND m2==0, 1, 0) with period=3600.
#
# Why metric-math (not a plain ChunksSucceeded<=0 alarm):
#   default_value=0 on each filter emits a synthetic 0 every period even
#   between contacts.  A plain threshold-on-ChunksSucceeded<=0 alarm would
#   therefore be permanently ALARM between contacts.  treat_missing_data cannot
#   help because there IS always data (the synthetic 0).  The IF expression
#   becomes non-zero only when failures are present AND successes are absent
#   in the same bucket, producing no false positives between contacts.
#
# ── LIVE-RUN FAILURE (period=900) and root-cause analysis ─────────────────
#
#   Observed: 22 chunks failed; ChunksFailed summed to 44 in the window
#   ~15:51–15:59 UTC; ChunksSucceeded=0 throughout.  AllChunksFailed was true.
#   Alarm stayed OK.  CloudWatch StateReason: "Threshold Crossed: 1 datapoint
#   [0.0 (14:00:00)] was not greater than the threshold (0.0 with 1 datapoints
#   [0.0 (14:00:00)])."
#
#   With period=900 (15-min buckets) the burst fell entirely in the 13:45–14:00
#   bucket.  At the period boundary 14:00 the alarm evaluation ran.  Two
#   concurrent effects combined to hide the burst:
#
#   (a) Log metric filter ingestion latency (~1–5 min): the real datapoint
#       m1=44 for the 13:45 bucket had not yet been committed when the alarm
#       evaluated at 14:00.
#
#   (b) Synthetic-zero timing: default_value=0 causes CloudWatch to emit a
#       zero for any period with no matching events.  The new 14:00 period had
#       an immediate default_value=0 available.  CloudWatch took the 14:00
#       synthetic zero as the "most recent complete period" → e1=0 → no alarm.
#
#   terraform validate cannot catch this class of bug: it validates schema,
#   not alarm evaluation timing or metric latency against real data.
#
# ── Why period=3600 fires reliably ────────────────────────────────────────
#
#   1. Burst containment: any fan-out burst ≤10 min is guaranteed to land
#      entirely within one 60-min bucket.  No burst can split across a bucket
#      boundary the way it can with 15-min buckets.
#
#   2. Latency absorbed: log metric filter latency (≤5 min) is negligible
#      against a 60-min window.  By the time the alarm evaluates the completed
#      [13:00–14:00) bucket (at ~14:05), m1=44 and m2=0 are both committed.
#
#   3. No competing zero: the next default_value=0 datapoint belongs to
#      [14:00–15:00), which does not complete until 15:00.  At 14:05 that
#      bucket is still open.  CloudWatch takes the last COMPLETE period
#      [13:00–14:00) → e1=1 → ALARM fires.  With period=900 the adjacent
#      complete 14:00 bucket was available immediately with a synthetic zero,
#      winning the "most recent" comparison over the latency-delayed 13:45 data.
#
#   4. Between contacts: both m1=0 and m2=0 in every idle bucket → e1=0 → no
#      false positives.  treat_missing_data=notBreaching is belt-and-suspenders
#      for periods where even the synthetic zeros are absent.
#
# Trade-off: detection latency is ≤60 min instead of ≤15 min.  Acceptable for
# a satellite contact alarm — the pipeline always finishes before the alarm
# could have fired anyway.
# ──────────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "chunks_all_failed" {
  alarm_name        = "${var.project_name}-sdr-chunks-all-failed"
  alarm_description = "Every chunk in a contact fan-out failed — aggregation will produce no output. ToleratedFailurePercentage=100 keeps the pipeline running on partial results; this alarm fires when there are zero partial results."

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  metric_query {
    id    = "m1"
    label = "ChunksFailed"
    metric {
      namespace   = "SDRPipeline"
      metric_name = "ChunksFailed"
      stat        = "Sum"
      period      = 3600
    }
  }

  metric_query {
    id    = "m2"
    label = "ChunksSucceeded"
    metric {
      namespace   = "SDRPipeline"
      metric_name = "ChunksSucceeded"
      stat        = "Sum"
      period      = 3600
    }
  }

  metric_query {
    id          = "e1"
    expression  = "IF(m1 > 0 AND m2 == 0, 1, 0)"
    label       = "AllChunksFailed"
    return_data = true
  }

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-chunks-all-failed"
    Service = "sdr-pipeline"
  })
}
