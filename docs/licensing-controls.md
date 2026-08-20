# Licensing controls

Operational controls that support compliance with the Bloomberg agreement
(SPEC §1.8). **Gateway limits are not a substitute for Bloomberg's
contractual or entitlement limits.**

## Usage accounting

- Every accepted request is counted per principal per day and month, and per
  `service|operation` family (`policy/quota.py`).
- Subscription groups and durations are tracked by the subscription registry
  (per-principal group cap, topics-per-group cap, TTLs).
- Counters persist to `storage.directory/usage.json` when
  `governance.persist_usage_counters` is true.

## Budgets

- `governance.daily_request_budget` / `monthly_request_budget` — exceeding
  either rejects new requests with `LICENSE_BUDGET_EXCEEDED`.
- Per-principal rate limiting (sliding one-minute window) rejects bursts
  with `RATE_LIMITED` before Bloomberg is touched.
- The cost model (`policy/cost.py`) rejects clearly excessive requests
  (securities × fields × date range estimates, intraday granularity and tick
  multipliers) with `REQUEST_TOO_LARGE` prior to submission.

## Entitlement circuit breaker

- Consecutive entitlement failures (`NO_AUTH` item errors or request-level
  `NO_AUTH`) increment the breaker counter.
- At `governance.entitlement_failure_circuit_threshold` the breaker opens:
  further requests are rejected with `BLOOMBERG_NOT_ENTITLED`, avoiding
  repeated submission of known-unentitled requests.
- The breaker closes on operator intervention
  (`QuotaEngine.reset_entitlement_circuit`) or on the next successful
  entitled exchange (health-probe path).

## Artifacts

- `governance.persist_result_artifacts` can disable durable artifacts
  entirely (memory-only).
- File artifacts default to ≤ 24 h retention, are bounded by
  `storage.maximum_total_bytes`, and are removed within two cleanup
  intervals (SPEC §5.11). They are temporary transport artifacts, not a data
  warehouse.

## Verification duties (Milestone 0)

Installed-version limits and entitled services must be checked on the
workstation through Bloomberg's documentation and `WAPI <GO>` before
production use; record the results alongside `compatibility.lock.yaml`.

## Observability

`/metrics` (localhost, admin scope) exposes
`governance_requests_today`, `governance_requests_month`,
`blpapi_entitlement_failures_total`, `blpapi_requests_active`,
`blpapi_subscriptions_active`, `result_store_bytes`,
`result_store_artifacts` and related counters. Audit records capture
principal, service, operation, cost, duration and outcome without secrets.
