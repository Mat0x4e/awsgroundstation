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
# Trial 4 (bisect): with AWS's AquaSnppJpss stack deployed minimally, mix OUR pieces and the TEMPLATE's pieces
# in throwaway mission profiles and see which combinations get contacts. Everything is deleted at the end.
#   A = our antenna(demod/decode) config + template endpoint config + our tracking
#   B = template antenna config + our endpoint config + our tracking
#   C = template antenna config + template endpoint config + our tracking
# Creates temporarily (outside Terraform): the stack's resources, 3 mission profiles, 1 throwaway EC2 key pair. No contact reserved.
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$stack = "aqua-snpp-jpss-trial"; $key = "trial-aqua-key"
$tplLocal = Join-Path $env:TEMP "aqua_tpl.yml"
$subnet = "subnet-06696708fc235f51e"; $vpc = "vpc-0145dbbe1c5fb77d8"
$ours = aws groundstation get-mission-profile --mission-profile-id 2e719f99-35f5-4720-b974-ba177036c1ee --output json | ConvertFrom-Json
$ourAntenna = $ours.dataflowEdges[0][0]; $ourEndpoint = $ours.dataflowEdges[0][1]; $ourTracking = $ours.trackingConfigArn
$u8 = New-Object Text.UTF8Encoding($false); $made = @(); $keyCreated = $false
try {
  aws s3 cp s3://groundstation-cloudformation-templates-eu-central-1/AquaSnppJpss.yml $tplLocal | Out-Null
  aws ec2 create-key-pair --key-name $key --query KeyMaterial --output text | Out-Null; $keyCreated = $true
  aws cloudformation deploy --stack-name $stack --template-file $tplLocal --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset `
    --parameter-overrides CreateReceiverInstance=No CloudWatchEventActions=None CreateEC2VerificationForContacts=No SSHKey=$key SubnetId=$subnet VpcId=$vpc | Out-Null
  $cfgs = (aws groundstation list-configs --output json | ConvertFrom-Json).configList
  $tAntenna = ($cfgs | ? { $_.name -eq "SNPP JPSS Downlink Demod Decode Antenna Config" } | select -First 1).configArn
  $tEndpoint = ($cfgs | ? { $_.name -eq "Aqua SNPP JPSS Downlink Demod Decode Endpoint Config" } | select -First 1).configArn
  "template antenna : $tAntenna"; "template endpoint: $tEndpoint"
  $tests = @(
    @{ n = "A our-antenna+tpl-endpoint";  src = $ourAntenna; dst = $tEndpoint },
    @{ n = "B tpl-antenna+our-endpoint";  src = "$tAntenna/UncodedFramesEgress"; dst = $ourEndpoint },
    @{ n = "C tpl-antenna+tpl-endpoint";  src = "$tAntenna/UncodedFramesEgress"; dst = $tEndpoint },
    @{ n = "D our-antenna+our-endpoint";  src = $ourAntenna; dst = $ourEndpoint }
  )
  $ids = @{}
  foreach ($t in $tests) {
    $f = Join-Path $env:TEMP "gs_bisect_edges.json"
    [IO.File]::WriteAllText($f, '[["' + $t.src + '","' + $t.dst + '"]]', $u8)
    $m = aws groundstation create-mission-profile --name ("trial4-" + $t.n.Substring(0,1)) --contact-pre-pass-duration-seconds 120 --contact-post-pass-duration-seconds 60 `
      --minimum-viable-contact-duration-seconds 180 --tracking-config-arn $ourTracking --dataflow-edges "file://$f" --tags Purpose=trial-delete-me --output json | ConvertFrom-Json
    $made += $m.missionProfileId; $ids[$t.n] = $m.missionProfileId
  }
  "profiles created; waiting 150 s for propagation"; Start-Sleep -Seconds 150
  foreach ($t in $tests) {
    "== $($t.n) -> $($ids[$t.n])"
    powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\gs_list_sync_contacts.ps1" -ProfileId $ids[$t.n] | Select-String -NotMatch "^   " | Select-String "contacts|ERROR"
  }
} catch { "ERROR: $_" }
finally {
  "cleanup..."
  foreach ($id in $made) { aws groundstation delete-mission-profile --mission-profile-id $id | Out-Null; "deleted profile $id" }
  aws cloudformation delete-stack --stack-name $stack
  aws cloudformation wait stack-delete-complete --stack-name $stack
  if ($keyCreated) { aws ec2 delete-key-pair --key-name $key | Out-Null; "key pair deleted" }
  Remove-Item $tplLocal, (Join-Path $env:TEMP "gs_bisect_edges.json") -ErrorAction SilentlyContinue
}
