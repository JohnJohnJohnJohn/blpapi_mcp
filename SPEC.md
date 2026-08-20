# Bloomberg MCP Gateway — Revised Design and Specification

**Version:** 1.1  
**Date:** 2026-08-20  
**Target MCP revision:** `2026-07-28`  
**Audience:** Coding agent and implementation reviewer  
**Deployment:** Windows Bloomberg Terminal workstation serving a Linux Hermes agent over Tailscale

## 1. Scope and requirements

### 1.1 Objective

Build a production-quality MCP gateway on a Windows Bloomberg Terminal workstation. The gateway shall provide a Linux-hosted Hermes agent with controlled access to Bloomberg Desktop API consumer functionality over a private Tailscale network.

The implementation shall:

- Run all Bloomberg `blpapi` interactions on the Windows Bloomberg workstation.
- Connect only to the local Bloomberg Desktop API endpoint.
- Expose a stateless MCP `2026-07-28` Streamable HTTP endpoint.
- Support runtime discovery of Bloomberg services, operations and schemas.
- Execute permitted schema-defined Bloomberg consumer requests.
- Support Bloomberg market-data subscriptions through explicit application handles.
- Provide curated tools for common Bloomberg workflows.
- Preserve Bloomberg partial responses, item errors and entitlement errors.
- Return large results through MCP resource links and bounded resources.
- Enforce authentication, principal ownership, policy, quotas and auditing.
- Support development and CI without Bloomberg through a deterministic fake backend.
- Avoid reflecting arbitrary Python methods or native objects into MCP.
- Remain forward-compatible with newly discovered Bloomberg consumer operations, subject to policy approval.

### 1.2 Requirement language

- `MUST` and `MUST NOT` are mandatory.
- `SHOULD` and `SHOULD NOT` require a documented reason if not followed.
- `MAY` is optional.

### 1.3 Capability classes

“Full BLPAPI coverage” does not mean exposing every public method in the Python package. Capabilities shall be divided into three classes.

#### Class A: Remotely exposed

The gateway MAY expose the following to authorized MCP callers:

- List services known to the gateway.
- Open an explicitly allowlisted consumer service.
- Enumerate operations of an opened service.
- Inspect operation request and response schemas.
- Validate a request without submitting it.
- Execute permitted schema-defined consumer requests.
- Cancel an active request.
- Retrieve request state and results.
- Subscribe, resubscribe and unsubscribe from permitted market-data topics.
- Read bounded subscription updates.
- Access principal-owned result metadata and bounded result pages.
- Execute curated reference, historical, intraday, search and snapshot tools.

#### Class B: Internal only

The following capabilities MAY be used internally but MUST NOT be directly controlled by an MCP caller:

- Session startup, shutdown and reconnection.
- BBComm host and port configuration.
- Service reopening.
- Native correlation-ID creation.
- Native event queues and event dispatching.
- Request templates.
- Bloomberg identities and entitlement checks.
- Session options.
- Native request objects.
- Native event, message and element objects.
- Schema caches and `blpapi.Name` caches.
- Result file paths.
- Subscription restoration after connection loss.

#### Class C: Unsupported

The following MUST NOT be exposed:

- Provider sessions.
- Publishing Bloomberg data.
- Service registration.
- Topic creation for publishing.
- Caller-created or caller-selected Bloomberg identities.
- Caller-generated authorization requests.
- Arbitrary `Session` or `SessionOptions` methods.
- Caller-controlled BBComm host or port.
- Native Python object access.
- Arbitrary Python execution.
- Shell execution.
- Arbitrary filesystem access.
- Caller-selected local output paths.
- Bloomberg Terminal UI automation.
- Generic Excel automation.
- Reflection of arbitrary `blpapi` package methods.

### 1.4 Explicit exclusions

The first production release shall not include:

- Provider functionality.
- Public internet exposure.
- Tailscale Funnel.
- Multi-user Bloomberg identity delegation.
- A database, Redis, Celery or distributed task system.
- Automatic request replay after Bloomberg session loss.
- Universal normalization of arbitrary Bloomberg responses.
- Universal Parquet conversion of nested Bloomberg messages.
- Durable subscription restoration across gateway process restarts.
- OAuth authorization unless required by Hermes integration testing.

### 1.5 Environment constraints

- The native gateway MUST run under the same Windows user context as the interactive Bloomberg Terminal.
- The native gateway MUST use Bloomberg Desktop API on `127.0.0.1:8194`.
- BBComm port `8194` MUST NOT be proxied, tunneled or exposed.
- The HTTP backend MUST bind to `127.0.0.1` by default.
- Tailscale Serve shall provide tailnet-only HTTPS termination.
- The gateway MUST run as a scheduled task under the Bloomberg user for the initial release.
- It MUST NOT initially run as `LocalSystem` or as a Session 0 Windows service.
- A named Windows mutex MUST prevent more than one gateway process from running.
- Only native Bloomberg adapter modules may import `blpapi`.

### 1.6 Protocol and dependency pinning

The gateway shall target MCP protocol revision:

```text
2026-07-28
```

The implementation MUST use stateless Streamable HTTP. It MUST NOT depend on:

- The legacy `initialize` and `initialized` exchange.
- `Mcp-Session-Id`.
- `GET /mcp`.
- `DELETE /mcp`.
- A standalone SSE notification endpoint.
- Legacy HTTP+SSE transport.

Before production implementation begins, Milestone 0 shall generate and commit an exact compatibility lock containing:

```yaml
python_version: "<exact tested version>"
blpapi_version: "<exact installed and tested version>"
mcp_sdk_package: "mcp"
mcp_sdk_version: "<exact security-reviewed version>"
mcp_protocol_revision: "2026-07-28"
hermes_version: "<exact tested version>"
tailscale_version: "<exact tested version>"
```

All production dependencies MUST be locked in `uv.lock` or an equivalent lockfile.

### 1.7 Authentication profiles

Version 1 shall implement a private-deployment authentication profile:

```yaml
auth:
  profile: "private-static-bearer"
  mcp_oauth_compliant: false
```

This profile is suitable for one Hermes service on a private tailnet, but it MUST NOT be described as OAuth 2.1 compliance.

Requirements:

- Authentication MUST use the `Authorization: Bearer <token>` header.
- Tokens MUST NOT be accepted in query strings, paths, cookies or request bodies.
- Tokens MUST contain at least 256 bits of random entropy.
- Token comparison MUST use a constant-time comparison.
- Every request MUST be authenticated independently.
- Application handles MUST NOT be treated as authentication.
- Tokens MUST map to a server-owned principal and scope set.
- Token rotation MUST support a bounded overlap period.
- Authentication failures MUST be rate-limited and audited.
- Bearer tokens MUST never appear in logs or command-line arguments.

A future OAuth resource-server profile MAY be added. If enabled, it must implement protected-resource metadata, token audience validation, issuer validation, expiry validation and appropriate `WWW-Authenticate` responses.

### 1.8 Licensing and governance

The gateway shall provide operational controls that support compliance with the applicable Bloomberg agreement.

It MUST:

