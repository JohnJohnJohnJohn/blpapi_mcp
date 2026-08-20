"""MCP tool output schemas and the application result envelope (SPEC §3.2)."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.models import utc_now

ERROR_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
    },
    "required": ["code", "message", "retryable"],
}

ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "request_id": {"type": ["string", "null"]},
        "timestamp": {"type": "string", "format": "date-time"},
        "data": {},
        "error": {"anyOf": [ERROR_OBJECT_SCHEMA, {"type": "null"}]},
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                "required": ["code", "message"],
            },
        },
        "item_errors": {"type": "array", "items": {"type": "object"}},
        "metadata": {"type": "object"},
    },
    "required": ["ok", "timestamp", "warnings", "item_errors"],
}


def envelope(
    *,
    ok: bool,
    data: Any = None,
    request_id: str | None = None,
    error: dict[str, Any] | None = None,
    warnings: list[dict[str, str]] | None = None,
    item_errors: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard application envelope returned as structuredContent."""
    return {
        "ok": ok,
        "request_id": request_id,
        "timestamp": utc_now().isoformat(),
        "data": data,
        "error": error,
        "warnings": warnings or [],
        "item_errors": item_errors or [],
        "metadata": metadata or {},
    }
