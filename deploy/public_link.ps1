# NetraAttend - public https link through a Cloudflare quick tunnel (no account needed).
# Double-click deploy\PUBLIC_LINK.bat: shows the current link, or starts a new tunnel.
# The link changes every time a new tunnel starts; the current one is kept in logs\public_link.txt.
# -Service: used by the "NetraAttend Tunnel" scheduled task at boot (keeps running).
param([switch]$Service)
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "tools\cloudflared\cloudflared.exe"
$log = Join-Path $root "logs\tunnel.log"
$linkFile = Join-Path $root "logs\public_link.txt"
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
if (-not (Test-Path $exe)) { Write-Host "cloudflared.exe missing in tools\cloudflared"; exit 1 }

function Alive($url) {
    try { return (Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 8).StatusCode -eq 200 } catch { return $false }
}

if (-not $Service -and (Test-Path $linkFile)) {
    $current = (Get-Content $linkFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($current -and (Get-Process cloudflared -ErrorAction SilentlyContinue) -and (Alive $current)) {
        Write-Host ""; Write-Host "Public link (already running):" -ForegroundColor Green; Write-Host "   $current"
        Write-Host "   API docs: $current/api/docs"
        try { Set-Clipboard -Value $current; Write-Host "   (copied)" } catch {}
        exit 0
    }
}

function StartTunnel {
    Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch {} }
    if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
    $p = Start-Process -FilePath $exe -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://localhost:5000" `
        -WindowStyle Hidden -RedirectStandardError $log -RedirectStandardOutput "$log.out" -PassThru
    $url = $null
    for ($i = 0; $i -lt 45 -and -not $url -and -not $p.HasExited; $i++) {
        Start-Sleep -Seconds 2
        $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value }
    }
    if (-not $url) {
        Write-Host "Tunnel did not start - check the internet connection and $log" -ForegroundColor Red
        if (-not $p.HasExited) { try { $p.Kill() } catch {} }
        return $null
    }
    Set-Content -Path $linkFile -Value $url -Encoding utf8
    Write-Host ""; Write-Host "Public link (new):" -ForegroundColor Green; Write-Host "   $url"
    Write-Host "   API docs: $url/api/docs"
    Write-Host "   Admin pages stay on the office network only."
    try { Set-Clipboard -Value $url; Write-Host "   (copied)" } catch {}
    return $p
}

if (-not $Service) { if (StartTunnel) { exit 0 } else { exit 1 } }

# service mode: keep a tunnel running forever (no internet at boot, tunnel crash -> try again)
while ($true) {
    $p = StartTunnel
    if ($p) { $p.WaitForExit() }
    Start-Sleep -Seconds 30
}
