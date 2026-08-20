# configure-tailscale.ps1 — configure Tailscale Serve as a private HTTPS
# proxy to the localhost-bound gateway (SPEC §4.12).
#
# Keeps the gateway on 127.0.0.1:8765 and exposes it to the tailnet only via
# Tailscale Serve (never Funnel). Verifies Funnel is not enabled on the port.

param(
    [int]$BackendPort = 8765,
    [int]$HttpsPort = 443,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    Write-Error "tailscale CLI not found on PATH."
    exit 1
}

function Run([string[]]$args) {
    if ($WhatIf) {
        Write-Host "WOULD RUN: tailscale $($args -join ' ')"
    } else {
        & tailscale @args
        if ($LASTEXITCODE -ne 0) { throw "tailscale $($args -join ' ') failed ($LASTEXITCODE)" }
    }
}

Write-Host "== Tailscale status =="
& tailscale status | Select-Object -First 5

# Ensure HTTPS certificates are enabled for the tailnet.
Write-Host ""
Write-Host "Ensuring HTTPS is enabled..."
Run @("set", "--https=true")

# Configure Serve: tailnet-only HTTPS -> http://127.0.0.1:$BackendPort
Write-Host ""
Write-Host "Configuring Tailscale Serve (private HTTPS proxy)..."
Run @("serve", "--https=$HttpsPort", "--set-path=/", "http://127.0.0.1:$BackendPort")

# Verify Funnel is NOT enabled on this port (SPEC §1.4, §4.12).
Write-Host ""
Write-Host "Checking Funnel status..."
$funnelStatus = & tailscale funnel status 2>&1
Write-Host $funnelStatus
if ($funnelStatus -match "Funnel is on") {
    Write-Warning "Funnel appears to be enabled. The gateway MUST NOT be exposed via Funnel."
    Write-Warning "Review 'tailscale funnel' configuration and disable public exposure."
}

Write-Host ""
Write-Host "Done. The gateway is reachable over the tailnet at:"
Write-Host "  https://<this-node>.<tailnet>.ts.net/"
Write-Host ""
Write-Host "Next steps (SPEC §4.12):"
Write-Host "  - Apply a default-deny ACL grant; allow only the Hermes identity/tag."
Write-Host "  - Use a dedicated tag for this workstation and for Hermes."
Write-Host "  - Verify an unrelated tailnet node cannot connect."
