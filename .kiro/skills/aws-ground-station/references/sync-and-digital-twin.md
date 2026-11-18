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

## Reproduced 2026-11-13 (read-only, `scripts/gs_list_sync_contacts.ps1`)

Window 2026-11-13 17:56Z -> 2026-11-20 16:51Z, NOAA-20, all 7 visible ground stations:

| Mission profile | Contacts offered |
|---|---|
| sync `2e719f99-...` (demod/decode) | **0 on every station** |
| async `2655b0f6-...` (DigIF), control | Cape Town 20, Hawaii 17, Ohio 25, Oregon 32, Stockholm 33; Ireland 0, Singapore 0 |

Dataflow endpoint group `da5e60d9-...` is `HEALTHY`, endpoint `groundstation-noaa20-demo-sync-endpoint`
at 172.31.23.74:55888, status `created`. That rules out H1 (missing or unhealthy group); the name match
with the `DataflowEndpointConfig` was not re-checked.

Observed differences, none proven causal: sync profile has pre-pass 600 s / min viable 300 s while the
endpoint group has pre-pass 120 s (the AWS example uses 120 s on both); the sync profile is the only one
using `AntennaDownlinkDemodDecodeConfig`. The AWS API docs state no constraint tying the two durations.

**Trial 1 (2026-11-13, `scripts/gs_sync_profile_trial.ps1`)**: throwaway clone of the sync profile with
pre-pass 120 s / post 120 s / min viable 180 s (same edges, same tracking config), deleted right after.
Result: **still 0 contacts on all 7 stations** -> H3 (durations) is ruled out.
Also checked: the demod/decode config (7812 MHz, 30 MHz, RHCP, QPSK 15 Msps, Viterbi 1/2, NRZ-M,
`UncodedFramesEgress`) is identical to the AWS example; the `DataflowEndpointConfig` name equals the
endpoint name in the group. Satellite `33f035e1-...` is onboarded on Cape Town 1, Hawaii 1, Ohio 1,
Oregon 1, Stockholm 1, ephemeris source SPACE_TRACK. Remaining suspects: H5 (satellite/account not
onboarded for demod/decode, onboarding is per capability `[unverified]`) or an undocumented rule.
AWS support confirmed (2026-11-18) that the account is onboarded for demod/decode -> H5 ruled out.

**Trial 2 (`scripts/gs_sync_profile_trial2.ps1`)**: AWS's template sets `DataflowEndpointRegion` on the
endpoint config; ours does not. Throwaway endpoint config WITH the region + throwaway profile (our demod
config, our tracking config, 120/60/180 s): **still 0 contacts**. Both deleted.

**Trial 3 (`scripts/gs_aqua_template_trial.ps1`)**: AWS's own `AquaSnppJpss.yml`
(`s3://groundstation-cloudformation-templates-eu-central-1/AquaSnppJpss.yml`) deployed in minimal form
(`CreateReceiverInstance=No`, no Lambdas, no SNS; needs an existing EC2 key pair even without EC2, a
throwaway one was created and deleted). Result, window 2026-11-18 -> 2026-11-25:

| Profile (from the AWS template) | Cape Town 1 | Hawaii 1 | Ohio 1 | Oregon 1 | Stockholm 1 | Ireland 1 / Singapore 1 |
|---|---|---|---|---|---|---|
| `37849 SNPP And 43013 JPSS` (demod/decode) | 22 | 19 | 30 | 30 | 39 | 0 / 0 |

**A demod/decode profile does get contacts for this satellite and account.** The template's JPSS
profile has the same antenna config as ours, so the cause is in what differs around it. Stack, key pair
and stack resources were deleted; only our 3 original profiles and 1 endpoint group remain.

Still different between the working template and ours (not yet isolated):
- the dataflow endpoint group: ours was built by Terraform (`awscc`), pinned to an existing ENI
  172.31.23.74 and the shared `groundstation-noaa20-demo-groundstation-role`; the template's group uses
  its own ENI, security groups and `DataDeliveryServiceRole`, endpoint name `<stack>-Downlink`;
- our endpoint config name/ARN and tracking config (same `PREFERRED` setting).
Trial 2 already excluded the profile durations and `DataflowEndpointRegion` against OUR group, which
points at the group (or the objects it references) rather than the profile.

**Trial 4 - bisect** (`scripts/gs_bisect_trial.ps1`, template stack up, 4 throwaway profiles created together,
listed after 150 s so propagation is not the explanation; same tracking config in all):

| Profile | Antenna config | Endpoint config (+ its group) | Contacts |
|---|---|---|---|
| A | ours | template's | 0 |
| B | template's | ours | 0 |
| C | template's | template's | **22 / 19 / 30 / 30 / 40** (Cape Town / Hawaii / Ohio / Oregon / Stockholm) |
| D | ours | ours (= the real sync profile) | 0 |

Only the template's PAIR works; swapping either half breaks it. Our tracking config is not the problem.

**Diff of the stored objects** (`scripts/gs_diff_template_vs_ours.ps1`, read-only):
- antenna config: **semantically identical**. Only differences: name/tags and the formatting of
  `unvalidatedJSON` - the template's is the AWS sample string as written (spaces, order `type`, `qpsk`,
  `edges`, `nodeConfigs`...), ours is machine-compacted with alphabetically sorted keys (Terraform
  `jsonencode`). **H6: the backend may match this string textually.** `[unverified]`
- endpoint config: the template's has `dataflowEndpointRegion: eu-central-1` (tested alone in trial 2: no effect).
- endpoint group: ours has an explicit `"mtu": 1500`, the template's has none; ours uses the shared
  `groundstation-noaa20-demo-groundstation-role` and its own SG; post-pass 120 s vs 180 s. Both HEALTHY.
  **H7: something in our group (explicit mtu, role, SG) makes contacts unavailable.** `[unverified]`

Two separate breakages (A and B) need two separate causes: H6 for the antenna side, H7 for the group side.

Next (cheap, template stack up, throwaway objects): (1) copy the template's antenna JSON, re-serialise it
compact + key-sorted like ours, pair with the template endpoint -> if it fails, H6 is confirmed;
(2) throwaway endpoint group copied from ours but without `mtu` / with a dedicated role -> pair with the
template antenna config.

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
