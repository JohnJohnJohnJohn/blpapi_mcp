"""Property-based tests (SPEC §5.7).

Generated schema-valid payloads must survive:
validation -> canonical construction -> decode/encode round-trip.
"""

from __future__ import annotations

import datetime as dt
import json
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bloomberg_mcp.blp.schema_converter import validate_parameters
from bloomberg_mcp.blp.value_codec import decode_input_value, encode_value
from bloomberg_mcp.models import BloombergDatatype as D
from bloomberg_mcp.models import ElementDescriptor
from bloomberg_mcp.policy.models import PolicyLimits

LIMITS = PolicyLimits(maximum_nesting_depth=8, maximum_request_array_elements=200)

safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), min_size=1, max_size=30
)


def _scalar(name: str, datatype: D, **kw) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=datatype, **kw)


def _seq(name: str, children: tuple, **kw) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=D.SEQUENCE, children=children, **kw)


@given(
    st.one_of(
        safe_text,
        st.integers(min_value=-(2**31), max_value=2**31 - 1),
        st.floats(allow_nan=False, allow_infinity=False),
        st.booleans(),
    )
)
@settings(max_examples=120)
def test_scalar_roundtrip_string_field(value) -> None:
    """Canonical encode -> JSON round-trip preserves the value for strings."""
    if not isinstance(value, str):
        return
    encoded = encode_value(value, D.STRING, typed=False)
    assert json.loads(json.dumps(encoded)) == value


@given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
@settings(max_examples=150)
def test_integer_roundtrip(number: int) -> None:
    encoded = encode_value(number, D.INT64, typed=False)
    if isinstance(encoded, dict):
        assert int(encoded["value"]) == number
    else:
        assert encoded == number
        assert decode_input_value(encoded, D.INT64) == number


@given(st.floats(allow_nan=True, allow_infinity=True))
@settings(max_examples=150)
def test_float_roundtrip_fidelity(number: float) -> None:
    encoded = encode_value(number, D.FLOAT64, typed=False)
    if isinstance(encoded, dict):
        label = encoded["value"]
        recovered = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}[label]
        assert (math.isnan(recovered) and math.isnan(number)) or recovered == number
    else:
        assert encoded == number


@given(st.dates(min_value=dt.date(1990, 1, 1), max_value=dt.date(2099, 12, 31)))
@settings(max_examples=120)
def test_date_encode_decode_roundtrip(day: dt.date) -> None:
    encoded = encode_value(day, D.DATE, typed=False)
    assert isinstance(encoded, str) and "T" not in encoded
    decoded = decode_input_value(encoded, D.DATE)
    assert decoded == day


@given(
    st.datetimes(
        min_value=dt.datetime(1990, 1, 1),
        max_value=dt.datetime(2099, 12, 31),
        timezones=st.none() | st.timezones(),
    )
)
@settings(max_examples=120)
def test_datetime_preserves_awareness(moment: dt.datetime) -> None:
    encoded = encode_value(moment, D.DATETIME, typed=True)
    if moment.tzinfo is None:
        assert encoded["timezone"] is None
        decoded = decode_input_value(encoded["value"], D.DATETIME)
        assert decoded.tzinfo is None
        assert decoded.replace(microsecond=moment.microsecond) == moment
    else:
        assert encoded["timezone"] is not None
        decoded = decode_input_value(encoded["value"], D.DATETIME)
        assert decoded.utcoffset() == moment.utcoffset()


@st.composite
def schema_valid_parameters(draw):
    """Generate payloads valid for a fixed nested schema."""
    securities = draw(st.lists(safe_text, min_size=1, max_size=5))
    fields = draw(st.lists(safe_text, min_size=1, max_size=5))
    overrides_count = draw(st.integers(min_value=0, max_value=3))
    overrides = [
        {"fieldId": draw(safe_text), "value": draw(safe_text)}
        for _ in range(overrides_count)
    ]
    parameters = {"securities": securities, "fields": fields}
    if overrides:
        parameters["overrides"] = overrides
    return parameters


REQUEST_DESCRIPTOR = _seq(
    "ReferenceDataRequest",
    (
        _scalar("securities", D.STRING, min_values=1, max_values=None),
        _scalar("fields", D.STRING, min_values=1, max_values=None),
        _seq(
            "overrides",
            (_scalar("fieldId", D.STRING, min_values=1), _scalar("value", D.STRING, min_values=1)),
            max_values=None,
        ),
    ),
    type_name="ReferenceDataRequest",
)


@given(schema_valid_parameters())
@settings(max_examples=80)
def test_generated_payload_validates_and_roundtrips(parameters) -> None:
    validated = validate_parameters(REQUEST_DESCRIPTOR, parameters, LIMITS, reject_unknown_elements=True)
    assert validated["securities"] == parameters["securities"]
    assert validated["fields"] == parameters["fields"]
    # Canonical values survive a JSON round-trip.
    from bloomberg_mcp.mcp.discovery_tools import _json_safe

    dumped = json.loads(json.dumps(_json_safe(validated)))
    assert dumped["securities"] == parameters["securities"]
    for original, canonical in zip(parameters.get("overrides", []), dumped.get("overrides", []), strict=False):
        assert canonical["fieldId"] == original["fieldId"]
        assert canonical["value"] == original["value"]


@given(st.integers(min_value=1, max_value=8))
@settings(max_examples=20)
def test_depth_limit_enforced_for_manual_chains(depth: int) -> None:
    """Deeply nested inputs beyond the limit raise REQUEST_TOO_LARGE."""
    from bloomberg_mcp.errors import ErrorCode, GatewayError

    node = _seq("leaf", (_scalar("value", D.STRING),), type_name="leaf")
    for level in range(depth):
        node = _seq(f"n{level}", (node,), type_name=f"n{level}")
    root = _seq("Root", (node,), type_name="Root")

    payload: dict = {"leaf": {"value": "x"}}
    for level in range(depth):
        payload = {f"n{level}": payload}

    loose = PolicyLimits(maximum_nesting_depth=depth + 4, maximum_request_array_elements=100)
    validate_parameters(root, payload, loose, reject_unknown_elements=True)

    tight = PolicyLimits(maximum_nesting_depth=max(0, depth - 1), maximum_request_array_elements=100)
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(root, payload, tight, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.REQUEST_TOO_LARGE