- Record request counts by principal, service and operation family.
- Record subscription counts and durations.
- Support configurable daily and monthly request budgets.
- Support a hard-stop policy returning `LICENSE_BUDGET_EXCEEDED`.
- Support configurable artifact persistence.
- Treat file-backed results as temporary transport artifacts, not a permanent data warehouse.
- Default result retention to 24 hours or less.
- Track entitlement and authorization failures.
- Open an entitlement circuit breaker after a configurable number of consecutive entitlement failures.
- Require operator intervention or a successful health probe before closing that circuit breaker.
- Avoid repeatedly submitting known-unentitled requests.
- Document that policy limits do not replace Bloomberg’s contractual or entitlement limits.
- Require installed-version limits and entitled services to be checked through Bloomberg’s workstation documentation and `WAPI <GO>` during Milestone 0.

## 2. Architecture and data model

### 2.1 System topology

```text
Linux host
┌─────────────────────────────────┐
│ Hermes                          │
│ MCP 2026-07-28 client           │
└───────────────┬─────────────────┘
                │
                │ Stateless Streamable HTTP
                │ HTTPS over Tailscale
                ▼
Windows Bloomberg workstation
┌─────────────────────────────────┐
│ Tailscale Serve                 │
│ Tailnet-only HTTPS proxy        │
└───────────────┬─────────────────┘
                │
                │ http://127.0.0.1:8765
                ▼
┌─────────────────────────────────┐
│ Bloomberg MCP Gateway           │
│                                 │
│ Authentication and principals   │
│ Policy, quota and cost engine   │
│ MCP tools and resources         │
│ Request/subscription registries │
│ Temporary result storage        │
│ Audit and health                │
└───────────────┬─────────────────┘
                │
                │ Canonical adapter API
                ▼
┌─────────────────────────────────┐
│ Native Bloomberg Runtime        │
│                                 │
│ Persistent consumer Session     │
│ Per-request EventQueue          │
│ Subscription event dispatcher   │
│ Service and schema registry     │
│ Native request builder          │
│ Native-to-canonical decoder     │
└───────────────┬─────────────────┘
                │
                │ 127.0.0.1:8194
                ▼
┌─────────────────────────────────┐
│ BBComm and Bloomberg Terminal   │
└─────────────────────────────────┘
```

### 2.2 Component responsibilities

| Component | Responsibility |
|---|---|
| MCP server | Stateless MCP transport, tool definitions, output schemas, structured results and resource links |
| Authentication middleware | Validate bearer token and resolve principal |
| Policy engine | Authorize tool, service, operation, request cost and result access |
| Quota engine | Enforce request, subscription, artifact and licence budgets |
| Discovery service | Open allowed services and inspect operations and schemas |
| Schema converter | Convert Bloomberg definitions into deterministic internal and JSON schemas |
| Request validator | Convert raw input into an immutable canonical request |
| Native request builder | Populate native Bloomberg requests from canonical requests |
| Session manager | Start, monitor, reconnect and stop the Bloomberg session |
| Request executor | Submit requests with a per-request native `EventQueue` |
| Subscription dispatcher | Decode asynchronous subscription and administrative events |
| Request registry | Track public request state and result ownership |
| Subscription registry | Track subscription groups, items, generations, buffers and cursors |
| Normalizer registry | Convert explicitly supported responses into versioned tabular models |
| Result store | Store small results in memory and large temporary artifacts on disk |
| Audit logger | Record security and usage metadata without secrets |
| Health service | Report process, Bloomberg, service, queue and storage state |

### 2.3 Repository layout

```text
bloomberg-mcp/
├── pyproject.toml
├── uv.lock
├── README.md
├── CHANGELOG.md
├── .gitignore
├── .env.example
├── compatibility.lock.yaml
├── config/
│   ├── default.yaml
│   ├── policy.example.yaml
│   └── logging.yaml
├── scripts/
│   ├── environment-probe.ps1
│   ├── install.ps1
│   ├── run.ps1
│   ├── health-check.ps1
│   ├── register-task.ps1
│   ├── unregister-task.ps1
│   └── configure-tailscale.ps1
├── src/
│   └── bloomberg_mcp/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── errors.py
│       ├── models.py
│       ├── instance_lock.py
│       ├── auth/
│       │   ├── middleware.py
│       │   ├── principal.py
│       │   └── token_verifier.py
│       ├── policy/
│       │   ├── engine.py
│       │   ├── models.py
│       │   ├── cost.py
│       │   └── quota.py
│       ├── mcp/
│       │   ├── server.py
│       │   ├── output_schemas.py
│       │   ├── discovery_tools.py
│       │   ├── request_tools.py
│       │   ├── subscription_tools.py
│       │   ├── curated_tools.py
│       │   └── resources.py
│       ├── blp/
│       │   ├── backend.py
│       │   ├── native_backend.py
│       │   ├── fake_backend.py
│       │   ├── session_manager.py
│       │   ├── request_executor.py
│       │   ├── subscription_dispatcher.py
│       │   ├── service_registry.py
│       │   ├── schema_registry.py
│       │   ├── schema_converter.py
│       │   ├── request_builder.py
│       │   ├── event_decoder.py
│       │   ├── value_codec.py
│       │   └── name_cache.py
│       ├── registry/
│       │   ├── requests.py
│       │   ├── subscriptions.py
│       │   └── cursors.py
│       ├── normalization/
│       │   ├── registry.py
│       │   ├── reference.py
│       │   ├── historical.py
│       │   ├── intraday.py
│       │   ├── instruments.py
│       │   └── fields.py
│       ├── storage/
│       │   ├── result_store.py
│       │   ├── memory_store.py
│       │   ├── file_store.py
│       │   └── cleanup.py
│       └── observability/
│           ├── audit.py
│           ├── metrics.py
│           ├── usage.py
│           └── health.py
├── tests/
│   ├── unit/
│   ├── property/
│   ├── contract/
│   ├── integration/
│   ├── windows/
│   ├── fixtures/
│   │   ├── schemas/
│   │   ├── requests/
│   │   └── events/
│   └── conftest.py
└── docs/
    ├── architecture.md
    ├── configuration.md
    ├── deployment.md
    ├── security.md
    ├── licensing-controls.md
    ├── mcp-tools.md
    └── troubleshooting.md
```

### 2.4 Native object boundary

This boundary is mandatory:

> Native BLPAPI `Event`, `Message`, `Element`, `Request`, `Service`, `Operation`, schema-definition and correlation-ID objects MUST NOT cross a thread boundary, an `await` boundary or the native adapter boundary.

The native adapter MUST:

1. Receive the native event.
2. Decode the event completely into immutable Python primitives.
3. Replace native correlation IDs with external opaque identifiers.
4. Return or queue only canonical Python models.
5. Release native references before control crosses into the async application layer.

Registries MUST NOT store native objects.

Incorrect:

```python
internal_correlation_id: object
event: blpapi.Event
message: blpapi.Message
```

Correct:

```python
native_token: int
external_request_id: str
canonical_message: CanonicalMessage
```

The native token is an adapter-owned integer or immutable identifier. It MUST NOT expose a native object or memory address.

### 2.5 Request execution model

Request/response operations shall use a per-request native `EventQueue` for the initial production implementation.

The executor shall:

1. Create a validated native request.
2. Allocate a unique native correlation token.
3. Create a request-specific native event queue.
4. Call `sendRequest` with that event queue.
5. Read events on a bounded worker.
6. Decode each event before leaving the worker.
7. Accumulate partial responses.
8. Finish only on final response, cancellation, deadline or session failure.
9. Publish canonical messages to the request registry.
10. Release all native objects.

The shared asynchronous BLPAPI event handler shall be reserved for:

- Subscription events.
- Subscription status.
- Session status.
- Service status.
- Administrative events.

A later implementation MAY use a unified dispatcher only after equivalent ordering, cancellation and native-lifetime tests pass.

### 2.6 Session lifecycle

Session states:

```text
STOPPED
STARTING
CONNECTING
CONNECTED
DEGRADED
RECONNECTING
STOPPING
FAILED
```

The session manager MUST:

- Use a single long-lived consumer session.
- Connect to `127.0.0.1:8194`.
- Open configured services at startup.
- Open additional allowlisted services on demand.
- Increment a `session_generation` after each successful new connection.
- Reopen configured services after reconnection.
- Invalidate service and schema caches after generation changes.
- Reject stale events from older generations.
- Use exponential backoff with jitter.
- Never automatically replay an in-flight request.
- Return `BLOOMBERG_SESSION_LOST` with `retryable: true` when a request is interrupted.
- Optionally re-establish subscriptions if policy permits.
- Mark re-established subscriptions with a new generation and possible data-gap warning.

### 2.7 Canonical schema descriptors

Native schema objects shall be converted into immutable descriptors:

```python
@dataclass(frozen=True)
class ElementDescriptor:
    name: str
    alternate_names: tuple[str, ...]
    datatype: BloombergDatatype
    description: str | None
    status: str | None
    min_values: int
    max_values: int | None
    children: tuple["ElementDescriptor", ...]
    enum_values: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class OperationDescriptor:
    service: str
    operation: str
    description: str | None
    request: ElementDescriptor | None
    responses: tuple[ElementDescriptor, ...]
    service_generation: int
    schema_hash: str
```

The converter MUST support:

- Optional and mandatory elements.
- Bounded and unbounded arrays.
- Nested sequences.
- Repeated scalar and complex elements.
- Choices.
- Enumerations.
- Alternate names.
- Deprecated definitions.
- Multiple response definitions.
- Anonymous nested definitions.
- Deterministic ordering.
- Deterministic `$defs` and `$ref` generation.
- Schema-cycle detection.
- Maximum traversal depth.
- Unsupported-datatype fallback.
- Deterministic schema hashing.

### 2.8 Canonical request pipeline

```text
Raw MCP arguments
→ MCP input-schema validation
→ Authentication and scope check
→ Service and operation lookup
→ Bloomberg schema validation
→ Scalar and cardinality conversion
→ Canonical request construction
→ Policy and cost evaluation
→ Schema-hash recheck
→ Native request construction
→ Bloomberg submission
```

Canonical request:

```python
@dataclass(frozen=True)
class CanonicalRequest:
    service: str
    operation: str
    schema_hash: str
    parameters: Mapping[str, CanonicalValue]
    estimated_cost: RequestCost
    response_mode: ResponseMode
    normalized_schema_version: str | None
```

The native backend MUST reject submission if the request’s `schema_hash` differs from the current operation schema. It shall return `SCHEMA_DRIFT_DETECTED`.

### 2.9 Request states

```text
RECEIVED
VALIDATING
QUEUED
SENT
PARTIAL
COMPLETED
FAILED
TIMED_OUT
CANCELLING
CANCELLED
EXPIRED
```

Request record:

```python
@dataclass
class RequestRecord:
    request_id: str
    principal_id: str
    client_request_id: str | None
    service: str
    operation: str
    schema_hash: str
    parameters_hash: str
    created_at: datetime
    queued_at: datetime | None
    sent_at: datetime | None
    completed_at: datetime | None
    deadline: datetime
    status: RequestStatus
    session_generation: int | None
    native_token: int | None
    event_count: int
    partial_response_count: int
    byte_count: int
    result_id: str | None
    item_errors: list[ItemError]
    warnings: list[GatewayWarning]
    error: GatewayError | None
```

### 2.10 Idempotency and retries

`blpapi_send_request` MUST accept an optional `client_request_id`.

The gateway shall maintain a configurable deduplication window, defaulting to 300 seconds, keyed by:

```text
principal_id + client_request_id
```

If the same key is received within the window:

- The request MUST NOT be submitted again.
- The existing request ID and state shall be returned.
- The response metadata shall include `idempotent_replay: true`.

Automatic request replay after native submission MUST be disabled in version 1.

### 2.11 Subscription group model

One external subscription group may contain multiple Bloomberg subscription items. Every native subscription item requires its own native correlation token.

```python
@dataclass
class SubscriptionGroup:
    subscription_id: str
    principal_id: str
    generation: int
    status: SubscriptionGroupStatus
    items: dict[str, SubscriptionItem]
    created_at: datetime
    expires_at: datetime
    dropped_events: int
```

```python
@dataclass
class SubscriptionItem:
    item_id: str
    topic: str
    fields: tuple[str, ...]
    options: Mapping[str, str]
    native_token: int
    status: SubscriptionItemStatus
    sequence: int
```

Lifecycle:

```text
CREATED
STARTING
ACTIVE
DEGRADED
RESUBSCRIBING
CANCELLING
CANCELLED
FAILED
EXPIRED
```

Rules:

- Every group belongs to one principal.
- Every item has its own native correlation token.
- Native tokens MUST never be returned externally.
- Events must identify group ID, item ID, group generation and item sequence.
- `blpapi_resubscribe` replaces the complete group definition in version 1.
- Resubscription preserves the group ID.
- Resubscription increments the group generation.
- Resubscription creates new item IDs and native tokens.
- Existing cursors become invalid after resubscription.
- Buffers are bounded.
- Dropped events are counted and reported.
- Automatic post-reconnect restoration is policy-controlled.
- Restored subscriptions receive a new generation and a data-gap warning.

### 2.12 Time and numeric fidelity

The global “UTC internally” rule applies to gateway event timestamps, not indiscriminately to Bloomberg field values.

Required field-value rules:

- Bloomberg `DATE` MUST remain a calendar date such as `2026-08-20`.
- A `DATE` MUST NOT be converted to midnight UTC.
- Bloomberg `TIME` MUST remain a time without an invented date.
- A `DATETIME` with an offset shall preserve that offset and may also expose a UTC representation.
- A naive `DATETIME` MUST retain `timezone: null`.
- A naive datetime MUST NOT be assumed to be UTC.
- Normalized market data MUST expose relevant timezone or calendar metadata where known.
- Gateway timestamps such as `received_at` shall use UTC ISO 8601.

Numeric rules:

- Float64 values MUST use a shortest round-trip representation.
- The gateway MUST NOT apply undocumented rounding.
- Fixed-precision conversion shall occur only when the schema or normalizer explicitly requires it.
- Integers outside interoperable JSON-safe ranges shall use tagged string encoding.
- NaN and infinity shall use tagged values.
- Arrow and Parquet columns MUST use declared deterministic types.
- If a normalized column has incompatible types, it shall fall back to string and produce a warning rather than silently coercing or dropping values.

