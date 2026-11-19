# AWS Ground Station digital twin - onboarding request (draft)

Draft 2026-11-18. Fill the `<...>` placeholders before sending. Paste the sections into the
matching fields of the Satellite Onboarding Questionnaire (see "How to submit" at the end).

---

## Request summary

Please onboard the public broadcast satellite **JPSS-1 / NOAA-20 (NORAD 43013)** to the
**AWS Ground Station digital twin** for AWS account **`<non-production account ID>`**.

The account will only be used for testing. Our production Ground Station account is
`471112743408`, where NOAA-20 is already onboarded and has flown several real contacts.

## Organization and contact

| | |
|---|---|
| Organization | `<organization name>` |
| Technical contact | `<name>`, `<email>` |
| AWS account to onboard | `<non-production account ID>` |
| Production account (reference) | `471112743408` |
| Control / data delivery region | `eu-central-1` (Frankfurt) |

## Mission summary

A research prototype ground segment for NOAA-20 direct broadcast, **receive-only**, using
public data. Production flow: Ground Station contact -> data delivery -> processing on AWS
(SatDump / RT-STPS / CSPP) -> VIIRS imagery products in S3.

We want the digital twin to test changes to the ground segment before they reach production,
without using production antenna time:

1. Contact scheduling through the API (`ListContacts` / `ReserveContact` / `CancelContact`).
2. Mission profile and config validation after infrastructure-as-code changes (Terraform).
3. The event-driven chain: EventBridge pre-pass and completed events -> Lambda -> receiver EC2
   start -> Step Functions, including IAM permissions and CloudWatch alarms.
4. Timing of the receiver arm sequence against the mission profile pre-pass duration.

## Satellite and signal

| | |
|---|---|
| Satellite | JPSS-1 / NOAA-20, NORAD 43013 (public broadcast satellite) |
| Launch date | 2017-11-18 (in orbit, operational) |
| Orbit | LEO, sun-synchronous, ~824 km |
| Ephemeris | AWS-managed (Space-Track), as in production |
| Downlink | HRD X-band, 7812 MHz centre, 30 MHz bandwidth, RHCP, QPSK 15 Msps |
| Uplink | None for the satellite (public broadcast, downlink only) |

## Ground stations requested (digital twin)

Mirror of the production onboarding:

- Digital Twin Cape Town 1
- Digital Twin Hawaii 1
- Digital Twin Ohio 1
- Digital Twin Oregon 1
- Digital Twin Stockholm 1

## Configurations to be tested

| Path | Configs | Delivery |
|---|---|---|
| Asynchronous (DigIF) | `TrackingConfig`, `AntennaDownlinkConfig` -> `S3RecordingConfig` | Amazon S3 |
| Synchronous (demod/decode) | `TrackingConfig`, `AntennaDownlinkDemodDecodeConfig` (`UncodedFramesEgress`) -> `DataflowEndpointConfig` | Dataflow endpoint group on EC2 (narrowband) |

Both are based on the AWS examples `AquaSnppJpssTerraDigIF.yml` and `AquaSnppJpss.yml`.

## Questions

1. **Account:** must the digital twin be onboarded in a separate non-production account, or can
   it be added to the production account `471112743408`?
2. **Pricing:** how are digital twin contacts billed (per minute, narrowband/wideband tiers,
   reserved vs on-demand)? Is there a free allowance?
3. **Quotas and limits:** do the production limits apply on the twin (number of reservations,
   the 7-day `ListContacts` horizon we observe in production, minimum contact duration)?
4. **Lead time:** how long does digital twin onboarding usually take for a public broadcast
   satellite that is already onboarded in production?
5. **Uplink (exploratory):** can a mission profile with an S-band `AntennaUplinkConfig`
   (2025-2110 MHz) and `UplinkEchoConfig` be created and scheduled on the twin, to exercise the
   uplink control plane? If not against NOAA-20, is there another option, such as a notional test
   satellite with customer-provided ephemeris? No RF transmission is expected or requested.
6. **Data path:** the documentation states that no data is delivered on the twin. Does the twin
   still check dataflow endpoint group health and emit the dataflow endpoint group state events
   (pre-pass / post-pass), as in production?

---

## How to submit

1. **Choose the account.** AWS states that digital twin onboarding happens in a non-production
   account. Either create one (in your AWS Organization), or send the request from production
   with question 1 and let AWS answer. Creating an account is your decision: it adds a new
   account to the organization, with its own billing and guardrails (SCPs).
2. **Sign in to the account to be onboarded** and open the AWS Ground Station console in
   `eu-central-1`.
3. Go to **Satellites and Resources**
   (<https://console.aws.amazon.com/groundstation/home#resources>) and open the **Satellite
   Onboarding Questionnaire**. This is the channel the
   [AWS onboarding guide](https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.step1.html)
   documents. Say explicitly that it is a **digital twin** request for a **public broadcast
   satellite**.
4. Paste the sections above into the matching fields. `[unverified]` The exact field layout of
   the form: adapt where the fields differ.
5. **Wait for AWS.** AWS notifies you once onboarding is done. Verify:

   ```powershell
   aws groundstation list-ground-stations --region eu-central-1 --profile <test-account-profile> --query "groundStationList[].groundStationName"
   ```

   Expect names starting with `Digital Twin`.

Fallback: account `471112743408` has **Basic support only** (the Support API returns
`SubscriptionRequiredException`), so it cannot open technical support cases. If the
questionnaire gets no answer, use the AWS contact that confirmed the demod/decode onboarding
on 2026-11-18, or your AWS account team.

After onboarding, the test account needs its own copy of the Ground Station infrastructure
(Terraform from `infra/`): mission profiles, S3 bucket, dataflow endpoint group, EventBridge rules.
