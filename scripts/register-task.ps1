# register-task.ps1 — register the gateway as a Windows Scheduled Task that
# runs under the interactive Bloomberg Terminal user (SPEC §4.13).
#
# The task:
#   - runs only after the Bloomberg user logs on,
#   - refuses to register under LocalSystem or other Session 0 accounts,
#   - restarts on failure with bounded retry,
#   - uses the repository working directory,
#   - loads secrets from the environment / credential manager (no CLI secrets).

param(
    [Parameter(Mandatory = $true)]
    [string]$User,                 # Bloomberg Terminal user (DOMAIN\user)
    [string]$TaskName = "BloombergMCP-Gateway",
    [string]$Backend = "native"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

# --- refuse privileged / service accounts (SPEC §1.5, §4.13) ---------------
$forbidden = @("NT AUTHORITY\SYSTEM", "NT AUTHORITY\LOCALSERVICE",
               "NT AUTHORITY\NETWORKSERVICE", "LocalSystem", "SYSTEM")
if ($forbidden -contains $User) {
    Write-Error "Refusing to register under '$User'. The gateway must run as the interactive Bloomberg Terminal user, not LocalSystem or a Session 0 account."
    exit 1
}

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Virtual environment missing. Run scripts\install.ps1 first."
    exit 1
}

$logsDir = Join-Path $env:LOCALAPPDATA "BloombergMCP\logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

# The gateway reads BLOOMBERG_MCP_BEARER_TOKEN from the environment or the
# Windows Credential Manager. We do NOT place secrets on the command line.
$arguments = "-m bloomberg_mcp.main --backend $Backend"

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $arguments `
    -WorkingDirectory $RepoRoot

# Run only when the user is logged on (interactive), not whether or not.
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

# Restart on failure: up to 3 attempts, 1 minute apart (bounded retry).
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Trigger: at log on of the Bloomberg user.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.User = $User

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Trigger $trigger `
    -Description "Bloomberg MCP Gateway (stateless MCP 2026-07-28 over Tailscale)" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' for user '$User'."
Write-Host "The task starts at next log on. Start it now with:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
