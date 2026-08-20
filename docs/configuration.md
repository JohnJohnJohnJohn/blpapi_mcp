# Configuration

Two YAML files drive the gateway:

| File | Purpose |
|---|---|
| `config/default.yaml` | Server, backend, quotas, storage, auth, governance, audit, logging |
| `config/policy.example.yaml` | Principals/scopes, per-service/operation discovery & execution policy, limits |

Paths may be overridden with `--config` / `--policy` or the
`BLOOMBERG_MCP_CONFIG` / `BLOOMBERG_MCP_POLICY` environment variables.
The HTTP port may be overridden with `BLOOMBERG_MCP_PORT` (or `--port`,
or `scripts\run.ps1 -Port`) — useful when another workstation application
occupies the 8765 default.
String values support `%ENVVAR%` and `${ENVVAR}` expansion (e.g. the default
artifact directory `%LOCALAPPDATA%\BloombergMCP\data`).

At startup the gateway also loads a `.env` file from the working directory
(repo root) if present: `KEY=VALUE` lines, `#` comments, optional quotes.
Already-set environment variables always take precedence. `.env` is
git-ignored — copy `.env.example` to `.env` and fill in your token.

## Backend selection

`backend: native | fake` (or `--backend`, or `BLOOMBERG_MCP_BACKEND`).
`fake` runs the deterministic in-memory backend for development/CI without a
Terminal; `native` requires a running, logged-on Bloomberg Terminal.

## Hard constraints enforced at load time (SPEC §1.5, §1.6)

- `bloomberg.host/port` must remain `127.0.0.1:8194`.
- `bloomberg.automatic_request_replay` must be `false`.
- `server.stateless` must be `true`.
- `auth.profile` must be `private-static-bearer`.

## Auth (SPEC §1.7)

`auth.token_source`:

- `env` — tokens from `BLOOMBERG_MCP_BEARER_TOKEN` (current) and
  `BLOOMBERG_MCP_BEARER_TOKEN_PREVIOUS` (bounded rotation overlap).
- `file` — token read from `auth.token_file`.
- `windows_credential_manager` — generic credential named by
  `auth.credential_target` (default `BloombergMCP/bearer`), with env fallback.

Tokens require ≥ 256 bits of entropy. Generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Tokens map to the first principal defined in the policy file; scopes come
from that principal's entry. Tokens are compared in constant time (SHA-256 +
`hmac.compare_digest`) and never logged.

## Policy (SPEC §4.2)

- `principals.<name>.scopes` — capability scopes (`bloomberg:discover`,
  `bloomberg:reference`, `bloomberg:historical`, `bloomberg:intraday`,
  `bloomberg:generic-request`, `bloomberg:subscribe`, `bloomberg:result-read`).
- `services.<name>` — `open`, `discover`, `subscriptions` flags plus an
  `operations` map. The `"*"` entry is the wildcard; explicit entries grant
  `execute: true` and may require `required_scope`.
- `defaults` — deny-unconfigured behavior and unknown-element rejection.
- `limits` — max securities/fields/observations/array elements/nesting depth.

Discovery and execution are distinct: a wildcard may allow schema inspection
while only named operations are executable.

## Governance (SPEC §1.8)

Daily/monthly request budgets per principal (`LICENSE_BUDGET_EXCEEDED` on
exhaustion), an entitlement circuit breaker that opens after
`entitlement_failure_circuit_threshold` consecutive entitlement failures and
closes on operator intervention (`reset_entitlement_circuit`) or a successful
entitled exchange, and persistence toggles for usage counters and artifacts.
Policy limits do not replace Bloomberg contractual or entitlement limits.

## Storage (SPEC §4.7)

Results under the inline cap stay in memory; larger results are written as
JSONL artifacts under `storage.directory` with random server-generated IDs,
atomic writes, checksums, ownership metadata and TTL expiry (default
`requests.result_ttl_seconds` = 24 h). Artifacts are transport artifacts, not
a data warehouse. The optional artifact HTTP endpoint is disabled by default
(`server.artifact_endpoint_enabled`).

## Logging and audit (SPEC §4.8)

`logging.level/json` control application diagnostics. Audit records include
principal, tool/service/operation, request id, hashed client-request id,
counts, cost, duration, outcome and result id. Security names, field names
and parameters are redacted unless explicitly enabled in `audit.*`.
