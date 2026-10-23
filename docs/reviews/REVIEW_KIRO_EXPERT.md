# Kiro Usage Review -- awsgroundstation

Reviewer lens: Kiro expert (steering, specs, hooks, skills, custom agents, workflows, MCP).
Subject: how this project *uses* Kiro, i.e. the `.kiro/` directory and adjacent agent tooling.
Branch reviewed: `docs/post-batch-accuracy-review`.
Scope: `.kiro/steering/`, `.kiro/specs/`, `.kiro/hooks/`, (absent) `.kiro/skills/`,
(absent) `.kiro/agents/`, and the legacy `.agents/` tracker.

---

## Executive summary

This is an unusually *mature* Kiro project for its steering and spec craft, undermined by
some lifecycle-hygiene debris and by a strong operating discipline that lives only in prose
Kiro never loads.

What stands out as genuinely good:

- Three steering files with deliberate, correct inclusion modes: a large always-on
  `aws-knowledge.md` of hard-won service gotchas, and two `manual` playbooks
  (`articles.md`, `wa-delta-workflow.md`) that are loaded by name only when relevant.
- Seven specs that consistently use EARS-style requirements (23-69 `SHALL`/`WHEN...THEN`
  clauses each) and tasks that cite requirement IDs back for traceability (7-38 refs each).
- A real, working agentic quality workflow (`wa-delta-workflow.md`: IMPLEMENT ->
  VALIDATE -> SECURITY -> REVIEW, wave-based, plan-only Terraform) that is far beyond
  typical Kiro usage.
- A genuine, risk-aware PostFileSave hook (`wa-checkov-on-tf-save.json`) that degrades
  gracefully when the tool is absent.

What drags the score down:

- **Spec sprawl / lifecycle debris.** Only `ec2-aggregation` is explicitly marked
  `SUPERSEDED`. The three original demo-era specs (`aws-ground-control-demo`,
  `noaa20-cadu-to-tiff`, `noaa20-viirs-visualization`) are in **French** while the four
  newer specs are in **English**, and none of the old three is marked superseded despite
  being overtaken by `clean-compute-pipeline`.
- **A spec-inside-a-spec anomaly** that is not even this project:
  `.kiro/specs/aws-ground-control-demo/.kiro/specs/` contains a complete **"IoT Sensor
  Dashboard"** spec (Zigbee / Aqara / Raspberry Pi). It is copy-paste debris from another
  project and should be deleted.
- **The project's single most important rule -- "all `*.tf` -> terraform subagent, all
  `*.py` -> python subagent, agents plan-only, operator applies" -- is codified only in a
  `manual`-inclusion steering file and a handoff doc Kiro never auto-loads.** There is no
  always-on steering and no `.kiro/agents/` definition enforcing it.
- No `.kiro/skills/` at all, despite several obviously repeatable operator procedures.
- A **legacy `.agents/` tracker** (JSON task/feature records) that overlaps the
  `.kiro/specs/` concept and is now stale.

### Maturity rating

**4 / 5 -- Advanced.** Steering and specs are top-decile; the agentic workflow is real and
documented. It falls short of a 5 because the load-bearing discipline is not enforced where
Kiro can see it, there is no skills layer, and spec lifecycle hygiene has visible debris
(nested foreign spec, unmarked superseded French specs). Every gap below is low-effort to close.

---

## Findings: Steering

Files: `.kiro/steering/aws-knowledge.md` (24 KB, `inclusion: always`),
`.kiro/steering/articles.md` (7 KB, `inclusion: manual`),
`.kiro/steering/wa-delta-workflow.md` (5 KB, `inclusion: manual`).

### Strengths

- **Right *kind* of content.** `aws-knowledge.md` is exactly what steering is for: durable,
  project-specific service gotchas that a model will not know and that cost real pass money
  to rediscover -- e.g. the metric-math-alarm period race, the CodeBuild
  dispatch-vs-completion `ProductsWritten=0` trap, the five Batch-on-Spot launch blockers,
  the `batch:submitJob.sync` managed-rule IAM race, and the Windows bash-subshell SSO
  `InvalidToken` gotcha. Each entry follows a tight `Symptom / Root cause / INCORRECT /
  CORRECT / Rule` shape with live evidence. This is reference-grade.
