# Operator Runbook — Post-Refinement Follow-ups

Operator-owned actions remaining after the Well-Architected refinement
([`ARCHITECTURE_REFINEMENT.md`](ARCHITECTURE_REFINEMENT.md),
spec [`.kiro/specs/architecture-refinement/`](../.kiro/specs/architecture-refinement/)).

None are blocked. Each needs real spend, protected config, or a `terraform apply` that the
refinement workflow deliberately left to a human (it ran plan-only throughout). Ordered by
suggested sequence.

---

## 1. Apply the MCP TLS fix (2 min — unblocks AWS doc/knowledge MCP tools)

**Why:** the MCP servers run under Python/`uvx`, which uses `certifi`'s CA store and does not
trust the corporate TLS-interception root CA — so outbound HTTPS fails with
`CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`. PowerShell works
because it uses the Windows cert store, which has the root. The CA bundle has been exported
and the config already edited; the file is a protected path for agents, so a human reloads it.

```
File:   C:\Users\mbonnet\.kiro\settings\mcp.json   (backup: mcp.json.bak-<timestamp>)
Bundle: C:\Users\mbonnet\.kiro\certs\corporate-ca-bundle.pem   (102 CAs incl. corporate root)
```

The three TLS-making servers (`fetch`, `aws-docs`, `aws-mcp`) already carry:
```jsonc
"env": {
  "SSL_CERT_FILE":      "C:/Users/mbonnet/.kiro/certs/corporate-ca-bundle.pem",
  "REQUESTS_CA_BUNDLE": "C:/Users/mbonnet/.kiro/certs/corporate-ca-bundle.pem"
}
```

- **Action:** reload MCP (servers reconnect on config change).
- **Verify:** run an `aws-docs` search — no `CERTIFICATE_VERIFY_FAILED`.
- **If it still fails:** some `httpx`-based servers need the bundle via a different var; revert
  with the `.bak` file and investigate.
- **Maintenance:** regenerate the PEM if the corporate CA rotates. (Export: Windows cert store
  `Cert:\LocalMachine\Root` + `CA` → one PEM.)
- **Tool-name note:** `search_documentation` lives on the `aws-docs` server (`mcp_aws_docs_*`),
  NOT the `aws-mcp` proxy (`mcp_aws_mcp_aws___*`) — they are different servers.

---

## 2. Apply the refinement Terraform (pipeline or manual)

All waves are `terraform validate`-clean with **0 net-new HIGH/CRITICAL** (Checkov:
492 passed / 81 failed / 14 skipped; all 81 failures pre-existing on unchanged resources).

```bash
aws sso login --profile AWSAdminAccess-471112743408
export AWS_PROFILE=AWSAdminAccess-471112743408 AWS_REGION=eu-central-1
cd infra && terraform plan        # review the diff first
```

### One-time: adopt-then-rename the SDR-image CodeBuild project (Task 1.1 / OE-1)

The deployed out-of-band project `satdump-noaa20-processing` must be **imported** before the
first apply, or Terraform creates a parallel project. In
`infra/modules/sdr_pipeline/codebuild_image.tf`:

```bash
# Option A (preferred — native import block):
#   1. Uncomment the import { } block in codebuild_image.tf
terraform -chdir=infra plan       # expect: 1 to import + 1 to replace
terraform -chdir=infra apply      # imports AND renames in one step
#   2. Re-comment the import block after apply

# Option B (CLI equivalent):
terraform -chdir=infra import \
  'module.sdr_pipeline[0].aws_codebuild_project.sdr_docker_build' \
  satdump-noaa20-processing
```

The first apply **replaces** the project (CodeBuild names are immutable → rename forces
destroy+recreate). This is expected and safe: the project is stateless (NO_SOURCE, only
builds+pushes the ECR image, which is untouched), and its hand-added S3 `docker-build/*` read
is now codified, so there is no access gap during the swap.

### New standing cost (minor)

The PERF-1 LUT-cache refresh (`sdr_pipeline/lut_cache.tf`) runs a CodeBuild job weekly
(`rate(7 days)`) — one internet LUT fetch per week instead of one per contact. First
aggregation after a cold cache falls back to `sdr_luts.sh` automatically (self-healing).

---

## 3. Run the compute bake-off, then close ADR-001 (the measured gate)

