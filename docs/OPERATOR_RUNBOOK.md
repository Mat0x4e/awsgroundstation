# Operator Runbook

Standing rules and checks for whoever operates the pipeline. How to book and process a pass is in
the [README](../README.md) ("Booking a pass", "Processing a pass"); how to fly a first sync contact
is the checklist in [`SYNC_PIPELINE.md`](SYNC_PIPELINE.md#first-sync-contact---checklist).

> Rewritten 2026-11-19. The earlier "post-refinement follow-ups" (apply the refinement Terraform,
> run the compute bake-off, adopt the out-of-band CodeBuild project) are all done or superseded:
> the refinement is applied, ADR-001 is accepted and the Batch migration replaced the bake-off,
> and `satdump-noaa20-processing` was deleted. Their history is in git and in
> [`ARCHITECTURE_REFINEMENT.md`](ARCHITECTURE_REFINEMENT.md).

## Standing operational reminders

- **Post-pass verification:** `python scripts/verify_pass.py <contact_id> --date YYYY/MM/DD --save-report output/contact-NN_<station>_<date>` checks products, metrics and alarms in one go and exits non-zero if the pass produced no imagery. Reports land in the contact output folder (see `output/README.md`). Example (contact #7): `python scripts/verify_pass.py dcef1518-c454-401f-bdcd-dc2e73fe89a5 --date 2026/10/17 --save-report output/contact-07_oregon-1_2026-10-17`. It covers the async path only; a sync pass is verified by hand for now.
- **Verify products, not execution status.** A `SUCCEEDED` run does not prove imagery. Failures surface via SNS: `ProductsWritten==0`, `VisualizationFailed`, `ChunksFailed` / all-chunks-failed, async `ExecutionsFailed`, and the sync `ReceiverReadyFailure` and `OptDriftDetected` alarms. Make sure the SNS topic has a **confirmed** subscription, or the alarms fire into the void.
- **Scheduler cron stays DISABLED.** Booking is a deliberate manual act (~$105-130 per contact). The monthly budget (`security/budget.tf`, default $500, account-wide, SNS at 80%/100%/forecast) is the backstop against an accidental re-enable.
- **Book new async contacts on the Terraform-managed profile** (`terraform -chdir=infra output mission_profile_arn`, `32a1fdbb-...`). The older unmanaged `2655b0f6-...` profile still exists (contacts #1-#6); see [`WORKSTREAMS.md`](WORKSTREAMS.md).
- **Terraform apply** is run by the orchestrating session only after the user agrees to the plan; subagents are plan-only ([`.kiro/steering/delegation-and-safety.md`](../.kiro/steering/delegation-and-safety.md)). `terraform plan` should show "No changes" between deliberate changes.
- **Sync path is not flight-ready** until the packet-format gate is cleared - follow the [first sync contact checklist](SYNC_PIPELINE.md#first-sync-contact---checklist).
- **`/opt/*` drift is detected, not reconstructed.** If `OptDriftDetected` fires, `/opt/rt-stps` or `/opt/SDR_4_1` is missing or empty on an instance: restore it by hand before the next pass (`ssm_drift_check.tf`, OE-2 in the refinement doc).
- **Images bake the code.** Batch jobs run code built into the ECR images: after changing `scripts/`, rebuild the image from the right branch before validating (`.kiro/skills/rebuild-image-from-branch`).
- **LUT cache.** A weekly CodeBuild job (`sdr_pipeline/lut_cache.tf`) refreshes the CSPP LUTs in S3; a cold cache falls back to `sdr_luts.sh` automatically.
- **NOAA-20 HRD horizon.** The S-NPP data cessation (2026-11-02) does not affect NOAA-20, but it is a precedent. Find NOAA-20's own HRD Direct Broadcast sunset date (JPSS / NOAA OSPO) and record it in [`CONTACTS.md`](CONTACTS.md): it bounds the useful life of the DigIF pipeline.

## Operator workstation: MCP TLS behind the corporate proxy

The MCP servers run under Python/`uvx`, which uses `certifi`'s CA store and does not trust the
corporate TLS-interception root CA (`CERTIFICATE_VERIFY_FAILED`). The fix is in
`C:\Users\mbonnet\.kiro\settings\mcp.json`: the `fetch`, `aws-docs` and `aws-mcp` servers set
`SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to `C:/Users/mbonnet/.kiro/certs/corporate-ca-bundle.pem`.
Regenerate the PEM if the corporate CA rotates (export `Cert:\LocalMachine\Root` + `CA` to one PEM).
`search_documentation` lives on the `aws-docs` server, not the `aws-mcp` proxy.

## Where the design decisions live

| Artifact | Path |
|---|---|
| Target design, delta backlog, ADR-001/002 | [`ARCHITECTURE_REFINEMENT.md`](ARCHITECTURE_REFINEMENT.md) |
| Accepted trade-offs | [`ARCHITECTURE.md`](ARCHITECTURE.md) -> "Accepted architectural tradeoffs" |
| Batch migration | [`CLEAN_COMPUTE_PIPELINE_MIGRATION.md`](CLEAN_COMPUTE_PIPELINE_MIGRATION.md), [`CLEAN_COMPUTE_PIPELINE_HANDOFF.md`](CLEAN_COMPUTE_PIPELINE_HANDOFF.md) |
| Reviews and investigations | [`reviews/`](reviews/) |
| Quality-gate workflow | [`.kiro/steering/wa-delta-workflow.md`](../.kiro/steering/wa-delta-workflow.md) |
