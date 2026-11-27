# Requirements: Sync Receiver Rebuilt Around the Dataflow Endpoint Application (DDX)

## Introduction

Contacts #8 and #9 (the first synchronous, demod/decode contacts) both failed. #9 proved the
cause: a Ground Station dataflow endpoint stream is a **DTLS 1.2 session**, and our receiver ran a
plain UDP socket that never answered the ClientHello (fault 5 in
[`docs/LESSONS_SYNC_FIRST_CONTACTS.md`](../../../docs/LESSONS_SYNC_FIRST_CONTACTS.md)). AWS expects
the receiver to run its dataflow endpoint application ("DDX", RT Logic DataDefender 2.6.2), shipped
in the shared AMI `groundstation-al2023-ddx2.6.2-ami-*` and verified read-only on 2026-11-26
([`docs/DDX_AMI_INSPECTION.md`](../../../docs/DDX_AMI_INSPECTION.md)).

This spec rebuilds the synchronous-path receiver so that **DDX terminates the DTLS session and
egresses the decrypted stream as plain UDP on the loopback**, where our listener reads it and
feeds the existing S3 -> shared aggregation chain. It also closes the process gaps the first two
contacts exposed: the dry run could not see a missing protocol layer, evidence was lost on a
`FAILED` contact, and the receiver was left running after the pass.

Companion document: [`design.md`](design.md) (architecture, per-file changes, dry run, cutover).
Research it builds on: `.agents/tasks/ddx-dataflow-endpoint-2026-11-26/findings.md` (untracked
desk research) and the AMI inspection above.

### Decisions already taken (not open)

| Id | Decision | Source |
|----|----------|--------|
| D1 | Receiver rebuilt from the shared DDX-only AMI, **pinned by AMI id** in a variable; instance type stays `c6i.xlarge` (DDX needs >= 4 vCPUs) | user, verified on the AMI |
| D2 | Kept as is: DFEG, mission profile and configs, standalone dataflow ENI `172.31.23.74`, EIP on the primary ENI (with `associate_with_private_ip`), buckets, KMS, state machine shape, shared aggregation chain | user |
| D3 | DDX stream: WAN `eth1:55888` -> LAN `udpClient 127.0.0.1:50000`; the listener binds `127.0.0.1:50000`; DDX owns `55888` | user, verified |
| D4 | **Operating model = the AWS template model.** DDX comes up at boot with its saved stream; the listener is a systemd service started at boot that follows the current contact id; at PREPASS `receiver_arm` only starts the instance and verifies readiness; it no longer arms by SSM | user (2026-11-26) |
| D5 | **No shorter contact sub-window.** Book full passes: either the path fails (the cost is the same as a short window would have been to find out) or it works and the full data is wanted | user (2026-11-26) |
| D6 | The network canary is **extended with a DTLS ClientHello replay mode and kept** | user (2026-11-26) |
| D7 | Terraform: subagents plan only; the orchestrator applies after explicit user agreement; one apply at a time; instance replacement is never scheduled inside a booked contact window | repo rule |

### Accepted technical debt (decided 2026-11-26)

| Id | Decision | Source |
|----|----------|--------|
| D8 | The shared receiver/DFEG security group and the reused DFEG role are accepted as **temporary** technical debt and a recorded deviation from the AWS Ground Station pattern. Target state: two security groups per the AWS "VPC configuration with a dataflow endpoint" page plus a dedicated DFEG role. Trigger: a separate change after the first successful DDX contact (it replaces the DFEG and touches the mission profile) | user |

There is no open decision left in this spec.
## Glossary

- **DDX**: the AWS dataflow endpoint application (systemd unit `rtlogic-ddx`), DTLS endpoint.
- **WAN port**: `55888`, the DDX DTLS port on `eth1` (SG, DFEG, canary). **LAN port**: `50000`, the
  loopback port DDX sends decrypted datagrams to (listener only).
- **Dataflow IP**: `172.31.23.74`, the private IP of the standalone dataflow ENI (`eth1`).
- **Segment**: the listener's per-contact spool directory.
- **Evidence**: everything needed to diagnose a contact without the instance (DDX journal, stream
  configuration, listener log and counters, network state, raw tee).

---

## R1 - Receiver image and provisioning

**User story:** As the operator, I want the receiver built from the DDX AMI by Terraform with
deterministic first-boot provisioning, so that replacing the instance reproduces a working
receiver without manual steps or silent image drift.

