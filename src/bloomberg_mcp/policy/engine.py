"""Policy engine: authorizes tools, services and operations (SPEC §4.2, §4.3)."""

from __future__ import annotations

from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.policy.models import (
    OperationPolicy,
    PolicyConfig,
    ServicePolicy,
    is_forbidden_operation_family,
)

SCOPE_DISCOVER = "bloomberg:discover"
SCOPE_GENERIC_REQUEST = "bloomberg:generic-request"
SCOPE_SUBSCRIBE = "bloomberg:subscribe"
SCOPE_RESULT_READ = "bloomberg:result-read"


class PolicyEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self._policy = policy

    @property
    def config(self) -> PolicyConfig:
        return self._policy

    # ------------------------------------------------------------- services

    def service_policy(self, service: str) -> ServicePolicy:
        configured = self._policy.services.get(service)
        if configured is None and self._policy.defaults.deny_unconfigured_services:
            raise GatewayError(
                ErrorCode.INVALID_SERVICE,
                f"Service {service!r} is not configured for this gateway.",
            )
        return configured or ServicePolicy()

    def authorize_open(self, service: str) -> None:
        policy = self.service_policy(service)
        if not policy.open:
            raise GatewayError(
                ErrorCode.INVALID_SERVICE,
                f"Service {service!r} is not openable by configuration.",
            )

    def authorize_discovery(self, principal: Principal, service: str) -> None:
        if not principal.has_scope(SCOPE_DISCOVER):
            raise GatewayError(ErrorCode.AUTH_FORBIDDEN, "Missing scope bloomberg:discover.")
        policy = self.service_policy(service)
        if not policy.discover:
            raise GatewayError(
                ErrorCode.AUTH_FORBIDDEN,
                f"Discovery of service {service!r} is not permitted.",
            )

    # ----------------------------------------------------------- operations

    def operation_policy(self, service: str, operation: str) -> OperationPolicy:
        forbidden = is_forbidden_operation_family(operation, self._policy.defaults)
        if forbidden:
            raise GatewayError(ErrorCode.AUTH_FORBIDDEN, forbidden)
        service_policy = self.service_policy(service)
        op_policy = service_policy.operation_policy(operation)
        if op_policy is None and self._policy.defaults.deny_unconfigured_operations:
            raise GatewayError(
                ErrorCode.INVALID_OPERATION,
                f"Operation {operation!r} on {service!r} is not configured.",
            )
        return op_policy or OperationPolicy()

    def authorize_operation_discovery(self, principal: Principal, service: str, operation: str) -> OperationPolicy:
        self.authorize_discovery(principal, service)
        op_policy = self.operation_policy(service, operation)
        if not op_policy.discover and not op_policy.execute:
            raise GatewayError(
                ErrorCode.AUTH_FORBIDDEN,
                f"Discovery of operation {operation!r} is not permitted.",
            )
        return op_policy

    def authorize_execution(self, principal: Principal, service: str, operation: str) -> OperationPolicy:
        op_policy = self.operation_policy(service, operation)
        if not op_policy.execute:
            raise GatewayError(
                ErrorCode.AUTH_FORBIDDEN,
                f"Execution of operation {operation!r} is not permitted.",
            )
        if op_policy.required_scope and not principal.has_scope(op_policy.required_scope):
            raise GatewayError(
                ErrorCode.AUTH_FORBIDDEN,
                f"Missing scope {op_policy.required_scope!r} for operation {operation!r}.",
            )
        return op_policy

    def describe_operation_policy(self, service: str, operation: str) -> dict[str, Any]:
        """Discovery/execution policy block for describe outputs (SPEC §3.4)."""
        try:
            op_policy = self.operation_policy(service, operation)
        except GatewayError:
            return {"discover_allowed": False, "execute_allowed": False, "required_scope": None}
        return {
            "discover_allowed": op_policy.discover or op_policy.execute,
            "execute_allowed": op_policy.execute,
            "required_scope": op_policy.required_scope,
        }

    # --------------------------------------------------------- subscriptions

    def authorize_subscription_service(self, principal: Principal, service: str) -> None:
        if not principal.has_scope(SCOPE_SUBSCRIBE):
            raise GatewayError(ErrorCode.AUTH_FORBIDDEN, "Missing scope bloomberg:subscribe.")
        policy = self.service_policy(service)
        if not policy.subscriptions:
            raise GatewayError(
                ErrorCode.AUTH_FORBIDDEN,
                f"Subscriptions are not permitted on service {service!r}.",
            )
