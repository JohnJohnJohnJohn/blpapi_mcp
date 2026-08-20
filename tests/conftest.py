"""Shared fixtures: fake-backend gateway over ASGI with the 2026-07-28 envelope."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio

from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.config import load_gateway_config
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.server import build_app
from bloomberg_mcp.policy.models import load_policy_config

TEST_TOKEN = "test-bearer-" + "x" * 48

PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
PROTOCOL_REVISION = "2026-07-28"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def _bearer_env() -> None:
    os.environ["BLOOMBERG_MCP_BEARER_TOKEN"] = TEST_TOKEN


@pytest.fixture
def gateway_config(tmp_path: Any) -> Any:
    config = load_gateway_config(os.path.join(REPO_ROOT, "config", "default.yaml"))
    return replace(
        config,
        backend="fake",
        storage=replace(config.storage, directory=str(tmp_path / "data")),
    )


@pytest.fixture
def policy_config() -> Any:
    return load_policy_config(os.path.join(REPO_ROOT, "config", "policy.example.yaml"))


@pytest.fixture
def gateway(gateway_config: Any, policy_config: Any) -> Gateway:
    return Gateway(gateway_config, policy_config)


class McpClient:
    """Minimal MCP 2026-07-28 client over ASGI."""

    def __init__(self, http_client: Any) -> None:
        self.client = http_client
        self._id = 0

    def _headers(self, method: str, name: str | None, *, token: str | None = TEST_TOKEN) -> dict[str, str]:
        headers = {
            "MCP-Protocol-Version": PROTOCOL_REVISION,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _meta(self) -> dict[str, Any]:
        return {PROTOCOL_VERSION_KEY: PROTOCOL_REVISION, CLIENT_CAPABILITIES_KEY: {}}

    async def call_tool(self, name: str, arguments: dict[str, Any], *, token: str | None = TEST_TOKEN) -> Any:
        self._id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"_meta": self._meta(), "name": name, "arguments": arguments},
        }
        return await self.client.post("/mcp", json=body, headers=self._headers("tools/call", name, token=token))

    async def read_resource(self, uri: str, *, token: str | None = TEST_TOKEN) -> Any:
        self._id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "resources/read",
            "params": {"_meta": self._meta(), "uri": uri},
        }
        return await self.client.post("/mcp", json=body, headers=self._headers("resources/read", uri, token=token))

    async def list_tools(self) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": "tools/list", "params": {"_meta": self._meta()}}
        return await self.client.post("/mcp", json=body, headers=self._headers("tools/list", None))

    async def raw_post(self, body: Any, headers: dict[str, str]) -> Any:
        return await self.client.post("/mcp", json=body, headers=headers)

    @staticmethod
    def structured(response: Any) -> dict[str, Any]:
        return response.json()["result"]["structuredContent"]


@pytest_asyncio.fixture
async def running_gateway(gateway: Gateway) -> AsyncIterator[Gateway]:
    import asyncio

    verifier = TokenVerifier(gateway.config.auth, gateway.policy_config)
    app = build_app(gateway, verifier)
    gateway._test_app = app  # type: ignore[attr-defined]

    # Host the lifespan inside a single task: anyio cancel scopes must be
    # entered and exited by the same task, and pytest-asyncio runs fixture
    # setup and teardown in different tasks.
    started = asyncio.Event()
    stop = asyncio.Event()
    error: list[BaseException] = []

    async def host() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:  # surface lifespan failures to the test
            error.append(exc)
            started.set()

    task = asyncio.get_running_loop().create_task(host())
    await started.wait()
    if error:
        raise error[0]
    try:
        yield gateway
    finally:
        stop.set()
        await task


@pytest_asyncio.fixture
async def mcp_client(running_gateway: Gateway) -> AsyncIterator[McpClient]:
    import httpx2

    app = running_gateway._test_app  # type: ignore[attr-defined]
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        yield McpClient(client)


CallTool = Callable[[str, dict[str, Any]], Awaitable[Any]]