## 3. MCP and Bloomberg interfaces

### 3.1 HTTP surface

```text
POST /mcp
GET  /health/live
GET  /health/ready
GET  /version
GET  /artifacts/{result_id}       Optional, disabled by default
GET  /metrics                     Optional, localhost only
```

The MCP endpoint MUST support only the stateless `2026-07-28` Streamable HTTP behavior.

It MUST:

- Accept each MCP message as a separate POST.
- Support JSON responses and request-scoped SSE where required.
- Require `MCP-Protocol-Version`.
- Require and validate `Mcp-Method`.
- Require and validate `Mcp-Name` where applicable.
- Reject header/body mismatches.
- Reject unsupported protocol versions.
- Validate `Origin`.
- Return HTTP 403 for a present but disallowed `Origin`.
- Validate `Host`.
- Enforce request-body limits.
- Require authentication before tool or resource execution.
- Avoid protocol-level sessions.

Access policy:

| Endpoint | Authentication | Exposure |
|---|---|---|
| `/health/live` | No | Boolean liveness only |
| `/health/ready` | Yes | Detailed component health |
| `/version` | Yes | Gateway and dependency versions |
| `/mcp` | Yes | MCP tools and resources |
| `/artifacts/{id}` | Yes and owner-bound | Optional artifact transfer |
| `/metrics` | Admin scope | Localhost only |

Unauthenticated liveness MUST NOT reveal:

- Bloomberg connectivity.
- Service names.
- User identity.
- Versions.
- Queue depth.
- File paths.
- Entitlements.

### 3.2 MCP result semantics

The application envelope shall be returned as MCP `structuredContent`; it shall not replace HTTP, JSON-RPC or MCP protocol errors.

Every tool MUST define:

- `inputSchema`.
- `outputSchema`.
- Machine-readable `structuredContent`.
- A concise text-content fallback for client compatibility.
- Appropriate tool annotations.

Application result:

```json
{
  "ok": true,
  "request_id": "req_01K...",
  "timestamp": "2026-08-20T04:20:00Z",
  "data": {},
  "warnings": [],
  "item_errors": [],
  "metadata": {
    "service": "//blp/refdata",
    "operation": "HistoricalDataRequest",
    "session_generation": 3,
    "elapsed_ms": 245
  }
}
```

Rules:

- Transport, authentication and malformed MCP failures use HTTP and MCP/JSON-RPC errors.
- A gateway-wide Bloomberg operation failure returns an MCP tool result with `isError: true`.
- Per-security, per-field and per-topic failures may coexist with useful data.
- Partial item failures do not automatically set `ok: false`.
- `ok: true` means the gateway completed the operation lifecycle.
- `item_errors` contains individual Bloomberg failures.
- `warnings` contains non-fatal gateway or Bloomberg conditions.

Example partial success:

```json
{
  "ok": true,
  "data": {
    "rows": []
  },
  "item_errors": [
    {
      "kind": "field",
      "field": "INVALID_FIELD",
      "category": "BAD_FLD",
      "message": "Field is not valid for this request."
    }
  ],
  "warnings": []
}
```

### 3.3 Error codes

Stable application errors:

```text
AUTH_REQUIRED
AUTH_INVALID
AUTH_FORBIDDEN
INVALID_ARGUMENT
INVALID_SERVICE
INVALID_OPERATION
INVALID_SCHEMA
UNKNOWN_ELEMENT
MISSING_REQUIRED_ELEMENT
INVALID_ELEMENT_TYPE
INVALID_ENUM_VALUE
INVALID_CHOICE
REQUEST_TOO_LARGE
RESPONSE_TOO_LARGE
RESULT_NOT_FOUND
RESULT_EXPIRED
REQUEST_NOT_FOUND
REQUEST_ALREADY_COMPLETE
SUBSCRIPTION_NOT_FOUND
SUBSCRIPTION_EXPIRED
SUBSCRIPTION_LIMIT_EXCEEDED
CURSOR_INVALID
QUEUE_FULL
RATE_LIMITED
LICENSE_BUDGET_EXCEEDED
SCHEMA_DRIFT_DETECTED
NORMALIZER_NOT_AVAILABLE
ARTIFACT_FORMAT_NOT_AVAILABLE
BLOOMBERG_NOT_CONNECTED
BLOOMBERG_TERMINAL_NOT_LOGGED_IN
BLOOMBERG_SESSION_FAILED
BLOOMBERG_SESSION_LOST
BLOOMBERG_SERVICE_NOT_OPEN
BLOOMBERG_SERVICE_OPEN_FAILED
BLOOMBERG_REQUEST_FAILED
BLOOMBERG_RESPONSE_ERROR
BLOOMBERG_SECURITY_ERROR
BLOOMBERG_FIELD_ERROR
BLOOMBERG_NOT_ENTITLED
BLOOMBERG_SUBSCRIPTION_FAILED
TIMEOUT
CANCELLED
INTERNAL_ERROR
```

Protocol-version and header mismatch errors shall use the selected MCP SDK’s protocol-defined error shapes rather than these application codes.

### 3.4 Discovery tools

#### `blpapi_list_services`

Lists services known to the gateway, not every Bloomberg service that may exist.

Known services are:

```text
configured services
∪ successfully opened services
∪ explicitly requested allowlisted services
```

Input:

```json
{
  "include_unopened": false
}
```

Output:

```json
{
  "services": [
    {
      "name": "//blp/refdata",
      "opened": true,
      "discover_allowed": true,
      "execute_allowed": true,
      "operation_count": 12,
      "schema_hash": "sha256:...",
      "session_generation": 3
    }
  ]
}
```

#### `blpapi_open_service`

Explicitly opens an allowlisted service.

Input:

```json
{
  "service": "//blp/refdata"
}
```

Requirements:

- Requires `bloomberg:discover`.
- Service must be configured as openable.
- Opening a service does not imply that its operations are executable.
- Repeated calls are idempotent.

#### `blpapi_describe_service`

Input:

```json
{
  "service": "//blp/refdata",
  "open_if_allowed": true,
  "include_operations": true,
  "include_event_schemas": false
}
```

#### `blpapi_describe_operation`

Input:

```json
{
  "service": "//blp/refdata",
  "operation": "HistoricalDataRequest",
  "schema_format": "json_schema"
}
```

Output shall include separate discovery and execution policy:

```json
{
  "service": "//blp/refdata",
  "operation": "HistoricalDataRequest",
  "request_schema": {},
  "response_schemas": [],
  "schema_hash": "sha256:...",
  "policy": {
    "discover_allowed": true,
    "execute_allowed": true,
    "required_scope": "bloomberg:historical"
  }
}
```

#### `blpapi_validate_request`

Validation MUST NOT submit to Bloomberg.

Input:

```json
{
  "service": "//blp/refdata",
  "operation": "HistoricalDataRequest",
  "schema_hash": "sha256:...",
  "parameters": {},
  "options": {
    "reject_unknown_elements": true
  }
}
```

Output:

```json
{
  "valid": true,
  "canonical_request": {},
  "estimated_cost": {
    "securities": 10,
    "fields": 5,
    "estimated_observations": 5000,
    "risk_score": 12
  },
  "warnings": []
}
```

