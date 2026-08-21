"""Shared canonical tree-walking utilities (review finding G).

Bloomberg's decoder emits a *single sequence object* (dict) for one-entry
sequences and lists for multi-entry sequences. Every walker in this module
accepts both shapes so item-error extraction, normalizers and entitlement
detection never disagree about the canonical shape.

All consumers must import from here instead of re-implementing list-only
walks (the pre-fix `extract_item_errors` bug).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from bloomberg_mcp.models import CanonicalMessage


def iter_sequence(node: Mapping[str, Any], key: str) -> Iterator[Any]:
    """Yield the entries of a nested sequence, dict (single) or list (multi)."""
    value = node.get(key)
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                yield entry
    elif isinstance(value, dict):
        yield value


def walk_sequence(messages: list[CanonicalMessage], *path: str) -> list[Any]:
    """Collect dict nodes along a path whose intermediate steps may be dicts OR lists.

    The canonical decoder emits a *single sequence object* (dict) for elements
    such as HistoricalDataResponse ``securityData`` / IntradayBarResponse
    ``barData`` (one object per message), while arrays of sequences decode as
    lists. This helper handles both at every step so normalizers are robust to
    either shape.
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


def merged_sequence(messages: list[CanonicalMessage], *path: str) -> list[Any]:
    """Concatenate a nested list-typed sequence across partial + final responses.

    Prefer `walk_sequence` when the terminal sequence may decode as a dict;
    this exists for callers that require flat list semantics.
    """
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
