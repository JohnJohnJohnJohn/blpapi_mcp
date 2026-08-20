# health-check.ps1 — liveness (unauthenticated) and readiness (authenticated).
#
# The port defaults to BLOOMBERG_MCP_PORT from the environment or the repo
# .env file (falling back to 8765), so it matches however run.ps1 starts.

param(
    [string]$BaseUrl = "",
    [string]$Token = $env:BLOOMBERG_MCP_BEARER_TOKEN
)

$ErrorActionPreference = "Stop"

function Read-DotEnv([string]$Key) {
    $existing = [Environment]::GetEnvironmentVariable($Key)
    if ($existing) { return $existing }
    $envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match "^\s*$Key\s*=\s*(.+)$") { return $Matches[1].Trim() }
        }
    }
    return $null
}

function Get-ConfiguredPort {
    $fromEnv = Read-DotEnv "BLOOMBERG_MCP_PORT"
    if ($fromEnv) { return [int]$fromEnv }
    return 8765
}

if (-not $BaseUrl) {
    # Default to the machine name: it resolves to the node's addresses
    # (including the Tailscale interface), so it works whether the gateway
    # binds to loopback, 0.0.0.0 or the Tailscale IP only.
    $name = Read-DotEnv "BLOOMBERG_MCP_PUBLIC_HOST"
    if (-not $name) { $name = $env:COMPUTERNAME.ToLower() }
    $BaseUrl = "http://${name}:$(Get-ConfiguredPort)"
}
Write-Host "target: $BaseUrl"

if (-not $Token) {
    $Token = Read-DotEnv "BLOOMBERG_MCP_BEARER_TOKEN"
}

$live = Invoke-RestMethod -Uri "$BaseUrl/health/live" -Method Get
Write-Host "live:   $($live.status)"

if (-not $Token) {
    Write-Warning "BLOOMBERG_MCP_BEARER_TOKEN not set; skipping authenticated readiness check."
    exit 0
}

$ready = Invoke-RestMethod -Uri "$BaseUrl/health/ready" -Method Get `
    -Headers @{ Authorization = "Bearer $Token" }
$ready | ConvertTo-Json -Depth 5
if ($ready.bloomberg_session -ne "CONNECTED") {
    Write-Warning "Bloomberg session not connected; request admission is REJECTING."
    exit 2
}
exit 0
