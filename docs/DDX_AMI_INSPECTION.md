# DDX AMI inspection (2026-11-26)

Read-only inspection of the AWS Ground Station dataflow endpoint application AMI, done to
design the sync receiver rebuild after contact #9 (see
[`LESSONS_SYNC_FIRST_CONTACTS.md`](LESSONS_SYNC_FIRST_CONTACTS.md), fault 5). Desk research
that preceded it: `.agents/tasks/ddx-dataflow-endpoint-2026-11-26/findings.md` (untracked).

## Method

- AMI `ami-0da9217abb117f5a1`, `groundstation-al2023-ddx2.6.2-ami-2026-11-23T17-00-34.194Z`
  (owner 575398917104, shared with this account; "DDX (v2.6.2) Only", no qRadio).
- Temporary instance in the default subnet, own security group with no inbound rules, receiver
  instance profile (SSM), encrypted root volume, `Owner` tag (org SCP). Not attached to the
  dataflow endpoint group: no Ground Station side effect.
- First boot as `c6i.large` (2 vCPU, one NIC), then stopped, resized to `c6i.xlarge` (4 vCPU,
  the receiver type), a second ENI attached at device index 1, restarted.
- Read via SSM only. All temporary resources (instance, ENI, 2 EIPs, security group) deleted the
  same day; leftover check by tag returned nothing. Cost: well under 1 USD.

## Results

| Topic | Finding |
|---|---|
| OS / kernel | Amazon Linux 2023, kernel 6.18; SSM agent 3.3 and AWS CLI v2 preinstalled; SSM registered with the receiver role in ~40 s |
| NIC names | Legacy names: `eth0`, `eth1` (not `ens5`/`ens6`) |
| DDX package | `ddx-2.6.2-104`, systemd unit `rtlogic-ddx` (enabled), depends on `postgresql` (local, 127.0.0.1:5432) |
| **vCPU floor** | Unit sets `CPUAffinity=2 3 4 ... 15`. On 2 vCPUs DDX does not start (`status=215/CPUAFFINITY`). On 4 vCPUs (`c6i.xlarge`) it is `active`. **Receiver needs >= 4 vCPUs.** |
| Licence | Permanent licences detected at start (`LicenseManager`); no licence step needed |
| Pre-built stream | Image ships a `Downlink` stream (WAN device -> `lo:50000`) in `/opt/aws/groundstation/customer_stream_config.json`, saved as DDX default config |
| WAN bind | DDX binds the WAN device IP, port 55888 (`172.31.23.174:55888` on the test ENI; will be `172.31.23.74:55888` on ours) |
| LAN output | `socketType: udpClient`, `remoteHost 127.0.0.1`, port 50000: **DDX sends** decrypted data to `127.0.0.1:50000`; our listener binds there |
| WAN protocol | `wanProtocol: Sink`, `enableFraming: true`, `enablePacking: true`, `mtu: 1450`, reordering window 5, max path skew 20 |
| REST API | `http://localhost:80/rest/dataDefender/_attribute/streamConfigs/` (JSON); helpers `configure_streams.py`, `display_all_streams.py`, `delete_all_streams.py`, `save_default_config.py` in `/opt/aws/groundstation/bin` |
| DTLS | Replaying the real contact #9 ClientHello (143 bytes, DTLS 1.2) at `eth1:55888` got a **HelloVerifyRequest** (48 bytes, handshake type 3, cookie): DDX answers Ground Station's handshake |
| `aws-groundstation-startup.service` | Fails at boot (relative `./getRegion.sh` paths); harmless: it only reinstalls DDX from `s3://groundstation-customer-assets-<region>/` when `/opt/aws/groundstation/.allowAwsToInstallSwAfterAnyReboot` exists. **No first-boot download, no extra IAM.** |

## Traps for the rebuild

1. **First-boot NIC re-pin.** `ddx-stream-nic-firstboot.service` (`/usr/local/bin/ddx-stream-nic-firstboot.sh`)
   rewrites `wanConfigDevice` to the NIC of the **first default route** unless the saved name
   still exists, then applies and saves it. Our Ground Station ENI is `eth1`, not the default
   route NIC. User data must write `wanConfigDevice: "eth1"` and run `configure_streams.py` +
   `save_default_config.py` itself (as AWS's templates do), before that unit runs (it is ordered
   `After=cloud-final.service`).
2. **Default route order with two ENIs.** On first boot cloud-init listed `default via eth1`
   first; `eth1` has no public IP, so SSM and S3 timed out and the SSM agent hibernated. After a
   reboot the metrics were `eth0` 512 / `eth1` 513. The order is not guaranteed: pin it (e.g.
   no default route on `eth1` via systemd-networkd config in user data) so egress always uses
   `eth0` + the EIP.
3. **Public IP with two ENIs.** Attaching the second ENI removed the auto-assigned public IP
   (same as the current receiver): the EIP on the primary ENI stays required.
4. **vCPU floor**: keep `c6i.xlarge` or larger; never a 2-vCPU type.

## Still open

- The data layout DDX emits on `127.0.0.1:50000` for `UncodedFramesEgress` (VITA-49.2 header
  variant). Only a real contact shows it; the raw tee on the listener captures it.
- Whether a non-Ground-Station peer can push data through DDX: the WAN side speaks DDX framing
  and packing inside DTLS (`wanProtocol: Sink`), so a plain OpenSSL client can at most complete
  the handshake. A full offline test of the WAN leg would need a second DDX acting as sender
  (`isUplink: true` stream); not explored.
