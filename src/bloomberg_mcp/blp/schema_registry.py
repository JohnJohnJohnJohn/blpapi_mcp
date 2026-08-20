"""Native schema conversion into canonical descriptors (SPEC §2.7).

Converts ``blpapi`` Service/Operation/SchemaElementDefinition/
SchemaTypeDefinition trees into immutable :class:`ElementDescriptor` and
:class:`OperationDescriptor` values. Handles optional/mandatory elements,
bounded and unbounded arrays, nested sequences, choices, enumerations,
alternate names, deprecated definitions, multiple response definitions,
anonymous nested definitions, deterministic ordering, cycle detection and a
maximum traversal depth.

Native methods used (blpapi 3.26.7.1): ``Service.name``,
``Service.description``, ``Service.operations``, ``Operation.name``,
``Operation.description``, ``Operation.requestDefinition``,
``Operation.numResponseDefinitions``, ``Operation.getResponseDefinitionAt``,
``SchemaElementDefinition.name/alternateNames/description/status/minValues/
maxValues/typeDefinition``, ``SchemaTypeDefinition.datatype/isComplexType/
isEnumerationType/elementDefinitions/enumeration``, ``ConstantList.numConstants/
getConstantAt``, ``Constant.name``, ``Name.__str__``.
"""

from __future__ import annotations

import logging
from typing import Any

import blpapi

from bloomberg_mcp.blp.event_decoder import canonical_datatype
from bloomberg_mcp.blp.schema_converter import hash_operation_schema
from bloomberg_mcp.models import BloombergDatatype, ElementDescriptor, OperationDescriptor

logger = logging.getLogger(__name__)

MAX_CONVERSION_DEPTH = 32


def _status_label(status: Any) -> str | None:
    deprecated = getattr(blpapi.SchemaElementDefinition, "DEPRECATED", 1)
    if status == deprecated:
        return "deprecated"
    return None


class SchemaRegistry:
    """Stateless converter with cycle tracking; results are immutable."""

    def convert_service(self, service: blpapi.Service, generation: int) -> dict[str, OperationDescriptor]:
        operations: dict[str, OperationDescriptor] = {}
        for native_operation in service.operations():
            descriptor = self._convert_operation(service, native_operation, generation)
            operations[descriptor.operation] = descriptor
        return operations

    def _convert_operation(
        self, service: blpapi.Service, native_operation: blpapi.Operation, generation: int
    ) -> OperationDescriptor:
        request_definition = native_operation.requestDefinition()
        request = (
            self._convert_element_definition(request_definition, depth=0, visiting=set())
            if request_definition is not None
            else None
        )
        responses: list[ElementDescriptor] = []
        for index in range(native_operation.numResponseDefinitions()):
            response_definition = native_operation.getResponseDefinitionAt(index)
            responses.append(self._convert_element_definition(response_definition, depth=0, visiting=set()))
        descriptor = OperationDescriptor(
            service=str(service.name()),
            operation=str(native_operation.name()),
            description=native_operation.description() or None,
            request=request,
            responses=tuple(responses),
            service_generation=generation,
            schema_hash="",
        )
        return OperationDescriptor(
            service=descriptor.service,
            operation=descriptor.operation,
            description=descriptor.description,
            request=descriptor.request,
            responses=descriptor.responses,
            service_generation=descriptor.service_generation,
            schema_hash=hash_operation_schema(descriptor),
        )

    def _convert_element_definition(
        self, definition: blpapi.SchemaElementDefinition, depth: int, visiting: set[str]
    ) -> ElementDescriptor:
        type_definition = definition.typeDefinition()
        datatype = canonical_datatype(type_definition.datatype())
        type_name = str(type_definition.name()) if type_definition.isValid() else None

        if depth > MAX_CONVERSION_DEPTH:
            logger.warning("schema conversion depth limit reached at %s", definition.name())
            return ElementDescriptor(
                name=str(definition.name()),
                datatype=BloombergDatatype.UNSUPPORTED,
                description=definition.description() or None,
                status=_status_label(definition.status()),
                min_values=definition.minValues(),
                max_values=self._max_values(definition),
                type_name=type_name,
            )

        enum_values: tuple[str, ...] = ()
        children: tuple[ElementDescriptor, ...] = ()
        choices: tuple[ElementDescriptor, ...] = ()

        if datatype == BloombergDatatype.ENUMERATION and type_definition.isEnumerationType():
            constants = type_definition.enumeration()
            if constants is not None:
                enum_values = tuple(
                    str(constants.getConstantAt(i).name()) for i in range(constants.numConstants())
                )
        elif datatype == BloombergDatatype.SEQUENCE and type_definition.isComplexType():
            if type_name and type_name in visiting:
                # Schema cycle: emit the reference marker and stop recursion.
                return ElementDescriptor(
                    name=str(definition.name()),
                    datatype=datatype,
                    description=definition.description() or None,
                    status=_status_label(definition.status()),
                    min_values=definition.minValues(),
                    max_values=self._max_values(definition),
                    type_name=type_name,
                )
            if type_name:
                visiting = visiting | {type_name}
            children = tuple(
                self._convert_element_definition(
                    type_definition.getElementDefinition(i), depth + 1, visiting
                )
                for i in range(type_definition.numElementDefinitions())
            )
        elif datatype == BloombergDatatype.CHOICE and type_definition.isComplexType():
            if type_name:
                visiting = visiting | {type_name}
            choices = tuple(
                self._convert_element_definition(
                    type_definition.getElementDefinition(i), depth + 1, visiting
                )
                for i in range(type_definition.numElementDefinitions())
            )

        alternate = tuple(str(alias) for alias in (definition.alternateNames() or ()))
        return ElementDescriptor(
            name=str(definition.name()),
            alternate_names=alternate,
            datatype=datatype,
            description=definition.description() or None,
            status=_status_label(definition.status()),
            min_values=definition.minValues(),
            max_values=self._max_values(definition),
            children=children,
            enum_values=enum_values,
            choices=choices,
            type_name=type_name,
        )

    @staticmethod
    def _max_values(definition: blpapi.SchemaElementDefinition) -> int | None:
        maximum = definition.maxValues()
        if maximum == blpapi.SchemaElementDefinition.UNBOUNDED:
            return None
        return int(maximum)
