---
inclusion: manual
---

# Article Series — Objectives & Tone (steering)

Persistent guidance for the blog-article series about this project
(protogroundstation: receiving NOAA-20 X-band downlinks via AWS Ground Station
and processing raw RF into geolocated VIIRS imagery, all in the cloud).
Load this file before drafting or editing any article.

## Author & publication context

Written by an **AWS Certified Architect and Business Analyst**, for the
**AWS Ambassador program**. Two consequences for every article:

- **Honest, critical assessment of AWS.** Show the value AWS genuinely brings —
  and say plainly where it costs too much, imposes a constraint, or is the wrong
  tool. Ambassador content earns credibility by being critical, not by advocating.
  No marketing register; no "unlock", "seamless", "game-changing".
- **Architect + analyst voice.** Argue from architecture (services, data flow,
  trade-offs) and from business value (cost, time-to-first-result, what capability
  is bought, what the alternative would have cost).

## Objective

Show, concretely and honestly, **what it takes to turn raw satellite radio into a
geolocated Earth image using rented cloud infrastructure** — the capabilities you
gain at each layer and the limits you hit — so a technical reader could reason
about doing something similar. Educational, not promotional.

## Audience

Technically literate generalists (cloud/data/software engineers, remote-sensing-curious).
Assume comfort with AWS and pipelines; do **not** assume RF, CCSDS, or satellite-
processing background — introduce those terms in one line when first used.

## Series structure (each ~600–800 words, English, Medium/dev.to register)

1. **Cloud + open source** — AWS Ground Station + SatDump: raw RF → imagery, and
   the geolocation limit (~100–300 km with TLE-only overlay).
2. **NASA software** — RT-STPS + CSPP SDR: what the official stack *adds*
   (calibrated Level 1 + sub-km per-pixel geolocation → true, aligned maps) and
   what it takes to run in the cloud.
3. **The end-to-end result / pragmatic path** — the working pipeline and the
   engineering lessons.
4. **File formats** — the chain `.pcap`/VITA-49 → `.cs8` → `.cadu` → RDR HDF5 →
   SDR+GEO HDF5 → GeoTIFF; theme "strip a container, add meaning".

## Tone rules

- **Neutral and factual.** State results plainly. Avoid dramatized phrasing
  ("humbling", "brutal", "the X surprise", "nightmare", exclamation marks).
- **Lead with capability, not struggle.** Each article's spine is *what you can do
  and what it costs / where it stops* — not a debugging diary. Failures appear only
  as short, generalizable lessons (one or two lines), never as the main narrative.
- **Numbers over adjectives.** Prefer concrete figures (GB, km, minutes, dollars,
  band names) to qualitative emphasis. Draw pass-specific numbers from docs/CONTACTS.md.
- **Honest about limits.** Name what does *not* work or is approximate; don't
  oversell. But frame limits as boundaries of an approach, not as defeat.
- **Show, then explain.** Diagrams/images carry structure; prose explains the "why".
- **Prefer a schema to a paragraph.** If something can be a diagram, a table, or a
  labelled flow, make it one and keep the prose to what the picture cannot say.
  Text is the connective tissue between figures, not the primary carrier.

## Diagrams

- **At least one AWS architecture diagram per article**, generated with
  **[awslabs/diagram-as-code](https://github.com/awslabs/diagram-as-code)** (`awsdac`,
  YAML → PNG, official AWS icon set) so the figures are reproducible and consistent
  across the series. Keep the YAML in the repo and export PNG for Medium/dev.to.
- Each diagram shows the real services and data flow for that article's stage —
  Ground Station, S3, EventBridge, Step Functions, CodeBuild, ECR — not a generic
  reference architecture.
- Non-AWS structure (format chains, packet layouts, processing levels) stays in
  mermaid or ASCII; use the AWS icon set only for AWS infrastructure.
- Annotate figures with the numbers that matter: data volume, duration, cost.

## Business value

Every article states, explicitly, what the stage is **worth**: the capability
acquired, its cost (antenna minutes, compute, storage), the time to first usable
result, and the build-vs-rent comparison (owning an X-band ground station vs.
renting one by the minute). Where a capability is not worth its price, say so.

## Factual anchors (keep articles consistent with these — verified 2026-07-24)

- **CSPP SDR WORKS.** The official NASA path is not a dead end. It produces
  calibrated Level 1 SDR + terrain-corrected per-pixel geolocation (sub-km).
  Root cause of the long struggle was mundane: `viirs_sdr.sh` reads the spacecraft
  from the RDR **filename** (`_j01_`); renaming the RDR broke it. See docs/CSPP_SOLVED.md.
  → Article 2 must present the NASA stack as **working**, with the difficulties
  compressed to a couple of lines of lessons.
- **Delivered results to feature:**
  - contact-02 (daytime): full-pass **true-color** GeoTIFF + coastline overlay,
    **sub-km geolocation** (coastlines land exactly). This is the hero image.
  - contact-03 (night): thermal-IR GeoTIFF, sub-km. Good contrast (night → thermal
    only; daytime → true color).
- **Featured pass = contact #2** (daytime, true-color-capable). BUT verify its
  ground station: docs/CONTACTS.md currently says "Hawaii 1 / Pacific", which contradicts
  the output dir `contact-02_ohio-1` and the actual imagery (North America → Caribbean:
  Hudson Bay, Great Lakes, Florida, Cuba). Resolve before publishing; the scene is
  **eastern North America to the Caribbean**, not the Pacific.
- Geolocation numbers: SatDump/TLE overlay ≈ 100–300 km; CSPP per-pixel ≈ sub-km.
- Cost anchor: ~$100–130 antenna time per pass, no software licences.

## Editing conventions

- Edit the working drafts in `articles/` (current) — `articles/version1/` holds the
  first version for reference. Preserve mermaid/ASCII diagrams; note that Medium/dev.to
  need diagrams exported as images.
- Diagram sources live in `articles/diagrams/` (one `.yaml` per figure), rendered with
  `awsdac <figure>.yaml -o out/<figure>.png -f` into `articles/diagrams/out/`.
  Reference the exported PNG from the article; keep the YAML committed.
  `awsdac` v0.23 is installed at `C:\Users\mbonnet\tools\awsdac` and is on the user PATH.
  Schema notes: region blocks are `AWS::Region` (not `AWS::Diagram::Region`); set
  `Direction: vertical` on a container to stack rows; link captions go under
  `Labels: { AutoRight: { Title: ... } }`; use `Type: orthogonal` for right-angled
  links between rows. Link labels land at the midpoint of long diagonals and collide with
  other text — keep them under ~5 words, or anchor them with `TargetLeft`/`TargetRight`
  instead of `AutoRight`. Multi-line titles are not supported (`\n` is ignored); keep
  resource titles short and put detail in the article caption.
  Reference figures: `articles/diagrams/article-{1,2}-*.yaml`.
- Keep each article self-contained but cross-linked as a numbered series.
