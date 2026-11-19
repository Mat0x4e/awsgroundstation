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
# Read-only diff: deploy AWS's AquaSnppJpss minimally, dump the template's antenna config, endpoint config,
# endpoint group and tracking config next to OURS, print differences, then delete the stack + throwaway key pair.
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$stack = "aqua-snpp-jpss-trial"; $key = "trial-aqua-key"; $tplLocal = Join-Path $env:TEMP "aqua_tpl.yml"
$out = Join-Path $env:TEMP "gs_diff"; New-Item -ItemType Directory -Force $out | Out-Null
$subnet = "subnet-06696708fc235f51e"; $vpc = "vpc-0145dbbe1c5fb77d8"; $keyCreated = $false
function Norm($j) { ($j | ConvertFrom-Json) | ConvertTo-Json -Depth 30 }
try {
  aws s3 cp s3://groundstation-cloudformation-templates-eu-central-1/AquaSnppJpss.yml $tplLocal | Out-Null
  aws ec2 create-key-pair --key-name $key --query KeyMaterial --output text | Out-Null; $keyCreated = $true
  aws cloudformation deploy --stack-name $stack --template-file $tplLocal --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset `
    --parameter-overrides CreateReceiverInstance=No CloudWatchEventActions=None CreateEC2VerificationForContacts=No SSHKey=$key SubnetId=$subnet VpcId=$vpc | Out-Null
  $ours = aws groundstation get-mission-profile --mission-profile-id 2e719f99-35f5-4720-b974-ba177036c1ee --output json | ConvertFrom-Json
  $cfgs = (aws groundstation list-configs --output json | ConvertFrom-Json).configList
  $pairs = @(
    @{ k = "antenna";  t = "antenna-downlink-demod-decode"; ours = $ours.dataflowEdges[0][0].Split("/")[-2]; theirs = ($cfgs | ? { $_.name -eq "SNPP JPSS Downlink Demod Decode Antenna Config" }).configId },
    @{ k = "endpoint"; t = "dataflow-endpoint"; ours = $ours.dataflowEdges[0][1].Split("/")[-1]; theirs = ($cfgs | ? { $_.name -eq "Aqua SNPP JPSS Downlink Demod Decode Endpoint Config" }).configId },
    @{ k = "tracking"; t = "tracking"; ours = $ours.trackingConfigArn.Split("/")[-1]; theirs = ($cfgs | ? { $_.name -eq "Aqua SNPP JPSS Tracking Config" }).configId }
  )
  foreach ($p in $pairs) {
    foreach ($side in "ours", "theirs") {
      Norm (aws groundstation get-config --config-id $p[$side] --config-type $p.t --output json) | Set-Content "$out\$($p.k)_$side.json" -Encoding UTF8
    }
    "=== $($p.k) ==="
    $d = Compare-Object (Get-Content "$out\$($p.k)_ours.json") (Get-Content "$out\$($p.k)_theirs.json")
    if ($d) { $d | % { "{0} {1}" -f $_.SideIndicator, $_.InputObject.Trim() } } else { "(identical)" }
  }
  "=== endpoint groups ==="
  $groups = (aws groundstation list-dataflow-endpoint-groups --output json | ConvertFrom-Json).dataflowEndpointGroupList
  foreach ($g in $groups) { Norm (aws groundstation get-dataflow-endpoint-group --dataflow-endpoint-group-id $g.dataflowEndpointGroupId --output json) | Set-Content "$out\group_$($g.dataflowEndpointGroupId).json" -Encoding UTF8 }
  Get-ChildItem "$out\group_*.json" | % { "--- $($_.Name)"; Get-Content $_ }
} catch { "ERROR: $_" }
finally {
  aws cloudformation delete-stack --stack-name $stack
  aws cloudformation wait stack-delete-complete --stack-name $stack
  if ($keyCreated) { aws ec2 delete-key-pair --key-name $key | Out-Null }
  Remove-Item $tplLocal -ErrorAction SilentlyContinue
  "cleanup done"
}
