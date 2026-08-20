"""Periodic cleanup of expired artifacts, requests and subscriptions (SPEC §4.7)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class CleanupTask:
    def __init__(self, interval_seconds: int, jobs: list[Callable[[], Awaitable[int] | int]]) -> None:
        self._interval = interval_seconds
        self._jobs = jobs
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            for job in self._jobs:
                try:
                    result = job()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception("cleanup job failed")
