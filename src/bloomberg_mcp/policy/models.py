"""Policy model: principals, service/operation permissions, limits (SPEC §4.2).

Discovery and execution permissions are distinct. Unconfigured services and
operations are denied by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.errors import ErrorCode, GatewayError


@dataclass(frozen=True)
class OperationPolicy:
    discover: bool = False
    execute: bool = False
    required_scope: str | None = None


@dataclass(frozen=True)
class ServicePolicy:
    open: bool = False
    discover: bool = False
    subscriptions: bool = False
    # Exact operation name -> policy; "*" is the wildcard entry.
    operations: dict[str, OperationPolicy] = field(default_factory=dict)

    def operation_policy(self, operation: str) -> OperationPolicy | None:
        if operation in self.operations:
            return self.operations[operation]
        return self.operations.get("*")


@dataclass(frozen=True)
class PolicyDefaults:
    deny_unconfigured_services: bool = True
    deny_unconfigured_operations: bool = True
    deny_authorization_operations: bool = True
    deny_provider_operations: bool = True
    reject_unknown_elements: bool = True


@dataclass(frozen=True)
class PolicyLimits:
    maximum_securities: int = 100
    maximum_fields: int = 100
    maximum_estimated_observations: int = 1_000_000
    maximum_request_array_elements: int = 10_000
    maximum_nesting_depth: int = 32


@dataclass(frozen=True)
class PolicyConfig:
    principals: dict[str, Principal] = field(default_factory=dict)
    services: dict[str, ServicePolicy] = field(default_factory=dict)
    defaults: PolicyDefaults = field(default_factory=PolicyDefaults)
    limits: PolicyLimits = field(default_factory=PolicyLimits)


# Operation-name substrings that indicate authorization / provider semantics
# and must never be executable through the gateway (SPEC §1.3 Class C).
_AUTH_MARKERS = ("authorization", "generateToken", "token")
_PROVIDER_MARKERS = ("publish", "registerService", "createTopic")


def load_policy_config(path: str | None) -> PolicyConfig:
    raw: dict[str, Any] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except OSError as exc:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"cannot read policy {path}: {exc}") from exc
        if loaded:
            if not isinstance(loaded, dict):
                raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"policy {path} must be a mapping")
            raw = loaded

    principals: dict[str, Principal] = {}
    for name, spec in (raw.get("principals") or {}).items():
        scopes = tuple((spec or {}).get("scopes") or [])
        principals[str(name)] = Principal(principal_id=str(name), scopes=frozenset(scopes))

    services: dict[str, ServicePolicy] = {}
    for service_name, spec in (raw.get("services") or {}).items():
        spec = spec or {}
        operations: dict[str, OperationPolicy] = {}
        for op_name, op_spec in (spec.get("operations") or {}).items():
            op_spec = op_spec or {}
            operations[str(op_name)] = OperationPolicy(
                discover=bool(op_spec.get("discover", False)),
                execute=bool(op_spec.get("execute", False)),
                required_scope=op_spec.get("required_scope"),
            )
        services[str(service_name)] = ServicePolicy(
            open=bool(spec.get("open", False)),
            discover=bool(spec.get("discover", False)),
            subscriptions=bool(spec.get("subscriptions", False)),
            operations=operations,
        )

    d = raw.get("defaults") or {}
    defaults = PolicyDefaults(
        deny_unconfigured_services=bool(d.get("deny_unconfigured_services", True)),
        deny_unconfigured_operations=bool(d.get("deny_unconfigured_operations", True)),
        deny_authorization_operations=bool(d.get("deny_authorization_operations", True)),
        deny_provider_operations=bool(d.get("deny_provider_operations", True)),
        reject_unknown_elements=bool(d.get("reject_unknown_elements", True)),
    )

    limits_raw = raw.get("limits") or {}
    limits = PolicyLimits(
        maximum_securities=int(limits_raw.get("maximum_securities", 100)),
        maximum_fields=int(limits_raw.get("maximum_fields", 100)),
        maximum_estimated_observations=int(limits_raw.get("maximum_estimated_observations", 1_000_000)),
        maximum_request_array_elements=int(limits_raw.get("maximum_request_array_elements", 10_000)),
        maximum_nesting_depth=int(limits_raw.get("maximum_nesting_depth", 32)),
    )

    return PolicyConfig(principals=principals, services=services, defaults=defaults, limits=limits)


def is_forbidden_operation_family(operation: str, defaults: PolicyDefaults) -> str | None:
    """Return a reason string if the operation belongs to a forbidden family."""
    lowered = operation.lower()
    if defaults.deny_authorization_operations and any(m.lower() in lowered for m in _AUTH_MARKERS):
        return "authorization operations are not exposed (SPEC §1.3 Class C)"
    if defaults.deny_provider_operations and any(m.lower() in lowered for m in _PROVIDER_MARKERS):
        return "provider operations are not exposed (SPEC §1.3 Class C)"
    return None
