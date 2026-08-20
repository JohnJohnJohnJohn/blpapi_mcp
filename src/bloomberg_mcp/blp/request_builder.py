"""Native request construction from canonical parameters (SPEC §2.5, §2.8).

Populates a native ``blpapi.Request`` from a validated canonical parameter
mapping. Every element access uses interned ``blpapi.Name`` values.

Native methods used (blpapi 3.26.7.1): ``Service.createRequest``,
``Request.getElement``, ``Element.setValue``, ``Element.appendValue``,
``Element.appendElement``, ``Element.setChoice``, ``Element.setElement``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import blpapi

from bloomberg_mcp.blp.name_cache import NameCache
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import BloombergDatatype, ElementDescriptor


def populate_request(
    request: blpapi.Request,
    descriptor: ElementDescriptor,
    parameters: Mapping[str, Any],
    name_cache: NameCache,
) -> None:
    children = {child.name: child for child in descriptor.children}
    for key, value in parameters.items():
        child = children.get(key)
        if child is None:
            raise GatewayError(ErrorCode.UNKNOWN_ELEMENT, f"Unknown element {key!r} during native build.")
        _set_element(request.getElement(name_cache.get(child.name)), child, value, name_cache)


def _set_element(
    element: blpapi.Element, descriptor: ElementDescriptor, value: Any, name_cache: NameCache
) -> None:
    if value is None:
        return

    if descriptor.datatype == BloombergDatatype.SEQUENCE:
        entries = value if isinstance(value, list) else [value]
        if descriptor.max_values == 1 and len(entries) == 1 and isinstance(entries[0], dict):
            _set_members(element, descriptor, entries[0], name_cache)
            return
        for entry in entries:
            if not isinstance(entry, dict):
                raise GatewayError(
                    ErrorCode.INVALID_ELEMENT_TYPE,
                    f"Sequence element {descriptor.name!r} requires object entries.",
                )
            appended = element.appendElement()
            _set_members(appended, descriptor, entry, name_cache)
        return

    if descriptor.datatype == BloombergDatatype.CHOICE:
        if not isinstance(value, dict) or len(value) != 1:
            raise GatewayError(ErrorCode.INVALID_CHOICE, f"Choice {descriptor.name!r} requires one branch.")
        branch_name, branch_value = next(iter(value.items()))
        branch = next((c for c in descriptor.choices if c.name == branch_name), None)
        if branch is None:
            raise GatewayError(ErrorCode.INVALID_CHOICE, f"Unknown choice branch {branch_name!r}.")
        choice_element = element.setChoice(name_cache.get(branch_name))
        _set_element(choice_element, branch, branch_value, name_cache)
        return

    if descriptor.max_values != 1:
        items = value if isinstance(value, list) else [value]
        for item in items:
            element.appendValue(item)
        return

    element.setValue(value)


def _set_members(
    element: blpapi.Element, descriptor: ElementDescriptor, values: Mapping[str, Any], name_cache: NameCache
) -> None:
    children = {child.name: child for child in descriptor.children}
    for key, value in values.items():
        child = children.get(key)
        if child is None:
            raise GatewayError(ErrorCode.UNKNOWN_ELEMENT, f"Unknown element {key!r} during native build.")
        _set_element(element.getElement(name_cache.get(child.name)), child, value, name_cache)
