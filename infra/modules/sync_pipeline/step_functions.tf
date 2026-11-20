# step_functions.tf — Step Functions state machine for the synchronous pipeline
#
# Flow: DeriveDateOnly -> DeriveDateParts -> BuildContactDate -> WaitForDelivery
#       -> FinishReceiver (SSM: stop listener, upload the CADU frame capture)
#       -> WaitForFinish -> CheckFinishStatus -> StopReceiver
#       -> SubmitBatchAggregation (shared Batch job: RT-STPS -> RDR -> CSPP)
#       -> StartVisualization -> PipelineSucceeded
#       -> (any stage) FailureNotify -> FailExecution
#
# Architecture principle: sync and async converge at the first common artifact
# (the frame capture in S3) and share everything after it. This state machine
# only owns what is sync-specific: the receiver handoff. Aggregation is the
# SAME Batch queue + job definition as the async pipeline, parameterised by env
# overrides (CADU_PREFIX, PN_ENCODED, OUTPUT_BUCKET, ASSETS_BUCKET). There is
# no chunk fan-out Map state: the receiver writes one combined CADU capture per
# contact (see scripts/sync_receiver_finish.sh).
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
    Comment = "NOAA-20 synchronous demod/decode pipeline — receiver handoff + shared Batch aggregation + visualization"
    # Budget (worst case, single aggregation attempt):
    #   WaitForDelivery                     30 s
    #   FinishReceiver (SSM executionTimeout) 1800 s
    #   WaitForFinish/CheckFinishStatus poll   ~60 s (30 s cadence + last poll)
    #   StopReceiver                          ~10 s
    #   SubmitBatchAggregation (Batch attempt_duration_seconds) 3600 s
    #   StartVisualization (+ 2 retries 5 s/10 s)  ~60 s
    #   NotifyVisualizationFailed / SNS             ~5 s
    #   total                               ~5565 s
    # 7200 s leaves ~27 percent headroom, so Batch's own 3600 s timeout always
    # fires first and the Catch/SNS path runs (the previous 5400 s was below the
    # worst case, and Step Functions timeouts skip Catch blocks).
    TimeoutSeconds = 7200
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
      # listener, assembles the capture into a combined CADU and uploads it
      # (+ the raw capture) to S3. RT-STPS is NOT run here any more: it is part
      # of the shared Batch aggregation chain (see SubmitBatchAggregation).
      FinishReceiver = {
        Type     = "Task"
        Comment  = "SSM: stop listener, upload the CADU frame capture"
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

      # Poll every 30 s: assembling and uploading the capture is fast
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
            Next        = "SubmitBatchAggregation"
            ResultPath  = "$.stop_error"
          }
        ]
        Next = "SubmitBatchAggregation"
      }

      # ── 5. SubmitBatchAggregation ─────────────────────────────────────────
      # The SHARED aggregation chain (architecture principle: both downlink
      # paths converge at the first common artifact -- the frame capture in S3
      # -- and share everything after it). Same Batch queue + job definition as
      # the async pipeline: frames in S3 -> RT-STPS -> RDR -> CSPP -> SDR/GEO.
      # Sync differences are expressed ONLY as env overrides:
      #   CADU_PREFIX=cadu   frames live under contacts/.../cadu/ (async: satdump/)
      #   PN_ENCODED=true    the demod/decode egress is not PN-derandomized, so
      #                      the image's pristine jpss1.pn-encoded.xml is used
      #   OUTPUT_BUCKET      the sync bucket (frames in, rdr/sdr/logs out)
      #   ASSETS_BUCKET      the SDR output bucket, which holds software/ (CSPP
      #                      J01 LUTs) and lut-cache/ -- the sync bucket has neither
      #
      # CRITICAL: Batch Ref:: substitution only works inside job-def command[],
      # NOT in environment[].value. All per-contact values are passed via
      # ContainerOverrides.Environment (mirrors the sdr_pipeline pattern).
      SubmitBatchAggregation = {
        Type     = "Task"
        Comment  = "Submit the shared Batch aggregation job (frames -> RT-STPS -> RDR -> CSPP -> SDR/GEO)"
        Resource = "arn:aws:states:::batch:submitJob.sync"
        Parameters = {
          "JobName.$"   = "States.Format('agg-{}', $.contact_id)"
          JobQueue      = var.batch_aggregation_job_queue_arn
          JobDefinition = var.batch_aggregation_job_definition_arn
          ContainerOverrides = {
            Environment = [
              {
                Name  = "RTSTPS"
                Value = "on"
              },
              {
                Name  = "CADU_PREFIX"
                Value = "cadu"
              },
              {
                Name  = "PN_ENCODED"
                Value = "true"
              },
              {
                Name  = "OUTPUT_BUCKET"
                Value = aws_s3_bucket.sync_output.id
              },
              {
                Name  = "ASSETS_BUCKET"
                Value = var.assets_bucket_name
              },
              {
                Name      = "CONTACT_ID"
                "Value.$" = "$.contact_id"
              },
              {
                Name      = "CONTACT_DATE"
                "Value.$" = "$.contact_date"
              },
              {
                Name  = "KMS_KEY_ID"
                Value = var.kms_key_arn
              }
            ]
          }
        }
        ResultPath = "$.aggregation_batch"
        # Deliberately NO Retry: a single attempt, so a failed or timed-out Batch
        # job (attempt_duration_seconds = 3600, enforced by Batch itself) always
        # lands on the Catch -> AggregationFailure -> SNS path. A retry cannot
        # finish inside the execution budget (see TimeoutSeconds) and a Step
        # Functions timeout skips Catch blocks (silent loss of the pass).
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "AggregationFailure"
            ResultPath  = "$.error"
          }
        ]
        Next = "StartVisualization"
      }
      # ── 6. StartVisualization ──────────────────────────────────────────────
      # Reuses the existing VIIRS orchestrator Lambda UNMODIFIED -- see
      # lambdas/viirs_visualizer/handler.py. It only parses contact_id and
      # contact_date out of the key, then lists the whole S3 prefix itself; the
      # key does not need to exist. Same "invoke directly, not via S3 events"
      # lesson as modules/sdr_pipeline (see that module's step_functions.tf).
      #
      # Best effort: the science products are already in S3 by this point, so a
      # visualization failure must not mark the pipeline failed. It is NOT silent
      # though: the Catch routes to NotifyVisualizationFailed (SNS + metric), as in
      # the async machine. The event carries the sync bucket as "bucket"; the
      # orchestrator uses it for the listing and the render job INPUT_BUCKET.
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
            Next        = "NotifyVisualizationFailed"
            ResultPath  = "$.visualization_error"
          }
        ]
        Next = "PipelineSucceeded"
      }

      # ── 6a. NotifyVisualizationFailed ──────────────────────────────────────
      # Catch target for StartVisualization. Mirrors the async machine: publish
      # an SNS notification, then proceed to PipelineSucceeded (products are
      # already in S3). The metric filter in monitoring.tf counts entries into
      # this state and drives the visualization-failed alarm.
      NotifyVisualizationFailed = {
        Type     = "Task"
        Comment  = "Publish viz-failure SNS notification - products preserved, pipeline still SUCCEEDS"
        Resource = "arn:aws:states:::aws-sdk:sns:publish"
        Parameters = {
          TopicArn = var.sns_topic_arn
          Message = {
            "input.$"               = "$$.Execution.Input"
            "visualization_error.$" = "$.visualization_error"
            "stage"                 = "StartVisualization"
          }
          Subject = "Sync Pipeline - Visualization failed (products preserved, pipeline still succeeded)"
        }
        ResultPath = null
        Next       = "PipelineSucceeded"
      }

      # ── 7. Terminal states ─────────────────────────────────────────────────
      PipelineSucceeded = {
        Type    = "Succeed"
        Comment = "Receiver handoff, shared Batch aggregation, and visualization complete"
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
            "stage"   = "SubmitBatchAggregation"
          }
          Subject = "Sync Pipeline — Aggregation Failed"
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
