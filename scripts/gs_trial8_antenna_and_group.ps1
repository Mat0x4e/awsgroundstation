# Trial 8: (R1) is OUR existing endpoint group fine once the endpoint config carries dataflowEndpointRegion?
#          (R2/R3) is the antenna-side failure the unvalidatedJSON string formatting (H6)?
#   R1 = template antenna + NEW endpoint config WITH region -> OUR existing group (groundstation-noaa20-demo-sync-endpoint)
#   R2 = copy of OUR antenna configData (compact, key-sorted JSON) under a new name + template endpoint (known good)
#   R3 = copy of the TEMPLATE antenna configData (AWS sample string) under a new name + template endpoint (control, = trial 5 P1)
# R2 vs R3 differ only by the JSON string formatting (both new names, no tags). Throwaway objects, deleted in finally.
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$stack = "aqua-snpp-jpss-trial"; $key = "trial-aqua-key"; $tplLocal = Join-Path $env:TEMP "aqua_tpl.yml"
$subnet = "subnet-06696708fc235f51e"; $vpc = "vpc-0145dbbe1c5fb77d8"
$u8 = New-Object Text.UTF8Encoding($false); $tmp = $env:TEMP
$profiles = @(); $configs = @(); $groups = @(); $antCfgs = @(); $keyCreated = $false
$ours = aws groundstation get-mission-profile --mission-profile-id 2e719f99-35f5-4720-b974-ba177036c1ee --output json | ConvertFrom-Json
$ourTracking = $ours.trackingConfigArn

