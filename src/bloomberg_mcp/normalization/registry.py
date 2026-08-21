"""Versioned normalizer registry (SPEC §3.6).

Only registered operations may produce ``normalized`` responses; every
normalized payload carries ``normalized_schema_version``, ``source_service``,
``source_operation`` and ``schema_hash``.
"""

from __future__ import annotations

from typing import Any, Protocol

from bloomberg_mcp.models import CanonicalMessage, CanonicalRequest


class Normalizer(Protocol):
    schema_version: str

    def normalize(self, messages: list[CanonicalMessage], request: CanonicalRequest) -> dict[str, Any]: ...


class NormalizerRegistry:
    def __init__(self) -> None:
        self._normalizers: dict[tuple[str, str], Normalizer] = {}

    def register(self, service: str, operation: str, normalizer: Normalizer) -> None:
        self._normalizers[(service, operation)] = normalizer

    def get(self, service: str, operation: str) -> Normalizer | None:
        return self._normalizers.get((service, operation))

    def operations(self) -> list[tuple[str, str]]:
        return sorted(self._normalizers)


def stamp(request: CanonicalRequest, normalizer: Normalizer, data: dict[str, Any]) -> dict[str, Any]:
    """Attach the mandatory normalized-response provenance fields (SPEC §3.6)."""
    return {
        "normalized_schema_version": normalizer.schema_version,
        "source_service": request.service,
        "source_operation": request.operation,
        "schema_hash": request.schema_hash,
        **data,
    }


def merged_sequence(messages: list[CanonicalMessage], *path: str) -> list[Any]:
    """Concatenate a nested sequence across partial + final responses."""
    collected: list[Any] = []
    for message in messages:
        node: Any = dict(message.payload)
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, list):
            collected.extend(node)
    return collected


def walk_sequence(messages: list[CanonicalMessage], *path: str) -> list[Any]:
    """Collect dict nodes along a path whose intermediate steps may be dicts OR lists.

    The canonical decoder emits a *single sequence object* (dict) for elements
    such as HistoricalDataResponse ``securityData`` / IntradayBarResponse
    ``barData`` (one object per message), while arrays of sequences decode as
    lists. ``merged_sequence`` only handles the list case; this helper handles
    both so normalizers are robust to either shape.
    """
    nodes: list[Any] = [dict(message.payload) for message in messages]
    for key in path:
        expanded: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            child = node.get(key)
            if isinstance(child, list):
                expanded.extend(child)
            elif isinstance(child, dict):
                expanded.append(child)
        nodes = expanded
    return nodes


def build_default_registry() -> NormalizerRegistry:
    from bloomberg_mcp.normalization.fields import FieldSearchNormalizer
    from bloomberg_mcp.normalization.historical import HistoricalNormalizer
    from bloomberg_mcp.normalization.instruments import (
        CurveSearchNormalizer,
        GovernmentSecuritySearchNormalizer,
        InstrumentSearchNormalizer,
    )
    from bloomberg_mcp.normalization.intraday import IntradayBarNormalizer, IntradayTickNormalizer
    from bloomberg_mcp.normalization.reference import ReferenceNormalizer

    registry = NormalizerRegistry()
    registry.register("//blp/refdata", "ReferenceDataRequest", ReferenceNormalizer())
    registry.register("//blp/refdata", "HistoricalDataRequest", HistoricalNormalizer())
    registry.register("//blp/refdata", "IntradayBarRequest", IntradayBarNormalizer())
    registry.register("//blp/refdata", "IntradayTickRequest", IntradayTickNormalizer())
    registry.register("//blp/instruments", "instrumentListRequest", InstrumentSearchNormalizer())
    registry.register("//blp/instruments", "curveListRequest", CurveSearchNormalizer())
    registry.register("//blp/instruments", "govtListRequest", GovernmentSecuritySearchNormalizer())
    registry.register("//blp/apiflds", "FieldSearchRequest", FieldSearchNormalizer())
    return registry
