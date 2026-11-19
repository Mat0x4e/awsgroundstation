# Terraform clean reference - investigation report (2026-11-19)

## UPDATE (after orchestrator notice) - this supersedes the "lost code" theory below

**Confirmed: AWS was applied from `main`'s code.** Commit `67be1ae` "feat(compute-pipeline): Stage 3 render -> Batch (job def + IAM + Lambda strangler swap)" (2026-10-22) is on `origin/main` (`adb617c`) and not on this branch (merge-base `097b803`). It adds `batch_render_job`, `batch_render_execution`, the render job def and log group, and the viirs Lambda env/IAM. Commit `ca877d5` retires `satdump-noaa20-processing`. My earlier `git log -S` search missed it because that search did not cover `origin/main`, so the sections below that say "never committed / lost" are **wrong**. Section 2.1's AWS evidence stays valid.

Test: read-only `terraform plan` from a temporary worktree of `origin/main` (deleted afterwards), against the live state:
`Plan: 0 to add, 6 to change, 0 to destroy`:
- 0 render destroys. The `viirs_docker_build` buildspec and the `viirs_visualization` IAM policy no longer show a change (CRLF in this checkout's working tree was the buildspec cause).
- `viirs_orchestrator`: only `source_code_hash`. `lambdas/viirs_visualizer/handler.py` on main is byte-identical to the deployed one (31,398 chars, same after EOL normalisation), so it is the zip-mode / `__pycache__` noise (Group B).
- `contact_scheduler`, `aggregation_trigger`, `receiver_arm`: zip hash noise (Group B), unchanged conclusion.
- `batch_job_definition.chunk`: empty-parameter normalisation (Group C), unchanged.
- `sync_pipeline demod_decode`: main has the old `jsonencode` JSON; this branch's fix (e387c63 / e6bd60c, already applied) would be reverted by main's code. This is expected until the branch is merged.

**Revised recommended path (replaces step 2 of section 6; steps 3-7 remain valid):**
1. Do NOT apply from this branch as is (it would destroy the live render path).
2. Merge `origin/main` into `med-pass-cspp-codebuild` (or rebase). Expect conflicts around `infra/main.tf`, `viirs_visualization`, `sdr_pipeline`, docs, and `lambdas/viirs_visualizer`. The branch's own `sync_pipeline` changes must win for `demod_decode` / `dataflow_endpoint`.
3. Re-run `terraform plan` on the merged tree. Expected result: only Group B (4 zip hashes) and Group C (chunk parameters), with `demod_decode` and `dataflow_endpoint` unchanged.
4. Then apply the Group B/C fixes (normalise EOL, `output_file_mode = "0644"`, exclude `__pycache__`, drop empty `chunk` parameter defaults), plan until `No changes`.
5. The `recovered/` folder is no longer needed to restore code (main has it); keep it only as a cross-check. `docs/ARCHITECTURE.md` and `docs/OPERATOR_RUNBOOK.md` stale `satdump-noaa20-processing` text may already be fixed by main's `ca877d5` (not checked).
6. The orphan list (section 5) is unchanged, except the notes on the CodeBuild project being gone are consistent with `ca877d5`.

Uncertain: I did not test the merge itself or its conflicts, and I did not test whether `output_file_mode` makes the hashes match the deployed ones.

---

> **Correction (orchestrator, 2026-11-19, after the report):** Group A's central claim, "Stage 3 render
> was never committed", is **wrong**. It is committed on `origin/main` as `67be1ae` "feat(compute-pipeline):
> Stage 3 render -> Batch (job def + IAM + Lambda strangler swap)", merged via PR #3 (`adb617c`).
> `git grep origin/main` finds `aws_iam_role.batch_render_job` (`batch_compute/iam.tf:524`), the render
> job definition (`batch_compute/main.tf:721`), `RENDER_COMPUTE`/`BATCH_JOB_*` env
> (`viirs_visualization/main.tf:45-49`) and the wiring in `infra/main.tf:60-64,177-178`; main's
> `lambdas/viirs_visualizer/handler.py` is 31,397 chars, matching the deployed one (31,398 recovered).
> Group A is therefore **this branch being 20 commits behind main**, not lost code. Step 2 of section 6
> becomes "merge `origin/main` into the branch", not a reconstruction. Groups B and C, and sections 4-5,
> are unaffected.
>
> **Outcome (2026-11-19):** `origin/main` merged into the branch, plan noise removed (EOL, zip mode,
> `__pycache__`, empty `chunk` parameters), last 2 Lambda hash updates applied with the user's
> agreement: `terraform plan` = **No changes**. Orphans `groundstation-processing-role` (+ instance
> profile), ECR `groundstation-satdump` and log group `/aws/codebuild/satdump-noaa20-processing`
> deleted. The `recovered/` files mentioned below were deleted after checking the deployed handler is
> identical (modulo line endings) to `lambdas/viirs_visualizer/handler.py` on the branch.
> Moved here from `.agents/tasks/terraform-clean-reference-2026-11-19/report.md`.

Read-only investigation. Branch `med-pass-cspp-codebuild`, HEAD `8250016`. AWS profile `AWSAdminAccess-471112743408`, eu-central-1, SSO active. Nothing was applied, imported or changed. `terraform plan -lock=false -out` was run to a temp file and deleted afterwards.

## 1. Summary

A fresh plan now shows `Plan: 0 to add, 7 to change, 6 to destroy` (the 2 Ground Station configs applied earlier today are gone from the diff, as intended).

**The 13 changes do not have one common cause. They fall into three groups:**

| Group | Resources | Cause | Risk if applied as-is |
|---|---|---|---|
| A. **Stage 3 "render on Batch" was deployed from the working tree but never committed** | 6 render destroys + `viirs_orchestrator` (env + code) + `viirs_visualization lambda_execution` IAM policy (8 resources) | Code lost from git, AWS is ahead of the code | **HIGH. Applying would break the live visualization path** |
| B. Build-artifact / line-ending noise | `contact_scheduler`, `aggregation_trigger`, `receiver_arm` (zip hash), `viirs_docker_build` (buildspec) | Windows CRLF working-tree files, zip file-mode and `__pycache__` differences. The code is functionally identical | None (harmless updates) |
| C. Provider normalisation | `batch_job_definition.chunk` | Batch drops empty-string `parameters`, so the diff comes back on every plan | None, but it registers a new revision on every apply |

**Key finding:** the 6 Batch "render" resources are not leftovers. They are in use. The deployed `viirs-orchestrator` Lambda runs with `RENDER_COMPUTE=batch`. Batch render jobs ran successfully on 2026-10-22 and 2026-10-23, and the products are in S3. The Terraform and Python source for that was never committed. `terraform apply` as-is would destroy the job definition and roles and strip the Lambda's Batch env vars and IAM permissions. The Lambda code (the Batch path) would stay deployed. Because the env vars would be gone, it would most likely fall back to CodeBuild and run the old, pre-fix path (not confirmed).

**Do not run a full `terraform apply` yet.**

The `contact_scheduler` update is code-identical. The EventBridge rule `groundstation-noaa20-demo-scheduler-cron` is `DISABLED` in AWS and in code, and the plan does not touch it, so there is no booking risk.

`infra/terraform.tfvars` is a whitespace-only change (column alignment). `git diff -w` is empty. It causes none of the planned changes.

## 2. Q1 - Per-resource analysis

### 2.1 Group A - Stage 3 render (committed nowhere)

Evidence that this was never in git:
- `git log --all -S"batch-render"`, `-S"RENDER_COMPUTE"` and `-S"batch_render_job"` return nothing. No stash holds it. `git fsck --unreachable` only finds two old stash commits from 2026-10-21 (before the work).
- `git grep -e RENDER_COMPUTE -e BATCH_JOB_DEFINITION HEAD -- lambdas infra tests` finds nothing. `infra/modules/batch_compute/main.tf` has only `chunk` (line ~372) and `aggregation` (line ~571) job definitions. Its header comments (lines 13, `outputs.tf:2`, `variables.tf:4`) still say render is "added in tasks 5/10".
- The last relevant commit is `dc2fd94` (2026-10-22 10:57). Its message and `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md` ("Stage 3 ... IN PROGRESS", item 1, tasks a-d "not started") describe render as still to do.
- The AWS objects were created at 2026-10-22 13:02 UTC (roles' `create_date` 13:02:40/41Z, Lambdas' `LastModified` 13:02:53-59Z), about 2 hours after that commit. Code written in the working tree and applied, then lost or reverted without a commit. Probably a `git checkout`/reset or an overwrite by a later session. The cause is unknown (reflog shows nothing between `dc2fd94`, `9e203a3` and `097b803`).

Evidence the resources are live and used:
- Lambda `groundstation-noaa20-viirs-orchestrator`: `LastModified 2026-10-22T13:02:59`. Env has `RENDER_COMPUTE=batch`, `BATCH_JOB_QUEUE=...agg-queue`, `BATCH_JOB_DEFINITION=...job-definition/groundstation-noaa20-render`, `KMS_KEY_ID`, `ENABLE_DESTRIPE=true`.
- The deployed zip's `handler.py` (31,355 chars) differs from `lambdas/viirs_visualizer/handler.py` at HEAD (23,352 chars). It contains the `RENDER_COMPUTE` strangler switch and `submit_job`. HEAD has none.
- Both Step Functions (`groundstation-noaa20-sdr-pipeline`, `-sync-pipeline`) invoke this Lambda. They do not reference render directly.
- CloudWatch log group `/aws/batch/groundstation-noaa20-render` has streams from 2026-10-22 13:11-13:25 UTC and 2026-10-23 16:51-16:54 UTC (`render_entrypoint.sh starting: RENDER_BRANCH=nasa contact=dcef1518-...`).
- S3 `products/2026/10/17/dcef1518-.../` holds `viirs_nasa_*` outputs written 2026-10-23 18:54, and satdump outputs from 2026-10-22 15:24.
- ECR `groundstation-noaa20-viirs-visualization:latest` was pushed 2026-10-23 18:48. It includes `render_entrypoint.sh` and the Task 9 fixes.
- No render Batch job is currently RUNNABLE/RUNNING/SUCCEEDED in the queue (history has expired).

Plan attribute detail:

| Resource | Plan change | Source |
|---|---|---|
| `batch_compute aws_batch_job_definition.render` (`groundstation-noaa20-render:1`) | destroy. 8 vCPU / 16 GiB, command `/opt/scripts/viirs/render_entrypoint.sh`, image viirs-visualization:latest, timeout 3600, retries 1 | in state, not in code |
| `aws_cloudwatch_log_group.render` (`/aws/batch/groundstation-noaa20-render`) | destroy. 90 days, KMS. Destroying it deletes the render logs | in state, not in code |
| `aws_iam_role.batch_render_execution`, `batch_render_job` and their `aws_iam_role_policy` | destroy. Inline policies: ECR pull, logs, S3 `contacts/*` read and `products/*` write, KMS | in state, not in code |
| `viirs_visualization aws_iam_policy.lambda_execution` | removes statements `SubmitRenderBatchJob` (`batch:SubmitJob` on agg-queue and render job-def) and `PassRenderJobRoles` (`iam:PassRole` for the 2 render roles to ecs-tasks) | code lacks the statements |
| `viirs_visualization aws_lambda_function.viirs_orchestrator` | removes env `BATCH_JOB_DEFINITION`, `BATCH_JOB_QUEUE`, `ENABLE_DESTRIPE`, `KMS_KEY_ID`, `RENDER_COMPUTE`. `source_code_hash W0OD...` becomes `zczV...` (HEAD code) | code lacks env and Batch handler |

A full attribute snapshot of these 7 resources from state is saved in `recovered/render_resources_state_snapshot.txt`. The deployed (Batch-capable) handler is preserved in `recovered/viirs_visualizer_handler.DEPLOYED-2026-10-22.py`. The Lambda zip in AWS is the **only** copy of that source, so keep this file.

### 2.2 Group B - build artifacts (code is the same)

- **Zip modes.** The deployed zips have unix file mode `0644` (ExternalAttributes `-2119958528`). The zips Terraform builds locally on Windows have `0666` (`-2118778880`). For `receiver_arm` the file bytes are identical (SHA-256 prefix `6B6A108750` on both sides, 8,581 bytes), so only the zip metadata differs. The 2026-10-22 apply must have run in another environment, or an older archive build. Cause not proven. Provider archive 2.8.1 is locked, but the 10-22 provider version was not checked.
  - `receiver_arm`: `source_code_hash WUVw...` becomes `QFcL...`. Mode difference only.
  - `aggregation_trigger`: `7+Xq...` becomes `Tqt+...`. `handler.py` and `__init__.py` match, but the deployed zip contains `__pycache__/*.cpython-311.pyc`. The source dir now has `__pycache__/*.cpython-312.pyc` (untracked, git-ignored) and the new mode.
  - `contact_scheduler`: `lEEE...` becomes `4Kf5...`. The working-tree `lambdas/contact_scheduler/handler.py` has **CRLF** line endings (270 CRs, 8,410 bytes vs 8,140 deployed). Content is identical modulo EOL. `git ls-files --eol` shows `i/lf w/crlf attr/text eol=lf`, so git sees no change.
- **CodeBuild `viirs_docker_build` buildspec.** The diff is line-by-line identical text. `infra/modules/viirs_visualization/codebuild.tf` is CRLF in the working tree (`w/crlf`), so the heredoc has `\r`. AWS has LF.
- `last_modified` changes are only computed noise.

The working-tree CRLF files (git considers them unmodified) in `infra/` and `lambdas/`: `infra/modules/{contact_scheduler,mission_profile,observability,processing_pipeline,s3_delivery}/*.tf`, `sdr_pipeline/{ecr,main,outputs,s3}.tf`, `security/outputs.tf`, `viirs_visualization/{codebuild,ecr,main,outputs,variables}.tf`, `infra/outputs.tf`, `infra/versions.tf`, `.terraform.lock.hcl`, `lambdas/{contact_scheduler,data_processor}/handler.py`, plus `scripts/*.py`, `scripts/viirs/*.py` and `scripts/archive/...`. `.gitattributes` says `*.py/*.tf/*.sh/*.yml eol=lf`, but editor writes produced CRLF.

### 2.3 Group C - Batch job definition `chunk`

The plan adds null-valued `parameters` (`chunk_key`, `contact_date`, `contact_id`, `input_bucket`, `kms_key_id`, `output_bucket`). The code (`batch_compute/main.tf` ~lines 372-385) defines them as `""`. AWS stores only `chunk_id = "0"` (empty strings are dropped; revision 17 shows `{"chunk_id":"0"}`). It is provider/API normalisation, not drift. It returns on every plan. Each apply registers a new revision (17 is already reached) and deregisters the old one (`deregister_on_new_revision`). The command does not use `Ref::`, so removing the empty defaults has no runtime effect. (The job def's `environment` does use `Ref::` entries, which, per `.kiro/steering/aws-knowledge.md` item 6, are not resolved there. The SFN overrides env at submit time. I did not re-verify the SFN input.)

## 3. Q2 - Are the Batch render resources safe to destroy?

**No.** They are live and in use. Destroying them would:
- delete `groundstation-noaa20-render:1`, so `submit_job` from the Lambda would fail, and remove the log group (render logs lost);
- remove `batch:SubmitJob` and `iam:PassRole` from the Lambda role, so a Batch render submission would be denied;
- remove the Lambda's `RENDER_COMPUTE=batch` env. The old strangler default is probably CodeBuild, but the deployed Lambda code (not HEAD's) would then run until the code is also reverted. The CodeBuild project `groundstation-noaa20-viirs-visualization` still exists (`aws codebuild list-projects`), so a fallback path may exist. Not tested.

They were **not** removed on purpose. No commit, doc or spec removes them. `docs/CLEAN_COMPUTE_PIPELINE_MIGRATION.md` (Stage 3 section, line ~221 and status table ~309: "Visualization render | CodeBuild | NOT migrated") and the HANDOFF doc still describe render as the next step, which is out of date relative to AWS.

## 4. Q3 - contact_scheduler

Only `source_code_hash` and `last_modified` change. Code is identical modulo EOL (verified byte-compare of deployed `handler.py` vs the working tree). Env vars (`MISSION_PROFILE_ARN=...32a1fdbb...`, `SATELLITE_ARN`, `SNS_TOPIC_ARN`, `MINIMUM_ELEVATION_DEGREES=10`) are unchanged. The booking rule `groundstation-noaa20-demo-scheduler-cron` (`cron(0 6 ? * MON-FRI *)`) is `DISABLED` in AWS and `state = "DISABLED"` in `infra/modules/contact_scheduler/main.tf`. The plan does not touch `aws_cloudwatch_event_rule.scheduler_cron`, the target or the permission. The handler calls `reserve_contact`, but it only runs when the rule is enabled or the Lambda is invoked manually. **Applying this update cannot re-enable booking.** To be extra safe, apply it with `-target` only after Group A is fixed, or confirm in the plan that no `scheduler_cron` change appears.

## 5. Q4 - In AWS but in no Terraform

Checked against `terraform show` and `git grep` of HEAD:

| Object | Details | Notes |
|---|---|---|
| Ground Station mission profile **`2655b0f6-8196-44d3-bbc0-3782b1942d34`** `groundstation-noaa20-demo-noaa20-hrd` | Not in state. Duplicate name of the managed profile `32a1fdbb-...` | Operationally important: it is the profile in `docs/CONTACTS.md` (lines 36, 97, 227), `docs/MEDITERRANEAN_PASS.md:177` and the aws-ground-station skill (async DigIF). It uses configs `265ceaff` and `091b9cb2` and tracking `1bdacbdb`. **Do not delete** |
| GS configs `265ceaff-...` (antenna-downlink), `091b9cb2-...` (s3-recording), `1bdacbdb-...` (tracking) | Not in state. Duplicates (same names) of the managed `bd859ca5`, `94d1cbf2`, `4b4f917e` | Belong to the 2655b0f6 profile |
| IAM role `groundstation-processing-role` | Created 2026-06-19, never used. No reference in `infra/` at HEAD | Orphan |
| ECR repo `groundstation-satdump` | Image pushed 2026-06-19. No reference in HEAD | Orphan (older image repo) |
| CloudWatch log group `/aws/codebuild/satdump-noaa20-processing` | 448 KB, no retention | Left behind by the deleted project |
| CloudWatch log group `/aws/batch/job` | Created by Batch itself, no retention | Not Terraform's |
| S3 `groundstation-noaa20-tfstate-471112743408` | Terraform backend bucket | Intentionally outside state |
| CodeBuild `satdump-noaa20-processing` and role `codebuild-satdump-role` | **Already gone** (`batch-get-projects` returns it in `projectsNotFound`, `get-role` NoSuchEntity) | OE-1 / HANDOFF item 3 is resolved. `docs/ARCHITECTURE.md:187-197` and `docs/OPERATOR_RUNBOOK.md:58` ("must be imported") are stale |

Checked and consistent with state (no orphan found): Lambdas (4 project ones), Step Functions (2), CodeBuild projects (5, all in state), Batch compute environments (2), job queues (2), launch template `fanout-lt` (the other `Batch-lt-*` are Batch-managed), EC2 instances (both stopped, in state), the dataflow endpoint group `da5e60d9`, IAM roles, S3 buckets, SNS topics. I did not enumerate security groups, VPC endpoints, KMS aliases, CloudWatch alarms or dashboards beyond what the plan's refresh covers.

## 6. Q5 - Recommended path to a clean reference

Principle: AWS is ahead of the code for Group A. Make the code match AWS, do not destroy.

1. **Preserve first (done).** `recovered/` holds the deployed handler and the state snapshot of the 7 resources.
2. **Restore Stage 3 into code (code changes before any apply):**
   - `infra/modules/batch_compute/main.tf`: re-add `aws_batch_job_definition.render`, `aws_cloudwatch_log_group.render`, `aws_iam_role.batch_render_execution`, `aws_iam_role.batch_render_job` and the two `aws_iam_role_policy` (attributes in the snapshot or the plan's destroy diff). Use ASCII-only descriptions (see `aws-knowledge.md`). Add any outputs.
   - `infra/modules/viirs_visualization`: add back the env vars (`RENDER_COMPUTE=batch`, `BATCH_JOB_QUEUE`, `BATCH_JOB_DEFINITION`, `KMS_KEY_ID`, `ENABLE_DESTRIPE`) and the two IAM statements (`SubmitRenderBatchJob`, `PassRenderJobRoles`), plus the new module variables and wiring in `infra/main.tf`. The exact wiring in `infra/main.tf` is not preserved. The current values come from the plan diff and the state snapshot.
   - `lambdas/viirs_visualizer/handler.py`: restore from the recovered file. Check `tests/test_viirs_visualizer.py`, which probably needs the matching updates (not preserved, cannot confirm).
   - Update the stale statuses in `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md`, the MIGRATION doc and `.kiro/specs/clean-compute-pipeline/tasks.md` (task 10 is effectively deployed). Commit.
   - Verify: `terraform plan` must show no render destroys and no `viirs_orchestrator` env/IAM change. (The Lambda hash should match once the handler bytes match; EOL must be LF.)
3. **Remove build-artifact noise (apply-safe, can be applied as-is):**
   - Convert working-tree CRLF `.tf` and `.py` to LF (at least `lambdas/contact_scheduler/handler.py` and `viirs_visualization/codebuild.tf`), per `.gitattributes`.
   - In the `archive_file` data sources (4 modules) set `output_file_mode = "0644"` and `excludes = ["__pycache__"]` (for `source_dir` ones) so the hash is the same across machines. Whether the new hash will equal the deployed one is uncertain, especially for `aggregation_trigger` (deployed zip contains 3.11 pyc), so expect one apply per Lambda that only re-uploads identical code. `terraform plan` before and after should confirm.
4. **Group C, `chunk` parameters:** remove the six empty-string defaults from `batch_compute/main.tf` (keep `chunk_id = "0"`). Confirm no consumer relies on the placeholder (the container `command` does not use `Ref::`). Then the plan stops flagging them. Alternative with less certainty: `lifecycle { ignore_changes = [parameters] }`.
5. **Apply** the remaining in-place updates (Lambda hashes, buildspec, and the chunk job def, which makes a revision 18). All are behaviour-neutral. Use a full `terraform apply` of the saved plan only after step 2 makes the plan show no destroys. Apply is run by the agent with the user's agreement.
6. **Orphans (separate decisions, not required for a clean plan):**
   - Mission profile `2655b0f6` plus its three configs: not required for `terraform plan` to be clean, as they are simply unmanaged. Options: (a) leave documented as manual (simplest, recommended now); (b) switch bookings to the managed `32a1fdbb` profile and delete the old set after checking that it offers equal contacts; (c) import into Terraform via `awscc` resources (needs distinct resource addresses, uncertain). Never delete before bookings are migrated.
   - Delete `groundstation-processing-role`, the `groundstation-satdump` ECR repo and the `/aws/codebuild/satdump-noaa20-processing` log group after you agree (all low-risk, all manual).
   - Fix the stale `satdump-noaa20-processing` import instructions in `docs/OPERATOR_RUNBOOK.md:58` and `docs/ARCHITECTURE.md:187`.
7. **Verification of the end state:** `terraform plan -no-color -detailed-exitcode` returns 0 ("No changes"). Re-run `terraform plan` once more on a clean checkout (CRLF/LF and zip hash must be stable). Note that `infra/terraform.tfvars` changes are whitespace-only and can be committed or reverted independently.

### Not recommended

Applying the plan as it is today (or `terraform apply -target` on any Group A resource). It would remove the live Stage 3 render path and the only repository-visible record of how it is configured.

### Uncertain / not verified

- Why the Stage 3 code is missing from git (lost checkout/reset vs overwritten by another session): no evidence in reflog or fsck.
- Which OS or provider version produced the deployed zip modes on 2026-10-22.
- What the `viirs-orchestrator` would do after the env vars vanish while the Batch code stays (depends on the deployed handler's default for `RENDER_COMPUTE`; I did not read it line by line).
- Whether SFN input to the chunk job overrides env as the steering note expects.
- Whether `tests/test_viirs_visualizer.py` at HEAD matches the deployed handler (not run).
