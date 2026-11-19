# Regression test for .kiro/hooks/block-terraform-apply.sh. Each case line is EXPECTED|command.
# Needs Git for Windows sh. Run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_block_terraform_apply_hook.ps1
$sh = "C:\Program Files\Git\bin\sh.exe"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$hook = Join-Path $root ".kiro\hooks\block-terraform-apply.sh"
$fail = 0
foreach ($line in [IO.File]::ReadAllLines((Join-Path $root ".kiro\hooks\tests\block-terraform-apply.cases.txt"))) {
  if (-not $line) { continue }
  $exp, $c = $line -split '\|', 2
  $json = @{ toolInput = @{ command = $c } } | ConvertTo-Json -Compress
  $tmp = "$env:TEMP\hook_in.json"; [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
  cmd /c "`"$sh`" `"$hook`" < `"$tmp`" 2>nul"
  $got = if ($LASTEXITCODE -eq 2) { "BLOCK" } else { "ALLOW" }
  $ok = if ($got -eq $exp) { "ok  " } else { $fail++; "FAIL" }
  "{0} expected {1,-5} got {2,-5} (exit {3})  {4}" -f $ok, $exp, $got, $LASTEXITCODE, $c
}
Remove-Item "$env:TEMP\hook_in.json" -ErrorAction SilentlyContinue
"failures: $fail"
if ($fail -gt 0) { exit 1 }
