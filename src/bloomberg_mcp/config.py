"""Configuration loading and validation (SPEC §4.1).

Supports ``%ENVVAR%`` (Windows-style) and ``${ENVVAR}`` expansion in string
values, e.g. the default artifact directory ``%LOCALAPPDATA%\\BloombergMCP\\data``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from bloomberg_mcp.errors import ErrorCode, GatewayError

_WIN_ENV_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_POSIX_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str) -> str:
    def _win(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    def _posix(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    return _POSIX_ENV_RE.sub(_posix, _WIN_ENV_RE.sub(_win, value))


def load_dotenv(path: str = ".env") -> int:
    """Load ``KEY=VALUE`` pairs from a dotenv file into the process environment.

    Existing environment variables take precedence. The file is read once at
    startup, stays process-scoped, and is never committed (git-ignored).
    Returns the number of variables loaded.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return 0
    loaded = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _expand(node: Any) -> Any:
    if isinstance(node, str):
        return expand_env(node)
    if isinstance(node, list):
        return [_expand(v) for v in node]
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    return node


def _get(raw: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _req_int(raw: dict[str, Any], path: str, default: int, *, minimum: int = 0) -> int:
    value = _get(raw, path, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise GatewayError(
            ErrorCode.INVALID_ARGUMENT,
            f"config {path!r} must be an integer >= {minimum}",
        )
    return value


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    mcp_path: str = "/mcp"
    protocol_revision: str = "2026-07-28"
    stateless: bool = True
    max_request_body_bytes: int = 1_048_576
    shutdown_timeout_seconds: int = 30
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    artifact_endpoint_enabled: bool = False
    metrics_endpoint_enabled: bool = True


@dataclass(frozen=True)
class ReconnectConfig:
    enabled: bool = True
    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.2


@dataclass(frozen=True)
class BloombergConfig:
    host: str = "127.0.0.1"
    port: int = 8194
    connect_timeout_seconds: int = 10
    automatic_request_replay: bool = False
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    startup_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestsConfig:
    max_concurrent: int = 4
    max_queued: int = 50
    default_wait_seconds: int = 30
    maximum_wait_seconds: int = 60
    default_deadline_seconds: int = 120
    maximum_deadline_seconds: int = 300
    deduplication_window_seconds: int = 300
    maximum_response_bytes: int = 268_435_456
    inline_result_bytes: int = 1_048_576
    result_ttl_seconds: int = 86_400


@dataclass(frozen=True)
class SubscriptionsConfig:
    maximum_per_principal: int = 20
    maximum_topics_per_group: int = 100
    maximum_fields_per_topic: int = 50
    maximum_buffered_events: int = 10_000
    maximum_ttl_seconds: int = 86_400
    default_ttl_seconds: int = 3_600
    maximum_long_poll_seconds: int = 15
    maximum_concurrent_long_polls: int = 10
    restore_after_reconnect: bool = True


@dataclass(frozen=True)
class StorageConfig:
    enabled: bool = True
    directory: str = ""
    maximum_total_bytes: int = 10_737_418_240
    cleanup_interval_seconds: int = 300
    default_canonical_format: str = "jsonl"
    default_tabular_format: str = "parquet"


@dataclass(frozen=True)
class AuthConfig:
    profile: str = "private-static-bearer"
    mcp_oauth_compliant: bool = False
    token_source: str = "env"  # noqa: S105 - env | file | windows_credential_manager
    token_file: str = ""
    credential_target: str = "BloombergMCP/bearer"
    token_overlap_seconds: int = 3_600


@dataclass(frozen=True)
class GovernanceConfig:
    daily_request_budget: int = 10_000
    monthly_request_budget: int = 200_000
    entitlement_failure_circuit_threshold: int = 5
    persist_usage_counters: bool = True
    persist_result_artifacts: bool = True


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = True
    include_security_names: bool = False
    include_field_names: bool = True
    include_parameters: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    json: bool = True


@dataclass(frozen=True)
class GatewayConfig:
    backend: str = "native"
    server: ServerConfig = field(default_factory=ServerConfig)
    bloomberg: BloombergConfig = field(default_factory=BloombergConfig)
    requests: RequestsConfig = field(default_factory=RequestsConfig)
    subscriptions: SubscriptionsConfig = field(default_factory=SubscriptionsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_gateway_config(path: str | None) -> GatewayConfig:
    raw: dict[str, Any] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except OSError as exc:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"cannot read config {path}: {exc}") from exc
        if loaded:
            if not isinstance(loaded, dict):
                raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"config {path} must be a mapping")
            raw = _expand(loaded)

    server_raw = raw.get("server", {}) or {}
    server = ServerConfig(
        host=str(server_raw.get("host", "127.0.0.1")),
        port=_req_int(raw, "server.port", 8765, minimum=1),
        mcp_path=str(server_raw.get("mcp_path", "/mcp")),
        protocol_revision=str(server_raw.get("protocol_revision", "2026-07-28")),
        stateless=bool(server_raw.get("stateless", True)),
        max_request_body_bytes=_req_int(raw, "server.max_request_body_bytes", 1048576, minimum=1),
        shutdown_timeout_seconds=_req_int(raw, "server.shutdown_timeout_seconds", 30, minimum=1),
        allowed_hosts=tuple(server_raw.get("allowed_hosts", []) or []),
        allowed_origins=tuple(server_raw.get("allowed_origins", []) or []),
        artifact_endpoint_enabled=bool(server_raw.get("artifact_endpoint_enabled", False)),
        metrics_endpoint_enabled=bool(server_raw.get("metrics_endpoint_enabled", True)),
    )
    if not server.stateless:
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "server.stateless must be true (SPEC §1.6)")

    blp_raw = raw.get("bloomberg", {}) or {}
    reconnect_raw = blp_raw.get("reconnect", {}) or {}
    bloomberg = BloombergConfig(
        host=str(blp_raw.get("host", "127.0.0.1")),
        port=_req_int(raw, "bloomberg.port", 8194, minimum=1),
        connect_timeout_seconds=_req_int(raw, "bloomberg.connect_timeout_seconds", 10, minimum=1),
        automatic_request_replay=bool(blp_raw.get("automatic_request_replay", False)),
        reconnect=ReconnectConfig(
            enabled=bool(reconnect_raw.get("enabled", True)),
            initial_delay_seconds=float(reconnect_raw.get("initial_delay_seconds", 1.0)),
            maximum_delay_seconds=float(reconnect_raw.get("maximum_delay_seconds", 60.0)),
            multiplier=float(reconnect_raw.get("multiplier", 2.0)),
            jitter=float(reconnect_raw.get("jitter", 0.2)),
        ),
        startup_services=tuple(blp_raw.get("startup_services", []) or []),
    )
    if bloomberg.automatic_request_replay:
        raise GatewayError(
            ErrorCode.INVALID_ARGUMENT,
            "bloomberg.automatic_request_replay must be false in version 1 (SPEC §2.6)",
        )
    if (bloomberg.host, bloomberg.port) != ("127.0.0.1", 8194):
        raise GatewayError(
            ErrorCode.INVALID_ARGUMENT,
            "the gateway must use the local Bloomberg Desktop API endpoint 127.0.0.1:8194 (SPEC §1.5)",
        )

    requests = RequestsConfig(
        max_concurrent=_req_int(raw, "requests.max_concurrent", 4, minimum=1),
        max_queued=_req_int(raw, "requests.max_queued", 50, minimum=0),
        default_wait_seconds=_req_int(raw, "requests.default_wait_seconds", 30, minimum=1),
        maximum_wait_seconds=_req_int(raw, "requests.maximum_wait_seconds", 60, minimum=1),
        default_deadline_seconds=_req_int(raw, "requests.default_deadline_seconds", 120, minimum=1),
        maximum_deadline_seconds=_req_int(raw, "requests.maximum_deadline_seconds", 300, minimum=1),
        deduplication_window_seconds=_req_int(raw, "requests.deduplication_window_seconds", 300, minimum=0),
        maximum_response_bytes=_req_int(raw, "requests.maximum_response_bytes", 268435456, minimum=1),
        inline_result_bytes=_req_int(raw, "requests.inline_result_bytes", 1048576, minimum=1),
        result_ttl_seconds=_req_int(raw, "requests.result_ttl_seconds", 86400, minimum=1),
    )

    sub_raw = raw.get("subscriptions", {}) or {}
    subscriptions = SubscriptionsConfig(
        maximum_per_principal=_req_int(raw, "subscriptions.maximum_per_principal", 20, minimum=1),
        maximum_topics_per_group=_req_int(raw, "subscriptions.maximum_topics_per_group", 100, minimum=1),
        maximum_fields_per_topic=_req_int(raw, "subscriptions.maximum_fields_per_topic", 50, minimum=1),
        maximum_buffered_events=_req_int(raw, "subscriptions.maximum_buffered_events", 10000, minimum=1),
        maximum_ttl_seconds=_req_int(raw, "subscriptions.maximum_ttl_seconds", 86400, minimum=1),
        default_ttl_seconds=_req_int(raw, "subscriptions.default_ttl_seconds", 3600, minimum=1),
        maximum_long_poll_seconds=_req_int(raw, "subscriptions.maximum_long_poll_seconds", 15, minimum=0),
        maximum_concurrent_long_polls=_req_int(raw, "subscriptions.maximum_concurrent_long_polls", 10, minimum=1),
        restore_after_reconnect=bool(sub_raw.get("restore_after_reconnect", True)),
    )

    storage_raw = raw.get("storage", {}) or {}
    storage = StorageConfig(
        enabled=bool(storage_raw.get("enabled", True)),
        directory=str(storage_raw.get("directory", "")),
        maximum_total_bytes=_req_int(raw, "storage.maximum_total_bytes", 10737418240, minimum=1),
        cleanup_interval_seconds=_req_int(raw, "storage.cleanup_interval_seconds", 300, minimum=1),
        default_canonical_format=str(storage_raw.get("default_canonical_format", "jsonl")),
        default_tabular_format=str(storage_raw.get("default_tabular_format", "parquet")),
    )

    auth_raw = raw.get("auth", {}) or {}
    auth = AuthConfig(
        profile=str(auth_raw.get("profile", "private-static-bearer")),
        mcp_oauth_compliant=bool(auth_raw.get("mcp_oauth_compliant", False)),
        token_source=str(auth_raw.get("token_source", "env")),
        token_file=str(auth_raw.get("token_file", "")),
        credential_target=str(auth_raw.get("credential_target", "BloombergMCP/bearer")),
        token_overlap_seconds=_req_int(raw, "auth.token_overlap_seconds", 3600, minimum=0),
    )
    if auth.profile != "private-static-bearer":
        raise GatewayError(
            ErrorCode.INVALID_ARGUMENT,
            f"unsupported auth profile {auth.profile!r}; version 1 implements private-static-bearer only",
        )

    governance = GovernanceConfig(
        daily_request_budget=_req_int(raw, "governance.daily_request_budget", 10000, minimum=1),
        monthly_request_budget=_req_int(raw, "governance.monthly_request_budget", 200000, minimum=1),
        entitlement_failure_circuit_threshold=_req_int(
            raw, "governance.entitlement_failure_circuit_threshold", 5, minimum=1
        ),
        persist_usage_counters=bool(_get(raw, "governance.persist_usage_counters", True)),
        persist_result_artifacts=bool(_get(raw, "governance.persist_result_artifacts", True)),
    )

    audit_raw = raw.get("audit", {}) or {}
    audit = AuditConfig(
        enabled=bool(audit_raw.get("enabled", True)),
        include_security_names=bool(audit_raw.get("include_security_names", False)),
        include_field_names=bool(audit_raw.get("include_field_names", True)),
        include_parameters=bool(audit_raw.get("include_parameters", False)),
    )

    logging_raw = raw.get("logging", {}) or {}
    logging_cfg = LoggingConfig(
        level=str(logging_raw.get("level", "INFO")).upper(),
        json=bool(logging_raw.get("json", True)),
    )

    backend = str(raw.get("backend", "native"))
    if backend not in ("native", "fake"):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, f"backend must be native or fake, got {backend!r}")

    return GatewayConfig(
        backend=backend,
        server=server,
        bloomberg=bloomberg,
        requests=requests,
        subscriptions=subscriptions,
        storage=storage,
        auth=auth,
        governance=governance,
        audit=audit,
        logging=logging_cfg,
    )
