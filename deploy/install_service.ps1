# NetraAttend - install / reinstall as a background service on this PC.
# Run as Administrator (double-click deploy\INSTALL.bat). It is safe to run again.
#
# What it changes on this PC:
#   1. stops any other running copy of NetraAttend (the camera allows one connection)
#   2. Windows Firewall: allows the office network (local subnet only) to reach port 5000
#   3. Power plan: the PC never sleeps / hibernates while plugged in
#   4. Scheduled task "NetraAttend": starts the app at boot as SYSTEM, no login needed,
#      and keeps it running (netra_service.bat restarts it if it stops)
#   5. Scheduled task "NetraAttend Backup": MongoDB backup every night at 23:30
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv-service\Scripts\python.exe"
$port = 5000

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "Run this as Administrator (use deploy\INSTALL.bat)." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $py)) { Write-Host "Service Python not found: $py`nRun setup.bat first." -ForegroundColor Red; exit 1 }
if (-not (Test-Path (Join-Path $root "config.py"))) { Write-Host "config.py missing - copy config.example.py to config.py and fill it in." -ForegroundColor Red; exit 1 }

Step "1/6 Stopping other running copies of NetraAttend"
Get-ScheduledTask -TaskName "NetraAttend" -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -like "*app.py*" -and $_.CommandLine -like "*NetraAttend*") -or
    ($_.Name -eq "cmd.exe" -and $_.CommandLine -like "*netra_service.bat*") }
foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host "   stopped $($p.Name) $($p.ProcessId)" } catch {} }
Start-Sleep -Seconds 2

Step "2/6 Firewall: office network may open port $port"
Get-NetFirewallRule -DisplayName "NetraAttend $port" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "NetraAttend $port" -Direction Inbound -Protocol TCP -LocalPort $port `
    -Action Allow -RemoteAddress LocalSubnet -Profile Any | Out-Null
Write-Host "   rule 'NetraAttend $port' added (local subnet only, not the internet)"

Step "3/6 Power: never sleep while plugged in"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
Write-Host "   sleep and hibernate on AC power: never"

Step "4/6 Service task 'NetraAttend' (starts at boot, no login needed)"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$root\netra_service.bat`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"   # give the network 30 s after boot
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "NetraAttend" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "NetraAttend face attendance (website, API, camera)" -Force | Out-Null
Write-Host "   task registered"

Step "5/6 Backup task 'NetraAttend Backup' (every night 23:30)"
$bAction = New-ScheduledTaskAction -Execute $py -Argument "`"$root\backup.py`"" -WorkingDirectory $root
$bTrigger = New-ScheduledTaskTrigger -Daily -At "23:30"
$bSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "NetraAttend Backup" -Action $bAction -Trigger $bTrigger -Settings $bSettings `
    -Principal $principal -Description "NetraAttend nightly MongoDB backup (backups folder)" -Force | Out-Null
Write-Host "   backups go to $root\backups (last 30 kept)"

Step "6/6 Starting NetraAttend"
Start-ScheduledTask -TaskName "NetraAttend"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try { if ((Invoke-WebRequest "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $ok = $true; break } } catch {}
}
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
Write-Host ""
if ($ok) {
    Write-Host "NetraAttend is running as a service." -ForegroundColor Green
    Write-Host "   This PC:        http://localhost:$port"
    Write-Host "   Office network: http://${ip}:$port"
    Write-Host "   API docs:       http://${ip}:$port/api/docs"
    Write-Host "   Log file:       $root\logs\netraattend.log"
    Write-Host "It starts by itself after every restart / power cut. If the camera was in use by another"
    Write-Host "copy, it reconnects by itself within about 2 minutes."
} else {
    Write-Host "NetraAttend did not answer within 2 minutes. Last log lines:" -ForegroundColor Yellow
    $log = Join-Path $root "logs\netraattend.log"
    if (Test-Path $log) { Get-Content $log -Tail 25 }
}
Write-Host ""
Write-Host "Also do once: BIOS -> 'Restore on AC Power Loss' = Power On, so the PC turns itself on after a power cut."
