"""Native event decoding into canonical models (SPEC §2.4, §3.7).

This module is part of the native adapter boundary: it receives native
``blpapi`` Event/Message/Element objects and returns only immutable,
JSON-safe canonical values. Native references must not escape.

Native methods used (blpapi 3.26.7.1):
``Event.eventType``, ``Event.__iter__`` (via numMessages/messageAt),
``Message.messageType``, ``Message.correlationIds``, ``Message.asElement``,
``Element.numValues``, ``Element.datatype``, ``Element.isNullValue``,
``Element.getValueAsBool/Integer/Float/String/Bytes/Datetime/Element``,
``Element.getChoice``, ``CorrelationId.value``, ``Name.__str__``.
"""

from __future__ import annotations

import logging
from typing import Any

import blpapi

from bloomberg_mcp.blp.value_codec import encode_value
from bloomberg_mcp.models import BloombergDatatype, CanonicalMessage, EventKind, utc_now

logger = logging.getLogger(__name__)

_DATATYPE_MAP: dict[int, BloombergDatatype] = {
    blpapi.DataType.BOOL: BloombergDatatype.BOOL,
    blpapi.DataType.BYTE: BloombergDatatype.BYTE,
    blpapi.DataType.BYTEARRAY: BloombergDatatype.BYTEARRAY,
    blpapi.DataType.CHAR: BloombergDatatype.CHAR,
    blpapi.DataType.CHOICE: BloombergDatatype.CHOICE,
    blpapi.DataType.CORRELATION_ID: BloombergDatatype.CORRELATION_ID,
    blpapi.DataType.DATE: BloombergDatatype.DATE,
    blpapi.DataType.DATETIME: BloombergDatatype.DATETIME,
    blpapi.DataType.DECIMAL: BloombergDatatype.DECIMAL,
    blpapi.DataType.ENUMERATION: BloombergDatatype.ENUMERATION,
    blpapi.DataType.FLOAT32: BloombergDatatype.FLOAT32,
    blpapi.DataType.FLOAT64: BloombergDatatype.FLOAT64,
    blpapi.DataType.INT32: BloombergDatatype.INT32,
    blpapi.DataType.INT64: BloombergDatatype.INT64,
    blpapi.DataType.SEQUENCE: BloombergDatatype.SEQUENCE,
    blpapi.DataType.STRING: BloombergDatatype.STRING,
    blpapi.DataType.TIME: BloombergDatatype.TIME,
}

_EVENT_KIND_MAP: dict[int, EventKind] = {
    blpapi.Event.PARTIAL_RESPONSE: EventKind.PARTIAL_RESPONSE,
    blpapi.Event.RESPONSE: EventKind.RESPONSE,
    blpapi.Event.REQUEST_STATUS: EventKind.REQUEST_STATUS,
    blpapi.Event.SESSION_STATUS: EventKind.SESSION_STATUS,
    blpapi.Event.SERVICE_STATUS: EventKind.SERVICE_STATUS,
    blpapi.Event.SUBSCRIPTION_STATUS: EventKind.SUBSCRIPTION_STATUS,
    blpapi.Event.SUBSCRIPTION_DATA: EventKind.SUBSCRIPTION_DATA,
    blpapi.Event.ADMIN: EventKind.ADMIN,
    blpapi.Event.TIMEOUT: EventKind.TIMEOUT,
}


def canonical_datatype(native: int) -> BloombergDatatype:
    return _DATATYPE_MAP.get(native, BloombergDatatype.UNSUPPORTED)


def canonical_event_kind(native: int) -> EventKind:
    return _EVENT_KIND_MAP.get(native, EventKind.UNKNOWN)


