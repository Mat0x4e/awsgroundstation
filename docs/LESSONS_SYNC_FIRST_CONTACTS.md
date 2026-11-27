# Lessons learned: the first sync (demod/decode) contacts

Scope: contacts #8 (2026-11-22) and #9 (2026-11-26), both on the synchronous path
(Ground Station demod/decode -> dataflow endpoint group -> receiver EC2 -> shared
aggregation chain). Both FAILED; neither delivered a frame. This document records what went
wrong, why it was not caught earlier, what was fixed, and what changes in how this project
works. Per-contact facts are in [`CONTACTS.md`](CONTACTS.md) #8 and #9; the dry-run method is in
[`SYNC_DRY_RUN.md`](SYNC_DRY_RUN.md).

## Summary

Five independent faults stood between the design and a working sync contact. Four were
configuration faults found and fixed between the two contacts. The fifth is a **design fault**:
the receiver does not run the software that a Ground Station dataflow endpoint requires.

| # | Fault | Found by | Cost of finding it |
|---|-------|----------|--------------------|
| 1 | Dataflow endpoint group role had no EC2 ENI permissions | Contact #8 error message | 1 contact (#8) |
| 2 | Receiver could not start: CMK-encrypted root volume, Lambda role had no KMS access | Contact #8 CloudTrail / instance StateReason | same contact |
| 3 | Security group followed the Ground Station Agent pattern, not the dataflow endpoint pattern | Reading the AWS VPC guide while preparing the dry run | none (caught before #9) |
| 4 | receiver_arm fail-fast read a stale `stopped` state 91 ms after `StartInstances` | Live-faithful dry run 2026-11-23 | none (caught before #9) |
| 5 | Receiver runs a plain UDP socket; Ground Station requires a DTLS session terminated by the AWS dataflow endpoint application | Contact #9 raw capture (`16 fe ff ... 01` = DTLS ClientHello) | 1 contact (#9) |

Billing: Cost Explorer shows $0 for Ground Station on 2026-11-20..25, so contact #8 appears
not to have been charged (contact #9 not yet visible at the time of writing). AWS does not
document whether `FAILED` contacts are billed.

## Timeline

| Date (UTC) | Event |
|------------|-------|
| 2026-11-20 | Receiver dry run via SSM **with operator credentials**; passed. Found and fixed: no egress (EIP), listener not Python 3.9 compatible. |
| 2026-11-22 10:54 | Contact #8 PREPASS. receiver_arm Lambda starts the instance 3 times; each falls back to `stopped` (`Client.InvalidKMSKey`). |
| 2026-11-22 11:23 | Contact #8 `FAILED`: "Dataflow Endpoint Group's role is missing the necessary permissions to create Elastic Network Interfaces". |
| 2026-11-23 | Faults 1-3 fixed (PR #10). New project rule: dry runs mirror live conditions. Network canary Lambda added. |
| 2026-11-23 14:35 | Live-faithful dry run, attempt 1: aborted by fault 4. Fixed. |
| 2026-11-23 15:01 | Dry run attempt 2: PASS (real Lambda, canary, real state machine, Batch). |
| 2026-11-26 11:13 | Contact #9: Ground Station creates its ENI in our subnet (fault 1 fix confirmed). |
| 2026-11-26 11:19 | receiver_arm starts the instance, SSM Online, listener armed (faults 2 and 4 fixes confirmed). |
| 2026-11-26 11:26-11:40 | Listener receives 946 packets, ~1/s, ~127 B each (fault 3 fix confirmed); all DTLS ClientHello, 0 frames. |
| 2026-11-26 | Contact #9 `FAILED`, empty `errorMessage`. Diagnosed 14:30 from the raw capture. |

## Fault by fault

### 1. Dataflow endpoint group role without EC2 permissions

- **What**: the DFEG reused the async S3-delivery role (`groundstation-noaa20-demo-groundstation-role`,
  S3 + KMS only). Ground Station assumes the DFEG role to create the ENI it streams through.
- **Why missed**: the ENI is created only at contact time; nothing before AOS exercises it. The
  role was reused because it "was already the Ground Station role", without reading the DFEG
  role requirements.
- **Fix**: `DataflowEndpointEni` + `DataflowEndpointDescribe` statements (account/region-scoped),
  validated with `simulate-principal-policy`; confirmed live on #9 (CloudTrail
  `CreateNetworkInterface` by `groundstation.amazonaws.com`).
- **Lesson**: one role per purpose. Before reusing a role for a new service integration, read
  that integration's role policy example in the AWS docs.

### 2. Receiver start denied by the CMK

- **What**: the receiver root volume is encrypted with the pipeline CMK. Starting it requires the
  caller to create a KMS grant for EC2. The Lambda role had only `ec2:StartInstances`; EC2 accepted
  the call, then the instance fell back to `stopped` with `Client.InvalidKMSKey`.
- **Why missed**: the 2026-11-20 dry run started the instance with **admin credentials**, which
  have KMS access. The real principal was never exercised.
- **Fix**: KMS `CreateGrant` (`GrantIsForAWSResource`) + `Decrypt` / `GenerateDataKeyWithoutPlaintext`
  / `DescribeKey`, all via `kms:ViaService = ec2.<region>`; confirmed live on the dry run and #9.
- **Lesson**: a dry run that substitutes a privileged principal proves nothing about permissions.
  This is the origin of the dry-run rule (below).

### 3. Security group pattern

- **What**: ingress UDP from the Ground Station prefix list and no UDP egress. That is the
  **Ground Station Agent** pattern (public IPs). The **dataflow endpoint** pattern sends from an
  ENI that Ground Station creates in our own subnet with the DFEG security group, so the stream
  comes from a private IP and needs DFEG-SG egress + receiver-SG ingress.
- **Why missed**: the two patterns were conflated; the rule was written from memory of the agent
  pattern.
- **Fix**: self-referencing UDP 55888 ingress and egress on the shared SG; validated by the
  network canary and confirmed live on #9 (946 packets received).
- **Lesson**: Ground Station has two delivery patterns with different network, IAM and software
  requirements. Name the pattern explicitly in the design and follow that pattern's AWS page.

### 4. Stale instance state after `StartInstances`

- **What**: the new fail-fast check treated `stopped` + `StateReason` on the first poll as a
  failure. Right after `StartInstances`, `DescribeInstances` can still return the previous
  `stopped` state and its old `StateReason` (here, contact #8's `Client.InvalidKMSKey`).
- **Why missed**: the unit test encoded the same wrong assumption.
- **Fix**: fail only after a transition out of `stopped` was observed, or after a grace period.
- **Lesson**: eventual consistency also applies to EC2 state; never fail on the first poll after
  a mutation. Caught by the live-faithful dry run, at no contact cost.

### 5. DTLS: the receiver lacks the dataflow endpoint application (design fault)

- **What**: dataflow endpoint streams are encrypted with DTLS (self-signed certificates). Ground
  Station opens a DTLS session to the endpoint and streams data only once it is established. The
  receiver is expected to run the AWS-supplied **dataflow endpoint application** ("DDX"), shipped
  in private AMIs shared with the account after onboarding
  (`groundstation-al2023-ddx2.6.2-ami-*`, owner 575398917104, present in this account). Our
  receiver runs a plain ECS AL2023 AMI and a raw UDP socket, so the ClientHello was never
  answered and no frames were sent. The contact was adjudicated `FAILED` with an **empty**
  `errorMessage`.
- **Evidence**: `raw/raw_capture.bin` (120293 bytes, sha256 `62705a9f...`), every datagram
  `16 fe ff ...` with handshake type `01` and `client_version` `fe fd` (DTLS 1.2), empty cookie,
  ECDHE AES256-GCM/SHA384 cipher suites, retransmitted ~1/s (standard DTLS retransmit timer);
  receiver NetworkIn ~1 KB/s for the whole pass.
- **Why missed**:
  - The design assumed "Ground Station sends plain VITA-49 datagrams to the endpoint address".
    The listener's own docstring flagged the packet layout as an open risk, but the risk was
    framed as *which* VITA-49 layout, never *whether* the stream is plain UDP at all.
  - The AWS pages that state it (EC2 configuration "Supplied common software", "Data encryption
    during transit") were not part of the design review; the DFEG/VPC pages were.
  - The network canary sends plain UDP, so the dry run validated the path but structurally could
    not see a missing session layer. This was listed as an accepted risk ("real packet format"),
    which was too narrow: it should have read "real protocol on the wire".
- **Fix**: not yet. Requires a receiver redesign around the dataflow endpoint application
  (investigation in progress, report in `.agents/tasks/ddx-dataflow-endpoint-2026-11-26/`).
- **Lesson**: for a managed integration, identify every component the provider expects on the
  customer side (software, AMI, agent) before designing the custom part. "We will parse what
  arrives" is not a design when the provider negotiates a session first.

## Cross-cutting lessons (process)

1. **Dry runs mirror live conditions.** Same triggers, same principals, same starting state, same
   network path. Every deviation is listed beforehand with how it is validated; an unvalidated
   gap is a stated risk before booking. (Rule in
   [`.kiro/steering/delegation-and-safety.md`](../.kiro/steering/delegation-and-safety.md).)
   It caught fault 4 at zero cost.
2. **Phrase accepted risks at the right level.** "Packet format unverified" hid "protocol
   unverified". Write the risk as the broadest assumption it rests on.
3. **Read the provider's customer-side requirements first.** Faults 1, 3 and 5 are all stated
   in the AWS Ground Station user guide. A checklist of "what AWS expects us to provide" (role
   policy, SG pattern, endpoint software, AMI) would have caught all three before contact #8.
4. **An empty error message is not an absence of error.** Contact #9 failed with no
   `errorMessage`; the diagnosis came from evidence on the receiver (packet counters, raw
   capture, hex). Keep a raw tee of whatever arrives; it was the only evidence.
5. **Preserve evidence before cleanup.** On #9 the finish path never ran (no COMPLETED event),
   so the capture existed only on the instance. It was saved to S3 before stopping it. A
   `FAILED` contact should trigger the same evidence upload as a `COMPLETED` one.
6. **Operator presence on contact day.** The planned pre-check before #9 did not happen: the
   session only acts on user prompts, and the AWS SSO session had expired. The receiver was left
   running after the pass for ~3 hours. For a paid contact, schedule the pre-check and the
   post-pass check with the operator, and refresh SSO shortly before.
7. **One concern per role, per component.** Reusing the async role for the DFEG (fault 1) and
   sharing one SG for two roles (fault 3) both made the configuration harder to reason about.

## What worked

- The raw UDP tee (`--raw-tee`) produced the decisive evidence for fault 5.
- The fail-fast logging (`state_reason_code` in the Lambda log) made fault 4 obvious in seconds.
- The live-faithful dry run exercised the real Lambda, KMS, SSM, security group, finish script,
  state machine and Batch job, and every one of those held on contact #9.
- Sync and async share one aggregation chain; none of the failures were in it.

## Follow-ups

| Item | Status |
|------|--------|
| Receiver redesign around the dataflow endpoint application (DDX) | Investigation + AMI inspection done ([`DDX_AMI_INSPECTION.md`](DDX_AMI_INSPECTION.md)); design next |
| Dry run able to exercise the DTLS leg (DTLS-speaking canary or DDX test mode) | Open, depends on investigation |
| Evidence upload on `FAILED` contacts (not only on `COMPLETED`) | Open |
| Pre-flight checklist of AWS customer-side requirements per delivery pattern | Open |
| Split the shared receiver/DFEG security group and the reused DFEG role | Open, low priority |
| Contact #9 billing check | Open (Cost Explorer lag) |
