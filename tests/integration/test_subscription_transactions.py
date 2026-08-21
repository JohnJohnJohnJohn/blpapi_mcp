"""CS4 regression tests: transactional subscriptions, async expiry teardown,
retryable unsubscribe, retention-mode enforcement, cursor consume-on-use + TTL.

Failing against 0445a48: expiry never unsubscribes natively; cancel swallows
unsubscribe failures; resubscribe destroys old state before success; cursors
never expire and old handles stay valid.
"""

from __future__ import annotations

from typing import Any

import pytest

from bloomberg_mcp.errors import ErrorCode, GatewayError

pytestmark = pytest.mark.asyncio

TOPIC = "EURUSD Curncy"
FIELDS = ["LAST_PRICE", "BID", "ASK"]


async def _subscribe(mcp_client, retention: dict[str, Any] | None = None) -> str:
    args: dict[str, Any] = {"subscriptions": [{"topic": TOPIC, "fields": FIELDS}]}
    if retention is not None:
        args["retention"] = retention
    response = await mcp_client.call_tool("blpapi_subscribe", args)
    data = mcp_client.structured(response)["data"]
    assert data.get("error") is None
    assert data.get("subscription_id")
    return data["subscription_id"]


async def test_expiry_unsubscribes_natively(running_gateway, mcp_client) -> None:
    """expire_due() must attempt a native unsubscribe, not just drop routing."""
    sub_id = await _subscribe(mcp_client)
    group = running_gateway.subscriptions._groups[sub_id].group  # type: ignore[attr-defined]
    from datetime import timedelta

    from bloomberg_mcp.models import utc_now

    group.expires_at = utc_now() - timedelta(seconds=1)
    backend = running_gateway.backend
    assert len(backend._active_subscriptions) >= 1  # type: ignore[attr-defined]
    expired = await running_gateway.subscriptions.expire_due()
    assert sub_id in expired
    assert len(backend._active_subscriptions) == 0  # type: ignore[attr-defined]


async def test_cancel_unsubscribe_failure_is_retryable(running_gateway, mcp_client) -> None:
    """A failed native unsubscribe during cancel must stay observable/retryable."""
    sub_id = await _subscribe(mcp_client)
    backend = running_gateway.backend

    real_unsubscribe = backend.unsubscribe
    calls = {"n": 0}

    async def flaky_unsubscribe(tokens):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network blip")

    backend.unsubscribe = flaky_unsubscribe  # type: ignore[method-assign]
    response = await mcp_client.call_tool("blpapi_cancel_subscription", {"subscription_id": sub_id})
    assert mcp_client.structured(response)["data"]["status"] == "CANCELLED"
    pending = running_gateway.subscriptions._pending_unsubscribes  # type: ignore[attr-defined]
    assert len(pending) >= 1

    backend.unsubscribe = real_unsubscribe  # type: ignore[method-assign]
    retried = await running_gateway.subscriptions.retry_pending_unsubscribes()
    assert retried >= 1
    assert len(running_gateway.subscriptions._pending_unsubscribes) == 0  # type: ignore[attr-defined]


async def test_resubscribe_failure_preserves_old_state(running_gateway, mcp_client) -> None:
    """A failed resubscribe must not destroy the old subscription state."""
    sub_id = await _subscribe(mcp_client)
    registry = running_gateway.subscriptions
    group = registry._groups[sub_id].group  # type: ignore[attr-defined]
    old_generation = group.generation
    old_item_ids = set(group.items)

    backend = running_gateway.backend

    async def failing_subscribe(items, tokens):  # noqa: ANN001
        raise RuntimeError("bloomberg down")

    backend.subscribe = failing_subscribe  # type: ignore[method-assign]
    with pytest.raises(GatewayError):
        await registry.resubscribe(
            "hermes", sub_id, [{"topic": TOPIC, "fields": ["LAST_PRICE"]}]
        )
    backend.subscribe = backend.__class__.subscribe  # type: ignore[attr-defined]
    # Old state intact: same generation, same items, still readable.
    assert group.generation == old_generation
    assert set(group.items) == old_item_ids
    response = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": sub_id, "mode": "latest", "wait_seconds": 1}
    )
    # Old state intact: the group is still readable and has not errored.
    assert mcp_client.structured(response).get("error") is None


