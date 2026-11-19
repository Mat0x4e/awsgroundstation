---
name: validate-a-pass
description: Verify a satellite contact actually produced imagery, and reprocess one if needed. Use when checking a pass outcome, re-running a contact through the pipeline, or confirming a Batch render succeeded. Enforces "verify by product evidence, not by exit code" and the correct re-run inputs.
---

# Validate (or reprocess) a pass

The cardinal rule: a green Step Functions execution or a Batch job that exits 0 is NOT proof
imagery exists. `StartVisualization` catches to success on purpose so a render failure does not
fail the run. ALWAYS verify against the products prefix and the metric, never the status.

## Verify a pass produced products

```
BUCKET=groundstation-noaa20-sdr-output-471112743408
aws s3 ls "s3://$BUCKET/products/<YYYY/MM/DD>/<contact_id>/" --recursive
```
Expect composites (True Color, Thermal IR, Day Microphysics) each as PNG + GeoTIFF + JSON.
An empty prefix = visualization failed, even if the execution says SUCCEEDED.

Completion evidence to confirm (not exit code):
- `ProductsWritten` metric emitted with a NON-ZERO value, AFTER the upload (it is emitted from
  the render/aggregation job post-sync, not at Lambda dispatch -- a dispatch-time read is always 0).
- The render job log shows `render_entrypoint.sh complete`.
- Fresh object timestamps under `products/<date>/<contact>/`.
- Render logs: log group `/aws/batch/groundstation-noaa20-render`.

The repo tool `scripts/verify_pass.py` automates this ("S3 is truth, metric is corroboration"):
```
python scripts/verify_pass.py <contact_id> --date <YYYY/MM/DD>
```

## Reprocess a contact end-to-end

1. Clear the idempotence marker. The key has NO date segment (it is `contacts/<contact_id>/.processing`,
   matching step_functions.tf) -- do NOT add the date, or the re-run short-circuits to AlreadyProcessing:
   ```
   aws s3 rm "s3://$BUCKET/contacts/<contact_id>/.processing"
   ```
2. Start a new execution. `contact_time` is REQUIRED (the state machine derives `contact_date`
   from it); there is no `contact_date` input field:
   ```
   aws stepfunctions start-execution \
     --state-machine-arn arn:aws:states:eu-central-1:471112743408:stateMachine:groundstation-noaa20-sdr-pipeline \
     --input '{"contact_id":"<id>","bucket":"aws-groundstation-demo-reception-471112743408","satellite_id":"33f035e1-73f7-47a5-9df8-fbc48636dca8","contact_time":"2026-10-17T20:33:15Z"}'
   ```
3. Monitor the Batch stages (not CodeBuild): queues `...-fanout-queue` / `...-agg-queue`,
   log groups `/aws/batch/groundstation-noaa20-{fanout,aggregation,render}`.

## Re-render only (inputs already in S3)

If `satdump/` composites and `sdr/` HDF5 already exist for the contact, you can validate just
the render path without re-running fan-out/aggregation:
- satdump branch: invoke the `groundstation-noaa20-viirs-orchestrator` Lambda with
  `{bucket, key: contacts/<date>/<id>/manifest.json}` (it detects satdump and submits a render job).
- nasa branch: submit the render Batch job directly (satdump wins `_detect_path` when both inputs
  exist), passing `RENDER_BRANCH=nasa` plus INPUT_BUCKET/INPUT_PREFIX/CONTACT_ID/CONTACT_DATE/
  KMS_KEY_ID/ENABLE_GEOTIFF/ENABLE_DESTRIPE via `--container-overrides`.
Then re-verify products as above.
