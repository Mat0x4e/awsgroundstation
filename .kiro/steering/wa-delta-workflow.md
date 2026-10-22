---
inclusion: manual
---

# Well-Architected Delta Workflow (steering)

A generic, reusable workflow for executing any **remediation or refinement backlog** in this
repository — a set of well-bounded changes, each traceable to a confirmed finding, each driven
to "done" through agentic quality gates. It was authored for
[`.kiro/specs/architecture-refinement/`](../specs/architecture-refinement/) but is deliberately
backlog-agnostic: load it (`#wa-delta-workflow`) for any spec whose tasks are discrete
infrastructure/code deltas with verifiable acceptance criteria.

Load this before executing delta tasks. It governs *how* each task is driven, not *what* the
tasks are (that is the spec's `tasks.md`).

---

## The four gates

Every task passes through four gates in order. A task is **not done** until all four pass. A
failed gate blocks progression — fix and re-run the gate, do not proceed past it.

```
┌───────────┐   ┌──────────┐   ┌───────────────┐   ┌────────┐
│ IMPLEMENT │ → │ VALIDATE │ → │ SECURITY GATE │ → │ REVIEW │
└───────────┘   └──────────┘   └───────────────┘   └────────┘
   build          it works       it's safe          it's right
```

### Gate 1 — IMPLEMENT (delegated to a domain subagent)
Route the change to the subagent that owns the domain. Never edit a domain file directly when a
specialized subagent exists:

| Change touches | Subagent |
|---|---|
| `*.tf`, `*.tfvars`, `*.tftest.hcl` | `terraform` |
| `*.py` (new logic, multi-file, moto tests) | `python` |
| `*.py` (mechanical: run tests, small utility, named fix) | `python-haiku` |
| `*.py` (cross-file bug, boto3/edge-case, after `python` failed) | `python-debugger` |
| Docs, READMEs, runbooks | `docs-writer` or handled directly |

The subagent reads the spec's `requirements.md`/`design.md` itself (pass file paths, not
excerpts) and implements exactly one task's scope.

### Gate 2 — VALIDATE (correctness)
- **Terraform:** `terraform validate` then `terraform -chdir=infra plan`. **Plan-only — never
  `apply`** (operators/pipeline own apply). The plan must show only the intended diff; an import
  task must show import/no-destroy, not recreate.
- **Python:** run the relevant pytest (and moto tests) via the project venv
  (`./test_env/Scripts/python.exe -m pytest ...`). New behaviour needs a test that fails if the
  code is reverted.
- A task that changed behaviour must leave the **full relevant test suite green** — no
  "pre-existing failures" left behind if the change caused them.

### Gate 3 — SECURITY GATE (safety) — delegated to `security-scanner`
- Run `checkov -d . --quiet --download-external-modules false` on `infra/`.
- **HIGH and CRITICAL findings block the task.** Fix, or document with a `checkov:skip` + a
  one-line justification (the pattern already used repo-wide for the deliberate no-DR skips).
- Verify **no new unconditioned IAM wildcards** were introduced. `cloudwatch:PutMetricData` must
  carry a `cloudwatch:namespace` condition; log policies use the three write actions, not
  `logs:*`; `Resource = "*"` is only acceptable for genuinely un-scopeable AWS actions and must
  be commented as such.
- This gate also runs automatically on Terraform file save via the
  `wa-checkov-on-tf-save` hook — the hook is an early-warning tripwire; this gate is the
  blocking checkpoint.

### Gate 4 — REVIEW (correctness of intent)
- Confirm the task's acceptance criteria in `requirements.md` are each met (quote the criterion,
  state how it was verified).
- Confirm **docs truth**: if the change invalidated a constraint in `docs/ARCHITECTURE.md` or
  `docs/ARCHITECTURE_REFINEMENT.md`, it was updated in the same task (continuous-learning
  convention from `CLAUDE.md`).
- For non-trivial or behavioural changes, invoke `semantic_reviewer` on the diff.

---

## Running a wave

The spec's `tasks.md` ends with a **Task Dependency Graph** (`waves`). Execute wave by wave:

1. Tasks in the same wave have no inter-dependencies — dispatch their IMPLEMENT gates in
   parallel (one subagent each).
2. When all tasks in a wave have passed gates 1–2, run the wave's **checkpoint** (gates 3–4) once
   for the wave's combined output.
3. Only then start the next wave.

A **Checkpoint task** in `tasks.md` *is* a shared Gate-3/Gate-4 run for the preceding waves —
treat it as a hard stop.

---

## Rules

- **Plan-only Terraform.** This workflow never runs `terraform apply`. The CI/CD pipeline or an
  operator applies after review.
- **One concern per commit.** Stage only the files for the task at hand; review `git status`
  first. Never `git add -A`.
- **Backport learnings.** A gotcha discovered mid-task is written back into the most specific
  steering/docs file (the error, the ❌ wrong pattern, the ✅ correct pattern, and why).
- **Brevity.** Subagents and the orchestrator report what changed and where in 1–3 sentences;
  expand only for a decision made, a surprise, or an action the user must take.
- **Accepted tradeoffs are not failures.** A finding resolved by documenting a deliberate
  tradeoff (single-region, single CMK, single-AZ) passes the review gate when the rationale is
  recorded — it is not left as an open gate.
