# docs/ index

Start with the [repo README](../README.md). This page groups everything in `docs/` so you can
find the right file without opening them all.

## Run it

| Doc | Read it when |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | You want the system in one picture, the two data pipelines (ASCII, sync and async), the state machine, and the accepted trade-offs |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Before touching CSPP or the images: the exact conditions CSPP needs and what is deployed |
| [SYNC_PIPELINE.md](SYNC_PIPELINE.md) | The synchronous (demod/decode) path, and the **first sync contact checklist** |
| [LESSONS_SYNC_FIRST_CONTACTS.md](LESSONS_SYNC_FIRST_CONTACTS.md) | Why sync contacts #8 and #9 failed, and what changed (read before the next sync contact) |
| [DDX_AMI_INSPECTION.md](DDX_AMI_INSPECTION.md) | What the Ground Station dataflow endpoint app (DDX) AMI contains, and its boot traps |
| [Spec sync-receiver-ddx](../.kiro/specs/sync-receiver-ddx/design.md) | The receiver rebuild around DDX: requirements, design, dry run, cutover, Well-Architected deviation register |
| [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) | Standing rules: verify a pass, alarms, scheduler, apply rule, drift |
| [CONTACTS.md](CONTACTS.md) | Every pass booked so far: what it contained, what it cost, what went wrong |
| [SATDUMP.md](SATDUMP.md) | Before touching geolocation: what SatDump already does |
| [GLOSSARY.md](GLOSSARY.md) | A term is unclear (DigIF, CADU, RDR, PN...) |
| [WORKSTREAMS.md](WORKSTREAMS.md) | Before starting work: parallel sessions, coordination rules, AWS objects outside Terraform |

## Decisions and history

| Doc | What it records |
|---|---|
| [ARCHITECTURE_REFINEMENT.md](ARCHITECTURE_REFINEMENT.md) | Well-Architected target design, delta backlog, ADR-001/002 (compute) |
| [CLEAN_COMPUTE_PIPELINE_MIGRATION.md](CLEAN_COMPUTE_PIPELINE_MIGRATION.md) | CodeBuild -> AWS Batch migration, stage by stage |
| [CLEAN_COMPUTE_PIPELINE_HANDOFF.md](CLEAN_COMPUTE_PIPELINE_HANDOFF.md) | Migration handoff: what was validated, how to reprocess a contact |
| [MEDITERRANEAN_PASS.md](MEDITERRANEAN_PASS.md) | Planning a targeted acquisition (contacts #5, #6) and why the basin stayed out of reach |
| [SYNC_ONBOARDING_INVESTIGATION.md](SYNC_ONBOARDING_INVESTIGATION.md) | **Superseded** (2026-11-19): the "not onboarded" reading of the zero sync windows |
| [reviews/](reviews/) | Expert reviews, the Terraform clean-reference investigation, PR reviews, the 2026-11-19 repo review |
| [Asciiart-archi.txt](Asciiart-archi.txt) | Historical component-level ASCII diagrams (pre- and post-Batch migration); the current two-pipeline diagram is in [ARCHITECTURE.md](ARCHITECTURE.md#architecture-principle-converge-early-mutualize-the-rest) |
| [diagrams/](diagrams/) | awsdac sources (`*.yaml`) and rendered PNGs (`out/`), including the sync-contact data flow ([`sync-contact-dataflow.png`](diagrams/out/sync-contact-dataflow.png), receiver steps superseded) and the DDX receiver data path ([`sync-receiver-ddx.png`](diagrams/out/sync-receiver-ddx.png)) |

## Side topics (not the pipeline)

| Doc | Topic |
|---|---|
| [SATELLITES_PBS_AND_UPLINK.md](SATELLITES_PBS_AND_UPLINK.md) | Public broadcast vs operator-authorized satellites, uplink limits |
| [UPLINK_AWS_VS_KSAT.md](UPLINK_AWS_VS_KSAT.md) | Uplink: AWS Ground Station vs KSAT |
| [DIGITAL_TWIN_ONBOARDING_REQUEST.md](DIGITAL_TWIN_ONBOARDING_REQUEST.md) | Draft request to onboard the account to the digital twin |
| `*.html`, `pandoc-doc.css` | Rendered copies of the side-topic docs |

## Specs (`.kiro/specs/`) - status

The task checkboxes were not always ticked as work landed; trust this table and the docs above.

| Spec | Status |
|---|---|
| `aws-ground-control-demo` | Done (demo era) |
| `noaa20-cadu-to-tiff` | Done |
| `noaa20-viirs-visualization` | Delivered (visualization runs on Batch); remaining boxes not reconciled |
| `ec2-aggregation` | Superseded: aggregation moved to CodeBuild, then AWS Batch |
| `architecture-refinement` | Done (40/40) |
| `compute-bakeoff-batch-spot` | Superseded by the Batch migration (ADR-001 accepted without the bake-off) |
| `sync-receiver-ddx` | Requirements and design written (2026-11-26), no tasks yet: rebuild of the sync receiver around DDX (DTLS), AWS template operating model |
| `clean-compute-pipeline` | Stages 1-3 deployed and validated on contact #7 data; remaining boxes are follow-ups (see the HANDOFF) |
