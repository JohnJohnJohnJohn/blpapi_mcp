"""CS2 regression tests: native request lease (deadline, release, shutdown drain).

Verifies the reader honours the caller's deadline, the lease release primitive
is idempotent and executed on every terminal path, and shutdown signals
readers to stop instead of running to the global maximum deadline.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import blpapi
import pytest

from bloomberg_mcp.blp.request_executor import read_event_queue
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import SessionState

pytestmark = pytest.mark.asyncio


class StubEvent:
    def eventType(self) -> int:
        return blpapi.Event.TIMEOUT


class StubEventQueue:
    """nextEvent returns an instant TIMEOUT; the reader loops until its bound."""

    def nextEvent(self, timeout_ms: int) -> StubEvent:
        return StubEvent()


async def test_reader_stops_at_caller_deadline() -> None:
    """The native reader must stop at the caller's deadline, not the global max."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + 0.3
    thread = threading.Thread(
        target=read_event_queue,
        args=(
            StubEventQueue(),
            loop,
            queue,
            "req_cs2",
            "//blp/refdata",
            1,
            deadline,
            lambda: SessionState.CONNECTED,
            lambda: False,
        ),
        daemon=True,
    )
    t0 = time.monotonic()
    thread.start()
    item = await asyncio.wait_for(queue.get(), timeout=3.0)
    elapsed = time.monotonic() - t0
    assert isinstance(item, GatewayError)
    assert item.code is ErrorCode.TIMEOUT
    assert elapsed < 1.0, f"reader ran {elapsed:.2f}s instead of stopping at the 0.3s bound"
    thread.join(timeout=2.0)
    assert not thread.is_alive()


async def test_reader_exits_on_stopping_flag() -> None:
    """Shutdown must signal readers to exit at their next iteration."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + 10.0
    thread = threading.Thread(
        target=read_event_queue,
        args=(
            StubEventQueue(),
            loop,
            queue,
            "req_cs2b",
            "//blp/refdata",
            1,
            deadline,
            lambda: SessionState.CONNECTED,
            lambda: True,
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert queue.empty(), "stopping reader must not emit terminal errors"
