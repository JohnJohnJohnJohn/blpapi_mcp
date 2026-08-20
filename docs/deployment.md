# Deployment

Target: Windows Bloomberg Terminal workstation serving a Linux Hermes agent
over Tailscale (SPEC §4.12, §4.13).

## Prerequisites

- Bloomberg Terminal installed, logged on, Desktop API enabled
  (`127.0.0.1:8194`).
- Python 3.12 and `uv` (or plain `pip`).
- Tailscale installed and joined to the tailnet with tailnet HTTPS enabled.

Verified combinations are pinned in `compatibility.lock.yaml` and `uv.lock`.

## 1. Install

```powershell
git clone <repo> C:\Tools\bloomberg-mcp
cd C:\Tools\bloomberg-mcp
.\scripts\install.ps1
```

Creates `.venv` (project-local; global environments untouched), installs
`blpapi` from the Bloomberg package index and the locked dependencies.

## 2. Milestone 0 probe

```powershell
.\scripts\environment-probe.ps1
```

Records exact versions, connects to BBComm, opens configured services and
dumps operation/schema introspection into `probe-output/` (git-ignored).
Bloomberg-side items (entitled services, installed-version limits) must be
cross-checked on the workstation via `WAPI <GO>` (SPEC §1.8).

## 3. Configure

- Copy `.env.example` to `.env` semantics: set
  `BLOOMBERG_MCP_BEARER_TOKEN` (≥ 256-bit) in the environment of the
  Bloomberg user, or store it in the Windows Credential Manager under
  `BloombergMCP/bearer` and set `auth.token_source:
  windows_credential_manager`.
- Review `config/default.yaml` and the policy file; in particular
  `server.allowed_hosts` (add the workstation's tailnet DNS name so
  Tailscale-proxied requests pass Host validation).

## 4. Run

Interactive/foreground:

```powershell
.\scripts\run.ps1                 # native backend
.\scripts\run.ps1 -Backend fake   # CI / no Terminal
```

Production: register the scheduled task **as the Bloomberg Terminal user**
(the script refuses LocalSystem):

```powershell
.\scripts\register-task.ps1 -User "CONTOSO\bloomberg-user"
Start-ScheduledTask -TaskName BloombergMCP-Gateway
```

The task runs after that user logs on, restarts on failure with bounded
retry, and stops on logout/shutdown. A named Windows mutex prevents a second
gateway instance.

### Stopping the gateway

```powershell
.\scripts\stop.ps1
```

Handles every launch mode: stops the scheduled task if it is running,
otherwise terminates the `bloomberg_mcp.main` python process(es). For an
interactive foreground run, `Ctrl+C` also performs a graceful shutdown.
Stopping is always safe: all writes are atomic and no state requires a
graceful flush.

## 5. Tailscale Serve

```powershell
.\scripts\configure-tailscale.ps1
```

Configures private HTTPS (`tailscale serve`) proxying to
`http://127.0.0.1:8765` and verifies Funnel is off. Then, in the tailnet ACL:

- dedicated tag for the workstation (e.g. `tag:bloomberg-mcp`) and for Hermes
  (e.g. `tag:hermes`);
- default-deny; allow only `tag:hermes` to reach the workstation's HTTPS
  service.

## 6. Verify

```powershell
.\scripts\health-check.ps1
```

- `/health/live` is unauthenticated and reveals nothing but liveness.
- `/health/ready` (bearer-authenticated) reports session state, per-service
  open state, admission and the entitlement circuit.

End-to-end from Hermes: point the MCP client at
`https://<workstation>.<tailnet>.ts.net/mcp` with the bearer token, protocol
revision `2026-07-28`, and exercise discovery → reference → historical flows.

## Recovery expectations (SPEC §2.6)

- Terminal/BBComm drop → session RECONNECTING with backoff; in-flight
  requests fail `BLOOMBERG_SESSION_LOST` (never replayed); after reconnect
  services reopen, generation increments, schema caches invalidate, and
  subscriptions restore (new generation, data-gap warning) when enabled.
- Gateway process death → scheduled task restarts it; request/subscription
  state is intentionally not durable across process restarts (SPEC §1.4).
