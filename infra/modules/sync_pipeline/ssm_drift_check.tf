# ssm_drift_check.tf — SSM State Manager association for /opt/* drift detection on the
# synchronous receiver EC2 instance (Requirement 5, OE-2, acceptance criterion 5.2).
# Mirror of infra/modules/sdr_pipeline/ssm_drift_check.tf scoped to the receiver's
# critical paths (/opt/rt-stps + the patched jpss1.xml) and the SyncPipeline namespace.
#
# SCOPING DECISION: drift-DETECTION only. Full reconstruction is DEFERRED — same
# rationale as the sdr_pipeline module: the RT-STPS install is complex and hard-won;
# re-encoding it blind risks silent wrongness. See sdr_pipeline/ssm_drift_check.tf.

###############################################################################
# SSM Command Document — /opt/* presence check (sync receiver)
###############################################################################
# Checks that the two critical receiver paths exist and are non-empty:
#   /opt/rt-stps                      — RT-STPS installation directory
#   /opt/rt-stps/config/jpss1.xml     — patched config (PnEncoded="true" variant,
#                                        applied by scripts/sync_receiver_finish.sh)
#
# On any missing/empty path: emits OptDriftDetected=1 to CloudWatch (namespace
# SyncPipeline, dimension InstanceId), prints the missing paths, exits 1.
# On all-present: emits OptDriftDetected=0 and exits 0.

resource "aws_ssm_document" "opt_drift_check" {
  name            = "${var.project_name}-sync-opt-drift-check"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Drift detection for the sync receiver EC2 instance: asserts /opt/rt-stps and /opt/rt-stps/config/jpss1.xml are present and non-empty. Emits OptDriftDetected=1 to CloudWatch (SyncPipeline) on drift; 0 on OK."
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
            "NAMESPACE=\"SyncPipeline\"",
            "MISSING=\"\"",
            "",
            "# /opt/rt-stps must exist and be non-empty",
            "if [ ! -d /opt/rt-stps ] || [ -z \"$(ls -A /opt/rt-stps)\" ]; then MISSING=\"$MISSING /opt/rt-stps\"; fi",
            "",
            "# Patched jpss1.xml (PnEncoded=true variant) must be present",
            "if [ ! -f /opt/rt-stps/config/jpss1.xml ]; then MISSING=\"$MISSING /opt/rt-stps/config/jpss1.xml\"; fi",
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
    Name    = "${var.project_name}-sync-opt-drift-check"
    Service = "sync-pipeline"
  })
}

###############################################################################
# SSM State Manager Association — daily drift check on the receiver instance
###############################################################################
# The receiver instance is normally STOPPED between passes. The association runs
# on each start — which is triggered by the receiver_arm Lambda during PREPASS —
# so any drift is visible before AOS, when there is still time to intervene.
#
# apply_only_at_cron_interval is not set: not supported for rate() expressions.
# The initial-run attempt on association creation is harmless while the instance
# is stopped (SSM silently defers execution when the agent is offline).

resource "aws_ssm_association" "opt_drift_check" {
  name             = aws_ssm_document.opt_drift_check.name
  association_name = "${var.project_name}-sync-opt-drift-check"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.receiver.id]
  }

  # Run once per day; silently deferred when the receiver is stopped (most of
  # the time). Executes on the next start, which is PREPASS — before AOS.
  schedule_expression = "rate(1 day)"

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-opt-drift-check"
    Service = "sync-pipeline"
  })
}
