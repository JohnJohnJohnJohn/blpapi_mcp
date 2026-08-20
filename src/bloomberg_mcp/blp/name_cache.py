"""Adapter-owned interned ``blpapi.Name`` cache (SPEC §4.6).

Native ``Name`` objects never leave this module; callers exchange plain
strings. Growth is bounded by ``max_entries``.
"""

from __future__ import annotations

import threading

import blpapi


class NameCache:
    def __init__(self, max_entries: int = 4096) -> None:
        self._max = max_entries
        self._lock = threading.Lock()
        self._names: dict[str, blpapi.Name] = {}

    def get(self, name: str) -> blpapi.Name:
        with self._lock:
            cached = self._names.get(name)
            if cached is not None:
                return cached
            found = blpapi.Name.findName(name)
            if found is None:
                found = blpapi.Name(name)
            if len(self._names) >= self._max:
                # Bounded growth: drop ~10% (dict ordering approximates age).
                for stale in list(self._names)[: self._max // 10]:
                    self._names.pop(stale, None)
            self._names[name] = found
            return found

    def clear(self) -> None:
        with self._lock:
            self._names.clear()
