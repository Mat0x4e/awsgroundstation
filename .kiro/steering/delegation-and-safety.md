---
inclusion: always
---

# Delegation and Safety (non-negotiable)

The project's load-bearing operating rules. These apply to EVERY session in this repo,
without needing to invoke a workflow file. Keep this file short so the always-on cost stays low.

## Delegation by file type

- `*.tf` / `*.tfvars` / `*.tftest.hcl` -> the `terraform` subagent. Never edit infrastructure
  files directly.
- `*.py` -> a Python subagent: `python` (new or multi-file work), `python-haiku` (small
  mechanical edits), `python-debugger` (cross-file bug, or after `python` has failed). Never
  edit Python directly.
- Docs (`*.md`, diagram YAML) may be edited directly, or via `docs-writer` for large rewrites.

## Terraform apply: orchestrator only, with the user's explicit agreement

(Decided by the project owner 2026-11-19; replaces the earlier "operator applies" rule.)

- **Subagents are PLAN-ONLY.** The `terraform` subagent and every workflow step produce
  `terraform validate` + `terraform plan` and stop. They never run `terraform apply` / `destroy`.
- **The orchestrating session applies**, and only after the user has explicitly agreed to a plan
  summary shown in the conversation (resources, change type, any destroy/replace). No agreement,
  no apply. A general "go ahead with the work" is not agreement to apply.
- Save the agreed plan as `$env:TEMP\<name>.approved.tfplan` and apply exactly that file
  (`terraform apply <file>.approved.tfplan`), as one unchained command. Prefer `-target` when the
  full plan contains unrelated changes. Never reuse a plan file for a second apply.
- **Delete the plan file right after the apply attempt**, success or failure: plan files hold
  variable values and resource attributes in cleartext.
- One apply at a time across all sessions (see `docs/WORKSTREAMS.md`). After applying, verify by
  evidence (read the resources back, re-run `terraform plan`).
- **What the guard is and is not.** `.kiro/hooks/block-terraform-apply.sh` (tests:
  `scripts/test_block_terraform_apply_hook.ps1`) blocks `destroy`, `apply -destroy` and any apply
  other than a single unchained apply of a `*.approved.tfplan` file. It catches accidents and
  subagents; it cannot prove the user agreed, because approval is only a filename. The agreement in
  the conversation is the control. Terraform itself rejects a saved plan once state has moved.
- Exception: after a `latest_version`-pinned launch-template change, a saved plan file throws
  "Provider produced inconsistent final plan" (see aws-knowledge.md). Then apply FRESH (no plan
  file), which the hook blocks: ask the user to run that apply themselves.

## Verify by evidence, not by exit code

- A green Step Functions execution or a Batch job that exits 0 is NOT proof the work happened.
  Confirm the real artifact: products under `products/<date>/<contact>/`, a non-zero
  `ProductsWritten` emitted AFTER the upload, and fresh object timestamps. (See aws-knowledge.md
  "dispatch-vs-completion".)

## ASCII-only in Terraform argument values

- `description`, `name`, and tag VALUES in `*.tf` must be ASCII only. A non-ASCII character
  (em-dash, en-dash, smart quotes, arrow, ellipsis, x-sign) makes `terraform apply` fail at
  resource creation even though `terraform validate` passes. Replace: em/en-dash -> `-`,
  arrow -> `->`, smart quotes -> `"` / `'`, ellipsis -> `...`, x-sign -> `x`.

## Git hygiene

- One concern per commit. Stage specific files by name; never `git add -A` / `git add .`.
- Never commit directly to `main`/`master`; work on a branch and open a PR.
- Never commit AWS credentials or secrets.

## Source of truth

- `.kiro/specs/` is the single source of truth for requirements/design/tasks. (A legacy
  `.agents/` task tracker existed in the demo era; it was removed 2026-10-23 -- git history
  retains it.)
- Deeper context lives in: `.kiro/steering/aws-knowledge.md` (live-learned AWS gotchas),
  `.kiro/steering/wa-delta-workflow.md` (`#wa-delta-workflow`, the four-gate delivery workflow),
  and `docs/` (architecture, runbook, pass history, reviews).
