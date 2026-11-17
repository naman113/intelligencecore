<#
  local.ps1 — agentctl for the Windows/Docker Desktop stack.

  Usage:  .\deploy\local.ps1 <command>
    up          build + start everything (gateway, connector, postgres, falkordb, langfuse, warehouse);
                adds SETTINGS_ENCRYPTION_KEY to .env when missing or empty (never overwrites or prints it)
    down        stop the stack (volumes, and therefore the WhatsApp pairing, survive)
    restart     restart the two app services only
    status      container table + gateway health + WhatsApp link state
    qr          open the pairing QR in a browser (only needed once, or after a logout)
    mobile      publish the UI to your Tailscale network so a phone can reach it
    logs        follow logs;   .\deploy\local.ps1 logs whatsapp
    selftest    probe every configured connection from inside the app container
    nuke        stop AND delete volumes — destroys the WhatsApp pairing and all memory
#>

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Compose = @('compose', '-f', "$Root\docker-compose.yml", '-f', "$Root\docker-compose.local.yml")

function Get-EnvValue($name) {
    $line = Select-String -Path "$Root\.env" -Pattern "^$name=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $line) { return '' }
    return ($line.Line -split '=', 2)[1].Trim()
}

# Phase 14 (WH-02): add a generated secret to .env if missing; never overwrite, never print it.
function Ensure-EnvSecret($name) {
    $path = "$Root\.env"
    if (-not (Test-Path $path)) { New-Item -ItemType File -Path $path | Out-Null }
    if (-not [string]::IsNullOrEmpty((Get-EnvValue $name))) { return }
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $hex = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    $lines = @(Get-Content -Path $path -ErrorAction SilentlyContinue)
    $found = $false
    $out = foreach ($l in $lines) {
        if ($l -eq "$name=") { $found = $true; "$name=$hex" } else { $l }
    }
    if (-not $found) { $out = @($out) + "$name=$hex" }
    Set-Content -Path $path -Value $out -Encoding ASCII
}

function Assert-Docker {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker not found. Install Docker Desktop, then re-run."
    }
    docker info --format '{{.ServerVersion}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Docker daemon not responding. Start Docker Desktop and wait for the whale to settle." }
}

function Invoke-Compose { docker @Compose @args }