### 3.5 Generic request tools

#### `blpapi_send_request`

Input:

```json
{
  "client_request_id": "hermes-job-42-step-3",
  "service": "//blp/refdata",
  "operation": "HistoricalDataRequest",
  "schema_hash": "sha256:...",
  "parameters": {
    "securities": ["700 HK Equity"],
    "fields": ["PX_LAST", "VOLUME"],
    "startDate": "20260101",
    "endDate": "20260820",
    "periodicitySelection": "DAILY"
  },
  "execution": {
    "wait_seconds": 30,
    "overall_deadline_seconds": 120,
    "response_mode": "canonical",
    "allow_canonical_fallback": false,
    "artifact_format": "auto",
    "preview_items": 100
  }
}
```

Execution semantics:

| Event | Required behavior |
|---|---|
| Completes within `wait_seconds` | Return bounded inline result or resource link |
| Still running after `wait_seconds` | Return request handle; request remains active |
| Reaches overall deadline | Attempt native cancellation and mark `TIMED_OUT` |
| Client HTTP disconnects | Continue by default; request remains principal-owned |
| Bloomberg session is lost | Fail request; do not replay |
| Duplicate `client_request_id` | Return existing request without resubmission |
| Schema hash changed | Reject with `SCHEMA_DRIFT_DETECTED` |

#### `blpapi_get_request`

Input:

```json
{
  "request_id": "req_01K...",
  "include_preview": true,
  "cursor": null,
  "limit": 100
}
```

Only the creating principal or an administrator may access it.

#### `blpapi_cancel_request`

Cancellation shall be idempotent and principal-bound.

### 3.6 Response modes

Supported modes:

| Mode | Availability | Meaning |
|---|---|---|
| `canonical` | All supported operations | Recursive JSON preserving event and message structure |
| `typed` | All supported operations | Canonical form with explicit Bloomberg type markers |
| `normalized` | Registered operations only | Stable operation-specific tabular or record schema |

If `normalized` is requested for an unregistered operation:

- Return `NORMALIZER_NOT_AVAILABLE`; or
- Fall back to canonical only when `allow_canonical_fallback=true`.

Initial normalizers:

- Reference data.
- Historical data.
- Intraday bars.
- Intraday ticks.
- Instrument search.
- Curve search.
- Government security search.
- Field search.

Every normalized response MUST include:

```text
normalized_schema_version
source_service
source_operation
schema_hash
```

### 3.7 Canonical message format

```json
{
  "event_type": "PARTIAL_RESPONSE",
  "message_type": "HistoricalDataResponse",
  "request_id": "req_01K...",
  "service": "//blp/refdata",
  "session_generation": 3,
  "sequence": 2,
  "received_at": "2026-08-20T04:20:00.123Z",
  "payload": {}
}
```

Typed example:

```json
{
  "$blp_type": "DATETIME",
  "value": "2026-08-20T12:20:00.123+08:00",
  "timezone": "+08:00"
}
```

Naive datetime:

```json
{
  "$blp_type": "DATETIME",
  "value": "2026-08-20T12:20:00.123",
  "timezone": null
}
```

Calendar date:

```json
{
  "$blp_type": "DATE",
  "value": "2026-08-20"
}
```

### 3.8 Overrides

Curated reference and historical tools may accept:

```json
{
  "overrides": {
    "EQY_FUND_CRNCY": "HKD",
    "REFERENCE_DATE": "20260820"
  }
}
```

Mapping rules:

- Each object entry becomes one repeating Bloomberg override sequence.
- Each sequence contains `fieldId` and `value`.
- Override values are canonically converted to strings.
- Input object order shall be preserved where the Python runtime guarantees it.
- Duplicate override keys are invalid.
- Empty override names are invalid.
- The generic request engine may also accept the native schema-defined repeating sequence form.
- Curated and generic override representations must produce equivalent canonical requests.

### 3.9 Subscription tools

#### `blpapi_subscribe`

Input:

```json
{
  "subscriptions": [
    {
      "topic": "700 HK Equity",
      "fields": ["LAST_PRICE", "BID", "ASK", "VOLUME"],
      "options": {
        "interval": "2"
      }
    }
  ],
  "retention": {
    "mode": "latest_and_changes",
    "max_events": 10000,
    "ttl_seconds": 3600
  }
}
```

Output includes group and item IDs:

```json
{
  "subscription_id": "sub_01K...",
  "generation": 1,
  "status": "STARTING",
  "items": [
    {
      "item_id": "subitem_01K...",
      "topic": "700 HK Equity",
      "status": "STARTING"
    }
  ],
  "expires_at": "2026-08-20T05:20:00Z"
}
```

#### `blpapi_read_subscription`

Input:

```json
{
  "subscription_id": "sub_01K...",
  "generation": 1,
  "mode": "changes",
  "cursor": "cur_01K...",
  "limit": 1000,
  "wait_seconds": 5
}
```

Long-polling requirements:

- `wait_seconds` shall have a configurable upper bound.
- Concurrent long-polls shall have a separate bounded limit.
- Long-polls MUST NOT consume Bloomberg request-concurrency slots.
- Invalidated-generation cursors return `CURSOR_INVALID`.

#### `blpapi_resubscribe`

Version 1 replacement semantics:

- Complete group definition required.
- Group ID preserved.
- Generation incremented.
- All native subscriptions replaced.
- New item IDs returned.
- Old cursors invalidated.
- Existing buffer cleared.
- A warning identifies the possible data gap.

#### `blpapi_cancel_subscription`

Cancellation is idempotent and principal-bound.

#### `blpapi_list_subscriptions`

Returns only subscriptions owned by the principal unless the caller has administrative scope.

### 3.10 Curated tools

Required tools:

```text
get_reference_data
get_historical_data
get_intraday_bars
get_intraday_ticks
search_instruments
search_curves
search_government_securities
search_fields
get_market_snapshot
```

Curated tools MUST:

- Use the same canonical validation and execution engine as generic requests.
- Avoid duplicate native BLPAPI logic.
- Apply conservative defaults.
- Provide stable input and output schemas.
- Return `normalized_schema_version`.
- Preserve item-level errors.
- Identify underlying service and operation.
- Enforce tighter limits than generic operations where appropriate.

Request templates MAY be used internally for `get_market_snapshot` after a dedicated lifecycle implementation and test suite exists. They are deferred from Milestone 1 and MUST NOT be exposed as raw MCP tools.

### 3.11 Large results and resources

Tool results that exceed inline limits shall return an MCP resource link.

Example structured result:

```json
{
  "status": "COMPLETED",
  "request_id": "req_01K...",
  "preview": [],
  "artifact": {
    "result_id": "res_01K...",
    "resource_uri": "bloomberg-result://res_01K.../metadata",
    "representation": "canonical-events",
    "format": "jsonl",
    "content_type": "application/x-ndjson",
    "byte_count": 28415562,
    "message_count": 5000,
    "sha256": "...",
    "expires_at": "2026-08-21T04:20:00Z"
  }
}
```

Storage rules:

