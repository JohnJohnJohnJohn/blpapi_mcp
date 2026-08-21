"""Result store facade: memory + file-backed temporary artifacts (SPEC §3.11, §4.7)."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any

from bloomberg_mcp.config import StorageConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import utc_now
from bloomberg_mcp.storage.file_store import FileStore
from bloomberg_mcp.storage.memory_store import MemoryStore
from bloomberg_mcp.storage.models import ArtifactInfo

_CONTENT_TYPES = {
    "json": "application/json",
    "jsonl": "application/x-ndjson",
}


class ResultStore:
    def __init__(self, config: StorageConfig, *, persist_artifacts: bool = True) -> None:
        self._config = config
        self._memory = MemoryStore()
        self._file: FileStore | None = None
        if config.enabled and persist_artifacts and config.directory:
            self._file = FileStore(config.directory, config.maximum_total_bytes)
        self._index: dict[str, ArtifactInfo] = {}

    def is_ready(self) -> bool:
        if self._file is not None:
            return self._file.is_ready()
        return True

    def put(
        self,
        principal_id: str,
        representation: str,
        fmt: str,
        payload: bytes,
        message_count: int,
        ttl_seconds: int,
    ) -> ArtifactInfo:
        if fmt not in _CONTENT_TYPES:
            raise GatewayError(ErrorCode.ARTIFACT_FORMAT_NOT_AVAILABLE, f"Unsupported artifact format {fmt!r}.")
        result_id = f"res_{secrets.token_urlsafe(12)}"
        info = ArtifactInfo(
            result_id=result_id,
            principal_id=principal_id,
            representation=representation,
            format=fmt,
            content_type=_CONTENT_TYPES[fmt],
            byte_count=len(payload),
            message_count=message_count,
            sha256=hashlib.sha256(payload).hexdigest(),
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
            backend="memory",
        )
        stored = False
        if self._file is not None:
            file_info = ArtifactInfo(**{**info.__dict__, "backend": "file"})
            try:
                stored = self._file.put(file_info, payload)
            except OSError:
                stored = False
            if stored:
                self._index[result_id] = file_info
                return file_info
        if self._memory.put(info, payload):
            self._index[result_id] = info
            return info
        raise GatewayError(ErrorCode.RESPONSE_TOO_LARGE, "Result exceeds storage budgets.")

    def _owned(self, result_id: str, principal_id: str, *, admin: bool) -> ArtifactInfo:
        info = self._index.get(result_id)
        if info is None:
            raise GatewayError(ErrorCode.RESULT_NOT_FOUND, "Result not found.")
        if utc_now() >= info.expires_at:
            self._index.pop(result_id, None)
            raise GatewayError(ErrorCode.RESULT_EXPIRED, "Result has expired.")
        if info.principal_id != principal_id and not admin:
            # Cross-principal reads are denied with the same error as absence.
            raise GatewayError(ErrorCode.RESULT_NOT_FOUND, "Result not found.")
        return info

    def metadata(self, result_id: str, principal_id: str, *, admin: bool = False) -> dict[str, Any]:
        return self._owned(result_id, principal_id, admin=admin).to_dict()

    def get_bytes(self, result_id: str, principal_id: str, *, admin: bool = False) -> tuple[ArtifactInfo, bytes]:
        info = self._owned(result_id, principal_id, admin=admin)
        if info.backend == "file" and self._file is not None:
            entry = self._file.get(result_id)
        else:
            entry = self._memory.get(result_id)
        if entry is None:
            raise GatewayError(ErrorCode.RESULT_EXPIRED, "Result artifact is no longer available.")
        return entry

    def get_page(
        self, result_id: str, principal_id: str, page: int, page_size: int, *, admin: bool = False
    ) -> dict[str, Any]:
        info = self._owned(result_id, principal_id, admin=admin)
        if page < 1 or page_size < 1 or page_size > 5000:
            raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Invalid page or page size.")
        _, payload = self.get_bytes(result_id, principal_id, admin=admin)
        if info.format == "jsonl":
            lines = payload.decode("utf-8").splitlines()
            start = (page - 1) * page_size
            chunk = lines[start : start + page_size]
            items = [json.loads(line) for line in chunk if line]
            total_pages = (len(lines) + page_size - 1) // page_size if lines else 1
        elif info.format == "json":
            data = json.loads(payload.decode("utf-8"))
            if isinstance(data, list):
                start = (page - 1) * page_size
                items = data[start : start + page_size]
                total_pages = (len(data) + page_size - 1) // page_size if data else 1
            else:
                if page != 1:
                    raise GatewayError(ErrorCode.INVALID_ARGUMENT, "Single-page resource.")
                items = [data]
                total_pages = 1
        else:
            raise GatewayError(
                ErrorCode.ARTIFACT_FORMAT_NOT_AVAILABLE,
                "Tabular artifacts are delivered via the artifact endpoint, not pages.",
            )
        return {
            "result_id": result_id,
            "page": page,
            "total_pages": total_pages,
            "items": items,
            "expires_at": info.expires_at.isoformat(),
        }

    def sweep_expired(self) -> int:
        now = utc_now()
        removed = 0
        for result_id, info in list(self._index.items()):
            if now >= info.expires_at:
                self._index.pop(result_id, None)
                if info.backend == "file" and self._file is not None:
                    self._file.remove(result_id)
                else:
                    self._memory.remove(result_id)
                removed += 1
        if self._file is not None:
            removed += self._file.sweep_expired(now)
        removed += self._memory.sweep_expired(now)
        return removed

    def stats(self) -> dict[str, int]:
        file_bytes, file_count = self._file.stats() if self._file else (0, 0)
        mem_bytes, mem_count = self._memory.stats()
        return {
            "result_store_bytes": file_bytes + mem_bytes,
            "result_store_artifacts": file_count + mem_count,
        }