function NewGroup($name, $addr, $mtu, $role, $sg, $pre, $post) {
  $ep = @{ name = $name; address = @{ name = $addr; port = 55888 } }; if ($mtu) { $ep.mtu = $mtu }
  $d = @(@{ endpoint = $ep; securityDetails = @{ subnetIds = @($subnet); securityGroupIds = @($sg); roleArn = $role } })
  $f = Join-Path $tmp "gs_t7_group.json"; [IO.File]::WriteAllText($f, (ConvertTo-Json $d -Depth 8 -Compress), $u8)
  $r = aws groundstation create-dataflow-endpoint-group --endpoint-details "file://$f" --contact-pre-pass-duration-seconds $pre --contact-post-pass-duration-seconds $post --output json 2>&1 | Out-String
  try { ($r | ConvertFrom-Json).dataflowEndpointGroupId } catch { "ERR: " + ($r -replace "\s+", " ") }
}
function NewEpConfig($name, $epName, $withRegion) {
  $body = '{"dataflowEndpointConfig":{"dataflowEndpointName":"' + $epName + '"' + $(if ($withRegion) { ',"dataflowEndpointRegion":"eu-central-1"' } else { '' }) + '}}'
  $f = Join-Path $tmp "gs_t7_cfg.json"; [IO.File]::WriteAllText($f, $body, $u8)
  aws groundstation create-config --name $name --config-data "file://$f" --output json | ConvertFrom-Json
}
function NewProfile($name, $src, $dst) {
  $f = Join-Path $tmp "gs_t7_edges.json"; [IO.File]::WriteAllText($f, '[["' + $src + '","' + $dst + '"]]', $u8)
  (aws groundstation create-mission-profile --name $name --contact-pre-pass-duration-seconds 120 --contact-post-pass-duration-seconds 60 `
    --minimum-viable-contact-duration-seconds 180 --tracking-config-arn $ourTracking --dataflow-edges "file://$f" --tags Purpose=trial-delete-me --output json | ConvertFrom-Json).missionProfileId
}

try {
  aws s3 cp s3://groundstation-cloudformation-templates-eu-central-1/AquaSnppJpss.yml $tplLocal | Out-Null
  aws ec2 create-key-pair --key-name $key --query KeyMaterial --output text | Out-Null
  if ($LASTEXITCODE -eq 0) { $keyCreated = $true } else { throw "key pair $key already exists - clean up first" }
  aws cloudformation deploy --stack-name $stack --template-file $tplLocal --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset `
    --parameter-overrides CreateReceiverInstance=No CloudWatchEventActions=None CreateEC2VerificationForContacts=No SSHKey=$key SubnetId=$subnet VpcId=$vpc | Out-Null
  $cfgs = (aws groundstation list-configs --output json | ConvertFrom-Json).configList
  $tAnt = $cfgs | ? { $_.name -eq "SNPP JPSS Downlink Demod Decode Antenna Config" }
  $tGroupId = ((aws groundstation list-dataflow-endpoint-groups --output json | ConvertFrom-Json).dataflowEndpointGroupList | ? { $_.dataflowEndpointGroupId -ne "da5e60d9-1bc2-42c5-8d8e-22bd379465a3" }).dataflowEndpointGroupId
  $tg = aws groundstation get-dataflow-endpoint-group --dataflow-endpoint-group-id $tGroupId --output json | ConvertFrom-Json
  $te = $tg.endpointsDetails[0]
  $tAddr = $te.endpoint.address.name; $tRole = $te.securityDetails.roleArn; $tSg = $te.securityDetails.securityGroupIds[0]
  "template group: addr $tAddr, pre $($tg.contactPrePassDurationSeconds), post $($tg.contactPostPassDurationSeconds), mtu '$($te.endpoint.mtu)'"

  $tEp = $cfgs | ? { $_.name -eq "Aqua SNPP JPSS Downlink Demod Decode Endpoint Config" }
  $ourAntId = $ours.dataflowEdges[0][0].Split("/")[-2]
  $ids = [ordered]@{}; $antCfgs = @()
  # R1
  $c = NewEpConfig "trial8-cfg-R1" "groundstation-noaa20-demo-sync-endpoint" $true; $configs += $c.configId
  $mpId = NewProfile "trial8-R1" "$($tAnt.configArn)/UncodedFramesEgress" $c.configArn; $profiles += $mpId
  $ids["R1 template antenna + region config -> OUR existing group"] = $mpId
  # R2 / R3
  foreach ($r in @(@{ n = "R2 copy of OUR antenna JSON (compact, sorted)"; id = $ourAntId; nm = "trial8-ant-ours" },
                  @{ n = "R3 copy of TEMPLATE antenna JSON (AWS sample string)"; id = $tAnt.configId; nm = "trial8-ant-tpl" })) {
    $cd = Join-Path $tmp "gs_t7_ant.json"
    [IO.File]::WriteAllText($cd, (aws groundstation get-config --config-id $r.id --config-type antenna-downlink-demod-decode --query configData --output json | Out-String), $u8)
    $x = aws groundstation create-config --name $r.nm --config-data "file://$cd" --output json | ConvertFrom-Json; $antCfgs += $x.configId
    $mpId = NewProfile ("trial8-" + $r.n.Substring(0, 2)) "$($x.configArn)/UncodedFramesEgress" $tEp.configArn; $profiles += $mpId
    $ids[$r.n] = $mpId
  }
  "objects created; waiting 150 s"; Start-Sleep -Seconds 150
  foreach ($k in $ids.Keys) {
    "== $k"
    if ($ids[$k]) { powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\gs_list_sync_contacts.ps1" -ProfileId $ids[$k] | Select-String "contacts|ERROR" }
  }
} catch { "ERROR: $_" }
finally {
  "cleanup..."
  foreach ($p in $profiles) { aws groundstation delete-mission-profile --mission-profile-id $p | Out-Null }
  foreach ($c in $configs)  { aws groundstation delete-config --config-id $c --config-type dataflow-endpoint | Out-Null }
  foreach ($a in $antCfgs)  { aws groundstation delete-config --config-id $a --config-type antenna-downlink-demod-decode | Out-Null }
  foreach ($g in $groups)   { aws groundstation delete-dataflow-endpoint-group --dataflow-endpoint-group-id $g | Out-Null }
  aws cloudformation delete-stack --stack-name $stack
  aws cloudformation wait stack-delete-complete --stack-name $stack
  if ($keyCreated) { aws ec2 delete-key-pair --key-name $key | Out-Null }
  Remove-Item $tplLocal, "$tmp\gs_t7_*.json" -ErrorAction SilentlyContinue
  "cleanup done: profiles=$($profiles.Count) configs=$($configs.Count) antenna=$($antCfgs.Count) groups=$($groups.Count)"
}
