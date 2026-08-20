"""In-process metrics registry (SPEC §4.10).

A localhost-only text exposition endpoint renders these; nothing here
allocates unbounded state — labels are bounded by configured principals,
services and operations.
"""

from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._duration_sum: dict[str, float] = defaultdict(float)
        self._duration_count: dict[str, int] = defaultdict(int)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe_duration(self, name: str, seconds: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._duration_sum[key] += seconds
            self._duration_count[key] += 1

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{rendered}}}"

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for key, value in sorted(self._counters.items()):
                lines.append(f"{key} {value:g}")
            for key, value in sorted(self._gauges.items()):
                lines.append(f"{key} {value:g}")
            for key, total in sorted(self._duration_sum.items()):
                lines.append(f"{key}_sum {total:g}")
                lines.append(f"{key}_count {self._duration_count[key]}")
        return "\n".join(lines) + "\n"
