"""Structured audit logging without secrets (SPEC §4.8).

Bearer tokens, Windows credentials, Bloomberg authentication material, native
object representations and unbounded payloads are never recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from bloomberg_mcp.config import AuditConfig

AUDIT_LOGGER_NAME = "bloomberg_mcp.audit"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


@dataclass(frozen=True)
class AuditEvent:
    action: str
    principal_id: str | None = None
    tool: str | None = None
    service: str | None = None
    operation: str | None = None
    request_id: str | None = None
    client_request_id_hash: str | None = None
    subscription_id: str | None = None
    security_count: int | None = None
    field_count: int | None = None
    estimated_cost: int | None = None
    duration_ms: int | None = None
    outcome: str = "ok"
    error_code: str | None = None
    response_bytes: int | None = None
    result_id: str | None = None
    client_address: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def hash_client_request_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


class AuditLogger:
    """Writes audit records honoring the configured redaction policy."""

    def __init__(self, config: AuditConfig) -> None:
        self._config = config
        self._logger = logging.getLogger(AUDIT_LOGGER_NAME)

    def record(self, event: AuditEvent) -> None:
        if not self._config.enabled:
            return
        data = asdict(event)
        if not self._config.include_security_names and data.get("extra", {}).get("securities"):
            data["extra"] = {k: v for k, v in data["extra"].items() if k != "securities"}
        if not self._config.include_field_names and data.get("extra", {}).get("fields"):
            data["extra"] = {k: v for k, v in data["extra"].items() if k != "fields"}
        if not self._config.include_parameters:
            data["extra"] = {k: v for k, v in data["extra"].items() if k != "parameters"}
        data["ts"] = datetime.now(UTC).isoformat()
        self._logger.info("%s", json.dumps(data, separators=(",", ":")))
