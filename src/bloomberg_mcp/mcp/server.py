"""Stateless MCP 2026-07-28 Streamable HTTP server (SPEC §3.1, §3.2).

Built on the official MCP Python SDK: every request is a self-contained POST
(no initialize handshake, no ``Mcp-Session-Id``, no GET/DELETE). The SDK
validates ``MCP-Protocol-Version``, ``Mcp-Method``/``Mcp-Name`` header/body
consistency and unsupported versions using the protocol-defined error shapes;
a pre-gate additionally requires the configured modern revision on every
``/mcp`` request so legacy-era requests never reach a handler.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import jsonschema
import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS, MCP_PROTOCOL_VERSION_HEADER
from mcp_types import UNSUPPORTED_PROTOCOL_VERSION, UnsupportedProtocolVersionErrorData
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from bloomberg_mcp import PROTOCOL_REVISION, __version__
from bloomberg_mcp.auth.middleware import BearerAuthMiddleware, current_principal
from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.commit import COMMIT
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.mcp import (
    curated_tools,
    discovery_tools,
    request_tools,
    resources,
    subscription_tools,
)
from bloomberg_mcp.mcp.output_schemas import ENVELOPE_SCHEMA, envelope
from bloomberg_mcp.mcp.tool_spec import ToolSpec
from bloomberg_mcp.observability.audit import AuditEvent
from bloomberg_mcp.observability.health import HealthInputs, HealthService

logger = logging.getLogger(__name__)

#: Gateway-wide operation failures set ``isError`` (SPEC §3.2); validation and
#: authorization failures stay ``ok:false`` with ``isError`` false.
_EXECUTION_ERROR_CODES = frozenset(
    {
        ErrorCode.BLOOMBERG_NOT_CONNECTED,
        ErrorCode.BLOOMBERG_TERMINAL_NOT_LOGGED_IN,
        ErrorCode.BLOOMBERG_SESSION_FAILED,
        ErrorCode.BLOOMBERG_SESSION_LOST,
        ErrorCode.BLOOMBERG_SERVICE_NOT_OPEN,
        ErrorCode.BLOOMBERG_SERVICE_OPEN_FAILED,
        ErrorCode.BLOOMBERG_REQUEST_FAILED,
        ErrorCode.BLOOMBERG_RESPONSE_ERROR,
        ErrorCode.BLOOMBERG_NOT_ENTITLED,
        ErrorCode.BLOOMBERG_SUBSCRIPTION_FAILED,
        ErrorCode.TIMEOUT,
        ErrorCode.CANCELLED,
        ErrorCode.QUEUE_FULL,
        ErrorCode.INTERNAL_ERROR,
    }
)


def build_tool_catalog() -> dict[str, ToolSpec]:
    catalog: dict[str, ToolSpec] = {}
    for module in (discovery_tools, request_tools, subscription_tools, curated_tools):
        for tool in module.TOOLS:
            if tool.name in catalog:
                raise RuntimeError(f"duplicate tool registration: {tool.name}")
            catalog[tool.name] = tool
    return catalog


class ProtocolGateMiddleware:
    """Require the configured modern protocol revision on ``/mcp``.

    Rejections use the SDK's protocol-defined error shapes (SPEC §3.3 note).
    """

    def __init__(self, app: ASGIApp, mcp_path: str, required_revision: str) -> None:
        self.app = app
        self._path = mcp_path
        self._required = required_revision

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self._path:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        version = headers.get(MCP_PROTOCOL_VERSION_HEADER)
        if version != self._required:
            data = UnsupportedProtocolVersionErrorData(
                supported=[self._required], requested=version or ""
            ).model_dump(mode="json")
            error = types.JSONRPCError(
                jsonrpc="2.0",
                id=None,
                error=types.ErrorData(
                    code=UNSUPPORTED_PROTOCOL_VERSION,
                    message="Unsupported protocol version",
                    data=data,
                ),
            )
            status = ERROR_CODE_HTTP_STATUS.get(UNSUPPORTED_PROTOCOL_VERSION, 400)
            body = error.model_dump(mode="json", by_alias=True, exclude_none=True)
            body["id"] = None
            response = Response(
                json.dumps(body, separators=(",", ":")), status_code=status, media_type="application/json"
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _summary_text(payload: dict[str, Any]) -> str:
    """Concise text-content fallback for client compatibility (SPEC §3.2)."""
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return f"error: {error.get('code', 'INTERNAL_ERROR')}: {error.get('message', 'unknown error')}"
    data = payload.get("data")
    if isinstance(data, dict):
        keys = ", ".join(list(data)[:6])
        return f"ok: {keys}" if keys else "ok"
    return "ok"


def build_mcp_handlers(gateway: Gateway, tools: dict[str, ToolSpec]) -> dict[str, Any]:
    async def on_list_tools(
        ctx: Any, params: Any
    ) -> types.ListToolsResult:
        mcp_tools = []
        for tool in tools.values():
            mcp_tools.append(
                types.Tool(
                    name=tool.name,
                    title=tool.title,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema or ENVELOPE_SCHEMA,
                    annotations=types.ToolAnnotations(
                        read_only_hint=tool.read_only,
                        destructive_hint=False,
                        idempotent_hint=tool.idempotent,
                        open_world_hint=True,
                    ),
                )
            )
        return types.ListToolsResult(tools=mcp_tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = current_principal.get()
        if principal is None:
            error = GatewayError(ErrorCode.AUTH_REQUIRED, "Authentication required.")
            payload = envelope(ok=False, error=error.to_dict())
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_summary_text(payload))],
                structured_content=payload,
                is_error=True,
            )
        tool = tools.get(params.name)
        gateway.metrics.inc("mcp_tool_calls_total", tool=params.name)
        if tool is None:
            payload = envelope(
                ok=False,
                error=GatewayError(ErrorCode.INVALID_ARGUMENT, f"Unknown tool {params.name!r}.").to_dict(),
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_summary_text(payload))],
                structured_content=payload,
                is_error=True,
            )
        arguments = dict(params.arguments or {})

        try:
            jsonschema.validate(instance=arguments, schema=tool.input_schema)
        except jsonschema.ValidationError as exc:
            payload = envelope(
                ok=False,
                error=GatewayError(ErrorCode.INVALID_ARGUMENT, f"Invalid arguments: {exc.message}").to_dict(),
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_summary_text(payload))],
                structured_content=payload,
                is_error=False,
            )

        if tool.scope is not None and not principal.has_scope(tool.scope):
            payload = envelope(
                ok=False,
                error=GatewayError(ErrorCode.AUTH_FORBIDDEN, f"Missing scope {tool.scope}.").to_dict(),
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_summary_text(payload))],
                structured_content=payload,
                is_error=False,
            )

        try:
            payload = await tool.handler(gateway, principal, arguments)
        except GatewayError as exc:
            gateway.metrics.inc("mcp_tool_failures_total", tool=params.name)
            gateway.audit.record(_tool_audit_event(principal.principal_id, params.name, exc))
            payload = envelope(ok=False, error=exc.to_dict())
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_summary_text(payload))],
                structured_content=payload,
                is_error=exc.code in _EXECUTION_ERROR_CODES,
            )
        except Exception:
            logger.exception("tool %s crashed", params.name)
            gateway.metrics.inc("mcp_tool_failures_total", tool=params.name)
            error = GatewayError(ErrorCode.INTERNAL_ERROR, "Internal gateway error.")
            payload = envelope(ok=False, error=error.to_dict())
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_summary_text(payload))],
                structured_content=payload,
                is_error=True,
            )

        gateway.audit.record(_tool_audit_event(principal.principal_id, params.name, None))
        return types.CallToolResult(
            content=_result_content_blocks(payload),
            structured_content=payload,
            is_error=payload.get("ok") is False and isinstance(payload.get("error"), dict),
        )

    def _result_content_blocks(payload: dict[str, Any]) -> list[Any]:
        """Tool-result content: summary text plus an MCP resource-link block
        when the result is artifact-backed (finding L3: URI strings become
        real EmbeddedResource content blocks)."""
        content: list[Any] = [types.TextContent(type="text", text=_summary_text(payload))]
        data = payload.get("data")
        artifact = data.get("artifact") if isinstance(data, dict) else None
        if isinstance(artifact, dict):
            resource_uri = artifact.get("resource_uri") or ""
            mime_type = artifact.get("content_type") or "application/json"
            preview = data.get("preview")  # type: ignore[union-attr]
            content.append(
                types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=resource_uri,
                        mime_type=mime_type,
                        text=str(preview) if preview is not None else "",
                    ),
                )
            )
        return content

    async def on_list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
        static, _ = resources.list_resources_payload()
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=item["uri"],
                    name=item["name"],
                    description=item.get("description"),
                    mime_type=item.get("mimeType"),
                )
                for item in static
            ]
        )

    async def on_list_resource_templates(ctx: Any, params: Any) -> types.ListResourceTemplatesResult:
        _, templates = resources.list_resources_payload()
        return types.ListResourceTemplatesResult(
            resource_templates=[
                types.ResourceTemplate(
                    uri_template=item["uriTemplate"],
                    name=item["name"],
                    description=item.get("description"),
                    mime_type=item.get("mimeType"),
                )
                for item in templates
            ]
        )

    async def on_read_resource(ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        principal = current_principal.get()
        if principal is None:
            raise GatewayError(ErrorCode.AUTH_REQUIRED, "Authentication required.")
        try:
            text, mime_type = await resources.read_resource(gateway, principal, str(params.uri))
        except GatewayError as exc:
            payload = envelope(ok=False, error=exc.to_dict())
            text, mime_type = json.dumps(payload), "application/json"
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=str(params.uri), text=text, mime_type=mime_type)]
        )

    return {
        "on_list_tools": on_list_tools,
        "on_call_tool": on_call_tool,
        "on_list_resources": on_list_resources,
        "on_list_resource_templates": on_list_resource_templates,
        "on_read_resource": on_read_resource,
    }


def _tool_audit_event(principal_id: str, tool: str, error: GatewayError | None) -> AuditEvent:
    return AuditEvent(
        action="tool_call",
        principal_id=principal_id,
        tool=tool,
        outcome="error" if error else "ok",
        error_code=error.code.value if error else None,
    )


def build_app(gateway: Gateway, verifier: TokenVerifier) -> Starlette:
    config = gateway.config
    tools = build_tool_catalog()
    handlers = build_mcp_handlers(gateway, tools)
    server: Server[Any] = Server(
        name="bloomberg-mcp",
        version=__version__,
        description="Bloomberg MCP Gateway (stateless Streamable HTTP, MCP 2026-07-28)",
        **handlers,
    )

    allowed_hosts = list(config.server.allowed_hosts) or [
        f"127.0.0.1:{config.server.port}",
        f"localhost:{config.server.port}",
        f"[::1]:{config.server.port}",
    ]
    allowed_origins = list(config.server.allowed_origins) or [
        f"http://127.0.0.1:{config.server.port}",
        f"http://localhost:{config.server.port}",
    ]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=True,
        stateless=True,
        security_settings=security,
        max_request_body_size=config.server.max_request_body_bytes,
    )
    health = HealthService(HealthInputs(*gateway.health_inputs()))

    # ----------------------------------------------------------- HTTP routes

    async def health_live(request: Request) -> Response:
        return JSONResponse(health.liveness(), headers={"cache-control": "no-store"})

    async def health_ready(request: Request) -> Response:
        return JSONResponse(health.readiness(), headers={"cache-control": "no-store"})

    async def version_endpoint(request: Request) -> Response:
        from importlib.metadata import version as _pkg_version

        return JSONResponse(
            {
                "gateway": __version__,
                "commit": COMMIT,
                "protocol_revision": PROTOCOL_REVISION,
                "mcp_sdk": _safe_version(_pkg_version, "mcp"),
                "blpapi": _safe_version(_pkg_version, "blpapi"),
                "backend": gateway.config.backend,
            },
            headers={"cache-control": "no-store"},
        )

    async def metrics_endpoint(request: Request) -> Response:
        principal = current_principal.get()
        if principal is None or not principal.has_scope("bloomberg:admin"):
            body = {"error": {"code": "AUTH_FORBIDDEN", "message": "Admin scope required."}}
            return JSONResponse(body, status_code=403)
        client = request.client.host if request.client else "unknown"
        if client not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"error": {"code": "AUTH_FORBIDDEN", "message": "Localhost only."}}, status_code=403)
        text = gateway.metrics.render_prometheus()
        return Response(text, media_type="text/plain; version=0.0.4")

    async def artifact_endpoint(request: Request) -> Response:
        if not config.server.artifact_endpoint_enabled:
            return Response(status_code=404)
        principal = current_principal.get()
        if principal is None:
            body = {"error": {"code": "AUTH_REQUIRED", "message": "Authentication required."}}
            return JSONResponse(body, status_code=401)
        result_id = request.path_params["result_id"]
        try:
            info, payload = gateway.result_store.get_bytes(result_id, principal.principal_id, admin=principal.admin)
        except GatewayError as exc:
            status = 404 if exc.code in (ErrorCode.RESULT_NOT_FOUND,) else 410
            return JSONResponse({"error": exc.to_dict()}, status_code=status)
        gateway.audit.record(_artifact_audit_event(principal.principal_id, result_id, len(payload)))
        return Response(
            payload,
            media_type=info.content_type,
            headers={"content-length": str(len(payload)), "cache-control": "no-store"},
        )

    def _artifact_audit_event(principal_id: str, result_id: str, size: int) -> AuditEvent:
        return AuditEvent(
            action="artifact_download",
            principal_id=principal_id,
            result_id=result_id,
            response_bytes=size,
            outcome="ok",
        )

    mcp_path = config.server.mcp_path
    gated_mcp = ProtocolGateMiddleware(manager.asgi_app, mcp_path, config.server.protocol_revision)

    routes: list[Route | Mount] = [
        Route("/health/live", health_live, methods=["GET"]),
        Route("/health/ready", health_ready, methods=["GET"]),
        Route("/version", version_endpoint, methods=["GET"]),
        Route("/artifacts/{result_id}", artifact_endpoint, methods=["GET"]),
        Route(mcp_path, gated_mcp),
    ]
    if config.server.metrics_endpoint_enabled:
        routes.insert(4, Route("/metrics", metrics_endpoint, methods=["GET"]))

    protected = (mcp_path, "/health/ready", "/version", "/artifacts/", "/metrics")
    authenticated = BearerAuthMiddleware(_Router(routes), verifier, gateway.quota, gateway.audit, protected)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            await gateway.start()
            try:
                yield
            finally:
                await gateway.stop()

    return Starlette(debug=False, routes=[Mount("/", app=authenticated)], lifespan=lifespan)


class _Router:
    """Minimal router so the auth middleware wraps all routes as one ASGI app."""

    def __init__(self, routes: list[Route | Mount]) -> None:
        self._app = Starlette(routes=routes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


def _safe_version(get_version: Any, package: str) -> str:
    try:
        return str(get_version(package))
    except Exception:
        return "unknown"
