"""Native Bloomberg backend (SPEC §2.1, §2.5, §2.6).

Composes the session manager, service registry, request builder, request
executor and subscription dispatcher behind the canonical
:class:`BloombergBackend` contract. All ``blpapi`` imports stay within the
``blp`` package; only canonical models cross outward.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import blpapi

from bloomberg_mcp.blp.backend import (
    BloombergBackend,
    ExecutionHandle,
    ServiceSummary,
    SessionListener,
    SubscriptionSink,
)
from bloomberg_mcp.blp.name_cache import NameCache
from bloomberg_mcp.blp.request_builder import populate_request
from bloomberg_mcp.blp.request_executor import read_event_queue
from bloomberg_mcp.blp.schema_registry import SchemaRegistry
from bloomberg_mcp.models import ResponseMode
from bloomberg_mcp.blp.service_registry import ServiceRegistry
from bloomberg_mcp.blp.session_manager import SessionManager
from bloomberg_mcp.blp.subscription_dispatcher import SubscriptionDispatcher
from bloomberg_mcp.config import BloombergConfig, RequestsConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import CanonicalRequest, OperationDescriptor, SessionState

logger = logging.getLogger(__name__)

EntitlementCallback = Callable[[], None]


class NativeBloombergBackend(BloombergBackend):
    def __init__(
        self,
        bloomberg_config: BloombergConfig,
        requests_config: RequestsConfig,
        on_entitlement_failure: EntitlementCallback | None = None,
    ) -> None:
        self._config = bloomberg_config
        self._requests = requests_config
        self._on_entitlement_failure = on_entitlement_failure
        self._name_cache = NameCache()
        self._dispatcher = SubscriptionDispatcher()
        self._session_manager = SessionManager(bloomberg_config, self._dispatcher, self._on_generation_change)
        self._schema_registry = SchemaRegistry()
        self._services = ServiceRegistry()
        self._session_listener: SessionListener | None = None
        self._correlation_ids: dict[int, blpapi.CorrelationId] = {}
        self._token_counter = 0
        self._state_lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._dispatcher.set_loop(asyncio.get_running_loop())
        await self._session_manager.start()
        await self._refresh_schemas()
        await self._notify_session_listener()

    async def stop(self) -> None:
        await self._session_manager.stop()
        self._services.invalidate_all()
        await self._notify_session_listener()

    def _on_generation_change(self, generation: int) -> None:
        # Invalidate schema caches when the generation changes (SPEC §2.6).
        # Called from the event loop (session start / reconnect paths).
        self._services.invalidate_all()
        asyncio.ensure_future(self._refresh_and_notify())

    async def _refresh_and_notify(self) -> None:
        try:
            await self._refresh_schemas()
        except Exception:
            logger.exception("schema refresh after reconnect failed")
        await self._notify_session_listener()

    async def _notify_session_listener(self) -> None:
        if self._session_listener is not None:
            await self._session_listener(self.session_state, self.session_generation)

    @property
    def session_state(self) -> SessionState:
        return self._session_manager.state

    @property
    def session_generation(self) -> int:
        return self._session_manager.generation

    def service_states(self) -> Mapping[str, bool]:
        return self._services.snapshot(self._session_manager.configured_services())

    def set_session_listener(self, listener: SessionListener | None) -> None:
        self._session_listener = listener

    def set_subscription_sink(self, sink: SubscriptionSink | None) -> None:
        self._dispatcher.set_sink(sink)

    # ----------------------------------------------------------------- services

    async def open_service(self, service: str) -> None:
        await self._session_manager.open_service(service)
        self._services.mark_opened(service, self.session_generation)
        await self._refresh_service_schema(service)

    async def _refresh_schemas(self) -> None:
        for service in list(self._session_manager.opened_services()):
            self._services.mark_opened(service, self.session_generation)
            await self._refresh_service_schema(service)

    async def _refresh_service_schema(self, service: str) -> None:
        native_service = await asyncio.to_thread(self._get_native_service, service)
        if native_service is None:
            return
        operations = await asyncio.to_thread(
            self._schema_registry.convert_service, native_service, self.session_generation
        )
        self._services.set_operations(service, self.session_generation, operations)

    def _get_native_service(self, name: str) -> blpapi.Service | None:
        session = self._session_manager.session
        if session is None:
            return None
        try:
            return session.getService(name)
        except Exception:
            return None

    def list_service_summaries(self) -> list[ServiceSummary]:
        summaries: list[ServiceSummary] = []
        for name, opened in self.service_states().items():
            operations = self._services.operations(name)
            hashes = sorted({o.schema_hash for o in operations.values()})
            summaries.append(
                ServiceSummary(
                    name=name,
                    opened=opened,
                    discover_allowed=True,
                    execute_allowed=bool(operations),
                    operation_count=len(operations),
                    schema_hash=hashes[0] if hashes else None,
                    session_generation=self.session_generation,
                )
            )
        return summaries

    def get_operation(self, service: str, operation: str) -> OperationDescriptor:
        operations = self._services.operations(service)
        if operation not in operations:
            if self.session_state is not SessionState.CONNECTED:
                # Schemas are unavailable while disconnected: report the real
                # cause (retryable) instead of a misleading INVALID_OPERATION.
                self.assert_available()
            raise GatewayError(ErrorCode.INVALID_OPERATION, f"Unknown operation {operation!r} on {service!r}.")
        return operations[operation]

    def list_operations(self, service: str) -> list[OperationDescriptor]:
        return list(self._services.operations(service).values())

    # ------------------------------------------------------------------ requests

    @staticmethod
    def _log_built_request(native_request: blpapi.Request, service: str, operation: str) -> None:
        """INFO-level dump of the built native request (element -> value count).

        This is the discriminator for silently-ignored parameters (e.g.
        overrides): an element that was built appears with its value count;
        one that never made it into the native request is absent or array[0].
        """
        try:
            parts: list[str] = []
            for index in range(native_request.numElements()):
                element = native_request.getElement(index)
                name = element.name().toString()
                if element.isArray():
                    parts.append(f"{name}=array[{element.numValues()}]")
                else:
                    parts.append(f"{name}=scalar")
            logger.info("native request %s/%s built: %s", service, operation, ", ".join(parts))
        except Exception:  # pragma: no cover - observability must never break requests
            pass

    async def submit_request(self, request: CanonicalRequest, external_request_id: str) -> ExecutionHandle:
        await self.require_connected()
        descriptor = self.get_operation(request.service, request.operation)
        if descriptor.schema_hash != request.schema_hash:
            raise GatewayError(
                ErrorCode.SCHEMA_DRIFT_DETECTED,
                "Operation schema changed since discovery; re-discover and retry.",
                retryable=True,
            )
        native_service = await asyncio.to_thread(self._get_native_service, request.service)
        if native_service is None:
            raise GatewayError(ErrorCode.BLOOMBERG_SERVICE_NOT_OPEN, f"Service {request.service!r} is not open.")

        with self._state_lock:
            self._token_counter += 1
            token = self._token_counter
            correlation_id = blpapi.CorrelationId(value=token)
            self._correlation_ids[token] = correlation_id

        if descriptor.request is None:
            raise GatewayError(ErrorCode.INVALID_SCHEMA, f"Operation {request.operation!r} has no request schema.")
        event_queue = blpapi.EventQueue()
        try:
            native_request = await asyncio.to_thread(native_service.createRequest, request.operation)
            await asyncio.to_thread(
                populate_request, native_request, descriptor.request, request.parameters, self._name_cache
            )
            self._log_built_request(native_request, request.service, request.operation)
            session = self._session_manager.session
            assert session is not None
            await asyncio.to_thread(session.sendRequest, native_request, None, correlation_id, event_queue)
        except GatewayError:
            raise
        except Exception as exc:
            with self._state_lock:
                self._correlation_ids.pop(token, None)
            raise GatewayError(
                ErrorCode.BLOOMBERG_REQUEST_FAILED,
                "Failed to submit request to Bloomberg.",
                details={"local_detail": str(exc)},
            ) from exc

        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + self._requests.maximum_deadline_seconds
        threading.Thread(
            target=read_event_queue,
            args=(
                event_queue,
                loop,
                queue,
                external_request_id,
                request.service,
                self.session_generation,
                deadline,
                lambda: self._session_manager.state,
                self._on_entitlement_failure,
            ),
            kwargs={"typed": request.response_mode == ResponseMode.TYPED},
            name=f"blp-request-{token}",
            daemon=True,
        ).start()
        return ExecutionHandle(native_token=token, session_generation=self.session_generation, messages=queue)

    async def cancel_request(self, native_token: int) -> None:
        session = self._session_manager.session
        with self._state_lock:
            correlation_id = self._correlation_ids.get(native_token)
        if session is None or correlation_id is None:
            return
        try:
            await asyncio.to_thread(session.cancel, correlation_id)
        except Exception:
            logger.debug("cancel failed for token %d", native_token, exc_info=True)

    # ------------------------------------------------------------- subscriptions

    def _build_subscription_list(
        self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]
    ) -> blpapi.SubscriptionList:
        subscription_list = blpapi.SubscriptionList()
        with self._state_lock:
            for item, token in zip(items, native_tokens, strict=True):
                correlation_id = blpapi.CorrelationId(value=token)
                self._correlation_ids[token] = correlation_id
                fields = list(item.get("fields") or [])
                options = dict(item.get("options") or {})
                subscription_list.add(str(item.get("topic", "")), fields, options, correlation_id)
        return subscription_list

    async def subscribe(self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]) -> None:
        await self.require_connected()
        session = self._session_manager.session
        assert session is not None
        subscription_list = self._build_subscription_list(items, native_tokens)
        errors = await asyncio.to_thread(session.subscribe, subscription_list)
        if errors:
            raise GatewayError(
                ErrorCode.BLOOMBERG_SUBSCRIPTION_FAILED,
                "Bloomberg rejected part of the subscription request.",
                details={"count": len(errors)},
            )

    async def resubscribe(self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]) -> None:
        # Group replacement semantics (SPEC §2.11) are implemented by the
        # gateway as unsubscribe(old tokens) + subscribe(new items/tokens);
        # this method starts the new native subscriptions only.
        await self.subscribe(items, native_tokens)

    async def unsubscribe(self, native_tokens: list[int]) -> None:
        session = self._session_manager.session
        if session is None:
            return
        subscription_list = blpapi.SubscriptionList()
        with self._state_lock:
            for token in native_tokens:
                correlation_id = self._correlation_ids.pop(token, None)
                if correlation_id is not None:
                    subscription_list.add(None, None, None, correlation_id)
        if subscription_list.size() > 0:
            await asyncio.to_thread(session.unsubscribe, subscription_list)
