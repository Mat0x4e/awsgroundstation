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
# Trial 2: AWS AquaSnppJpss sets DataflowEndpointRegion on the dataflow-endpoint config; ours does not.
# Creates a throwaway endpoint config WITH the region + a throwaway mission profile using it
# (our existing demod/decode + tracking configs), lists contacts, then deletes both. Free; no contact reserved.
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$src = "2e719f99-35f5-4720-b974-ba177036c1ee"
$epName = "groundstation-noaa20-demo-sync-endpoint"
$p = aws groundstation get-mission-profile --mission-profile-id $src --output json | ConvertFrom-Json
$demodArn = $p.dataflowEdges[0][0]
$u8 = New-Object Text.UTF8Encoding($false)
$cfgFile = Join-Path $env:TEMP "gs_trial2_cfg.json"; $edgeFile = Join-Path $env:TEMP "gs_trial2_edges.json"
[IO.File]::WriteAllText($cfgFile, '{"dataflowEndpointConfig":{"dataflowEndpointName":"' + $epName + '","dataflowEndpointRegion":"eu-central-1"}}', $u8)
$cfgId = $null; $mpId = $null
try {
  $c = aws groundstation create-config --name "trial2-endpoint-with-region" --config-data "file://$cfgFile" --output json | ConvertFrom-Json
  $cfgId = $c.configId; $cfgArn = $c.configArn; "created endpoint config $cfgId"
  [IO.File]::WriteAllText($edgeFile, '[["' + $demodArn + '","' + $cfgArn + '"]]', $u8)
  $m = aws groundstation create-mission-profile --name "trial2-sync-endpoint-region" `
    --contact-pre-pass-duration-seconds 120 --contact-post-pass-duration-seconds 60 --minimum-viable-contact-duration-seconds 180 `
    --tracking-config-arn $p.trackingConfigArn --dataflow-edges "file://$edgeFile" --tags Purpose=trial-delete-me --output json | ConvertFrom-Json
  $mpId = $m.missionProfileId; "created mission profile $mpId"
  powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\gs_list_sync_contacts.ps1" -ProfileId $mpId
} catch { "ERROR: $_" }
finally {
  if ($mpId)  { aws groundstation delete-mission-profile --mission-profile-id $mpId | Out-Null; "deleted profile $mpId" }
  if ($cfgId) { aws groundstation delete-config --config-id $cfgId --config-type dataflow-endpoint | Out-Null; "deleted config $cfgId" }
  Remove-Item $cfgFile, $edgeFile -ErrorAction SilentlyContinue
}
