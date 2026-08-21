"""Single-instance enforcement (SPEC §1.5, §4.13).

On Windows a named mutex prevents more than one gateway process from running.
Elsewhere (developer machines) a name-keyed ``fcntl.flock`` file lock provides
the same guarantee. The previous loopback-socket approach was broken on Linux:
``SO_REUSEADDR`` permits duplicate binds, so a second process could acquire the
"lock" silently (review finding, reproduced 2026-08-21).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TextIO

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\BloombergMCP.SingleInstance"

_ERROR_ALREADY_EXISTS = 183

_LOCK_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


class InstanceLockHeld(RuntimeError):
    """Another gateway process already holds the instance lock."""


class InstanceLock:
    def __init__(self, mutex_name: str = MUTEX_NAME) -> None:
        self._mutex_name = mutex_name
        self._mutex_handle: object | None = None
        self._lock_file: TextIO | None = None  # file object on non-Windows

    def acquire(self) -> None:
        if sys.platform == "win32":
            self._acquire_windows()
        else:
            self._acquire_file_lock()

    def _acquire_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        if not hasattr(ctypes, "windll"):  # pragma: no cover - mypy/Windows guard
            raise InstanceLockHeld("ctypes.windll unavailable on this platform")
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, self._mutex_name)
        if not handle:
            raise InstanceLockHeld("CreateMutexW failed")
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise InstanceLockHeld(f"mutex {self._mutex_name} is already held")
        self._mutex_handle = handle
        logger.info("acquired single-instance mutex %s", self._mutex_name)

    def _acquire_file_lock(self) -> None:
        """Name-keyed advisory file lock (Linux/macOS single-instance guard).

        ``flock(LOCK_EX | LOCK_NB)`` on a per-name lock file: the kernel
        serialises by open-file-description, so a second process (or a second
        acquire in the same process) fails atomically — no port binding, no
        SO_REUSEADDR duplicate-bind loophole.
        """
        import fcntl
        import os
        import tempfile

        if self._lock_file is None:
            safe = _LOCK_NAME_RE.sub("_", self._mutex_name)
            path = os.path.join(tempfile.gettempdir(), f"bloomberg_mcp_{safe}.lock")
            self._lock_file = open(path, "w")  # noqa: SIM115 - held for process lifetime
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # A failed acquire must not leak the lock file descriptor.
            self._lock_file.close()
            self._lock_file = None
            raise InstanceLockHeld(f"lock {self._mutex_name} is already held") from exc
        logger.info("acquired single-instance lock %s", self._mutex_name)

    def release(self) -> None:
        if self._mutex_handle is not None and sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None
        if self._lock_file is not None:
            import fcntl

            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.debug("instance lock release failed", exc_info=True)
            self._lock_file.close()
            self._lock_file = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
