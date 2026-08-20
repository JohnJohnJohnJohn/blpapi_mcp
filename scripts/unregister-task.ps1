# unregister-task.ps1 — remove the gateway scheduled task.

param(
    [string]$TaskName = "BloombergMCP-Gateway"
)

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Scheduled task '$TaskName' is not registered; nothing to do."
    exit 0
}

# Stop gracefully before removing (SPEC §4.13: graceful stop on logout).
if ($existing.State -eq "Running") {
    Write-Host "Stopping running task..."
    Stop-ScheduledTask -TaskName $TaskName
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
