"""File-backed temporary result artifacts (SPEC §4.7).

- Server-generated random IDs only; no path is ever derived from user input.
- Atomic writes via temp file + rename.
- Restrictive permissions where the platform supports them.
- Per-artifact and total quotas; expired artifacts are removed by the cleaner.
- Local paths are never returned across the adapter boundary.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from bloomberg_mcp.models import utc_now
from bloomberg_mcp.storage.models import ArtifactInfo

logger = logging.getLogger(__name__)

_RESULT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class FileStore:
    def __init__(self, directory: str, maximum_total_bytes: int) -> None:
        self._root = Path(directory).resolve()
        self._max_total = maximum_total_bytes
        self._lock = threading.Lock()
        self._meta: dict[str, ArtifactInfo] = {}
        self._total_bytes = 0
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, result_id: str, fmt: str) -> Path:
        if not _RESULT_ID_RE.match(result_id):
            raise ValueError("invalid result id")
        suffix = ".jsonl" if fmt == "jsonl" else (".parquet" if fmt == "parquet" else ".json")
        return self._root / f"{result_id}{suffix}"

    def put(self, info: ArtifactInfo, payload: bytes) -> bool:
        with self._lock:
            if self._total_bytes + len(payload) > self._max_total:
                return False
            path = self._path_for(info.result_id, info.format)
            fd, tmp_name = tempfile.mkstemp(dir=str(self._root), prefix=f".{info.result_id}-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                if sys.platform != "win32":
                    os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
            self._meta[info.result_id] = info
            self._total_bytes += len(payload)
            return True

    def get(self, result_id: str) -> tuple[ArtifactInfo, bytes] | None:
        with self._lock:
            info = self._meta.get(result_id)
            if info is None:
                return None
            if utc_now() >= info.expires_at:
                self._remove_locked(result_id)
                return None
            try:
                payload = self._path_for(result_id, info.format).read_bytes()
            except OSError:
                return None
            return info, payload

    def read_lines(self, result_id: str, start: int, count: int) -> tuple[ArtifactInfo, list[str]] | None:
        entry = self.get(result_id)
        if entry is None:
            return None
        info, payload = entry
        lines = payload.decode("utf-8").splitlines()
        return info, lines[start : start + count]

    def remove(self, result_id: str) -> None:
        with self._lock:
            self._remove_locked(result_id)

    def _remove_locked(self, result_id: str) -> None:
        info = self._meta.pop(result_id, None)
        if info is None:
            return
        self._total_bytes = max(0, self._total_bytes - info.byte_count)
        try:
            self._path_for(result_id, info.format).unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.debug("artifact removal failed for %s", result_id, exc_info=True)

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
            return self._total_bytes, len(self._meta)

    def is_ready(self) -> bool:
        return os.access(self._root, os.W_OK)
