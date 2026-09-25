# Stops HogWatch from starting automatically at login. Your history in data\ is kept.

$ErrorActionPreference = 'Stop'
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
try {
    Invoke-WebRequest -Method POST -Uri http://127.0.0.1:8765/api/shutdown -Headers @{ 'X-HogWatch' = '1' } -UseBasicParsing -TimeoutSec 3 | Out-Null
} catch { }
if (Get-ScheduledTask -TaskName 'HogWatch' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'HogWatch' -Confirm:$false
    Write-Host 'HogWatch will no longer start automatically.'
} else {
    Write-Host 'HogWatch was not set to start automatically.'
}
Read-Host 'Press Enter to close'
