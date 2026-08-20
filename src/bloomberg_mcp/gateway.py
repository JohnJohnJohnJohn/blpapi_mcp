"""Gateway composition root.

Wires backend, policy, quotas, registries, storage and observability into a
single object owned by the process lifespan. The MCP server layer consumes
this object only through canonical APIs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bloomberg_mcp.blp.backend import BloombergBackend
from bloomberg_mcp.blp.fake_backend import FakeBloombergBackend
from bloomberg_mcp.blp.native_backend import NativeBloombergBackend
from bloomberg_mcp.config import GatewayConfig
from bloomberg_mcp.models import SessionState
from bloomberg_mcp.normalization.registry import NormalizerRegistry, build_default_registry
from bloomberg_mcp.observability.audit import AuditLogger
from bloomberg_mcp.observability.metrics import Metrics
from bloomberg_mcp.observability.usage import UsageTracker
from bloomberg_mcp.policy.engine import PolicyEngine
from bloomberg_mcp.policy.models import PolicyConfig
from bloomberg_mcp.policy.quota import QuotaEngine
from bloomberg_mcp.registry.cursors import CursorRegistry
from bloomberg_mcp.registry.requests import RequestExecutor
from bloomberg_mcp.registry.requests_registry import RequestRegistry
from bloomberg_mcp.registry.subscriptions import SubscriptionRegistry
from bloomberg_mcp.storage.cleanup import CleanupTask
from bloomberg_mcp.storage.result_store import ResultStore

logger = logging.getLogger(__name__)


class Gateway:
    def __init__(
        self, config: GatewayConfig, policy: PolicyConfig, *, backend_override: BloombergBackend | None = None
    ) -> None:
        self.config = config
        self.policy_config = policy
        self.policy = PolicyEngine(policy)
        self.metrics = Metrics()
        self.audit = AuditLogger(config.audit)

        storage_dir = Path(config.storage.directory) if config.storage.directory else Path.cwd() / "data"
        self.quota = QuotaEngine(
            config.governance,
            config.requests,
            config.subscriptions,
            persist_path=storage_dir / "usage.json" if config.governance.persist_usage_counters else None,
        )
        self.usage = UsageTracker(self.quota, self.metrics)

        if backend_override is not None:
            self.backend: BloombergBackend = backend_override
        elif config.backend == "fake":
            self.backend = FakeBloombergBackend(startup_services=tuple(config.bloomberg.startup_services))
        else:
            self.backend = NativeBloombergBackend(
                config.bloomberg, config.requests, on_entitlement_failure=self.usage.entitlement_failure
            )

        self.result_store = ResultStore(config.storage, persist_artifacts=config.governance.persist_result_artifacts)
        self.normalizers: NormalizerRegistry = build_default_registry()
        self.cursors = CursorRegistry()
        self.request_registry = RequestRegistry()
        self.subscriptions = SubscriptionRegistry(self.backend, config.subscriptions, self.cursors)
        self.executor = RequestExecutor(
            backend=self.backend,
            registry=self.request_registry,
            result_store=self.result_store,
            quota=self.quota,
            usage=self.usage,
            audit=self.audit,
            metrics=self.metrics,
            config=config.requests,
            normalizers=self.normalizers,
        )
        self.backend.set_session_listener(self._on_session_event)
        self._cleanup = CleanupTask(
            config.storage.cleanup_interval_seconds,
            [self._sweep_once],
        )
        self._started = False

    async def start(self) -> None:
        self._started = True
        try:
            await self.backend.start()
        except Exception:
            # The HTTP process stays available even when Bloomberg is down
            # (SPEC §4.9); the health model reports the degraded state.
            logger.exception("backend startup failed; serving degraded")
        self._cleanup.start()

    async def stop(self) -> None:
        await self._cleanup.stop()
        try:
            await self.backend.stop()
        except Exception:
            logger.exception("backend shutdown failed")
        self._started = False

    async def _on_session_event(self, state: SessionState, generation: int) -> None:
        logger.info("session event: %s generation=%d", state.value, generation)
        self.metrics.inc("blpapi_session_reconnects_total")
        if state is SessionState.CONNECTED and self.config.subscriptions.restore_after_reconnect:
            await self.subscriptions.restore_after_reconnect()

    def _sweep_once(self) -> int:
        removed = self.result_store.sweep_expired()
        removed += len(self.subscriptions.expire_due())
        self.request_registry.sweep()
        stats = self.result_store.stats()
        self.metrics.set_gauge("result_store_bytes", stats["result_store_bytes"])
        self.metrics.set_gauge("result_store_artifacts", stats["result_store_artifacts"])
        quota = self.quota.snapshot()
        self.metrics.set_gauge("governance_requests_today", float(quota["governance_requests_today"]))
        self.metrics.set_gauge("governance_requests_month", float(quota["governance_requests_month"]))
        self.metrics.set_gauge(
            "blpapi_subscriptions_active", float(len(self.subscriptions.list_groups("", admin=True)))
        )
        self.metrics.set_gauge("blpapi_requests_active", float(self.request_registry.active_count()))
        self.metrics.set_gauge("blpapi_queue_depth", float(self.request_registry.queued_count()))
        return removed

    def health_inputs(self) -> tuple[BloombergBackend, QuotaEngine, ResultStore]:
        return self.backend, self.quota, self.result_store
