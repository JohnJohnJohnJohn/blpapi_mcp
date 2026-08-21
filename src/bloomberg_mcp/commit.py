"""Best-effort build-commit resolution for the /version endpoint.

Resolution order:
  1. BLOOMBERG_MCP_COMMIT environment variable (set by scripts/run.ps1).
  2. git HEAD of the repository containing this package (git rev-parse).
  3. A _COMMIT.txt file shipped next to this module (CI builds).
  4. None (unknown).

Importing this module must never raise: a failed resolution yields None.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _from_git() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git on PATH is intentional
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _from_file() -> str | None:
    try:
        text = (Path(__file__).resolve().parent / "_COMMIT.txt").read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def resolve_commit() -> str | None:
    env = os.environ.get("BLOOMBERG_MCP_COMMIT")
    if env:
        return env.strip() or None
    return _from_git() or _from_file()


COMMIT: str | None = resolve_commit()