- **Correct inclusion modes.** `articles.md` and `wa-delta-workflow.md` are `manual` -- they
  are task playbooks (writing articles; running a remediation backlog) that should load only
  when invoked (`#articles`, `#wa-delta-workflow`), not pollute every prompt. That is the
  right call.
- **Steering points at living docs.** `articles.md` anchors to `docs/CONTACTS.md` and the
  awsdac diagram sources; `wa-delta-workflow.md` points at `requirements.md`/`design.md`
  and `docs/ARCHITECTURE*.md`. Steering references the source of truth rather than
  duplicating it.

### Issues

1. **`aws-knowledge.md` is 24 KB and `inclusion: always`.** It is high quality, but it is a
   fixed tax on *every* prompt in the repo, including article-writing and doc tasks that
   never touch CloudWatch alarms or Batch. As more gotchas are backported (the workflow
   explicitly says "backport learnings"), this file only grows.
2. **The delegation / plan-only discipline is not in always-on steering.** The rule set that
   the whole project runs on -- `*.tf -> terraform`, `*.py -> python`/`python-haiku`/
   `python-debugger`, docs -> `docs-writer`, agents never run `terraform apply` -- appears
   in `wa-delta-workflow.md` (Gate 1 table) and in `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md`
   ("Delegation rules (enforced)"). But `wa-delta-workflow.md` is `manual`, and the handoff
   doc is not steering at all. So the "enforced" rule is only in effect when someone
   remembers to type `#wa-delta-workflow`. For a rule this load-bearing, that is a gap.
3. **No topical split.** Article guidance, workflow, and AWS gotchas are three files; there is
   no small always-on "project conventions / delegation" file -- the thing most worth making
   always-on is the thing currently missing.

### Recommendations

- **Add a small always-on `delegation-and-safety.md`** (the one rule set that should never be
  optional). Keep it short so the always-on tax stays low:

  ```markdown
  ---
  inclusion: always
  ---
  # Delegation and Safety (non-negotiable)
  - `*.tf` / `*.tfvars` / `*.tftest.hcl` -> `terraform` subagent. Never edit infra directly.
  - `*.py` -> `python` (new/multi-file), `python-haiku` (mechanical), `python-debugger`
    (cross-file bug / after `python` failed). Never edit Python directly.
  - Terraform is PLAN-ONLY from agents. Never run `terraform apply`; the operator applies
    (SSO + spend). `validate` + `plan` only.
  - One concern per commit; never `git add -A`.
  - `description`/`name`/tag argument values are ASCII-only (see aws-knowledge.md).
  ```

- **Convert `aws-knowledge.md` to `inclusion: fileMatch`** so the heavy gotchas load only when
  infra/compute files are in play, instead of on every prompt:

  ```markdown
  ---
  inclusion: fileMatch
  fileMatchPattern: "infra/**/*.tf|buildspecs/**|scripts/**/*.py|tests/**/*.py|src/**/*.py"
  ---
  ```

  If always-on is preferred for safety, instead **split** it: keep the two cross-cutting
  rules (ASCII `description`, verify-completion-not-exit-code) in the small always-on file,
  and move the long per-service case studies to a `fileMatch` doc.

---

## Findings: Specs

Seven specs under `.kiro/specs/`. All use `workflowType: requirements-first`, `specType:
feature`. EARS adoption is consistent (clause counts: architecture-refinement 39,
aws-ground-control-demo 35, clean-compute-pipeline 69, compute-bakeoff-batch-spot 23,
ec2-aggregation 35, noaa20-cadu-to-tiff 47, noaa20-viirs-visualization 69). Task->requirement
traceability is present in every `tasks.md`.

Completion snapshot (checkbox state):

| Spec | done | open | Note |
|---|---|---|---|
| architecture-refinement | 40 | 0 | complete |
| aws-ground-control-demo | 14 | 0 | complete (French; demo-era) |
| clean-compute-pipeline | 21 | 18 | **in flight** |
| compute-bakeoff-batch-spot | 0 | 18 | **folded into clean-compute-pipeline as its first strangler step** |
| ec2-aggregation | 21 | 1 | explicitly SUPERSEDED |
| noaa20-cadu-to-tiff | 49 | 0 | complete (French; demo-era) |
| noaa20-viirs-visualization | 35 | 17 | complete-ish (French; demo-era) |

