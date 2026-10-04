# Starts dist\HogWatch.exe on a spare port with a throwaway data folder, checks that the
# dashboard and its API answer, and shuts it down. Used by the GitHub workflows so a
# release is never published without having actually run.

$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot '..\dist\HogWatch.exe'
$data = Join-Path ([IO.Path]::GetTempPath()) ("hogwatch-smoke-" + [Guid]::NewGuid().ToString('N'))
$port = 8799
$base = "http://127.0.0.1:$port"

$proc = Start-Process -FilePath $exe -ArgumentList '--data-dir', "`"$data`"", '--port', $port, 'run', '--no-browser' -PassThru
try {
    $settings = $null
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 1
        try { $settings = Invoke-RestMethod "$base/api/settings" -TimeoutSec 10; break } catch { }
    }
    if (-not $settings) { throw 'HogWatch.exe did not start answering within 60 seconds' }
    if (-not $settings.packaged) { throw 'The exe does not report itself as packaged' }

    $page = Invoke-WebRequest "$base/" -UseBasicParsing
    if ($page.Content -notmatch 'eero-step-login') { throw 'The dashboard page is missing from the exe' }
    foreach ($file in 'app.js', 'style.css') {
        if ((Invoke-WebRequest "$base/$file" -UseBasicParsing).StatusCode -ne 200) { throw "$file is missing from the exe" }
    }
    $now = Invoke-RestMethod "$base/api/now" -TimeoutSec 10
    if (-not $now.status) { throw 'The monitor is not reporting a status' }
    if (-not (Test-Path (Join-Path $data 'hogwatch.db'))) { throw 'The exe did not use the data folder it was given' }

    Write-Host "HogWatch.exe $($settings.version) started, served the dashboard, status '$($now.status)', admin=$($settings.admin)"
}
finally {
    try { Invoke-RestMethod -Method Post "$base/api/shutdown" -Headers @{ 'X-HogWatch' = '1' } -TimeoutSec 10 | Out-Null } catch { }
    if (-not $proc.WaitForExit(20000)) { $proc.Kill() }
}
