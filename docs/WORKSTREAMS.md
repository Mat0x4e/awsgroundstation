# Parallel workstreams

Several agent sessions work on this repo at the same time, in the **same checkout** and against the
**same AWS account** (`471112743408`, `eu-central-1`). This page lists them and the rules that keep them
from stepping on each other. Update it when a workstream starts or ends.

Status as of 2026-11-19.

| Workstream | Goal | Main outputs | Status |
|---|---|---|---|
| **Sync reception + digital twin** | Get the synchronous (demod/decode, live UDP) path to receive contacts, then test it with the AWS Ground Station digital twin | `.kiro/skills/aws-ground-station/` (SKILL + `references/sync-and-digital-twin.md`), `scripts/gs_*.ps1`, `infra/modules/sync_pipeline/`, `docs/SYNC_PIPELINE.md` | Root cause of "0 contacts" found 2026-11-18 (missing `dataflowEndpointRegion`; `jsonencode`d `unvalidatedJSON`). Terraform fix in progress, not applied |
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
- **One `terraform apply` at a time**, decided by the user after reading the plan.
- **SSO sessions expire** roughly every few hours; any session may need `aws sso login --use-device-code`.
- Findings that change how the project works go to `.kiro/steering/aws-knowledge.md` (practice) and the
  Ground Station SKILL, so the other sessions pick them up.
