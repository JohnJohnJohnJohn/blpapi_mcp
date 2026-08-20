# configure-tailscale.ps1 — configure Tailscale Serve as a private HTTPS
# proxy to the localhost-bound gateway (SPEC §4.12).
#
# Exposes the gateway to the tailnet only via Tailscale Serve (never Funnel).
# The backend port defaults to BLOOMBERG_MCP_PORT from the environment or the
# repo .env file (falling back to 8765). Verifies Funnel is not enabled.

param(
    [int]$BackendPort = 0,
    [int]$HttpsPort = 443,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

function Get-ConfiguredPort {
    if ($env:BLOOMBERG_MCP_PORT) { return [int]$env:BLOOMBERG_MCP_PORT }
    $envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*BLOOMBERG_MCP_PORT\s*=\s*(\d+)') { return [int]$Matches[1] }
        }
    }
    return 8765
}

if ($BackendPort -le 0) {
    $BackendPort = Get-ConfiguredPort
}

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    Write-Error "tailscale CLI not found on PATH."
    exit 1
}

function Run([string[]]$CmdArgs) {
    if ($WhatIf) {
        Write-Host "WOULD RUN: tailscale $($CmdArgs -join ' ')"
    } else {
        & tailscale @CmdArgs
        if ($LASTEXITCODE -ne 0) { throw "tailscale $($CmdArgs -join ' ') failed ($LASTEXITCODE)" }
    }
}

Write-Host "== Tailscale status =="
& tailscale status | Select-Object -First 5

# HTTPS certificates are managed by the tailnet admin console. Some builds
# also expose `tailscale set --https`; try it best-effort but do not fail if
# this build has no such flag — `tailscale serve --https` provisions certs.
Write-Host ""
Write-Host "Enabling HTTPS (best-effort)..."
try { Run @("set", "--https=true") } catch {
    Write-Host "  (skipped: this tailscale build has no 'set --https' flag; serve will handle certs)"
}

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
