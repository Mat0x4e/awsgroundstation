# Business Analyst Review -- Functional Clarity

Lens: functional clarity only. This review asks whether a reader can understand WHAT the
system does, WHY, FOR WHOM, and at what value -- at different levels of granularity and for
different audiences. It does NOT assess code quality or infrastructure correctness.

Reviewer role: senior business analyst / quality reviewer.
Repo: /projects/sandbox/awsgroundstation (branch docs/post-batch-accuracy-review).
Scope reviewed: README.md; docs/ARCHITECTURE.md, CONTACTS.md, MEDITERRANEAN_PASS.md,
OPERATOR_RUNBOOK.md, DEPLOYMENT.md, SYNC_PIPELINE.md, SYNC_ONBOARDING_INVESTIGATION.md;
articles/ (parts 1-5 + part-2 published); .kiro/specs/*/requirements.md and tasks.md
(sampled: aws-ground-control-demo, noaa20-cadu-to-tiff, noaa20-viirs-visualization,
clean-compute-pipeline, ec2-aggregation).

---

## Executive summary

This is a strong solo-engineer demonstrator with unusually rich, honest documentation: the
value proposition is stated plainly in the first two lines of the README, the architecture doc
carries a real end-to-end sequence with measured timings and cost, and the contact log records
actual outcomes (including failures) pass by pass. Functional honesty is a standout strength --
the docs repeatedly and explicitly separate what WORKS from what is PLANNED or BLOCKED (the
sync demod/decode path, the parked SatDump projection, the uncalibrated Mediterranean target),
so a reader is rarely at risk of believing a capability exists when it does not. The chief
functional-clarity gaps are structural rather than substantive: there is no one-page
non-technical value/outcome summary for a sponsor, no mid-level feature catalogue bridging the
high-level README and the per-subsystem specs, and no single project glossary (domain jargon is
defined well in the articles and per-spec, but a developer landing in docs/ meets DigIF, CADU,
RDR, SDR, granule, and demod/decode before any consolidated definition). A material audience
mismatch also exists: the formal requirements -- the richest functional-spec source -- are
written in French prose (with English EARS keywords), which narrows their usefulness for a
non-French-reading reviewer or developer. Net: excellent depth and traceability for the
technical and operator audiences; partial for auditors; weakest for the non-technical sponsor.

---

## Strengths

1. **The value proposition is explicit and immediate.** README.md opens with "Receive a
   NOAA-20 X-band downlink on a rented AWS antenna and turn the raw radio into calibrated,
   geolocated VIIRS imagery -- no owned hardware, no software licences," then quantifies one
   contact (~10 min antenna time, ~43 GB raw, ~100 min to Level 1). The problem and the "no
   capex" angle do not have to be inferred. articles/article-1-cloud-opensource.md reinforces
   it with an own-the-dish vs rent-by-the-minute business table.

2. **Granularity has a genuine top-to-middle-to-detail path.** README.md ("Documentation" and
   "Repository layout" tables) -> docs/ARCHITECTURE.md (one diagram, one sequence with real
   timings, the processing chain, the Step Functions state table) -> .kiro/specs/*/
   requirements.md (per-subsystem acceptance criteria). The ARCHITECTURE.md "Where the time
   goes" and "Cost per contact" tables, and its "Accepted architectural tradeoffs" table, are
   model mid/high-level functional+rationale artifacts.

