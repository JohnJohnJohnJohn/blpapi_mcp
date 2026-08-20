# stop.ps1 — stop the running Bloomberg MCP Gateway.
#
# Handles every launch mode:
#   1. scheduled task (registered by register-task.ps1)
#   2. foreground/interactive python process (also Ctrl+C in that terminal)
#
# The gateway writes atomically and keeps no state that requires a graceful
# stop, so terminating the process is always safe.

param(
    [string]$TaskName = "BloombergMCP-Gateway",
    [int]$WaitSeconds = 10
)

$ErrorActionPreference = "Stop"

function Find-GatewayProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "bloomberg_mcp\.main" }
}

# --- 1. Scheduled task ------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and $task.State -eq "Running") {
    Write-Host "Stopping scheduled task '$TaskName'..."
    Stop-ScheduledTask -TaskName $TaskName
    $waited = 0
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running" -and $waited -lt $WaitSeconds) {
        Start-Sleep -Seconds 1
        $waited++
    }
    if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Running") {
        Write-Host "Scheduled task stopped."
    } else {
        Write-Warning "Task still running after $WaitSeconds s; falling back to process stop."
    }
}

# --- 2. Gateway processes ---------------------------------------------------
$procs = Find-GatewayProcesses
if (-not $procs) {
    Write-Host "No running gateway process found."
    exit 0
}

foreach ($proc in $procs) {
    Write-Host "Stopping gateway process PID $($proc.ProcessId)..."
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1
$remaining = Find-GatewayProcesses
if ($remaining) {
    Write-Warning "Gateway still running: PIDs $($remaining.ProcessId -join ', ')"
    exit 1
}
Write-Host "Gateway stopped."
exit 0