### Strengths

- **EARS discipline is real, not cosmetic.** Requirements read as testable `SHALL` /
  `WHEN ... THEN` clauses, and tasks cite them back (`_Requirements:` style). This is the
  part Kiro specs most often get wrong, and here it is done well across seven specs.
- **Design docs carry rationale, not just "what".** `clean-compute-pipeline` ties directly to
  ADR-002, explains the strangler model, names the exact warm-data contact used for
  validation (`dcef1518...`, `2026/10/17`, Oregon), and explains *why* it supersedes ADR-001.
  `ec2-aggregation`'s superseded banner is a model of how to retire a spec -- it states the
  new approach (CodeBuild), the root cause (CSPP ancillary fetch vs SSM-outbound-only SG),
  the cost tradeoff that was reversed, and why the instance is retained anyway.
- **`compute-bakeoff-batch-spot` is correctly described as folded in** rather than abandoned:
  `clean-compute-pipeline/requirements.md` states its Spot measurement is the first strangler
  step. So its 0/18 "open" tasks are not neglected work -- but see Issues.

### Issues

1. **Spec-inside-a-spec, and it is a foreign project.**
   `.kiro/specs/aws-ground-control-demo/.kiro/specs/` holds `requirements.md` (6.6 KB) +
   `design.md` (13.9 KB) titled **"IoT Sensor Dashboard"** -- a Zigbee / Aqara WSDCGQ11LM /
   Raspberry Pi pipeline that has nothing to do with ground stations. This is copy-paste
   scaffolding debris from an unrelated project that landed under a nested `.kiro/specs/`.
   It is confusing (a reader or a model crawling specs will trip on it) and should be
   **deleted**. (I am not editing `.kiro` here per instructions -- flagging only.)
2. **Language split with no marker.** The three demo-era specs
   (`aws-ground-control-demo`, `noaa20-cadu-to-tiff`, `noaa20-viirs-visualization`) are
   written in **French** (90-138 accented/French-keyword hits each) while the four newer
   specs are **English**. Nothing is wrong with French per se, but the inconsistency plus the
   absence of a status banner means a reader cannot tell these three are the *original*
   build, now largely superseded by `clean-compute-pipeline` (which migrated every compute
   stage off CodeBuild). They read as equally current.
3. **Superseded hygiene is applied once, not consistently.** `ec2-aggregation` has an
   exemplary `> ## WARNING SUPERSEDED -- 2026-09-01` banner. The demo-era trio has no
   equivalent, even though `clean-compute-pipeline` explicitly supersedes ADR-001's direction
   and re-platforms the same stages. A reader cannot reconstruct the lineage from the specs alone.
4. **`clean-compute-pipeline` is in flight (21/18).** Fine -- but because it is the spec that
   supersedes others, its half-done state is exactly when stale sibling specs are most
   misleading. The "consistent with what shipped" question cannot be answered from checkbox
   state alone; it needs a short "status / what shipped" note at the top (the handoff doc
   `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md` has this context but the spec does not link it).

### Recommendations

- **Delete the nested `aws-ground-control-demo/.kiro/specs/` IoT spec.** It is not this
  project.
- **Add a one-line status banner to each superseded/legacy spec**, matching the
  `ec2-aggregation` pattern, e.g. at the top of the three French specs:
  `> ## LEGACY (demo-era) -- compute stages re-platformed by clean-compute-pipeline (ADR-002). Kept for history.`
- **Pick one spec language** going forward (the newer specs are English; standardize on that)
  and note the French specs as legacy rather than translating them -- translation is wasted
  effort on retired specs.
- **Add a short "Status / shipped" header to `clean-compute-pipeline`** linking
  `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md`, so an in-flight spec declares what is already live.
- Consider an **`.kiro/specs/ARCHIVE/`** convention (or a top-level `SPECS.md` index) that
  lists each spec with status: active / shipped / superseded-by. This scales better than
  per-file banners as the spec count grows past seven.

---

## Findings: Hooks

File: `.kiro/hooks/wa-checkov-on-tf-save.json`.

### Strengths

