# Implementation Plan: Sync Receiver Rebuilt Around the Dataflow Endpoint Application (DDX)

## Overview

Implements [`design.md`](design.md) for [`requirements.md`](requirements.md). Order is chosen for
safety: pure Python/bash and their tests first (nothing deployed), then Terraform (plan only),
then docs, then the cutover (one gated apply, outside any booked contact), then the live-faithful
dry run, then a gated "book a sync contact" task that books nothing without the user.

**Delegation (non-negotiable, `.kiro/steering/delegation-and-safety.md`):**
`*.tf`/`*.tfvars`/`*.tftest.hcl` -> `terraform` subagent (PLAN-ONLY). `*.py` -> `python` (new or
multi-file), `python-haiku` (small mechanical), `python-debugger` (cross-file bug or after `python`
failed). Bash scripts (`scripts/*.sh`, `*.tftpl`) -> `python` agent as the nearest owner (no
dedicated shell agent exists). Docs/diagram YAML -> `docs` (docs-writer) or the orchestrator.
`orchestrator` = the main session. **Only the orchestrator applies, only after explicit user
agreement on a plan summary shown in the conversation**, from a saved `$env:TEMP\sync-receiver-ddx.approved.tfplan`
applied as one unchained command, plan file deleted right after, one apply at a time.

**Git hygiene:** work on branch `design/sync-receiver-ddx` (or a child branch of it); one concern per
commit; stage files by name; never commit to `main`; changes land via PR. Every sub-task = one commit.

**Test rule:** every test must fail if the code it covers is reverted (R13.7). Where a sub-task says
"revert check", run the new test against the old code (or with the fix removed) and record that it
fails.

**Status of the contact book:** no sync contact is booked now. Task 12.1 re-checks this before the apply.

---

## Stage A - Pure Python / bash and tests (nothing deployed)

- [ ] 1. **Listener service mode (`scripts/sync_receiver_listen.py`)**
  - [ ] 1.1 VITA-49 header parser derived from flags (replaces the fixed 28-byte header): formula of
    design 5.4, drop counters per reason (`unsupported`, `malformed`, `empty`, `truncated`, `padded`),
    context packets (types 4/5) counted not written, `header_stats`, `--data-types` (default 0-3),
    raw tee receives every datagram (P2). Tests in `tests/test_sync_receiver_listen.py`: Hypothesis
    property over (type 0..3, C, T, TSI, TSF, random payload); pinned 28-byte case; a no-class-id
    variant (offset 20) that fails against the old fixed parser; truncated, padded, context, empty.
    Delegate: `python`. Verify: `pytest tests/test_sync_receiver_listen.py` green; revert check on
    the parser fails the tests.
    - _Requirements: R4.2, R4.3, R4.4, R4.11, R13.1, R13.7_
  - [ ] 1.2 Service mode: `--service --bind 127.0.0.1 --port --spool`; refuse `0.0.0.0` and the WAN
    port (P3); bind failure -> not ready; `SO_RCVBUF` 16 MiB requested and granted value logged;
    single-thread recv loop with 1 s timeout; `/run/sync_receiver/status.json` every 5 s
    (`bound`, `bind`, `rcvbuf`, `started_at`, `heartbeat`, `contact_id`, `contact_source`, `segment`,
    counters, `header_stats`). Delegate: `python`. Verify: new tests for bind refusal, status fields,
    heartbeat advance; Python 3.9 compatibility (`python3.9 -m py_compile` or the repo's linter
    target).
    - _Requirements: R4.1, R4.10, R4.11, R13.2_
  - [ ] 1.3 Contact following and segments: IMDSv2 token + `tags/instance/SyncContactId` every 5 s
    and at start (base URL injectable for tests); id regex `^[A-Za-z0-9-]{1,64}$`; segment
    `spool/<id>/` or `spool/unassigned-<UTC compact>/`; lazy file creation (`combined.cadu`,
    `raw_capture.bin`); `segment.json` rewritten every 30 s and at close; `.closed` / `.uploaded`
    markers, refuse to reopen an uploaded id; tag switch in the same receive loop without losing a
    datagram; startup prune of segments older than 14 days (see Spec gaps G1). Tests with a fake IMDS
    HTTP server: lazy creation, switch without loss, unassigned on absent/invalid tag, `.uploaded`
    refusal, prune. Delegate: `python`. Verify: `pytest tests/test_sync_receiver_listen.py`; revert
    check on the switch logic.
    - _Requirements: R4.5, R4.6, R4.7, R6.6, R13.2_
  - [ ] 1.4 Signals: SIGUSR1 rotate (flush, close, `.closed`, `last_rotation.json`
    `{segment, closed_at, packets, bytes}`, go idle); SIGTERM flush+close then exit. Tests send real
    signals to a subprocess listener and assert files. Delegate: `python`. Verify: pytest green;
    SIGTERM test fails if the flush is removed.
    - _Requirements: R4.8, R4.9, R13.2_

