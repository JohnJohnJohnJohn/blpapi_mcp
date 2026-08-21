"""Generic request tools (SPEC §3.5): send, get, cancel."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp.canonical import build_canonical_request
from bloomberg_mcp.mcp.output_schemas import envelope, with_data
from bloomberg_mcp.mcp.tool_spec import ToolSpec
from bloomberg_mcp.models import ResponseMode
from bloomberg_mcp.policy.engine import SCOPE_GENERIC_REQUEST, SCOPE_RESULT_READ

_RESPONSE_MODES = {m.value: m for m in ResponseMode}


def _require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Argument {key!r} must be a non-empty string.")
    return value


async def send_request(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    service = _require_str(arguments, "service")
    operation = _require_str(arguments, "operation")
    parameters = arguments.get("parameters")
    if not isinstance(parameters, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "parameters must be an object.")

    execution = arguments.get("execution") or {}
    if not isinstance(execution, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "execution must be an object.")
    wait_seconds = int(execution.get("wait_seconds", gateway.config.requests.default_wait_seconds))
    deadline_seconds = int(execution.get("overall_deadline_seconds", gateway.config.requests.default_deadline_seconds))
    mode_raw = str(execution.get("response_mode", "canonical"))
    response_mode = _RESPONSE_MODES.get(mode_raw)
    if response_mode is None:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unknown response_mode {mode_raw!r}.")
    preview_items = int(execution.get("preview_items", 100))

    canonical = build_canonical_request(
        gateway,
        principal,
        service,
        operation,
        parameters,
        schema_hash=arguments.get("schema_hash"),
        response_mode=response_mode,
    )
    result = await gateway.executor.submit(
        principal.principal_id,
        canonical,
        client_request_id=arguments.get("client_request_id"),
        wait_seconds=wait_seconds,
        deadline_seconds=deadline_seconds,
        preview_items=preview_items,
        is_admin=principal.admin,
        allow_canonical_fallback=bool(execution.get("allow_canonical_fallback", False)),
        artifact_format=execution.get("artifact_format"),
    )
    ok = not isinstance(result.get("error"), dict)
    return envelope(
        ok=ok,
        request_id=result.get("request_id"),
        data=result,
        error=result.get("error"),
        warnings=result.get("warnings", []),
        item_errors=result.get("item_errors", []),
        metadata=result.get("metadata", {}),
    )


async def get_request(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    request_id = _require_str(arguments, "request_id")
    include_preview = bool(arguments.get("include_preview", True))
    limit = int(arguments.get("limit", 100))
    cursor = arguments.get("cursor")

    if cursor is not None:
        # Cursor form: page through a stored large result.
        record = gateway.request_registry.get(request_id, principal.principal_id, admin=principal.admin)
        if record is None:
            raise GatewayError(ErrorCode.REQUEST_NOT_FOUND, "Request not found.")
        if record.result_id is None:
            raise GatewayError(ErrorCode.RESULT_NOT_FOUND, "Request has no stored result artifact.")
        page_number = _parse_page_cursor(cursor)
        result_page = gateway.result_store.get_page(
            record.result_id, principal.principal_id, page_number, limit, admin=principal.admin
        )
        return envelope(ok=True, request_id=request_id, data=result_page)

    result = gateway.executor.get_request(
        principal.principal_id,
        request_id,
        include_preview=include_preview,
        limit=limit,
        is_admin=principal.admin,
    )
    return envelope(
        ok=result.get("status") in ("COMPLETED", "PARTIAL", "SENT", "QUEUED", "RECEIVED", "VALIDATING"),
        request_id=request_id,
        data=result,
        error=result.get("error"),
        warnings=result.get("warnings", []),
        item_errors=result.get("item_errors", []),
        metadata=result.get("metadata", {}),
    )


def _parse_page_cursor(cursor: Any) -> int:
    if not isinstance(cursor, str) or not cursor.isdigit() or int(cursor) < 1:
        raise GatewayError(ErrorCode.CURSOR_INVALID, "Result cursor must be a positive page number string.")
    return int(cursor)


async def cancel_request(gateway: Gateway, principal: Principal, arguments: dict[str, Any]) -> dict[str, Any]:
    request_id = _require_str(arguments, "request_id")
    result = await gateway.executor.cancel_request(
        principal.principal_id, request_id, is_admin=principal.admin
    )
    return envelope(ok=True, request_id=request_id, data=result)


_REQUEST_RESULT_DATA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "request_id": {"type": "string"},
        "pending": {"type": "boolean"},
        "data": {},
        "preview": {},
        "artifact": {"type": "object"},
        "error": {"type": ["object", "null"]},
    },
    "required": ["status", "request_id"],
}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="blpapi_send_request",
        title="Execute a Bloomberg request",
        description=(
            "Execute a schema-defined Bloomberg consumer request through the canonical pipeline "
            "(validation, policy, quotas) and return results or a request handle."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "client_request_id": {"type": "string", "maxLength": 128},
                "service": {"type": "string"},
                "operation": {"type": "string"},
                "schema_hash": {"type": "string"},
                "parameters": {"type": "object"},
                "execution": {
                    "type": "object",
                    "properties": {
                        "wait_seconds": {"type": "integer", "minimum": 1},
                        "overall_deadline_seconds": {"type": "integer", "minimum": 1},
                        "response_mode": {"type": "string", "enum": ["canonical", "typed", "normalized"]},
                        "allow_canonical_fallback": {"type": "boolean"},
                        "artifact_format": {"type": "string", "enum": ["auto", "json", "jsonl"]},
                        "preview_items": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["service", "operation", "parameters"],
            "additionalProperties": False,
        },
        scope=SCOPE_GENERIC_REQUEST,
        handler=send_request,
        output_schema=with_data(_REQUEST_RESULT_DATA),
    ),
    ToolSpec(
        name="blpapi_get_request",
        title="Get request state and results",
        description="Retrieve the state, results or result pages of a principal-owned request.",
        input_schema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "include_preview": {"type": "boolean", "default": True},
                "cursor": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
        scope=SCOPE_RESULT_READ,
        handler=get_request,
        read_only=True,
        output_schema=with_data(_REQUEST_RESULT_DATA),
    ),
    ToolSpec(
        name="blpapi_cancel_request",
        title="Cancel a request",
        description="Cancel an active principal-owned request. Idempotent.",
        input_schema={
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
        scope=SCOPE_RESULT_READ,
        handler=cancel_request,
        idempotent=True,
    ),
]