**Why:** ADR-001 (in `ARCHITECTURE_REFINEMENT.md`) recommends AWS Batch on EC2 Spot for the
chunk fan-out, but on **list-price estimates**. Replace them with one real measurement before
any migration. The `compute-bakeoff-batch-spot` module is built and gated **off**. Scope:
**fan-out only** (aggregation/CSPP deferred — now unblocked by PERF-1's cache, but out of this
bake-off's scope).

```bash
# Enable the throwaway module (bake-off spec Task 4):
#   infra/terraform.tfvars →  enable_compute_bakeoff = true   (requires enable_sdr_pipeline = true)
terraform -chdir=infra apply

# Run one already-received contact's chunks on Batch-on-Spot + the CodeBuild baseline:
./scripts/bakeoff/run_bakeoff.sh ba2c5446    # e.g. contact #5 — 22 chunks, already in S3
```

Records actual $/contact, wall-clock, queue delay, and Spot-interruption count for both
platforms. Cost of the experiment: ~$1–2 Spot + one CodeBuild baseline (~$7–9).

**Then record the result (Task 5):**
- Paste the measured results table into ADR-001 in `ARCHITECTURE_REFINEMENT.md`.
- Flip ADR-001 status to Accepted or Rejected (migrate / do-not-migrate) with the measured
  justification.
- If it recommends migrating, open a **new** migration spec — do not migrate from the bake-off
  spec.

**Mandatory teardown (Task 6):**
```bash
#   infra/terraform.tfvars →  enable_compute_bakeoff = false
terraform -chdir=infra apply
# Verify: no `bakeoff-` Batch resources, no running Spot instances tagged
# Objective=compute-bakeoff.
```

---

## Standing operational reminders

- **Post-pass verification:** `python scripts/verify_pass.py <contact_id> --date YYYY/MM/DD --save-report output/contact-NN_<station>_<date>` — checks products, metrics, and alarms in one go; exits non-zero if the pass produced no imagery. Report files (`verify_pass_report.txt`, `verify_pass_report.json`) are saved into the contact output folder (see `output/README.md`). Contact #7 example: `python scripts/verify_pass.py dcef1518-c454-401f-bdcd-dc2e73fe89a5 --date 2026/10/17 --save-report output/contact-07_oregon-1_2026-10-17`
- **Verify products, not execution status.** A `SUCCEEDED` run still does not prove imagery —
  but the refinement now surfaces failures via SNS: `ProductsWritten==0`, `VisualizationFailed`,
  `ChunksFailed` / all-chunks-failed, async `ExecutionsFailed`, and the sync `ReceiverReadyFailure`
  and `OptDriftDetected` alarms. **Subscribe an endpoint** (email/Slack/etc.) to the shared SNS
  topic if one is not already attached, or the alarms fire into the void.
- **Scheduler cron stays DISABLED.** Booking is a deliberate manual act. The new monthly budget
  (`security/budget.tf`, default $500, account-wide → SNS at 80%/100%/forecast) is the backstop
  against an accidental re-enable. Narrow its `cost_filter` to `"AWS Ground Station"` once the
  exact Cost Explorer service string is confirmed (committed, commented, ready to enable).
- **Sync path is not flight-ready** until the packet-format gate in
  [`SYNC_PIPELINE.md`](SYNC_PIPELINE.md) is cleared: capture a raw sample via the new `--raw-tee`
  (uploaded to `s3://<bucket>/contacts/<date>/<contact>/raw/`), inspect against the onboarding
  spec, confirm RT-STPS `frame_sync` locks, record the verified layout.
- **`/opt/*` drift is now detected, not reconstructed.** If the `OptDriftDetected` alarm fires,
  `/opt/rt-stps` or `/opt/SDR_4_1` is missing/empty on an instance — restore it by hand before
  the next pass (full automated reconstruction was deliberately deferred — see
  `ssm_drift_check.tf` and OE-2 in the refinement doc).
- **NOAA-20 HRD horizon.** The S-NPP data cessation (2026-11-02) does NOT affect NOAA-20, but it
  is a precedent arriving on the same `cspp_registrants` channel. Find NOAA-20's own HRD Direct
  Broadcast sunset date (JPSS program / NOAA OSPO) and record it in [`CONTACTS.md`](CONTACTS.md)
  as a hard horizon — it bounds the useful life of the whole async DigIF pipeline.

---

## Where the refinement artifacts live

| Artifact | Path |
|---|---|
| Target design + delta backlog | [`ARCHITECTURE_REFINEMENT.md`](ARCHITECTURE_REFINEMENT.md) |
| Compute decision (ADR-001) | `ARCHITECTURE_REFINEMENT.md` → ADR-001 |
| Accepted tradeoffs table | [`ARCHITECTURE.md`](ARCHITECTURE.md) → "Accepted architectural tradeoffs" |
| Refinement spec | [`.kiro/specs/architecture-refinement/`](../.kiro/specs/architecture-refinement/) |
| Bake-off spec | [`.kiro/specs/compute-bakeoff-batch-spot/`](../.kiro/specs/compute-bakeoff-batch-spot/) |
| Reusable quality-gate workflow | [`.kiro/steering/wa-delta-workflow.md`](../.kiro/steering/wa-delta-workflow.md) |
| Checkov-on-save hook | `.kiro/hooks/wa-checkov-on-tf-save.json` |
| Target-state diagram | `diagrams/out/architecture-refinement.png` |