async def test_resubscribe_enforces_field_limit(running_gateway, mcp_client) -> None:
    """resubscribe must enforce per-topic field limits like create does."""
    sub_id = await _subscribe(mcp_client)
    many_fields = [f"FIELD_{i}" for i in range(60)]
    with pytest.raises(Exception) as excinfo:
        await running_gateway.subscriptions.resubscribe(
            "hermes", sub_id, [{"topic": TOPIC, "fields": many_fields}]
        )

    assert excinfo.value.code == ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED


async def test_retention_mode_enforced(running_gateway, mcp_client) -> None:
    """retention.mode must gate which read modes are permitted."""
    sub_id = await _subscribe(mcp_client, retention={"mode": "latest_only", "max_events": 100})
    response = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": sub_id, "mode": "changes", "wait_seconds": 1}
    )
    err = mcp_client.structured(response).get("error") or {}
    assert err.get("code") == "INVALID_ARGUMENT"

    sub2 = await _subscribe(mcp_client, retention={"mode": "changes_only", "max_events": 100})
    response2 = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": sub2, "mode": "latest", "wait_seconds": 1}
    )
    err2 = mcp_client.structured(response2).get("error") or {}
    assert err2.get("code") == "INVALID_ARGUMENT"


async def test_cursor_consumed_on_use(running_gateway, mcp_client) -> None:
    """Reading with a cursor must consume it: the old handle becomes invalid."""
    sub_id = await _subscribe(mcp_client)
    response = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": sub_id, "mode": "changes", "wait_seconds": 3}
    )
    data = mcp_client.structured(response)["data"]
    cursor = data.get("cursor")
    assert cursor is not None
    # A read WITH the cursor consumes it and returns a fresh handle.
    response2 = await mcp_client.call_tool(
        "blpapi_read_subscription",
        {"subscription_id": sub_id, "mode": "changes", "cursor": cursor, "wait_seconds": 1},
    )
    assert mcp_client.structured(response2).get("error") is None
    new_cursor = mcp_client.structured(response2)["data"].get("cursor")
    assert new_cursor is not None and new_cursor != cursor
    # Replaying the ORIGINAL (now consumed) handle must fail.
    response3 = await mcp_client.call_tool(
        "blpapi_read_subscription",
        {"subscription_id": sub_id, "mode": "changes", "cursor": cursor, "wait_seconds": 1},
    )
    err = mcp_client.structured(response3).get("error") or {}
    assert err.get("code") == "CURSOR_INVALID"
    # A fresh read (no cursor) still works.
    response4 = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": sub_id, "mode": "changes", "wait_seconds": 1}
    )
    assert mcp_client.structured(response4)["data"].get("cursor") is not None


async def test_cursor_ttl_expires(running_gateway, mcp_client) -> None:
    """Cursors past their TTL must be rejected as CURSOR_INVALID."""
    from dataclasses import replace

    running_gateway.subscriptions._config = replace(  # type: ignore[attr-defined]
        running_gateway.subscriptions._config,  # type: ignore[attr-defined]
        cursor_ttl_seconds=1,
    )
    sub_id = await _subscribe(mcp_client)
    response = await mcp_client.call_tool(
        "blpapi_read_subscription", {"subscription_id": sub_id, "mode": "changes", "wait_seconds": 1}
    )
    cursor = mcp_client.structured(response)["data"].get("cursor")
    assert cursor is not None
    import asyncio

    await asyncio.sleep(1.3)
    response2 = await mcp_client.call_tool(
        "blpapi_read_subscription",
        {"subscription_id": sub_id, "mode": "changes", "cursor": cursor, "wait_seconds": 1},
    )
    err = mcp_client.structured(response2).get("error") or {}
    assert err.get("code") == "CURSOR_INVALID"
