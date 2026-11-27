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
- Bash scripts (`scripts/*.sh`) and template files (`*.tftpl`, `*.sh.tpl`) -> the `python` subagent
  (decided 2026-11-27; no dedicated shell agent exists). Use `python-haiku` for small mechanical
  edits and `python-debugger` after `python` has failed, as for `*.py`. A `*.tftpl` that is rendered
  by Terraform is still verified by a pytest render test; the Terraform side of it goes through the
  `terraform` agent.
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
  It errs on the side of blocking: any command whose TEXT contains "terraform ... apply" (a commit
  message, an echo) is blocked too. Reword the text rather than working around the hook.
- Exception: after a `latest_version`-pinned launch-template change, a saved plan file throws
  "Provider produced inconsistent final plan" (see aws-knowledge.md). Then apply FRESH (no plan
  file), which the hook blocks: ask the user to run that apply themselves.

## Architecture principle: converge early, mutualize the rest

- The sync and async downlink paths converge at the first common artifact (CCSDS frames in S3)
  and share everything after it: one aggregation job, one image, one set of CSPP assets, one
  visualization. Before adding a path-specific component, check whether the shared one can take
  a parameter instead; a separate component needs a reason in the data. Details:
  `docs/ARCHITECTURE.md` "Architecture principle".

## Verify by evidence, not by exit code

- A green Step Functions execution or a Batch job that exits 0 is NOT proof the work happened.
  Confirm the real artifact: products under `products/<date>/<contact>/`, a non-zero
  `ProductsWritten` emitted AFTER the upload, and fresh object timestamps. (See aws-knowledge.md
  "dispatch-vs-completion".)

## Dry runs: as close to live conditions as possible

(Decided by the project owner 2026-11-23, after contact #8 failed on two faults the dry run
could not see.)

- A dry run before a paid contact exercises the REAL live path: the same triggers, the same
  principals (invoke the real Lambda / state machine with a test event; never substitute operator
  or admin credentials), the same instance state at start (stopped), the same network path.
- Every deviation from live conditions (fake contact id, no satellite signal, skipped step) is
  listed BEFORE the dry run, with how that gap is validated another way (IAM policy simulation,
  reading the role policy against the AWS doc, a canary). A gap with no validation is a known
  risk to state to the user before booking, not something to discover at AOS.
- Record the deviation list and its validation in the contact's entry in `docs/CONTACTS.md`.

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
