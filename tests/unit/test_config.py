"""Configuration parsing and environment expansion (SPEC §4.1, §5.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bloomberg_mcp.config import expand_env, load_gateway_config
from bloomberg_mcp.errors import ErrorCode, GatewayError

REPO = str(Path(__file__).resolve().parents[2])


def test_default_config_loads() -> None:
    config = load_gateway_config(f"{REPO}/config/default.yaml")
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8765
    assert config.server.protocol_revision == "2026-07-28"
    assert config.server.stateless is True
    assert config.bloomberg.port == 8194
    assert config.requests.deduplication_window_seconds == 300
    assert config.governance.daily_request_budget == 10000


def test_missing_config_uses_defaults() -> None:
    config = load_gateway_config(None)
    assert config.backend == "native"
    assert config.server.max_request_body_bytes == 1_048_576


def test_env_expansion_windows_and_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BMCP_TEST_DIR", "C:/tmp/bmcp")
    assert expand_env("%BMCP_TEST_DIR%/data") == "C:/tmp/bmcp/data"
    assert expand_env("${BMCP_TEST_DIR}/data") == "C:/tmp/bmcp/data"
    assert expand_env("%BMCP_UNSET_XYZ%") == "%BMCP_UNSET_XYZ%"


def test_stateful_mode_rejected(tmp_path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("server:\n  stateless: false\n", encoding="utf-8")
    with pytest.raises(GatewayError) as excinfo:
        load_gateway_config(str(path))
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_remote_bloomberg_host_rejected(tmp_path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("bloomberg:\n  host: 10.0.0.5\n", encoding="utf-8")
    with pytest.raises(GatewayError) as excinfo:
        load_gateway_config(str(path))
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_request_replay_rejected(tmp_path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("bloomberg:\n  automatic_request_replay: true\n", encoding="utf-8")
    with pytest.raises(GatewayError):
        load_gateway_config(str(path))


def test_unknown_backend_rejected(tmp_path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("backend: magic\n", encoding="utf-8")
    with pytest.raises(GatewayError):
        load_gateway_config(str(path))
