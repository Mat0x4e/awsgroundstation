# Parallel workstreams

Several agent sessions work on this repo at the same time, in the **same checkout** and against the
**same AWS account** (`471112743408`, `eu-central-1`). This page lists them and the rules that keep them
from stepping on each other. Update it when a workstream starts or ends.

Status as of 2026-11-19.

| Workstream | Goal | Main outputs | Status |
|---|---|---|---|
| **Sync reception + digital twin** | Get the synchronous (demod/decode, live UDP) path to receive contacts, then test it with the AWS Ground Station digital twin | `.kiro/skills/aws-ground-station/` (SKILL + `references/sync-and-digital-twin.md`), `scripts/gs_*.ps1`, `infra/modules/sync_pipeline/`, `docs/SYNC_PIPELINE.md` | Fixed and applied 2026-11-19: the sync profile now gets contacts. Next: Terraform drift investigation (below), then a first real sync contact |
| **Terraform clean reference** | Explain the 13 unrelated changes a full `terraform plan` shows and bring code and AWS back in line | `.agents/tasks/terraform-clean-reference-2026-11-19/report.md` | **Done 2026-11-19**: `origin/main` merged, plan noise removed, last 2 Lambda hash updates applied. `terraform plan` = **No changes**. Orphans deleted (below) |

### AWS objects outside Terraform (checked 2026-11-19)

| Object | State | Proposed |
|---|---|---|
| Mission profile `2655b0f6-...` `groundstation-noaa20-demo-noaa20-hrd` + configs `265ceaff`, `091b9cb2`, `1bdacbdb` | Not in state; duplicate name of the managed `32a1fdbb-...`. It is the async profile every booking so far used (`docs/CONTACTS.md`) | **Keep**, documented as manual. Switching bookings to `32a1fdbb` first is a separate decision |
| IAM role `groundstation-processing-role` + instance profile `groundstation-processing-profile` | Created 2026-06-19, never used, no instance or launch template using the profile, no reference in code | **Deleted 2026-11-19** (with its inline policy `s3-and-ec2-access`) |
| ECR repo `groundstation-satdump` | Created 2026-06-19, empty, no reference in code | **Deleted 2026-11-19** |
| Log group `/aws/codebuild/satdump-noaa20-processing` | 448 KB; its project was deleted 2026-10-22 | **Deleted 2026-11-19** |
| S3 `groundstation-noaa20-tfstate-471112743408`, log group `/aws/batch/job` | Terraform backend; created by Batch itself | Keep, intentionally unmanaged |
| **Public satellites allowing uplink** | Inventory the AWS public broadcast satellites and find public/European satellites that accept an uplink | `docs/SATELLITES_PBS_AND_UPLINK.md` | In progress (separate session) |
| **AWS Ground Station vs KSAT** | Compare how AWS Ground Station and KSAT are used (access, pricing, coverage, partnership) | not yet in the repo | In progress (separate session) |

Earlier work in the same period (slide deck v16, world maps, contact #6 write-up) is done; see git history
from `cdcfbfa`.

## Coordination rules

- **Stage only your own files.** Never `git add -A` / `git add .`: other sessions leave untracked or
  modified files in the shared working tree. Check `git status` and add files by name.
- **Do not edit another workstream's main outputs** (table above). Link to them instead.
- **Throwaway AWS objects carry a recognisable prefix** (`trial*` mission profiles/configs/groups,
  stack `aqua-snpp-jpss-trial`, key pair `trial-aqua-key`) and are deleted by the script that created
  them. Before running a trial, check nothing from another session is mid-run with the same names.
- **Nothing is reserved without the user's go-ahead.** A contact is a paid act (~$110-130); the scheduler
  cron stays disabled.
- **`terraform apply` is run by the orchestrating session, with the user's explicit agreement** given
  after reading the plan summary; subagents are plan-only (`.kiro/steering/delegation-and-safety.md`).
  One apply at a time across all sessions.
- **SSO sessions expire** roughly every few hours; any session may need `aws sso login --use-device-code`.
- Findings that change how the project works go to `.kiro/steering/aws-knowledge.md` (practice) and the
  Ground Station SKILL, so the other sessions pick them up.
