# environment-probe.ps1 — Milestone 0 environment probe (SPEC §5.2).
#
# Records exact Python / blpapi / MCP SDK versions, probes the local Desktop
# API endpoint and dumps service/operation/schema introspection. Bloomberg
# connectivity steps require a logged-on Terminal; they are reported as
# PENDING otherwise. Outputs land in probe-output/ (git-ignored).

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$OutDir = Join-Path $RepoRoot "probe-output"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

Write-Host "== Python =="
& $Python --version

$probeScript = @'
import importlib, json, platform, sys

report = {
    "python_version": platform.python_version(),
    "platform": platform.platform(),
    "blpapi_version": None,
    "mcp_sdk_version": None,
    "mcp_protocol_versions": None,
    "session_probe": "not-run",
}

try:
    import blpapi
    report["blpapi_version"] = blpapi.__version__
except Exception as exc:
    report["blpapi_import_error"] = str(exc)

try:
    from importlib.metadata import version
    report["mcp_sdk_version"] = version("mcp")
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS, HANDSHAKE_PROTOCOL_VERSIONS
    report["mcp_protocol_versions"] = {
        "modern": list(MODERN_PROTOCOL_VERSIONS),
        "handshake": list(HANDSHAKE_PROTOCOL_VERSIONS),
    }
except Exception as exc:
    report["mcp_probe_error"] = str(exc)

# Bloomberg Desktop API connection proof (requires logged-on Terminal).
try:
    import blpapi
    options = blpapi.SessionOptions()
    options.setServerHost("127.0.0.1")
    options.setServerPort(8194)
    options.setConnectTimeout(5000)
    session = blpapi.Session(options)
    if session.start():
        report["session_probe"] = "connected"
        opened = []
        for service_name in ("//blp/refdata", "//blp/mktdata", "//blp/instruments", "//blp/apiflds"):
            if session.openService(service_name):
                opened.append(service_name)
                service = session.getService(service_name)
                dump = {
                    "service": service_name,
                    "operations": [],
                }
                for op in service.operations():
                    operation = {"name": str(op.name()), "description": op.description()}
                    request_definition = op.requestDefinition()
                    if request_definition is not None:
                        operation["request_schema"] = request_definition.toString()
                    responses = [
                        op.getResponseDefinitionAt(i).toString()
                        for i in range(op.numResponseDefinitions())
                    ]
                    operation["response_schemas"] = responses
                    dump["operations"].append(operation)
                with open("probe-output/schema-" + service_name.replace("/", "_") + ".json", "w") as fh:
                    json.dump(dump, fh, indent=2)
        report["opened_services"] = opened
        session.stop()
    else:
        report["session_probe"] = "failed (Terminal not running/logged on, or BBComm down)"
except Exception as exc:
    report["session_probe"] = f"error: {exc}"

with open("probe-output/environment-probe.json", "w") as fh:
    json.dump(report, fh, indent=2)
print(json.dumps(report, indent=2))
'@

Push-Location $RepoRoot
try {
    & $Python -c $probeScript
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Probe artifacts written to $OutDir"
Write-Host "NOTE: entitlements and installed-version operation limits must be verified"
Write-Host "      manually on the workstation via WAPI <GO> (SPEC §1.8, §5.2)."
