# Sync (demod/decode) onboarding investigation -- NOAA-20

**Date:** 2026-10-22
**Account:** 471112743408 (`mbonnet-adm@soprasteria.cloud`, AWSAdminAccess) . Region eu-central-1
**Satellite:** NOAA-20 / JPSS-1 (NORAD 43013, UUID `33f035e1-73f7-47a5-9df8-fbc48636dca8`)
**Question:** Is this account's NOAA-20 onboarded for SYNCHRONOUS (demod/decode) Ground Station
use, or only for ASYNCHRONOUS (DigIF / S3 data delivery)?

## TL;DR

Live Ground Station evidence shows NOAA-20 on this account can schedule **asynchronous (DigIF)**
contacts but returns **zero schedulable windows for the synchronous (demod/decode) mission
profile** -- across three ground stations and multi-day windows. This is consistent with the
satellite being onboarded for the DigIF dataflow but **NOT** for the antenna-downlink-demod-decode
dataflow on this account. It matches the error previously hit (and contradicts the PSA statement
that both were onboarded). For a public broadcast satellite, the demod/decode path is subject to
AWS onboarding/eligibility that is separate from DigIF; it is not automatically available.

This is the root cause of the long-standing "Stage 2 sync-branch not validated" blocker: the sync
path has never had a contact to run because none can be scheduled.

## Background -- two different dataflows, two different onboardings

AWS Ground Station supports (among others) two downlink dataflows relevant here:

- **Asynchronous / DigIF + S3 data delivery** -- the antenna delivers raw digitized RF (VITA-49
  Signal Data/IP, "DigIF") to an S3 bucket. The customer demodulates/decodes off-line (SatDump,
  etc.). This is the pipeline that has been flown successfully (contacts 1-7).
- **Synchronous / demod-decode to a dataflow endpoint** -- Ground Station demodulates + decodes
  on the antenna side and streams `UncodedFramesEgress` (VITA-49 Extension) live over UDP to a
  customer EC2 "receiver" running the GS dataflow-endpoint / DDX agent. This is the JPSS-1 / NASA
  Direct Readout Labs (RT-STPS + IPOPP) use case.

Onboarding a satellite is a multi-step process (data collection, technical validation, spectrum
licensing, NDAs, integration + testing) and is done per capability. Being onboarded for one
dataflow does **not** imply the other. For public broadcast satellites the demod/decode
configuration in particular has to be validated/enabled; it is not granted by default just
because DigIF works.

### AWS documentation references
Content below is paraphrased/summarised for licensing compliance; see the sources for specifics.

- Public broadcast satellite utilizing a dataflow endpoint (demodulated and decoded) --
  the demod/decode-to-dataflow-endpoint example, explicitly the JPSS-1 / RT-STPS + IPOPP path.
  https://docs.aws.amazon.com/ground-station/latest/ug/examples.pbs-dataflow-endpoint-demod-decode.html
- Public broadcast satellite utilizing Amazon S3 data delivery -- the DigIF-to-S3 path (what we
  use today). https://docs.aws.amazon.com/ground-station/latest/ug/examples.pbs-to-s3.html
- How AWS Ground Station works / Getting started -- onboarding is a multistep process (data
  collection, technical validation, spectrum licensing, NDAs, integration + testing).
  https://docs.aws.amazon.com/ground-station/latest/ug/how-it-works.html
  https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.step1.html
- AntennaDownlinkDemodDecodeConfig -- the config type used to receive demodulated+decoded
  downlink in a mission profile.
  https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-groundstation-config-antennadownlinkdemoddecodeconfig.html
- Set up and configure Amazon EC2 -- the receiver EC2 for synchronous delivery of VITA-49
  Signal/IP or VITA-49 Extension data via the GS Agent or a dataflow endpoint.
  https://docs.aws.amazon.com/ground-station/latest/ug/dataflows.ec2-configuration.html

## What is already built on this account (so the gap is NOT our config)

The synchronous infrastructure is fully deployed in Terraform and present live -- the gap is
onboarding/eligibility, not missing resources:

Live `aws groundstation list-configs` (eu-central-1) shows, among others:

| Config name | Type |
|---|---|
| groundstation-noaa20-demo-sync-demod-decode | antenna-downlink-demod-decode |
| groundstation-noaa20-demo-sync-dataflow-endpoint | dataflow-endpoint |
| groundstation-noaa20-demo-sync-tracking | tracking |
| groundstation-noaa20-demo-antenna-downlink | antenna-downlink (DigIF, async) |
| groundstation-noaa20-demo-s3-recording | s3-recording (async) |

