"""Canonical adapter boundary (SPEC §2.1, §2.4).

The gateway core talks only to this interface. Native ``blpapi`` objects
never cross it: backends deliver immutable canonical models only.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bloomberg_mcp.errors import GatewayError
from bloomberg_mcp.models import (
    CanonicalRequest,
    EventKind,
    OperationDescriptor,
    SessionState,
)


@dataclass(frozen=True)
class ServiceSummary:
    name: str
    opened: bool
    discover_allowed: bool
    execute_allowed: bool
    operation_count: int
    schema_hash: str | None
    session_generation: int


@dataclass(frozen=True)
class ExecutionHandle:
    """One in-flight request inside the backend.

    ``messages`` receives canonical messages; the stream terminates with
    either a final ``RESPONSE`` message (success) or a :class:`GatewayError`
    instance pushed as the last item (failure).
    """

    native_token: int
    session_generation: int
    messages: asyncio.Queue[Any] = field(repr=False)


@dataclass(frozen=True)
class SubscriptionEvent:
    """Canonical subscription event decoded by the backend dispatcher."""

    native_token: int
    kind: EventKind  # SUBSCRIPTION_DATA or SUBSCRIPTION_STATUS
    message_type: str
    payload: Mapping[str, Any]
    received_at: str
    sequence: int = 0
    # For status events: SUBSCRIPTION_STARTED / SUBSCRIPTION_FAILURE / ...
    status: str | None = None
    error_code: str | None = None
    error_message: str | None = None


#: Async sink for subscription events delivered by the backend.
SubscriptionSink = Callable[[SubscriptionEvent], Awaitable[None]]
#: Notification of session state transitions (state, generation).
SessionListener = Callable[[SessionState, int], Awaitable[None]]


class BloombergBackend(abc.ABC):
    """Consumer-facing backend contract shared by native and fake adapters."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @property
    @abc.abstractmethod
    def session_state(self) -> SessionState: ...

    @property
    @abc.abstractmethod
    def session_generation(self) -> int: ...

    @abc.abstractmethod
    def service_states(self) -> Mapping[str, bool]:
        """Known service name -> opened flag."""

    @abc.abstractmethod
    def list_service_summaries(self) -> list[ServiceSummary]: ...

    @abc.abstractmethod
    async def open_service(self, service: str) -> None:
        """Open an allowlisted service; idempotent."""

    @abc.abstractmethod
    def get_operation(self, service: str, operation: str) -> OperationDescriptor: ...

    @abc.abstractmethod
    def list_operations(self, service: str) -> list[OperationDescriptor]: ...

    @abc.abstractmethod
    async def submit_request(
        self, request: CanonicalRequest, external_request_id: str, deadline_seconds: int | None = None
    ) -> ExecutionHandle:
        """Submit a validated canonical request; returns the execution handle.

        ``deadline_seconds`` is the caller's overall deadline; the native
        reader must stop at this bound instead of the global maximum.
        """

    @abc.abstractmethod
    async def cancel_request(self, native_token: int) -> None:
        """Idempotent native cancellation of an in-flight request."""

    @abc.abstractmethod
    async def release_request(self, native_token: int) -> None:
        """Release the native lease (correlation id, reader bookkeeping).

        Idempotent; called from every terminal path (complete / fail /
        cancel / timeout / session loss / shutdown) exactly once.
        """

    @abc.abstractmethod
    async def subscribe(self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]) -> None:
        """Start native subscriptions; one correlation token per item."""

    @abc.abstractmethod
    async def resubscribe(self, items: Sequence[Mapping[str, Any]], native_tokens: list[int]) -> None: ...

    @abc.abstractmethod
    async def unsubscribe(self, native_tokens: list[int]) -> None: ...

    @abc.abstractmethod
    def set_subscription_sink(self, sink: SubscriptionSink | None) -> None: ...

    @abc.abstractmethod
    def set_session_listener(self, listener: SessionListener | None) -> None: ...

    # ------------------------------------------------------------------ helpers

    def assert_available(self) -> None:
        """Synchronous availability gate for tool pipelines.

        When the Bloomberg Terminal session is down the gateway keeps serving
        HTTP (SPEC §4.9) but every Bloomberg-dependent call fails fast with a
        stable, retryable error so agents know to retry later.
        """
        if self.session_state is not SessionState.CONNECTED:
            raise GatewayError(
                code=_session_error_code(self.session_state),
                message=(
                    "Bloomberg service is currently unavailable (Terminal session not "
                    "connected). Nothing was submitted; retry later."
                ),
                retryable=True,
            )

    async def require_connected(self) -> None:
        self.assert_available()


def _session_error_code(state: SessionState) -> Any:
    from bloomberg_mcp.errors import ErrorCode

    if state is SessionState.FAILED:
        return ErrorCode.BLOOMBERG_SESSION_FAILED
    return ErrorCode.BLOOMBERG_NOT_CONNECTED
