# Bloomberg MCP Gateway

Production-grade MCP gateway that gives a Linux-hosted agent (Hermes) controlled
access to the Bloomberg Desktop API (`blpapi`) on a Windows Bloomberg Terminal
workstation, over a private Tailscale network.

- **Protocol:** stateless MCP Streamable HTTP, revision `2026-07-28`
- **Transport:** HTTPS via Tailscale Serve -> `http://127.0.0.1:8765`
- **Bloomberg:** Desktop API only, local `127.0.0.1:8194` (BBComm never exposed)
- **Auth:** private static bearer tokens mapped to server-owned principals
- **Safety:** policy, cost/quota budgets, principal-bound handles, audit trail,
  entitlement circuit breaker, bounded result storage
- **CI:** deterministic fake backend allows development and testing without a
  Bloomberg Terminal

See `SPEC.md` for the full design specification.

## Layout

```
src/bloomberg_mcp/   gateway implementation (native blpapi confined to blp/)
config/              default configuration and policy examples
scripts/             PowerShell deployment and operations scripts
tests/               unit, property and contract tests (fake backend)
docs/                architecture, deployment, security and operations docs
```

## Development setup (Windows)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). All work happens in a
project-local virtual environment (`.venv`); global Python environments are never
touched.

```powershell
# 1. Create the isolated virtual environment
uv venv

# 2. Install the gateway with dev tooling (blpapi is fetched from the
#    Bloomberg package index automatically, see pyproject.toml)
uv sync --extra dev

# 3. Run the test suite against the deterministic fake backend
uv run pytest

# 4. Lint and type-check
uv run ruff check src tests
uv run mypy
```

Without `uv`, the equivalent plain-Python flow is:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]" `
  --extra-index-url https://blpapi.bloomberg.com/repository/releases/python/simple/
```

## Running the gateway

```powershell
# Fake backend (no Bloomberg Terminal required)
uv run bloomberg-mcp --backend fake

# Native backend (Bloomberg Terminal must be running and logged on)
uv run bloomberg-mcp --config config/default.yaml --policy config/policy.example.yaml
```

The gateway binds `127.0.0.1:8765` by default and refuses to start twice
(Windows named mutex). Configure Tailscale Serve separately
(`scripts/configure-tailscale.ps1`) to terminate tailnet-only HTTPS.

## Milestone status

| Milestone | Scope | Status |
|---|---|---|
| 0 | Environment probe + compatibility lock | See `compatibility.lock.yaml` |
| 1 | Core generic gateway (fake + native adapter) | Implemented |
| 2 | Production lifecycle (reconnect, quotas, storage, audit) | Implemented |
| 3 | Subscriptions | Implemented |
| 4 | Curated tools + normalizers | Implemented |

Bloomberg-side verification items (Terminal connectivity, entitlements,
operation limits) require an actual logged-on Terminal; see
`docs/deployment.md` and `scripts/environment-probe.ps1`.

## Security notes

- Never expose port 8765 or BBComm port 8194 beyond localhost.
- Bearer tokens must have >= 256 bits of entropy; rotate with a bounded overlap.
- Policy limits do not replace Bloomberg contractual or entitlement limits.
