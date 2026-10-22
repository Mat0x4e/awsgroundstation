# ssm_drift_check.tf — SSM State Manager association for /opt/* drift detection on the
# aggregation EC2 instance (Requirement 5, OE-2, acceptance criterion 5.2).
#
# SCOPING DECISION: This implements the drift-DETECTION path only.
# Full automated reconstruction of /opt/rt-stps and /opt/SDR_4_1 from vendor tarballs is
# DEFERRED. Rationale: the CSPP/RT-STPS installation is complex and hard-won (weeks of
# manual work per the docs); re-encoding it blind into an SSM Document risks silent
# wrongness that only surfaces during a pass — far worse than a detected-drift alarm
# prompting a human to verify and restore. When reconstruction is attempted, validate it
# against a real contact run before removing this DEFERRED flag.

###############################################################################
# SSM Command Document — /opt/* presence and integrity check (aggregation)
###############################################################################
# Checks that the three critical paths exist and are non-empty on every run:
#   /opt/rt-stps             — RT-STPS installation directory
#   /opt/rt-stps/bin/batch.sh — RT-STPS batch processing entry point
#   /opt/SDR_4_1             — SDR_4_1_DB LUT package directory (CSPP calibration)
#
# On any missing/empty path: emits OptDriftDetected=1 to CloudWatch (namespace
# SDRPipeline, dimension InstanceId), prints the missing paths to stdout, exits 1.
# On all-present: emits OptDriftDetected=0 and exits 0.
# The metric is consumed by aws_cloudwatch_metric_alarm.opt_drift in monitoring.tf.

resource "aws_ssm_document" "opt_drift_check" {
  name            = "${var.project_name}-opt-drift-check"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Drift detection for the aggregation EC2 instance: asserts /opt/rt-stps, /opt/rt-stps/bin/batch.sh, and /opt/SDR_4_1 are present and non-empty. Emits OptDriftDetected=1 to CloudWatch (SDRPipeline) on drift; 0 on OK."
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "checkOptPaths"
        inputs = {
          runCommand = [
            "#!/bin/bash",
            "set -uo pipefail",
            "INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)",
            "REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)",
            "NAMESPACE=\"SDRPipeline\"",
            "MISSING=\"\"",
            "",
            "# /opt/rt-stps must exist and be non-empty",
            "if [ ! -d /opt/rt-stps ] || [ -z \"$(ls -A /opt/rt-stps)\" ]; then MISSING=\"$MISSING /opt/rt-stps\"; fi",
            "",
            "# RT-STPS batch entry point must be present",
            "if [ ! -f /opt/rt-stps/bin/batch.sh ]; then MISSING=\"$MISSING /opt/rt-stps/bin/batch.sh\"; fi",
            "",
            "# /opt/SDR_4_1 (SDR_4_1_DB LUT package dir) must exist and be non-empty",
            "if [ ! -d /opt/SDR_4_1 ] || [ -z \"$(ls -A /opt/SDR_4_1)\" ]; then MISSING=\"$MISSING /opt/SDR_4_1\"; fi",
            "",
            "if [ -n \"$MISSING\" ]; then",
            "  echo \"DRIFT DETECTED -- missing or empty:$MISSING\"",
            "  aws cloudwatch put-metric-data --namespace \"$NAMESPACE\" --metric-name OptDriftDetected --value 1 --unit Count --dimensions \"Name=InstanceId,Value=$INSTANCE_ID\" --region \"$REGION\"",
            "  exit 1",
            "fi",
            "",
            "echo \"All /opt paths intact -- no drift\"",
            "aws cloudwatch put-metric-data --namespace \"$NAMESPACE\" --metric-name OptDriftDetected --value 0 --unit Count --dimensions \"Name=InstanceId,Value=$INSTANCE_ID\" --region \"$REGION\""
          ]
        }
      }
    ]
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-opt-drift-check"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# SSM State Manager Association — daily drift check on the aggregation instance
###############################################################################
# Note: SSM State Manager associations execute only when the SSM agent is Online
# (i.e. when the instance is running). The aggregation instance is normally
# STOPPED between contacts — this is intentional. The check runs at the next
# start, which is exactly when it matters: the Trigger Lambda starts the instance
# immediately before a pass, so any drift surfaces while there is still time for
# an operator to intervene before AOS.
#
# apply_only_at_cron_interval is not set: AWS does not support it for rate()
# expressions (only for cron expressions). The association will attempt an
# initial run on creation, but execution is silently deferred when the SSM agent
# is offline (instance stopped), so no action is taken.
#
# Target is pinned to the aggregation instance ID rather than a tag query for
# precision — the document runs specific path checks tied to this instance only.

resource "aws_ssm_association" "opt_drift_check" {
  name             = aws_ssm_document.opt_drift_check.name
  association_name = "${var.project_name}-opt-drift-check"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.aggregation.id]
  }

  # Run once per day. Execution is silently skipped when the instance is stopped
  # (SSM agent offline). On the next start — which precedes every contact — the
  # check runs and any drift is visible to the operator via the OptDriftDetected
  # alarm before the aggregation CodeBuild build begins.
  schedule_expression = "rate(1 day)"

  tags = merge(var.tags, {
    Name    = "${var.project_name}-opt-drift-check"
    Service = "sdr-pipeline"
  })
}
