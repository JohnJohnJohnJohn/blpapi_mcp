"""Bounded in-memory result storage (SPEC §4.7)."""

from __future__ import annotations

import threading
from datetime import datetime

from bloomberg_mcp.models import utc_now
from bloomberg_mcp.storage.models import ArtifactInfo

MEMORY_STORE_CAP_BYTES = 256 * 1024 * 1024


class MemoryStore:
    def __init__(self, cap_bytes: int = MEMORY_STORE_CAP_BYTES) -> None:
        self._cap = cap_bytes
        self._lock = threading.Lock()
        self._blobs: dict[str, bytes] = {}
        self._meta: dict[str, ArtifactInfo] = {}
        self._total_bytes = 0

    def put(self, info: ArtifactInfo, payload: bytes) -> bool:
        with self._lock:
            if self._total_bytes + len(payload) > self._cap:
                return False
            self._blobs[info.result_id] = payload
            self._meta[info.result_id] = info
            self._total_bytes += len(payload)
            return True

    def get(self, result_id: str) -> tuple[ArtifactInfo, bytes] | None:
        with self._lock:
            info = self._meta.get(result_id)
            blob = self._blobs.get(result_id)
            if info is None or blob is None:
                return None
            if utc_now() >= info.expires_at:
                self._remove_locked(result_id)
                return None
            return info, blob

    def remove(self, result_id: str) -> None:
        with self._lock:
            self._remove_locked(result_id)

    def _remove_locked(self, result_id: str) -> None:
        blob = self._blobs.pop(result_id, None)
        self._meta.pop(result_id, None)
        if blob is not None:
            self._total_bytes -= len(blob)

    def sweep_expired(self, now: datetime) -> int:
        removed = 0
        with self._lock:
            for result_id, info in list(self._meta.items()):
                if now >= info.expires_at:
                    self._remove_locked(result_id)
                    removed += 1
        return removed

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return self._total_bytes, len(self._blobs)
