# Architecture

Implementation map for SPEC.md v1.1. The gateway is a single Windows process
that binds `127.0.0.1:8765`; Tailscale Serve terminates tailnet-only HTTPS in
front of it; BBComm (`127.0.0.1:8194`) is never exposed.

```
Hermes (Linux, MCP 2026-07-28 client)
   │  HTTPS over Tailscale (Serve, never Funnel)
   ▼
Tailscale Serve ──► 127.0.0.1:8765
   │
   ▼
Bloomberg MCP Gateway (this repository)
   ├─ auth/           bearer verification, principals, ASGI middleware
   ├─ policy/         discovery vs execution policy, cost model, quotas
   ├─ mcp/            stateless MCP tools/resources over Streamable HTTP
   ├─ registry/       request records, idempotency, subscriptions, cursors
   ├─ normalization/  versioned tabular normalizers
   ├─ storage/        bounded memory/file result artifacts + cleanup
   ├─ observability/  audit, metrics, usage, health
   └─ blp/            the ONLY package allowed to import blpapi
```

## Native object boundary (SPEC §2.4)

`blpapi` objects (`Event`, `Message`, `Element`, `Request`, `Service`,
`Operation`, schema definitions, `CorrelationId`) never cross the `blp/`
boundary. The adapter decodes events into immutable canonical models
(`models.py`) before handing them to the async layer:

- `blp/event_decoder.py` — native event → `CanonicalMessage` (JSON-safe).
- `blp/schema_registry.py` — native schema → `ElementDescriptor` /
  `OperationDescriptor` with deterministic hashing.
- `blp/request_builder.py` — canonical parameters → native `Request`.
- Correlation IDs are gateway-allocated integers (`native_token`); native
  `CorrelationId` objects stay inside the backend.

## Request execution (SPEC §2.5)

Each request gets a dedicated `blpapi.EventQueue`. A bounded worker thread
(`blp/request_executor.py`) reads events until the final RESPONSE, a
REQUEST_STATUS failure, cancellation, deadline, or session loss, decoding
every event before it leaves the thread. The shared asynchronous event
handler (`blp/subscription_dispatcher.py`) is reserved for subscription,
subscription-status, session-status, service-status and admin events.

## Session lifecycle (SPEC §2.6)

`blp/session_manager.py` owns one long-lived consumer session on
`127.0.0.1:8194` with exponential backoff + jitter reconnection. Every new
connection increments `session_generation`; schema caches are invalidated on
generation change; in-flight requests fail with `BLOOMBERG_SESSION_LOST`
(`retryable: true`) and are never replayed automatically. Subscriptions are
restored after reconnect only when `subscriptions.restore_after_reconnect`
is set, with a new generation and a data-gap warning.

## Canonical request pipeline (SPEC §2.8, §4.3)

```
MCP input-schema validation (jsonschema, additionalProperties=false)
→ authentication + principal scope
→ service/operation policy (discover vs execute, required_scope)
→ Bloomberg schema validation (blp/schema_converter.validate_parameters)
→ cost estimation + policy limits (policy/cost.py, PolicyLimits)
→ governance budgets + rate limits (policy/quota.py)
→ idempotency dedup (registry/requests_registry.py)
→ concurrency/queue admission (registry/requests.py)
→ schema-hash recheck at submission (SCHEMA_DRIFT_DETECTED)
→ native submission (blp/native_backend.py)
```

## Protocol posture (SPEC §1.6, §3.1)

The gateway targets MCP revision `2026-07-28` only, via the official MCP
Python SDK's stateless single-exchange path: each request is a self-contained
POST carrying the `_meta` envelope (`protocolVersion`, `clientCapabilities`).
No `initialize` handshake, no `Mcp-Session-Id`, no GET/DELETE on `/mcp`, no
legacy HTTP+SSE. The SDK validates `MCP-Protocol-Version`, `Mcp-Method` and
`Mcp-Name` header/body consistency and rejects unsupported versions with the
protocol-defined error shapes; a pre-gate (`mcp/server.ProtocolGateMiddleware`)
requires the configured modern revision on every `/mcp` request so legacy
requests never reach a handler. Host/Origin validation uses the SDK transport
security settings (invalid `Origin` → HTTP 403).

## Native API notes

Exact `blpapi` (3.26.7.1) methods used by the adapter (introspected from the
installed package, SPEC §5.1): `Session(options, eventHandler)`,
`Session.start/stop/openService/getService/sendRequest/cancel/subscribe/
resubscribe/unsubscribe`, `SessionOptions.setServerHost/setServerPort/
setConnectTimeout`, `EventQueue.nextEvent`, `Service.operations/
createRequest`, `Operation.requestDefinition/getResponseDefinitionAt/
numResponseDefinitions`, `SchemaElementDefinition.name/alternateNames/
minValues/maxValues/typeDefinition/status`, `SchemaTypeDefinition.datatype/
isComplexType/isEnumerationType/numElementDefinitions/getElementDefinition/
enumeration`, `ConstantList.numConstants/getConstantAt`, `SubscriptionList.add`,
`CorrelationId(value=...)`, `Name(nameString)/Name.findName`. A reference stub
of this surface lives in `stubs/blpapi.pyi`.
