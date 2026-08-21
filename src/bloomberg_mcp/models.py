"""Canonical, immutable data models shared across the gateway.

Nothing in this module imports ``blpapi`` (SPEC §1.5: only native adapter
modules may). Native Bloomberg objects never cross the adapter boundary
(SPEC §2.4); every model here is built from JSON-safe primitives.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from bloomberg_mcp.errors import GatewayError


def utc_now() -> datetime:
    """Gateway event timestamps use UTC (SPEC §2.12)."""
    return datetime.now(UTC)

# Canonical JSON-safe value: str / int / float / bool / None / list / dict.
CanonicalJson = Any


class BloombergDatatype(StrEnum):
    """Canonical mirror of ``blpapi.DataType`` constants."""

    BOOL = "BOOL"
    BYTE = "BYTE"
    BYTEARRAY = "BYTEARRAY"
    CHAR = "CHAR"
    CHOICE = "CHOICE"
    CORRELATION_ID = "CORRELATION_ID"
    DATE = "DATE"
    DATETIME = "DATETIME"
    DECIMAL = "DECIMAL"
    ENUMERATION = "ENUMERATION"
    FLOAT32 = "FLOAT32"
    FLOAT64 = "FLOAT64"
    INT32 = "INT32"
    INT64 = "INT64"
    SEQUENCE = "SEQUENCE"
    STRING = "STRING"
    TIME = "TIME"
    UNSUPPORTED = "UNSUPPORTED"


class ResponseMode(StrEnum):
    CANONICAL = "canonical"
    TYPED = "typed"
    NORMALIZED = "normalized"


class SessionState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class RequestStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    SENT = "SENT"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


COMPLETE_REQUEST_STATUSES = frozenset(
    {
        RequestStatus.COMPLETED,
        RequestStatus.FAILED,
        RequestStatus.TIMED_OUT,
        RequestStatus.CANCELLED,
        RequestStatus.EXPIRED,
    }
)


class SubscriptionGroupStatus(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    RESUBSCRIBING = "RESUBSCRIBING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class SubscriptionItemStatus(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class EventKind(StrEnum):
    """Canonical event classification (mirrors ``blpapi.Event`` types)."""

    PARTIAL_RESPONSE = "PARTIAL_RESPONSE"
    RESPONSE = "RESPONSE"
    REQUEST_STATUS = "REQUEST_STATUS"
    SESSION_STATUS = "SESSION_STATUS"
    SERVICE_STATUS = "SERVICE_STATUS"
    SUBSCRIPTION_STATUS = "SUBSCRIPTION_STATUS"
    SUBSCRIPTION_DATA = "SUBSCRIPTION_DATA"
    ADMIN = "ADMIN"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ElementDescriptor:
    """Immutable schema descriptor for one Bloomberg element (SPEC §2.7)."""

    name: str
    alternate_names: tuple[str, ...] = ()
    datatype: BloombergDatatype = BloombergDatatype.STRING
    description: str | None = None
    status: str | None = None
    min_values: int = 0
    max_values: int | None = None  # None => unbounded array
    children: tuple[ElementDescriptor, ...] = ()
    enum_values: tuple[str, ...] = ()
    # For CHOICE elements: name -> child descriptor of each choice branch.
    choices: tuple[ElementDescriptor, ...] = ()
    # Bloomberg type-definition name used for $defs/$ref and cycle detection.
    type_name: str | None = None


@dataclass(frozen=True)
class OperationDescriptor:
    """Immutable schema descriptor for one Bloomberg operation (SPEC §2.7)."""

    service: str
    operation: str
    description: str | None
    request: ElementDescriptor | None
    responses: tuple[ElementDescriptor, ...]
    service_generation: int
    schema_hash: str


@dataclass(frozen=True)
class RequestCost:
    """Conservative request cost estimate (SPEC §4.4)."""

    securities: int = 0
    fields: int = 0
    repeating_elements: int = 0
    estimated_observations: int = 0
    risk_score: int = 0


@dataclass(frozen=True)
class CanonicalRequest:
    """Validated, immutable request ready for native construction (SPEC §2.8)."""

    service: str
    operation: str
    schema_hash: str
    parameters: Mapping[str, CanonicalJson]
    estimated_cost: RequestCost
    response_mode: ResponseMode = ResponseMode.CANONICAL
    normalized_schema_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class CanonicalMessage:
    """Fully decoded native event message (SPEC §3.7).

    ``payload`` contains only JSON-safe primitives; temporal values follow the
    fidelity rules of SPEC §2.12 (calendar dates stay dates, naive datetimes
    stay naive).
    """

    event_type: EventKind
    message_type: str
    request_id: str | None
    service: str | None
    session_generation: int
    sequence: int
    received_at: str  # UTC ISO 8601
    payload: Mapping[str, CanonicalJson]
    correlation_token: int | None = None


@dataclass(frozen=True)
class ItemError:
    """Per-item Bloomberg failure that can coexist with useful data."""

    kind: Literal["security", "field", "topic", "element", "message"]
    code: str
    message: str
    security: str | None = None
    field: str | None = None
    topic: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class GatewayWarning:
    code: str
    message: str


@dataclass
class RequestRecord:
    """Public state of one gateway request (SPEC §2.9)."""

    request_id: str
    principal_id: str
    client_request_id: str | None
    service: str
    operation: str
    schema_hash: str
    parameters_hash: str
    created_at: datetime
    deadline: datetime
    status: RequestStatus = RequestStatus.RECEIVED
    queued_at: datetime | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    session_generation: int | None = None
    native_token: int | None = None
    event_count: int = 0
    partial_response_count: int = 0
    byte_count: int = 0
    result_id: str | None = None
    messages: list[CanonicalMessage] = field(default_factory=list)
    item_errors: list[ItemError] = field(default_factory=list)
    warnings: list[GatewayWarning] = field(default_factory=list)
    error: GatewayError | None = None
    idempotent_replay: bool = False
    response_mode: str | None = None


@dataclass
class SubscriptionItem:
    """One native subscription item inside a group (SPEC §2.11)."""

    item_id: str
    topic: str
    fields: tuple[str, ...]
    options: Mapping[str, str]
    native_token: int
    status: SubscriptionItemStatus = SubscriptionItemStatus.CREATED
    sequence: int = 0


@dataclass
class SubscriptionGroup:
    """External subscription group owned by one principal (SPEC §2.11)."""

    subscription_id: str
    principal_id: str
    generation: int
    status: SubscriptionGroupStatus
    items: dict[str, SubscriptionItem] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    dropped_events: int = 0
    restored_with_gap: bool = False
