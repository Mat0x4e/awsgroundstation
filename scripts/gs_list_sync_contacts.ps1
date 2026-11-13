# Read-only: list AVAILABLE contacts for a mission profile on every ground station, next 7 days.
# Usage: powershell -File scripts/gs_list_sync_contacts.ps1 [-ProfileId <id>] [-Hours 167]
param(
  [string]$ProfileId = "2e719f99-35f5-4720-b974-ba177036c1ee",   # sync (demod/decode)
  [int]$Hours = 167,                                               # stay under the 7-day wall
  [string]$Satellite = "33f035e1-73f7-47a5-9df8-fbc48636dca8"
)
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$acct = "471112743408"
$mp  = "arn:aws:groundstation:eu-central-1:${acct}:mission-profile/$ProfileId"
$sat = "arn:aws:groundstation::${acct}:satellite/$Satellite"
$start = (Get-Date).ToUniversalTime().AddMinutes(5).ToString("yyyy-MM-ddTHH:mm:ssZ")
$end   = (Get-Date).ToUniversalTime().AddHours($Hours).ToString("yyyy-MM-ddTHH:mm:ssZ")
"window $start -> $end ; profile $ProfileId"
$stations = (aws groundstation list-ground-stations --query "groundStationList[].groundStationName" --output json | ConvertFrom-Json)
"stations visible: " + ($stations -join ", ")
foreach ($gs in $stations) {
  $raw = aws groundstation list-contacts --status-list AVAILABLE --ground-station $gs --mission-profile-arn $mp --satellite-arn $sat --start-time $start --end-time $end --output json 2>&1 | Out-String
  try { $j = $raw | ConvertFrom-Json; "{0,-14} {1} contacts" -f $gs, @($j.contactList).Count
        foreach ($c in $j.contactList) { "   {0} -> {1}  maxEl {2}" -f $c.startTime, $c.endTime, $c.maximumElevation.value } }
  catch { "{0,-14} ERROR: {1}" -f $gs, ($raw.Trim() -replace "\s+"," ") }
}
