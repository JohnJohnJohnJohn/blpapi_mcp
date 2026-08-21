"""Tool registration model shared by all tool modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.gateway import Gateway

ToolHandler = Callable[[Gateway, Principal, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    scope: str | None  # principal scope required in addition to deeper checks
    handler: ToolHandler
    read_only: bool = False
    idempotent: bool = False
    #: Per-tool data contract advertised as outputSchema (finding O8); when
    #: None the generic envelope schema is advertised.
    output_schema: dict[str, Any] | None = None
