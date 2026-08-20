"""Registry behaviors: idempotency, ownership, cursors, TTL (SPEC §2.10-§2.11, §5.7)."""

from __future__ import annotations

from datetime import timedelta

from bloomberg_mcp.models import (
    RequestRecord,
    RequestStatus,
    SubscriptionGroup,
    SubscriptionGroupStatus,
    SubscriptionItem,
    SubscriptionItemStatus,
    utc_now,
)
from bloomberg_mcp.registry.cursors import CursorRegistry
from bloomberg_mcp.registry.requests_registry import RequestRegistry


def _record(request_id: str, principal: str = "hermes") -> RequestRecord:
    return RequestRecord(
        request_id=request_id,
        principal_id=principal,
        client_request_id=None,
        service="//blp/refdata",
        operation="ReferenceDataRequest",
        schema_hash="sha256:x",
        parameters_hash="y",
        created_at=utc_now(),
        deadline=utc_now() + timedelta(seconds=60),
    )


def test_idempotency_window() -> None:
    registry = RequestRegistry()
    registry.dedupe_register("hermes", "job-1", "req_a", window_seconds=300)
    assert registry.dedupe_lookup("hermes", "job-1") == "req_a"
    # Different principal cannot replay another principal's id.
    assert registry.dedupe_lookup("other", "job-1") is None


def test_idempotency_expiry() -> None:
    registry = RequestRegistry()
    registry.dedupe_register("hermes", "job-1", "req_a", window_seconds=-1)
    assert registry.dedupe_lookup("hermes", "job-1") is None


def test_principal_ownership() -> None:
    registry = RequestRegistry()
    registry.register(_record("req_1", "hermes"))
    assert registry.get("req_1", "hermes") is not None
    assert registry.get("req_1", "mallory") is None  # cross-principal: absent
    assert registry.get("req_1", "mallory", admin=True) is not None


def test_state_transition_counts() -> None:
    registry = RequestRegistry()
    record = _record("req_2")
    record.status = RequestStatus.SENT
    registry.register(record)
    assert registry.active_count() == 1
    assert registry.queued_count() == 0
    record.status = RequestStatus.QUEUED
    assert registry.queued_count() == 1


def test_cursor_generation_invalidation() -> None:
    cursors = CursorRegistry()
    cursor = cursors.create("sub_1", generation=1, offset=0)
    assert cursors.resolve(cursor.cursor_id) is not None
    cursors.invalidate_subscription("sub_1")
    assert cursors.resolve(cursor.cursor_id) is None


def test_cursor_advance_replaces_handle() -> None:
    cursors = CursorRegistry()
    cursor = cursors.create("sub_1", generation=1, offset=0)
    advanced = cursors.consume(cursor, new_offset=5)
    assert cursors.resolve(cursor.cursor_id) is None
    assert cursors.resolve(advanced.cursor_id).offset == 5  # type: ignore[union-attr]


def test_subscription_group_generation_fields() -> None:
    group = SubscriptionGroup(
        subscription_id="sub_x",
        principal_id="hermes",
        generation=1,
        status=SubscriptionGroupStatus.STARTING,
    )
    group.items["i1"] = SubscriptionItem(
        item_id="i1", topic="700 HK Equity", fields=("LAST_PRICE",), options={}, native_token=1
    )
    assert group.items["i1"].status is SubscriptionItemStatus.CREATED
    group.generation += 1
    assert group.generation == 2
