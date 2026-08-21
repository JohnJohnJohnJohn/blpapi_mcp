"""Canonical schema conversion and request validation (SPEC §2.7, §2.8).

Pure module: operates on :class:`ElementDescriptor` trees, never on native
``blpapi`` objects. Provides:

- deterministic JSON Schema generation with ``$defs``/``$ref``,
  cycle detection and a maximum traversal depth;
- deterministic schema hashing;
- request-parameter validation against the Bloomberg schema, enforcing
  cardinality, enumerations, choices, unknown-element rejection, nesting and
  array-size limits.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bloomberg_mcp.blp.value_codec import decode_input_value
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype, ElementDescriptor, OperationDescriptor
from bloomberg_mcp.policy.models import PolicyLimits

MAX_SCHEMA_DEPTH = 64

_JSON_PRIMITIVES: dict[BloombergDatatype, dict[str, Any]] = {
    BloombergDatatype.STRING: {"type": "string"},
    BloombergDatatype.CHAR: {"type": "string", "maxLength": 1},
    BloombergDatatype.BOOL: {"type": "boolean"},
    BloombergDatatype.INT32: {"type": "integer"},
    BloombergDatatype.INT64: {"type": "integer"},
    BloombergDatatype.BYTE: {"type": "integer"},
    BloombergDatatype.FLOAT32: {"type": "number"},
    BloombergDatatype.FLOAT64: {"type": "number"},
    BloombergDatatype.DATE: {"type": "string", "format": "date"},
    BloombergDatatype.TIME: {"type": "string", "format": "time"},
    BloombergDatatype.DATETIME: {"type": "string", "format": "date-time"},
    BloombergDatatype.BYTEARRAY: {"type": "string", "contentEncoding": "base64"},
}


def descriptor_to_json_schema(
    root: ElementDescriptor, *, include_defs: bool = True, max_depth: int = MAX_SCHEMA_DEPTH
) -> dict[str, Any]:
    """Convert a descriptor tree to deterministic JSON Schema.

    Repeated complex types are emitted once under ``$defs`` keyed by their
    Bloomberg type name; a schema cycle becomes a ``$ref`` so generation
    terminates.
    """
    defs: dict[str, Any] = {}
    in_progress: set[str] = set()

    def emit(descriptor: ElementDescriptor, depth: int) -> dict[str, Any]:
        if depth > max_depth:
            return {"type": "string", "description": "maximum schema depth exceeded"}

        if descriptor.datatype == BloombergDatatype.SEQUENCE or (
            descriptor.datatype == BloombergDatatype.CHOICE
        ):
            type_name = descriptor.type_name
            if type_name and type_name in defs:
                return {"$ref": f"#/$defs/{type_name}"}
            if type_name and type_name in in_progress:
                # Cycle: register a placeholder so the $ref resolves.
                defs.setdefault(type_name, {"type": "object"})
                return {"$ref": f"#/$defs/{type_name}"}
            if type_name:
                in_progress.add(type_name)
            schema = _composite_schema(descriptor, depth, emit)
            if type_name:
                in_progress.discard(type_name)
                defs[type_name] = schema
                return {"$ref": f"#/$defs/{type_name}"}
            return schema

        if descriptor.datatype == BloombergDatatype.ENUMERATION:
            enum_schema: dict[str, Any] = {"type": "string"}
            if descriptor.enum_values:
                enum_schema["enum"] = sorted(descriptor.enum_values)
            return enum_schema

        primitive = _JSON_PRIMITIVES.get(descriptor.datatype)
        return dict(primitive) if primitive is not None else {"type": "string"}

    def wrap_array(descriptor: ElementDescriptor, item_schema: dict[str, Any]) -> dict[str, Any]:
        if descriptor.max_values == 1:
            return item_schema
        schema: dict[str, Any] = {"type": "array", "items": item_schema}
        if descriptor.min_values > 0:
            schema["minItems"] = descriptor.min_values
        if descriptor.max_values is not None:
            schema["maxItems"] = descriptor.max_values
        return schema

    def _composite_schema(
        descriptor: ElementDescriptor, depth: int, emit_fn: Any
    ) -> dict[str, Any]:
        if descriptor.datatype == BloombergDatatype.CHOICE:
            branches = [
                {"required": [choice.name], "properties": {choice.name: emit_fn(choice, depth + 1)}}
                for choice in descriptor.choices
            ]
            return {
                "type": "object",
                "maxProperties": 1,
                "anyOf": branches or [{"type": "object"}],
            }
        properties: dict[str, Any] = {}
        required: list[str] = []
        for child in descriptor.children:
            item = emit_fn(child, depth + 1)
            properties[child.name] = wrap_array(child, item)
            if child.min_values >= 1:
                required.append(child.name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        # Match the runtime unknown-element policy (finding J6): schema-driven
        # clients see that unknown keys are rejected.
        schema["additionalProperties"] = False
        if required:
            schema["required"] = required
        return schema
    root_schema = emit(root, 0)
    if include_defs and defs:
        root_schema = {**root_schema, "$defs": {name: defs[name] for name in sorted(defs)}}
    return root_schema


def hash_operation_schema(descriptor: OperationDescriptor) -> str:
    """Deterministic content hash over the operation's canonical descriptors."""
    payload = {
        "service": descriptor.service,
        "operation": descriptor.operation,
        "request": _descriptor_dump(descriptor.request) if descriptor.request else None,
        "responses": [_descriptor_dump(r) for r in descriptor.responses],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _descriptor_dump(descriptor: ElementDescriptor) -> Any:
    return {
        "name": descriptor.name,
        "alternate_names": list(descriptor.alternate_names),
        "datatype": descriptor.datatype.value,
        "status": descriptor.status,
        "min_values": descriptor.min_values,
        "max_values": descriptor.max_values,
        "enum_values": list(descriptor.enum_values),
        "type_name": descriptor.type_name,
        "children": [_descriptor_dump(c) for c in descriptor.children],
        "choices": [_descriptor_dump(c) for c in descriptor.choices],
    }


# --------------------------------------------------------------------- validation


def validate_parameters(
    descriptor: ElementDescriptor,
    parameters: dict[str, Any],
    limits: PolicyLimits,
    *,
    reject_unknown_elements: bool,
    strict_types: bool = False,
    reject_unknown_override: bool | None = None,
) -> dict[str, Any]:
    """Validate raw input against the operation request descriptor.

    Returns the canonical parameter mapping (decoded native Python values).
    Raises :class:`GatewayError` with the precise application error code.

    ``strict_types`` disables the lenient singleton coercion (a bare scalar
    where the schema declares an array): the value must then be a list.

    ``reject_unknown_elements`` is the server policy floor; the per-request
    ``reject_unknown_override`` can only tighten it, never weaken it.
    """
    if not isinstance(parameters, dict):
        raise GatewayError(ErrorCode.INVALID_ARGUMENT, "parameters must be an object")
    reject = reject_unknown_elements or bool(reject_unknown_override)
    counters = _Counters(limits)
    return _validate_object(descriptor, parameters, reject, counters, depth=0, strict_types=strict_types, path="")


class _Counters:
    def __init__(self, limits: PolicyLimits) -> None:
        self.limits = limits
        self.array_elements = 0

    def count_array_items(self, count: int) -> None:
        self.array_elements += count
        if self.array_elements > self.limits.maximum_request_array_elements:
            raise GatewayError(
                ErrorCode.REQUEST_TOO_LARGE,
                f"Request exceeds the maximum of {self.limits.maximum_request_array_elements} array elements.",
            )


def _validate_object(
    descriptor: ElementDescriptor,
    values: dict[str, Any],
    reject_unknown: bool,
    counters: _Counters,
    depth: int,
    *,
    strict_types: bool = False,
    path: str = "",
) -> dict[str, Any]:
    if depth > counters.limits.maximum_nesting_depth:
        raise GatewayError(ErrorCode.REQUEST_TOO_LARGE, "Request nesting depth exceeds policy limit.")

    known: dict[str, ElementDescriptor] = {child.name: child for child in descriptor.children}
    for child_entry in descriptor.children:
        for alias in child_entry.alternate_names:
            known.setdefault(alias, child_entry)

    result: dict[str, Any] = {}
    seen_targets: set[str] = set()
    for key, raw in values.items():
        child = known.get(key)
        if child is None:
            if reject_unknown:
                raise GatewayError(
                    ErrorCode.UNKNOWN_ELEMENT,
                    f"Unknown element {key!r} for this request schema.",
                    details={"path": f"{path}.{key}"} if path else None,
                )
            continue
        # Canonical name plus alternate name for the same element is an
        # ambiguity error (finding J4) rather than last-one-wins.
        if child.name in seen_targets:
            raise GatewayError(
                ErrorCode.INVALID_ARGUMENT,
                f"Element {child.name!r} provided more than once (canonical name and alias).",
                details={"path": f"{path}.{key}"} if path else None,
            )
        seen_targets.add(child.name)
        result[child.name] = _validate_element(
            child, raw, reject_unknown, counters, depth, strict_types=strict_types, path=f"{path}.{key}"
        )

    for child in descriptor.children:
        if child.min_values >= 1 and child.name not in result:
            raise GatewayError(
                ErrorCode.MISSING_REQUIRED_ELEMENT,
                f"Required element {child.name!r} is missing.",
                details={"path": f"{path}.{child.name}"} if path else None,
            )
    return result


def _validate_element(
    descriptor: ElementDescriptor,
    raw: Any,
    reject_unknown: bool,
    counters: _Counters,
    depth: int,
    *,
    strict_types: bool = False,
    path: str = "",
) -> Any:
    if descriptor.datatype == BloombergDatatype.SEQUENCE:
        if descriptor.max_values != 1:
            if strict_types and not isinstance(raw, list):
                raise GatewayError(
                    ErrorCode.INVALID_ELEMENT_TYPE,
                    f"Element {descriptor.name!r} expects an array.",
                    details={"path": path} if path else None,
                )
            items = raw if isinstance(raw, list) else [raw]
            counters.count_array_items(len(items))
            if descriptor.max_values is not None and len(items) > descriptor.max_values:
                raise GatewayError(
                    ErrorCode.REQUEST_TOO_LARGE,
                    f"Element {descriptor.name!r} allows at most {descriptor.max_values} values.",
                    details={"path": path} if path else None,
                )
            if descriptor.min_values >= 1 and len(items) < descriptor.min_values:
                raise GatewayError(
                    ErrorCode.MISSING_REQUIRED_ELEMENT,
                    f"Element {descriptor.name!r} requires at least {descriptor.min_values} value(s).",
                    details={"path": path} if path else None,
                )
            validated_items = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise GatewayError(
                        ErrorCode.INVALID_ELEMENT_TYPE,
                        f"Sequence element {descriptor.name!r} expects object entries.",
                        details={"path": f"{path}[{index}]"} if path else None,
                    )
                validated_items.append(
                    _validate_object(
                        descriptor, item, reject_unknown, counters, depth + 1, strict_types=strict_types,
                        path=f"{path}[{index}]",
                    )
                )
            return validated_items
        if strict_types and isinstance(raw, list):
            raise GatewayError(
                ErrorCode.INVALID_ELEMENT_TYPE,
                f"Element {descriptor.name!r} is scalar; received an array.",
                details={"path": path} if path else None,
            )
        if isinstance(raw, list):
            if len(raw) != 1:
                raise GatewayError(
                    ErrorCode.INVALID_ELEMENT_TYPE,
                    f"Element {descriptor.name!r} is scalar; received {len(raw)} values.",
                    details={"path": path} if path else None,
                )
            raw = raw[0]
        if not isinstance(raw, dict):
            raise GatewayError(
                ErrorCode.INVALID_ELEMENT_TYPE,
                f"Element {descriptor.name!r} expects an object.",
                details={"path": path} if path else None,
            )
        return _validate_object(
            descriptor, raw, reject_unknown, counters, depth + 1, strict_types=strict_types, path=path
        )

    if descriptor.datatype == BloombergDatatype.CHOICE:
        if not isinstance(raw, dict) or len(raw) != 1:
            raise GatewayError(
                ErrorCode.INVALID_CHOICE,
                f"Choice element {descriptor.name!r} requires exactly one branch.",
                details={"path": path} if path else None,
            )
        branch_name = next(iter(raw))
        branch = next((c for c in descriptor.choices if c.name == branch_name), None)
        if branch is None:
            raise GatewayError(
                ErrorCode.INVALID_CHOICE,
                f"Unknown choice branch {branch_name!r} on {descriptor.name!r}.",
                details={"path": path} if path else None,
            )
        branch_value = raw[branch_name]
        if branch.datatype == BloombergDatatype.SEQUENCE and isinstance(branch_value, dict):
            return _validate_object(
                branch, branch_value, reject_unknown, counters, depth + 1, strict_types=strict_types,
                path=f"{path}.{branch_name}",
            )
        return {branch_name: _validate_scalar(branch, branch_value, path=f"{path}.{branch_name}")}

    # Scalar or repeated scalar / repeated sequence.
    if descriptor.max_values != 1:
        if strict_types and not isinstance(raw, list):
            raise GatewayError(
                ErrorCode.INVALID_ELEMENT_TYPE,
                f"Element {descriptor.name!r} expects an array.",
                details={"path": path} if path else None,
            )
        items = raw if isinstance(raw, list) else [raw]
        counters.count_array_items(len(items))
        if descriptor.max_values is not None and len(items) > descriptor.max_values:
            raise GatewayError(
                ErrorCode.REQUEST_TOO_LARGE,
                f"Element {descriptor.name!r} allows at most {descriptor.max_values} values.",
                details={"path": path} if path else None,
            )
        if descriptor.min_values >= 1 and len(items) < descriptor.min_values:
            raise GatewayError(
                ErrorCode.MISSING_REQUIRED_ELEMENT,
                f"Element {descriptor.name!r} requires at least {descriptor.min_values} value(s).",
                details={"path": path} if path else None,
            )
        return [
            _validate_scalar(descriptor, item, path=f"{path}[{index}]")
            for index, item in enumerate(items)
        ]

    if strict_types and isinstance(raw, list):
        raise GatewayError(
            ErrorCode.INVALID_ELEMENT_TYPE,
            f"Element {descriptor.name!r} is scalar; received an array.",
            details={"path": path} if path else None,
        )
    if isinstance(raw, list):
        if len(raw) != 1:
            raise GatewayError(
                ErrorCode.INVALID_ELEMENT_TYPE,
                f"Element {descriptor.name!r} is scalar; received {len(raw)} values.",
                details={"path": path} if path else None,
            )
        raw = raw[0]
    return _validate_scalar(descriptor, raw, path=path)


def _validate_scalar(descriptor: ElementDescriptor, raw: Any, *, path: str = "") -> Any:
    if raw is None:
        raise GatewayError(
            ErrorCode.INVALID_ELEMENT_TYPE,
            f"Element {descriptor.name!r} must not be null.",
            details={"path": path} if path else None,
        )
    if descriptor.datatype == BloombergDatatype.ENUMERATION and descriptor.enum_values:
        if not isinstance(raw, str) or raw not in descriptor.enum_values:
            raise GatewayError(
                ErrorCode.INVALID_ENUM_VALUE,
                f"Value {raw!r} is not permitted for enumeration {descriptor.name!r}.",
                details={"path": path} if path else None,
            )
        return raw
    if descriptor.datatype == BloombergDatatype.BYTE and isinstance(raw, int):  # noqa: SIM102 - range guard reads clearer nested
        if not 0 <= raw <= 255:
            raise GatewayError(
                ErrorCode.INVALID_ELEMENT_TYPE,
                f"BYTE value {raw!r} out of 0-255 range.",
                details={"path": path} if path else None,
            )
    return decode_input_value(raw, descriptor.datatype)
