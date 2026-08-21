"""CS9 tests: session-generation lifecycle coordinator (N)."""

from __future__ import annotations

import pytest

from bloomberg_mcp.blp.session_manager import SessionManager
from bloomberg_mcp.config import BloombergConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import SessionState


class _StubDispatcher:
    def handle_event(self, event):  # noqa: ANN001
        pass

    def attach_session_manager(self, manager):  # noqa: ANN001
        pass


def _manager(startup_services: tuple[str, ...], notifications: list[int] | None = None) -> SessionManager:
    sm = SessionManager(
        BloombergConfig(startup_services=startup_services),
        _StubDispatcher(),
        on_generation_change=(notifications.append if notifications is not None else None),
    )
    sm._start_native = lambda: None  # type: ignore[method-assign]
    return sm


async def test_reconnect_optional_service_failure_completes() -> None:
    """N4: one flaky service must not stall reconnection."""
    notifications: list[int] = []
    sm = _manager(("//blp/a", "//blp/b"), notifications)

    async def open(service: str, *, required: bool):  # noqa: ANN001
        if service == "//blp/b":
            raise GatewayError(ErrorCode.BLOOMBERG_SERVICE_NOT_OPEN, "b is down")
        return object()

    sm._open_native_service = open  # type: ignore[method-assign]
    await sm._transition(services_required=False)
    assert sm.state is SessionState.CONNECTED
    assert sm.generation == 1
    # Exactly one generation-change notification per transition (N1/N5).
    assert notifications == [1]


async def test_startup_required_service_failure_aborts() -> None:
    """N2/N3: CONNECTED is never published when a required service fails."""
    sm = _manager(("//blp/a", "//blp/b"))

    async def open(service: str, *, required: bool):  # noqa: ANN001
        if service == "//blp/b":
            raise GatewayError(ErrorCode.BLOOMBERG_SERVICE_OPEN_FAILED, "b refused")
        return object()

    sm._open_native_service = open  # type: ignore[method-assign]
    with pytest.raises(GatewayError):
        await sm._transition(services_required=True)
    assert sm.state is not SessionState.CONNECTED


async def test_transition_notifies_exactly_once() -> None:
    """N1: one notification per generation transition, even with many services."""
    notifications: list[int] = []
    sm = _manager(("//blp/a", "//blp/b", "//blp/c"), notifications)

    async def open(service: str, *, required: bool):  # noqa: ANN001
        return object()

    sm._open_native_service = open  # type: ignore[method-assign]
    await sm._transition(services_required=False)
    await sm._transition(services_required=False)
    assert notifications == [1, 2]
    assert sm.generation == 2
