# monitoring.tf — CloudWatch alarms for the synchronous pipeline's single
# points of failure. There is no S3 buffer in this path (see ec2.tf), so a
# receiver EC2 failure or a missed arm/finish command means the pass is lost
# outright -- these alarms exist to surface that immediately via the shared
# SNS topic (modules/security), and to auto-recover the instance where AWS
# supports it.

###############################################################################
# Receiver EC2 -- status check alarms
###############################################################################

# System status check failures are hardware/host-level and recoverable by
# AWS's stop/start-on-different-hardware automation. Thresholds/periods match
# AWS's documented EC2 auto-recovery alarm pattern.
resource "aws_cloudwatch_metric_alarm" "receiver_system_status_check" {
  alarm_name          = "${var.project_name}-sync-receiver-system-status-check"
  alarm_description   = "Receiver EC2 failed its system status check -- triggers AWS auto-recovery"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    InstanceId = aws_instance.receiver.id
  }

  # The instance is normally stopped between passes, so there's no metric
  # data most of the time -- that must not be treated as a breach.
  treat_missing_data = "notBreaching"

  alarm_actions = [
    "arn:aws:automate:${data.aws_region.current.id}:ec2:recover",
    var.sns_topic_arn,
  ]
  ok_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-system-status-check"
    Service = "sync-pipeline"
  })
}

# Instance status check failures are guest-OS-level and not recoverable via
# stop/start automation -- notify only.
resource "aws_cloudwatch_metric_alarm" "receiver_instance_status_check" {
  alarm_name          = "${var.project_name}-sync-receiver-instance-status-check"
  alarm_description   = "Receiver EC2 failed its instance (guest OS) status check"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_Instance"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    InstanceId = aws_instance.receiver.id
  }

  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-instance-status-check"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Receiver Arm Lambda -- errors (e.g. instance never reached running/SSM
# Online within the timing budget, send_command failures)
###############################################################################

resource "aws_cloudwatch_metric_alarm" "receiver_arm_lambda_errors" {
  alarm_name          = "${var.project_name}-sync-receiver-arm-lambda-errors"
  alarm_description   = "Receiver Arm Lambda raised an error -- the UDP listener may not be armed before AOS"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    FunctionName = aws_lambda_function.receiver_arm.function_name
  }

  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-arm-lambda-errors"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Step Functions -- pipeline execution failures (post-contact processing)
###############################################################################

resource "aws_cloudwatch_metric_alarm" "sync_pipeline_execution_failures" {
  alarm_name          = "${var.project_name}-sync-pipeline-execution-failures"
  alarm_description   = "Sync pipeline Step Functions execution failed"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.sync_pipeline.arn
  }

  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-pipeline-execution-failures"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Task 6.1 (mirror) — /opt/* drift detected on sync receiver EC2 (R5-AC2, OE-2)
#
# Mirror of sdr_pipeline/monitoring.tf's opt_drift alarm. The SSM association in
# ssm_drift_check.tf emits OptDriftDetected=1 (namespace SyncPipeline) when
# /opt/rt-stps or /opt/rt-stps/config/jpss1.xml is missing or empty on the receiver.
#
# The receiver is normally stopped between passes. treat_missing_data=notBreaching
# means no data during stopped periods is not treated as drift.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "opt_drift" {
  alarm_name          = "${var.project_name}-sync-receiver-opt-drift"
  alarm_description   = "Hand-installed /opt/rt-stps or jpss1.xml absent or empty on sync receiver EC2 -- manual restoration required before next pass (OE-2)"
  namespace           = "SyncPipeline"
  metric_name         = "OptDriftDetected"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    InstanceId = aws_instance.receiver.id
  }

  # No data during stopped periods is not a breach.
  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-opt-drift"
    Service = "sync-pipeline"
  })
}

###############################################################################
# Task 9.1 (REL-5) — PREPASS receiver-readiness alarm
#
# PREVENTIVE signal: the Python receiver script (sync_receiver_listen.py) emits
# ReceiverReadyFailure=1 (namespace "SyncPipeline") when the UDP listener did
# NOT confirm bound+receiving within the pre-pass window; 0 on success. This
# alarm fires during/around PREPASS so an operator learns of the arming failure
# BEFORE the pass is fully lost -- unlike the detective receiver_arm_lambda_errors
# alarm above, which only surfaces that the Arm Lambda itself errored, not
# whether the socket actually bound and saw first packets.
#
# IAM: the receiver EC2 role already holds a namespace-conditioned
# cloudwatch:PutMetricData statement (Sid "PutMetricDataDriftCheck", condition
# cloudwatch:namespace = "SyncPipeline") added in Wave 3. No new IAM is needed.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "receiver_ready_failure" {
  alarm_name          = "${var.project_name}-sync-receiver-ready-failure"
  alarm_description   = "Receiver UDP listener did not confirm bound+receiving during PREPASS -- pass may be lost (REL-5)"
  namespace           = "SyncPipeline"
  metric_name         = "ReceiverReadyFailure"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # The metric is only emitted around PREPASS. No data between passes is not
  # a failure -- it means the listener was not armed (instance stopped), which
  # is the expected state.
  treat_missing_data = "notBreaching"

  alarm_actions = [var.sns_topic_arn]

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-ready-failure"
    Service = "sync-pipeline"
  })
}
