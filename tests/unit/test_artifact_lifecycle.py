"""CS7 regression tests: artifact and result completeness (finding L).

Failing against 0445a48: normalized results bypass inline_result_bytes;
artifact_format is a dead option; page reads load whole artifacts; file
metadata disappears across restart (orphaned files); parquet/arrow are
declared but never produced; tool results carry URI strings instead of MCP
resource-link content blocks.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime

import httpx2
import pytest
import pytest_asyncio

from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.config import load_gateway_config
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.server import build_app
from bloomberg_mcp.policy.models import load_policy_config
from bloomberg_mcp.storage.file_store import FileStore
from bloomberg_mcp.storage.models import ArtifactInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Client:
    def __init__(self, app, token: str) -> None:
        transport = httpx2.ASGITransport(app=app)
        self.client = httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "mcp-method": "tools/call",
        }

    async def call_tool(self, name: str, args: dict) -> dict:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
                "name": name,
                "arguments": args,
            },
        }
        self.headers["mcp-name"] = name
        response = await self.client.post("/mcp", json=body, headers=self.headers)
        return response.json()

    def structured(self, response: dict) -> dict:
        return response["result"]["structuredContent"]


def _make_gateway(tmp_path) -> Gateway:
    config = load_gateway_config(os.path.join(REPO_ROOT, "config", "default.yaml"))
    config = replace(
        config,
        backend="fake",
        storage=replace(config.storage, directory=str(tmp_path / "data"), enabled=True),
        requests=replace(config.requests, inline_result_bytes=256),
    )
    policy = load_policy_config(os.path.join(REPO_ROOT, "config", "policy.example.yaml"))
    return Gateway(config, policy)


@pytest_asyncio.fixture
async def small_gateway(tmp_path) -> asyncio.Task:
    import asyncio

    gateway = _make_gateway(tmp_path)
    verifier = TokenVerifier(gateway.config.auth, gateway.policy_config)
    app = build_app(gateway, verifier)
    gateway._test_app = app  # type: ignore[attr-defined]

    started = asyncio.Event()
    stop = asyncio.Event()
    error: list[BaseException] = []

    async def host() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001
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
async def client(small_gateway):
    c = _Client(small_gateway._test_app, "test-bearer-" + "x" * 48)  # type: ignore[attr-defined]
    yield c
    await c.client.aclose()


def _fixture_info(result_id: str) -> ArtifactInfo:
    return ArtifactInfo(
        result_id=result_id,
        principal_id="hermes",
        representation="normalized",
        format="json",
        content_type="application/json",
        byte_count=5,
        message_count=1,
        sha256="x",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        backend="file",
    )


async def _submit(client: _Client, *, execution: dict | None = None) -> dict:
    args = {
        "service": "//blp/refdata",
        "operation": "ReferenceDataRequest",
        "parameters": {
            "securities": ["AAPL US Equity"],
            "fields": ["PX_LAST", "CRNCY", "VOLUME", "PX_OPEN", "PX_HIGH"],
        },
        "execution": {"wait_seconds": 5, "response_mode": "normalized", **(execution or {})},
    }
    response = await client.call_tool("blpapi_send_request", args)
    data = client.structured(response)["data"]
    assert data["status"] == "COMPLETED"
    return data


async def test_normalized_large_result_becomes_artifact(client: _Client, small_gateway: Gateway) -> None:
    """Normalized output beyond inline_result_bytes must be an artifact + preview."""
    data = await _submit(client)
    artifact = data.get("artifact")
    assert artifact and artifact.get("result_id")  # large normalized payload was spooled
    preview = data.get("preview")
    assert preview is not None
    assert "rows" in preview  # bounded preview with the table shape


async def test_artifact_format_jsonl_forced(client: _Client, small_gateway: Gateway) -> None:
    """artifact_format=jsonl forces a JSONL artifact even for small results."""
    data = await _submit(client, execution={"artifact_format": "jsonl"})
    artifact = data.get("artifact")
    assert artifact and artifact.get("result_id")
    info = small_gateway.result_store.metadata(artifact["result_id"], "hermes")
    assert info["format"] == "jsonl"


def test_parquet_arrow_removed(small_gateway: Gateway) -> None:
    with pytest.raises(GatewayError) as excinfo:
        small_gateway.result_store.put("hermes", "normalized", "parquet", b"x", 1, 60)
    assert excinfo.value.code is ErrorCode.ARTIFACT_FORMAT_NOT_AVAILABLE


def test_file_store_manifest_reconstruction(tmp_path) -> None:
    store = FileStore(str(tmp_path / "data"), 1_000_000)
    store.put(_fixture_info("res_a1"), b"hello")
    # New instance over the same directory: metadata must be reconstructed.
    store2 = FileStore(str(tmp_path / "data"), 1_000_000)
    entry = store2.get("res_a1")
    assert entry is not None and entry[1] == b"hello"


def test_file_store_startup_sweep_without_manifest(tmp_path) -> None:
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    stray = root / "res_orphaned.json"
    stray.write_bytes(b"{}")
    FileStore(str(root), 1_000_000)
    assert not stray.exists()  # no manifest => stale artifacts are deleted
