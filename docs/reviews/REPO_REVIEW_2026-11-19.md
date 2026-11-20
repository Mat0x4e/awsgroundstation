# Repo review - protogroundstation (main @ 92d7347), 2026-11-19

Read-only. Live checks ran (SSO was valid). Nothing was edited except this report.

> **Follow-up (same day, branch `docs/repo-review-fixes`):** done - first sync contact checklist
> in `SYNC_PIPELINE.md`; stale entry points fixed (`CLAUDE.md`, contact #7 status, `GLOSSARY.md`,
> `OPERATOR_RUNBOOK.md` reduced to standing reminders); `docs/README.md` index with a spec status
> table; trial scripts moved to `scripts/archive/sync-onboarding-trials/`; root `contacts_*.json`
> removed and ignored; `.worktrees/` ignored. Settled: contact #7 used the managed profile
> `32a1fdbb` (`describe-contact`), so `CONTACTS.md` was right and `WORKSTREAMS.md` was corrected.
> **New finding, fixed the same day:** the async rule `contact-completed-sdr` did not filter on the
> mission profile, so a sync contact would also have started the async state machine. It now
> matches only the managed profile `32a1fdbb` (applied with agreement, `terraform plan` = No
> changes). Local test suite green after installing `matplotlib==3.9.0` in `test_env` (293 passed,
> 6 skipped). Still open: the two paid contacts (fresh async on Batch, first sync).

## Bottom line

The **async (DigIF) path is operational and Well-Architected enough for a one-person demonstrator**. It has flown 7 contacts and contact #7 verified PASS. The Batch migration is deployed and its stages ran on real data, but **only as replays of contact #7's data**. No fresh pass has gone through the auto-triggered Batch chain. The **sync (demod/decode) path is deployed and now gets contacts offered, but has never flown**. Its packet-format gate is open, and the receiver prerequisites and the Batch sync aggregation are unproven. Docs are rich and mostly honest, but several entry docs are stale or contradict each other, and there is **no "fly your first sync contact" runbook**. Layout is navigable at the top level. The main cost is docs/ and scripts/ sprawl plus a few stale signposts.

| Question | Verdict |
|---|---|
| 1. Operational WA system for sync AND async | **Async: yes (with one gap). Sync: no, deployed but never exercised.** Overall: **partly** |
| 2. Well documented | **Partly** (good depth, stale entry points, one missing runbook) |
| 3. Well structured / easy to navigate | **Mostly yes** (a few cheap moves) |

---

## 1. Operational system, sync + async

### 1a. Status by stage

