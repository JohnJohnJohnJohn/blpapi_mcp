"""Canonical value codec (SPEC §2.12, §3.7).

Fidelity rules implemented here:

- ``DATE`` stays a calendar date string; it is never converted to a UTC instant.
- ``TIME`` stays a time without an invented date.
- Aware ``DATETIME`` keeps its offset; naive ``DATETIME`` keeps ``timezone: null``
  and is never assumed UTC.
- Floats use the shortest round-trip representation; NaN/infinity use tagged
  values; integers outside the interoperable JSON range use tagged strings.
"""

from __future__ import annotations

import base64
import datetime as _dt
import math
import re
from typing import Any

from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype

# JSON integers are interoperable within the IEEE-754 exact range.
JSON_SAFE_INT_MAX = 2**53 - 1
JSON_SAFE_INT_MIN = -(2**53 - 1)

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?$")


def _iso_time(value: _dt.time) -> str:
    text = value.isoformat(timespec="microseconds")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _iso_datetime(value: _dt.datetime) -> str:
    text = value.isoformat(timespec="microseconds")
    if "." in text:
        base, _, tail = text.partition(".")
        frac = tail[:6].rstrip("0")
        suffix = ""
        # keep offset after fraction
        for sep in ("+", "-"):
            if sep in tail:
                idx = tail.index(sep)
                frac = tail[:idx].rstrip("0")
                suffix = tail[idx:]
                break
        text = f"{base}.{frac}{suffix}" if frac else f"{base}{suffix}"
    return text


def _timezone_label(value: _dt.datetime | _dt.time) -> str | None:
    """UTC offset label from the value itself; naive values yield None."""
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError):
        return None
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def encode_value(value: Any, datatype: BloombergDatatype, *, typed: bool) -> Any:
    """Encode a native Python value into canonical JSON-safe form."""
    if value is None:
        return None

    if datatype == BloombergDatatype.DATE:
        if isinstance(value, _dt.datetime):
            value = value.date()
        if isinstance(value, _dt.date):
            text = value.isoformat()
            return {"$blp_type": "DATE", "value": text} if typed else text
        raise GatewayError(ErrorCode.INTERNAL_ERROR, "DATE value is not a calendar date")

    if datatype == BloombergDatatype.TIME:
        if isinstance(value, _dt.time):
            text = _iso_time(value)
            if typed:
                return {"$blp_type": "TIME", "value": text, "timezone": _timezone_label(value)}
            return text
        raise GatewayError(ErrorCode.INTERNAL_ERROR, "TIME value is not a time")

    if datatype == BloombergDatatype.DATETIME:
        if isinstance(value, _dt.datetime):
            text = _iso_datetime(value)
            if typed:
                return {"$blp_type": "DATETIME", "value": text, "timezone": _timezone_label(value)}
            return text
        if isinstance(value, _dt.date):
            text = value.isoformat()
            return {"$blp_type": "DATE", "value": text} if typed else text
        raise GatewayError(ErrorCode.INTERNAL_ERROR, "DATETIME value is not a datetime")

    if datatype in (BloombergDatatype.FLOAT32, BloombergDatatype.FLOAT64):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            label = "NaN" if math.isnan(number) else ("Infinity" if number > 0 else "-Infinity")
            return {"$blp_type": datatype.value, "value": label}
        return number

    if datatype in (BloombergDatatype.INT32, BloombergDatatype.INT64,
                    BloombergDatatype.BYTE, BloombergDatatype.CHAR):
        number = int(value)
        if JSON_SAFE_INT_MIN <= number <= JSON_SAFE_INT_MAX:
            return number
        return {"$blp_type": datatype.value, "value": str(number)}

    if datatype == BloombergDatatype.BOOL:
        return bool(value)

    if datatype == BloombergDatatype.BYTEARRAY:
        data = bytes(value)
        encoded = base64.b64encode(data).decode("ascii")
        return {"$blp_type": "BYTEARRAY", "value": encoded}

    # STRING, ENUMERATION, CHOICE names, CORRELATION_ID and unsupported types
    # fall back to text; Bloomberg text is untrusted content downstream.
    text = value if isinstance(value, str) else str(value)
    if typed:
        return {"$blp_type": datatype.value, "value": text}
    return text


def _parse_date(text: str) -> _dt.date:
    for pattern in (_DATE_RE, _DATE_COMPACT_RE):
        m = pattern.match(text)
        if m:
            year, month, day = (int(g) for g in m.groups())
            try:
                return _dt.date(year, month, day)
            except ValueError as exc:
                raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Invalid date {text!r}") from exc
    raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Invalid date {text!r}; expected YYYY-MM-DD")


def _parse_time(text: str) -> _dt.time:
    m = _TIME_RE.match(text)
    if not m:
        raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Invalid time {text!r}; expected HH:MM:SS")
    hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = m.group(4) or ""
    microsecond = int(float(frac) * 1_000_000) if frac else 0
    try:
        return _dt.time(hour, minute, second, microsecond)
    except ValueError as exc:
        raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Invalid time {text!r}") from exc


def decode_input_value(value: Any, datatype: BloombergDatatype) -> Any:
    """Validate and convert a raw MCP input value to the canonical Python value.

    Returns native Python objects (``datetime.date``/``time``/``datetime``,
    ``int``, ``float``, ``bool``, ``str``, ``bytes``) that the native request
    builder can hand to ``blpapi``.
    """
    if value is None:
        return None

    if datatype in (BloombergDatatype.STRING, BloombergDatatype.ENUMERATION):
        if not isinstance(value, str):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Expected string, got {type(value).__name__}")
        return value

    if datatype in (BloombergDatatype.INT32, BloombergDatatype.INT64, BloombergDatatype.BYTE):
        if isinstance(value, bool) or not isinstance(value, int):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Expected integer, got {type(value).__name__}")
        low, high = (INT32_MIN, INT32_MAX) if datatype == BloombergDatatype.INT32 else (INT64_MIN, INT64_MAX)
        if not low <= value <= high:
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Integer out of {datatype.value} range")
        return value

    if datatype in (BloombergDatatype.FLOAT32, BloombergDatatype.FLOAT64):
        if isinstance(value, bool):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "Expected number, got boolean")
        if isinstance(value, int):
            return float(value)
        if isinstance(value, float):
            return value
        raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Expected number, got {type(value).__name__}")

    if datatype == BloombergDatatype.BOOL:
        if not isinstance(value, bool):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Expected boolean, got {type(value).__name__}")
        return value

    if datatype == BloombergDatatype.DATE:
        if not isinstance(value, str):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "DATE expects an ISO calendar date string")
        return _parse_date(value)

    if datatype == BloombergDatatype.TIME:
        if not isinstance(value, str):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "TIME expects an ISO time string")
        return _parse_time(value)

    if datatype == BloombergDatatype.DATETIME:
        if not isinstance(value, str):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "DATETIME expects an ISO 8601 string")
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Invalid datetime {value!r}") from exc
        return parsed

    if datatype == BloombergDatatype.BYTEARRAY:
        if not isinstance(value, str):
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "BYTEARRAY expects a base64 string")
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "Invalid base64 for BYTEARRAY") from exc

    if datatype == BloombergDatatype.CHAR:
        if not isinstance(value, str) or len(value) != 1:
            raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, "CHAR expects a single character")
        return value

    # CHOICE / SEQUENCE handled structurally by the validator; anything else
    # passes through as text.
    if isinstance(value, str):
        return value
    raise GatewayError(ErrorCode.INVALID_ELEMENT_TYPE, f"Unsupported datatype {datatype.value}")
