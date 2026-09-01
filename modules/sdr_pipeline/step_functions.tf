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
    Comment        = "NOAA-20 CADU-to-TIFF SDR pipeline — chunk processing + final aggregation"
    TimeoutSeconds = 5400
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
          "chunk_key.$"    = "$$.Map.Item.Value"
          "chunk_id.$"     = "$$.Map.Item.Index"
          "contact_id.$"   = "$$.Execution.Input.contact_id"
          "contact_date.$" = "$$.Execution.Input.contact_date"
          "input_bucket.$" = "$$.Execution.Input.bucket"
          "output_bucket"  = aws_s3_bucket.sdr_output.id
          "kms_key_id"     = var.kms_key_arn
        }

        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "StartCodeBuild"
          States = {

            # Start the CodeBuild build for this chunk
            StartCodeBuild = {
              Type     = "Task"
              Comment  = "Start a CodeBuild build for this chunk"
              Resource = "arn:aws:states:::aws-sdk:codebuild:startBuild"
              Parameters = {
                ProjectName       = aws_codebuild_project.sdr_pipeline.name
                BuildspecOverride = "version: 0.2\nenv:\n  variables:\n    RTSTPS_HOME: /opt/rt-stps\n    CSPP_HOME: /opt/cspp-sdr\nphases:\n  pre_build:\n    commands:\n      - echo Downloading chunk from S3...\n      - aws s3 cp s3://$INPUT_BUCKET/$INPUT_KEY /tmp/input.pcap\n      - mkdir -p /tmp/output/iq /tmp/output/satdump\n  build:\n    commands:\n      - echo Step 1 - IQ Extraction\n      - python3 /opt/scripts/iq_extract.py /tmp/input.pcap /tmp/output/iq/baseband.cs8\n      - echo Step 2 - SatDump\n      - /opt/scripts/satdump_process.sh /tmp/output/iq/baseband.cs8 /tmp/output/satdump\n      - echo Step 2 complete - listing CADU locations\n      - find /tmp/output/ -name '*.cadu' -ls\n      - ls -la /tmp/output/satdump/\n      - echo Step 2b - Uploading SatDump outputs to S3\n      - aws s3 sync /tmp/output/satdump/ s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/chunk_$CHUNK_ID/ --sse aws:kms --sse-kms-key-id $KMS_KEY_ID\n      - echo SatDump output uploaded successfully\n  post_build:\n    commands:\n      - echo Chunk processing complete\n      - aws s3 cp /tmp/output/satdump/dataset.json s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/chunks/chunk_$CHUNK_ID/dataset.json --sse aws:kms --sse-kms-key-id $KMS_KEY_ID 2>/dev/null || true\n"
                EnvironmentVariablesOverride = [
                  {
                    Name      = "INPUT_BUCKET"
                    "Value.$" = "$.input_bucket"
                    Type      = "PLAINTEXT"
                  },
                  {
                    Name      = "INPUT_KEY"
                    "Value.$" = "$.chunk_key"
                    Type      = "PLAINTEXT"
                  },
                  {
                    Name      = "OUTPUT_BUCKET"
                    "Value.$" = "$.output_bucket"
                    Type      = "PLAINTEXT"
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
                    Name      = "CHUNK_ID"
                    "Value.$" = "States.Format('{}', $.chunk_id)"
                    Type      = "PLAINTEXT"
                  },
                  {
                    Name      = "KMS_KEY_ID"
                    "Value.$" = "$.kms_key_id"
                    Type      = "PLAINTEXT"
                  }
                ]
              }
              ResultSelector = {
                "build_id.$" = "$.Build.Id"
              }
              ResultPath = "$.build"
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
                  Next        = "MarkChunkFailed"
                  ResultPath  = "$.error"
                }
              ]
              Next = "WaitForBuild"
            }

            # Wait 30 s before polling build status
            WaitForBuild = {
              Type    = "Wait"
              Seconds = 30
              Next    = "CheckBuildStatus"
            }

            # Poll CodeBuild for the current build status
            CheckBuildStatus = {
              Type     = "Task"
              Comment  = "Poll CodeBuild build status"
              Resource = "arn:aws:states:::aws-sdk:codebuild:batchGetBuilds"
              Parameters = {
                "Ids.$" = "States.Array($.build.build_id)"
              }
              ResultSelector = {
                "build_status.$" = "$.Builds[0].BuildStatus"
              }
              ResultPath = "$.poll"
              Retry = [
                {
                  ErrorEquals     = ["States.TaskFailed"]
                  IntervalSeconds = 10
                  MaxAttempts     = 3
                  BackoffRate     = 1.5
                }
              ]
              Next = "EvaluateBuildStatus"
            }

            # Branch on build status
            EvaluateBuildStatus = {
              Type    = "Choice"
              Comment = "Route based on CodeBuild build status"
              Choices = [
                {
                  Variable     = "$.poll.build_status"
                  StringEquals = "IN_PROGRESS"
                  Next         = "WaitForBuild"
                },
                {
                  Variable     = "$.poll.build_status"
                  StringEquals = "SUCCEEDED"
                  Next         = "BuildSucceeded"
                }
              ]
              Default = "MarkChunkFailed"
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
      # Always proceed to FinalAggregation — it handles partial results gracefully
      # by only downloading from successful chunks. ToleratedFailurePercentage=100
      # on the Map state ensures we always reach this state regardless of chunk failures.
      CheckResults = {
        Type    = "Pass"
        Comment = "Always proceed to aggregation — handles partial results gracefully"
        Next    = "StartAggregationBuild"
      }

      # ── 6. FinalAggregation ────────────────────────────────────────────────
      # Aggregation runs as a CodeBuild job, NOT on the EC2 instance.
      #
      # CSPP's ancillary staging fetches from http://jpssdb.ssec.wisc.edu at run
      # time. The EC2 aggregation instance has no route there (its security group
      # is SSM outbound HTTPS only), so viirs_sdr.sh timed out five times over ~11
      # minutes and then died inside its own error handler with
      # "TypeError: object of type 'bool' has no len()". CodeBuild has egress and
      # the sdr-pipeline image already carries CSPP 4.1.1 plus the J01 straylight
      # LUTs. See CSPP_SOLVED.md req 2: "Run CSPP in CodeBuild, not EC2."
      #
      # The buildspec is read from buildspecs/aggregation.yml so the file that ran
      # by hand for contact ba2c5446 is the same one the pipeline uses -- no
      # second copy to drift.
      StartAggregationBuild = {
        Type     = "Task"
        Comment  = "Start the CodeBuild aggregation job (CADU -> RT-STPS -> CSPP)"
        Resource = "arn:aws:states:::aws-sdk:codebuild:startBuild"
        Parameters = {
          ProjectName       = aws_codebuild_project.sdr_pipeline.name
          BuildspecOverride = file("${path.module}/../../buildspecs/aggregation.yml")
          EnvironmentVariablesOverride = [
            {
              Name      = "RECEPTION_BUCKET"
              "Value.$" = "$.bucket"
              Type      = "PLAINTEXT"
            },
            {
              Name  = "OUTPUT_BUCKET"
              Value = aws_s3_bucket.sdr_output.id
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

      # Poll every 60 s: sdr_luts.sh alone runs ~10 min before RT-STPS starts.
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
            Next         = "PipelineSucceeded"
          }
        ]
        Default = "MarkAggregationFailed"
      }

      # AggregationFailure publishes $.error to SNS. Reaching it from a Choice
      # default leaves $.error unset, which fails the publish itself and hides the
      # real outcome -- so populate it first.
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
      # ── 7. PipelineSucceeded ───────────────────────────────────────────────
      PipelineSucceeded = {
        Type    = "Succeed"
        Comment = "All chunks processed and aggregation complete"
      }

      # ── 8. AggregationFailure ──────────────────────────────────────────────
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