| Capability | State | Evidence |
|---|---|---|
| Infra matches code | **Verified** | `terraform -chdir=infra plan -lock=false` -> "No changes. Your infrastructure matches the configuration." |
| Async: reserve -> DigIF -> S3 -> SFN -> fan-out -> agg -> render, original CodeBuild chain | **Flown end-to-end, verified** | `docs/CONTACTS.md` L459-540 (contact #7, 2026-10-17): 50/50 chunks, 9/9 products, `verify_pass.py` OVERALL PASS. Live SFN history still shows the original execution `a40b74f2-..._ed0efd51-...` SUCCEEDED 2026-10-17 |
| Async: Batch fan-out (Spot) | **Ran on real data, as a replay** | `batch-validation-contact7-20261020...`, `batch-val-c7-20261020...` SUCCEEDED; 25/25 chunks (CONTACTS.md, HANDOFF) |
| Async: Batch aggregation (RT-STPS + CSPP) | **Ran on real data, as a replay** | `stage2-agg-val2-c7`, `stage2-agg-val3-c7` SUCCEEDED 2026-10-21; `stage2-agg-val-c7` TIMED_OUT (since fixed) |
| Async: Batch render (satdump + nasa) | **Ran on real data, as a replay** | `ProductsWritten=11` / `14` after 2026-10-22 (CONTACTS.md) |
| Async: **fresh live contact -> auto-trigger -> Batch end to end** | **Never exercised** | The last SDR execution that EventBridge started is 2026-10-17 (CodeBuild era). Every execution since then is a hand-started replay. No contact has flown since the migration |
| Sync: mission profile, endpoint, EC2, SFN, alarms deployed | **Deployed** | Plan clean. State machine `groundstation-noaa20-sync-pipeline` exists. Rules `contact-prepass-sync` and `contact-completed-sync` ENABLED. Receiver `i-06a51f5b...` (c6i.xlarge) is `stopped` (expected between contacts). All 6 sync alarms are OK |
| Sync: contacts offered | **Verified today** | `scripts/gs_list_sync_contacts.ps1`: Cape Town 20, Hawaii 17, Ohio 22, Oregon 29, Stockholm 36, Ireland 0, Singapore 0 (7-day window) |
| Sync: flown | **Never.** Zero executions in `groundstation-noaa20-sync-pipeline` | Execution list is empty |
| Sync: packet-format gate (REL-5) | **Open** | `docs/SYNC_PIPELINE.md` "Packet-format validation gate". The parser assumes a 7-word VITA-49 header (`scripts/sync_receiver_listen.py`) that AWS says is different |
| Sync: receiver `/opt` prerequisites | **Unverified.** Hand-installed, no `user_data` | `SYNC_PIPELINE.md` "Manual prerequisites". `sync-receiver-opt-drift` alarm is OK, but I did not SSM into the box (that would be a write call) |
| Sync: Batch aggregation `RTSTPS=off` | **Coded, deployed, never run on real data** | `SYNC_PIPELINE.md` NOTE after the sequence diagram; HANDOFF |
| Scheduler cron | **DISABLED** (verified) | Rule `groundstation-noaa20-demo-scheduler-cron` DISABLED. `pcap-uploaded` rule also DISABLED (intended) |
| Plumbing | **Verified present and enabled** | Batch CEs (fanout, agg) ENABLED/VALID, queues ENABLED/VALID, 4 Lambdas (python3.12), 15 groundstation alarms all OK with actions enabled. Two SNS email subscriptions exist, so alarms do reach a human (I did not confirm which topic the alarms publish to) |
| Tests | 288 pass, 6 skip, **5 fail** locally | All 5 are in `tests/test_nasa_render_bugs.py` (`TestBug2`/`TestBug3`) with `ModuleNotFoundError: matplotlib`. This is a local `test_env` gap, not a code defect (the HANDOFF reported 287 passing in a fuller env). It is still a red suite for a newcomer |

### 1b. Well-Architected, briefly (prior art: `docs/ARCHITECTURE_REFINEMENT.md` -> backlog + "Explicitly accepted tradeoffs"; `docs/ARCHITECTURE.md` "Accepted architectural tradeoffs"; `docs/reviews/REVIEW_SOLUTIONS_ARCHITECT.md`; spec `architecture-refinement` 40/40 tasks done)

| Pillar | Assessment | Open vs accepted |
|---|---|---|
| Operational Excellence | IaC with clean plan, apply guard hook, runbooks, `verify_pass.py`, alarms to SNS. | **Open:** `/opt/rt-stps` and `/opt/SDR_4_1` are hand-installed. Drift is *detected* (alarm), not rebuilt. This is deliberately deferred (ARCHITECTURE.md ~L223-228) and is the real operational risk for sync. `verify_pass.py` has no sync awareness (no "sync" token in the script), so a first sync pass must be verified by hand |
| Security | CMK, least-privilege roles, Checkov in the workflow, budget guard. | **Accepted:** single shared KMS key, single region. Nothing open that matters for a demonstrator |
| Reliability | Idempotence marker, `ChunksFailed` metrics, all-failed alarm, Spot only for restartable chunks. | **Accepted:** single-AZ receiver, `ToleratedFailurePercentage=100`. **Open (real):** the sync path has no S3 buffer. If the receiver is not ready before AOS, the pass is lost. The mitigation is PREPASS (T-600s) plus a `ReceiverReadyFailure` alarm, and it has **never been exercised live** |
| Performance | LUT cache (PERF-1), Batch removes the CodeBuild throttle. | Fine. The LUT cache was cold on contact #7. The fast path is not shown anywhere as having been hit |
| Cost | Cron disabled, $500 account budget. Compute ~$1-2 per contact against ~$110-130 antenna time. | **Accepted:** the budget is account-wide, not Ground Station-scoped. Antenna time dominates, so no more engineering is warranted |
| Sustainability | Spot, stopped receiver, no idle compute. | Nothing open |

**Verdict.** For a one-person demonstrator the architecture is sound and the trade-offs are written down. What is missing is not design, it is **proof**. Two proofs are missing: a post-migration contact flown through the whole Batch chain, and a first sync contact.

### 1c. Recommended actions

**Must, before calling it "operational" for both modes**
1. **Fly one fresh async contact on the Batch chain** (S, about $110-130). This is the only way to prove EventBridge -> SFN -> Batch fan-out -> agg -> render auto-triggers outside replay. Verify by products with `verify_pass.py`, not by the execution status.
2. **Fly one sync contact as a format-validation experiment** (M). Prerequisites, in order:
   - Hand-install `/opt` on the receiver.
   - Do a dry arm via SSM without a contact, to prove the listener binds.
   - Reserve one sync contact. Stockholm 1 has 36 offered windows and is in the same region as the receiver, which avoids cross-region UDP from the us-west-2 stations. This is my inference and I did not verify the delivery path.
   - Keep the `--raw-tee` capture.
   - Inspect the capture, then record the verified header layout in `SYNC_PIPELINE.md`.
   - Expect RT-STPS `frame_sync` to possibly fail on the first pass. The doc already says "every pass is a format validation experiment".
3. **Write the first-sync-contact checklist** (S) so (2) is repeatable (see Q2).

**Nice to have**
- Make `verify_pass.py` accept the sync state machine (S/M). Do this after (2), once you know what a sync pass produces.
- Add `matplotlib` to `test_env` or mark those 5 tests `skipif` (S). This gives a green suite out of the box.
- Close the SFN-level "dispatch vs completion" blind spot for the sync path after the first flight (S).

---

## 2. Documentation

**Verdict: partly.** Depth and honesty are strong. Examples:
- `SYNC_PIPELINE.md` and `DEPLOYMENT.md` mark what is unverified.
- `.kiro/skills/aws-ground-station` separates `[practice]` from public facts.
- `validate-a-pass` enforces "verify by product evidence".

Navigation from the entry docs has stale or contradictory spots.

### Can a newcomer go README -> run/validate a pass?
- **Async: yes.** README "Booking a pass / Processing a pass" gives the commands. The reprocess recipe is correct and points at the HANDOFF. `validate-a-pass` gives the verification.
- **Sync: no.** Pieces exist: `SYNC_PIPELINE.md` "Manual prerequisites", "Choosing which pipeline runs", and the gate steps. They are scattered. There is no ordered "fly your first sync contact" checklist, and no README quick-start for it. README only describes sync as "not yet flown".

### Contradictions and stale statements found
| # | Where | Problem |
|---|---|---|
| 1 | `docs/OPERATOR_RUNBOOK.md` (whole file) | Titled "Post-Refinement Follow-ups", "None are blocked". Section 3 tells the operator to **run the compute bake-off** via `./scripts/bakeoff/run_bakeoff.sh`. That script is **absent** (`scripts/bakeoff` does not exist). ADR-001 was already Accepted (2026-10-22) and the bake-off superseded by the Batch migration. Section 2 refers to applying refinement Terraform that is long applied. The "standing reminders" at the bottom are still valid and useful. **Not linked from README** |
| 2 | `CLAUDE.md` diagram | Still shows "CodeBuild x22 (SatDump) -> CodeBuild (RT-STPS + CSPP)". README says Batch since 2026-10-22 |
| 3 | `docs/CONTACTS.md` Contact #7 | Header table says `Status: SCHEDULED (reserved 2026-10-13)` and the title says "(scheduled)", but the same section later says FLOWN, PASS. The `Mission Profile` row says `32a1fdbb`, while `docs/WORKSTREAMS.md` says every booking so far used the **unmanaged** `2655b0f6` profile (contacts #1-#6 show `2655b0f6`). One of them is wrong for #7. I did not check AWS to settle which |
| 4 | `docs/GLOSSARY.md` | Describes sync as "an alternative ... path *would* let ...", but the path is built and deployed |
| 5 | `docs/DEPLOYMENT.md` | "Last verified 2026-09-01" with a patch-note for Batch. Readers must mentally translate "CodeBuild" to "Batch". Works, but fragile |
| 6 | README docs table | Does not list `OPERATOR_RUNBOOK`, `ARCHITECTURE_REFINEMENT` (it holds ADR-001/002 and the accepted-trade-off backlog), `CLEAN_COMPUTE_PIPELINE_*` (only the HANDOFF via one link), or `docs/reviews/` |
| 7 | Spec checkboxes | `clean-compute-pipeline/tasks.md` shows 18 open boxes (tasks 3.x, 10-15) although the HANDOFF/CONTACTS say Stage 3 render is done and validated. `compute-bakeoff-batch-spot` is 0/18 yet superseded. `noaa20-viirs-visualization` 17 open. So "locked vs active" is not readable from the specs. The Kiro review already flagged unmarked superseded specs |
| 8 | Duplicated sources of truth | Sync status is restated in README, `ARCHITECTURE.md` L20-28, `SYNC_PIPELINE.md`, `WORKSTREAMS.md` and the SKILL. They agree today, but any change touches 5 places. The contact log, `HANDOFF` and `MIGRATION` also overlap on the contact-7 Batch validation |

### Recommended actions
**Must (small)**
1. **Write the "First sync contact" checklist** (S). Put it as a short section in `SYNC_PIPELINE.md` and link it from the README. It covers the `/opt` install, dry arm, reserve, capture, inspect, record, verify. It matters because it is the only missing runbook for a headline capability.
2. **Fix the stale entry signposts** (S, one commit): `CLAUDE.md` diagram, Contact #7 status/title (and settle the 2655b0f6 vs 32a1fdbb question), GLOSSARY sync wording.
3. **Fix or archive `OPERATOR_RUNBOOK.md`** (S). Keep only "Standing operational reminders" (and the MCP TLS note if still relevant). Drop the bake-off section and the apply-refinement section. Link it from the README.

**Nice**
- Add 3 rows to the README docs table: OPERATOR_RUNBOOK, ARCHITECTURE_REFINEMENT, reviews (S).
- Add a one-line "Status: superseded / done / active" header to each `.kiro/specs/*/tasks.md` or `requirements.md` (S). Do not tick the boxes retroactively.
- Keep one canonical sync status line, and let other docs link to it (S).

---

## 3. Structure and navigation

**Verdict: mostly yes.** The top level is readable: `infra/` (modules per concern), `lambdas/`, `scripts/`, `docker/`, `buildspecs/`, `tests/`, `docs/`, `articles/`. Tests are in one place (`tests/`, plus `tests/integration/`). Generated artifacts are properly ignored: `.build/` (0 tracked files), `*.tfstate*`, `*.tfplan`, `output/`, `nasa_software/`, `test_env/`, `.hypothesis/`. The many `terraform.tfstate*.backup` and `*.tfplan` files in `infra/` are untracked and ignored, so they are clutter, not repo debt.

Observations:
| Item | Finding |
|---|---|
| `docs/` | 20 files at the top level, 37 files in total with `reviews/` and `diagrams/`. Largest: CONTACTS 548, UPLINK_AWS_VS_KSAT 631 (untracked, other session), ARCHITECTURE_REFINEMENT 488, SATELLITES_PBS_AND_UPLINK 388. Three "uplink" docs plus HTML copies live beside the core pipeline docs. They are a different topic and bury the entry docs. `SYNC_ONBOARDING_INVESTIGATION.md` has a "superseded" note at its top but is still listed beside live docs. `Asciiart-archi.txt` looks like a leftover |
| `scripts/` | 37 files at the top level, plus `archive/` (17 files in `cspp-debug`), `viirs/`, `tle/`, `slides/`. 10 `gs_*_trial*.ps1` scripts are throwaway investigation scripts for the sync onboarding fix (`gs_trial6/7/8`, `gs_bisect`, `gs_h6_h7`, `gs_diff_template...`, `gs_aqua_template_trial`, `gs_sync_profile_trial/2`). Only `gs_list_sync_contacts.ps1` is reusable. The PR #5 review already notes non-unique trial script names as a follow-up. The `vita49_*` scripts (4) and `inspect_pcap.py` are analysis tools mixed with pipeline entrypoints. `scripts/bakeoff` is referenced by docs but missing |
| Dead or disabled code | `infra/modules/processing_pipeline` (disabled: `enable_processing_pipeline=false` in `terraform.tfvars`) with `lambdas/data_processor` and `processing/` (3 old buildspecs, a Dockerfile). `infra/modules/compute_bakeoff` (superseded by Batch, gated off, spec 0/18). `buildspecs/` is mostly legacy (README says so). The CodeBuild fallbacks are retained deliberately and documented |
| Root | `contacts_async.json`, `contacts_sync.json` are tracked one-off offer-set dumps at the repo root. `Slidedecks/` holds two binary pptx versions. `postprocessing/` and `processing/` are two overlapping folders for legacy processing code |
| `.kiro/` | Steering (4 files), 4 skills and 7 specs are well organised. The skills carry the knowledge that matters. Specs lack a visible locked/active/superseded marker (see Q2 #7) |
| Worktrees / branches | `.worktrees/` is untracked and not ignored (appears in the listing). Two remote doc branches remain (`docs/pbs-uplink-digital-twin`, `docs/uplink-aws-vs-ksat`), and the latter is another session's work |

### Recommended actions (few, high value)
1. **Move throwaway trial scripts to `scripts/archive/sync-onboarding-trials/`** (S) and keep `gs_list_sync_contacts.ps1` plus the few reusable helpers at the top level. This removes ~9 files from `scripts/` that look like pipeline tools.
2. **Add a short `docs/README.md` index** (S) with three groups: Run it (ARCHITECTURE, DEPLOYMENT, SYNC_PIPELINE, OPERATOR_RUNBOOK, CONTACTS), Decisions and history (REFINEMENT, CLEAN_COMPUTE_*, reviews, SYNC_ONBOARDING_INVESTIGATION, MEDITERRANEAN_PASS), Side topics (SATELLITES_PBS_AND_UPLINK, UPLINK_*, DIGITAL_TWIN_*). This is cheaper than moving files, and it fixes the sprawl at the point where it hurts.
3. **Mark superseded specs and obsolete modules** (S): a one-line status in `compute-bakeoff-batch-spot` and a note on `processing_pipeline`. Deleting `compute_bakeoff` and `processing_pipeline` is optional (M, needs a terraform plan and an apply by the orchestrator). They are harmless while gated off.
4. Optional: add `.worktrees/` to `.gitignore`, and move the two root `contacts_*.json` into `output/` or `docs/data/` (S).

Not recommended (over-engineering for this demonstrator): reorganising `docs/` into folders, splitting `scripts/` into packages, consolidating all CodeBuild remnants, adding CI, per-domain KMS, multi-AZ or DR.

---

## Unverified / not checked
- Whether `/opt/rt-stps`, `/opt/SDR_4_1` and the `/opt/scripts` files are present on the stopped sync receiver (needs an SSM or instance start).
- Which SNS topic the alarms publish to, and whether the subscriptions are confirmed. Two email subscriptions exist on the `demo-contact-*` topics.
- Which mission profile (`32a1fdbb` or `2655b0f6`) contact #7 was actually booked on.
- Content of the other session's untracked uplink and KSAT docs (ignored by request).
- I read the pytest failure cause (`matplotlib` missing) but did not install anything.
