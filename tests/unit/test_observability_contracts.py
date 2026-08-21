"""CS10 regression tests: observability single-counts, per-tool output
schemas, and the Linux single-instance lock (O + Phase-1 lock requirement).

Failing against 0445a48: mcp_tool_calls_total counts twice; the reconnect
metric increments on startup; all tools share one generic output schema; the
non-Windows instance lock permits duplicate binds (SO_REUSEADDR).
"""

from __future__ import annotations

import pytest

from bloomberg_mcp.models import SessionState


def _counter(metrics, name: str, **labels: str) -> float:
    key = metrics._key(name, labels)  # type: ignore[attr-defined]
    return metrics._counters.get(key, 0.0)  # type: ignore[attr-defined]


async def test_tool_calls_metric_counts_once(running_gateway, mcp_client) -> None:
    """O1: one send_request must increment mcp_tool_calls_total exactly once."""
    before = _counter(running_gateway.metrics, "mcp_tool_calls_total", tool="blpapi_send_request")
    await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["AAPL US Equity"], "fields": ["PX_LAST"]},
            "execution": {"wait_seconds": 5},
        },
    )
    after = _counter(running_gateway.metrics, "mcp_tool_calls_total", tool="blpapi_send_request")
    assert after == before + 1


async def test_reconnect_metric_only_counts_reconnects(running_gateway) -> None:
    """O2: the reconnect counter must ignore the initial CONNECTED event."""
    await running_gateway._on_session_event(SessionState.CONNECTED, 1)
    assert _counter(running_gateway.metrics, "blpapi_session_reconnects_total") == 0
    await running_gateway._on_session_event(SessionState.CONNECTED, 2)
    assert _counter(running_gateway.metrics, "blpapi_session_reconnects_total") == 1


async def test_per_tool_output_schemas_advertised(mcp_client) -> None:
    """O8: tools/list must expose a per-tool data contract, not one generic."""
    response = await mcp_client.list_tools()
    tools = response.json().get("result", {}).get("tools") or []
    schemas = {tool["name"]: tool.get("outputSchema") for tool in tools}
    send = schemas["blpapi_send_request"]
    assert send is not None
    assert send.get("properties", {}).get("data") is not None
    # The send tool's data contract carries the request-result shape.
    data_props = send["properties"]["data"].get("properties", {})
    assert "status" in data_props and "request_id" in data_props


async def test_instance_lock_holds_on_linux() -> None:
    """Phase-1 requirement: the Linux single-instance lock must actually lock."""
    import os

    from bloomberg_mcp.instance_lock import InstanceLock, InstanceLockHeld

    name = f"Local\\BloombergMCP.Test.{os.getpid()}"
    first = InstanceLock(name)
    second = InstanceLock(name)
    first.acquire()
    try:
        with pytest.raises(InstanceLockHeld):
            second.acquire()
    finally:
        first.release()
        second.release()
