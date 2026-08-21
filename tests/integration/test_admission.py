"""CS3 regression tests: atomic bounded admission + quota ordering (finding D).

Failing against 0445a48: check-then-act admission races (two submits can both
pass the count check) and queue-rejected requests still consume the governance
budget because quota accounting runs before the admission check.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx2
import pytest
import pytest_asyncio

from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.config import load_gateway_config
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.server import build_app
from bloomberg_mcp.policy.models import load_policy_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def small_gateway(tmp_path: Any) -> AsyncIterator[Gateway]:
    """Gateway with max_concurrent=1 / max_queued=1 (or given values)."""
    config = load_gateway_config(os.path.join(REPO_ROOT, "config", "default.yaml"))
    config = replace(
        config,
        backend="fake",
        storage=replace(config.storage, directory=str(tmp_path / "data")),
        requests=replace(config.requests, max_concurrent=1, max_queued=1),
    )
    policy = load_policy_config(os.path.join(REPO_ROOT, "config", "policy.example.yaml"))
    gw = Gateway(config, policy)
    verifier = TokenVerifier(gw.config.auth, gw.policy_config)
    app = build_app(gw, verifier)
    gw._test_app = app  # type: ignore[attr-defined]

    started = asyncio.Event()
    stop = asyncio.Event()
    error: list[BaseException] = []

    async def host() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001 - surface to the test
            error.append(exc)
            started.set()

    task = asyncio.get_running_loop().create_task(host())
    await started.wait()
    if error:
        raise error[0]
    try:
        yield gw
    finally:
        stop.set()
        await task


class _Client:
    def __init__(self, gw: Gateway) -> None:
        transport = httpx2.ASGITransport(app=gw._test_app)  # type: ignore[attr-defined]
        self.client = httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")
        self._id = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._id += 1
        headers = {
            "Authorization": "Bearer " + "test-bearer-" + "x" * 48,
            "MCP-Protocol-Version": "2026-07-28",
            "mcp-method": "tools/call",
            "mcp-name": name,
        }
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
                "name": name,
                "arguments": arguments,
            },
        }
        return await self.client.post("/mcp", json=payload, headers=headers)

    @staticmethod
    def structured(response: Any) -> dict[str, Any]:
        return response.json()["result"]["structuredContent"]


@pytest_asyncio.fixture
async def client(small_gateway: Gateway) -> AsyncIterator[_Client]:
    c = _Client(small_gateway)
    yield c
    await c.client.aclose()


async def test_admission_hard_bound(client: _Client, small_gateway: Gateway) -> None:
    """Only max_concurrent+max_queued requests are admitted; the rest are
    rejected atomically with QUEUE_FULL (no over-admit under concurrency)."""
    small_gateway.backend.response_delay_seconds = 1.0
    try:
        results = await asyncio.gather(
            *[
                client.call_tool(
                    "blpapi_send_request",
                    {
                        "service": "//blp/refdata",
                        "operation": "ReferenceDataRequest",
                        "parameters": {"securities": [f"S{i}"], "fields": ["PX_LAST"]},
                        "execution": {"wait_seconds": 1, "overall_deadline_seconds": 30},
                    },
                )
                for i in range(8)
            ]
        )
        statuses = [
            client.structured(r).get("error", {}).get("code", "OK") if client.structured(r).get("error") else "OK"
            for r in results
        ]
        assert statuses.count("OK") == 2
        assert statuses.count("QUEUE_FULL") == 6, statuses
    finally:
        small_gateway.backend.response_delay_seconds = 0.0


async def test_queue_rejected_does_not_consume_governance_budget(client: _Client, small_gateway: Gateway) -> None:
    """Queue-rejected requests must NOT be counted against the governance
    budget (admission happens before quota accounting). With max_concurrent=1
    and max_queued=1 the bound is 2: exactly two of the four requests are
    admitted and counted; the two queue-rejected ones must not add anything."""
    small_gateway.backend.response_delay_seconds = 0.8
    before = small_gateway.usage.snapshot()["governance_requests_today"]
    try:
        await asyncio.gather(
            *[
                client.call_tool(
                    "blpapi_send_request",
                    {
                        "service": "//blp/refdata",
                        "operation": "ReferenceDataRequest",
                        "parameters": {"securities": [f"T{i}"], "fields": ["PX_LAST"]},
                        "execution": {"wait_seconds": 1, "overall_deadline_seconds": 30},
                    },
                )
                for i in range(4)
            ]
        )
        after = small_gateway.usage.snapshot()["governance_requests_today"]
        assert after - before == 2, f"queue-rejected requests consumed budget: {after - before}"
    finally:
        small_gateway.backend.response_delay_seconds = 0.0
