# Sync reception and the digital twin

Purpose: a side project that exercises the **synchronous** (demod/decode, live UDP) path against
the AWS Ground Station **digital twin**, because no real sync contact has ever been obtained.

## Status of the sync path

- Built and deployed as `infra/modules/sync_pipeline` (mission profile `noaa20_sync`,
  receiver EC2, `receiver_arm` Lambda, Step Functions, EventBridge PREPASS/COMPLETED rules).
- **Zero contacts obtained** (reported by the project owner). Nothing in the repo records the
  `list-contacts` output or an error, so the cause is **not established**. First action of the
  side project: capture the exact `list-contacts` / `reserve-contact` response for the sync
  mission profile and write it into `docs/CONTACTS.md`.
- Even with a contact, the packet format is unvalidated (`docs/SYNC_PIPELINE.md`, "Open risk").

## Hypotheses for "no contact offered" (to test, not to assume)

| # | Hypothesis | How to test |
|---|---|---|
| H1 | The dataflow endpoint group is missing, unhealthy, or its endpoint name differs from the `DataflowEndpointConfig` name. The code comment in `groundstation.tf` says no contacts are offered until an endpoint with that exact name exists. | `aws groundstation list-dataflow-endpoint-groups` + `get-dataflow-endpoint-group`; compare names and status |
| H2 | The mission profile is not valid for the satellite's onboarded capabilities (config values, frequency, bandwidth). AWS publishes a JPSS demod/decode example that should work. | Diff our configs against the AWS example (`https://docs.aws.amazon.com/ground-station/latest/ug/aqua-snpp-jpss.html`) |
| H3 | Pre/post-pass duration or minimum viable duration makes every window invalid (our profile uses `contact_pre_pass_duration_seconds` = 600 s; the AWS example uses 120 s). | Lower to 120 s on a test profile and re-list |
| H4 | Antenna / region: the receiver region must match the antennas considered. | List contacts per ground station |
| H5 | Account/satellite onboarding state differs between the async and sync paths. | `get-satellite`; ask AWS support |

Differences between our config and the AWS example already seen (not yet shown to matter):
tracking `PREFERRED` (same), `symbolTimingRecovery.range` 0.75 ksps (same), pre-pass 600 s vs 120 s,
`minimum_viable_contact_duration_seconds` from a variable vs 180 s.

## What the digital twin is (AWS documentation)

Source: https://docs.aws.amazon.com/ground-station/latest/ug/digital-twin.html

- A test environment for scheduling, configuration verification and error handling, **without
  production antenna capacity or spectrum licensing**.
- You must request onboarding of the satellite to the digital twin feature (via the satellite
  onboarding process).
- Twin ground stations are copies of the production ones, named with a **"Digital Twin " prefix**
  (same capabilities, site mask and coordinates). Find them with `ListGroundStations`.
- It emits the **same EventBridge events and API responses** as production.
- **No data delivery** (dataflows) and **no telemetry** on twin ground stations.
- `[unverified]` here: whether NOAA-20 can be onboarded to the twin, its pricing, and its
  quotas/limits. Check before relying on it.

## What it can and cannot test for the sync path

| Can test (events and APIs) | Cannot test (needs real data) |
|---|---|
| `list-contacts` / `reserve-contact` against the sync mission profile (settles H1-H4 if they reproduce) | Packet format of `UncodedFramesEgress` (REL-5 gate) |
| PREPASS event -> `receiver_arm` Lambda -> EC2 start -> SSM arm -> listener bind | UDP delivery to the receiver, `sync_receiver_listen.py` parsing |
| COMPLETED event -> Step Functions wiring, IAM, alarms (`ReceiverReadyFailure`) | RT-STPS frame sync, RDR output, CSPP |
| Timing of the pre-AOS arm (420 s Lambda, EC2 + SSM polling) against the 600 s budget | Link budget, elevation effects |

So the twin validates the **control plane**; the **data plane** still needs one real sync contact.

## Test plan

1. `aws groundstation list-ground-stations` and look for names starting with `Digital Twin`.
   None found: request twin onboarding for NOAA-20 (or a test satellite), and stop here.
2. List contacts on a twin station with the **sync** mission profile ARN. Record the result.
   Same call with the **async** profile as a control (it is known to work on real stations).
3. If sync lists nothing on the twin either, the cause is in our configs (H1-H3): fix them first.
4. Reserve one twin contact. Watch: PREPASS event, `receiver_arm` logs
   (`/aws/lambda/...receiver-arm`), EC2 start, SSM `sync_receiver_arm.sh`, listener bind,
   then COMPLETED and the state machine. Use the CloudWatch tooling in `.kiro/steering/aws-knowledge.md`.
5. Write the outcome in `docs/CONTACTS.md` and the verified/unverified split in
   `docs/SYNC_PIPELINE.md`. Do not mark the sync path flight-ready: the packet-format gate remains.

## Guardrails for the side project

- Reservation and twin usage may be billed: check pricing first and get the user's go-ahead.
- Keep the sync path's EventBridge rules filtered on the sync mission profile ARN; a twin contact
  on the async profile must not trigger the sync state machine, and vice versa.
- Receiver EC2 start/stop costs money: the Step Functions flow stops it after the pass; confirm
  it is stopped after each test.
- Never re-guess the VITA-49 header; capture with `--raw-tee` and inspect against the onboarding spec.
