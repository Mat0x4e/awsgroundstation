---
name: aws-ground-station
description: AWS Ground Station knowledge verified on this project's NOAA-20 pipeline, plus the repo's procedures (plan, reserve, process, verify). Use when working on Ground Station contacts, mission profiles, DigIF vs demod/decode (sync) reception, the digital twin, pass planning, CSPP/RT-STPS processing or contact verification in protogroundstation.
---

# AWS Ground Station - protogroundstation

Two kinds of content, kept separate on purpose:

- **Public/AWS facts**: from AWS documentation, cited by URL.
- **Verified in this project**: marked `[practice]`, observed on real contacts. Where practice
  differs from public documentation, practice wins and the difference is stated.

Anything not verified is marked `[unverified]`. Do not turn an `[unverified]` item into a fact.

## Project constants

| | |
|---|---|
| Account / region | `471112743408` / `eu-central-1` (SSO profile `AWSAdminAccess-471112743408`) |
| Satellite | NOAA-20 (JPSS-1), NORAD 43013 - HRD X-band 7812 MHz, RHCP, QPSK, 15 Msps, 30 MHz wide |
| Mission profiles | async DigIF -> S3: `terraform -chdir=infra output mission_profile_arn`; sync demod/decode: `... output sync_pipeline_mission_profile_arn` |
| Antennas onboarded for NOAA-20 | Cape Town 1, Hawaii 1, Ohio 1, Oregon 1, Stockholm 1 [practice] |
| Scheduler cron | **DISABLED on purpose** (it once booked two unintended paid contacts). Reserve by hand. |

## Hard rules

1. **A reservation is a paid act.** ~$110-130 for a ~10.5 min narrowband contact. Never script
   `reserve-contact` without the user's explicit go-ahead.
2. **`terraform apply` is run by the agent, only with the user's explicit agreement.** This repo has
   no CI pipeline (no `.github/workflows`); apply runs locally from `infra/`. Show the `plan` summary
   first, wait for the go-ahead, then apply. No go-ahead, no apply.
   Re-enabling the scheduler cron through an apply once booked two unintended paid contacts.
3. **7-day wall** [practice]: `ListContacts` throws `ResourceLimitExceededException` beyond 7 days
   ahead on this account.
4. **Plan against `ListContacts`, never against a propagator alone** [practice]: AWS offers only a
   subset of the geometrically possible passes (shared antennas).
5. **Effective horizon mask is ~10 deg, not 5** [practice] (window-edge elevation 9-11 deg over 32 offers).
6. **IAM/EC2 `description` fields must be ASCII**; `terraform validate` will not catch it.
   See `.kiro/steering/aws-knowledge.md`.

## Pricing and signal facts

- NOAA-20 HRD is 30 MHz wide, below the 40 MHz threshold, so it bills as **narrowband ($10/min)**,
  not wideband (AWS pricing page). The v15 deck assumed wideband: wrong.
- DigIF ships ~30x the demodulated bitstream: one ~10 min contact is ~43-59 GB of `.pcap`
  (22-27 chunks of ~2.18 GB) [practice].
- Public-broadcast (PBS) satellites such as NOAA-20: AWS manages the ephemeris; custom ephemeris
  goes through `CreateEphemeris` (enabled via support).
- AWS example template for JPSS DigIF is `AquaSnppJpssTerraDigIF.yml`; the demod/decode example is
  `AquaSnppJpss.yml` ([AWS example](https://docs.aws.amazon.com/ground-station/latest/ug/aqua-snpp-jpss.html)).

## Two reception paths

| | Async (DigIF) | Sync (demod/decode) |
|---|---|---|
| Config | `AntennaDownlinkConfig` -> `S3RecordingConfig` | `AntennaDownlinkDemodDecodeConfig` (`UncodedFramesEgress`) -> `DataflowEndpointConfig` |
| Delivery | `.pcap` chunks in S3 after the contact | live UDP to a receiver EC2 during the contact |
| Safety net | S3 holds the data; replayable | none: receiver must be listening before AOS |
| RT-STPS PN | `PnEncoded="false"` (SatDump derandomizes upstream) | `PnEncoded="true"` (no PN, no Reed-Solomon in the egress) |
| Status | flown on several contacts (`docs/CONTACTS.md`) | **0 contacts offered so far**; root cause found 2026-11-18 (missing `dataflowEndpointRegion` + `jsonencode`d `unvalidatedJSON`), Terraform fix pending - see `references/sync-and-digital-twin.md` |

Details: `docs/ARCHITECTURE.md` (async), `docs/SYNC_PIPELINE.md` (sync).

## Procedures

- **Plan a pass**: `scripts/plan_pass.py --days 7 --contacts-json contacts.json --step 5`
  (SGP4 + VIIRS swath + sun geometry). Runbook: `docs/MEDITERRANEAN_PASS.md` section 6.
- **List / reserve** (always pass the mission profile ARN of the path you want; EventBridge rules
  filter on it): see `docs/SYNC_PIPELINE.md` "Choosing which pipeline runs".
- **Verify a pass**: `python scripts/verify_pass.py <contact-id> --date YYYY/MM/DD --save-report output/contact-NN_...`
- **Pull products**: `aws s3 sync ... --exact-timestamps` (sidecars keep the same size when re-rendered).
- **Contact log**: `docs/CONTACTS.md`. Record every contact there, including failures.
- **Debug**: CloudWatch/Batch gotchas and the live-canary rule are in `.kiro/steering/aws-knowledge.md`.

## Practice vs documentation (what the public docs do not tell you)

- Calibration is all-or-nothing per granule: a partial granule from RF loss is dropped.
- From Stockholm, the Mediterranean is scanned in the opening ~2 min of the contact, at low link
  elevation; on contacts #5 and #6 nothing before ~AOS+2.5 min survived calibration. Cause of the
  early-granule loss is **not proven** `[unverified]`.
- Stockholm 1 is the only antenna within reach of the Mediterranean; Bahrain 1 is not available here.
- The effective horizon mask and the 7-day limit above.

## References

- `references/sync-and-digital-twin.md` - sync path status, the "0 contacts" problem and the
  digital-twin test plan (side project).