- [ ] 2. **Health script `scripts/sync_receiver_health.sh` (new)**
  - [ ] 2.1 Read-only checks C1..C7, one JSON object on stdout, exit 0 only if all pass; output also
    carries script sha256 set, AMI id, uptime. Test `tests/test_sync_receiver_health.py` runs the script
    with stubbed `systemctl`, `ss`, `ip`, `curl` on `PATH`: each check fails independently and the JSON
    names it; C6 distinguishes default route on eth1 from the source-route lookup. Delegate: `python`.
    Verify: `pytest tests/test_sync_receiver_health.py`; revert check: break one check, its test fails.
    - _Requirements: R5.4, R2.4, R2.5, R13.6_

- [ ] 3. **Finish script `scripts/sync_receiver_finish.sh`**
  - [ ] 3.1 Add 4th argument `outcome` (default `completed`), rotation via
    `systemctl kill -s USR1` with 15 s wait on `last_rotation.json`, segment selection with
    `unassigned-*` adoption (WARN, `adopted_from` recorded), evidence bundle (cap 50 MB, content list
    per R6.4, no IMDS credential paths), upload order evidence -> raw -> cadu (evidence/raw
    best-effort), `.uploaded` written only after a successful frame upload, exit rules per outcome,
    log-upload trap also on failure exits. Extend `tests/test_sync_receiver_finish.py` (PATH stubs):
    upload order, adoption, empty-capture exit code by outcome, `.uploaded` timing, evidence list has
    no credential path. Delegate: `python`. Verify: `pytest tests/test_sync_receiver_finish.py`;
    revert check on the order and on the outcome rule.
    - _Requirements: R6.1, R6.2, R6.3, R6.4, R6.5, R6.6, R6.7, R13.4_
  - [ ] 3.2 **Evidence-upload-on-FAILED behaviour test:** a dedicated test running finish with
    `outcome=failed`, empty capture and a missing frame file: evidence and raw are uploaded, exit
    code is 0, no cadu upload attempted; and the same inputs with `completed` exit 1 after evidence
    was uploaded (P1: evidence before any failure exit). Delegate: `python-haiku` (test only, explicit
    inputs). Verify: new test green; fails if the failed-outcome branch is reverted to exit 1.
    - _Requirements: R6.3, R6.5, R7.2, R13.4, R13.7_

- [ ] 4. **Inject script `scripts/sync_receiver_inject.py` (new)**
  - [ ] 4.1 Sender of framed VITA-49 datagrams to `127.0.0.1:<port>`: `--variants all` (7-word, no
    class id, trailer, context packet, truncated), `--count`, optional `--rate-bps` for the rate test
    (design 9.2 step 3/5b); Python 3.9 compatible. Test in `tests/test_sync_receiver_inject.py`:
    feed the injector into the real listener parser (task 1.1) and assert counters equal the injected
    counts per variant. Delegate: `python`. Verify: pytest green.
    - _Requirements: R10.5, R13.1_

- [ ] 5. **Provisioning script `scripts/sync_receiver_provision.sh` (new)**
  - [ ] 5.1 Idempotent, in order (design 5.1): wait for `eth1` (<=180 s) -> network pin
    (networkd drop-in; default route only on eth0, source rule kept) -> sysctl `rmem_max=33554432` ->
    stream JSON (values from arguments/variables) -> wait DDX REST :80 (<=180 s) ->
    `configure_streams.py` with 6 attempts 10 s apart -> `sleep 2` -> `save_default_config.py` ->
    REST read-back compare -> install-scripts unit, three systemd units
    (`sync-receiver-install-scripts`, `sync-receiver-listener`, and the manifest verify), enable the
    listener -> `provisioned` marker (AMI id, instance type, UTC time, script sha256s);
    `provisioning.failed` with the failing step on error. Install-scripts unit: bounded retries 60 s,
    sha256 check against `MANIFEST.sha256`, keep last good copy if S3 unreachable (exit 0).
    Test `tests/test_sync_receiver_provision.py`: temp root, stubbed commands; assert step order,
    stream JSON content, `provisioning.failed` on a failing step, idempotent second run. Mutation
    check: remove the retry loop and the test must fail. Delegate: `python`. Verify: pytest green.
    - _Requirements: R1.6, R1.7, R1.8, R2.1, R2.3, R2.4, R2.5, R2.6, R4.1, R4.10, R13.6_

