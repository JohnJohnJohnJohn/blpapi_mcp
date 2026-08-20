# run.ps1 — start the gateway from the project virtual environment.
#
# Usage:
#   .\scripts\run.ps1                     # native backend, default config
#   .\scripts\run.ps1 -Backend fake       # deterministic backend (no Terminal)
#   .\scripts\run.ps1 -Config x.yaml -Policy y.yaml

param(
    [ValidateSet("native", "fake")]
    [string]$Backend = "",
    [string]$Config = "",
    [string]$Policy = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment missing. Run scripts\install.ps1 first."
    exit 1
}

# Secrets are read from the environment / credential manager — never passed
# on the command line (SPEC §4.13).
$arguments = @("-m", "bloomberg_mcp.main")
if ($Backend) { $arguments += @("--backend", $Backend) }
if ($Config) { $arguments += @("--config", $Config) }
if ($Policy) { $arguments += @("--policy", $Policy) }

Set-Location $RepoRoot
& $Python @arguments
exit $LASTEXITCODE
