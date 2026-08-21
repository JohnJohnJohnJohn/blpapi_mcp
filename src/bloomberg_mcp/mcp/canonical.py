"""Canonical request construction shared by generic and curated tools (SPEC §2.8).

Pipeline stage: policy -> schema lookup -> schema-hash validation -> Bloomberg
schema validation -> canonical conversion -> cost evaluation and limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.blp.schema_converter import validate_parameters
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.models import CanonicalRequest, RequestCost, ResponseMode
from bloomberg_mcp.policy.cost import estimate_cost


def build_canonical_request(
    gateway: Gateway,
    principal: Principal,
    service: str,
    operation: str,
    parameters: Mapping[str, Any],
    *,
    schema_hash: str | None,
    response_mode: ResponseMode = ResponseMode.CANONICAL,
    strict_types: bool = False,
) -> CanonicalRequest:
    gateway.policy.authorize_execution(principal, service, operation)
    # Schema lookup needs a live session; when the Terminal is down, fail
    # fast with a clean retryable error instead of a misleading one.
    gateway.backend.assert_available()
    descriptor = gateway.backend.get_operation(service, operation)
    if schema_hash is not None and schema_hash != descriptor.schema_hash:
        raise GatewayError(
            ErrorCode.SCHEMA_DRIFT_DETECTED,
            "The provided schema_hash does not match the current operation schema.",
            retryable=True,
        )

    limits = gateway.policy_config.limits
    validated = validate_parameters(
        descriptor.request,
        dict(parameters),
        limits,
        reject_unknown_elements=gateway.policy_config.defaults.reject_unknown_elements,
        strict_types=strict_types,
    ) if descriptor.request else dict(parameters)

    cost = estimate_cost(operation, validated)
    _enforce_limits(cost, limits)

    return CanonicalRequest(
        service=service,
        operation=operation,
        schema_hash=descriptor.schema_hash,
        parameters=validated,
        estimated_cost=cost,
        response_mode=response_mode,
    )


def _enforce_limits(cost: RequestCost, limits: Any) -> None:
    if cost.securities > limits.maximum_securities:
        raise GatewayError(
            ErrorCode.REQUEST_TOO_LARGE,
            f"Request exceeds the maximum of {limits.maximum_securities} securities.",
        )
    if cost.fields > limits.maximum_fields:
        raise GatewayError(
            ErrorCode.REQUEST_TOO_LARGE,
            f"Request exceeds the maximum of {limits.maximum_fields} fields.",
        )
    if cost.estimated_observations > limits.maximum_estimated_observations:
        raise GatewayError(
            ErrorCode.REQUEST_TOO_LARGE,
            "Estimated observation count exceeds the configured budget for one request.",
        )


def canonical_overrides(overrides: Mapping[str, Any]) -> list[dict[str, str]]:
    """Convert the curated ``overrides`` object to Bloomberg override sequences (SPEC §3.8)."""
    if not overrides:
        return []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for field_id, value in overrides.items():
        if not field_id:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Override field id must be non-empty.")
        if field_id in seen:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"Duplicate override {field_id!r}.")
        seen.add(field_id)
        entries.append({"fieldId": field_id, "value": str(value)})
    return entries
