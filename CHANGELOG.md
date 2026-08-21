# Changelog

All notable changes to the Bloomberg MCP Gateway.

## [0.2.0] - 2026-08-21

### Added

- Durable `FinalResult` ownership (review finding A): finalized payloads are
  stored on the request record, so polling and idempotent replay return the
  same normalized/canonical result the synchronous path does; records expire
  via a TTL; decoded messages are released after finalization.
- Native request leases (C): reader threads honour the caller's deadline, the
  lease release primitive is idempotent, and shutdown drains reader threads.
- Atomic bounded admission (D): a single registry-side counter bounds
  active+queued work; queue-rejected requests no longer consume governance
  budget.
- Transactional subscriptions (E): resubscribe builds the new native state
  before destroying the old; expiry performs best-effort native unsubscribe
  with retry; unsubscribe failures stay observable via a pending-unsubscribe
  set; retention.mode is enforced on reads; resubscribe enforces field limits.
- Cursor consume-on-use + TTL + principal binding + per-subscription cap (F).
- Central validation (J): required-null rejection, repeated min_values,
  canonical/alias ambiguity detection, bidirectional strict_types, BYTE range,
  path-qualified error details, `additionalProperties: false` in advertised
  schemas, and a wired non-weakenable `reject_unknown_elements` option.
- Shared canonical tree walkers (G): item-error extraction handles the
  dict-or-list shapes the decoder emits for single-entry sequences.
- Live-derived fake backend shapes (H) and normalizer data retention (I):
  identifier columns (`security`, `curve`, `parseky`, `ticker`) and an
  `extra` map for unknown fields.
- Artifact lifecycle (L): normalized output honours `inline_result_bytes`;
  `artifact_format` (auto/json/jsonl) is implemented; artifact-backed tool
  results carry an MCP EmbeddedResource block; page reads stream only the
  requested range; a sidecar manifest survives restarts and startup sweeps
  orphaned artifacts; parquet/arrow claims removed.
- Auth semantics (M): explicit single-principal invariant (or per-token
  principal binding), previous-token expiry after the overlap window, separate
  previous-token file source, per-address auth rate limiting, wired
  `admit_auth_attempt`.
- Entitlement semantics (K): single consumer-side accounting point (native
  callback removed) and per-service entitlement circuits.
- Session-generation coordinator (N): one transition primitive for startup
  and reconnect; CONNECTED only after required services open; optional-service
  failures never stall reconnection; exactly one refresh/notify per
  generation.
- Observability (O): single `mcp_tool_calls_total` count, reconnect metric
  only on actual reconnects, per-tool output schemas, async quota persistence,
  and a name-keyed `fcntl.flock` single-instance lock on non-Windows (the
  Linux `SO_REUSEADDR` duplicate-bind defect is fixed).

### Removed

- Dead `parquet`/`arrow` storage formats (never implemented).
- The native entitlement-failure callback (double-counted REQUEST_STATUS
  NO_AUTH; the consumer path is the single accounting point).

## [0.1.0] - 2026-08-20

### Added

- Initial implementation of SPEC.md v1.1.
- Stateless MCP Streamable HTTP endpoint targeting revision `2026-07-28`.
- Private static bearer authentication with principal and scope enforcement.
- Policy engine separating discovery and execution permissions.
- Canonical request pipeline with schema validation, cost model and quotas.
- Native Bloomberg adapter (session manager, per-request EventQueue executor,
  subscription dispatcher, schema converter) and deterministic fake backend.
- Request registry with idempotency, cancellation and deadline handling.
- Subscription groups with per-item native correlation tokens, bounded
  buffers, cursors and long-poll limits.
- Curated tools: reference, historical, intraday bars/ticks, instrument,
  curve, government-security and field search, market snapshot.
- Versioned normalizers with canonical/typed/normalized response modes.
- Bounded result storage (memory + file-backed JSONL artifacts) with TTL
  cleanup and quotas; MCP resource links for large results.
- Governance budgets, entitlement circuit breaker, audit logging, metrics and
  compositional health model.
- Windows single-instance mutex, Task Scheduler and Tailscale deployment
  scripts.
