"""CS1 regression tests: durable final results, record expiry, message release.

Failing against 0445a48: finalized payloads live in a closure-local outcome
dict; polling/replay only see snapshots; completed records keep every message
and never expire.
"""

from __future__ import annotations

import asyncio

import pytest

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.mcp.curated_tools import get_reference_data
from bloomberg_mcp.models import utc_now
from bloomberg_mcp.registry.requests_registry import RequestRegistry

pytestmark = pytest.mark.asyncio


async def _poll_until_completed(mcp_client, request_id: str, tries: int = 60) -> dict:
    state = {}
    for _ in range(tries):
        await asyncio.sleep(0.1)
        resp = await mcp_client.call_tool("blpapi_get_request", {"request_id": request_id, "include_preview": False})
        state = mcp_client.structured(resp)["data"]
        if state["status"] in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"):
            break
    return state


async def test_pending_then_complete_poll_returns_data(mcp_client, running_gateway) -> None:
    """A request that outlives the wait window must still expose its result via poll."""
    running_gateway.backend.response_delay_seconds = 1.5
    try:
        resp = await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 1},
            },
        )
        structured = mcp_client.structured(resp)
        assert structured["data"]["pending"] is True
        request_id = structured["request_id"]
        state = await _poll_until_completed(mcp_client, request_id)
        assert state["status"] == "COMPLETED"
        # The durable result must be retrievable — not just a raw preview.
        assert "data" in state, "polled completed request must return its data"
        messages = state["data"]["messages"]
        assert len(messages) >= 1
        payload = messages[0]["payload"]
        assert payload["securityData"][0]["fieldData"]["PX_LAST"] is not None
    finally:
        running_gateway.backend.response_delay_seconds = 0.0


async def test_completed_replay_returns_data(mcp_client) -> None:
    """Idempotent replay of a completed request returns the same final result."""
    first = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            "client_request_id": "cs1-replay-1",
        },
    )
    first_data = mcp_client.structured(first)["data"]
    assert first_data["status"] == "COMPLETED"
    second = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            "client_request_id": "cs1-replay-1",
        },
    )
    second_data = mcp_client.structured(second)["data"]
    assert second_data["idempotent_replay"] is True
    assert "data" in second_data, "replay of a completed request must include its result"
    assert second_data["data"] == first_data["data"]


async def test_record_messages_released_after_finalize(mcp_client, running_gateway) -> None:
    """After finalization the record must release raw messages but keep errors."""
    resp = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
        },
    )
    request_id = mcp_client.structured(resp)["request_id"]
    record = running_gateway.request_registry.get(request_id, "hermes")
    assert record is not None
    assert record.status.value == "COMPLETED"
    assert record.messages == [], "completed records must release raw canonical messages"
    assert record.final_result is not None
    assert record.final_result.data is not None


async def test_curated_timeout_includes_request_id(running_gateway) -> None:
    """Curated TIMEOUT must surface the request handle so the client can poll."""
    principal = Principal(principal_id="hermes", scopes=frozenset({"bloomberg:reference"}))

    async def fake_submit(*args, **kwargs):  # noqa: ANN002, ANN003
        return {"pending": True, "request_id": "req_cs1_timeout"}

    running_gateway.executor.submit = fake_submit  # type: ignore[method-assign]
    with pytest.raises(GatewayError) as excinfo:
        await get_reference_data(running_gateway, principal, {"securities": ["A"], "fields": ["PX_LAST"]})
    assert excinfo.value.code is ErrorCode.TIMEOUT
    assert excinfo.value.details.get("request_id") == "req_cs1_timeout"


async def test_sweep_evicts_expired_records() -> None:
    """sweep() must evict completed records past their TTL, not just dedup keys."""
    from bloomberg_mcp.models import RequestRecord, RequestStatus

    registry = RequestRegistry()
    now = utc_now()
    from datetime import timedelta

    old = RequestRecord(
        request_id="req_old",
        principal_id="hermes",
        client_request_id=None,
        service="//blp/refdata",
        operation="ReferenceDataRequest",
        schema_hash="h",
        parameters_hash="p",
        created_at=now - timedelta(seconds=10_000),
        deadline=now - timedelta(seconds=9_000),
        status=RequestStatus.COMPLETED,
        expires_at=now - timedelta(seconds=1),
    )
    fresh = RequestRecord(
        request_id="req_fresh",
        principal_id="hermes",
        client_request_id=None,
        service="//blp/refdata",
        operation="ReferenceDataRequest",
        schema_hash="h",
        parameters_hash="p",
        created_at=now,
        deadline=now + timedelta(seconds=60),
        status=RequestStatus.QUEUED,
    )
    registry.register(old)
    registry.register(fresh)
    removed = registry.sweep(record_ttl_seconds=3600)
    assert removed == 1
    assert registry.get("req_old", "hermes") is None
    assert registry.get("req_fresh", "hermes") is not None
