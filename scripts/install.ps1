# install.ps1 — create the isolated virtual environment and install
# dependencies. Never touches global Python environments.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if ($Uv) {
    Write-Host "Using uv for environment management"
    uv venv --python 3.12
    uv sync --extra dev --extra tabular
} else {
    Write-Host "uv not found; falling back to python -m venv"
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install -e ".[dev]" `
        --extra-index-url https://blpapi.bloomberg.com/repository/releases/python/simple/
}

Write-Host ""
Write-Host "Installed versions:"
& ".venv\Scripts\python.exe" -c "import blpapi, platform; from importlib.metadata import version; print('python', platform.python_version()); print('blpapi', blpapi.__version__); print('mcp', version('mcp'))"
Write-Host ""
Write-Host "Environment ready. Run the gateway with scripts\run.ps1."