| Representation | Permitted formats |
|---|---|
| Canonical nested messages | JSON, JSON Lines |
| Typed canonical messages | JSON, JSON Lines |
| Normalized tabular data | JSON, Arrow IPC, Parquet |
| Metadata and previews | JSON |

Parquet and Arrow MUST NOT be used for arbitrary nested canonical messages.

Resources:

```text
bloomberg://services
bloomberg://service/{percent-encoded-service}
bloomberg://operation?service={encoded}&name={encoded}
bloomberg-result://{result-id}/metadata
bloomberg-result://{result-id}/page/{page}
bloomberg-subscription://{subscription-id}/latest
```

Resource URIs MUST be structurally parsed. Service names such as `//blp/refdata` MUST be percent-encoded or passed as encoded query parameters.

The optional artifact endpoint may be enabled if Hermes cannot efficiently consume large MCP resources:

```text
GET /artifacts/{result_id}
```

It MUST:

- Require bearer authentication.
- Enforce principal ownership.
- Use a short TTL.
- Return the correct content type and length.
- Support range requests where practical.
- Never expose local paths.
- Be audited.
- Enforce concurrent-download limits.

### 3.12 Cache policy

The generic MCP tool catalog is stable and may use a longer cache lifetime. Bloomberg service and schema resources must use shorter cache lifetimes.

Suggested policy:

| Content | TTL |
|---|---:|
| Generic tool catalog | 1 hour |
| Curated tool catalog | 1 hour |
| Known-service list | 30 seconds |
| Service descriptor | 5 minutes |
| Operation schema | 15 minutes |
| Result metadata | Remaining result TTL |
| Subscription latest state | 1 second or no-cache |

Schema and service caches MUST be invalidated when:

- Session generation changes.
- A service is reopened.
- A schema hash changes.
- An administrator requests refresh.
- The gateway restarts.

## 4. Policy, operations and security

### 4.1 Configuration

```yaml
server:
  host: "127.0.0.1"
  port: 8765
  mcp_path: "/mcp"
  protocol_revision: "2026-07-28"
  stateless: true
  max_request_body_bytes: 1048576
  shutdown_timeout_seconds: 30
  allowed_hosts: []
  allowed_origins: []

bloomberg:
  host: "127.0.0.1"
  port: 8194
  connect_timeout_seconds: 10
  automatic_request_replay: false
  reconnect:
    enabled: true
    initial_delay_seconds: 1
    maximum_delay_seconds: 60
    multiplier: 2
    jitter: 0.2
  startup_services:
    - "//blp/refdata"
    - "//blp/mktdata"
    - "//blp/instruments"
    - "//blp/apiflds"

requests:
  max_concurrent: 4
  max_queued: 50
  default_wait_seconds: 30
  maximum_wait_seconds: 60
  default_deadline_seconds: 120
  maximum_deadline_seconds: 300
  deduplication_window_seconds: 300
  maximum_response_bytes: 268435456
  inline_result_bytes: 1048576
  result_ttl_seconds: 86400

subscriptions:
  maximum_per_principal: 20
  maximum_topics_per_group: 100
  maximum_fields_per_topic: 50
  maximum_buffered_events: 10000
  maximum_ttl_seconds: 86400
  default_ttl_seconds: 3600
  maximum_long_poll_seconds: 15
  maximum_concurrent_long_polls: 10
  restore_after_reconnect: true

storage:
  enabled: true
  directory: "%LOCALAPPDATA%\\BloombergMCP\\data"
  maximum_total_bytes: 10737418240
  cleanup_interval_seconds: 300
  default_canonical_format: "jsonl"
  default_tabular_format: "parquet"

auth:
  profile: "private-static-bearer"
  token_source: "windows_credential_manager"
  token_overlap_seconds: 3600

governance:
  daily_request_budget: 10000
  monthly_request_budget: 200000
  entitlement_failure_circuit_threshold: 5
  persist_usage_counters: true
  persist_result_artifacts: true

audit:
  enabled: true
  include_security_names: false
  include_field_names: true
  include_parameters: false

logging:
  level: "INFO"
  json: true
```

### 4.2 Policy model

Discovery and execution permissions MUST be distinct.

```yaml
principals:
  hermes:
    scopes:
      - "bloomberg:discover"
      - "bloomberg:reference"
      - "bloomberg:historical"
      - "bloomberg:intraday"
      - "bloomberg:generic-request"
      - "bloomberg:subscribe"
      - "bloomberg:result-read"

services:
  "//blp/refdata":
    open: true
    discover: true
    operations:
      "*":
        discover: true
        execute: false
      "ReferenceDataRequest":
        execute: true
        required_scope: "bloomberg:reference"
      "HistoricalDataRequest":
        execute: true
        required_scope: "bloomberg:historical"

  "//blp/instruments":
    open: true
    discover: true
    operations:
      "*":
        discover: true
        execute: false
      "instrumentListRequest":
        execute: true
      "curveListRequest":
        execute: true
      "govtListRequest":
        execute: true

  "//blp/apiflds":
    open: true
    discover: true
    operations:
      "*":
        discover: true
        execute: false

  "//blp/mktdata":
    open: true
    discover: true
    subscriptions: true

defaults:
  deny_unconfigured_services: true
  deny_unconfigured_operations: true
  deny_authorization_operations: true
  deny_provider_operations: true
  reject_unknown_elements: true

limits:
  maximum_securities: 100
  maximum_fields: 100
  maximum_estimated_observations: 1000000
  maximum_request_array_elements: 10000
  maximum_nesting_depth: 32
```

Operation-specific Bloomberg limits shall be recorded after Milestone 0 verification. Conservative configured limits must be used instead of relying on Bloomberg’s internal maximum pending-request threshold.

### 4.3 Policy evaluation order

```text
HTTP and MCP protocol validation
→ Authentication
→ Principal status
→ Tool scope
→ Service open/discovery permission
→ Operation discovery/execute permission
→ MCP input validation
→ Bloomberg schema validation
→ Canonical conversion
→ Schema-hash validation
→ Request cost calculation
→ Daily/monthly governance budget
→ Per-principal rate limit
→ Global queue and concurrency limit
→ Native Bloomberg submission
```

Generic request execution MUST NOT bypass any stage.

### 4.4 Cost model

Cost shall conservatively include:

```text
base operation cost
+ security count
+ field count
+ total repeating-element count
+ estimated date observations
+ intraday granularity multiplier
+ tick multiplier
+ subscription topic count
+ subscription field count
+ subscription duration multiplier
```

The model does not need to predict Bloomberg latency exactly. It must reject clearly excessive agent-generated requests before submission.

### 4.5 Event ordering

The gateway shall record:

- Session generation.
- Request message sequence.
- Subscription group generation.
- Subscription item sequence.
- Monotonic local receive timestamp.
- UTC wall-clock receive timestamp.

Local receive order MUST NOT be represented as Bloomberg market sequence unless Bloomberg explicitly provides such a sequence.

A single native subscription-dispatch thread SHOULD be used initially.

### 4.6 Name caching

All repeated element and message-name access SHOULD use interned `blpapi.Name` values.

The name cache shall:

- Be populated during schema conversion where possible.
- Be native-adapter-owned.
- Have bounded growth.
- Be reset only when required by process lifecycle.
- Never expose native `Name` objects outside the adapter.

