"""Fake-backend scenario coverage (SPEC §5.8) at the gateway level."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_multiple_partial_responses_combined(mcp_client, running_gateway) -> None:
    response = await mcp_client.call_tool(
        "get_reference_data",
        {"securities": ["A EQUITY", "B EQUITY", "C EQUITY"], "fields": ["PX_LAST"]},
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is True
    securities = {row["security"] for row in structured["data"]["rows"]}
    assert securities == {"A EQUITY", "B EQUITY", "C EQUITY"}
    # The executor combined partial + final responses.
    record = running_gateway.request_registry.get(structured["request_id"], "hermes")
    assert record is not None and record.partial_response_count >= 1


async def test_wait_timeout_returns_handle_then_completes(mcp_client, running_gateway) -> None:
    running_gateway.backend.response_delay_seconds = 1.5
    try:
        response = await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 1},
            },
        )
        structured = mcp_client.structured(response)
        assert structured["data"]["pending"] is True
        request_id = structured["request_id"]
        # Request remains active and completes in the background.
        for _ in range(50):
            await asyncio.sleep(0.1)
            status = await mcp_client.call_tool(
                "blpapi_get_request", {"request_id": request_id, "include_preview": False}
            )
            state = mcp_client.structured(status)["data"]["status"]
            if state == "COMPLETED":
                break
        assert state == "COMPLETED"
    finally:
        running_gateway.backend.response_delay_seconds = 0.0


async def test_overall_deadline_times_out(mcp_client, running_gateway) -> None:
    running_gateway.backend.response_delay_seconds = 10.0
    try:
        response = await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 1, "overall_deadline_seconds": 2},
            },
        )
        request_id = mcp_client.structured(response)["request_id"]
        for _ in range(60):
            await asyncio.sleep(0.1)
            status = await mcp_client.call_tool("blpapi_get_request", {"request_id": request_id})
            data = mcp_client.structured(status)["data"]
            if data["status"] == "TIMED_OUT":
                break
        assert data["status"] == "TIMED_OUT"
        assert data["error"]["code"] == "TIMEOUT"
    finally:
        running_gateway.backend.response_delay_seconds = 0.0


async def test_session_disconnect_fails_request_without_replay(mcp_client, running_gateway) -> None:
    running_gateway.backend.response_delay_seconds = 2.0
    try:
        response = await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 1},
            },
        )
        request_id = mcp_client.structured(response)["request_id"]
        await running_gateway.backend.simulate_session_loss()
        for _ in range(50):
            await asyncio.sleep(0.1)
            status = await mcp_client.call_tool("blpapi_get_request", {"request_id": request_id})
            data = mcp_client.structured(status)["data"]
            if data["status"] == "FAILED":
                break
        assert data["status"] == "FAILED"
        assert data["error"]["code"] == "BLOOMBERG_SESSION_LOST"
        assert data["error"]["retryable"] is True
    finally:
        running_gateway.backend.response_delay_seconds = 0.0


async def test_reconnect_reopens_services_and_bumps_generation(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    generation_before = backend.session_generation
    await backend.simulate_session_loss()
    await backend.simulate_reconnect()
    assert backend.session_generation == generation_before + 1
    assert backend.service_states()["//blp/refdata"] is True
    # Requests work again after reconnect.
    response = await mcp_client.call_tool(
        "get_reference_data", {"securities": ["A EQUITY"], "fields": ["PX_LAST"]}
    )
    assert mcp_client.structured(response)["ok"] is True


async def test_schema_drift_after_reconnect_with_schema_change(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    describe = await mcp_client.call_tool(
        "blpapi_describe_operation", {"service": "//blp/refdata", "operation": "HistoricalDataRequest"}
    )
    old_hash = mcp_client.structured(describe)["data"]["schema_hash"]
    await backend.simulate_session_loss()
    await backend.simulate_reconnect(schema_change=True)
    response = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "HistoricalDataRequest",
            "schema_hash": old_hash,
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"], "startDate": "20260101", "endDate": "20260105"},
            "execution": {"wait_seconds": 5},
        },
    )
    structured = mcp_client.structured(response)
    assert structured["ok"] is False
    assert structured["error"]["code"] == "SCHEMA_DRIFT_DETECTED"


async def test_subscription_lifecycle_and_resubscription(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    create = await mcp_client.call_tool(
        "blpapi_subscribe",
        {"subscriptions": [{"topic": "A EQUITY", "fields": ["LAST_PRICE"]}]},
    )
    structured = mcp_client.structured(create)
    subscription_id = structured["data"]["subscription_id"]
    generation = structured["data"]["generation"]
    token = next(iter(running_gateway.subscriptions._token_map))

    await backend.emit_market_data(token, {"LAST_PRICE": 1.0})
    await asyncio.sleep(0.05)
    read = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": subscription_id, "mode": "changes"}
    )
    read_data = mcp_client.structured(read)["data"]
    cursor = read_data["cursor"]
    assert read_data["events"]

    # Resubscription replaces the group: new generation, old cursor invalid.
    resub = await mcp_client.call_tool(
        "blpapi_resubscribe",
        {
            "subscription_id": subscription_id,
            "subscriptions": [{"topic": "B EQUITY", "fields": ["LAST_PRICE"]}],
        },
    )
    resub_data = mcp_client.structured(resub)["data"]
    assert resub_data["subscription_id"] == subscription_id
    assert resub_data["generation"] == generation + 1

    stale = await mcp_client.call_tool(
        "blpapi_read_subscription",
        {"subscription_id": subscription_id, "mode": "changes", "cursor": cursor},
    )
    stale_structured = mcp_client.structured(stale)
    assert stale_structured["ok"] is False
    assert stale_structured["error"]["code"] == "CURSOR_INVALID"

    cancel = await mcp_client.call_tool("blpapi_cancel_subscription", {"subscription_id": subscription_id})
    assert mcp_client.structured(cancel)["data"]["status"] == "CANCELLED"
    # Cancellation is idempotent.
    again = await mcp_client.call_tool("blpapi_cancel_subscription", {"subscription_id": subscription_id})
    assert mcp_client.structured(again)["ok"] is True


async def test_subscription_item_failure_partial_group(mcp_client, running_gateway) -> None:
    create = await mcp_client.call_tool(
        "blpapi_subscribe",
        {
            "subscriptions": [
                {"topic": "GOOD EQUITY", "fields": ["LAST_PRICE"]},
                {"topic": "FAIL EQUITY", "fields": ["LAST_PRICE"]},
            ]
        },
    )
    data = mcp_client.structured(create)["data"]
    statuses = {item["topic"]: item["status"] for item in data["items"]}
    await asyncio.sleep(0.05)
    listed = await mcp_client.call_tool("blpapi_list_subscriptions", {})
    groups = mcp_client.structured(listed)["data"]["subscriptions"]
    group = next(g for g in groups if g["subscription_id"] == data["subscription_id"])
    item_status = {item["topic"]: item["status"] for item in group["items"]}
    assert item_status["FAIL EQUITY"] == "FAILED"
    assert item_status["GOOD EQUITY"] == "ACTIVE"
    assert group["status"] == "DEGRADED"
    del statuses


async def test_subscription_buffer_bounded_drops_counted(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    create = await mcp_client.call_tool(
        "blpapi_subscribe",
        {
            "subscriptions": [{"topic": "A EQUITY", "fields": ["LAST_PRICE"]}],
            "retention": {"max_events": 5},
        },
    )
    subscription_id = mcp_client.structured(create)["data"]["subscription_id"]
    token = next(iter(running_gateway.subscriptions._token_map))
    for i in range(20):
        await backend.emit_market_data(token, {"LAST_PRICE": float(i)})
    await asyncio.sleep(0.05)
    read = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": subscription_id, "mode": "changes"}
    )
    data = mcp_client.structured(read)["data"]
    assert len(data["events"]) <= 5
    assert data["dropped_events"] > 0


async def test_stale_generation_events_rejected(mcp_client, running_gateway) -> None:
    backend = running_gateway.backend
    create = await mcp_client.call_tool(
        "blpapi_subscribe",
        {"subscriptions": [{"topic": "A EQUITY", "fields": ["LAST_PRICE"]}]},
    )
    subscription_id = mcp_client.structured(create)["data"]["subscription_id"]
    old_token = next(iter(running_gateway.subscriptions._token_map))
    await mcp_client.call_tool(
        "blpapi_resubscribe",
        {
            "subscription_id": subscription_id,
            "subscriptions": [{"topic": "A EQUITY", "fields": ["LAST_PRICE"]}],
        },
    )
    # Event on the retired token must not reach the buffer.
    await backend.emit_stale_event(old_token)
    await asyncio.sleep(0.05)
    read = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": subscription_id, "mode": "changes", "wait_seconds": 0}
    )
    data = mcp_client.structured(read)["data"]
    assert all(e["payload"].get("LAST_PRICE") != 0.0 for e in data["events"])


async def test_entitlement_circuit_breaker_opens(mcp_client, running_gateway) -> None:
    threshold = running_gateway.config.governance.entitlement_failure_circuit_threshold
    for _ in range(threshold):
        await mcp_client.call_tool(
            "blpapi_send_request",
            {
                "service": "//blp/refdata",
                "operation": "ReferenceDataRequest",
                "parameters": {"securities": ["NOENTITLE X"], "fields": ["PX_LAST"]},
                "execution": {"wait_seconds": 5},
            },
        )
    assert running_gateway.quota.entitlement_circuit_open
    blocked = await mcp_client.call_tool(
        "blpapi_send_request",
        {
            "service": "//blp/refdata",
            "operation": "ReferenceDataRequest",
            "parameters": {"securities": ["A"], "fields": ["PX_LAST"]},
            "execution": {"wait_seconds": 5},
        },
    )
    structured = mcp_client.structured(blocked)
    assert structured["ok"] is False
    assert structured["error"]["code"] == "BLOOMBERG_NOT_ENTITLED"
    # Operator intervention resets the breaker.
    running_gateway.quota.reset_entitlement_circuit()
    ok = await mcp_client.call_tool(
        "get_reference_data", {"securities": ["A EQUITY"], "fields": ["PX_LAST"]}
    )
    assert mcp_client.structured(ok)["ok"] is True


async def test_daily_budget_exhaustion(mcp_client, running_gateway) -> None:
    # Force exhaustion by shrinking the daily budget (frozen config: replace).
    from dataclasses import replace

    running_gateway.quota._governance = replace(
        running_gateway.quota._governance, daily_request_budget=1
    )
    first = await mcp_client.call_tool(
        "get_reference_data", {"securities": ["A EQUITY"], "fields": ["PX_LAST"]}
    )
    assert mcp_client.structured(first)["ok"] is True
    second = await mcp_client.call_tool(
        "get_reference_data", {"securities": ["A EQUITY"], "fields": ["PX_LAST"]}
    )
    structured = mcp_client.structured(second)
    assert structured["ok"] is False
    assert structured["error"]["code"] == "LICENSE_BUDGET_EXCEEDED"
