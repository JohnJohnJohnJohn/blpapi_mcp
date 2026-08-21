"""commit.py resolution tests: never raises, returns str or None."""

from __future__ import annotations

import bloomberg_mcp.commit as commit_mod


def test_resolve_commit_never_raises() -> None:
    value = commit_mod.resolve_commit()
    assert value is None or isinstance(value, str)
    assert value is None or len(value) in (40, 64)


def test_env_var_wins() -> None:
    import os

    os.environ["BLOOMBERG_MCP_COMMIT"] = "a" * 40
    try:
        assert commit_mod.resolve_commit() == "a" * 40
    finally:
        del os.environ["BLOOMBERG_MCP_COMMIT"]


def test_module_constant_is_string_or_none() -> None:
    assert commit_mod.COMMIT is None or isinstance(commit_mod.COMMIT, str)
