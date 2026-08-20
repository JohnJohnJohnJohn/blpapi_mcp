"""Stable application error codes and the gateway error model (SPEC §3.3).

Protocol-version and header-mismatch errors use the MCP SDK's protocol-defined
error shapes rather than these application codes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_SERVICE = "INVALID_SERVICE"
    INVALID_OPERATION = "INVALID_OPERATION"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    UNKNOWN_ELEMENT = "UNKNOWN_ELEMENT"
    MISSING_REQUIRED_ELEMENT = "MISSING_REQUIRED_ELEMENT"
    INVALID_ELEMENT_TYPE = "INVALID_ELEMENT_TYPE"
    INVALID_ENUM_VALUE = "INVALID_ENUM_VALUE"
    INVALID_CHOICE = "INVALID_CHOICE"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
    RESULT_EXPIRED = "RESULT_EXPIRED"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
    REQUEST_ALREADY_COMPLETE = "REQUEST_ALREADY_COMPLETE"
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    SUBSCRIPTION_EXPIRED = "SUBSCRIPTION_EXPIRED"
    SUBSCRIPTION_LIMIT_EXCEEDED = "SUBSCRIPTION_LIMIT_EXCEEDED"
    CURSOR_INVALID = "CURSOR_INVALID"
    QUEUE_FULL = "QUEUE_FULL"
    RATE_LIMITED = "RATE_LIMITED"
    LICENSE_BUDGET_EXCEEDED = "LICENSE_BUDGET_EXCEEDED"
    SCHEMA_DRIFT_DETECTED = "SCHEMA_DRIFT_DETECTED"
    NORMALIZER_NOT_AVAILABLE = "NORMALIZER_NOT_AVAILABLE"
    ARTIFACT_FORMAT_NOT_AVAILABLE = "ARTIFACT_FORMAT_NOT_AVAILABLE"
    BLOOMBERG_NOT_CONNECTED = "BLOOMBERG_NOT_CONNECTED"
    BLOOMBERG_TERMINAL_NOT_LOGGED_IN = "BLOOMBERG_TERMINAL_NOT_LOGGED_IN"
    BLOOMBERG_SESSION_FAILED = "BLOOMBERG_SESSION_FAILED"
    BLOOMBERG_SESSION_LOST = "BLOOMBERG_SESSION_LOST"
    BLOOMBERG_SERVICE_NOT_OPEN = "BLOOMBERG_SERVICE_NOT_OPEN"
    BLOOMBERG_SERVICE_OPEN_FAILED = "BLOOMBERG_SERVICE_OPEN_FAILED"
    BLOOMBERG_REQUEST_FAILED = "BLOOMBERG_REQUEST_FAILED"
    BLOOMBERG_RESPONSE_ERROR = "BLOOMBERG_RESPONSE_ERROR"
    BLOOMBERG_SECURITY_ERROR = "BLOOMBERG_SECURITY_ERROR"
    BLOOMBERG_FIELD_ERROR = "BLOOMBERG_FIELD_ERROR"
    BLOOMBERG_NOT_ENTITLED = "BLOOMBERG_NOT_ENTITLED"
    BLOOMBERG_SUBSCRIPTION_FAILED = "BLOOMBERG_SUBSCRIPTION_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Error codes after which a retried identical request may succeed.
RETRYABLE_CODES = frozenset(
    {
        ErrorCode.BLOOMBERG_SESSION_LOST,
        ErrorCode.BLOOMBERG_NOT_CONNECTED,
        ErrorCode.TIMEOUT,
        ErrorCode.QUEUE_FULL,
        ErrorCode.RATE_LIMITED,
    }
)

#: Generic, externally-safe message for unexpected failures (SPEC §4.11).
GENERIC_INTERNAL_MESSAGE = "Internal gateway error."


class GatewayError(Exception):
    """Application-level gateway error carrying a stable code.

    Immutable by construction (attributes are set once in ``__init__``);
    serializable into result envelopes and storable on request records.
    """

    code: ErrorCode
    message: str
    retryable: bool
    details: dict[str, Any]

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable or code in RETRYABLE_CODES
        self.details = details if details is not None else {}

    def to_dict(self, *, expose_details: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if expose_details and self.details:
            payload["details"] = self.details
        return payload


def internal_error(detail: str = "") -> GatewayError:
    """Return a sanitized INTERNAL_ERROR; details stay in local logs only."""
    return GatewayError(
        code=ErrorCode.INTERNAL_ERROR,
        message=GENERIC_INTERNAL_MESSAGE if not detail else GENERIC_INTERNAL_MESSAGE,
        details={"local_detail": detail} if detail else {},
    )