- **Correct trigger + matcher.** `PostFileSave` with matcher `\.tf$` -- fires exactly when a
  Terraform file is saved, which is when a security regression is cheapest to catch.
- **Graceful degradation.** If `checkov` is not on `PATH`, it prints an actionable message
  (`pip install checkov`) *and* reminds that Gate 3 via the `security-scanner` subagent is
  still the blocking checkpoint. It does not fail the save. Good hook manners.
- **Honest about its own role.** Both the hook description and `wa-delta-workflow.md` Gate 3
  call it an "early-warning tripwire," not the authority -- the blocking gate is still the
  subagent. That is the right mental model for a save hook.

### Issues

1. **Windows-only.** The command is `pwsh -NoProfile -Command "..."`. On a Linux/CI checkout
   (and this sandbox is Linux) the hook cannot run. Given `CLAUDE.md` documents a Windows
   operator box but CI is Linux, a cross-platform form (or a sibling hook) would widen coverage.
2. **It is the only hook.** The project documents a rich set of apply-time failure modes in
   `aws-knowledge.md` that are *perfect* hook material, and none of them has a tripwire yet.
3. **`timeout: 180` running full `checkov -d infra` on every `.tf` save** can be heavy for a
   large infra tree; scoping to the changed module would be snappier (minor).

### Recommendations -- high-value hooks tailored to *this* project's documented gotchas

Each maps to a real `aws-knowledge.md` entry a hook could have caught earlier:

- **Non-ASCII `description` linter (PostFileSave on `\.tf$`).** Directly prevents the
  documented `apply`-time `ValidationError` on IAM `description` / EC2 `GroupDescription`
  (em-dash U+2014, smart quotes, arrows). `terraform validate` cannot catch it; a grep hook
  can, at save time, before the partial-apply hazard:

  ```json
  {
    "version": "v1",
    "hooks": [{
      "name": "WA Delta -- non-ASCII guard on Terraform save",
      "trigger": "PostFileSave",
      "matcher": "\\.tf$",
      "description": "Blocks the documented apply-time non-ASCII description failure (aws-knowledge.md).",
      "action": {
        "type": "command",
        "command": "f=\"$KIRO_FILE_PATH\"; if LC_ALL=C grep -nP '[^\\x00-\\x7F]' \"$f\"; then echo 'wa-delta: non-ASCII char in a .tf file -- if it is in a description/name/tag value, apply WILL fail (aws-knowledge.md). Replace em-dash->-, smart quotes->\\\"/'\\'' , arrow->-> , x-sign->x.' >&2; exit 2; fi",
        "timeout": 20
      }
    }]
  }
  ```

- **`terraform fmt -check` + `validate` on save (PostFileSave on `\.tf$`).** Cheap correctness
  gate; `CLAUDE.md` already treats `terraform fmt -check -recursive` as an acceptance
  criterion, so a save hook enforces it continuously rather than at review time.

- **`pytest` on Python change (PostFileSave on `\.py$`, scoped).** The project's venv is
  `test_env/` and it uses moto tests; a save hook that runs the matching test module turns
  the "new behaviour needs a failing-if-reverted test" rule (Gate 2) into a live signal. It
  would also have surfaced the `ProductsWritten=0` lifecycle class of bug sooner.

- **"No `terraform apply` from an agent" guard (PreToolUse on the shell/command tool).** The
  single most important safety rule ("agents plan-only; operator applies") currently relies on
  prose. A PreToolUse hook that inspects the command and blocks (`exit 2`) when it matches
  `terraform\s+.*\bapply\b` makes the rule *mechanical*:

  ```json
  {
    "version": "v1",
    "hooks": [{
      "name": "Block terraform apply from agent",
      "trigger": "PreToolUse",
      "matcher": "execute_bash|shell|command",
      "description": "Plan-only discipline: agents must never run terraform apply (operator applies).",
      "action": {
        "type": "command",
        "command": "cmd=$(cat); if echo \"$cmd\" | grep -Eq 'terraform([[:space:]]|-chdir=[^[:space:]]+[[:space:]])+apply'; then echo 'BLOCKED: terraform apply is operator-only (plan-only discipline, wa-delta-workflow.md). Produce a plan and hand off.' >&2; exit 2; fi",
        "timeout": 15
      }
    }]
  }
  ```

  (Confirm the exact PreToolUse matcher/stdin contract against the Kiro hook docs before
  shipping -- the shape above follows the documented exit-2-blocks convention.)

