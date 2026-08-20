"""Single-instance enforcement (SPEC §1.5, §4.13).

On Windows a named mutex prevents more than one gateway process from running.
Elsewhere (developer machines) a loopback socket bind provides the same
guarantee.
"""

from __future__ import annotations

import logging
import socket
import sys

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\BloombergMCP.SingleInstance"
_SOCKET_PORT = 47911

_ERROR_ALREADY_EXISTS = 183


class InstanceLockHeld(RuntimeError):
    """Another gateway process already holds the instance lock."""


class InstanceLock:
    def __init__(self) -> None:
        self._mutex_handle: object | None = None
        self._socket: socket.socket | None = None

    def acquire(self) -> None:
        if sys.platform == "win32":
            self._acquire_windows()
        else:
            self._acquire_socket()

    def _acquire_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise InstanceLockHeld("CreateMutexW failed")
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise InstanceLockHeld(f"mutex {MUTEX_NAME} is already held")
        self._mutex_handle = handle
        logger.info("acquired single-instance mutex %s", MUTEX_NAME)

    def _acquire_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", _SOCKET_PORT))
        except OSError as exc:
            sock.close()
            raise InstanceLockHeld("another gateway instance is already running") from exc
        self._socket = sock

    def release(self) -> None:
        if self._mutex_handle is not None and sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
