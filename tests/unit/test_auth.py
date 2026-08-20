"""Token verification and principal resolution (SPEC §1.7, §5.7)."""

from __future__ import annotations

import pytest

from bloomberg_mcp.auth.principal import Principal
from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.config import AuthConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.policy.models import PolicyConfig

GOOD_TOKEN = "unit-token-" + "a" * 48
PREVIOUS_TOKEN = "unit-token-" + "b" * 48


def _policy() -> PolicyConfig:
    return PolicyConfig(principals={"hermes": Principal("hermes", frozenset({"bloomberg:discover"}))})


def _verifier(monkeypatch: pytest.MonkeyPatch, *, previous: bool = False) -> TokenVerifier:
    monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN", GOOD_TOKEN)
    if previous:
        monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN_PREVIOUS", PREVIOUS_TOKEN)
    else:
        monkeypatch.delenv("BLOOMBERG_MCP_BEARER_TOKEN_PREVIOUS", raising=False)
    return TokenVerifier(AuthConfig(token_source="env"), _policy())


def test_valid_token_resolves_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = _verifier(monkeypatch).verify(GOOD_TOKEN)
    assert principal.principal_id == "hermes"
    assert principal.has_scope("bloomberg:discover")


def test_invalid_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch)
    with pytest.raises(GatewayError) as excinfo:
        verifier.verify("wrong-token")
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_rotation_overlap_accepts_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch, previous=True)
    assert verifier.verify(PREVIOUS_TOKEN).principal_id == "hermes"
    assert verifier.verify(GOOD_TOKEN).principal_id == "hermes"


def test_low_entropy_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOOMBERG_MCP_BEARER_TOKEN", "short")
    with pytest.raises(GatewayError) as excinfo:
        TokenVerifier(AuthConfig(token_source="env"), _policy())
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_missing_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLOOMBERG_MCP_BEARER_TOKEN", raising=False)
    with pytest.raises(GatewayError) as excinfo:
        TokenVerifier(AuthConfig(token_source="env"), _policy())
    assert excinfo.value.code is ErrorCode.AUTH_REQUIRED


def test_file_token_source(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text(GOOD_TOKEN + "\n", encoding="utf-8")
    monkeypatch.delenv("BLOOMBERG_MCP_BEARER_TOKEN", raising=False)
    verifier = TokenVerifier(AuthConfig(token_source="file", token_file=str(token_file)), _policy())
    assert verifier.verify(GOOD_TOKEN).principal_id == "hermes"


def test_scope_check() -> None:
    principal = Principal("p", frozenset({"bloomberg:reference"}))
    assert principal.has_scope("bloomberg:reference")
    assert not principal.has_scope("bloomberg:historical")
    assert Principal("admin", frozenset(), admin=True).has_scope("anything")