- [ ] 6. **`receiver_arm` Lambda (`lambdas/receiver_arm/handler.py`)**
  - [ ] 6.1 Rewrite per design 5.5: validate contact id (reject = no tag, no start); `CreateTags`
    `SyncContactId` BEFORE `StartInstances`; tag failure does not stop the start but raises at the end;
    keep `_wait_for_running` and stale-`stopped` grace; `_wait_for_ssm_online` <=120 s; verify loop
    (`AWS-RunShellScript` health script, `GetCommandInvocation` poll 3 s, retry every 10 s until pass
    or `VERIFY_TIMEOUT_SECONDS`); metrics `ReceiverReadyFailure`, `DdxReadyFailure`,
    `ListenerReadyFailure` (dimension `InstanceId`, namespace `SyncPipeline`); structured time-to-ready
    log field; no arming command. Env: `RECEIVER_INSTANCE_ID`, `RECEIVER_WAN_PORT`,
    `RECEIVER_LAN_PORT`, `RECEIVER_DATAFLOW_IP`, `VERIFY_TIMEOUT_SECONDS`. Update
    `tests/test_receiver_arm.py` (boto3 stubs): tag before start, invalid id rejected, no arm
    `send_command`, health success on 3rd attempt, failure -> three metrics + raise, SSM never Online
    -> metric + raise, stale-`stopped` tests kept, time-to-ready present. Delegate: `python`. Verify:
    `pytest tests/test_receiver_arm.py`; revert check on tag-before-start ordering.
    - _Requirements: R5.1, R5.2, R5.3, R5.5, R5.6, R8.3, R13.3_

- [ ] 7. **Canary `dtls_replay` mode**
  - [ ] 7.1 Fixture `lambdas/network_canary/clienthello_contact9.bin` = first 143 bytes of the first
    datagram of
    `s3://groundstation-noaa20-sync-output-471112743408/contacts/2026/11/26/ccc4fe43-eb79-4ac3-9ebe-80d44330908a/raw/raw_capture.bin`.
    Extraction is a one-off READ of that object (user's S3 read allowed for this task only; no write,
    no Terraform): record the SHA-256 in the handler docstring, in this tasks.md (below) and in the
    design file comment. Check byte 0 = 0x16, bytes 1-2 = `fe ff`/`fe fd`, record length 0x0082. A
    throwaway extraction script is deleted after use (workflow rule).
    Delegate: orchestrator runs the read, `python-haiku` writes the fixture-hash test. Verify: file is
    143 bytes; `tests/test_network_canary.py` asserts size and SHA-256.
    - Recorded SHA-256: _(fill in at execution)_
    - _Requirements: R9.3_
  - [ ] 7.2 `dtls_replay` mode in `lambdas/network_canary/handler.py` (design 5.8): event
    `{mode, target_ip, port, attempts=3, timeout_s=1.0}`, defaults from env; ephemeral socket,
    retransmit up to `attempts`; classify first reply (content type 22, version `fe ff`/`fe fd`,
    handshake 3 or 2 -> `pass`; other -> `fail` with types; none -> `no_reply`; other source ->
    `wrong_source`); return `result, reply_bytes, content_type, handshake_type, source, latency_ms,
    attempt`. Plain-UDP docstring states they validate only the SG path against a DDX receiver
    (R9.4). Tests with a local fake UDP server: HelloVerifyRequest, ServerHello, garbage, nothing
    (3 attempts with timing), wrong source. Delegate: `python`. Verify:
    `pytest tests/test_network_canary.py`; revert check on the classification.
    - _Requirements: R9.1, R9.2, R9.4, R13.5_

- [ ] 8. **Checkpoint A - Python/bash complete**
  - Full `pytest` green (whole repo suite, not only the changed files); ruff/flake8 per repo
    config if present; no stale test left from the behaviour change (workflow "Test Consistency").
    Delegate: `python-haiku` runs the suite. Verify: 100% green, result quoted.
    - _Requirements: R13.1-R13.7_

## Stage B - Terraform (plan-only; nothing applied)

