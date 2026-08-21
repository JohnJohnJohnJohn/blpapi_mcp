# run.ps1 — start the gateway from the project virtual environment.
#
# Usage:
#   .\scripts\run.ps1                     # native backend, default config
#   .\scripts\run.ps1 -Backend fake       # deterministic backend (no Terminal)
#   .\scripts\run.ps1 -Config x.yaml -Policy y.yaml
#   .\scripts\run.ps1 -Port 8766          # use another HTTP port

param(
    [ValidateSet("native", "fake")]
    [string]$Backend = "",
    [string]$Config = "",
    [string]$Policy = "",
    [Alias("Host")]
    [string]$ListenHost = "",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment missing. Run scripts\install.ps1 first."
    exit 1
}

# Expose the exact deployed commit via the /version endpoint (best effort).
$env:BLOOMBERG_MCP_COMMIT = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    $commit = git -C $RepoRoot rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $commit) {
        $env:BLOOMBERG_MCP_COMMIT = $commit.Trim()
    }
}

# Secrets are read from the environment / credential manager — never passed
# on the command line (SPEC §4.13).
$arguments = @("-m", "bloomberg_mcp.main")
if ($Backend) { $arguments += @("--backend", $Backend) }
if ($Config) { $arguments += @("--config", $Config) }
if ($Policy) { $arguments += @("--policy", $Policy) }
if ($ListenHost) { $arguments += @("--host", $ListenHost) }
if ($Port -gt 0) { $arguments += @("--port", $Port) }

Set-Location $RepoRoot
& $Python @arguments
exit $LASTEXITCODE