3. **Outcome traceability is real, not aspirational.** docs/CONTACTS.md logs seven contacts
   with actual delivered volumes, costs, coverage, and -- critically -- what calibrated vs what
   did not (e.g. contact #5: "GEO for 4, calibrated SDR for 1," and the explicit "The
   calibrated granule does not cover the Mediterranean"). tasks.md files carry
   `_Requirements: X.Y_` back-references (e.g. noaa20-cadu-to-tiff/tasks.md task 2.1 cites
   Requirements 1.1-1.7), giving requirement->task->delivered-outcome traceability that is rare
   at this scale.

4. **Functional honesty about limits is systematic.** DEPLOYMENT.md has a "What is deployed"
   status table and a "Known limit -- reception quality, not software" section;
   SYNC_PIPELINE.md leads with a "Blocker -- onboarding (read this first)" callout and an "Open
   risk -- packet format unverified" section; ARCHITECTURE.md warns twice that "a green
   execution does not mean imagery." A reader is actively steered away from false confidence.

5. **Requirements are testable and user-story-shaped.** The sampled specs use INCOSE/EARS
   acceptance criteria (THE ... SHALL, WHEN, IF/THEN, WHERE, WHILE) with explicit, measurable
   thresholds (e.g. noaa20-cadu-to-tiff Req 1.3: sample rate 34312500 Hz +/-1 Hz; Req 7.1:
   chunk end-to-end < 15 min). Each requirement has a "As a <role>, I want ... so that ..."
   user story.

6. **Domain jargon is well defined where the articles live.** articles/article-1 has a four-part
   glossary (Orbit and delivery; The signal chain; Instrument and scan geometry; Positioning and
   output) covering DigIF, HRD, VITA-49, I/Q, QPSK, Viterbi/Reed-Solomon, CADU, VIIRS, nadir,
   swath, composite, ephemeris, TLE, WGS84, GeoTIFF; article-2 adds a software/Level-1 glossary
   and cross-links back to part 1.

---

## Gaps

1. **No one-page non-technical value/outcome summary.** Nothing targets a sponsor who wants
   "what did we get, what did it cost, was it worth it" without reading the signal chain. The
   value framing in README and article-1 is strong but embedded in technical prose; the cost
   data (ARCHITECTURE.md "Cost per contact"; CONTACTS.md per-contact ~$80-130) and the outcome
   evidence (CONTACTS.md, DEPLOYMENT.md "Delivered products") are never pulled into a single
   outcomes-and-cost view. A sponsor must assemble it themselves.

2. **No mid-level feature catalogue.** There is a high-level picture (ARCHITECTURE.md) and deep
   per-subsystem specs, but nothing enumerates the discrete capabilities as a flat list -- e.g.
   "pass planning / feasibility ranking," "automated DigIF reception," "chunk fan-out decode,"
   "RT-STPS+CSPP aggregation," "geolocation (3 paths)," "visualization render," "post-pass
   verification," "sync demod path (blocked)," "cost guardrail/budget," "drift detection." A new
   developer must reverse-engineer the feature set from the README repo-layout table, the specs
   directory names, and ARCHITECTURE.md prose.

3. **No single project glossary, and jargon precedes definition in docs/.** The only glossaries
   live in the articles and inside each spec's "## Glossary" (and the spec glossaries are in
   French). A developer or auditor who enters through docs/ meets DigIF, CADU, RDR, SDR, granule,
   demod/decode, LUT, and VITA-49 (README, ARCHITECTURE.md, DEPLOYMENT.md) with no consolidated
   definitions and no link to the article glossaries. Grep confirms no top-level or docs/ GLOSSARY.

4. **Formal requirements are in French -- an audience-fit barrier.** The richest functional-spec
   source (.kiro/specs/aws-ground-control-demo, noaa20-cadu-to-tiff, noaa20-viirs-visualization,
   ec2-aggregation requirements.md) is written in French prose with only the EARS keywords in
   English. The newer specs (clean-compute-pipeline, compute-bakeoff-batch-spot) are in English.
   The result is a split-language requirements corpus: a non-French-reading reviewer or developer
   can read the keywords but not the intent, triggers, or error-handling clauses of the
   foundational subsystems.

5. **No index of which spec maps to which deployed/flown capability.** OPERATOR_RUNBOOK.md has a
   "Where the refinement artifacts live" table (good, but scoped to the refinement). There is no
   equivalent map of all seven specs to their status (implemented / partially implemented /
   superseded) and to the ARCHITECTURE.md path. An auditor must infer, e.g., that
   noaa20-cadu-to-tiff is the flown async path, that ec2-aggregation was superseded by Batch, and
   that aws-ground-control-demo's "Processing Pipeline (optional)" Req 4 is deliberately
   `enable_processing_pipeline = false` (DEPLOYMENT.md), not missing.

6. **"Planned vs delivered" is honest but scattered across many docs.** The planned/blocked/
   superseded distinctions are excellent individually but are spread over README, ARCHITECTURE,
   DEPLOYMENT, CONTACTS, SYNC_PIPELINE, SYNC_ONBOARDING_INVESTIGATION, and OPERATOR_RUNBOOK. There
   is no single "current capability state" board, so building an accurate deployed-vs-planned
   picture requires reading seven files. Contact #6 in CONTACTS.md is also flagged "OUTCOME NOT
   RECORDED ... Needs verification," a known traceability hole in the outcome log itself.

7. **Terraform feature flags are the de-facto capability switchboard but are not catalogued
   functionally.** `enable_sdr_pipeline`, `enable_sync_pipeline`, `enable_batch_compute`,
   `enable_processing_pipeline`, `enable_compute_bakeoff`, `RENDER_COMPUTE`, `RTSTPS=on|off`
   appear across docs as the real on/off state of features, but no single place lists each flag,
   what capability it gates, and its current value -- so "what is actually on right now" is hard
   to establish from the docs alone.

---

## Recommendations

Ordered by functional-clarity impact per unit of effort.