#### Acceptance Criteria
1. WHEN the receiver instance is planned THEN its AMI SHALL come from the variable
   `receiver_ami_id` (validated as `ami-` + hex) and SHALL NOT be a `most_recent` lookup.
2. WHEN `receiver_ami_id` is planned THEN a plan-time check SHALL fail unless the image is owned by
   `575398917104` and its name starts with `groundstation-al2023-ddx2.6.2-ami-` (rejects the
   qRadio, AL2 and agent variants).
3. WHEN the instance type is planned THEN a validation SHALL reject any type with fewer than 4 vCPUs
   (default `c6i.xlarge`).
4. WHEN the AMI id changes THEN `aws_instance.receiver` SHALL be replaced; `ami` SHALL NOT be in
   `ignore_changes`.
5. WHEN the instance is created THEN `user_data` SHALL provision it on first boot and
   `user_data_replace_on_change` SHALL be true, so Terraform state and the running provisioning
   cannot diverge.
6. WHEN first boot runs THEN provisioning SHALL complete, in this order and before
   `ddx-stream-nic-firstboot.service` runs: wait for `eth1` to carry the dataflow IP; write
   `customer_stream_config.json`; wait for the DDX REST API; run `configure_streams.py` with retry;
   run `save_default_config.py`; install the runtime scripts and systemd units; enable the
   listener service; write a `provisioned` marker with the AMI id and timestamp.
7. IF any provisioning step fails after its retries THEN provisioning SHALL write
   `/var/lib/sync_receiver/provisioning.failed` with the failing step and SHALL exit non-zero; the
   health check (R5) SHALL report it.
8. WHEN provisioning must be repeated on a running instance THEN the same provisioning script SHALL
   be re-runnable by SSM and idempotent.
9. WHEN the receiver is rebuilt THEN no new IAM permission SHALL be required for DDX first boot
   (the AMI inspection found no first-boot download).

## R2 - DDX stream, NIC and routing

**User story:** As the operator, I want DDX bound to the Ground Station ENI with replies leaving
the same way, so that the DTLS handshake completes and survives stop/start cycles.

#### Acceptance Criteria
1. WHEN provisioning writes the stream THEN it SHALL be exactly one downlink stream with
   `wanConfigDevice` = `eth1` (variable `receiver_dataflow_device`), `wanConfigPort` = WAN port,
   `lanConfigDevice` = `lo`, `lanConfigPort` = LAN port, `isUplink` = false.
2. WHEN the instance is stopped and started THEN DDX SHALL come up with that saved stream and
   SHALL bind the dataflow IP on the WAN port without any per-contact configuration.
3. WHEN `ddx-stream-nic-firstboot.service` runs after provisioning THEN `wanConfigDevice` SHALL
   still be `eth1`.
4. WHEN two ENIs are attached THEN the default route SHALL exist on `eth0` only, on every boot
   (first boot and later), so SSM, S3 and CloudWatch use `eth0` + the EIP.
5. WHEN DDX replies to a Ground Station ENI in the subnet THEN the reply, sourced from the dataflow
   IP, SHALL leave via `eth1` (source-based routing), because the VPC drops packets whose source IP
   does not belong to the sending ENI.
6. WHEN the receiver is up THEN the LAN output SHALL be loopback only and no inbound TCP/80 (DDX UI)
   SHALL be reachable from the network.

## R3 - Port split and security group

**User story:** As a maintainer, I want the WAN and LAN ports to be separate settings, so that the
SG/DFEG/canary and the listener cannot be confused again.

#### Acceptance Criteria
1. WHEN the module is configured THEN `receiver_udp_port` SHALL be replaced by `receiver_wan_port`
   (default 55888) and `receiver_lan_port` (default 50000), each validated as 1..65535, and the two
   SHALL differ.
2. WHEN the WAN port is used THEN it SHALL feed the SG ingress/egress rules, the DFEG endpoint
   address port, the canary default port and the DDX stream; the DFEG SHALL show no diff after the
   rename (same value).
3. WHEN the LAN port is used THEN it SHALL feed only the DDX stream and the listener.
4. WHEN the SG is reviewed THEN it SHALL keep UDP `WAN port` ingress and egress from itself and
   HTTPS egress; DTLS replies SHALL be covered by connection tracking (verified by the dry run, R10);
   no inbound rule SHALL be added.

## R4 - Listener service

**User story:** As the operator, I want the listener to run from boot, read DDX's loopback output,
and file each contact's data under its contact id, so that capture does not depend on a pre-AOS
command.

