# Trial: clone the sync mission profile with different durations, list contacts, then delete the clone.
# Creates ONE throwaway Ground Station mission profile (free, no contact reserved) and always deletes it.
# Usage: powershell -File scripts/gs_sync_profile_trial.ps1 [-PrePass 120] [-MinViable 180]
param([int]$PrePass = 120, [int]$MinViable = 180, [int]$PostPass = 120)
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$src = "2e719f99-35f5-4720-b974-ba177036c1ee"
$p = aws groundstation get-mission-profile --mission-profile-id $src --output json | ConvertFrom-Json
$edge = $p.dataflowEdges[0]
$name = "trial-sync-pre$PrePass-min$MinViable"
$id = $null
$edgeFile = Join-Path $env:TEMP "gs_trial_edges.json"
$edgeJson = "[[`"" + $edge[0] + "`",`"" + $edge[1] + "`"]]"
[IO.File]::WriteAllText($edgeFile, $edgeJson, (New-Object Text.UTF8Encoding($false)))
try {
  $r = aws groundstation create-mission-profile --name $name `
    --contact-pre-pass-duration-seconds $PrePass --contact-post-pass-duration-seconds $PostPass `
    --minimum-viable-contact-duration-seconds $MinViable `
    --tracking-config-arn $p.trackingConfigArn `
    --dataflow-edges "file://$edgeFile" `
    --tags Purpose=trial-delete-me --output json 2>&1 | Out-String
  $id = ($r | ConvertFrom-Json).missionProfileId
  "created $name -> $id"
  powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\gs_list_sync_contacts.ps1" -ProfileId $id
} catch { "ERROR: $_ ; raw: $r" }
finally {
  if ($id) { aws groundstation delete-mission-profile --mission-profile-id $id | Out-Null; "deleted $id" }
}
