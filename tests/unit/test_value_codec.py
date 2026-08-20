"""Value codec fidelity rules (SPEC §2.12, §5.7)."""

from __future__ import annotations

import datetime as dt

import pytest

from bloomberg_mcp.blp.value_codec import (
    JSON_SAFE_INT_MAX,
    decode_input_value,
    encode_value,
)
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype as D


def test_date_stays_calendar_date() -> None:
    encoded = encode_value(dt.date(2026, 8, 20), D.DATE, typed=False)
    assert encoded == "2026-08-20"  # no midnight-UTC instant
    typed = encode_value(dt.date(2026, 8, 20), D.DATE, typed=True)
    assert typed == {"$blp_type": "DATE", "value": "2026-08-20"}


def test_time_stays_time() -> None:
    encoded = encode_value(dt.time(12, 20, 0, 123000), D.TIME, typed=False)
    assert encoded == "12:20:00.123"


def test_aware_datetime_keeps_offset() -> None:
    aware = dt.datetime(2026, 8, 20, 12, 20, 0, 123000, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    encoded = encode_value(aware, D.DATETIME, typed=True)
    assert encoded["value"].endswith("+08:00")
    assert encoded["timezone"] == "+08:00"


def test_naive_datetime_keeps_null_timezone() -> None:
    naive = dt.datetime(2026, 8, 20, 12, 20, 0, 123000)
    typed = encode_value(naive, D.DATETIME, typed=True)
    assert typed["timezone"] is None
    assert "+" not in typed["value"] and "Z" not in typed["value"]


def test_float_shortest_roundtrip() -> None:
    value = 0.1 + 0.2
    encoded = encode_value(value, D.FLOAT64, typed=False)
    assert encoded == value
    assert float(repr(encoded)) == value


def test_nan_and_infinity_tagged() -> None:
    assert encode_value(float("nan"), D.FLOAT64, typed=False) == {"$blp_type": "FLOAT64", "value": "NaN"}
    assert encode_value(float("inf"), D.FLOAT64, typed=False)["value"] == "Infinity"
    assert encode_value(float("-inf"), D.FLOAT64, typed=False)["value"] == "-Infinity"


def test_big_integer_tagged_string() -> None:
    big = JSON_SAFE_INT_MAX + 1
    encoded = encode_value(big, D.INT64, typed=False)
    assert encoded == {"$blp_type": "INT64", "value": str(big)}
    assert encode_value(42, D.INT64, typed=False) == 42


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2026-08-20", dt.date(2026, 8, 20)), ("20260820", dt.date(2026, 8, 20))],
)
def test_decode_date(text: str, expected: dt.date) -> None:
    assert decode_input_value(text, D.DATE) == expected


def test_decode_date_rejects_instant() -> None:
    with pytest.raises(GatewayError) as excinfo:
        decode_input_value("2026-08-20T00:00:00Z", D.DATE)
    assert excinfo.value.code is ErrorCode.INVALID_ELEMENT_TYPE


def test_decode_naive_datetime_stays_naive() -> None:
    parsed = decode_input_value("2026-08-20T12:20:00.123", D.DATETIME)
    assert parsed.tzinfo is None


def test_decode_offset_datetime_keeps_offset() -> None:
    parsed = decode_input_value("2026-08-20T12:20:00.123+08:00", D.DATETIME)
    assert parsed.utcoffset() == dt.timedelta(hours=8)


def test_integer_bounds() -> None:
    assert decode_input_value(5, D.INT32) == 5
    with pytest.raises(GatewayError):
        decode_input_value(2**31, D.INT32)
    with pytest.raises(GatewayError):
        decode_input_value(2**63, D.INT64)
    with pytest.raises(GatewayError):
        decode_input_value(True, D.INT32)  # bool is not an integer here


def test_float_rejects_bool() -> None:
    with pytest.raises(GatewayError):
        decode_input_value(True, D.FLOAT64)
    assert decode_input_value(3, D.FLOAT64) == 3.0


def test_string_type_enforced() -> None:
    with pytest.raises(GatewayError):
        decode_input_value(5, D.STRING)


def test_bytearray_base64() -> None:
    decoded = decode_input_value("aGVsbG8=", D.BYTEARRAY)
    assert decoded == b"hello"
    with pytest.raises(GatewayError):
        decode_input_value("!!not-base64!!", D.BYTEARRAY)


def test_char_single_character() -> None:
    assert decode_input_value("A", D.CHAR) == "A"
    with pytest.raises(GatewayError):
        decode_input_value("AB", D.CHAR)
