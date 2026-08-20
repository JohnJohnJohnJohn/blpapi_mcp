# Troubleshooting

## Startup

| Symptom | Cause / fix |
|---|---|
| `another gateway instance is running` | The named mutex `Local\BloombergMCP.SingleInstance` is held. Find the other process (`Get-Process python*`) and stop it, or let the scheduled task own the lifecycle. |
| `no bearer token configured` | Set `BLOOMBERG_MCP_BEARER_TOKEN` (≥ 32 bytes), point `auth.token_file` at a token file, or create the credential named by `auth.credential_target`. |
| `bearer token must contain at least 256 bits` | Regenerate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `configuration error: ...` | Hard constraints violated (non-local BBComm host, `stateless: false`, replay enabled, unknown auth profile). See `docs/configuration.md`. |
| Gateway starts but `/health/ready` shows `bloomberg_session: DISCONNECTED` | The HTTP process intentionally stays up. Check that the Terminal is running and logged on; see session errors below. |
| `error: cannot bind 127.0.0.1:8765 - the port is already in use` (WinError 10048) | Another application owns the port (on trading workstations, tools like DeskPricer use 8765). Find it with `netstat -ano \| findstr :8765`, then run the gateway on another port: `.\scripts\run.ps1 -Port 8766` (and point `scripts\configure-tailscale.ps1 -BackendPort 8766` / `scripts\health-check.ps1 -BaseUrl http://127.0.0.1:8766` at it too). |

## Bloomberg session

| Symptom | Diagnosis |
|---|---|
| `BLOOMBERG_SESSION_FAILED` at startup | BBComm not reachable or startup rejected. Run `scripts\environment-probe.ps1`; check Bloomberg Desktop API is enabled; check `%LOCALAPPDATA%\BloombergMCP\logs\gateway.log` for the last session-status text. |
| `BLOOMBERG_TERMINAL_NOT_LOGGED_IN` | Terminal process up but no logged-on session. Log on to the Terminal. |
| `BLOOMBERG_SESSION_LOST` (retryable) mid-request | Connection dropped while executing. The gateway reconnects with backoff and reopens services automatically; the failed request is **not** replayed — resubmit with the same `client_request_id` for idempotent retry. |
| `SCHEMA_DRIFT_DETECTED` after reconnect | Schema changed across generations. Re-run discovery (`blpapi_describe_operation`) and retry with the fresh `schema_hash`. |
| Subscription gap after reconnect | Expected when `restore_after_reconnect` is on: restored groups get a new generation and a `SUBSCRIPTION_DATA_GAP` warning; old cursors are invalid. |

## Requests

| Symptom | Diagnosis |
|---|---|
| `AUTH_FORBIDDEN` / missing scope | Principal lacks the scope or the operation's `required_scope`. Adjust the policy file. |
| `INVALID_OPERATION` / `INVALID_SERVICE` | Not configured (`defaults.deny_unconfigured_*`). Add the service/operation to the policy. |
| `UNKNOWN_ELEMENT` | Parameter not in the live Bloomberg schema (and `reject_unknown_elements` is on). Use `blpapi_describe_operation` to inspect the schema. |
| `REQUEST_TOO_LARGE` | Policy limits (securities/fields/observations/array/nesting) exceeded. Split the request. |
| `QUEUE_FULL` | More than `requests.max_queued` waiting. Back off; monitor `blpapi_queue_depth`. |
| `TIMEOUT` / `TIMED_OUT` | Raise `execution.overall_deadline_seconds` (bounded by `requests.maximum_deadline_seconds`) or narrow the request. |
| `NORMALIZER_NOT_AVAILABLE` | `response_mode: normalized` for an unregistered operation. Use `canonical`/`typed` or set `allow_canonical_fallback: true`. |
| Pending handle never completes | Poll `blpapi_get_request`; check the deadline and Bloomberg connectivity. Requests survive client disconnects by design. |
| `BLOOMBERG_NOT_ENTITLED` repeated, circuit open | The entitlement circuit breaker tripped. Verify entitlements on the Terminal (`WAPI <GO>`), then reset via operator intervention or wait for a successful entitled exchange. |
| `LICENSE_BUDGET_EXCEEDED` | Daily/monthly budget exhausted. Adjust `governance.*` budgets if the agreement allows. |

## Transport / Tailscale

| Symptom | Diagnosis |
|---|---|
| HTTP 400 `Unsupported protocol version` | Client sent a legacy revision or no `MCP-Protocol-Version`. Hermes must target `2026-07-28` with the modern per-request envelope. |
| HTTP 400 header mismatch | `Mcp-Method`/`Mcp-Name` disagree with the body; duplicate routing header. Use an SDK client, not hand-rolled HTTP. |
| HTTP 403 on `/mcp` | `Origin` header present but not in `server.allowed_origins` (DNS-rebinding protection). For tailnet names, add the origin or remove the browser-sent Origin. |
| HTTP 421 | `Host` header not in `server.allowed_hosts`. Add the tailnet DNS name/port pattern. |
| 401 everywhere | Wrong/missing bearer token; check `Authorization: Bearer ...`. Repeated failures get rate-limited (429). |
| Connection refused from tailnet | Gateway is localhost-only by design. Verify `tailscale serve` is configured (`scripts\configure-tailscale.ps1`) and the ACL grants the Hermes identity. |

## Where to look

- Application logs: `%LOCALAPPDATA%\BloombergMCP\logs\gateway.log` (JSON).
- Audit events: same log, logger `bloomberg_mcp.audit`.
- Metrics: `GET /metrics` (localhost, admin scope).
- Readiness detail: `GET /health/ready` (authenticated).
