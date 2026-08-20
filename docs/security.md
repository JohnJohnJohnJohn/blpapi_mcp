# Security

Controls implementing SPEC §1.7, §3.1, §4.11.

## Network posture

- Gateway binds `127.0.0.1:8765` only; BBComm `127.0.0.1:8194` is never
  proxied, tunneled or exposed.
- Tailnet access is via Tailscale Serve (private HTTPS). Funnel is checked
  and must remain off (SPEC §4.12).
- Host/Origin validation via the SDK transport security settings: disallowed
  `Origin` → HTTP 403; invalid `Host` → 421. Configure
  `server.allowed_hosts`/`allowed_origins` for the tailnet name.

## Authentication (SPEC §1.7)

- Profile `private-static-bearer`; **not** OAuth 2.1 compliant.
- Bearer tokens accepted only from the `Authorization` header; query strings,
  paths, cookies and bodies are never consulted.
- ≥ 256-bit entropy enforced at startup; constant-time comparison over
  SHA-256 digests; bounded rotation overlap via a second token.
- Every request authenticated independently; failures are rate-limited and
  audit-recorded; tokens never appear in logs or command lines.
- Tokens map to server-owned principals and scope sets. Application handles
  (request ids, subscription ids) are never treated as authentication.

## Protocol hardening (SPEC §3.1)

- `MCP-Protocol-Version` required and pinned to `2026-07-28`; unsupported or
  missing revisions rejected with SDK protocol error shapes (HTTP 400).
- `Mcp-Method`/`Mcp-Name` header/body consistency validated by the SDK;
  duplicated routing headers rejected.
- Request body size limited (`server.max_request_body_bytes`); POST only on
  `/mcp`.
- Endpoint exposure policy: `/health/live` public (boolean liveness only —
  no connectivity, identity, version or queue details); `/health/ready`,
  `/version`, `/mcp`, `/artifacts/*` bearer-authenticated; `/metrics`
  admin-scope and localhost-only.

## Authorization and isolation

- Discovery vs execution policy per service/operation; unknown operations
  denied by default; authorization and provider operation families are never
  exposed (SPEC §1.3 Class C).
- All handles (requests, results, subscriptions, cursors) are
  cryptographically random (`secrets.token_urlsafe`) and principal-bound;
  cross-principal access returns the same response as absence.
- Unknown input elements rejected by default; nesting depth, array size,
  securities/fields/observations limits enforced before Bloomberg submission.
- Result artifacts: server-generated IDs, path-traversal-safe storage,
  restrictive permissions, TTL expiry; local paths never returned.
- Bloomberg response text is treated as untrusted: normalized description /
  name / headline fields are flagged via `untrusted_text_fields`, and the
  gateway never interprets response text as instructions.
- Internal errors return a generic external message; details stay in local
  logs (SPEC §4.11).

## What is deliberately absent (SPEC §1.4)

No provider/publishing functionality, no caller-selected identities or
authorization requests, no caller-controlled session options/BBComm
settings, no arbitrary filesystem paths, no shell/Excel/Terminal UI
automation, no reflection of arbitrary `blpapi` methods.

## Secrets handling

- Tokens live in the process environment or the Windows Credential Manager;
  the scheduled task never carries secrets on the command line.
- `.env`, token files and `probe-output/` are git-ignored.
