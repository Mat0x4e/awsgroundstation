# step_functions.tf — Step Functions state machine for the synchronous pipeline
#
# Flow: DeriveDateOnly -> DeriveDateParts -> BuildContactDate -> WaitForDelivery
#       -> FinishReceiver (SSM: stop listener, run RT-STPS, upload CADU+RDR)
#       -> WaitForFinish -> CheckFinishStatus -> StopReceiver
#       -> StartAggregationBuild (CodeBuild: CSPP only) -> WaitForAggregation
#       -> CheckAggregationBuild -> StartVisualization -> PipelineSucceeded
#       -> (any stage) FailureNotify -> FailExecution
#
# Unlike modules/sdr_pipeline's state machine, there is no chunk fan-out Map
# state: the receiver EC2 writes one combined CADU capture per contact (there
# is nothing to parallelize -- see scripts/sync_receiver_finish.sh), and RT-STPS
# already ran on that EC2 by the time this state machine's CodeBuild stage
# starts. This state machine's job is: hand off from the receiver, then run
# CSPP, then visualize.

###############################################################################
# CloudWatch Log Group — Step Functions execution logs
###############################################################################

resource "aws_cloudwatch_log_group" "sfn" {
  name = "/aws/states/${var.project_name}-sync-pipeline"
  # checkov:skip=CKV_AWS_338: 90-day retention is sufficient for pipeline debug logs —
  # satellite contact data is the permanent record (stored in S3 with lifecycle policies)
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-pipeline-logs"
    Service = "sync-pipeline"
  })
}

###############################################################################
# State Machine
###############################################################################