def decode_element(element: blpapi.Element, *, typed: bool) -> Any:
    """Recursively decode one native Element into canonical JSON-safe values."""
    datatype = canonical_datatype(element.datatype())

    if datatype == BloombergDatatype.SEQUENCE:
        if element.isArray():
            # Array of sequences: each entry is decoded as a sequence object.
            values: list[Any] = []
            for index in range(element.numValues()):
                child = element.getValueAsElement(index)
                values.append(decode_sequence_element(child, typed=typed))
            return values
        # Single sequence object: decode its named members directly.
        # (getValueAsElement / isNullValue are invalid for non-array sequences.)
        return decode_sequence_element(element, typed=typed)

    if datatype == BloombergDatatype.CHOICE:
        choice = element.getChoice()
        return {str(choice.name()): decode_element(choice, typed=typed)}

    count = element.numValues()
    if count == 0:
        return None
    if count == 1:
        if element.isNullValue(0):
            return None
        return _decode_scalar(element, 0, datatype, typed=typed)
    decoded: list[Any] = []
    for index in range(count):
        if element.isNullValue(index):
            decoded.append(None)
        else:
            decoded.append(_decode_scalar(element, index, datatype, typed=typed))
    return decoded


def decode_sequence_element(element: blpapi.Element, *, typed: bool) -> dict[str, Any]:
    """Decode a SEQUENCE-typed element (an object of named members)."""
    result: dict[str, Any] = {}
    for index in range(element.numElements()):
        member = element.getElement(index)
        result[str(member.name())] = decode_element(member, typed=typed)
    return result


def _decode_scalar(element: blpapi.Element, index: int, datatype: BloombergDatatype, *, typed: bool) -> Any:
    if datatype == BloombergDatatype.BOOL:
        value: Any = element.getValueAsBool(index)
    elif datatype in (BloombergDatatype.INT32, BloombergDatatype.INT64, BloombergDatatype.BYTE):
        value = element.getValueAsInteger(index)
    elif datatype in (BloombergDatatype.FLOAT32, BloombergDatatype.FLOAT64):
        value = element.getValueAsFloat(index)
    elif datatype in (BloombergDatatype.DATE, BloombergDatatype.TIME, BloombergDatatype.DATETIME):
        value = element.getValueAsDatetime(index)
    elif datatype == BloombergDatatype.BYTEARRAY:
        value = element.getValueAsBytes(index)
    elif datatype == BloombergDatatype.ENUMERATION:
        value = str(element.getValueAsName(index)) if _is_name_value(element) else element.getValueAsString(index)
    elif datatype == BloombergDatatype.CHOICE:
        choice = element.getValueAsElement(index)
        return {str(choice.name()): decode_element(choice, typed=typed)}
    elif datatype == BloombergDatatype.SEQUENCE:
        return decode_sequence_element(element.getValueAsElement(index), typed=typed)
    else:
        value = element.getValueAsString(index)
    return encode_value(value, datatype, typed=typed)


def _is_name_value(element: blpapi.Element) -> bool:
    try:
        element.getValueAsName(0)
        return True
    except Exception:
        return False


def decode_message(
    message: blpapi.Message,
    *,
    kind: EventKind,
    request_id: str | None,
    service: str | None,
    session_generation: int,
    sequence: int,
    typed: bool,
) -> CanonicalMessage:
    """Decode one native Message fully into a canonical message."""
    correlation_tokens: list[int] = []
    for cid in message.correlationIds():
        if cid.type() == blpapi.CorrelationId.INT_TYPE:
            correlation_tokens.append(int(cid.value()))
    payload = decode_sequence_element(message.asElement(), typed=typed)
    return CanonicalMessage(
        event_type=kind,
        message_type=str(message.messageType()),
        request_id=request_id,
        service=service,
        session_generation=session_generation,
        sequence=sequence,
        received_at=utc_now().isoformat(),
        payload=payload,
        correlation_token=correlation_tokens[0] if correlation_tokens else None,
    )


def decode_event(
    event: blpapi.Event,
    *,
    request_id: str | None,
    service: str | None,
    session_generation: int,
    start_sequence: int,
    typed: bool = False,
) -> list[CanonicalMessage]:
    """Decode every message in a native event; native objects do not escape."""
    kind = canonical_event_kind(event.eventType())
    messages: list[CanonicalMessage] = []
    sequence = start_sequence
    for message in event:
        messages.append(
            decode_message(
                message,
                kind=kind,
                request_id=request_id,
                service=service,
                session_generation=session_generation,
                sequence=sequence,
                typed=typed,
            )
        )
        sequence += 1
    return messages