#### Acceptance Criteria
1. WHEN the instance boots THEN `sync-receiver-listener.service` SHALL start, bind
   `127.0.0.1:<LAN port>` (never `0.0.0.0`, never the WAN port), restart on failure within 2 s, and
   publish a status file with a heartbeat.
2. WHEN a datagram arrives THEN the listener SHALL derive the VITA-49 header length from the header
   flags (class id, timestamp integer/fractional, stream id by packet type) and SHALL NOT assume a
   fixed 28 bytes; trailer presence SHALL shorten the payload accordingly.
3. WHEN a packet is a context packet, truncated, malformed or of an unsupported type THEN it SHALL be
   counted per reason and excluded from the frame stream, and every datagram SHALL still be written
   to the raw tee.
4. WHEN a packet is a data packet THEN its payload SHALL be appended to the segment's
   `combined.cadu`; WHEN `--raw-passthrough` is selected THEN raw datagrams SHALL be written instead.
5. WHEN the listener starts or every 5 s THEN it SHALL read the current contact id from the instance
   tag `SyncContactId` through IMDSv2; a valid id SHALL select the segment `spool/<contact id>/`;
   WHEN the tag is absent, invalid or already uploaded THEN datagrams SHALL go to
   `spool/unassigned-<UTC timestamp>/`.
6. WHEN the tag changes THEN the listener SHALL close the current segment and switch without
   dropping datagrams that arrive during the switch.
7. WHEN a segment receives its first datagram THEN its files SHALL be created lazily (no empty
   segment per idle period) and `segment.json` SHALL record first/last packet time and the counters.
8. WHEN SIGUSR1 is received (rotate) THEN the listener SHALL flush and close the current segment,
   mark it `.closed`, record the rotation in `last_rotation.json` and continue with an idle state.
9. WHEN SIGTERM is received THEN the listener SHALL flush and close like a rotation, then exit.
10. WHEN the listener binds THEN it SHALL log the effective `SO_RCVBUF`; provisioning SHALL raise
    `net.core.rmem_max` so the requested 16 MiB is honoured.
11. WHEN the listener runs THEN it SHALL stay Python 3.9 compatible.

## R5 - PREPASS: start and verify (no SSM arming)

**User story:** As the operator, I want PREPASS to start the receiver and tell me, before AOS,
whether DDX and the listener are ready, without any pre-AOS command being needed for capture.

#### Acceptance Criteria
1. WHEN the PREPASS event arrives THEN `receiver_arm` SHALL set the instance tag `SyncContactId` to
   the contact id (validated), THEN start the instance (tolerating an already running instance), THEN
   wait for `running`, keeping the stale-`stopped` tolerance already in place.
2. WHEN the instance is running THEN `receiver_arm` SHALL NOT start the listener or send any arming
   command; the capture path (DDX + listener service + tag) SHALL work even if the Lambda fails
   after the tag and `StartInstances` calls.
3. WHEN the instance is running THEN `receiver_arm` SHALL wait for SSM Online and run the read-only
   health script `sync_receiver_health.sh`, polling until it passes or the verify budget ends.
4. WHEN the health script runs THEN it SHALL check and report as JSON: (C1) `rtlogic-ddx` active;
   (C2) the DDX REST stream list contains the expected stream (eth1, WAN port, lo, LAN port);
   (C3) a socket is bound on `<dataflow IP>:<WAN port>`; (C4) the listener service is active, its
   heartbeat is younger than 15 s and it is bound on `127.0.0.1:<LAN port>`; (C5) the listener's current
   segment matches the expected contact id (tag propagated); (C6) the default route is on `eth0`
   and a route lookup from the dataflow IP leaves via `eth1`; (C7) no `provisioning.failed` marker.
5. WHEN a check fails after the verify budget, or SSM never comes Online THEN `receiver_arm` SHALL
   emit `ReceiverReadyFailure = 1` (and `DdxReadyFailure` / `ListenerReadyFailure` as applicable)
   with dimension `InstanceId`, log the failing checks, and raise, so the existing Lambda-error alarm
   also fires.
6. WHEN all checks pass THEN `receiver_arm` SHALL emit `ReceiverReadyFailure = 0` and log the
   time-to-ready measured from `StartInstances`.
7. WHEN the Lambda timeout is set THEN it SHALL cover tag + running (240 s) + SSM Online (120 s) +
   verify (120 s) and SHALL stay below `contact_pre_pass_duration_seconds`.

