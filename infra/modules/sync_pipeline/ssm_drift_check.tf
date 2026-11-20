# ssm_drift_check.tf - SSM State Manager association for /opt/* drift detection on the
# synchronous receiver EC2 instance (Requirement 5, OE-2, acceptance criterion 5.2).
# Mirror of infra/modules/sdr_pipeline/ssm_drift_check.tf scoped to the receiver's
# critical paths (the hand-installed helper scripts) and the SyncPipeline namespace.
#
# The receiver no longer carries RT-STPS: RT-STPS runs in the shared Batch aggregation
# job (architecture principle: sync and async converge at the frame capture in S3).
# The receiver only listens, assembles the CADU capture and uploads it, so the only
# hand-installed artefacts that matter are its three helper scripts.
#
# SCOPING DECISION: drift-DETECTION only. Full reconstruction is DEFERRED - same
# rationale as the sdr_pipeline module. See sdr_pipeline/ssm_drift_check.tf.
###############################################################################
# SSM Command Document - /opt/* presence check (sync receiver)
###############################################################################
# Checks that the receiver helper scripts exist (and are executable where run
# directly):
#   /opt/scripts/sync_receiver_arm.sh       - executable (PREPASS arm, via SSM)
#   /opt/scripts/sync_receiver_finish.sh    - executable (post-contact finish, via SSM)
#   /opt/scripts/sync_receiver_listen.py    - present (live-UDP listener)
#
# On any missing path: emits OptDriftDetected=1 to CloudWatch (namespace
# SyncPipeline, dimension InstanceId), prints the missing paths, exits 1.
# On all-present: emits OptDriftDetected=0 and exits 0.
resource "aws_ssm_document" "opt_drift_check" {
  name            = "${var.project_name}-sync-opt-drift-check"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Drift detection for the sync receiver EC2 instance: asserts /opt/scripts/sync_receiver_arm.sh and sync_receiver_finish.sh exist and are executable, and /opt/scripts/sync_receiver_listen.py exists. Emits OptDriftDetected=1 to CloudWatch (SyncPipeline) on drift; 0 on OK."
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
            "# Helper scripts invoked directly via SSM must exist and be executable",
            "for f in /opt/scripts/sync_receiver_arm.sh /opt/scripts/sync_receiver_finish.sh; do",
            "  if [ ! -x \"$f\" ]; then MISSING=\"$MISSING $f\"; fi",
            "done",
            "",
            "# Live-UDP listener (launched by sync_receiver_arm.sh) must be present",
            "if [ ! -f /opt/scripts/sync_receiver_listen.py ]; then MISSING=\"$MISSING /opt/scripts/sync_receiver_listen.py\"; fi",
            "",
            "if [ -n \"$MISSING\" ]; then",
            "  echo \"DRIFT DETECTED -- missing or not executable:$MISSING\"",
            "  aws cloudwatch put-metric-data --namespace \"$NAMESPACE\" --metric-name OptDriftDetected --value 1 --unit Count --dimensions \"Name=InstanceId,Value=$INSTANCE_ID\" --region \"$REGION\"",
            "  exit 1",
            "fi",
            "",
            "echo \"All receiver helper scripts intact -- no drift\"",
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
