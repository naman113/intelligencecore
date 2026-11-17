<#
  Quick 029 (F9): add SETTINGS_ENCRYPTION_KEY to a .env if missing or empty; NEVER overwrite.
  PowerShell twin of ensure-settings-key.sh, for deploy/local.ps1 up (Windows PowerShell 5.1 safe:
  no Convert.ToHexString, no static RandomNumberGenerator.GetBytes(int)).
  Never prints the key. Usage: ensure-settings-key.ps1 <path-to-.env>
#>
param([Parameter(Position = 0)][string]$EnvFile)

$ErrorActionPreference = 'Stop'
if (-not $EnvFile -or -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    Write-Host "no .env: copy .env.example to .env first (docs/LOCAL_DOCKER.md)"
    exit 1
}
$text = [IO.File]::ReadAllText($EnvFile)
if ([regex]::IsMatch($text, '(?m)^SETTINGS_ENCRYPTION_KEY=[ \t]*[^ \t\r\n]')) {
    Write-Host "SETTINGS_ENCRYPTION_KEY kept"
    exit 0
}
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$key = (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')

$emptyLine = '(?m)^SETTINGS_ENCRYPTION_KEY=[ \t]*(?=\r?$)'
if ([regex]::IsMatch($text, $emptyLine)) {
    $text = [regex]::Replace($text, $emptyLine, { param($m) "SETTINGS_ENCRYPTION_KEY=$key" })
} else {
    $eol = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += $eol }
    $text += "SETTINGS_ENCRYPTION_KEY=$key$eol"
}
[IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "SETTINGS_ENCRYPTION_KEY added"
