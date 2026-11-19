# === HISTORICAL TRIAL SCRIPT (2026-11-18) - NOT SAFE TO RUN BLIND ===
# Creates and deletes REAL objects in account 471112743408 (CloudFormation stack, EC2 key pair,
# Ground Station configs / mission profiles / endpoint groups, ENIs) using FIXED names. Its cleanup
# deletes by name, so it can remove objects another session created with the same names. Kept as the
# record of the sync "0 contacts" investigation (.kiro/skills/aws-ground-station/references/
# sync-and-digital-twin.md). Before re-running: check no other session is mid-run (docs/WORKSTREAMS.md),
# then set $env:GS_TRIAL_I_UNDERSTAND = "yes".
if ($env:GS_TRIAL_I_UNDERSTAND -ne "yes") {
  Write-Error "Refusing to run: historical trial script that creates and deletes shared AWS objects. See the header."
  exit 1
}
# Trial 6: is the endpoint ADDRESS (our ENI is attached to a STOPPED instance) the problem? Groups identical to ours (mtu 1500, our role, our SG)
# but on (a) the template's unattached ENI and (b) a fresh unattached ENI. Derived from gs_h6_h7_trial.ps1 (trial 5).
# Template stack deployed minimally for reference objects; everything created here is throwaway and deleted at the end.
#   P1 = X1 (template's antenna config copied byte-for-byte under a NEW name, no CloudFormation origin) + template endpoint
#   P2 = template antenna + endpoint of group G1 (NO mtu, our role, our SG)
#   P3 = template antenna + endpoint of group G2 (mtu 1500, TEMPLATE role, TEMPLATE SG)
#   P4 = template antenna + endpoint of group G3 (NO mtu, TEMPLATE role, TEMPLATE SG)
# Known from trial 4: template antenna + OUR endpoint (mtu 1500, our role, our SG) = 0 contacts; template pair = contacts.
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$stack = "aqua-snpp-jpss-trial"; $key = "trial-aqua-key"; $tplLocal = Join-Path $env:TEMP "aqua_tpl.yml"
$subnet = "subnet-06696708fc235f51e"; $vpc = "vpc-0145dbbe1c5fb77d8"
$u8 = New-Object Text.UTF8Encoding($false); $tmp = $env:TEMP
$profiles = @(); $configs = @(); $groups = @(); $enis = @(); $keyCreated = $false
$ours = aws groundstation get-mission-profile --mission-profile-id 2e719f99-35f5-4720-b974-ba177036c1ee --output json | ConvertFrom-Json
$ourTracking = $ours.trackingConfigArn
$ourGroup = aws groundstation get-dataflow-endpoint-group --dataflow-endpoint-group-id da5e60d9-1bc2-42c5-8d8e-22bd379465a3 --output json | ConvertFrom-Json
$ourEp = $ourGroup.endpointsDetails[0]
function NewGroup($name, $addr, $mtu, $role, $sg) {
  $ep = @{ name = $name; address = @{ name = $addr; port = 55888 } }; if ($mtu) { $ep.mtu = $mtu }
  $d = @(@{ endpoint = $ep; securityDetails = @{ subnetIds = @($subnet); securityGroupIds = @($sg); roleArn = $role } })
  $f = Join-Path $tmp "gs_t6_group.json"; [IO.File]::WriteAllText($f, (ConvertTo-Json $d -Depth 8 -Compress), $u8)
  $r = aws groundstation create-dataflow-endpoint-group --endpoint-details "file://$f" --contact-pre-pass-duration-seconds 120 --contact-post-pass-duration-seconds 120 --output json 2>&1 | Out-String
  try { ($r | ConvertFrom-Json).dataflowEndpointGroupId } catch { "ERR: " + ($r -replace "\s+", " ") }
}
function NewEpConfig($name, $epName) {
  $f = Join-Path $tmp "gs_t6_cfg.json"; [IO.File]::WriteAllText($f, '{"dataflowEndpointConfig":{"dataflowEndpointName":"' + $epName + '"}}', $u8)
  aws groundstation create-config --name $name --config-data "file://$f" --output json | ConvertFrom-Json
}
function NewProfile($name, $src, $dst) {
  $f = Join-Path $tmp "gs_t6_edges.json"; [IO.File]::WriteAllText($f, '[["' + $src + '","' + $dst + '"]]', $u8)
  (aws groundstation create-mission-profile --name $name --contact-pre-pass-duration-seconds 120 --contact-post-pass-duration-seconds 60 `
    --minimum-viable-contact-duration-seconds 180 --tracking-config-arn $ourTracking --dataflow-edges "file://$f" --tags Purpose=trial-delete-me --output json | ConvertFrom-Json).missionProfileId
}
try {
  aws s3 cp s3://groundstation-cloudformation-templates-eu-central-1/AquaSnppJpss.yml $tplLocal | Out-Null
  aws ec2 create-key-pair --key-name $key --query KeyMaterial --output text | Out-Null; if ($LASTEXITCODE -eq 0) { $keyCreated = $true }
  aws cloudformation deploy --stack-name $stack --template-file $tplLocal --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset `
    --parameter-overrides CreateReceiverInstance=No CloudWatchEventActions=None CreateEC2VerificationForContacts=No SSHKey=$key SubnetId=$subnet VpcId=$vpc | Out-Null
  $cfgs = (aws groundstation list-configs --output json | ConvertFrom-Json).configList
  $tAnt = $cfgs | ? { $_.name -eq "SNPP JPSS Downlink Demod Decode Antenna Config" }
  $tEp  = $cfgs | ? { $_.name -eq "Aqua SNPP JPSS Downlink Demod Decode Endpoint Config" }
  $tGroup = (aws groundstation list-dataflow-endpoint-groups --output json | ConvertFrom-Json).dataflowEndpointGroupList | ? { $_.dataflowEndpointGroupId -ne $ourGroup.dataflowEndpointGroupId }
  $tg = aws groundstation get-dataflow-endpoint-group --dataflow-endpoint-group-id $tGroup.dataflowEndpointGroupId --output json | ConvertFrom-Json
  $tRole = $tg.endpointsDetails[0].securityDetails.roleArn; $tSg = $tg.endpointsDetails[0].securityDetails.securityGroupIds[0]
  $addr = $ourEp.endpoint.address.name; $tAddr = $tg.endpointsDetails[0].endpoint.address.name
  $ids = [ordered]@{}

  # fresh, unattached ENI in the same subnet / SG as ours (free)
  $eni = (aws ec2 create-network-interface --subnet-id $subnet --groups $ourEp.securityDetails.securityGroupIds[0] --description "trial6 throwaway ENI" --output json | ConvertFrom-Json).NetworkInterface
  $enis += $eni.NetworkInterfaceId
  $variants = @(
    @{ n = "P1 group = ours but on TEMPLATE ENI $tAddr"; g = "trial6-g1"; a = $tAddr },
    @{ n = "P2 group = ours but on FRESH unattached ENI $($eni.PrivateIpAddress)"; g = "trial6-g2"; a = $eni.PrivateIpAddress },
    @{ n = "P3 control: group = ours on OUR ENI $addr (attached to stopped instance)"; g = "trial6-g3"; a = $addr }
  )
  foreach ($v in $variants) {
    $gid = NewGroup $v.g $v.a 1500 $ourEp.securityDetails.roleArn $ourEp.securityDetails.securityGroupIds[0]
    if ($gid -like "ERR*") { $ids[$v.n + "  [group creation failed: $gid]"] = $null; continue }
    $groups += $gid
    $c = NewEpConfig ("trial6-ep-" + $v.g) $v.g; $configs += @{ id = $c.configId; type = "dataflow-endpoint" }
    $ids[$v.n] = NewProfile ("trial6-" + $v.n.Substring(0, 2)) "$($tAnt.configArn)/UncodedFramesEgress" $c.configArn
  }
  $profiles = $ids.Values | ? { $_ }
  "objects created; waiting 150 s"; Start-Sleep -Seconds 150
  foreach ($k in $ids.Keys) {
    "== $k"
    if ($ids[$k]) { powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\..\..\gs_list_sync_contacts.ps1" -ProfileId $ids[$k] | Select-String "contacts|ERROR" }
  }
} catch { "ERROR: $_" }
finally {
  "cleanup..."
  foreach ($p in $profiles) { aws groundstation delete-mission-profile --mission-profile-id $p | Out-Null }
  foreach ($c in $configs) { aws groundstation delete-config --config-id $c.id --config-type $c.type | Out-Null }
  foreach ($g in $groups) { aws groundstation delete-dataflow-endpoint-group --dataflow-endpoint-group-id $g | Out-Null }
  foreach ($e in $enis) { aws ec2 delete-network-interface --network-interface-id $e | Out-Null }
  aws cloudformation delete-stack --stack-name $stack
  aws cloudformation wait stack-delete-complete --stack-name $stack
  if ($keyCreated) { aws ec2 delete-key-pair --key-name $key | Out-Null }
  Remove-Item $tplLocal, "$tmp\gs_t6_*.json" -ErrorAction SilentlyContinue
  "cleanup done: profiles=$($profiles.Count) configs=$($configs.Count) groups=$($groups.Count)"
}