- [ ] 9. **Terraform changes in `infra/modules/sync_pipeline/` (delegate: `terraform`, all PLAN-ONLY)**
  - [ ] 9.1 `variables.tf`: remove `receiver_udp_port`; add `receiver_ami_id` (validated `ami-` +
    hex), `receiver_wan_port` (55888), `receiver_lan_port` (50000) with 1..65535 and not-equal
    validation, `receiver_dataflow_device` (`eth1`), `ddx_max_wan_rate_bps` (4000000000),
    `receiver_verify_timeout_seconds` (120); instance-type allow-list validation (>= 4 vCPUs; list,
    not regex). Pin the AMI id value in the root `main.tf`/tfvars: the id comes from the user/AMI
    inspection doc (do NOT look it up with a `most_recent` data source). Verify: `terraform validate`,
    `terraform fmt -check`.
    - _Requirements: R1.1, R1.3, R3.1_
  - [ ] 9.2 `ec2.tf` + `files/receiver_user_data.sh.tftpl` (new): `ami = var.receiver_ami_id`, plan-time
    `precondition`/`check` (owner `575398917104`, name prefix `groundstation-al2023-ddx2.6.2-ami-`);
    `user_data` with the provisioning script sha256 embedded, `user_data_replace_on_change = true`;
    `metadata_options.instance_metadata_tags = "enabled"` (IMDSv2 still required, hop limit 1);
    `ignore_changes = [root_block_device, tags["SyncContactId"]]` (no `ami`); SG rules use
    `receiver_wan_port`; remove the stale `checkov:skip CKV_AWS_8` and "installed manually" comments;
    keep ENI, attachment, EIP (`associate_with_private_ip`), DFEG (port = WAN var). ASCII only.
    Test `tests/test_user_data_render.py` (rendered template: stream JSON, eth1 wait, S3 wait before
    fetch, ordering before the NIC re-pin unit, route pin, hash embedded) is written by `python`.
    Verify: validate, fmt, pytest render test (fails if the template drops the eth1 wait).
    - _Requirements: R1.2, R1.4, R1.5, R3.2, R12.2, R12.5, R13.6_
  - [ ] 9.3 `s3.tf`: `aws_s3_object` for `software/sync_receiver/{sync_receiver_provision.sh,
    sync_receiver_listen.py, sync_receiver_finish.sh, sync_receiver_health.sh, sync_receiver_inject.py,
    MANIFEST.sha256}` (SSE-KMS pipeline key, `etag` = file hash, `depends_on` of the instance).
    `MANIFEST.sha256` generated from the files (Terraform `filesha256` rendered, not a committed
    hand-edited file). Verify: validate; plan shows only new objects.
    - _Requirements: R1.5, R12.4_
  - [ ] 9.4 `lambda.tf`: `receiver_arm` env vars, timeout 420 -> 540 (stays below
    `contact_pre_pass_duration_seconds`), role gains `ec2:CreateTags` (instance ARN,
    `ForAllValues:StringEquals aws:TagKeys = SyncContactId`), `ssm:GetCommandInvocation`
    (Checkov skip with justification, like `DescribeInstanceInformation`),
    `cloudwatch:PutMetricData` (`cloudwatch:namespace = SyncPipeline`). Package for canary includes
    `clienthello_contact9.bin` (check the zip `source_dir` picks it up). Verify: validate; plan.
    - _Requirements: R5.7, R9.3, R12.1_
  - [ ] 9.5 `eventbridge.tf` + `step_functions.tf`: new rule `contact_failed` (`FAILED`,
    `AWS_FAILED`, sync profile) -> state machine with `outcome = failed`; COMPLETED target input gains
    `outcome = completed`; PREPASS untouched. State machine passes `outcome` as 4th finish argument;
    after `StopReceiver` a `Choice` (`failed` -> SNS publish with evidence prefix -> `Succeed`; else
    `SubmitBatchAggregation`); `ReceiverFailure` best-effort `stopInstances` with `Catch` (verify
    what it does today first). Confirm SNS publish permission in the plan. Verify: validate; plan;
    `aws events test-event-pattern` with the recorded #9 event is run in task 12.2, not here.
    - _Requirements: R7.1, R7.2, R7.3, R12.1_
  - [ ] 9.6 `monitoring.tf` + `ssm_drift_check.tf`: alarms `DdxReadyFailure`, `ListenerReadyFailure`
    (shape of `receiver_ready_failure`, shared SNS topic, `notBreaching`), `ReceiverReadyFailure`
    text updated; drift document also checks the `/opt/scripts` set (arm removed; health, inject,
    provision added), the three units, `systemctl is-enabled rtlogic-ddx`, saved stream device and
    ports, emitting `OptDriftDetected`. Verify: validate; plan.
    - _Requirements: R8.1, R8.2_
  - [ ] 9.7 `network_canary.tf` + `outputs.tf`: `CANARY_PORT` = WAN port; outputs
    `receiver_dataflow_ip`, `receiver_ami_id`. ASCII-only test over every `description`/`name`/tag
    value in the module (`tests/test_terraform_ascii.py`, by `python-haiku` if absent). Verify:
    validate; pytest ASCII test.
    - _Requirements: R3.2, R10 (outputs for scripts), R12.3, R13.6_

