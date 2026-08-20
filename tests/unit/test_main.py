"""Startup behavior of the CLI entrypoint (bind-host sentinel)."""

from __future__ import annotations

import pytest

from bloomberg_mcp import main as main_module
from bloomberg_mcp.config import load_gateway_config


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "server:\n  host: tailscale\n  allowed_hosts: ['zhua8634-hppc:*']\n", encoding="utf-8"
    )
    return load_gateway_config(str(path))


def test_tailscale_sentinel_resolves_bind_host(config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "_resolve_tailscale_ip", lambda: "100.77.231.91")
    resolved = main_module._apply_bind_host(config)
    assert resolved.server.host == "100.77.231.91"
    # Resolved address is allow-listed for Host validation.
    assert "100.77.231.91:*" in resolved.server.allowed_hosts
    assert "zhua8634-hppc:*" in resolved.server.allowed_hosts


def test_tailscale_sentinel_failure_exits(config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "_resolve_tailscale_ip", lambda: None)
    with pytest.raises(SystemExit):
        main_module._apply_bind_host(config)


def test_non_sentinel_host_untouched(config, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    fixed = replace(config, server=replace(config.server, host="127.0.0.1"))

    def _boom() -> str:
        raise AssertionError("resolver must not run for non-sentinel hosts")

    monkeypatch.setattr(main_module, "_resolve_tailscale_ip", _boom)
    assert main_module._apply_bind_host(fixed).server.host == "127.0.0.1"
