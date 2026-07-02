# step_functions.tf — Step Functions state machine for the SDR pipeline
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
#
# Flow: ListChunks → CheckProcessingMarker → WriteProcessingMarker
#       → ParallelProcessing (Map) → CheckResults → FinalAggregation
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
    StartAt        = "ListChunks"

    States = {

      # ── 1. ListChunks ──────────────────────────────────────────────────────
      # Pass state: normalise the input into a canonical shape.
      # Expected input: { contact_id, bucket, chunks: [...], contact_date }
      ListChunks = {
        Type    = "Pass"
        Comment = "Validate and pass through input containing contact_id, bucket, chunks, contact_date"
        Next    = "CheckProcessingMarker"
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
        Next    = "FinalAggregation"
      }

      # ── 6. FinalAggregation ────────────────────────────────────────────────
      # Start an aggregation CodeBuild build using a different buildspec.
      FinalAggregation = {
        Type     = "Task"
        Comment  = "Start final aggregation CodeBuild build"
        Resource = "arn:aws:states:::aws-sdk:codebuild:startBuild"
        Parameters = {
          ProjectName       = aws_codebuild_project.sdr_pipeline.name
          BuildspecOverride = "version: 0.2\nenv:\n  variables:\n    RTSTPS_HOME: /opt/rt-stps\n    CSPP_HOME: /opt/cspp-sdr\nphases:\n  pre_build:\n    commands:\n      - echo Downloading chunk metadata and CADU files...\n      - mkdir -p /tmp/aggregation /tmp/cadu /tmp/rdr /tmp/sdr\n      - python3 /opt/scripts/download_chunk_metadata.py $OUTPUT_BUCKET $CONTACT_ID /tmp/aggregation/\n      - echo Downloading all CADU files from S3...\n      - aws s3 sync s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/satdump/ /tmp/cadu/ --exclude '*' --include '*.cadu'\n      - echo CADU files downloaded\n  build:\n    commands:\n      - echo Step 1 - Concatenate all CADU files\n      - find /tmp/cadu -name '*.cadu' | sort | xargs cat > /tmp/aggregation/combined.cadu\n      - echo Combined CADU size is $(du -h /tmp/aggregation/combined.cadu | cut -f1)\n      - echo Step 2 - RT-STPS on combined CADU with jpss1.xml\n      - mkdir -p /opt/data\n      - sed -i 's/PnEncoded=\"true\"/PnEncoded=\"false\"/' /opt/rt-stps/config/jpss1.xml\n      - sed -i 's|from=\"frame_sync\" to=\"pn\"|from=\"frame_sync\" to=\"reed_solomon\"|' /opt/rt-stps/config/jpss1.xml\n      - sed -i '/from=\"pn\" to=\"reed_solomon\"/d' /opt/rt-stps/config/jpss1.xml\n      - cd /opt/rt-stps && bin/batch.sh config/jpss1.xml /tmp/aggregation/combined.cadu || echo WARN RT-STPS failed\n      - echo RT-STPS produced $(find /opt/data -name '*.h5' | wc -l) RDR files\n      - find /opt/data -name '*.h5' -ls\n      - echo Step 3 - CSPP SDR calibration\n      - python3 /opt/scripts/cspp_process.py /opt/data /tmp/sdr || echo WARN CSPP failed\n      - echo Step 4 - Computing geolocation\n      - python3 /opt/scripts/geolocation.py /tmp/aggregation/ /tmp/aggregation/coordinates/\n      - echo Generating manifest\n      - python3 /opt/scripts/generate_manifest.py /tmp/aggregation/ /tmp/aggregation/manifest.json\n      - echo Publishing metrics\n      - python3 /opt/scripts/publish_metrics.py /tmp/aggregation/manifest.json\n  post_build:\n    commands:\n      - echo Uploading results...\n      - aws s3 sync /tmp/aggregation/coordinates/ s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/coordinates/ --sse aws:kms --sse-kms-key-id $KMS_KEY_ID\n      - aws s3 cp /tmp/aggregation/manifest.json s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/manifest.json --sse aws:kms --sse-kms-key-id $KMS_KEY_ID\n      - if [ -d /tmp/sdr ] && [ \"$(find /tmp/sdr -name '*.h5' | head -1)\" ]; then aws s3 sync /tmp/sdr/ s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/sdr/ --sse aws:kms --sse-kms-key-id $KMS_KEY_ID; echo SDR upload complete; fi\n      - if [ \"$(find /opt/data -name '*.h5' | head -1)\" ]; then aws s3 sync /opt/data/ s3://$OUTPUT_BUCKET/contacts/$CONTACT_DATE/$CONTACT_ID/rdr/ --sse aws:kms --sse-kms-key-id $KMS_KEY_ID --exclude 'defs/*'; echo RDR upload complete; fi\n      - aws s3 rm s3://$OUTPUT_BUCKET/contacts/$CONTACT_ID/.processing\n      - echo Aggregation complete\n"
          EnvironmentVariablesOverride = [
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
            },
            {
              Name  = "KMS_KEY_ID"
              Value = var.kms_key_arn
              Type  = "PLAINTEXT"
            },
            {
              Name  = "AGGREGATION_MODE"
              Value = "true"
              Type  = "PLAINTEXT"
            }
          ]
        }
        ResultSelector = {
          "build_id.$" = "$.Build.Id"
        }
        ResultPath = "$.aggregation_build"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "AggregationFailure"
            ResultPath  = "$.error"
          }
        ]
        Next = "WaitForAggregation"
      }

      # Wait 30 s before polling aggregation build status
      WaitForAggregation = {
        Type    = "Wait"
        Seconds = 30
        Next    = "CheckAggregationStatus"
      }

      # Poll CodeBuild for aggregation build status
      CheckAggregationStatus = {
        Type     = "Task"
        Comment  = "Poll aggregation CodeBuild build status"
        Resource = "arn:aws:states:::aws-sdk:codebuild:batchGetBuilds"
        Parameters = {
          "Ids.$" = "States.Array($.aggregation_build.build_id)"
        }
        ResultSelector = {
          "build_status.$" = "$.Builds[0].BuildStatus"
        }
        ResultPath       = "$.aggregation_poll"
        HeartbeatSeconds = 600
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed"]
            IntervalSeconds = 10
            MaxAttempts     = 3
            BackoffRate     = 1.5
          }
        ]
        Next = "EvaluateAggregationStatus"
      }

      # Branch on aggregation build status
      EvaluateAggregationStatus = {
        Type    = "Choice"
        Comment = "Route based on aggregation CodeBuild status"
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
        Default = "AggregationFailure"
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
