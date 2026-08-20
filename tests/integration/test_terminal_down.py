"""Graceful degradation when the Bloomberg Terminal is not running.

The HTTP process must stay up (SPEC §4.9) and every Bloomberg-dependent call
must fail fast with a clean, retryable "unavailable" error so agents know to
retry later. Once the Terminal returns, the background startup-retry loop
recovers the session automatically.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio

from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.config import load_gateway_config
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.server import build_app
from bloomberg_mcp.policy.models import load_policy_config
from tests.conftest import REPO_ROOT, TEST_TOKEN, McpClient

pytestmark = pytest.mark.asyncio


def _fast_reconnect_config(tmp_path: Any) -> Any:
    base = load_gateway_config(os.path.join(REPO_ROOT, "config", "default.yaml"))
    return replace(
        base,
        backend="fake",
        storage=replace(base.storage, directory=str(tmp_path / "data")),
        bloomberg=replace(
            base.bloomberg,
            reconnect=replace(
                base.bloomberg.reconnect,
                initial_delay_seconds=0.1,
                maximum_delay_seconds=0.3,
                jitter=0.0,
            ),
        ),
    )


@pytest_asyncio.fixture
async def down_gateway(tmp_path: Any) -> AsyncIterator[Gateway]:
    """Gateway whose backend fails to start (Terminal down) and keeps failing."""
    config = _fast_reconnect_config(tmp_path)
    policy = load_policy_config(os.path.join(REPO_ROOT, "config", "policy.example.yaml"))
    gateway = Gateway(config, policy)
    gateway.backend.start_failures_remaining = 10_000  # stay down for the whole test

    verifier = TokenVerifier(config.auth, policy)
    app = build_app(gateway, verifier)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def host() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.get_running_loop().create_task(host())
    await started.wait()
    gateway._test_app = app  # type: ignore[attr-defined]
    try:
        yield gateway
    finally:
        stop.set()
        await task


@pytest_asyncio.fixture
async def down_client(down_gateway: Gateway) -> AsyncIterator[McpClient]:
    import httpx2

    transport = httpx2.ASGITransport(app=down_gateway._test_app)  # type: ignore[attr-defined]
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        yield McpClient(client)


async def test_http_stays_up_when_terminal_down(down_gateway: Gateway, down_client: McpClient) -> None:
    import httpx2

    transport = httpx2.ASGITransport(app=down_gateway._test_app)  # type: ignore[attr-defined]
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as raw:
        live = await raw.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "UP"

        ready = await raw.get("/health/ready", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
        assert ready.status_code == 200
        body = ready.json()
        assert body["bloomberg_session"] == "DISCONNECTED"
        assert body["request_admission"] == "REJECTING"


async def test_tools_return_retryable_unavailable(down_client: McpClient) -> None:
    for tool, args in [
        (
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            },
        ),
        ("get_reference_data", {"securities": ["A"], "fields": ["PX_LAST"]}),
        (
            "blpapi_validate_request",
            {"service": "//blp/refdata", "operation": "ReferenceDataRequest", "parameters": {}},
        ),
    ]:
        response = await down_client.call_tool(tool, args)
        structured = McpClient.structured(response)
        assert structured["ok"] is False, tool
        error = structured["error"]
        assert error is not None, tool
        assert error["retryable"] is True, tool
        assert error["code"]
        assert "unavailable" in error["message"].lower() or "retry" in error["message"].lower(), tool


async def test_subscribe_returns_retryable_unavailable(down_client: McpClient) -> None:
    response = await down_client.call_tool(
        "blpapi_subscribe",
        {"subscriptions": [{"topic": "A EQUITY", "fields": ["LAST_PRICE"]}]},
    )
    structured = McpClient.structured(response)
    assert structured["ok"] is False
    assert structured["error"]["retryable"] is True


async def test_discovery_still_works_when_down(down_client: McpClient) -> None:
    # list_services reads the backend catalog/policy, not a live session, so it
    # stays usable while down — reporting configured services as not opened.
    response = await down_client.call_tool("blpapi_list_services", {"include_unopened": True})
    structured = McpClient.structured(response)
    assert structured["ok"] is True
    services = structured["data"]["services"]
    assert services
    assert all(s["opened"] is False for s in services)


async def test_recovers_when_terminal_returns(tmp_path: Any) -> None:
    config = _fast_reconnect_config(tmp_path)
    policy = load_policy_config(os.path.join(REPO_ROOT, "config", "policy.example.yaml"))
    gateway = Gateway(config, policy)
    gateway.backend.start_failures_remaining = 1  # fail once, then succeed

    verifier = TokenVerifier(config.auth, policy)
    app = build_app(gateway, verifier)
    started = asyncio.Event()
    stop = asyncio.Event()

    async def host() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.get_running_loop().create_task(host())
    await started.wait()

    try:
        # Degraded immediately after startup.
        from bloomberg_mcp.models import SessionState

        assert gateway.backend.session_state is not SessionState.CONNECTED

        # The retry loop reconnects within a bounded time.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if gateway.backend.session_state is SessionState.CONNECTED:
                break
        assert gateway.backend.session_state is SessionState.CONNECTED

        # And Bloomberg-dependent tools work again.
        import httpx2

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as raw:
            client = McpClient(raw)
            response = await client.call_tool(
                "get_reference_data", {"securities": ["A EQUITY"], "fields": ["PX_LAST"]}
            )
            structured = McpClient.structured(response)
            assert structured["ok"] is True
    finally:
        stop.set()
        await task
