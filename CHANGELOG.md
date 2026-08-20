# Changelog

All notable changes to the Bloomberg MCP Gateway.

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
