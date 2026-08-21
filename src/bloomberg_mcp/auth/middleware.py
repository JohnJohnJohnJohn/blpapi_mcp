"""Bearer authentication middleware (SPEC §1.7, §3.1).

- Tokens are accepted only from the ``Authorization`` header.
- Every request is authenticated independently; failures are rate-limited
  and audited; tokens never appear in logs.
- The resolved principal is exposed via ``current_principal`` for tool and
  resource handlers.
"""

from __future__ import annotations

import contextvars
import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.observability.audit import AuditEvent, AuditLogger
from bloomberg_mcp.policy.quota import QuotaEngine

logger = logging.getLogger(__name__)

current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "bloomberg_mcp_principal", default=None
)
current_client_address: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bloomberg_mcp_client_address", default=None
)


def _client_address(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return str(client[0])
    return "unknown"


async def _send_json_error(
    scope: Scope, receive: Receive, send: Send, status: int, code: ErrorCode, message: str, *, www_auth: bool
) -> None:
    body = json.dumps({"error": {"code": code.value, "message": message}}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if www_auth:
        headers.append((b"www-authenticate", b'Bearer realm="bloomberg-mcp"'))
    await send(
        {"type": "http.response.start", "status": status, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Authenticate protected paths before any handler runs."""

    def __init__(
        self,
        app: ASGIApp,
        verifier: TokenVerifier,
        quota: QuotaEngine,
        audit: AuditLogger,
        protected_prefixes: tuple[str, ...],
    ) -> None:
        self.app = app
        self._verifier = verifier
        self._quota = quota
        self._audit = audit
        self._protected = protected_prefixes

    def _is_protected(self, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix) for prefix in self._protected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_protected(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        address = _client_address(scope)
        current_client_address.set(address)

        try:
            self._quota.admit_auth_attempt(address)
        except GatewayError:
            self._audit.record(
                AuditEvent(action="auth", outcome="rate_limited", client_address=address, error_code="RATE_LIMITED")
            )
            await _send_json_error(
                scope,
                receive,
                send,
                429,
                ErrorCode.RATE_LIMITED,
                "Too many authentication attempts; try again later.",
                www_auth=True,
            )
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        authorization = headers.get("authorization")
        if authorization is None:
            self._audit.record(
                AuditEvent(action="auth", outcome="missing", client_address=address, error_code="AUTH_REQUIRED")
            )
            await _send_json_error(
                scope, receive, send, 401, ErrorCode.AUTH_REQUIRED, "Authentication required.", www_auth=True
            )
            return

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            self._audit.record(
                AuditEvent(action="auth", outcome="malformed", client_address=address, error_code="AUTH_INVALID")
            )
            await _send_json_error(
                scope, receive, send, 401, ErrorCode.AUTH_INVALID, "Invalid Authorization header.", www_auth=True
            )
            return

        try:
            principal = self._verifier.verify(token)
        except Exception:
            limited = self._quota.record_auth_failure(address)
            self._audit.record(
                AuditEvent(action="auth", outcome="rejected", client_address=address, error_code="AUTH_INVALID")
            )
            logger.warning("rejected bearer token from %s", address)
            if limited:
                await _send_json_error(
                    scope, receive, send, 429, ErrorCode.RATE_LIMITED, "Too many authentication failures.",
                    www_auth=False,
                )
                return
            await _send_json_error(
                scope, receive, send, 401, ErrorCode.AUTH_INVALID, "Invalid bearer token.", www_auth=True
            )
            return

        scope["principal"] = principal
        token_handle = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(token_handle)