### 4.7 Result storage

Default directory:

```text
%LOCALAPPDATA%\BloombergMCP\data
```

The store MUST:

- Use server-generated random IDs.
- Prevent path traversal.
- Use atomic writes.
- Apply restrictive file permissions.
- Track ownership, content type, checksum, size and expiry.
- Enforce per-artifact and total quotas.
- Remove expired artifacts.
- Never return local paths.
- Never derive paths directly from user input.
- Avoid storing raw secrets or authentication material.
- Support a configuration that disables persistent artifacts completely.

### 4.8 Logging and auditing

Audit fields:

```text
timestamp
principal_id
action
tool
service
operation
request_id
client_request_id hash
subscription_id
security_count
field_count
estimated_cost
duration_ms
outcome
error_code
response_bytes
result_id
client_address when available
```

Logs MUST NOT contain:

- Bearer tokens.
- Windows credentials.
- Bloomberg authentication material.
- Native object representations.
- Full unbounded response payloads.
- Unredacted local paths.
- Complete sensitive request parameters unless explicitly approved.

### 4.9 Health model

Detailed readiness shall be compositional:

```json
{
  "process": "UP",
  "mcp_transport": "READY",
  "authentication": "READY",
  "bloomberg_session": "DISCONNECTED",
  "session_generation": 4,
  "required_services": {
    "//blp/refdata": "CLOSED",
    "//blp/mktdata": "CLOSED"
  },
  "request_admission": "REJECTING",
  "subscription_admission": "REJECTING",
  "result_store": "READY",
  "entitlement_circuit": "CLOSED"
}
```

A disconnected Bloomberg session shall not make the HTTP process unavailable.

### 4.10 Metrics

Required internal metrics:

```text
blpapi_session_reconnects_total
blpapi_requests_total
blpapi_requests_active
blpapi_request_duration_seconds
blpapi_request_failures_total
blpapi_partial_responses_total
blpapi_response_bytes_total
blpapi_queue_depth
blpapi_subscriptions_active
blpapi_subscription_events_total
blpapi_subscription_events_dropped_total
blpapi_entitlement_failures_total
mcp_tool_calls_total
mcp_tool_failures_total
auth_failures_total
result_store_bytes
result_store_artifacts
governance_requests_today
governance_requests_month
```

Any Prometheus endpoint MUST bind only to localhost.

### 4.11 Security requirements

The gateway MUST:

- Bind the application backend to localhost.
- Use Tailscale Serve, not Funnel.
- Require application authentication in addition to Tailscale controls.
- Validate protocol version, method and name headers.
- Validate header/body consistency.
- Validate `Origin` and `Host`.
- Authenticate every request.
- Authorize every tool and resource call.
- Bind all handles to verified principals.
- Use cryptographically secure random handles.
- Expire requests, results, subscriptions and cursors.
- Reject unknown input elements by default.
- Enforce nesting, array, body, response, queue and storage limits.
- Apply rate limits to authentication failures.
- Treat Bloomberg text as untrusted content.
- Never interpret Bloomberg response text as instructions.
- Mark normalized news, headline, description and similar text as untrusted where supported.
- Apply maximum lengths to agent-visible text fields.
- Disable provider functionality.
- Disable caller identity selection.
- Disable caller-controlled session configuration.
- Disable caller-controlled filesystem paths.
- Prevent cross-principal handle access.
- Return generic external internal-error messages while retaining detailed local logs.

### 4.12 Tailscale deployment

Deployment shall:

- Keep the gateway on `127.0.0.1:8765`.
- Configure Tailscale Serve as a private HTTPS proxy.
- Verify that tailnet HTTPS certificates are enabled.
- Use a dedicated tag for the Bloomberg workstation.
- Use a dedicated identity or tag for Hermes.
- Apply a default-deny grant.
- Allow only Hermes to reach the Bloomberg MCP HTTPS service.
- Verify that Funnel is not configured on the service port.
- Test that an unrelated tailnet node cannot connect.

### 4.13 Windows deployment

Use Task Scheduler initially.

The task shall:

- Run under the Bloomberg Terminal user.
- Run only after that user logs on.
- Refuse registration under `LocalSystem`.
- Use an explicit working directory.
- Load secrets without command-line exposure.
- Restart on unexpected failure with bounded retry.
- Stop gracefully on logout or shutdown.
- Write logs to `%LOCALAPPDATA%\BloombergMCP\logs`.
- Acquire a named Windows mutex before opening a Bloomberg session.

## 5. Delivery plan and acceptance

### 5.1 Coding-agent execution contract

The coding agent MUST:

1. Complete Milestone 0 before production implementation.
2. Inspect installed APIs instead of assuming versions or methods.
3. Produce a file-level implementation plan before each milestone.
4. Obtain approval before beginning the next milestone.
5. Avoid inventing BLPAPI methods.
6. Reference the exact native BLPAPI method used in adapter docstrings.
7. Keep native imports inside the native adapter and tightly related native helpers.
8. Keep curated tools on top of the generic canonical request engine.
9. Run formatting, linting, type checking, unit tests, property tests and contract tests.
10. Produce a requirements traceability matrix.
11. Perform a post-implementation audit for dead code, duplicate abstractions, unbounded queues, secret leakage and cross-principal access.
12. Not commit, push or deploy unless explicitly instructed.
13. Not mark a milestone complete with skipped or `xfail` schema-converter, request-builder or event-decoder tests.

### 5.2 Milestone 0: Environment probe

Deliverables:

- Exact Python version.
- Exact installed BLPAPI version.
- Exact official MCP SDK version selected.
- Hermes MCP protocol compatibility report.
- Hermes bearer-header forwarding test.
- Hermes structured-content test.
- Hermes resource-link and resource-read test.
- BLPAPI connection proof.
- Open-service proof.
- Operation-introspection dump.
- Schema-introspection dump.
- List of configured and successfully opened entitled services.
- Verified Bloomberg operation limits from workstation documentation.
- Compatibility lockfile.
- Updated implementation plan.

No production server shall be implemented in this milestone.

### 5.3 Milestone 1: Core generic gateway

Deliver:

- Stateless MCP `2026-07-28` server.
- Private static bearer authentication.
- Principal and scope enforcement.
- Fake backend.
- Native session manager.
- Per-request event-queue executor.
- Service and operation discovery.
- Canonical schema descriptors.
- JSON Schema conversion.
- Canonical request validation.
- Generic request execution.
- Canonical and typed decoding.
- In-memory bounded results.
- Idempotency support.
- Reference and historical integration tests.

Exclude:

- Subscriptions.
- Arrow and Parquet.
- Artifact HTTP downloads.
- Automatic subscription restoration.
- Request templates.

### 5.4 Milestone 2: Production lifecycle

Deliver:

- Reconnection and generation tracking.
- Request queue and admission control.
- Cancellation and deadline handling.
- Schema drift detection.
- File-backed canonical JSONL results.
- Resource links and bounded pages.
- Cleanup and quotas.
- Audit logs.
- Usage and governance counters.
- Entitlement circuit breaker.
- Health and metrics.
- Windows scheduled-task deployment.
- Tailscale deployment scripts.