Mission profiles (`list-mission-profiles`):

| Name | ID | Purpose |
|---|---|---|
| groundstation-noaa20-demo-noaa20-hrd  | 32a1fdbb-99f1-413d-8905-436c5fccb382 | ASYNC DigIF |
| groundstation-noaa20-demo-noaa20-sync | 2e719f99-35f5-4720-b974-ba177036c1ee | SYNC demod/decode |

## The test we ran (read-only, non-destructive)

`list-contacts ... --status-list AVAILABLE` asks Ground Station for schedulable windows for a
given mission profile + satellite + ground station + time range. If the satellite is not
onboarded/eligible for the dataflow that the mission profile's configs require, GS returns no
AVAILABLE windows -- even though windows plainly exist (the async profile returns many for the
exact same satellite/station/time). This is a safe probe: listing does not reserve or spend.

Commands (the two profiles differ ONLY in config type; everything else identical):

```
SAT=arn:aws:groundstation::471112743408:satellite/33f035e1-73f7-47a5-9df8-fbc48636dca8
SYNC_MP=.../mission-profile/2e719f99-35f5-4720-b974-ba177036c1ee   # demod/decode
ASYNC_MP=.../mission-profile/32a1fdbb-99f1-413d-8905-436c5fccb382  # DigIF

aws groundstation list-contacts --region eu-central-1 \
  --mission-profile-arn "$SYNC_MP"  --satellite-arn "$SAT" \
  --status-list AVAILABLE --ground-station "<GS>" \
  --start-time <now> --end-time <now+N days> --query 'length(contactList)'
# repeat with $ASYNC_MP as the control
```

## Results (2026-10-22)

| Ground station | Window | SYNC (demod/decode) AVAILABLE | ASYNC (DigIF) AVAILABLE |
|---|---|---|---|
| Ohio 1 | now -> +2 days | **0** | 5 |
| Oregon 1 | now -> +2 days | **0** | 6 |
| Stockholm 1 | now -> +5 days | **0** | 22 |

Historical check (last 90 days, SYNC profile, statuses COMPLETED/FAILED/SCHEDULED/CANCELLED/
AWS_CANCELLED): `[]` -- **no sync contact has ever been scheduled, attempted, completed, or
failed** on this account. (Note: GS rejects scheduling/listing more than 7 days into the future
with `ResourceLimitExceededException` -- an unrelated account limit, not the onboarding signal.)

Same satellite, same stations, same windows, same account/region -- the ONLY variable is the
mission profile's dataflow (demod/decode vs DigIF). SYNC is uniformly 0; ASYNC is plentiful. That
is the onboarding/eligibility boundary, not a transient or a config bug on our side.

## Conclusion

- NOAA-20 on account 471112743408 is **onboarded for asynchronous DigIF** (proven: contacts 1-7
  flown, async AVAILABLE windows exist now).
- It is **effectively NOT usable for synchronous demod/decode**: GS offers zero schedulable
  windows for the demod/decode mission profile. The most likely cause is that the satellite is
  not onboarded/eligible for the antenna-downlink-demod-decode dataflow on this account (public
  broadcast demod/decode is gated separately from DigIF). This should be confirmed with AWS.
- Therefore the "Stage 2 sync-branch (RTSTPS=off) not validated" item is blocked UPSTREAM of our
  code: there is no sync contact to run. Our sync Terraform/configs are built and correct; they
  cannot be exercised until onboarding is resolved.

## Can we test it further?

- **Done (safe):** the read-only AVAILABLE probe above -- reproducible, costs nothing. Re-run it
  any time to check whether onboarding status has changed (sync count going non-zero = unblocked).
- **Definitive but spends/needs eligibility:** attempt `reserve-contact` against the sync profile
  on a concrete window. If onboarding is the blocker this returns an authorization/eligibility
  error (capture the exact message for the AWS case). We did NOT do this -- there were no AVAILABLE
  windows to reserve against, and a reserve is a billable/again eligibility-gated action; it should
  be an explicit operator decision, not run speculatively.
- **Offline alternative for the packet-format work (REL-5):** the sync receiver parser's VITA-49
  Extension header assumption (see `docs/SYNC_PIPELINE.md`) can only be validated against a real
  demod/decode capture, which requires a sync contact. Until onboarding is resolved, REL-5 cannot
  be cleared.

## Recommended next steps

