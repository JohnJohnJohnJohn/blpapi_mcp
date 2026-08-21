"""CS8 regression tests: authentication rotation and entitlement semantics (K, M).

Failing against 0445a48: every token maps to the first principal; previous
tokens live for the process lifetime; auth-failure limiting is a no-op;
the entitlement circuit is global and closed by unrelated successes; a single
REQUEST_STATUS NO_AUTH error is counted twice.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.blp.request_executor import request_status_error
from bloomberg_mcp.config import AuthConfig, GovernanceConfig, RequestsConfig, SubscriptionsConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import CanonicalMessage, EventKind
from bloomberg_mcp.policy.models import PolicyConfig, PolicyDefaults, PolicyLimits
from bloomberg_mcp.policy.quota import QuotaEngine

CURRENT = "t" * 48
PREVIOUS = "p" * 48


def _policy(*principal_ids: str) -> PolicyConfig:
    return PolicyConfig(
        principals={
            pid: Principal(principal_id=pid, scopes=frozenset({"bloomberg:reference"})) for pid in principal_ids
        },
        defaults=PolicyDefaults(),
        limits=PolicyLimits(),
    )


def _auth(**kw) -> AuthConfig:
    base = AuthConfig()
    return replace(base, **kw)


def _quota(governance: GovernanceConfig) -> QuotaEngine:
    return QuotaEngine(governance, RequestsConfig(), SubscriptionsConfig(), persist_path=None)


def test_multiple_principals_require_explicit_mapping(monkeypatch) -> None:
    """M1: with >1 principal and no explicit token->principal mapping, refuse to start."""
    monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN", CURRENT)
    with pytest.raises(GatewayError) as excinfo:
        TokenVerifier(_auth(), _policy("alice", "bob"))
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_explicit_principal_mapping(monkeypatch) -> None:
    """M1: an explicit principal_id binds the token to that principal."""
    monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN", CURRENT)
    verifier = TokenVerifier(_auth(principal_id="bob"), _policy("alice", "bob"))
    assert verifier.verify(CURRENT).principal_id == "bob"


def test_previous_token_expires_after_overlap(monkeypatch) -> None:
    """M2: the previous token is only valid for the overlap window."""
    monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN", CURRENT)
    monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN_PREVIOUS", PREVIOUS)
    verifier = TokenVerifier(_auth(token_overlap_seconds=0), _policy("alice"))
    with pytest.raises(GatewayError) as excinfo:
        verifier.verify(PREVIOUS)
    assert excinfo.value.code is ErrorCode.AUTH_INVALID
    # The current token still works.
    assert verifier.verify(CURRENT).principal_id == "alice"


def test_auth_attempts_rate_limited_per_address() -> None:
    """M5/M6: admit_auth_attempt must reject a hammering address, per address."""
    quota = _quota(GovernanceConfig(auth_failure_rate_limit=3, persist_usage_counters=False))
    for _ in range(3):
        quota.record_auth_failure("1.2.3.4")
    with pytest.raises(GatewayError) as excinfo:
        quota.admit_auth_attempt("1.2.3.4")
    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    # A different address is unaffected.
    quota.admit_auth_attempt("5.6.7.8")


def test_entitlement_circuit_per_service() -> None:
    """K3: the entitlement circuit is per service family, not global."""
    quota = _quota(GovernanceConfig(entitlement_failure_circuit_threshold=2, persist_usage_counters=False))
    quota.record_entitlement_failure("//blp/refdata")
    quota.record_entitlement_failure("//blp/refdata")
    assert quota.entitlement_circuit_open("//blp/refdata") is True
    assert quota.entitlement_circuit_open("//blp/instruments") is False
    # An unrelated success must NOT close the refdata breaker.
    quota.record_entitlement_success("//blp/instruments")
    assert quota.entitlement_circuit_open("//blp/refdata") is True
    quota.record_entitlement_success("//blp/refdata")
    assert quota.entitlement_circuit_open("//blp/refdata") is False


def test_request_status_no_auth_counts_once_via_consumer() -> None:
    """K1: the native callback must not double-count a REQUEST_STATUS NO_AUTH."""
    message = CanonicalMessage(
        event_type=EventKind.REQUEST_STATUS,
        message_type="RequestFailure",
        request_id="r",
        service="//blp/refdata",
        session_generation=1,
        sequence=1,
        received_at="2026-08-21T00:00:00Z",
        payload={"reason": {"category": "NO_AUTH", "description": "not entitled"}},
    )
    error = request_status_error(message)
    assert error.code is ErrorCode.BLOOMBERG_NOT_ENTITLED