# checkov:skip=CKV_AWS_284: X-Ray tracing not needed — CloudWatch execution logging
# at level=ALL provides sufficient observability for this batch pipeline
resource "aws_sfn_state_machine" "sync_pipeline" {
  name     = "${var.project_name}-sync-pipeline"
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment        = "NOAA-20 synchronous demod/decode pipeline — receiver handoff + CSPP aggregation + visualization"
    TimeoutSeconds = 5400
    StartAt        = "DeriveDateOnly"

    States = {

      # ── 1. Derive contact_date from the event timestamp ────────────────────
      # Same three-Pass-state pattern as modules/sdr_pipeline/step_functions.tf:
      # EventBridge input transformers substitute whole values and cannot
      # reformat a timestamp, and Step Functions rejects deeply nested
      # intrinsics, so each step nests at most one call.
      DeriveDateOnly = {
        Type    = "Pass"
        Comment = "2026-09-06T11:57:59Z -> 2026-09-06"
        Parameters = {
          "contact_id.$"   = "$.contact_id"
          "bucket.$"       = "$.bucket"
          "satellite_id.$" = "$.satellite_id"
          "date_only.$"    = "States.ArrayGetItem(States.StringSplit($.contact_time, 'T'), 0)"
        }
        Next = "DeriveDateParts"
      }

      DeriveDateParts = {
        Type    = "Pass"
        Comment = "2026-09-06 -> [2026, 09, 06]"
        Parameters = {
          "contact_id.$"   = "$.contact_id"
          "bucket.$"       = "$.bucket"
          "satellite_id.$" = "$.satellite_id"
          "date_parts.$"   = "States.StringSplit($.date_only, '-')"
        }
        Next = "BuildContactDate"
      }

      BuildContactDate = {
        Type    = "Pass"
        Comment = "[2026, 09, 06] -> 2026/09/06, the prefix used throughout the pipeline"
        Parameters = {
          "contact_id.$"   = "$.contact_id"
          "bucket.$"       = "$.bucket"
          "satellite_id.$" = "$.satellite_id"
          "contact_date.$" = "States.Format('{}/{}/{}', $.date_parts[0], $.date_parts[1], $.date_parts[2])"
        }
        Next = "WaitForDelivery"
      }

      # ── 2. WaitForDelivery ─────────────────────────────────────────────────
      # COMPLETED fires at LOS. Give the receiver a moment to flush its capture
      # file to disk before the FinishReceiver SSM command tries to read it.
      WaitForDelivery = {
        Type    = "Wait"
        Seconds = 30
        Next    = "FinishReceiver"
      }

      # ── 3. FinishReceiver ──────────────────────────────────────────────────
      # Sends scripts/sync_receiver_finish.sh via SSM: stops the live-UDP
      # listener, assembles the capture into a combined CADU, runs RT-STPS
      # locally (PnEncoded="true" kept -- see groundstation.tf's header
      # comment), and uploads the raw capture + RDR HDF5 to S3.
      FinishReceiver = {
        Type     = "Task"
        Comment  = "SSM: stop listener, run RT-STPS, upload CADU+RDR"
        Resource = "arn:aws:states:::aws-sdk:ssm:sendCommand"
        Parameters = {
          InstanceIds  = [aws_instance.receiver.id]
          DocumentName = "AWS-RunShellScript"
          Comment      = "Finish sync receiver"
          Parameters = {
            "commands.$"     = "States.Array(States.Format('/opt/scripts/sync_receiver_finish.sh {} {} {}', $.bucket, $.contact_id, $.contact_date))"
            executionTimeout = ["1800"]
          }
          TimeoutSeconds = 1800
        }
        ResultSelector = {
          "command_id.$" = "$.Command.CommandId"
        }
        ResultPath = "$.finish"
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed"]
            IntervalSeconds = 10
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "ReceiverFailure"
            ResultPath  = "$.error"
          }
        ]
        Next = "WaitForFinish"
      }

      # Poll every 30 s: RT-STPS batch processing of a full pass is fast
      # (seconds to low minutes), so this should resolve quickly.
      WaitForFinish = {
        Type    = "Wait"
        Seconds = 30
        Next    = "CheckFinishStatus"
      }

      CheckFinishStatus = {
        Type     = "Task"
        Comment  = "Poll the FinishReceiver SSM command status"
        Resource = "arn:aws:states:::aws-sdk:ssm:getCommandInvocation"
        Parameters = {
          "CommandId.$" = "$.finish.command_id"
          InstanceId    = aws_instance.receiver.id
        }
        ResultSelector = {
          "status.$" = "$.Status"
        }
        ResultPath = "$.finish_poll"
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed", "InvocationDoesNotExist"]
            IntervalSeconds = 10
            MaxAttempts     = 3
            BackoffRate     = 1.5
          }
        ]
        Next = "EvaluateFinishStatus"
      }

      EvaluateFinishStatus = {
        Type    = "Choice"
        Comment = "Route based on the FinishReceiver SSM command status"
        Choices = [
          {
            Variable     = "$.finish_poll.status"
            StringEquals = "InProgress"
            Next         = "WaitForFinish"
          },
          {
            Variable     = "$.finish_poll.status"
            StringEquals = "Pending"
            Next         = "WaitForFinish"
          },
          {
            Variable     = "$.finish_poll.status"
            StringEquals = "Success"
            Next         = "StopReceiver"
          }
        ]
        Default = "MarkFinishFailed"
      }

      MarkFinishFailed = {
        Type    = "Pass"
        Comment = "Record why FinishReceiver was considered failed"
        Parameters = {
          "Error"   = "FinishReceiverFailed"
          "Cause.$" = "States.Format('SSM command {} on instance ${aws_instance.receiver.id} finished with status {}', $.finish.command_id, $.finish_poll.status)"
        }
        ResultPath = "$.error"
        Next       = "ReceiverFailure"
      }

      # ── 4. StopReceiver ────────────────────────────────────────────────────
      # The receiver's data is now in S3; stop it so it isn't left running (and
      # billing) between passes. Best-effort -- a stop failure should not fail
      # the whole pipeline when the science data already made it to S3.
      StopReceiver = {
        Type     = "Task"
        Comment  = "Stop the receiver EC2 now that its data is in S3"
        Resource = "arn:aws:states:::aws-sdk:ec2:stopInstances"
        Parameters = {
          InstanceIds = [aws_instance.receiver.id]
        }
        ResultPath = null
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "StartAggregationBuild"
            ResultPath  = "$.stop_error"
          }
        ]
        Next = "StartAggregationBuild"
      }

      # ── 5. StartAggregationBuild ───────────────────────────────────────────
      # CSPP-only build -- RT-STPS already ran on the receiver EC2 in
      # FinishReceiver. Same reasoning as modules/sdr_pipeline for why this is
      # CodeBuild and not EC2: CSPP's sdr_luts.sh needs internet egress the
      # receiver's security group deliberately does not have.
      StartAggregationBuild = {
        Type     = "Task"
        Comment  = "Start the CodeBuild CSPP aggregation job (RDR -> SDR/GEO)"
        Resource = "arn:aws:states:::aws-sdk:codebuild:startBuild"
        Parameters = {
          ProjectName       = aws_codebuild_project.sync_aggregation.name
          BuildspecOverride = file("${path.module}/../../../buildspecs/aggregation_sync.yml")
          EnvironmentVariablesOverride = [
            {
              Name  = "OUTPUT_BUCKET"
              Value = aws_s3_bucket.sync_output.id
              Type  = "PLAINTEXT"
            },
            {
              Name      = "CONTACT_ID"
              "Value.$" = "$.contact_id"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "CONTACT_DATE"
              "Value.$" = "$.contact_date"
              Type      = "PLAINTEXT"
            },
            {
              Name  = "KMS_KEY_ID"
              Value = var.kms_key_id
              Type  = "PLAINTEXT"
            }
          ]
        }
        ResultSelector = {
          "build_id.$" = "$.Build.Id"
        }
        ResultPath = "$.aggregation"
        Retry = [
          {
            ErrorEquals     = ["CodeBuild.CodeBuildException", "States.TaskFailed"]
            IntervalSeconds = 30
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "AggregationFailure"
            ResultPath  = "$.error"
          }
        ]
        Next = "WaitForAggregation"
      }

      # Poll every 60 s: sdr_luts.sh alone runs ~10 min before CSPP itself starts.
      WaitForAggregation = {
        Type    = "Wait"
        Seconds = 60
        Next    = "CheckAggregationBuild"
      }

      CheckAggregationBuild = {
        Type     = "Task"
        Comment  = "Poll the aggregation build status"
        Resource = "arn:aws:states:::aws-sdk:codebuild:batchGetBuilds"
        Parameters = {
          "Ids.$" = "States.Array($.aggregation.build_id)"
        }
        ResultSelector = {
          "build_status.$" = "$.Builds[0].BuildStatus"
        }
        ResultPath = "$.aggregation_poll"
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed"]
            IntervalSeconds = 10
            MaxAttempts     = 3
            BackoffRate     = 1.5
          }
        ]
        Next = "EvaluateAggregation"
      }

      EvaluateAggregation = {
        Type    = "Choice"
        Comment = "Route based on the aggregation build status"
        Choices = [
          {
            Variable     = "$.aggregation_poll.build_status"
            StringEquals = "IN_PROGRESS"
            Next         = "WaitForAggregation"
          },
          {
            Variable     = "$.aggregation_poll.build_status"
            StringEquals = "SUCCEEDED"
            Next         = "StartVisualization"
          }
        ]
        Default = "MarkAggregationFailed"
      }

      MarkAggregationFailed = {
        Type    = "Pass"
        Comment = "Record why the aggregation build was considered failed"
        Parameters = {
          "Error"   = "AggregationBuildFailed"
          "Cause.$" = "States.Format('CodeBuild aggregation build {} finished with status {}', $.aggregation.build_id, $.aggregation_poll.build_status)"
        }
        ResultPath = "$.error"
        Next       = "AggregationFailure"
      }

      # ── 6. StartVisualization ──────────────────────────────────────────────
      # Reuses the existing VIIRS orchestrator Lambda UNMODIFIED -- see
      # lambdas/viirs_visualizer/handler.py. It only parses contact_id and
      # contact_date out of the key, then lists the whole S3 prefix itself; the
      # key does not need to exist. Same "invoke directly, not via S3 events"
      # lesson as modules/sdr_pipeline (see that module's step_functions.tf).
      #
      # Best effort: the science products are already in S3 by this point, so a
      # visualization failure must not mark the pipeline failed.
      StartVisualization = {
        Type     = "Task"
        Comment  = "Invoke the VIIRS visualization orchestrator for this contact"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = "arn:aws:lambda:${data.aws_region.current.id}:${var.account_id}:function:${var.project_name}-viirs-orchestrator"
          Payload = {
            "bucket" = aws_s3_bucket.sync_output.id
            "key.$"  = "States.Format('contacts/{}/{}/manifest.json', $.contact_date, $.contact_id)"
          }
        }
        ResultPath = "$.visualization"
        Retry = [
          {
            ErrorEquals     = ["Lambda.TooManyRequestsException", "Lambda.ServiceException"]
            IntervalSeconds = 5
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "PipelineSucceeded"
            ResultPath  = "$.visualization_error"
          }
        ]
        Next = "PipelineSucceeded"
      }

      # ── 7. Terminal states ─────────────────────────────────────────────────
      PipelineSucceeded = {
        Type    = "Succeed"
        Comment = "Receiver handoff, CSPP aggregation, and visualization complete"
      }

      ReceiverFailure = {
        Type     = "Task"
        Comment  = "Publish receiver handoff failure to SNS and fail the execution"
        Resource = "arn:aws:states:::aws-sdk:sns:publish"
        Parameters = {
          TopicArn = var.sns_topic_arn
          Message = {
            "input.$" = "$$.Execution.Input"
            "error.$" = "$.error"
            "stage"   = "FinishReceiver"
          }
          Subject = "Sync Pipeline — Receiver Handoff Failed"
        }
        ResultPath = null
        Next       = "FailExecution"
      }

      AggregationFailure = {
        Type     = "Task"
        Comment  = "Publish aggregation failure to SNS and fail the execution"
        Resource = "arn:aws:states:::aws-sdk:sns:publish"
        Parameters = {
          TopicArn = var.sns_topic_arn
          Message = {
            "input.$" = "$$.Execution.Input"
            "error.$" = "$.error"
            "stage"   = "StartAggregationBuild"
          }
          Subject = "Sync Pipeline — CSPP Aggregation Failed"
        }
        ResultPath = null
        Next       = "FailExecution"
      }

      FailExecution = {
        Type  = "Fail"
        Error = "SyncPipelineFailure"
        Cause = "Pipeline failed — see SNS notification for details"
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-pipeline"
    Service = "sync-pipeline"
  })
}
