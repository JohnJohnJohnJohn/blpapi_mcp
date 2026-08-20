"""Subscription tools (SPEC §3.9): subscribe, read, resubscribe, cancel, list."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.output_schemas import envelope
from bloomberg_mcp.mcp.tool_spec import ToolSpec
from bloomberg_mcp.models import SubscriptionGroup
from bloomberg_mcp.policy.engine import SCOPE_SUBSCRIBE

SUBSCRIPTION_SERVICE = "//blp/mktdata"


def _group_payload(group: SubscriptionGroup) -> dict[str, Any]:
    return {
        "subscription_id": group.subscription_id,
        "generation": group.generation,
        "status": group.status.value,
        "items": [
            {
                "item_id": item.item_id,
                "topic": item.topic,
                "fields": list(item.fields),
                "status": item.status.value,
                "sequence": item.sequence,
            }
            for item in group.items.values()
        ],
        "expires_at": group.expires_at.isoformat() if group.expires_at else None,
        "dropped_events": group.dropped_events,
        "restored_with_gap": group.restored_with_gap,
    }


def _subscriptions_argument(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    subscriptions = arguments.get("subscriptions")
    if not isinstance(subscriptions, list) or not subscriptions:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "subscriptions must be a non-empty array.")
    for entry in subscriptions:
        if not isinstance(entry, dict) or not isinstance(entry.get("topic"), str):
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Each subscription requires a topic string.")
        fields = entry.get("fields")
        if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)):
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "fields must be an array of strings.")
        options = entry.get("options")
        if options is not None and not isinstance(options, dict):
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "options must be an object.")
    return subscriptions


async def subscribe(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    gateway.policy.authorize_subscription_service(principal, SUBSCRIPTION_SERVICE)
    subscriptions = _subscriptions_argument(arguments)
    gateway.quota.admit_subscription(
        principal.principal_id, gateway.subscriptions.active_group_count(principal.principal_id), len(subscriptions)
    )
    retention = arguments.get("retention")
    if retention is not None and not isinstance(retention, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "retention must be an object.")
    group = await gateway.subscriptions.create(principal.principal_id, subscriptions, retention)
    gateway.metrics.inc("blpapi_subscription_events_total", kind="subscribe")
    return envelope(ok=True, data=_group_payload(group))


async def read_subscription(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    if not principal.has_scope(SCOPE_SUBSCRIBE):
        raise GatewayError(ErrorCode.AUTH_FORBIDDEN, "Missing scope bloomberg:subscribe.")
    subscription_id = arguments.get("subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "subscription_id is required.")
    mode = str(arguments.get("mode", "changes"))
    wait_seconds = float(arguments.get("wait_seconds", 0) or 0)
    if wait_seconds < 0:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "wait_seconds must be >= 0.")
    limit = int(arguments.get("limit", 1000))
    generation = arguments.get("generation")
    if generation is not None and not isinstance(generation, int):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "generation must be an integer when provided.")
    data = await gateway.subscriptions.read(
        principal.principal_id,
        subscription_id,
        generation,
        mode,
        arguments.get("cursor"),
        limit,
        wait_seconds,
        admin=principal.admin,
    )
    return envelope(ok=True, data=data)


async def resubscribe(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    gateway.policy.authorize_subscription_service(principal, SUBSCRIPTION_SERVICE)
    subscription_id = arguments.get("subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "subscription_id is required.")
    subscriptions = _subscriptions_argument(arguments)
    group = await gateway.subscriptions.resubscribe(
        principal.principal_id, subscription_id, subscriptions, admin=principal.admin
    )
    warnings = [
        {
            "code": "SUBSCRIPTION_DATA_GAP",
            "message": "Resubscription replaced the group; a data gap may exist and old cursors are invalid.",
        }
    ]
    return envelope(ok=True, data=_group_payload(group), warnings=warnings)


async def cancel_subscription(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    if not principal.has_scope(SCOPE_SUBSCRIBE):
        raise GatewayError(ErrorCode.AUTH_FORBIDDEN, "Missing scope bloomberg:subscribe.")
    subscription_id = arguments.get("subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "subscription_id is required.")
    group = await gateway.subscriptions.cancel(principal.principal_id, subscription_id, admin=principal.admin)
    return envelope(ok=True, data=_group_payload(group))


async def list_subscriptions(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    if not principal.has_scope(SCOPE_SUBSCRIBE):
        raise GatewayError(ErrorCode.AUTH_FORBIDDEN, "Missing scope bloomberg:subscribe.")
    groups = gateway.subscriptions.list_groups(principal.principal_id, admin=principal.admin)
    return envelope(ok=True, data={"subscriptions": [_group_payload(g) for g in groups]})


_SUBSCRIPTION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "minLength": 1},
        "fields": {"type": "array", "items": {"type": "string"}},
        "options": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["topic", "fields"],
    "additionalProperties": False,
}

_RETENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["latest_and_changes", "changes_only", "latest_only"]},
        "max_events": {"type": "integer", "minimum": 1},
        "ttl_seconds": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
}

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="blpapi_subscribe",
        title="Subscribe to market data",
        description="Create a subscription group of market-data topics with bounded buffering.",
        input_schema={
            "type": "object",
            "properties": {
                "subscriptions": {"type": "array", "items": _SUBSCRIPTION_ITEM_SCHEMA, "minItems": 1},
                "retention": _RETENTION_SCHEMA,
            },
            "required": ["subscriptions"],
            "additionalProperties": False,
        },
        scope=SCOPE_SUBSCRIBE,
        handler=subscribe,
    ),
    ToolSpec(
        name="blpapi_read_subscription",
        title="Read subscription updates",
        description="Read latest values or buffered changes for a subscription group; supports long-polling.",
        input_schema={
            "type": "object",
            "properties": {
                "subscription_id": {"type": "string"},
                "generation": {"type": ["integer", "null"]},
                "mode": {"type": "string", "enum": ["latest", "changes"], "default": "changes"},
                "cursor": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "wait_seconds": {"type": "number", "minimum": 0},
            },
            "required": ["subscription_id"],
            "additionalProperties": False,
        },
        scope=SCOPE_SUBSCRIBE,
        handler=read_subscription,
        read_only=True,
    ),
    ToolSpec(
        name="blpapi_resubscribe",
        title="Replace a subscription group",
        description=(
            "Replace the complete definition of a subscription group. Preserves the group id, increments "
            "the generation, invalidates cursors and clears buffers."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subscription_id": {"type": "string"},
                "subscriptions": {"type": "array", "items": _SUBSCRIPTION_ITEM_SCHEMA, "minItems": 1},
            },
            "required": ["subscription_id", "subscriptions"],
            "additionalProperties": False,
        },
        scope=SCOPE_SUBSCRIBE,
        handler=resubscribe,
    ),
    ToolSpec(
        name="blpapi_cancel_subscription",
        title="Cancel a subscription group",
        description="Cancel a subscription group. Idempotent and principal-bound.",
        input_schema={
            "type": "object",
            "properties": {"subscription_id": {"type": "string"}},
            "required": ["subscription_id"],
            "additionalProperties": False,
        },
        scope=SCOPE_SUBSCRIBE,
        handler=cancel_subscription,
        idempotent=True,
    ),
    ToolSpec(
        name="blpapi_list_subscriptions",
        title="List subscriptions",
        description="List subscription groups owned by the caller (all groups with admin scope).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        scope=SCOPE_SUBSCRIBE,
        handler=list_subscriptions,
        read_only=True,
    ),
]