## R6 - Finish: rotate, evidence, upload

**User story:** As the operator, I want finish to hand a closed, labelled capture and a full
evidence bundle to S3 whatever the outcome, so that no contact leaves me with only an instance disk.

#### Acceptance Criteria
1. WHEN `sync_receiver_finish.sh <bucket> <contact> <date> <outcome>` runs (`outcome` =
   `completed` | `failed`, default `completed`) THEN it SHALL ask the listener to rotate and SHALL wait
   (<= 15 s) for the rotation record naming the segment.
2. WHEN the segment `spool/<contact id>/` does not exist THEN finish SHALL adopt the newest
   `unassigned-*` segment, log a WARN and record the adoption in the evidence.
3. WHEN finish uploads THEN the order SHALL be: evidence bundle, raw tee, frame capture; evidence and
   raw tee uploads SHALL be best-effort and SHALL NOT block the frame capture upload.
4. WHEN the evidence bundle is built THEN it SHALL contain: provisioning and cloud-init logs, DDX
   journal since boot, the DDX stream list and any statistics the REST API exposes, listener and
   rotation journal, `status.json`, `segment.json`, `ip -d addr/route/rule`, `ss -lunp`, UDP error
   counters, failed units, `/opt/scripts` sha256, AMI id and instance type; it SHALL NOT contain IMDS
   credentials.
5. WHEN `outcome` = `completed` and the frame capture is empty THEN finish SHALL exit 1 (existing
   behaviour); WHEN `outcome` = `failed` THEN an empty capture SHALL NOT make finish fail.
6. WHEN a segment is uploaded THEN finish SHALL write `.uploaded` in it so the listener never reuses
   that contact id; spool segments older than 14 days SHALL be pruned at boot.
7. WHEN files are uploaded THEN they SHALL use SSE-KMS with the pipeline key, under
   `contacts/<date>/<contact>/{evidence,raw,cadu,logs}/`.

## R7 - FAILED contacts

**User story:** As the operator, I want a FAILED contact to collect evidence and stop the receiver,
so that a failed pass costs no evidence and no idle instance.

#### Acceptance Criteria
1. WHEN a Ground Station contact for the sync mission profile changes to `FAILED` or `AWS_FAILED`
   THEN a new EventBridge rule SHALL start the existing state machine with `outcome = failed`; the
   COMPLETED rule SHALL pass `outcome = completed`.
2. WHEN `outcome = failed` THEN the state machine SHALL run finish with that outcome, stop the
   receiver, publish an SNS notification naming the evidence prefix, and end without starting the
   aggregation job.
3. WHEN finish or the stop step fails on either path THEN the failure path SHALL still attempt to
   stop the instance (best effort) before notifying.
4. IF no FAILED event shape is available THEN the rule SHALL be validated with
   `aws events test-event-pattern` against the event recorded for contact #9 (see R10 deviations).

## R8 - Observability and alarms

#### Acceptance Criteria
1. WHEN the verification fails THEN `ReceiverReadyFailure` (existing alarm) SHALL alarm to the shared
   SNS topic; new metrics `DdxReadyFailure` and `ListenerReadyFailure` SHALL each have an alarm.
2. WHEN the drift check runs THEN it SHALL cover the scripts, the units, the enabled state of
   `rtlogic-ddx`, and the stream (device and ports) in the saved configuration, and SHALL emit
   `OptDriftDetected`.
3. WHEN time-to-ready is measured THEN it SHALL be in the Lambda log as a structured field.

## R9 - Network canary (extended, kept)

#### Acceptance Criteria
1. WHEN the canary is invoked with `mode = dtls_replay` THEN it SHALL send the recorded contact #9
   ClientHello (143 bytes) to `<dataflow IP>:<WAN port>` from the receiver subnet and SG, wait for a
   reply (up to 3 attempts, 1 s apart, matching DTLS retransmission), and return the reply size,
   DTLS content/handshake types, source address and latency.
2. WHEN the reply is a HelloVerifyRequest (handshake type 3) or a ServerHello (type 2) THEN the
   result SHALL be `pass`; otherwise `fail` with the reason (no reply, wrong source, other type).
3. WHEN the ClientHello is packaged THEN it SHALL be a file in the Lambda package (the VPC Lambda has
   no S3 or internet path) with its SHA-256 documented.
4. WHEN the existing plain-UDP modes are used THEN they SHALL keep working but their docstring SHALL
   state that against a DDX receiver they validate only the SG path, not delivery.

