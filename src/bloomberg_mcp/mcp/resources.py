"""MCP resources with structural URI parsing and ownership (SPEC §3.11).

Service names such as ``//blp/refdata`` are percent-encoded inside URIs or
passed as encoded query parameters; URIs are parsed structurally, never via
string concatenation into paths.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.blp.schema_converter import descriptor_to_json_schema
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway

SCHEME_BLOOMBERG = "bloomberg"
SCHEME_RESULT = "bloomberg-result"
SCHEME_SUBSCRIPTION = "bloomberg-subscription"

STATIC_RESOURCES = [
    {
        "uri": "bloomberg://services",
        "name": "Known Bloomberg services",
        "description": "Services known to the gateway with open/discovery/execution state.",
        "mimeType": "application/json",
    }
]

RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "bloomberg://service/{service}",
        "name": "Service descriptor",
        "description": "Descriptor for one Bloomberg service (percent-encoded service name).",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "bloomberg://operation{?service,name}",
        "name": "Operation schema",
        "description": "Request/response schema for one operation (encoded query parameters).",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "bloomberg-result://{resultId}/metadata",
        "name": "Result metadata",
        "description": "Metadata for a principal-owned stored result.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "bloomberg-result://{resultId}/page/{page}",
        "name": "Result page",
        "description": "Bounded page of a stored result.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "bloomberg-subscription://{subscriptionId}/latest",
        "name": "Subscription latest values",
        "description": "Latest buffered values for a principal-owned subscription group.",
        "mimeType": "application/json",
    },
]


def list_resources_payload() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return STATIC_RESOURCES, RESOURCE_TEMPLATES


async def read_resource(gateway: Gateway, principal: Principal, uri: str) -> tuple[str, str]:
    parts = urlsplit(uri)
    if parts.scheme == SCHEME_BLOOMBERG:
        return await _read_bloomberg_resource(gateway, principal, parts.netloc + parts.path, parts.query)
    if parts.scheme == SCHEME_RESULT:
        return _read_result_resource(gateway, principal, parts.netloc, parts.path)
    if parts.scheme == SCHEME_SUBSCRIPTION:
        return await _read_subscription_resource(gateway, principal, parts.netloc, parts.path)
    raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unsupported resource URI scheme {parts.scheme!r}.")


async def _read_bloomberg_resource(
    gateway: Gateway, principal: Principal, host_path: str, query: str
) -> tuple[str, str]:
    path = unquote(host_path).rstrip("/")
    if path in ("services", "/services", ""):
        gateway.policy.authorize_discovery(principal, _first_configured_service(gateway))
        payload = [s.__dict__ for s in gateway.backend.list_service_summaries()]
        return _json(payload)
    if path.startswith("service/"):
        service = path[len("service/") :]
        gateway.policy.authorize_discovery(principal, service)
        descriptor = _service_descriptor(gateway, service)
        return _json(descriptor)
    if path.startswith("operation"):
        params = parse_qs(query)
        service = unquote(params.get("service", [""])[0])
        operation = unquote(params.get("name", [""])[0])
        if not service or not operation:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "operation resource requires service and name parameters.")
        gateway.policy.authorize_operation_discovery(principal, service, operation)
        op_descriptor = gateway.backend.get_operation(service, operation)
        op_payload = {
            "service": service,
            "operation": operation,
            "schema_hash": op_descriptor.schema_hash,
            "request_schema": (
                descriptor_to_json_schema(op_descriptor.request) if op_descriptor.request else None
            ),
            "response_schemas": [descriptor_to_json_schema(r) for r in op_descriptor.responses],
            "policy": gateway.policy.describe_operation_policy(service, operation),
        }
        return _json(op_payload)
    raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unknown bloomberg resource path {path!r}.")


def _first_configured_service(gateway: Gateway) -> str:
    for name in gateway.policy_config.services:
        return name
    return "//blp/refdata"


def _service_descriptor(gateway: Gateway, service: str) -> dict[str, Any]:
    summaries = {s.name: s for s in gateway.backend.list_service_summaries()}
    summary = summaries.get(service)
    if summary is None:
        raise GatewayError(ErrorCode.INVALID_SERVICE, f"Service {service!r} is not known to the gateway.")
    operations = gateway.backend.list_operations(service)
    return {
        "name": service,
        "opened": summary.opened,
        "operation_count": len(operations),
        "operations": [
            {"operation": o.operation, "schema_hash": o.schema_hash, "description": o.description}
            for o in operations
        ],
        "session_generation": summary.session_generation,
    }


def _read_result_resource(gateway: Gateway, principal: Principal, result_id: str, path: str) -> tuple[str, str]:
    segments = [s for s in path.split("/") if s]
    if not result_id or len(segments) not in (1, 2):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Malformed bloomberg-result URI.")
    if segments[0] == "metadata" and len(segments) == 1:
        metadata = gateway.result_store.metadata(result_id, principal.principal_id, admin=principal.admin)
        return _json(metadata)
    if segments[0] == "page" and len(segments) == 2:
        if not segments[1].isdigit() or int(segments[1]) < 1:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Page must be a positive integer.")
        page = gateway.result_store.get_page(
            result_id, principal.principal_id, int(segments[1]), 500, admin=principal.admin
        )
        return _json(page)
    raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Unknown bloomberg-result resource kind.")


async def _read_subscription_resource(
    gateway: Gateway, principal: Principal, subscription_id: str, path: str
) -> tuple[str, str]:
    if path.strip("/") != "latest":
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Unknown bloomberg-subscription resource kind.")
    data = await gateway.subscriptions.read(
        principal.principal_id,
        subscription_id,
        None,
        "latest",
        None,
        100,
        0,
        admin=principal.admin,
    )
    return _json(data)


def _json(payload: Any) -> tuple[str, str]:
    return json.dumps(payload, indent=None, separators=(",", ":"), default=str), "application/json"
