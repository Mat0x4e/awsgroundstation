# PR #5 semantic review - consolidated issues (2026-11-19)

Two independent semantic reviewers (different models) on `origin/main...med-pass-cspp-codebuild`
at `130d06c`, merged into one de-duplicated list. Both verdicts: NEEDS_CHANGES.

> **Outcome:** issues 1-12 and 14-17 fixed in `80529f3`..`9da1323` before the merge of
> [PR #5](https://github.com/Mat0x4e/awsgroundstation/pull/5) (`04da051`); per-issue resolution in the
> [PR comment](https://github.com/Mat0x4e/awsgroundstation/pull/5#issuecomment-7015808365).
> Open follow-ups: #13 (dead `Ref::` placeholders in the chunk job definition) and the unique-name
> part of #4 (trial scripts now only refuse to run without explicit opt-in).

Issues are sorted from highest to lowest combined importance/confidence.

---

## 1. The apply hook admits unapproved and destructive Terraform commands

**Severity: High** · *(cluster: apply guardrail enforcement — see also #3, #10)*

- **User impact:** The guardrail the project calls non-negotiable can be walked past by an agent or by a careless command line, so an arbitrary apply — including a destroy of the live Ground Station stack — can run without the user ever seeing or agreeing to a plan.
- **Issue:** In `.kiro/hooks/block-terraform-apply.json` the allow decision is an independent substring search (`apply[^;|&]*\.approved\.tfplan`) rather than a check on the plan file Terraform will actually use. `terraform apply unsafe.tfplan # audit.approved.tfplan` and `terraform apply -var-file=vars.approved.tfplan` are both admitted, and `terraform apply -destroy -auto-approve # audit.approved.tfplan` passes because the destroy branch only recognises the `terraform destroy` subcommand, not apply's destroy mode. Command detection is equally narrow: the regex is `terraform([[:space:]]+-chdir=[^[:space:]]+)?[[:space:]]+apply`, so any other global flag position bypasses it (`terraform -no-color apply -auto-approve` was verified ALLOWED), as do `terraform.exe apply ...`, mixed-case `Terraform destroy ...`, a path to the executable, and `-chdir <dir>` with a space. In a multi-line command, one matching line clears the whole command because `grep -Eq` succeeds per line.
- **Recommendation:** Replace the inline regex with a checked-in, tested parser of `tool_input.command`: normalise the executable basename and case, reject comments, chaining and multi-line input, reject `-destroy`, and require exactly one positional plan path ending in `.approved.tfplan`. Commit test cases covering quoted `$env:TEMP` paths and `-chdir` (allow) plus `.exe`, mixed case, `destroy`, `apply -destroy`, comment suffix, pipes, chaining and extra positional arguments (deny). Keep subagent permission denials in place regardless — a filename cannot prove approval.

## 2. The new Ground Station JSON payloads are not pinned to LF

**Severity: High** · *(cluster: byte-exact unvalidatedJSON — see also #5)*

- **User impact:** A fresh Windows clone silently changes the exact `unvalidatedJSON` strings the contact-eligibility fix depends on. The next apply replaces a known-working configuration with an untested one, and the only symptom is the profile going back to zero schedulable contacts — discovered days later via `list-contacts`.
- **Issue:** `infra/modules/sync_pipeline/files/jpss1_decode.json` and `jpss1_demodulation.json.tpl` carry no text/EOL rule (`git check-attr` reports `text: unspecified, eol: unspecified` for both), while `.gitattributes` pins `*.sh`, `*.py`, `*.tf`, `*.yaml`, `*.yml` to LF precisely because `core.autocrlf=true` is set on this checkout. `file()` and `templatefile()` in `infra/modules/sync_pipeline/groundstation.tf` would then return CRLF strings. The investigation never isolated key order from whitespace, so whitespace must be treated as load-bearing.
- **Recommendation:** Add `*.json text eol=lf` and `*.tpl text eol=lf` (or pin the `infra/modules/sync_pipeline/files/` directory) to `.gitattributes`, and verify on a clean `core.autocrlf=true` checkout that the rendered payload is byte-identical.

## 3. The hook action is POSIX-shell-only and fails open on Windows

**Severity: High** · *(cluster: apply guardrail enforcement — see also #1, #10)*

- **User impact:** If Kiro dispatches the hook through PowerShell or `cmd.exe` — the documented shell for this repository — the command fails to parse, exits with a status other than 2, and every Terraform apply is allowed. The guardrail is then in exactly the silent no-op state the previous version was in.
- **Issue:** The `action` body in `.kiro/hooks/block-terraform-apply.json` uses `cmd=$(...)`, `if ... fi`, `printf`, `grep -Eq` and `/dev/null` with no pinned interpreter, and the JSON does not invoke `sh` or `bash`. On this machine `grep` and `printf` are not on PATH outside Git's `sh`, and `bash` resolves to the WSL launcher. Meanwhile the repository's conventions (`$env:TEMP`, `execute_pwsh`) are Windows-native. The PR description also states the hook has not been exercised and only takes effect in a new session.
- **Recommendation:** Move the predicate into a small checked-in Python script invoked through a known interpreter (Python is already a hook dependency): read the documented `tool_input` JSON from stdin, write the denial reason to stderr, and exit 2 only on denial. Then verify the deny and allow paths in a restarted Windows Kiro session before merge.

## 4. Trial scripts can delete AWS resources they did not create

**Severity: High**

- **User impact:** In a shared AWS account, running a trial script can delete another session's CloudFormation stack or EC2 key pair, destroying work in progress and potentially live Ground Station objects.
- **Issue:** `scripts/gs_aqua_template_trial.ps1`, `gs_bisect_trial.ps1`, `gs_diff_template_vs_ours.ps1` and `gs_h6_h7_trial.ps1` set `$keyCreated = $true` immediately after `aws ec2 create-key-pair`; native command failures do not trigger PowerShell's `catch`, so a pre-existing `trial-aqua-key` is treated as newly created and deleted in `finally`. Several scripts also unconditionally delete the fixed stack name `aqua-snpp-jpss-trial`, so a deploy that failed because of a concurrent run tears down that other run's stack. `gs_trial6_address.ps1` checks the key exit code but still operates on and deletes the fixed stack; trials 7 and 8 use the fixed stack name as their only ownership boundary, and `gs_trial8_antenna_and_group.ps1` additionally assumes exactly one non-baseline dataflow endpoint group exists. The scripts also carry hardcoded account, VPC, subnet and mission-profile identifiers with no dry-run or confirmation, and `gs_aqua_template_trial.ps1` assumes `$env:TEMP\aqua\AquaSnppJpss.yml` already exists.
- **Recommendation:** Generate a unique run suffix for every stack, key pair, config, endpoint group and profile; check `$LASTEXITCODE` after every mutating AWS CLI call; and delete only identifiers returned by a successful create in the current run. Fetch or explicitly require the reference template inside the run, and add a header to each script stating that it creates billable, account-wide objects and is not safe to run blind.

## 5. Nothing detects a reformatting of the load-bearing payload

**Severity: Medium** · *(cluster: byte-exact unvalidatedJSON — see also #2)*

- **User impact:** Any future edit that reformats the demod/decode JSON (an editor "format on save", a linter, a merge tool) silently reintroduces the zero-contacts outage, with no failure at plan or apply time.
- **Issue:** The fix in `infra/modules/sync_pipeline/groundstation.tf` depends on byte-level fidelity of a string the backend appears to match textually, but nothing in the repository guards that property; the regression is only observable via `list-contacts` after an apply. Nothing about a `.json` file otherwise signals that its formatting is functional.
- **Recommendation:** Add a cheap guard — a byte-compare against a recorded hash, or a Terraform `check` block on the rendered payload — and state in `groundstation.tf` that reformatting these files is a functional change, not a cosmetic one.

## 6. Approved plan files are created but never deleted

**Severity: Medium** · *(cluster: apply guardrail enforcement — see also #1, #10)*

- **User impact:** Saved plans accumulate indefinitely in the user's TEMP directory holding configuration, variable and state-derived values in cleartext, and a stale file remains a candidate for accidental reuse.
- **Issue:** `.kiro/steering/delegation-and-safety.md` introduces `$env:TEMP\<name>.approved.tfplan` as the approval token but specifies no disposal step and no prohibition on reuse.
- **Recommendation:** Require deletion of the plan file immediately after the apply attempt, whether it succeeded or failed, and forbid applying a previously saved plan. If audit evidence is needed, retain a human-readable plan summary or a checksum rather than the binary plan.

## 7. Primary docs still state the superseded onboarding blocker

**Severity: Medium** · *(cluster: stale sync status — see also #8)*

- **User impact:** A reader arriving through the architecture entry point or the glossary is told the sync path "cannot currently be flown" because NOAA-20 is not onboarded for demod/decode — the conclusion this PR disproves — so they may abandon or re-investigate a path that now returns 21–34 contact offers.
- **Issue:** `docs/ARCHITECTURE.md` (intro, around lines 24–26) still carries the onboarding-blocked claim and links to `SYNC_ONBOARDING_INVESTIGATION.md`, even though this PR edits that same file further down and adds a superseded banner to the investigation. `docs/GLOSSARY.md` repeats the onboarding-blocked status in its introduction and in several definitions, while `README.md` and `docs/SYNC_PIPELINE.md` now say the opposite.
- **Recommendation:** Rewrite the `ARCHITECTURE.md` intro and the `GLOSSARY.md` status text to match `SYNC_PIPELINE.md`: onboarding was not the blocker, contacts are offered, and the remaining gate is packet format / first flight.

## 8. The new skill reference opens on a stale "unresolved" status

**Severity: Medium** · *(cluster: stale sync status — see also #7)*

- **User impact:** Agents load this file as authoritative and read the first screen. It tells them zero contacts are obtained and the cause is not established, and hands them a hypothesis table and a test plan for a problem that was solved 150 lines later in the same file — leading to re-running mutating trials against real AWS resources.
- **Issue:** `.kiro/skills/aws-ground-station/references/sync-and-digital-twin.md` opens with "Zero contacts obtained" and "the cause is **not established**", keeps the H1–H5 hypothesis table under "Hypotheses for 'no contact offered' (to test, not to assume)", and keeps a test-plan step instructing the reader to fix the configs — all resolved further down.
- **Recommendation:** Add a resolved/current-state banner at the top, as `SYNC_ONBOARDING_INVESTIGATION.md` received, and separate the historical trial log from the current status and the remaining packet-format / first-flight gate.

## 9. Apply-ownership wording is inconsistent across active instructions

**Severity: Medium**

- **User impact:** Conflicting instructions on who may run `terraform apply` mean a session can legitimately conclude the operator applies and hand the user an unapproved fresh apply, defeating the plan-approval policy this PR introduces.
- **Issue:** The new policy (subagents plan-only, orchestrator applies an agreed saved plan) is stated in `.kiro/steering/delegation-and-safety.md`, `wa-delta-workflow.md`, `WORKSTREAMS.md` and the Ground Station skill, but `.kiro/agents/terraform.md` still says "the operator applies" in its front-matter description and "When the operator will re-apply" in the launch-template exception — fields that participate in routing and handoff. `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md` likewise says agents plan and the operator applies, then shows a fresh unapproved `terraform apply -var ...` command.
- **Recommendation:** Update all currently active instructions to the orchestrator/approved-plan model, including agent front matter and the clean-compute handoff command. Label wording inside completed, locked specs as historical rather than rewriting them.

## 10. Approval is encoded in a filename, and the skill overstates enforcement

**Severity: Medium** · *(cluster: apply guardrail enforcement — see also #1, #3, #6)*

- **User impact:** The user is told the hook enforces plan-only discipline, so they may trust it more than it deserves. Nothing binds `*.approved.tfplan` to a plan a human read, nothing expires it, and an agent intending to apply can simply name its plan file accordingly.
- **Issue:** The hook's entire notion of consent is the substring `.approved.tfplan`; Terraform's own staleness rejection — not the hook — is the real backstop against re-applying an old file. `.kiro/skills/aws-ground-station/SKILL.md` states that the `block-terraform-apply` hook "enforces" the policy, and `.kiro/steering/delegation-and-safety.md` does not record the limitation.
- **Recommendation:** Write the limit down in `delegation-and-safety.md` (filename-based approval raises the cost of an accidental apply, not a deliberate one) and soften the `SKILL.md` claim to describe the hook as a backstop for the convention rather than enforcement of it.

## 11. The new steering example does not match the implementation it documents

**Severity: Medium**

- **User impact:** This entry is the one piece of steering an agent will copy verbatim, so the next Ground Station change is likely to be written against an attribute and a filename that do not exist in the repository, producing a plan error or a silently different payload.
- **Issue:** The new Ground Station entry in `.kiro/steering/aws-knowledge.md` shows `data.aws_region.current.name` where the module uses `.id` throughout, and `file("${path.module}/files/jpss1_demodulation.json")` where the implementation is `templatefile("${path.module}/files/jpss1_demodulation.json.tpl", {...})` — a path that is not in the repository.
- **Recommendation:** Align the CORRECT block with `infra/modules/sync_pipeline/groundstation.tf`: `data.aws_region.current.id` and the `templatefile(...json.tpl)` call. The surrounding rule text and the note that `terraform validate` plus a `HEALTHY` endpoint group prove nothing here are accurate and should stay.

## 12. WORKSTREAMS.md mixes workstream rows into the AWS-objects table

**Severity: Low**

- **User impact:** Two active projects render as unmanaged AWS objects with a "Proposed" disposition and lose their Status cell entirely, inverting their meaning for the coordination audience the page exists to serve.
- **Issue:** `docs/WORKSTREAMS.md` defines the AWS-object inventory with three columns (`Object | State | Proposed`) but inserts the four-cell workstream rows "Public satellites allowing uplink" and "AWS Ground Station vs KSAT" into it; Markdown renderers drop or misalign the extra cell.
- **Recommendation:** Move both rows into the four-column workstream table.

## 13. Dead `Ref::` placeholders remain in the chunk job definition

**Severity: Low**

- **User impact:** A job submitted without `ContainerOverrides` — a manual `submit-job` during debugging, or a future second submitter — receives the literal string `Ref::input_bucket` as `INPUT_BUCKET` and fails somewhere deep in the entrypoint instead of at the boundary.
- **Issue:** Removing the six empty-string `parameters` defaults in `infra/modules/batch_compute/main.tf` is correct (the Batch API discards empty-string parameter values, and `sdr_pipeline/step_functions.tf` supplies all values through `ContainerOverrides.Environment`), but the seven `environment[]` entries whose values are literal `Ref::...` placeholders stay behind, and the new comment correctly notes they never resolve.
- **Recommendation:** Drop those `environment[]` entries, or make the container entrypoint fail fast when a required variable still has a `Ref::` prefix.

## 14. Slide-map generator has no pinned dependency manifest

**Severity: Low**

- **User impact:** A clean checkout cannot reproduce the committed station-map PNGs without guessing compatible package versions, so generated assets in the deck are effectively unreviewable and unregenerable.
- **Issue:** `scripts/slides/world_map.py` imports Cartopy, NumPy, Matplotlib and adjustText, but the diff adds no requirements or lock file for the gitignored `.venv-maps` environment, and the script downloads Natural Earth data on first use.
- **Recommendation:** Add a pinned map-specific requirements file plus a bootstrap command, and document the first-run network dependency. Pin `adjustText` in particular, since its label placement varies across versions.

## 15. `processing_pipeline` archive_file was left without a deterministic mode

**Severity: Low**

- **User impact:** The cross-runner zip-hash plan noise this PR removes elsewhere returns the moment `enable_processing_pipeline` is turned on, reintroducing phantom diffs for whoever enables it.
- **Issue:** Four `archive_file` resources received `output_file_mode = "0644"` (and `__pycache__` excludes where a `source_dir` is used), but the `data_processor` archive in the processing pipeline did not. It sits behind a disabled flag, so it produces no diff today.
- **Recommendation:** Apply the same `output_file_mode` and `__pycache__` exclude lines to the `processing_pipeline` archive now, while the pattern is fresh.

## 16. The production network diagram contradicts itself

**Severity: Low**

- **User impact:** Readers take the diagram as the reference topology and may design or review against a configuration that cannot exist, placing a public-IP interface in a private subnet.
- **Issue:** `docs/diagrams/network-production.yaml` places an `ENI publique + EIP` inside `PrivateSubnet` even though the instance already has NAT egress, and labels the NAT-to-Internet link "via Internet Gateway" without defining an Internet Gateway resource or the corresponding route relationship.
- **Recommendation:** Model the receiver by its private dataflow-endpoint ENI/IP, keep the NAT gateway in the public subnet, and add the Internet Gateway explicitly with its route relationship.

## 17. Repo hygiene on the newly touched files

**Severity: Low**

- **User impact:** Minor friction: noisy diffs on files that lost their trailing newline, and a deck that can never be committed without `-f`.
- **Issue:** `.gitignore` and `.kiro/hooks/block-terraform-apply.json` lost their trailing newlines, and the blanket `*.pptx` ignore added to `.gitignore` prevents committing any PowerPoint deliverable rather than just working files.
- **Recommendation:** Restore the trailing newlines and narrow the PowerPoint ignore to the working-file pattern actually intended (for example a `drafts/` path or a `*-wip.pptx` suffix).
