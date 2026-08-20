# health-check.ps1 — liveness (unauthenticated) and readiness (authenticated).

param(
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [string]$Token = $env:BLOOMBERG_MCP_BEARER_TOKEN
)

$ErrorActionPreference = "Stop"

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