- [ ] 10. **Checkpoint B - Terraform validated (no apply)**
  - [ ] 10.1 `terraform validate` + `terraform fmt -check` + `terraform plan` (plan-only, subagent)
    on the root; the plan must match R11.2: instance replace, ENI attachment replace, EIP update,
    Lambda env/IAM, new rule/alarms/S3 objects, canary env; NO change to DFEG, mission profile,
    dataflow ENI, SG, KMS key or buckets. Anything else is a finding and blocks the task. Delegate:
    `terraform`. Verify: plan summary in the conversation.
    - _Requirements: R11.2, R13.6_
  - [ ] 10.2 Checkov via `security-scanner`: `checkov -d . --quiet --download-external-modules false`
    on the changed files; HIGH/CRITICAL block; skips justified in comments (ASCII). Delegate:
    `security-scanner`. Verify: clean report quoted.
    - _Requirements: R12.1, R12.3_

## Stage C - Documentation and learning (same change set)

- [ ] 11. **Docs, steering and skill updates (per design section 13 and R14.3)**
  - [ ] 11.1 Docs that follow the implementation: `docs/SYNC_PIPELINE.md` (new sequence, manual
    prerequisites removed, superseded statements marked not deleted), `docs/ARCHITECTURE.md`,
    `docs/README.md` index, `docs/LESSONS_SYNC_FIRST_CONTACTS.md` follow-ups status,
    `docs/RUNBOOK.md` if it names the arm step (grep first), `docs/diagrams` regenerated only if the
    architecture changed from what is drawn (use the `render-awsdac-diagram` skill). Delegate: `docs`.
    Verify: links resolve (repo link check if present, else manual); no contradictions (see 16.1).
    - _Requirements: R14.1, R14.2, R14.3_
  - [ ] 11.2 Learning backport (workflow "Continuous Learning Loop"): `.kiro/steering/aws-knowledge.md`
    and `.kiro/skills/aws-ground-station/` get the DDX/DTLS facts, instance-tag contact id via IMDS,
    DDX stream and NIC pin gotchas, in the symptom / incorrect / correct format. Placeholders for
    anything only learned at the dry run are filled in task 15.4. Delegate: `docs`. Verify: entries
    follow the format.
    - _Requirements: R14.3_
  - [ ] 11.3 PR for Stage A-C: push the child branch, open the PR into the design branch/`main` as the
    repo rule requires; PR description lists tests, plan summary, checkov result and what is NOT
    validated (design 9.1 rows 1-3). Delegate: orchestrator. Verify: PR exists, CI (if any) green.
    - _Requirements: R11.3 (git hygiene), R14.3_

## Stage D - Cutover (one apply, outside any booked contact)

