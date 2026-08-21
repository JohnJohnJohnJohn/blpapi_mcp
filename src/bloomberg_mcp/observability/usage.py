"""Usage accounting facade over the quota engine (SPEC §1.8).

The quota engine owns the counters; this module exposes the recording API
used by the request pipeline and by governance reporting.
"""

from __future__ import annotations

from bloomberg_mcp.observability.metrics import Metrics
from bloomberg_mcp.policy.quota import QuotaEngine


class UsageTracker:
    def __init__(self, quota: QuotaEngine, metrics: Metrics) -> None:
        self._quota = quota
        self._metrics = metrics

    def request_accepted(self, principal_id: str, service: str, operation: str) -> None:
        self._metrics.inc("blpapi_requests_total", principal=principal_id, operation=operation)

    def request_failed(self, principal_id: str, operation: str) -> None:
        self._metrics.inc("blpapi_request_failures_total", principal=principal_id, operation=operation)

    def entitlement_failure(self, service: str) -> None:
        self._metrics.inc("blpapi_entitlement_failures_total", service=service)
        self._quota.record_entitlement_failure(service)

    def entitlement_success(self, service: str) -> None:
        self._quota.record_entitlement_success(service)

    def snapshot(self) -> dict[str, int | bool]:
        return self._quota.snapshot()
