"""Discovery tools (SPEC §3.4): services, operations, schemas, validation."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.blp.schema_converter import descriptor_to_json_schema
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.canonical import build_canonical_request
from bloomberg_mcp.mcp.output_schemas import envelope, with_data
from bloomberg_mcp.mcp.tool_spec import ToolSpec
from bloomberg_mcp.policy.engine import SCOPE_DISCOVER


async def list_services(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    if not principal.has_scope(SCOPE_DISCOVER):
        raise GatewayError(ErrorCode.AUTH_FORBIDDEN, "Missing scope bloomberg:discover.")
    include_unopened = bool(arguments.get("include_unopened", False))
    services = []
    for summary in gateway.backend.list_service_summaries():
        service_policy = gateway.policy.config.services.get(summary.name)
        discover_allowed = bool(service_policy and service_policy.discover)
        execute_allowed = bool(service_policy and any(op.execute for op in service_policy.operations.values()))
        if not discover_allowed:
            continue
        if not include_unopened and not summary.opened:
            continue
        operations = gateway.backend.list_operations(summary.name) if summary.opened else []
        services.append(
            {
                "name": summary.name,
                "opened": summary.opened,
                "discover_allowed": discover_allowed,
                "execute_allowed": execute_allowed,
                "operation_count": len(operations) if operations else summary.operation_count,
                "schema_hash": summary.schema_hash,
                "session_generation": summary.session_generation,
            }
        )
    return envelope(ok=True, data={"services": services})


async def open_service(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    service = _require_str(arguments, "service")
    gateway.policy.authorize_discovery(principal, service)
    gateway.policy.authorize_open(service)
    await gateway.backend.open_service(service)
    return envelope(ok=True, data={"service": service, "opened": True})


async def describe_service(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    service = _require_str(arguments, "service")
    gateway.policy.authorize_discovery(principal, service)
    open_if_allowed = bool(arguments.get("open_if_allowed", True))
    include_operations = bool(arguments.get("include_operations", True))
    include_event_schemas = bool(arguments.get("include_event_schemas", False))

    service_policy = gateway.policy.config.services.get(service)
    if service_policy is None:
        raise GatewayError(ErrorCode.INVALID_SERVICE, f"Service {service!r} is not configured.")
    opened = gateway.backend.service_states().get(service, False)
    if not opened and open_if_allowed and service_policy.open:
        await gateway.backend.open_service(service)
        opened = True

    operations_payload: list[dict[str, Any]] = []
    if include_operations and opened:
        for descriptor in gateway.backend.list_operations(service):
            op_policy = gateway.policy.describe_operation_policy(service, descriptor.operation)
            entry: dict[str, Any] = {
                "operation": descriptor.operation,
                "description": descriptor.description,
                "schema_hash": descriptor.schema_hash,
                "policy": op_policy,
            }
            if include_event_schemas and descriptor.request is not None:
                entry["request_schema"] = descriptor_to_json_schema(descriptor.request)
                entry["response_schemas"] = [descriptor_to_json_schema(r) for r in descriptor.responses]
            operations_payload.append(entry)

    return envelope(
        ok=True,
        data={
            "service": service,
            "opened": opened,
            "openable": service_policy.open,
            "subscriptions_allowed": service_policy.subscriptions,
            "operations": operations_payload,
            "session_generation": gateway.backend.session_generation,
        },
    )


async def describe_operation(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    service = _require_str(arguments, "service")
    operation = _require_str(arguments, "operation")
    gateway.policy.authorize_operation_discovery(principal, service, operation)
    descriptor = gateway.backend.get_operation(service, operation)
    policy_block = gateway.policy.describe_operation_policy(service, operation)
    schema_format = str(arguments.get("schema_format", "json_schema"))
    data: dict[str, Any] = {
        "service": service,
        "operation": operation,
        "description": descriptor.description,
        "schema_hash": descriptor.schema_hash,
        "session_generation": descriptor.service_generation,
        "policy": policy_block,
    }
    if schema_format == "json_schema":
        data["request_schema"] = descriptor_to_json_schema(descriptor.request) if descriptor.request else None
        data["response_schemas"] = [descriptor_to_json_schema(r) for r in descriptor.responses]
    else:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unsupported schema_format {schema_format!r}.")
    return envelope(ok=True, data=data)


async def validate_request(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validation without Bloomberg submission (SPEC §3.4)."""
    service = _require_str(arguments, "service")
    operation = _require_str(arguments, "operation")
    parameters = arguments.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "parameters must be an object.")
    schema_hash = arguments.get("schema_hash")
    options = arguments.get("options") or {}
    if not isinstance(options, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "options must be an object.")
    canonical = build_canonical_request(
        gateway,
        principal,
        service,
        operation,
        parameters,
        schema_hash=schema_hash,
        strict_types=bool(options.get("strict_types", False)),
        reject_unknown_elements=options.get("reject_unknown_elements"),
    )
    cost = canonical.estimated_cost
    return envelope(
        ok=True,
        data={
            "valid": True,
            "canonical_request": {
                "service": canonical.service,
                "operation": canonical.operation,
                "schema_hash": canonical.schema_hash,
                "parameters": _json_safe(canonical.parameters),
            },
            "estimated_cost": {
                "securities": cost.securities,
                "fields": cost.fields,
                "estimated_observations": cost.estimated_observations,
                "risk_score": cost.risk_score,
            },
            "warnings": [],
        },
    )


