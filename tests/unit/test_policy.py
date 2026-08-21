"""Policy, cost and quota behavior (SPEC §4.2-§4.4, §1.8, §5.7)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.config import GovernanceConfig, RequestsConfig, SubscriptionsConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.policy.cost import estimate_cost
from bloomberg_mcp.policy.engine import PolicyEngine
from bloomberg_mcp.policy.models import load_policy_config
from bloomberg_mcp.policy.quota import QuotaEngine

REPO = str(Path(__file__).resolve().parents[2])


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(load_policy_config(REPO + "/config/policy.example.yaml"))


@pytest.fixture
def hermes() -> Principal:
    return Principal(
        "hermes",
        frozenset(
            {
                "bloomberg:discover",
                "bloomberg:reference",
                "bloomberg:historical",
                "bloomberg:intraday",
                "bloomberg:generic-request",
                "bloomberg:subscribe",
                "bloomberg:result-read",
            }
        ),
    )


def test_discovery_vs_execution_distinct(engine: PolicyEngine, hermes: Principal) -> None:
    # Wildcard permits discovery of every refdata operation...
    engine.authorize_operation_discovery(hermes, "//blp/refdata", "SomeOtherRequest")
    # ...but not execution.
    with pytest.raises(GatewayError) as excinfo:
        engine.authorize_execution(hermes, "//blp/refdata", "SomeOtherRequest")
    assert excinfo.value.code is ErrorCode.AUTH_FORBIDDEN


def test_execution_requires_configured_operation(engine: PolicyEngine, hermes: Principal) -> None:
    engine.authorize_execution(hermes, "//blp/refdata", "HistoricalDataRequest")


def test_execution_requires_scope(engine: PolicyEngine) -> None:
    no_historical = Principal("limited", frozenset({"bloomberg:generic-request", "bloomberg:discover"}))
    with pytest.raises(GatewayError) as excinfo:
        engine.authorize_execution(no_historical, "//blp/refdata", "HistoricalDataRequest")
    assert excinfo.value.code is ErrorCode.AUTH_FORBIDDEN


def test_unconfigured_service_denied(engine: PolicyEngine, hermes: Principal) -> None:
    with pytest.raises(GatewayError) as excinfo:
        engine.authorize_discovery(hermes, "//blp/unknown")
    assert excinfo.value.code is ErrorCode.INVALID_SERVICE


def test_authorization_family_forbidden(engine: PolicyEngine, hermes: Principal) -> None:
    with pytest.raises(GatewayError) as excinfo:
        engine.operation_policy("//blp/refdata", "sendAuthorizationRequest")
    assert excinfo.value.code is ErrorCode.AUTH_FORBIDDEN


def test_subscription_service_policy(engine: PolicyEngine, hermes: Principal) -> None:
    engine.authorize_subscription_service(hermes, "//blp/mktdata")
    with pytest.raises(GatewayError):
        engine.authorize_subscription_service(hermes, "//blp/refdata")


def test_cost_model_historical() -> None:
    cost = estimate_cost(
        "HistoricalDataRequest",
        {
            "securities": ["A", "B"],
            "fields": ["PX_LAST", "VOLUME"],
            "startDate": "20260101",
            "endDate": "20260131",
        },
    )
    assert cost.securities == 2
    assert cost.fields == 2
    assert cost.estimated_observations == 2 * 2 * 31
    assert cost.risk_score > 0


def test_quota_budget_exhaustion() -> None:
    quota = QuotaEngine(
        GovernanceConfig(daily_request_budget=2, monthly_request_budget=10, persist_usage_counters=False),
        RequestsConfig(),
        SubscriptionsConfig(),
    )
    asyncio.run(quota.admit_request("hermes", "//blp/refdata", "ReferenceDataRequest"))
    asyncio.run(quota.admit_request("hermes", "//blp/refdata", "ReferenceDataRequest"))
    with pytest.raises(GatewayError) as excinfo:
        asyncio.run(quota.admit_request("hermes", "//blp/refdata", "ReferenceDataRequest"))
    assert excinfo.value.code is ErrorCode.LICENSE_BUDGET_EXCEEDED


def test_entitlement_circuit_breaker() -> None:
    quota = QuotaEngine(
        GovernanceConfig(entitlement_failure_circuit_threshold=3, persist_usage_counters=False),
        RequestsConfig(),
        SubscriptionsConfig(),
    )
    assert not quota.entitlement_circuit_open("//blp/refdata")
    for _ in range(3):
        quota.record_entitlement_failure("//blp/refdata")
    assert quota.entitlement_circuit_open("//blp/refdata")
    # Unrelated services are not affected by the refdata breaker (finding K3).
    assert not quota.entitlement_circuit_open("//blp/instruments")
    # Successful entitled exchange closes this family's breaker (health probe).
    quota.record_entitlement_success("//blp/refdata")
    assert not quota.entitlement_circuit_open("//blp/refdata")
    for _ in range(3):
        quota.record_entitlement_failure("//blp/refdata")
    assert quota.entitlement_circuit_open("//blp/refdata")
    quota.reset_entitlement_circuit()
    assert not quota.entitlement_circuit_open("//blp/refdata")


def test_subscription_limits() -> None:
    quota = QuotaEngine(
        GovernanceConfig(persist_usage_counters=False), RequestsConfig(), SubscriptionsConfig(maximum_per_principal=1)
    )
    with pytest.raises(GatewayError) as excinfo:
        asyncio.run(quota.admit_subscription("hermes", active_groups=1, requested_items=1))
    assert excinfo.value.code is ErrorCode.SUBSCRIPTION_LIMIT_EXCEEDED