## R10 - Live-faithful dry run

**User story:** As the operator, I want the dry run to exercise the real live path and list every
deviation beforehand, so that nothing is discovered at AOS.

#### Acceptance Criteria
1. WHEN the dry run is prepared THEN the deviation table in `design.md` SHALL be copied to the
   contact's entry in `docs/CONTACTS.md` BEFORE the run, with the validation of each deviation.
2. WHEN the dry run starts THEN the instance SHALL be `stopped` and the real `receiver_arm` Lambda
   SHALL be invoked with a test event shaped like the PREPASS target input; the real roles SHALL be
   used (no operator credentials substituted).
3. WHEN the receiver starts THEN the time from `StartInstances` to a passing health check SHALL be
   measured on at least 3 consecutive stop/start cycles and compared with the pre-pass budget.
4. WHEN the receiver is ready THEN the canary in `dtls_replay` mode SHALL get a HelloVerifyRequest or
   ServerHello (proves DDX bound on `eth1`, the SG both ways and reply routing).
5. WHEN synthetic VITA-49 datagrams (several header-flag variants) are injected into
   `127.0.0.1:<LAN port>` by SSM THEN the real state machine, started with the COMPLETED input shape,
   SHALL run finish, S3 upload and the Batch aggregation job, and SHALL end at the expected
   `AggregationFailure` (strict zero-RDR check on synthetic data).
6. WHEN the state machine is started with `outcome = failed` THEN evidence SHALL reach S3, the
   instance SHALL be stopped and no aggregation job SHALL start.
7. WHEN the contact tag is changed mid-run and when datagrams arrive before the tag exists THEN the
   segment switch and the `unassigned` adoption SHALL behave as specified in R4/R6.
8. WHEN the dry run ends THEN `design.md`'s residual list (DDX framing inside DTLS, real payload
   layout, Ground Station client behaviour) SHALL be stated to the user as unvalidated BEFORE a
   contact is booked.

## R11 - Cutover and operating safety

#### Acceptance Criteria
1. WHEN the instance replacement is scheduled THEN no sync contact SHALL be in `SCHEDULED`,
   `SCHEDULING` or within its pre-pass window (checked with `list-contacts`), and the contact #9 raw
   capture SHALL be confirmed present in S3.
2. WHEN `terraform plan` is reviewed THEN it SHALL show only: instance replace, ENI attachment
   replace, EIP update/replace, Lambda env/IAM updates, new rule/alarms/S3 objects; it SHALL show NO
   change to the DFEG, mission profile, dataflow ENI, SG, KMS key or buckets (other than new objects).
3. WHEN the plan is agreed THEN the orchestrator SHALL apply exactly the saved
   `*.approved.tfplan` as one command and delete the plan file afterwards.
4. WHEN the new instance has first-booted THEN provisioning, health and one stop/start cycle SHALL
   be verified by evidence before the dry run; the instance SHALL be left `stopped`.
5. WHEN a rollback is needed THEN the documented path SHALL be: revert the change set and re-plan;
   booking contacts SHALL be suspended until the receiver passes the dry run (the old receiver
   cannot succeed against DTLS, so no capability is lost).
6. WHEN a contact is booked THEN it SHALL be a full pass (D5), the operator SHALL be present for
   PREPASS and POSTPASS, and the SSO session SHALL be refreshed shortly before.

## R12 - Security and IAM

#### Acceptance Criteria
1. WHEN IAM changes THEN they SHALL be limited to: `receiver_arm` role gains `ec2:CreateTags` on the
   receiver instance only with `aws:TagKeys` = `SyncContactId`, `ssm:GetCommandInvocation`, and
   `cloudwatch:PutMetricData` conditioned to namespace `SyncPipeline`; the instance role, state
   machine role, canary role and DFEG role are unchanged (D8: the shared SG and DFEG role stay until the follow-up change).
2. WHEN the instance metadata options are set THEN IMDSv2 SHALL remain required and
   `instance_metadata_tags` SHALL be enabled.
3. WHEN Terraform argument values are written THEN `description`, `name` and tag values SHALL be
   ASCII only; `terraform validate` and Checkov SHALL be clean for the changed files with any skip
   justified in a comment.
4. WHEN user data and scripts are reviewed THEN they SHALL contain no secrets, and evidence SHALL
   exclude IMDS credentials.
5. WHEN `aws_instance.receiver` ignores out-of-band tags THEN only `tags["SyncContactId"]` SHALL be
   ignored.