def _json_safe(parameters: Any) -> Any:
    """Render decoded canonical values (dates etc.) into JSON-safe form."""
    import datetime as _dt
    from collections.abc import Mapping as _Mapping

    if isinstance(parameters, _Mapping):
        return {str(k): _json_safe(v) for k, v in parameters.items()}
    if isinstance(parameters, list):
        return [_json_safe(v) for v in parameters]
    if isinstance(parameters, tuple):
        return [_json_safe(v) for v in parameters]
    if isinstance(parameters, _dt.datetime):
        return parameters.isoformat()
    if isinstance(parameters, (_dt.date, _dt.time)):
        return parameters.isoformat()
    if isinstance(parameters, bytes):
        import base64

        return base64.b64encode(parameters).decode("ascii")
    return parameters


def _require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Argument {key!r} must be a non-empty string.")
    return value


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="blpapi_list_services",
        title="List Bloomberg services",
        description="List Bloomberg services known to the gateway (configured, opened or allowlisted).",
        input_schema={
            "type": "object",
            "properties": {"include_unopened": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
        scope=SCOPE_DISCOVER,
        handler=list_services,
        read_only=True,
        output_schema=with_data(
            {
                "type": "object",
                "properties": {
                    "services": {"type": "array", "items": {"type": "object"}},
                    "unopened": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["services"],
            }
        ),
    ),
    ToolSpec(
        name="blpapi_open_service",
        title="Open a Bloomberg service",
        description="Explicitly open an allowlisted Bloomberg consumer service. Idempotent.",
        input_schema={
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
            "additionalProperties": False,
        },
        scope=SCOPE_DISCOVER,
        handler=open_service,
        idempotent=True,
        output_schema=with_data(
            {
                "type": "object",
                "properties": {"service": {"type": "string"}, "opened": {"type": "boolean"}},
                "required": ["service"],
            }
        ),
    ),
    ToolSpec(
        name="blpapi_describe_service",
        title="Describe a Bloomberg service",
        description="Describe a service: operations, schemas and discovery/execution policy.",
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "open_if_allowed": {"type": "boolean", "default": True},
                "include_operations": {"type": "boolean", "default": True},
                "include_event_schemas": {"type": "boolean", "default": False},
            },
            "required": ["service"],
            "additionalProperties": False,
        },
        scope=SCOPE_DISCOVER,
        handler=describe_service,
        read_only=True,
        output_schema=with_data(
            {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "operations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["service"],
            }
        ),
    ),
    ToolSpec(
        name="blpapi_describe_operation",
        title="Describe a Bloomberg operation",
        description="Inspect one operation's request/response schemas, hash and policy.",
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "operation": {"type": "string"},
                "schema_format": {"type": "string", "enum": ["json_schema"], "default": "json_schema"},
            },
            "required": ["service", "operation"],
            "additionalProperties": False,
        },
        scope=SCOPE_DISCOVER,
        handler=describe_operation,
        read_only=True,
        output_schema=with_data(
            {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "operation": {"type": "string"},
                    "schema_hash": {"type": "string"},
                    "request_schema": {"type": "object"},
                    "response_schemas": {"type": "array", "items": {"type": "object"}},
                    "policy": {"type": "object"},
                },
                "required": ["service", "operation", "schema_hash"],
            }
        ),
    ),
    ToolSpec(
        name="blpapi_validate_request",
        title="Validate a Bloomberg request",
        description=(
            "Validate request parameters against the live Bloomberg schema without submitting. "
            "Note: the native schema declares array elements such as 'securities'/'fields' with "
            "min_values=0, so omitting them still validates true (schema-faithful). "
            "Pass options.strict_types=true to reject scalar-where-array-was-declared inputs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "operation": {"type": "string"},
                "schema_hash": {"type": "string"},
                "parameters": {"type": "object"},
                "options": {
                    "type": "object",
                    "properties": {
                        "reject_unknown_elements": {"type": "boolean"},
                        "strict_types": {
                            "type": "boolean",
                            "description": (
                                "Reject a bare scalar where the schema declares an array "
                                "(default: singleton coercion)."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["service", "operation", "parameters"],
            "additionalProperties": False,
        },
        scope=None,
        handler=validate_request,
        read_only=True,
        output_schema=with_data(
            {
                "type": "object",
                "properties": {
                    "valid": {"type": "boolean"},
                    "canonical_request": {"type": "object"},
                    "estimated_cost": {
                        "type": "object",
                        "properties": {
                            "securities": {"type": "integer"},
                            "fields": {"type": "integer"},
                            "estimated_observations": {"type": "integer"},
                            "risk_score": {"type": "integer"},
                        },
                    },
                    "warnings": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["valid", "estimated_cost"],
            }
        ),
    ),
]
