"""Gateway process entrypoint (SPEC §1.5, §4.13).

Runs under the interactive Bloomberg user, binds 127.0.0.1 only, acquires the
single-instance lock before opening any Bloomberg session, and never exposes
BBComm (8194) beyond localhost.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

import uvicorn

from bloomberg_mcp import __version__
from bloomberg_mcp.auth.token_verifier import TokenVerifier
from bloomberg_mcp.config import GatewayConfig, load_dotenv, load_gateway_config
from bloomberg_mcp.errors import GatewayError
from bloomberg_mcp.gateway import Gateway
from bloomberg_mcp.instance_lock import InstanceLock, InstanceLockHeld
from bloomberg_mcp.mcp.server import build_app
from bloomberg_mcp.observability.audit import JsonFormatter
from bloomberg_mcp.policy.models import load_policy_config

logger = logging.getLogger("bloomberg_mcp")


def _configure_logging(config: GatewayConfig) -> None:
    root = logging.getLogger("bloomberg_mcp")
    root.setLevel(config.logging.level)
    handler: logging.Handler
    if config.logging.json:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers[:] = [handler]
    log_dir = os.path.expandvars(r"%LOCALAPPDATA%\BloombergMCP\logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "gateway.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    # Load .env (repo root) before reading env-driven defaults; already-set
    # environment variables always take precedence.
    load_dotenv()
    parser = argparse.ArgumentParser(prog="bloomberg-mcp", description="Bloomberg MCP Gateway")
    parser.add_argument("--config", default=os.environ.get("BLOOMBERG_MCP_CONFIG", "config/default.yaml"))
    parser.add_argument("--policy", default=os.environ.get("BLOOMBERG_MCP_POLICY", "config/policy.example.yaml"))
    parser.add_argument("--backend", choices=["native", "fake"], default=os.environ.get("BLOOMBERG_MCP_BACKEND"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--version", action="version", version=f"bloomberg-mcp {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_gateway_config(args.config)
        policy = load_policy_config(args.policy)
    except GatewayError as exc:
        print(f"configuration error: {exc.message}", file=sys.stderr)
        return 2

    if args.backend:
        config = GatewayConfig(
            backend=args.backend,
            server=config.server,
            bloomberg=config.bloomberg,
            requests=config.requests,
            subscriptions=config.subscriptions,
            storage=config.storage,
            auth=config.auth,
            governance=config.governance,
            audit=config.audit,
            logging=config.logging,
        )
    if args.host:
        config = _with_server(config, host=args.host, port=args.port)
    elif args.port:
        config = _with_server(config, host=config.server.host, port=args.port)

    _configure_logging(config)

    if not _port_available(config.server.host, config.server.port):
        print(
            f"error: cannot bind {config.server.host}:{config.server.port} - the port is already in use.\n"
            f"Find the owner with:  netstat -ano | findstr :{config.server.port}\n"
            f"Free the port, or run the gateway on another one, e.g.\n"
            f"  scripts\\run.ps1 -Port {config.server.port + 1}",
            file=sys.stderr,
        )
        return 4

    try:
        verifier = TokenVerifier(config.auth, policy)
    except GatewayError as exc:
        print(f"authentication configuration error: {exc.message}", file=sys.stderr)
        return 2

    lock = InstanceLock()
    try:
        lock.acquire()
    except InstanceLockHeld as exc:
        print(f"another gateway instance is running: {exc}", file=sys.stderr)
        return 3

    gateway = Gateway(config, policy)
    app = build_app(gateway, verifier)

    host = config.server.host
    port = config.server.port
    logger.info("starting bloomberg-mcp %s on %s:%d (backend=%s)", __version__, host, port, config.backend)
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=config.logging.level.lower(),
            timeout_graceful_shutdown=config.server.shutdown_timeout_seconds,
        )
    finally:
        lock.release()
    return 0


def _with_server(config: GatewayConfig, *, host: str, port: int | None) -> GatewayConfig:
    from dataclasses import replace

    server = replace(config.server, host=host, port=port if port is not None else config.server.port)
    return replace(config, server=server)


def _port_available(host: str, port: int) -> bool:
    """Bind probe without SO_REUSEADDR so real conflicts are detected."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