### 5.5 Milestone 3: Subscriptions

Deliver:

- Subscription groups and per-item native correlation tokens.
- Bounded latest-value and change buffers.
- Cursor protocol.
- Long-poll limits.
- Dropped-event accounting.
- Complete-group resubscription.
- Subscription generation handling.
- Reconnect restoration with data-gap warning.
- Subscription burst and soak tests.

### 5.6 Milestone 4: Curated tools and tabular output

Deliver:

- Curated reference and historical tools.
- Intraday bars and ticks.
- Instrument, curve and government-security search.
- Field search.
- Snapshot tool.
- Versioned normalizers.
- Arrow and Parquet for registered tabular results.
- Untrusted-text marking.
- Optional authenticated artifact download if required by Hermes testing.
- Optional internal request-template implementation for efficient snapshots.

### 5.7 Unit and property tests

Unit tests MUST cover:

- Configuration parsing.
- Environment expansion.
- Token verification.
- Principal ownership.
- Scope checks.
- Discovery-versus-execution policy.
- Cost and quota calculation.
- Request state transitions.
- Subscription group and item transitions.
- Cursor generation and invalidation.
- Idempotency deduplication.
- TTL expiry.
- Storage quota enforcement.
- Path traversal rejection.
- Recursive schema conversion.
- Alternate names.
- Unbounded cardinality.
- Required and optional elements.
- Scalar conversion.
- Integer bounds.
- Enumeration validation.
- Choice validation.
- Repeated scalars.
- Repeated sequences.
- Date preservation.
- Naive datetime preservation.
- Offset datetime preservation.
- Float round-trip fidelity.
- NaN and infinity.
- Canonical decoding.
- Typed decoding.
- Partial-success semantics.
- Schema-hash mismatch.
- Audit redaction.

Property-based tests MUST generate schema-valid payloads and verify:

```text
Generated canonical input
→ request validation
→ request construction fixture
→ canonical decode
→ equivalent value representation
```

Property tests shall include arbitrary nesting up to configured limits, arrays, choices, enumerations, dates and typed numeric edge cases.

### 5.8 Fake backend scenarios

```text
Successful final response
Multiple partial responses
Request-level error
Security error
Field error
Entitlement error
Partial data with item errors
Wait timeout while request continues
Overall deadline and cancellation
Duplicate client request ID
Session disconnect during request
Reconnect and service reopen
Schema change after reconnect
Stale event from previous generation
Successful subscription group
Partial subscription item failure
Burst subscription updates
Dropped subscription events
Resubscription and cursor invalidation
Subscription cancellation
Reconnect with subscription data gap
Entitlement circuit breaker
Daily budget exhaustion
```

Fixtures MUST NOT contain licensed production Bloomberg data.

### 5.9 Contract tests

Contract tests MUST verify:

- Every tool has an input schema.
- Every tool has an output schema.
- Every `structuredContent` result validates against its output schema.
- Unsupported MCP protocol versions are rejected.
- Required MCP headers are validated.
- Header/body mismatches are rejected.
- Invalid Origin returns HTTP 403.
- Authentication occurs before Bloomberg submission.
- Generic execution obeys policy.
- Curated tools use the generic engine.
- Resource access enforces ownership.
- Large results return resource links.
- No response exceeds the configured inline cap.
- No native object representation appears externally.
- Cancellation is idempotent.
- Duplicate `client_request_id` creates one Bloomberg submission.
- A field error can coexist with successful data.
- `DATE` values survive without timezone-induced day shifts.
- Normalized mode fails cleanly when no normalizer exists.

### 5.10 Windows integration tests

Required integration tests:

1. Connect to local BBComm.
2. Detect Terminal-not-logged-in separately from connection failure.
3. Open `//blp/refdata`.
4. Discover `HistoricalDataRequest`.
5. Convert its runtime request schema.
6. Execute valid reference data.
7. Execute valid historical data.
8. Combine partial responses correctly.
9. Preserve invalid-security errors.
10. Preserve invalid-field errors.
11. Preserve entitlement errors.
12. Execute instrument search.
13. Execute curve search if entitled.
14. Execute government-security search if entitled.
15. Execute field search.
16. Start a multi-item market-data subscription.
17. Verify each item has a separate internal correlation token.
18. Read changes with a cursor.
19. Resubscribe and invalidate the old cursor.
20. Cancel the group.
21. Interrupt the Bloomberg session.
22. Confirm in-flight requests are not replayed.
23. Confirm services reopen after reconnection.
24. Confirm schema caches are invalidated.
25. Confirm stale-generation events are rejected.
26. Confirm Hermes connects through Tailscale HTTPS.
27. Confirm invalid bearer tokens are rejected.
28. Confirm an unauthorized tailnet node cannot connect.
29. Confirm the backend remains localhost-bound.
30. Confirm a second gateway process is rejected by the Windows mutex.
31. Confirm artifacts expire.
32. Confirm workstation lock behavior.
33. Document logout and subsequent login behavior.

### 5.11 Performance targets

| Measurement | Target |
|---|---:|
| Unauthenticated liveness response | Under 100 ms |
| Schema-cache lookup | Under 20 ms |
| Ordinary policy and schema validation | Under 50 ms |
| Gateway overhead excluding Bloomberg | Under 100 ms |
| Concurrent active Bloomberg requests | At least 4 |
| Queued requests | At least 50 |
| Subscription processing | At least 1,000 events/second without failure |
| Memory use | Bounded by configured buffers and result thresholds |
| Artifact cleanup | Within two cleanup intervals |
| Duplicate idempotent call | Zero additional Bloomberg submissions |

### 5.12 Final acceptance criteria

The implementation is accepted only when:

- It runs under the interactive Bloomberg Windows user.
- It binds only to localhost.
- It uses MCP protocol revision `2026-07-28`.
- It does not use MCP protocol sessions.
- Hermes reaches it through Tailscale Serve.
- Bearer authentication is enforced.
- Handles are principal-bound.
- Invalid Origin and header mismatches are rejected correctly.
- Runtime operation schemas can be discovered.
- Discovery permission is separate from execution permission.
- Invalid requests are rejected before Bloomberg submission.
- Generic requests execute reference, historical and search operations.
- Partial Bloomberg responses are combined correctly.
- Item errors remain distinguishable from gateway-wide failures.
- Calendar dates are not converted into UTC instants.
- In-flight requests are never automatically replayed.
- Duplicate client request IDs create one submission.
- Subscriptions use a group/item model with per-item native correlation tokens.
- Subscription buffers are bounded.
- Resubscription invalidates old cursors.
- Large results use resource links and bounded resources.
- Parquet is limited to registered tabular normalizers.
- Schema drift is detected.
- Usage budgets and entitlement circuit breakers work.
- The fake backend supports CI without Bloomberg.
- No native BLPAPI object crosses the adapter boundary.
- Provider sessions, publishing, identity selection and arbitrary session configuration remain inaccessible.
- Logs contain no tokens or unbounded Bloomberg payloads.
- No critical converter or decoder test is skipped or marked `xfail`.
- The requirements traceability matrix is complete.
- Installation, deployment, recovery, security and governance procedures are documented.