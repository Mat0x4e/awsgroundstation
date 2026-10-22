# step_functions.tf — Step Functions state machine for the SDR pipeline
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
#
# Flow: DeriveDateOnly → DeriveDateParts → BuildContactDate → WaitForDelivery → ListChunks → ShapeInput
#       → CheckChunksFound → CheckProcessingMarker → WriteProcessingMarker
#       → ParallelProcessing (Map) → CheckResults → StartAggregationBuild
#       → (success) or TotalFailure (SNS + Fail)
# Idempotence: CheckProcessingMarker short-circuits if a .processing marker exists.

###############################################################################
# CloudWatch Log Group — Step Functions execution logs
###############################################################################

resource "aws_cloudwatch_log_group" "sfn" {
  name = "/aws/states/${var.project_name}-sdr-pipeline"
  # checkov:skip=CKV_AWS_338: 90-day retention is sufficient for pipeline debug logs —
  # satellite contact data is the permanent record (stored in S3 with lifecycle policies)
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-pipeline-logs"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# State Machine
###############################################################################

# checkov:skip=CKV_AWS_284: X-Ray tracing not needed — CloudWatch execution logging
# at level=ALL provides sufficient observability for this batch pipeline
resource "aws_sfn_state_machine" "sdr_pipeline" {
  name     = "${var.project_name}-sdr-pipeline"
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "NOAA-20 CADU-to-TIFF SDR pipeline — chunk processing + final aggregation"
    # 180 min (10800s) headroom rationale:
    # - Batch fan-out: 25 chunks / MaxConcurrency=19 => 2 waves. Each fresh Spot instance
    #   pulls the ~20 GiB SDR image (~13 min cold) then processes (~23 min) => ~70-75 min
    #   worst case. Warm-instance reuse is faster but cannot be relied on for sizing.
    # - Batch aggregation (on-demand): ~25 min PERF-1 cache hit, up to ~40 min on a cold
    #   sdr_luts.sh fallback.
    # - Plus WaitForDelivery (120s) + visualization dispatch.
    # => realistic worst case ~110-115 min; 180 min gives comfortable headroom so a single
    #    slow straggler chunk does not kill the whole execution (the original 90 min / 5400s
    #    limit was sized for the CodeBuild era and was exceeded in a Stage 2 validation run
    #    where 24/25 Map iterations succeeded and 1 straggler pushed total past 5400s).
    TimeoutSeconds = 10800
    StartAt        = "DeriveDateOnly"

    States = {

      # ── 1. Derive contact_date from the event timestamp ────────────────────
      # The trigger supplies the contact id and the event time; the S3 layout is
      # keyed by date, so "2026-09-06T11:57:59Z" has to become "2026/09/06".
      # EventBridge input transformers substitute whole values and cannot
      # reformat a timestamp, so it happens here.
      #
      # Three small Pass states rather than one expression: Step Functions
      # rejects deeply nested intrinsics ("must be a valid JSONPath or a valid
      # intrinsic function call"), so each step nests at most one call and the
      # rest is plain JSONPath indexing.
      #
      # A contact straddling midnight UTC would have chunks under two date
      # prefixes; NOAA-20 daytime passes over this station run ~10-12 UTC, so
      # that case is not handled.
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
          "date_parts.$"   = "$.date_parts"
          "contact_date.$" = "States.Format('{}/{}/{}', $.date_parts[0], $.date_parts[1], $.date_parts[2])"
        }
        Next = "WaitForDelivery"
      }
      # ── 2. WaitForDelivery ─────────────────────────────────────────────────
      # COMPLETED fires at LOS, but Ground Station is still flushing the last
      # chunk: on contact ba2c5446 the final .pcap was written 19 s after LOS.
      # Wait before listing so the chunk set is complete.
      WaitForDelivery = {
        Type    = "Wait"
        Seconds = 120
        Next    = "ListChunks"
      }

      # ── 3. ListChunks ──────────────────────────────────────────────────────
      # Actually list them. This state was previously a Pass that assumed the
      # caller supplied chunks[] -- true for a hand-built input, never true for
      # an event-driven one.
      #
      # The contact id is in the OBJECT NAME, not a path segment (the key is
      # year=Y/month=M/day=D/satellite=<sat>/<contactId>_<ts>_<uuid>.pcap), so
      # prefixing with "<contactId>_" is what isolates one contact's chunks.
      ListChunks = {
        Type     = "Task"
        Comment  = "List this contact's .pcap chunks in the reception bucket"
        Resource = "arn:aws:states:::aws-sdk:s3:listObjectsV2"
        Parameters = {
          "Bucket.$" = "$.bucket"
          "Prefix.$" = "States.Format('year={}/month={}/day={}/satellite={}/{}_', $.date_parts[0], $.date_parts[1], $.date_parts[2], $.satellite_id, $.contact_id)"
        }
        ResultSelector = {
          "chunks.$" = "$.Contents[*].Key"
        }
        ResultPath = "$.listing"
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed"]
            IntervalSeconds = 10
            MaxAttempts     = 3
            BackoffRate     = 2.0
          }
        ]
        Next = "ShapeInput"
      }

      # ── 4. ShapeInput ──────────────────────────────────────────────────────
      # Flatten to the shape every downstream state already expects:
      # { contact_id, bucket, contact_date, chunks[] }.
      ShapeInput = {
        Type    = "Pass"
        Comment = "Normalise into { contact_id, bucket, contact_date, chunks }"
        Parameters = {
          "contact_id.$"   = "$.contact_id"
          "bucket.$"       = "$.bucket"
          "contact_date.$" = "$.contact_date"
          "chunks.$"       = "$.listing.chunks"
        }
        Next = "CheckChunksFound"
      }

      # ── 5. CheckChunksFound ────────────────────────────────────────────────
      # An empty listing means the contact delivered nothing (or the prefix is
      # wrong). Fail loudly rather than running a Map over zero items and
      # reporting success.
      CheckChunksFound = {
        Type    = "Choice"
        Comment = "Abort if the contact produced no .pcap chunks"
        Choices = [
          {
            Variable  = "$.chunks[0]",
            IsPresent = true,
            Next      = "CheckProcessingMarker"
          }
        ]
        Default = "NoChunksFound"
      }

      NoChunksFound = {
        Type     = "Task"
        Comment  = "Publish an empty-contact failure to SNS and fail"
        Resource = "arn:aws:states:::aws-sdk:sns:publish"
        Parameters = {
          TopicArn = var.sns_topic_arn
          Message = {
            "input.$" = "$$.Execution.Input"
            "stage"   = "ListChunks"
            "reason"  = "No .pcap objects found for this contact in the reception bucket"
          }
          Subject = "SDR Pipeline — contact delivered no data"
        }
        ResultPath = null
        Next       = "FailExecution"
      }
      # ── 2. CheckProcessingMarker ───────────────────────────────────────────
      # HeadObject on the .processing marker. If it exists the state machine
      # was already started for this contact — short-circuit via AlreadyProcessing.
      # S3.NoSuchKey (marker absent) → proceed to WriteProcessingMarker.
      CheckProcessingMarker = {
        Type     = "Task"
        Comment  = "Check whether a .processing marker already exists for this contact"
        Resource = "arn:aws:states:::aws-sdk:s3:headObject"
        Parameters = {
          Bucket  = aws_s3_bucket.sdr_output.id
          "Key.$" = "States.Format('contacts/{}/.processing', $.contact_id)"
        }
        ResultPath = null
        Catch = [
          {
            ErrorEquals = ["S3.NoSuchKeyException"]
            Next        = "WriteProcessingMarker"
            ResultPath  = null
          }
        ]
        Next = "AlreadyProcessing"
      }

      # ── 3. WriteProcessingMarker ───────────────────────────────────────────
      WriteProcessingMarker = {
        Type     = "Task"
        Comment  = "Write .processing marker to claim this contact for processing"
        Resource = "arn:aws:states:::aws-sdk:s3:putObject"
        Parameters = {
          Bucket      = aws_s3_bucket.sdr_output.id
          "Key.$"     = "States.Format('contacts/{}/.processing', $.contact_id)"
          Body        = "processing"
          ContentType = "text/plain"
        }
        ResultPath = null
        Next       = "ParallelProcessing"
      }

      # ── 4. ParallelProcessing (Map) ────────────────────────────────────────
      # Iterate over each chunk key. MaxConcurrency 19 matches the number of
      # satellite passes that can be scheduled simultaneously.
      # ToleratedFailurePercentage 100 keeps the Map from failing if individual
      # chunks error out — failures are tracked per-item and checked later.
      ParallelProcessing = {
        Type                       = "Map"
        Comment                    = "Process each chunk in parallel, up to 19 at a time"
        ItemsPath                  = "$.chunks"
        MaxConcurrency             = 19
        ToleratedFailurePercentage = 100

        ItemSelector = {
          "chunk_key.$" = "$$.Map.Item.Value"
          "chunk_id.$"  = "$$.Map.Item.Index"
          # Read from the state data, NOT $$.Execution.Input. contact_date does
          # not exist in the execution input: the trigger supplies contact_time
          # and BuildContactDate derives the date inside the machine. Reading it
          # from the execution input failed the whole Map with States.Runtime
          # "The JSONPath '$$.Execution.Input.contact_date' ... could not be
          # found" (rehearsal, 2026-09-01). contact_id and bucket happen to be
          # present in the input, but the post-ShapeInput state data is the
          # authoritative copy, so all three are read consistently from it.
          "contact_id.$"   = "$.contact_id"
          "contact_date.$" = "$.contact_date"
          "input_bucket.$" = "$.bucket"
          "output_bucket"  = aws_s3_bucket.sdr_output.id
          "kms_key_id"     = var.kms_key_arn
        }

        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "SubmitBatchChunk"
          States = {

            # Submit one single-container Batch chunk job via the synchronous
            # submitJob.sync integration. Replaces the 6-state poll loop
            # (StartCodeBuild -> WaitForBuild -> CheckBuildStatus ->
            # EvaluateBuildStatus) with a single task state.
            #
            # CRITICAL (canary blocker #6): Batch Ref:: substitution only works
            # inside the job definition 'command' array, NOT in environment[].value.
            # All seven values MUST be passed here via ContainerOverrides.Environment.
            # This is exactly what made the canary's container-overrides run succeed.
            SubmitBatchChunk = {
              Type     = "Task"
              Comment  = "Submit one Batch chunk job (clean-compute-pipeline Stage 1)"
              Resource = "arn:aws:states:::batch:submitJob.sync"
              Parameters = {
                # chunk_id is a Map item index (integer) so 'chunk-N' is always valid
                # Batch job name characters: [A-Za-z0-9_-], max 128 chars.
                "JobName.$"   = "States.Format('chunk-{}', $.chunk_id)"
                JobQueue      = var.batch_fanout_job_queue_arn
                JobDefinition = var.batch_chunk_job_definition_arn
                ContainerOverrides = {
                  Environment = [
                    {
                      Name      = "INPUT_BUCKET"
                      "Value.$" = "$.input_bucket"
                    },
                    {
                      Name      = "INPUT_KEY"
                      "Value.$" = "$.chunk_key"
                    },
                    {
                      Name      = "OUTPUT_BUCKET"
                      "Value.$" = "$.output_bucket"
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
                      Name      = "CHUNK_ID"
                      "Value.$" = "States.Format('{}', $.chunk_id)"
                    },
                    {
                      Name      = "KMS_KEY_ID"
                      "Value.$" = "$.kms_key_id"
                    }
                  ]
                }
              }
              ResultPath = "$.batch"
              Retry = [
                {
                  # States.TaskFailed covers SFN API errors; Batch.AWSBatchException
                  # covers transient Batch submit/status errors (not Spot reclaims --
                  # those are handled by the retryStrategy in the job definition).
                  ErrorEquals     = ["States.TaskFailed", "Batch.AWSBatchException"]
                  IntervalSeconds = 30
                  MaxAttempts     = 2
                  BackoffRate     = 2.0
                }
              ]
              Catch = [
                {
                  # Mirror the CodeBuild path: a Batch failure marks the chunk failed
                  # without failing the Map, preserving ToleratedFailurePercentage=100.
                  ErrorEquals = ["States.ALL"]
                  Next        = "MarkChunkFailed"
                  ResultPath  = "$.error"
                }
              ]
              # submitJob.sync returning without error == chunk succeeded.
              # Reuse the existing terminal Pass state (no new state needed).
              Next = "BuildSucceeded"
            }

            # Chunk succeeded — end this iteration successfully
            BuildSucceeded = {
              Type = "Pass"
              Parameters = {
                "chunk_id.$"   = "$.chunk_id"
                "chunk_key.$"  = "$.chunk_key"
                "build_status" = "SUCCEEDED"
              }
              End = true
            }

            # Chunk failed — record failure but do not fail the Map
            MarkChunkFailed = {
              Type = "Pass"
              Parameters = {
                "chunk_id.$"   = "$.chunk_id"
                "chunk_key.$"  = "$.chunk_key"
                "build_status" = "FAILED"
              }
              End = true
            }
          }
        }

        ResultPath = "$.chunk_results"
        Next       = "CheckResults"
      }

      # ── 5. CheckResults ────────────────────────────────────────────────────
      # Always proceed to Batch aggregation. The ChooseAggregationCompute gate
      # and the CodeBuild poll-loop (StartAggregationBuild / WaitForAggregation /
      # CheckAggregationBuild / EvaluateAggregation / MarkAggregationFailed) were
      # retired in task 8 (clean-compute-pipeline Stage 2): both fan-out and
      # aggregation now run on Batch. ToleratedFailurePercentage=100 on the Map
      # ensures we always reach this state regardless of chunk failures.
      CheckResults = {
        Type    = "Pass"
        Comment = "Always proceed to Batch aggregation (CodeBuild async path retired, task 8)"
        Next    = "SubmitBatchAggregation"
      }

      # ── 5a. SubmitBatchAggregation ────────────────────────────────────────
      # Batch on-demand aggregation job: CADU -> RT-STPS -> CSPP (RTSTPS=on).
      # Uses :::batch:submitJob.sync -- returning without error means the job
      # SUCCEEDED, so the CodeBuild poll-loop (WaitForAggregation /
      # CheckAggregationBuild / EvaluateAggregation) is bypassed entirely on
      # this path.
      #
      # CRITICAL: Batch Ref:: substitution only works inside job-def command[],
      # NOT in environment[].value. All per-contact values are passed here via
      # ContainerOverrides.Environment (mirrors the SubmitBatchChunk pattern).
      SubmitBatchAggregation = {
        Type     = "Task"
        Comment  = "Submit Batch aggregation job (CADU -> RT-STPS -> CSPP, Stage 2)"
        Resource = "arn:aws:states:::batch:submitJob.sync"
        Parameters = {
          # contact_id is a UUID (hyphens are valid Batch job name chars)
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
                Name  = "OUTPUT_BUCKET"
                Value = aws_s3_bucket.sdr_output.id
              },
              {
                Name      = "RECEPTION_BUCKET"
                "Value.$" = "$.bucket"
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
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed", "Batch.AWSBatchException"]
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
        Next = "StartVisualization"
      }

      # ── 7. StartVisualization ──────────────────────────────────────────────
      # Tell the orchestrator which contact just completed, rather than letting
      # it discover work from S3 object events.
      #
      # It used to be triggered by an EventBridge rule matching every .png in the
      # output bucket. A contact writes ~9,150 objects, most of them composites,
      # so switching that bucket to EventBridge produced ~6,000 CodeBuild builds
      # in a morning and exhausted the account build queue (2026-09-01). The
      # orchestrator only ever needed the contact id and date: it lists the whole
      # prefix itself and submits one build.
      #
      # The key does not need to exist -- the handler parses contact_id and
      # contact_date out of it and then lists the prefix.
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
            "bucket" = aws_s3_bucket.sdr_output.id
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
            # Repointed from PipelineSucceeded to NotifyVisualizationFailed (R4-AC1).
            # The notify state publishes an SNS message and then unconditionally
            # proceeds to PipelineSucceeded, preserving R4-AC2 (pipeline SUCCEEDS).
            Next       = "NotifyVisualizationFailed"
            ResultPath = "$.visualization_error"
          }
        ]
        Next = "PipelineSucceeded"
      }

      # ── 7a. NotifyVisualizationFailed ─────────────────────────────────────
      # Catch target for StartVisualization's States.ALL handler (R4-AC1).
      # Publishes an SNS notification describing the failure, then unconditionally
      # proceeds to PipelineSucceeded — products are already in S3 (R4-AC2).
      # The log_metric_filter in monitoring.tf fires on this state's entry in
      # the SFN execution log, incrementing the VisualizationFailed metric (R4-AC3).
      # No new IAM required: the SFN role already holds sns:Publish on this topic
      # (see iam.tf SNSPublish statement, also used by AggregationFailure / NoChunksFound).
      NotifyVisualizationFailed = {
        Type     = "Task"
        Comment  = "Publish viz-failure SNS notification — products preserved, pipeline still SUCCEEDS"
        Resource = "arn:aws:states:::aws-sdk:sns:publish"
        Parameters = {
          TopicArn = var.sns_topic_arn
          Message = {
            "input.$"               = "$$.Execution.Input"
            "visualization_error.$" = "$.visualization_error"
            "stage"                 = "StartVisualization"
          }
          Subject = "SDR Pipeline — Visualization failed (products preserved, pipeline still succeeded)"
        }
        ResultPath = null
        Next       = "PipelineSucceeded"
      }

      # ── 8. PipelineSucceeded ───────────────────────────────────────────────
      PipelineSucceeded = {
        Type    = "Succeed"
        Comment = "All chunks processed and aggregation complete"
      }

      # ── 9. AggregationFailure ──────────────────────────────────────────────
      AggregationFailure = {
        Type     = "Task"
        Comment  = "Publish aggregation failure to SNS and fail the execution"
        Resource = "arn:aws:states:::aws-sdk:sns:publish"
        Parameters = {
          TopicArn = var.sns_topic_arn
          Message = {
            "input.$" = "$$.Execution.Input"
            "error.$" = "$.error"
            "stage"   = "FinalAggregation"
          }
          Subject = "SDR Pipeline — Final Aggregation Failed"
        }
        ResultPath = null
        Next       = "FailExecution"
      }

      # Shared terminal Fail state
      FailExecution = {
        Type  = "Fail"
        Error = "SDRPipelineFailure"
        Cause = "Pipeline failed — see SNS notification for details"
      }

      # ── 10. AlreadyProcessing ──────────────────────────────────────────────
      AlreadyProcessing = {
        Type    = "Succeed"
        Comment = "A .processing marker already exists — this contact is already being processed (idempotent exit)"
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-pipeline"
    Service = "sdr-pipeline"
  })
}