- **Cross-platform Checkov.** Add a POSIX-shell sibling of the existing hook (or replace the
  `pwsh` wrapper with a shell that checks `command -v checkov`) so the tripwire also fires on
  the Linux/CI checkout, not just the Windows operator box.

---

## Findings: Skills

There is **no `.kiro/skills/` directory.** The project has several repeatable, parameterized
operator procedures that are ideal skill candidates -- they are currently spread across
`scripts/`, `docs/OPERATOR_RUNBOOK.md`, `docs/DEPLOYMENT.md`, and `articles.md`.

### Assessment

Skills would add real value here because the project has **recurring multi-step procedures with
known gotchas** -- exactly what a skill packages (procedure + the one-line traps) so it does not
have to be re-derived each time. Concrete candidates, each backed by existing assets:

- **`render-awsdac-diagram`** -- the article workflow in `articles.md` already documents the
  full `awsdac <figure>.yaml -o out/<figure>.png -f` procedure *and* its schema traps
  (`AWS::Region` not `AWS::Diagram::Region`; `Direction: vertical`; `Type: orthogonal`;
  multi-line titles unsupported; v0.23 path). That is a skill waiting to be extracted from
  steering prose.
- **`deploy-aggregation-entrypoint`** -- `scripts/deploy_aggregation_entrypoint.sh` plus the
  `aws-knowledge.md` gotcha that on Windows you must run the `aws s3 cp --sse aws:kms`
  *directly in PowerShell*, not via `bash script.sh` (the `InvalidToken` subshell trap). A
  skill encodes "use the PowerShell form on the operator box" so the trap is not re-hit.
- **`reprocess-a-contact` / `validate-a-pass`** -- `scripts/verify_pass.py`, `plan_pass.py`,
  and the "verify-completion-not-exit-code" rule (check `ProductsWritten` emitted *after* the
  `s3 sync`, the `complete` log line, fresh object timestamps). A skill captures the
  verify-by-evidence steps that two separate `aws-knowledge.md` entries insist on.
- **`rebuild-image-from-branch`** -- the `aws-knowledge.md` Batch-image gotcha: a container job
  runs image-baked code, so you must `aws codebuild start-build --source-version <branch>` and
  confirm the ECR `imagePushedAt` advanced before validating. This is a precise, repeatable,
  trap-laden procedure -- a textbook skill.
- **`run-batch-canary`** -- the "five launch-blocking gotchas" entry explicitly says run a
  one-chunk canary before wiring Batch into an orchestrator. A skill makes that the default
  path, not tribal knowledge.

### Recommendation

Create `.kiro/skills/` and start with the two highest-friction, highest-repeat procedures:
`render-awsdac-diagram` (extract straight out of `articles.md`) and `rebuild-image-from-branch`
(extract from `aws-knowledge.md`). Each skill should carry the "do / do-not" traps already
documented so they stop being prose a human must remember.

---

## Findings: Custom agents and workflows

### Strengths

- **A real, named, four-gate agentic workflow.** `wa-delta-workflow.md` defines
  IMPLEMENT -> VALIDATE -> SECURITY -> REVIEW, executed wave-by-wave from a Task Dependency
  Graph in `tasks.md`, with explicit checkpoint tasks as hard stops. This is a sophisticated,
  genuinely useful orchestration pattern -- well above typical Kiro usage.
- **Clear subagent taxonomy.** The Gate-1 table routes by file type to
  `terraform` / `python` / `python-haiku` / `python-debugger` / `docs-writer`, and names
  `security-scanner` (Gate 3) and `semantic_reviewer` (Gate 4). The roles are real and
  consistently referenced across specs and the handoff doc.