1. **Add a one-page sponsor/outcomes brief** (e.g. docs/OVERVIEW.md or a README "What this
   delivered" section): the problem, the no-capex proposition, 4-6 bullet outcomes ("received 7
   real NOAA-20 passes; produced calibrated sub-km true-colour over Ohio; ~$110-160 per pass; one
   targeted acquisition flown"), and the cost-per-contact table. Non-technical, no signal-chain
   vocabulary. Pulls from existing material in README, ARCHITECTURE.md, CONTACTS.md.

2. **Add a mid-level feature catalogue** (docs/FEATURES.md or a README table): one row per
   capability -> one-line description -> status (works / planned / blocked / superseded) -> owning
   spec -> where it lives in ARCHITECTURE.md. This simultaneously closes Gaps 2, 5, 6, and 7 and
   gives developers and auditors the missing bridge layer and the single capability-state board.

3. **Create a single project glossary** (docs/GLOSSARY.md) consolidating the two article
   glossaries and the per-spec terms, and link it from README's Documentation table and from the
   top of ARCHITECTURE.md/DEPLOYMENT.md. Define DigIF, CADU, RDR, SDR, GEO, granule, demod/decode,
   LUT, VITA-49, HRD, VIIRS, swath, nadir on first use or by link.

4. **Resolve the requirements-language split.** Either translate the four French requirements.md
   files to English to match the newer specs and the English docs/articles, or state the language
   policy explicitly and add a short English abstract (intro + requirement titles) at the top of
   each French spec so a non-French reader can navigate functional scope.

5. **Close the outcome-log hole.** Record contact #6's real disposition in CONTACTS.md (currently
   "OUTCOME NOT RECORDED") so the flown-pass evidence is complete and auditable.

6. **Add a feature-flag reference** (can be folded into the feature catalogue): each
   `enable_*`/`RENDER_COMPUTE`/`RTSTPS` flag, the capability it gates, and its intended current
   value, cross-referencing DEPLOYMENT.md "What is deployed."

---

## Per-audience clarity scorecard

| Audience | Can they get what they need? | Rating | Evidence |
|---|---|---|---|
| Non-technical stakeholder / sponsor (business value, cost, outcomes) | Partly. Value proposition and cost exist but are embedded in technical docs; no single outcomes-and-cost page, and the signal-chain vocabulary is unavoidable. Must assemble the business picture themselves. | Partial | README.md opening lines and "Cost per contact" (ARCHITECTURE.md); own-vs-rent table (article-1); per-contact cost in CONTACTS.md -- but no consolidated non-technical brief (Gap 1). |
| New operator (run / book / reprocess a pass) | Yes. End-to-end operational procedures with exact commands, guardrails, and gotchas. The strongest audience fit. | Good | README "Booking a pass" / "Processing a pass" / "Collecting a pass locally"; MEDITERRANEAN_PASS.md s6 booking; OPERATOR_RUNBOOK.md standing reminders + verify_pass.py; DEPLOYMENT.md CSPP recipe; explicit reprocess recipe and "SUCCEEDED is not proof of imagery" warnings. |
| New developer (understand the feature set and where it lives) | Mostly. Architecture, repo-layout, and deep specs are excellent; but no flat feature catalogue, no single glossary, and foundational requirements are in French. Must reverse-engineer the capability list. | Partial | README repo-layout + ARCHITECTURE.md processing chain and state-machine table (strong); but Gaps 2, 3, 4 -- feature set is implicit, jargon precedes definition in docs/, and 4 of 7 specs are French-language. |
| Auditor / reviewer (requirements traceability, deployed vs planned) | Mostly. Requirement->task traceability and deployed-state tables are genuinely good; weakened by scattered planned/blocked status, one unrecorded outcome, no spec-to-status index, and the French requirements. | Partial | EARS acceptance criteria + `_Requirements: X.Y_` tags (specs/tasks.md); DEPLOYMENT.md "What is deployed"; SYNC_ONBOARDING_INVESTIGATION.md evidence table -- but Gaps 4, 5, 6 (scattered state, contact #6 "OUTCOME NOT RECORDED", split language). |

Ratings use Good / Partial / Poor. No audience rates Poor -- the documentation is well above
demonstrator norms; the gaps are about consolidation, a single glossary, a feature catalogue,
and requirements-language consistency rather than missing substance.

---

## Functional completeness and honesty (explicit finding)

The documentation does clearly distinguish works vs planned/blocked, and the risk of a reader
believing a non-existent capability exists is LOW. The sync demod/decode path is labelled
"Blocked: cannot be flown" in the README documentation table, carries a read-this-first blocker
in SYNC_PIPELINE.md, and has a dedicated evidence file (SYNC_ONBOARDING_INVESTIGATION.md:
SYNC AVAILABLE = 0 across three stations vs ASYNC plentiful). The parked SatDump native
projection, the superseded bounding-box geolocation era, and the uncalibrated Mediterranean
target are all stated plainly in DEPLOYMENT.md, ARCHITECTURE.md, and CONTACTS.md. The one
honesty soft spot is dispersion (Gap 6) plus the single unrecorded contact #6 outcome -- issues
of consolidation and upkeep, not of misrepresentation.
