# Project quality reviews

Independent quality reviews of this project, each from a distinct expert lens. Produced
2026-10-22/23 against branch `docs/post-batch-accuracy-review` (post AWS Batch migration).

| Review | Lens | Headline |
|---|---|---|
| [REVIEW_BUSINESS_ANALYST.md](REVIEW_BUSINESS_ANALYST.md) | Business Analyst - functional clarity across audiences and granularity | Value proposition is clear; honesty/traceability strong; gaps are consolidation (no 1-page sponsor brief, no feature catalogue, no single glossary) + the French/English requirements split |
| [REVIEW_SOLUTIONS_ARCHITECT.md](REVIEW_SOLUTIONS_ARCHITECT.md) | Enterprise & Solutions Architect - architecture + Well-Architected | Adequate-to-strong; Security/Performance/Cost rate Strong, OpEx/Reliability/Sustainability Adequate; top gaps: hand-installed /opt/* outside IaC, image-build sourceVersion=main hazard, buffer-less sync SPOF |
| [REVIEW_SATCOM_EXPERT.md](REVIEW_SATCOM_EXPERT.md) | Satellite comms / ground segment - domain correctness, missed opportunities, SWOT | RF/signal chain accurate; refinements flagged (symbol-rate vs sample-rate, data-volume arithmetic); top opportunities: calibrate ATMS/CrIS/OMPS, fly the blocked demod/decode path, link-budget-driven ranking; dominant threat: NOAA-20 HRD broadcast sunset |
| [REVIEW_KIRO_EXPERT.md](REVIEW_KIRO_EXPERT.md) | Kiro usage - steering, specs, hooks, skills, agents, workflows | Maturity 4/5; load-bearing delegation/plan-only discipline lives only in non-auto-loaded prose; foreign nested "IoT" spec to delete; unmarked superseded French specs; no skills; recommends always-on delegation steering + a PreToolUse "no apply" guard + non-ASCII save hook |

These are review artifacts, not authoritative project docs. Cross-cutting themes that recur
across more than one review: (1) a missing single glossary / consolidation layer; (2) the
French/English requirements-language split in the demo-era specs; (3) codifying the
delegation / plan-only discipline where it is actually enforced; (4) the demod/decode
onboarding blocker and the NOAA-20 HRD sunset as the standout external risks.