- **Plan-only Terraform discipline is explicit and repeated** ("This workflow never runs
  `terraform apply`"; "operators/pipeline own apply").

### Issues

1. **The discipline is not where Kiro can enforce it.** The delegation table and the
   plan-only rule live in `wa-delta-workflow.md` (`inclusion: manual`) and in
   `docs/CLEAN_COMPUTE_PIPELINE_HANDOFF.md` ("Delegation rules (enforced)"). Neither is
   auto-loaded. "Enforced" is aspirational -- nothing stops a fresh session (no
   `#wa-delta-workflow`) from editing a `.tf` directly or running `apply`.
2. **No `.kiro/agents/` definitions.** The named subagents (`terraform`, `python`,
   `security-scanner`, etc.) are referenced everywhere but defined nowhere in the repo. If
   these are relied on, they are either ambient/host-provided or undocumented. Either way the
   contract (what each agent may touch, that it is plan-only, which steering it must load) is
   not captured as a Kiro custom-agent definition.
3. **Two parallel tracking systems.** The legacy `.agents/tasks/task-aws-groundstation-demo/`
   tree (its own `task.json`, `FEAT-001.json`, `FEAT-002.json`, dated review `.md` files,
   `status: completed`) is an older feature/task tracker that overlaps the `.kiro/specs/`
   concept. It is now stale (demo-era, completed) and conflicts conceptually with specs as the
   source of truth. It should be archived or removed to avoid two competing notions of
   "the task list."

### Recommendations

- **Codify delegation in always-on steering** (see the `delegation-and-safety.md` block in the
  Steering section) so the rule applies without anyone invoking a manual file.
- **Back it with the PreToolUse "no apply" guard** (Hooks section) so the plan-only rule is
  mechanical, not merely documented.
- **Define the subagents as `.kiro/agents/` custom agents** so each has an explicit contract.
  Minimum set: `terraform` (tools: file-read + plan/validate only, `apply` denied; must load
  `aws-knowledge.md`), `security-scanner` (checkov + IAM-wildcard review), `python` /
  `python-debugger`. Capturing them as definitions makes "which agent owns which files" and
  "this agent cannot apply" first-class instead of prose.
- **Retire `.agents/`** -- move it to an `archive/` path or delete it, with a note in
  `docs/` that `.kiro/specs/` is the single task source of truth.

---

## Prioritized recommendations

Effort: S (< 30 min), M (an hour or two), L (half-day+). Value: how much it reduces real risk
or rediscovery cost on this project.

| # | Recommendation | Type | Effort | Value |
|---|---|---|---|---|
| 1 | Add always-on `delegation-and-safety.md` steering (delegation table + plan-only + ASCII rule) | Steering | S | High |
| 2 | Add PreToolUse hook that blocks `terraform apply` from agents | Hook | S | High |
| 3 | Delete the nested foreign "IoT Sensor Dashboard" spec under `aws-ground-control-demo/.kiro/specs/` | Spec hygiene | S | High |
| 4 | Add non-ASCII `description` guard hook on `.tf` save (prevents documented apply failure) | Hook | S | High |
| 5 | Add `LEGACY/SUPERSEDED` status banners to the three French demo-era specs | Spec hygiene | S | Med-High |
| 6 | Convert `aws-knowledge.md` to `fileMatch` (or split: small always-on + fileMatch case studies) | Steering | S | Med-High |
| 7 | Make the Checkov hook cross-platform (POSIX-shell sibling for Linux/CI) | Hook | S | Med |
| 8 | Add `terraform fmt -check` + `validate` save hook | Hook | S | Med |
| 9 | Add `pytest`-on-`.py`-save hook scoped to the changed module (venv `test_env/`) | Hook | M | Med |
| 10 | Create `.kiro/skills/` with `render-awsdac-diagram` and `rebuild-image-from-branch` | Skill | M | Med |
| 11 | Define `.kiro/agents/` for `terraform` / `security-scanner` / `python` with explicit plan-only contracts | Custom agent | M | Med-High |
| 12 | Add a `SPECS.md` index (active / shipped / superseded-by) and a `Status/shipped` header on `clean-compute-pipeline` | Spec hygiene | S | Med |
| 13 | Archive or delete the legacy `.agents/` tracker; declare `.kiro/specs/` the single source of truth | Workflow | S | Med |

Items 1-5 are the quick wins: they close the biggest gap (load-bearing discipline Kiro cannot
see) and remove the most confusing debris (foreign nested spec, unmarked superseded specs) in
well under an afternoon, without touching any shipped infrastructure.

---

*Review covers `.kiro/` as of branch `docs/post-batch-accuracy-review`. No `.kiro` file was
modified in producing this report.*