switch ($args[0]) {

    'up' {
        Assert-Docker
        foreach ($k in 'WAREHOUSE_ADMIN_PASSWORD','WH_OWNER_PASSWORD','WH_LOADER_PASSWORD','WH_READER_PASSWORD') { Ensure-EnvSecret $k }  # Phase 14 (WH-02)
        # Quick 029 F9: the key that encrypts secret settings. Added only when missing or empty.
        & "$PSScriptRoot\ensure-settings-key.ps1" "$Root\.env"
        if ($LASTEXITCODE -ne 0) { throw "could not provision SETTINGS_ENCRYPTION_KEY (see above)" }
        # The sandbox image runs on the HOST daemon (the gateway shells out to
        # `docker run`), so it is built here rather than by compose. Built on
        # every up: the layer cache makes an unchanged Dockerfile a no-op, and a
        # changed one (Quick 004 added statsmodels and scikit-learn) reaches
        # installs that already had an image.
        Write-Host "building sandbox image (cached when unchanged)..." -ForegroundColor Cyan
        docker build -q -t agentic-sandbox:latest "$Root\sandbox" | Out-Null
        Invoke-Compose up -d --build
        Write-Host ""
        Write-Host "Second Brain : http://localhost:8090" -ForegroundColor Green
        Write-Host "Ops key      : $(Get-EnvValue 'OPS_KEY')" -ForegroundColor Green
        Write-Host "Old dashboard: http://localhost:8080/dashboard" -ForegroundColor DarkGray
        Write-Host "Langfuse     : http://localhost:3001" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "If WhatsApp is not paired yet:  .\deploy\local.ps1 qr" -ForegroundColor Yellow
    }

    'down'    { Assert-Docker; Invoke-Compose down }
    'restart' { Assert-Docker; Invoke-Compose restart app whatsapp }
    'logs'    {
        Assert-Docker
        if ($args.Count -gt 1) { Invoke-Compose logs -f --tail 200 $args[1] }
        else { Invoke-Compose logs -f --tail 200 }
    }

    'status' {
        Assert-Docker
        Invoke-Compose ps
        Write-Host ""
        try {
            $h = Invoke-RestMethod -Uri 'http://localhost:8080/health' -TimeoutSec 5
            if ($h.ok) { Write-Host "* gateway     : ok" -ForegroundColor Green }
            else { Write-Host "* gateway     : DEGRADED - $($h.reason)" -ForegroundColor Yellow }
        } catch { Write-Host "* gateway     : NOT RESPONDING" -ForegroundColor Red }
        try {
            $w = Invoke-RestMethod -Uri 'http://localhost:3010/api/status' -TimeoutSec 5
            if ($w.connected) {
                Write-Host "* whatsapp    : PAIRED as $($w.user.id)" -ForegroundColor Green
            } else {
                Write-Host "* whatsapp    : running but NOT paired - run: .\deploy\local.ps1 qr" -ForegroundColor Yellow
            }
        } catch { Write-Host "* whatsapp    : NOT RESPONDING" -ForegroundColor Red }
    }

    'qr' {
        $token = Get-EnvValue 'WA_QR_TOKEN'
        if ([string]::IsNullOrWhiteSpace($token)) { $token = 'whatsapp-qr-secret' }
        $url = "http://localhost:3010/api/qr?token=$token"
        Write-Host "Opening $url"
        Write-Host "On your phone: WhatsApp > Settings > Linked devices > Link a device"
        Start-Process $url
    }

    'mobile' {
        # Tailscale proxies from the tailnet to 127.0.0.1:8090 on this machine,
        # so the container keeps its loopback-only binding: nothing on the local
        # wifi can reach it, and nothing is exposed to the internet. The ops key
        # is an admin credential, so it should never ride over plain HTTP.
        $ts = Get-Command tailscale -ErrorAction SilentlyContinue
        if ($null -eq $ts) { $ts = 'C:\Program Files\Tailscale\tailscale.exe' } else { $ts = $ts.Source }
        if (-not (Test-Path $ts)) {
            Write-Host "Tailscale is not installed." -ForegroundColor Red
            Write-Host "  winget install Tailscale.Tailscale" -ForegroundColor Yellow
            Write-Host "Then sign in, and install Tailscale on your phone with the same account."
            break
        }

        $status = & $ts status --json 2>$null | ConvertFrom-Json
        if ($null -eq $status -or $status.BackendState -ne 'Running') {
            Write-Host "Tailscale is installed but not signed in. Run: tailscale up" -ForegroundColor Yellow
            break
        }

        & $ts serve --bg --https 443 http://127.0.0.1:8090
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "serve failed. The usual cause is HTTPS certificates being off for" -ForegroundColor Yellow
            Write-Host "the tailnet. Enable them at https://login.tailscale.com/admin/dns" -ForegroundColor Yellow
            break
        }

        $host_ = $status.Self.DNSName.TrimEnd('.')
        Write-Host ""
        Write-Host "On your phone : https://$host_" -ForegroundColor Green
        Write-Host "Ops key       : $(Get-EnvValue 'OPS_KEY')" -ForegroundColor Green
        Write-Host ""
        Write-Host "Install Tailscale on the phone and sign in with the same account first."
        Write-Host "Then use Share / Add to Home Screen so it opens without browser chrome."
        Write-Host "To stop publishing:  tailscale serve --https 443 off" -ForegroundColor DarkGray
    }

    'selftest' { Assert-Docker; docker exec -it agentic-app python -m app.selftest }

    'nuke' {
        Assert-Docker
        Write-Host "This deletes the WhatsApp pairing, the knowledge graph, and all working memory." -ForegroundColor Red
        $c = Read-Host "Type DELETE to confirm"
        if ($c -ceq 'DELETE') { Invoke-Compose down -v } else { "aborted" }
    }

    default {
        Write-Host "usage: .\deploy\local.ps1 up|down|restart|status|logs [service]|qr|mobile|selftest|nuke"
    }
}
