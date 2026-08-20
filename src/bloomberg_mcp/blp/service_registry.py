"""Canonical service and operation registry (SPEC §2.2, §2.6).

Tracks which services are known/opened at the current session generation and
caches converted operation descriptors. Caches are invalidated whenever the
session generation changes, a service is reopened, or the gateway restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bloomberg_mcp.models import OperationDescriptor


@dataclass
class ServiceEntry:
    opened: bool = False
    generation: int = 0
    operations: dict[str, OperationDescriptor] = field(default_factory=dict)


class ServiceRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ServiceEntry] = {}

    def invalidate_all(self) -> None:
        self._entries.clear()

    def invalidate_service(self, service: str) -> None:
        self._entries.pop(service, None)

    def mark_opened(self, service: str, generation: int) -> None:
        entry = self._entries.setdefault(service, ServiceEntry())
        entry.opened = True
        entry.generation = generation

    def set_operations(self, service: str, generation: int, operations: dict[str, OperationDescriptor]) -> None:
        entry = self._entries.setdefault(service, ServiceEntry())
        entry.generation = generation
        entry.operations = operations

    def operations(self, service: str) -> dict[str, OperationDescriptor]:
        entry = self._entries.get(service)
        return dict(entry.operations) if entry else {}

    def is_open(self, service: str) -> bool:
        entry = self._entries.get(service)
        return bool(entry and entry.opened)

    def snapshot(self, configured: set[str]) -> dict[str, bool]:
        known = set(self._entries) | configured
        return {name: self.is_open(name) for name in sorted(known)}
