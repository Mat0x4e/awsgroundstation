# Trial 3: deploy AWS's own AquaSnppJpss.yml in MINIMAL form (no EC2, no Lambdas, no SNS),
# list NOAA-20 contacts with the template's JPSS mission profile, then delete the stack and the throwaway key pair.
# Creates (temporarily, outside Terraform): IAM role, 2 SGs, 1 ENI, endpoint group, configs, 2 mission profiles. No contact reserved.
$env:AWS_PROFILE = "AWSAdminAccess-471112743408"; $env:AWS_REGION = "eu-central-1"
$stack = "aqua-snpp-jpss-trial"; $key = "trial-aqua-key"
$tpl = Join-Path $env:TEMP "aqua\AquaSnppJpss.yml"
$subnet = "subnet-06696708fc235f51e"; $vpc = "vpc-0145dbbe1c5fb77d8"
$sat = "arn:aws:groundstation::471112743408:satellite/33f035e1-73f7-47a5-9df8-fbc48636dca8"
$keyCreated = $false
try {
  aws ec2 create-key-pair --key-name $key --query KeyMaterial --output text | Out-Null; $keyCreated = $true; "key pair created"
  aws cloudformation deploy --stack-name $stack --template-file $tpl --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset `
    --parameter-overrides CreateReceiverInstance=No CloudWatchEventActions=None CreateEC2VerificationForContacts=No `
    SSHKey=$key SubnetId=$subnet VpcId=$vpc 2>&1 | Out-String
  $mps = aws groundstation list-mission-profiles --output json | ConvertFrom-Json
  foreach ($mp in $mps.missionProfileList | ? { $_.name -match "SNPP|Aqua" }) {
    "== profile '$($mp.name)' $($mp.missionProfileId)"
    powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\gs_list_sync_contacts.ps1" -ProfileId $mp.missionProfileId
  }
} catch { "ERROR: $_" }
finally {
  "cleanup..."
  aws cloudformation delete-stack --stack-name $stack
  aws cloudformation wait stack-delete-complete --stack-name $stack
  "stack status after delete: " + (aws cloudformation describe-stacks --stack-name $stack 2>&1 | Select-Object -First 1)
  if ($keyCreated) { aws ec2 delete-key-pair --key-name $key | Out-Null; "key pair deleted" }
}
