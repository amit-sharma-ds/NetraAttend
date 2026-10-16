# NetraAttend - remove the background service (run as Administrator via deploy\UNINSTALL.bat).
# Data in MongoDB and the backups folder are NOT touched.
$ErrorActionPreference = "Continue"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "Run this as Administrator (use deploy\UNINSTALL.bat)." -ForegroundColor Red; exit 1 }

foreach ($t in "NetraAttend", "NetraAttend Backup") {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Host "removed task '$t'"
    }
}
Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -like "*app.py*" -and $_.CommandLine -like "*NetraAttend*") -or
    ($_.Name -eq "cmd.exe" -and $_.CommandLine -like "*netra_service.bat*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped $($_.Name) $($_.ProcessId)" }
Get-NetFirewallRule -DisplayName "NetraAttend 5000" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "removed firewall rule 'NetraAttend 5000'"
Write-Host "Done. (Power settings were left as they are.)" -ForegroundColor Green