- [ ] 12. **Cutover**
  - [ ] 12.1 **Preconditions check (orchestrator, read-only):** `list-contacts` shows no sync contact
    `SCHEDULED`/`SCHEDULING` or inside a pre-pass window; contact #9 `raw_capture.bin` still exists in
    S3 (only copy of the ClientHello; the fixture of 7.1 is already committed); PR merged and Checkpoint
    A green. Use `scripts/gs_list_sync_contacts.ps1`. Verify: output quoted. If any sync contact is
    booked, STOP and propose a different window.
    - _Requirements: R11.1_
  - [ ] 12.2 **FAILED event shape (U9, R7.4):** inspect the `contact_prepass`/completed rule
    invocation history around 2026-11-26 11:42 UTC (read-only), save the recorded event as a test
    fixture (`tests/fixtures/contact9_failed_event.json`) and run
    `aws events test-event-pattern` against the new `contact_failed` pattern. If no event exists,
    record that in `docs/CONTACTS.md` and in Spec gaps (G4) and keep deviation row 5 as residual.
    Delegate: orchestrator (read) + `python-haiku` (fixture test). Verify: pattern matches the fixture
    or the absence is documented.
    - _Requirements: R7.4_
  - [ ] 12.3 **CHECKPOINT - plan summary and USER AGREEMENT (blocking).** `terraform` subagent
    produces `plan` and the orchestrator shows the user the summary (resources, change type, every
    destroy/replace, the instance replacement and its consequences: new instance id changes Lambda env,
    SSM/state machine targets, alarm dimensions). No agreement, no apply; "go ahead with the work" is
    NOT agreement. If the plan contains unrelated changes, use `-target`. Record the agreement
    (user's words) in the PR or CONTACTS entry.
    - _Requirements: R11.2, R11.3, D7_
  - [ ] 12.4 **APPLY (orchestrator only, one command).** Save `$env:TEMP\sync-receiver-ddx.approved.tfplan`
    and run `terraform apply` on exactly that file as a single unchained command; delete the plan
    file right after (success or failure). Never reuse the file. If the plan file throws "Provider
    produced inconsistent final plan" (latest_version-pinned launch template), ask the user to run
    a fresh apply themselves (the hook blocks it). Verify: command output quoted.
    - _Requirements: R11.3_
  - [ ] 12.5 **Post-apply `terraform plan` = No changes (checkpoint).** Re-plan (subagent, plan-only);
    expect "No changes". Anything else is a finding. Verify: plan output quoted.
    - _Requirements: R11.3_
  - [ ] 12.6 **Verify the new instance by evidence (read-only SSM, no contact):** `provisioned` marker
    with the pinned AMI id, `/var/log/user-data.log`, no `provisioning.failed`, health JSON all
    C1..C7 over SSM, `ip route show default` lists only eth0 and the source-route lookup leaves via
    `eth1`, `ddx-stream-nic-firstboot` left `wanConfigDevice = eth1`, one stop/start cycle with the
    same result (route order can flip, so repeat), instance left `stopped`. If only provisioning
    failed, re-run `sync_receiver_provision.sh` by SSM rather than replacing again. Delegate:
    orchestrator. Verify: outputs quoted, instance state `stopped`.
    - _Requirements: R11.4, R2.2, R2.3, R2.4, R2.5_
  - [ ] 12.7 **Cleanup of superseded artefacts:** delete `scripts/sync_receiver_arm.sh` (design 4:
    superseded, delete at cutover), its references (grep `sync_receiver_arm` in `scripts/`, `tests/`,
    `docs/`, `infra/`, `.kiro/`), and any `/opt/scripts/sync_receiver_arm.sh` expectation left in the
    drift check. One commit, in the same branch, after 12.6 proved the new path. Delegate:
    `python-haiku` (delete + reference fix), docs part by `docs`. Verify: full `pytest` green;
    `terraform validate`; grep returns no live reference (only "superseded" notes).
    - _Requirements: R5.2, R14.2_
  - [ ] 12.8 **Rollback readiness note:** confirm the documented rollback (design 10.6) is in
    `docs/SYNC_PIPELINE.md`/runbook: revert the change set, re-plan, suspend bookings until the dry
    run passes; AMI-refresh procedure (design 10.7). Delegate: `docs`. Verify: text present.
    - _Requirements: R11.5_

## Stage E - Dry run (live-faithful, design section 9)

- [ ] 13. **Prepare the dry run**
  - [ ] 13.1 **Deviation table BEFORE the run:** copy design 9.1 into the contact's entry in
    `docs/CONTACTS.md` with the validation and residual of each row, plus any deviation found at
    cutover (e.g. a missing FAILED event). Commit before the run. Delegate: `docs`. Verify: entry
    exists in git history before the run's first log line.
    - _Requirements: R10.1_
  - [ ] 13.2 **Preflight (steering "dry runs"):** deviations listed with their validation; instance
    `stopped`; SSO session fresh; real `receiver_arm` Lambda and real roles will be used (no operator
    credentials substituted). Delegate: orchestrator. Verify: checklist in the CONTACTS entry.
    - _Requirements: R10.2_

- [ ] 14. **Run the dry run (orchestrator drives, real principals)**
  - [ ] 14.1 **Cold starts x3 (design 9.2 step 1):** invoke the real `receiver_arm` with a
    PREPASS-shaped event (`dryrun-0001`; one run UUID-shaped, row 6) from `stopped`, three consecutive
    stop/start cycles; record time-to-ready each time and compare with the pre-pass budget; apply the
    5.5 decision rule if p95 + 120 s exceeds the pre-pass. Verify: Lambda log fields quoted;
    health JSON C1..C7 pass each time.
    - _Requirements: R10.2, R10.3, R5.6, R5.7_
  - [ ] 14.2 **ClientHello replay (step 2):** invoke the canary `dtls_replay` -> `pass`
    (HelloVerifyRequest/ServerHello); negative control to a closed port -> `no_reply`. Verify:
    responses quoted.
    - _Requirements: R10.4, R9.1, R9.2_
  - [ ] 14.3 **Injection + COMPLETED path (steps 3-4):** SSM runs `sync_receiver_inject.py`
    (all variants, optional rate test 5b with `UdpRcvbufErrors` and disk check); start the real state
    machine with the COMPLETED input and the same contact id; expect finish, evidence + raw + cadu in
    S3, instance stopped, Batch aggregation ending at the expected `AggregationFailure` (strict
    zero-RDR). Verify by evidence: S3 objects with fresh timestamps, `segment.json` counters = injected
    counts, evidence content matches R6.4 (not the Step Functions exit code).
    - _Requirements: R10.5, R6.4, R13.4_
  - [ ] 14.4 **FAILED path (step 5):** `dryrun-0002`, `outcome = failed`: evidence in S3, instance
    stopped, no Batch job, SNS received. Verify: S3 listing, EC2 state, absence of a Batch job.
    - _Requirements: R10.6, R7.2, R7.3_
  - [ ] 14.5 **Tag behaviours (step 6):** change tag mid-run (segment switch), inject before the tag
    exists (`unassigned` adoption), reuse an uploaded id (refused). Verify: `segment.json` and finish
    WARN/adoption record.
    - _Requirements: R10.7, R4.5, R4.6, R6.2, R6.6_
  - [ ] 14.6 **Alarm paths (step 7):** stop `rtlogic-ddx` by SSM -> `DdxReadyFailure` alarms; restore;
    stop the listener unit -> `ListenerReadyFailure`; restore; instance left `stopped`. Verify: alarm
    state history.
    - _Requirements: R8.1, R5.5_
  - [ ] 14.7 **Discovery (step 8, U6):** record DDX REST paths exposing stream statistics; add them
    to the finish evidence list (a follow-up commit on `sync_receiver_finish.sh` by `python`, with its
    test) and to `design.md`'s U6 via a review note, not by editing the locked spec. Verify: finish
    test green with the new paths.
    - _Requirements: R6.4_
  - [ ] 14.8 **Termination of the dry run:** stop the instance, confirm no Batch job or state machine
    execution is left running, delete temporary test S3 prefixes only if the user agrees (they are
    evidence). Verify: describe-instances `stopped`.
    - _Requirements: R10.2_

- [ ] 15. **Close the dry run**
  - [ ] 15.1 Append the run log to `docs/SYNC_DRY_RUN.md` (what ran, evidence, timings, deviations
    seen, findings). Delegate: `docs`. Verify: commit contains the run log.
    - _Requirements: R10.1, R14.3_
  - [ ] 15.2 `docs/CONTACTS.md` entry updated with results per deviation row; `docs/LESSONS_SYNC_FIRST_CONTACTS.md`
    status updated. Delegate: `docs`.
    - _Requirements: R10.1, R14.3_
  - [ ] 15.3 **Say the unvalidated residuals to the user BEFORE any booking (R10.8):** full DTLS
    handshake with the real Ground Station client, DDX framing inside DTLS, real payload layout and
    packet-type mix (U1, U2), real rate. State it in the conversation, and record it. Verify: message
    posted; user acknowledgement noted.
    - _Requirements: R10.8, R1 (risk R1)_
  - [ ] 15.4 Final learning backport: fill the dry-run findings into `aws-knowledge.md` and the
    `aws-ground-station` skill. Delegate: `docs`.
    - _Requirements: R14.3_

- [ ] 16. **Final docs-coherence pass**
  - [ ] 16.1 Grep the docs, steering, skills, scripts, tests and spec docs for stale statements:
    "listener on 55888" / "55888" near "listen", "arm" via SSM / `sync_receiver_arm`, the canary as
    plain-UDP-only, "installed manually" receiver scripts, `receiver_udp_port`, the old fixed 28-byte
    header claim, "~127 B" ClientHello. Each hit is fixed or marked superseded with a pointer to this
    spec (not deleted). Delegate: `docs`. Verify: grep output after the fix shows only marked
    historical passages; `docs/README.md` index lists the spec, the AMI inspection and the diagram.
    - _Requirements: R14.1, R14.2_

- [ ] 17. **Checkpoint E - everything green before booking**
  - Full `pytest` green, `terraform validate` + `fmt` + checkov clean, post-apply `terraform plan` =
    No changes (re-run), dry run log committed, docs coherent, PR merged. Verify: outputs quoted.
    - _Requirements: R13.6, R11.4_

## Stage F - Gated booking

- [ ] 18. **Book a sync contact (GATED - requires an explicit user go-ahead; books nothing by itself)**
  - [ ] 18.1 Preconditions: task 17 done, dry run passed, residuals acknowledged (15.3), SSO session
    refreshed shortly before, the operator present for PREPASS and POSTPASS, a full pass (D5).
    **Do NOT run `reserve-contact` until the user explicitly says "book" for a named pass.** Then
    reserve with `reserve-contact` on the sync profile (full pass, no sub-window), record it in
    `docs/CONTACTS.md`, and confirm the `list-contacts` entry.
    Delegate: orchestrator (only after go-ahead). Verify: `list-contacts` shows the contact.
    - _Requirements: R15.1, R11.6_
  - [ ] 18.2 After the contact: follow-up per spec section 11.1 (D8): open the separate spec for two
    security groups and a dedicated DFEG role only after the first successful DDX contact (R16.3);
    do not start it earlier. Delegate: orchestrator.
    - _Requirements: R16.2, R16.3_

---

## Checkpoint map

| Checkpoint | Task | Gate |
|------------|------|------|
| Full pytest green | 8, 17 | automated |
| `terraform validate` + `fmt` + Checkov | 10.1, 10.2, 17 | automated; HIGH/CRITICAL block |
| Plan summary shown to the user | 12.3 | **USER AGREEMENT required** |
| Terraform apply (orchestrator only) | 12.4 | only after 12.3 |
| Post-apply plan = No changes | 12.5, 17 | automated |
| Dry-run residuals stated to the user | 15.3 | user acknowledgement |
| Book a sync contact | 18.1 | **explicit user go-ahead** |

Only 12.4 applies Terraform. Any later infra tweak found in the dry run needs its own plan summary,
user agreement and apply (one at a time).

## Spec gaps found

(requirements.md and design.md were NOT edited; these are handled in the tasks or need a decision.)

- **G1 - 14-day prune has no owner.** R6.6 and design 5.7 say spool segments older than 14 days are
  pruned "at boot", but no component in section 4/5 does it. Task 1.3 puts it in the listener at
  start-up; alternative is the provisioning/install unit. Confirm.
- **G2 - Instance-type allow-list vs R1.3.** R1.3 says "reject fewer than 4 vCPUs"; design 4 uses a
  finite allow-list (`c6i.xlarge`, `c6i.2xlarge`, `c6i.4xlarge`, `c5.xlarge`...). The list is
  incomplete in the design ("...") and `c5.xlarge` is 4 vCPUs, others unspecified. Task 9.1 fixes the
  exact list; the user should confirm it.
- **G3 - Shell scripts have no delegate agent.** The delegation rule covers `*.tf` and `*.py`; bash and
  `.tftpl` are not named. Tasks assign them to `python`; confirm or add a rule.
- **G4 - FAILED event existence (U9) is still an assumption.** R7.1 depends on `contactStatus = FAILED`;
  design makes it a cutover-time check (12.2). If the event is absent, R7 cannot be validated before
  a live contact and row 5 stays a residual.
- **G5 - Pinned AMI id value is not in the spec.** R1 requires a variable but neither document records
  the id or name to pin (only "inspected 2026-11-26" in `docs/DDX_AMI_INSPECTION.md`). Task 9.1
  takes it from that doc; confirm the id is still available (R2 risk: AWS may withdraw it).
- **G6 - Fixture extraction needs an S3 read.** Design says the SHA-256 is "recorded in this design's
  task list"; task 7.1 does that, but extraction requires one read of contact #9's raw capture (an AWS
  call at implementation time, not at spec time).
- **G7 - R5.1 vs design 5.5 on tag failure.** R5.1 is silent when `CreateTags` fails; design 5.5
  starts the instance anyway and raises at the end. Task 6.1 follows the design.
- **G8 - Dry run needs a state machine input and a "U6 discovery" edit of finish after cutover.**
  Finish-evidence additions (14.7) change a runtime script, which reaches the instance via S3 at the
  next boot (design 5.2 "live change"): that follow-up needs a Terraform `aws_s3_object` update and
  therefore another apply (own plan summary and agreement), unless the paths are only recorded for
  the next change.
- **G9 - Canary SG shares the receiver SG (D8)**: the replay validates the SG in the shared form only;
  after the D8 follow-up it must be re-run. Recorded, no task change.
