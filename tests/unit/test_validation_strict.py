"""CS5 regression tests: central validation gaps (finding J).

Failing against 0445a48: null required values pass; repeated min_values is not
enforced; canonical+alias ambiguity is silent; strict_types only covers one
direction; BYTE has no 0-255 range; generated JSON Schema lacks
additionalProperties:false; validation errors carry no element path.
"""

from __future__ import annotations

import pytest

from bloomberg_mcp.blp.schema_converter import descriptor_to_json_schema, validate_parameters
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype, ElementDescriptor
from bloomberg_mcp.policy.models import PolicyLimits

LIMITS = PolicyLimits()


def _desc(
    name: str,
    datatype: BloombergDatatype = BloombergDatatype.STRING,
    *,
    min_values: int = 0,
    max_values: int | None = None,
    children: tuple[ElementDescriptor, ...] = (),
    alternate_names: tuple[str, ...] = (),
) -> ElementDescriptor:
    return ElementDescriptor(
        name=name,
        datatype=datatype,
        min_values=min_values,
        max_values=max_values,
        children=children,
        alternate_names=alternate_names,
    )


def _request(*elements: ElementDescriptor) -> ElementDescriptor:
    return _desc("Request", BloombergDatatype.SEQUENCE, children=elements)


FIELD_ID = _desc("fieldId", min_values=1, max_values=1)
REQ = _request(FIELD_ID)


def test_required_null_rejected() -> None:
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(REQ, {"fieldId": None}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.INVALID_ELEMENT_TYPE
    # A valid value still validates.
    assert validate_parameters(REQ, {"fieldId": "x"}, LIMITS, reject_unknown_elements=True) == {"fieldId": "x"}


def test_optional_null_rejected_too() -> None:
    """A present-but-null value is never silently dropped at build time."""
    req = _request(_desc("note", max_values=1))
    with pytest.raises(GatewayError):
        validate_parameters(req, {"note": None}, LIMITS, reject_unknown_elements=True)


def test_repeated_min_values_enforced() -> None:
    repeated = _desc("values", min_values=2, max_values=None)
    req = _request(repeated)
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(req, {"values": ["only-one"]}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.MISSING_REQUIRED_ELEMENT
    with pytest.raises(GatewayError):
        validate_parameters(req, {"values": []}, LIMITS, reject_unknown_elements=True)
    result = validate_parameters(req, {"values": ["a", "b"]}, LIMITS, reject_unknown_elements=True)
    assert result["values"] == ["a", "b"]


def test_canonical_alias_ambiguity_rejected() -> None:
    req = _request(_desc("name", alternate_names=("shortName",), max_values=1))
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(req, {"name": "x", "shortName": "y"}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    # A single alias name still resolves.
    result = validate_parameters(req, {"shortName": "y"}, LIMITS, reject_unknown_elements=True)
    assert result["name"] == "y"


def test_strict_scalar_array_both_directions() -> None:
    scalar = _desc("scalar", min_values=1, max_values=1)
    repeated = _desc("repeated", max_values=None, min_values=0)
    req = _request(scalar, repeated)
    # Lenient mode: scalar element accepts [x]; repeated accepts bare x.
    result = validate_parameters(req, {"scalar": ["v"], "repeated": "r"}, LIMITS, reject_unknown_elements=True)
    assert result == {"scalar": "v", "repeated": ["r"]}
    # Strict mode rejects both directions.
    with pytest.raises(GatewayError):
        validate_parameters(
            req, {"scalar": ["v"], "repeated": []}, LIMITS, reject_unknown_elements=True, strict_types=True
        )
    with pytest.raises(GatewayError):
        validate_parameters(
            req, {"scalar": "v", "repeated": "r"}, LIMITS, reject_unknown_elements=True, strict_types=True
        )
    result = validate_parameters(
        req, {"scalar": "v", "repeated": ["r"]}, LIMITS, reject_unknown_elements=True, strict_types=True
    )
    assert result == {"scalar": "v", "repeated": ["r"]}


def test_byte_range_enforced() -> None:
    req = _request(_desc("level", BloombergDatatype.BYTE, max_values=1))
    assert validate_parameters(req, {"level": 255}, LIMITS, reject_unknown_elements=True)["level"] == 255
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(req, {"level": 256}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.INVALID_ELEMENT_TYPE
    with pytest.raises(GatewayError):
        validate_parameters(req, {"level": -1}, LIMITS, reject_unknown_elements=True)


def test_json_schema_additional_properties_false() -> None:
    schema = descriptor_to_json_schema(REQ)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    nested = _request(_desc("row", BloombergDatatype.SEQUENCE, max_values=None, children=(FIELD_ID,)))
    nested_schema = descriptor_to_json_schema(nested)
    item = nested_schema["properties"]["row"]["items"]
    assert item["type"] == "object"
    assert item["additionalProperties"] is False


def test_validation_errors_carry_path() -> None:
    overrides = _request(
        _desc("overrides", BloombergDatatype.SEQUENCE, max_values=None, children=(FIELD_ID,))
    )
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(overrides, {"overrides": [{"fieldId": None}]}, LIMITS, reject_unknown_elements=True)
    assert excinfo.value.code is ErrorCode.INVALID_ELEMENT_TYPE
    assert excinfo.value.details and "overrides" in excinfo.value.details.get("path", "")


def test_unknown_element_policy_floor() -> None:
    """The per-request option can only tighten, never weaken, the policy."""
    with pytest.raises(GatewayError) as excinfo:
        validate_parameters(
            REQ,
            {"bogus": 1},
            LIMITS,
            reject_unknown_elements=True,
            reject_unknown_override=False,
        )
    assert excinfo.value.code is ErrorCode.UNKNOWN_ELEMENT
    # Without the policy floor the option may tighten rejection.
    with pytest.raises(GatewayError) as excinfo2:
        validate_parameters(
            REQ, {"bogus": 1}, LIMITS, reject_unknown_elements=False, reject_unknown_override=True
        )
    assert excinfo2.value.code is ErrorCode.UNKNOWN_ELEMENT
    # Policy relaxed + no override: unknowns are silently dropped (lenient mode).
    optional_req = _request(_desc("opt", max_values=1))
    assert validate_parameters(optional_req, {"bogus": 1}, LIMITS, reject_unknown_elements=False) == {}
