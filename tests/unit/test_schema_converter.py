"""Schema conversion and request validation (SPEC §2.7, §2.8, §5.7).

No converter or validator test here is skipped or xfail-marked (SPEC §5.1).
"""

from __future__ import annotations

import pytest

from bloomberg_mcp.blp.schema_converter import (
    descriptor_to_json_schema,
    hash_operation_schema,
    validate_parameters,
)
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype as D
from bloomberg_mcp.models import ElementDescriptor, OperationDescriptor
from bloomberg_mcp.policy.models import PolicyLimits

LIMITS = PolicyLimits(maximum_nesting_depth=8, maximum_request_array_elements=50)


def _scalar(name: str, datatype: D = D.STRING, **kw) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=datatype, **kw)


def _seq(name: str, children: tuple, **kw) -> ElementDescriptor:
    kw.setdefault("max_values", 1)
    return ElementDescriptor(name=name, datatype=D.SEQUENCE, children=children, **kw)


def _request(children: tuple) -> ElementDescriptor:
    return _seq("Request", children)


def test_required_and_optional_elements() -> None:
    descriptor = _request(
        (_scalar("securities", min_values=1, max_values=None), _scalar("returnEids", D.BOOL))
    )
    schema = descriptor_to_json_schema(descriptor)
    assert schema["required"] == ["securities"]
    assert "returnEids" in schema["properties"]

    validated = validate_parameters(descriptor, {"securities": ["A"]}, LIMITS, reject_unknown_elements=True)
    assert validated == {"securities": ["A"]}

    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(descriptor, {}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.MISSING_REQUIRED_ELEMENT


def test_unknown_element_rejection_toggle() -> None:
    descriptor = _request((_scalar("securities", min_values=1, max_values=None),))
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(
            descriptor, {"securities": ["A"], "bogus": 1}, LIMITS, reject_unknown_elements=True
        )
    assert excinfo.value.code is ErrorCode.UNKNOWN_ELEMENT
    accepted = validate_parameters(
        descriptor, {"securities": ["A"], "bogus": 1}, LIMITS, reject_unknown_elements=False
    )
    assert accepted == {"securities": ["A"]}


def test_unbounded_and_bounded_arrays() -> None:
    descriptor = _request(
        (
            _scalar("fields", min_values=1, max_values=None),
            _scalar("pair", max_values=2),
        )
    )
    schema = descriptor_to_json_schema(descriptor)
    assert schema["properties"]["fields"]["type"] == "array"
    assert "maxItems" not in schema["properties"]["fields"]
    assert schema["properties"]["pair"]["maxItems"] == 2

    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(
            descriptor, {"fields": ["A"], "pair": [1, 2, 3]}, LIMITS, reject_unknown_elements=True
        )
    assert excinfo.value.code is ErrorCode.REQUEST_TOO_LARGE


def test_scalar_accepts_singleton_list() -> None:
    descriptor = _request((_scalar("security"),))
    validated = validate_parameters(descriptor, {"security": ["IBM"]}, LIMITS, reject_unknown_elements=True)
    assert validated == {"security": "IBM"}
    with pytest.raises(GatewayError):
        validate_parameters(descriptor, {"security": ["A", "B"]}, LIMITS, reject_unknown_elements=True)


def test_strict_types_rejects_scalar_for_array() -> None:
    descriptor = _request((_scalar("securities", min_values=1, max_values=None),))
    # Lenient (default): a bare scalar is coerced to a singleton list.
    validated = validate_parameters(
        descriptor, {"securities": "AAPL US Equity"}, LIMITS, reject_unknown_elements=True
    )
    assert validated == {"securities": ["AAPL US Equity"]}
    # Strict: the advertised schema says array; a scalar is rejected.
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(
            descriptor,
            {"securities": "AAPL US Equity"},
            LIMITS,
            reject_unknown_elements=True,
            strict_types=True,
        )
    assert excinfo.value.code is ErrorCode.INVALID_ELEMENT_TYPE


def test_nested_sequences_and_repeated_sequences() -> None:
    overrides = _seq("overrides", (_scalar("fieldId", min_values=1), _scalar("value", min_values=1)), max_values=None)
    descriptor = _request((overrides,))
    validated = validate_parameters(
        descriptor,
        {"overrides": [{"fieldId": "EQY_FUND_CRNCY", "value": "HKD"}, {"fieldId": "X", "value": "1"}]},
        LIMITS,
        reject_unknown_elements=True,
    )
    assert validated["overrides"][1] == {"fieldId": "X", "value": "1"}


def test_enumeration_validation() -> None:
    descriptor = _request(
        (_scalar("periodicitySelection", D.ENUMERATION, enum_values=("DAILY", "WEEKLY")),)
    )
    validated = validate_parameters(
        descriptor, {"periodicitySelection": "DAILY"}, LIMITS, reject_unknown_elements=True
    )
    assert validated["periodicitySelection"] == "DAILY"
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(descriptor, {"periodicitySelection": "HOURLY"}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.INVALID_ENUM_VALUE


def test_choice_validation() -> None:
    choice = ElementDescriptor(
        name="identifier",
        datatype=D.CHOICE,
        choices=(_scalar("cusip"), _scalar("isin")),
    )
    descriptor = _request((choice,))
    validated = validate_parameters(
        descriptor, {"identifier": {"isin": "HK0000001"}}, LIMITS, reject_unknown_elements=True
    )
    assert validated["identifier"] == {"isin": "HK0000001"}
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(descriptor, {"identifier": {"sedol": "X"}}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.INVALID_CHOICE
    with pytest.raises(GatewayError):
        validate_parameters(
            descriptor, {"identifier": {"cusip": "A", "isin": "B"}}, LIMITS, reject_unknown_elements=True
        )


def test_alternate_names() -> None:
    descriptor = _request((_scalar("security", alternate_names=("sec",)),))
    validated = validate_parameters(descriptor, {"sec": "IBM"}, LIMITS, reject_unknown_elements=True)
    assert validated == {"security": "IBM"}


def test_nesting_depth_limit() -> None:
    inner = _seq("level9", (_scalar("value"),), type_name="L9")
    node = inner
    for i in range(8, 0, -1):
        node = _seq(f"level{i}", (node,), type_name=f"L{i}")
    descriptor = _request((node,))
    deep: dict = {"value": "x"}
    for i in range(9, 0, -1):
        deep = {f"level{i}": deep}
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(descriptor, deep, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.REQUEST_TOO_LARGE


def test_array_element_budget() -> None:
    descriptor = _request((_scalar("items", max_values=None),))
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(descriptor, {"items": ["x"] * 51}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.REQUEST_TOO_LARGE


def test_json_schema_generation_and_defs() -> None:
    row = _seq("row", (_date_field(), _scalar("value", D.FLOAT64)), type_name="Row", max_values=None)
    descriptor = _seq("Response", (_seq("data", (row,), type_name="Data"),), type_name="Response")
    schema = descriptor_to_json_schema(descriptor)
    assert "$defs" in schema
    assert "Row" in schema["$defs"]
    assert schema["$defs"]["Data"]["properties"]["row"]["items"]["$ref"] == "#/$defs/Row"


def test_schema_cycle_terminates() -> None:
    # Self-referencing type: cycle must terminate via $ref.
    node = _seq("node", (), type_name="Node", max_values=None)
    parent = _seq("parent", (node,), type_name="Parent")
    node_with_child = _seq("node", (parent,), type_name="Node", max_values=None)
    root = _seq("Root", (node_with_child,), type_name="Root")
    schema = descriptor_to_json_schema(root)
    assert "$defs" in schema


def _date_field() -> ElementDescriptor:
    return ElementDescriptor(name="date", datatype=D.DATE)


def test_schema_hash_deterministic_and_sensitive() -> None:
    def operation(children: tuple) -> OperationDescriptor:
        request = _request(children)
        descriptor = OperationDescriptor(
            service="//blp/refdata",
            operation="TestRequest",
            description=None,
            request=request,
            responses=(),
            service_generation=1,
            schema_hash="",
        )
        return OperationDescriptor(**{**descriptor.__dict__, "schema_hash": hash_operation_schema(descriptor)})

    a = operation((_scalar("securities", min_values=1, max_values=None),))
    b = operation((_scalar("securities", min_values=1, max_values=None),))
    c = operation((_scalar("securities", min_values=1, max_values=None), _scalar("extra")))
    assert a.schema_hash == b.schema_hash
    assert a.schema_hash != c.schema_hash
    assert a.schema_hash.startswith("sha256:")
