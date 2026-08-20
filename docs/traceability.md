# Requirements traceability matrix

Maps SPEC.md v1.1 requirements to implementation and tests.

| SPEC ref | Requirement | Implementation | Verified by |
|---|---|---|---|
| §1.1 | Stateless MCP `2026-07-28` Streamable HTTP | `mcp/server.py` (SDK stateless single-exchange path) | contract tests (protocol validation) |
| §1.3 | Capability classes A/B/C | policy deny defaults; no provider/auth APIs in `blp/` | `tests/unit/test_policy.py` (`test_authorization_family_forbidden`) |
| §1.5 | Desktop API only on 127.0.0.1:8194; localhost bind; single-instance mutex | `config.py` hard check; `main.py` binds config host; `instance_lock.py` | `test_config.py`, `test_safety.py::test_instance_lock_single_instance` |
| §1.5 | Only native adapter imports blpapi | all blpapi imports under `blp/` | architecture review; `stubs/blpapi.pyi` |
| §1.6 | Protocol/dependency pinning | `compatibility.lock.yaml`, `uv.lock` | Milestone 0 probe (`scripts/environment-probe.ps1`) |
| §1.7 | Bearer auth: header-only, entropy, constant-time, rotation overlap, rate-limited failures | `auth/token_verifier.py`, `auth/middleware.py`, `policy/quota.py` | `tests/unit/test_auth.py`; contract `test_invalid_bearer_rejected` |
| §1.8 | Budgets, hard-stop, circuit breaker, retention | `policy/quota.py`, `storage/result_store.py` | `test_policy.py` (budget/breaker), integration (budget exhaustion, breaker) |
| §2.4 | Native object boundary | `blp/event_decoder.py`, `blp/request_builder.py`, canonical models | `models.py` design; contract `test_no_native_object_representation_externally` |
| §2.5 | Per-request EventQueue; shared handler only for sub/admin events | `blp/request_executor.py`, `blp/subscription_dispatcher.py` | integration scenarios |
| §2.6 | Session states, generation, backoff, no replay | `blp/session_manager.py`, `blp/fake_backend.py` | integration (session loss, reconnect, drift, stale events) |
| §2.7 | Canonical descriptors, JSON Schema, cycles, hashing | `blp/schema_converter.py`, `blp/schema_registry.py` | `tests/unit/test_schema_converter.py` |
| §2.8 | Canonical pipeline + schema-hash recheck | `mcp/canonical.py`, `registry/requests.py`, backends | contract drift tests |
| §2.9 | Request states/records | `models.py`, `registry/requests_registry.py` | `tests/unit/test_registries.py` |
| §2.10 | Idempotency window | `registry/requests_registry.py`, `registry/requests.py` | contract `test_duplicate_client_request_id_single_submission` |
| §2.11 | Subscription groups, per-item tokens, bounded buffers, generations | `registry/subscriptions.py`, `registry/cursors.py` | integration subscription scenarios |
| §2.12 | Date/time/datetime fidelity; NaN/inf; tagged big ints | `blp/value_codec.py` | `tests/unit/test_value_codec.py`, property tests |
| §3.1 | HTTP surface, headers, Origin/Host, body limits | `mcp/server.py` (gate + SDK transport security + manager body limit) | contract (version/headers/Origin) |
| §3.2 | Envelope, structuredContent, text fallback, isError semantics | `mcp/output_schemas.py`, `mcp/server.py` | contract `test_structured_content_validates_against_output_schema` |
| §3.3 | Stable error codes | `errors.py` | used throughout; contract assertions |
| §3.4 | Discovery tools | `mcp/discovery_tools.py` | contract discovery tests |
| §3.5 | send/get/cancel semantics (wait handle, deadline, disconnect survival) | `registry/requests.py` | integration (wait timeout, deadline, cancel idempotent) |
| §3.6 | Response modes; normalizer metadata; fallback | `registry/requests.py::_finalize`, `normalization/` | contract normalized tests |
| §3.7 | Canonical message format | `models.CanonicalMessage`, `blp/event_decoder.py` | unit codec tests |
| §3.8 | Overrides mapping | `mcp/canonical.py::canonical_overrides` | curated tools; property round-trip |
| §3.9 | Subscription tools incl. long-poll caps | `mcp/subscription_tools.py`, `registry/subscriptions.py` | integration scenarios |
| §3.10 | Curated tools on generic engine | `mcp/curated_tools.py` | contract (curated outputs) |
| §3.11 | Resource links, pages, ownership; resources list | `storage/result_store.py`, `mcp/resources.py` | contract large-result test |
| §3.12 | Cache policy | SDK cache hints hook documented; short-TTL resources | docs/mcp-tools.md |
| §4.1 | Configuration model + env expansion | `config.py`, `config/default.yaml` | `test_config.py` |
| §4.2 | Policy model; discovery ≠ execution | `policy/models.py`, `policy/engine.py` | `test_policy.py` |
| §4.3 | Evaluation order (no bypass) | `mcp/canonical.py` + `registry/requests.py` pipeline | contract policy tests |
| §4.4 | Cost model | `policy/cost.py` | `test_policy.py::test_cost_model_historical` |
| §4.5 | Ordering metadata (generation/sequence/timestamps) | `models.CanonicalMessage`, registries | integration scenarios |
| §4.6 | Name caching | `blp/name_cache.py` | native adapter use |
| §4.7 | Result storage safety | `storage/file_store.py`, `storage/result_store.py` | `test_safety.py` (traversal/quota/expiry) |
| §4.8 | Audit fields, redaction | `observability/audit.py` | `test_safety.py` audit tests |
| §4.9 | Compositional health | `observability/health.py`, HTTP routes | contract/health checks |
| §4.10 | Metrics | `observability/metrics.py`, gauges in `Gateway._sweep_once` | `/metrics` endpoint |
| §4.11 | Security requirements | see docs/security.md | contract tests |
| §4.12 | Tailscale deployment | `scripts/configure-tailscale.ps1` | manual (Milestone 0) |
| §4.13 | Scheduled task deployment | `scripts/register-task.ps1` (refuses LocalSystem) | manual |
| §5.7 | Unit + property coverage | `tests/unit`, `tests/property` | suite run |
| §5.8 | Fake backend scenarios | `blp/fake_backend.py` | `tests/integration/test_fake_scenarios.py` |
| §5.9 | Contract tests | `tests/contract/test_contract.py` | suite run |
| §5.10 | Windows integration tests | require real Terminal; procedure in docs/deployment.md | manual checklist |

## Status notes

- Automated verification runs against the deterministic fake backend, so CI
  needs no Bloomberg Terminal (SPEC §5.8).
- Items requiring a logged-on Terminal (connectivity proof, entitlements,
  installed-version limits, Tailscale negative tests) are documented as
  manual Milestone 0 / §5.10 procedures; `compatibility.lock.yaml` records
  what was verified in this environment.
