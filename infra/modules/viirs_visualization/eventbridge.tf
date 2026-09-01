# eventbridge.tf — intentionally empty.
#
# Visualization is no longer triggered by S3 events. The Step Functions pipeline
# invokes the orchestrator directly once aggregation succeeds
# (StartVisualization in modules/sdr_pipeline/step_functions.tf).
#
# WHY, in full, because this cost real money on 2026-09-01:
#
# The rule that used to live here matched any object whose key ended in ".png"
# in the SDR output bucket:
#
#     object = { key = [{ suffix = "/manifest.json" }, { suffix = ".png" }] }
#
# The orchestrator does not use the triggering object. It parses only the
# contact id and date out of the key, lists the WHOLE contact prefix, and
# submits one CodeBuild build for the entire contact. So it never needed
# per-object events -- one signal per contact was always sufficient.
#
# Each chunk build uploads hundreds of SatDump composite PNGs (a contact prefix
# holds ~9,150 objects). The moment S3 -> EventBridge was switched on for that
# bucket, every PNG became an orchestrator invocation and every invocation
# became a CodeBuild build: ~6,000 builds in one morning. The account hit
# "Cannot have more than 100 builds in queue", which starved the SDR pipeline's
# own aggregation build and failed the run.
#
# Narrowing the pattern could not fix it properly. Nothing this pipeline writes
# is one-per-contact -- manifest.json is never produced, and even a single named
# composite (viirs_rgb_True_Color.png) appears once per chunk, so the best a
# filter achieves is 22 redundant whole-contact builds instead of ~6,000.
#
# The fix is structural: the pipeline knows which contact it just processed, so
# it tells the orchestrator instead of letting the orchestrator discover work
# from object events. If S3-event triggering is ever reintroduced, it needs a
# genuinely one-per-contact object to key on, and the sdr_output bucket needs
# eventbridge = true restored in modules/sdr_pipeline/s3.tf (removed for the
# same reason).