## R13 - Tests

#### Acceptance Criteria
1. WHEN the listener parser changes THEN unit tests SHALL cover each header-flag combination, the
   trailer, context packets, truncated packets and type filtering, and SHALL fail if the parser is
   reverted to a fixed 28-byte header.
2. WHEN the segment logic changes THEN tests SHALL cover tag-driven switch, `unassigned`, rotation by
   signal, lazy creation and the `.uploaded` rule.
3. WHEN `receiver_arm` changes THEN tests SHALL cover tagging order (tag before start), no arming
   command, health polling, verify failure -> metric + raise, time-to-ready log, and keep the stale
   `stopped` tests.
4. WHEN finish changes THEN tests SHALL cover outcome handling, evidence-first upload order,
   unassigned adoption and the empty-capture rule per outcome.
5. WHEN the canary changes THEN tests SHALL cover reply classification and the 3-attempt behaviour
   against a local fake UDP server.
6. WHEN user data is rendered THEN a rendering test SHALL assert the stream JSON, the eth1 wait, the
   ordering before the NIC re-pin unit, and the route pin; WHEN the Terraform changes THEN
   `terraform validate` and `terraform plan` SHALL be clean before any apply.
7. WHEN tests are reviewed THEN each SHALL fail if the code it covers is broken; no mocking away the
   function under test.

## R14 - Documentation coherence

#### Acceptance Criteria
1. WHEN this spec is committed THEN the global docs SHALL reference it: `docs/README.md` index,
   `docs/SYNC_PIPELINE.md`, `docs/ARCHITECTURE.md`, `docs/LESSONS_SYNC_FIRST_CONTACTS.md` follow-ups,
   `docs/SYNC_DRY_RUN.md`.
2. WHEN a statement in those docs contradicts the new design (listener on 55888, arming by SSM, the
   canary as plain UDP only, manual script installation) THEN it SHALL be marked superseded with a
   pointer to this spec, not deleted.
3. WHEN the implementation lands THEN the doc edits for the new behaviour SHALL be made in the same
   change, plus `docs/CONTACTS.md`, the `aws-ground-station` skill and `aws-knowledge.md`
   (continuous-learning rule).

## R15 - Booking policy

#### Acceptance Criteria
1. WHEN the first DDX contact is booked THEN it SHALL use a full pass reserved with
   `reserve-contact` on the sync profile; no sub-window reservation SHALL be attempted (D5).

## R16 - Well-Architected alignment and deviation register

**User story:** As the project owner, I want the receiver to follow AWS Ground Station guidance and
the Well-Architected pillars, and every departure written down with a reason and a plan, so that
technical debt is visible and not accidental.

#### Acceptance Criteria
1. WHEN the design is reviewed THEN it SHALL map to the six Well-Architected pillars and SHALL list
   every deviation from AWS Ground Station guidance with its reason and planned resolution.
2. WHEN a deviation is accepted as temporary THEN it SHALL have a named target state and a trigger,
   and SHALL appear in the follow-up list of `docs/LESSONS_SYNC_FIRST_CONTACTS.md` (D8 included).
3. WHEN the first DDX contact has succeeded THEN the follow-up change for D8 SHALL be opened as its
   own spec (new DFEG, two security groups, dedicated DFEG role, its own dry run).

---

## Assumptions

- The ClientHello of contact #9 is 143 bytes (DTLS record header 13 + record length 0x0082 = 130).
  Older notes say "~127 B"; they quote the handshake length, not the datagram.
- A `FAILED` Ground Station contact emits an EventBridge `Ground Station Contact State Change` event
  with `contactStatus = FAILED`. Not yet verified for #9; R7.4 and the first cutover task verify it.
- DDX output on `127.0.0.1:50000` is VITA-49.2 "AWS Uncoded Frame Data" datagrams. Not verified; the
  raw tee and the flag-derived header parsing exist because of this uncertainty.
- Per-contact tag propagation to IMDS completes within the 5 s listener poll plus the tag-before-start
  ordering. Verified in the dry run (R10.7), with the `unassigned` fallback if not.

## Out of scope

- Any change to the shared aggregation chain, CSPP, RT-STPS, visualization.
- Sub-window (shorter) contact reservation (D5).
- Uplink streams, qRadio, digital twin.
- A watchdog that stops a receiver left running when no terminal contact event arrives (listed as a
  risk in the design).
- Splitting the SG / DFEG role (D8: separate change after the first successful DDX contact).
- tasks.md (design review first).