1. Re-open with the AWS Alliance / PSA: share this evidence (SYNC AVAILABLE = 0 across 3 stations
   vs ASYNC plentiful, same satellite/account) and ask them to confirm whether NOAA-20 is
   onboarded for the antenna-downlink-demod-decode dataflow on account 471112743408, and if not,
   whether public broadcast demod/decode can be enabled.
2. Keep the sync Terraform deployed but flag-gated; keep the sync-aggregation CodeBuild fallback
   (do NOT retire via task 8) until a sync contact actually validates `RTSTPS=off`.
3. Once (if) onboarding is confirmed, re-run the AVAILABLE probe; when it returns non-zero,
   reserve one sync contact and clear REL-5 (packet-format validation) + Stage 2 sync validation
   together.

## Reproduce

```
source /projects/sandbox/.aws/envcreds.sh   # or your SSO creds
# SYNC should be 0, ASYNC should be > 0 for the same GS/window while onboarding is incomplete:
for MP in 2e719f99-35f5-4720-b974-ba177036c1ee 32a1fdbb-99f1-413d-8905-436c5fccb382; do
  aws groundstation list-contacts --region eu-central-1 \
    --mission-profile-arn "arn:aws:groundstation:eu-central-1:471112743408:mission-profile/$MP" \
    --satellite-arn "arn:aws:groundstation::471112743408:satellite/33f035e1-73f7-47a5-9df8-fbc48636dca8" \
    --status-list AVAILABLE --ground-station "Oregon 1" \
    --start-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u -d '+2 days' +%Y-%m-%dT%H:%M:%SZ)" \
    --query 'length(contactList)' --output text
done
```

## Conformance check vs. the AWS reference template (did we configure it correctly?)

Before concluding "onboarding," we verified our deployment against AWS's own reference,
"Public broadcast satellite utilizing a dataflow endpoint (demodulated and decoded)"
(https://docs.aws.amazon.com/ground-station/latest/ug/examples.pbs-dataflow-endpoint-demod-decode.html),
which is explicitly the JPSS-1/NOAA-20 + RT-STPS use case. Result: our demod/decode config and
mission-profile edge match the reference essentially exactly, so a config defect is NOT the cause
of the zero AVAILABLE windows.

Live-verified (get-config / get-mission-profile, 2026-10-22):

| Reference element | Our live value | Match |
|---|---|---|
| AntennaDownlinkDemodDecodeConfig center freq / BW / pol | 7812 MHz / 30 MHz / RIGHT_HAND | exact |
| Demod QPSK: CFR center 7812 +/-250 kHz; STR 15 Msps +/-0.75 ksps; RRC rolloff 0.5 | identical JSON | exact |
| Decode graph: I/Q-Ingress -> IQ-Recombiner -> CCSDS_171_133_VITERBI_DECODER(ONE_HALF) -> NRZ_M_DECODER -> UNCODED_FRAMES_EGRESS | identical node graph | exact |
| TrackingConfig Autotrack PREFERRED | PREFERRED | exact |
| MissionProfile dataflow edge: <demod-decode cfg>/UncodedFramesEgress -> dataflow-endpoint cfg | identical | exact |
| DataflowEndpointConfig -> named endpoint in a DataflowEndpointGroup on the receiver ENI | present (endpoint ...-sync-endpoint, group on receiver ENI) | yes |
| GS DataDelivery service role (ENI create/delete) | groundstation_role_arn on the endpoint group security_details | yes |

Non-material deviations (deliberate; do NOT affect contact eligibility):
- Receiver software install: AWS template uses EC2 UserData (ddx_streams + configure_streams.py +
  save_default_config.py); we install RT-STPS + listener by hand on the EBS volume (same pattern
  as our async aggregation box). GS only requires that a dataflow-endpoint listener is reachable
  on the ENI:port named in the dataflow endpoint group.
- Instance/network: reference m5.4xlarge in a dedicated VPC + cluster placement group; we use
  c6i.xlarge in the default VPC with a stable secondary ENI (REL-6 fixed-IP). Immaterial.
- Durations: reference pre/post 120/180 (endpoint group) and 120/60 (mission profile); ours
  600/120. A longer pre-pass is our choice (no S3 buffer); it does not affect whether windows
  are offered.

Why this matters: the config that Ground Station evaluates for demod/decode eligibility (the
antenna-downlink-demod-decode config + the mission-profile edge) is byte-for-byte the AWS JPSS-1
template at our frequency. With the config ruled out as the cause, the remaining variable is the
one the reference itself flags in its closing note -- "If ... JPSS-1/NOAA-20 ... are not onboarded
to your account, see Onboard satellite." i.e. demod/decode onboarding for NOAA-20 on this account.
