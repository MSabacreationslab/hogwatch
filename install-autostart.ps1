# Makes HogWatch start automatically (hidden, with admin rights) every time you
# log in to Windows, so it's always watching -- slowdowns that happen while you're
# away still get recorded. Undo with uninstall-autostart.cmd.

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# Scheduling a task that runs with admin rights requires admin itself.
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" -Argument '-m hogwatch run --no-browser' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
# No time limit, keep running on battery, and restart if it ever crashes.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'HogWatch' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'HogWatch home network monitor (dashboard: http://127.0.0.1:8765/)' -Force | Out-Null

# Stop a copy started by hand so the scheduled copy can take over the dashboard port.
try {
    Invoke-WebRequest -Method POST -Uri http://127.0.0.1:8765/api/shutdown -Headers @{ 'X-HogWatch' = '1' } -UseBasicParsing -TimeoutSec 3 | Out-Null
    Start-Sleep -Seconds 3
} catch { }
Start-ScheduledTask -TaskName 'HogWatch'

Write-Host ''
Write-Host 'Done. HogWatch now starts automatically every time you log in.'
Write-Host 'Dashboard: http://127.0.0.1:8765/  (or double-click open-dashboard.cmd)'
Read-Host 'Press Enter to close'
