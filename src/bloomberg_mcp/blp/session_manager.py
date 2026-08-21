"""Bloomberg session lifecycle (SPEC §2.6).

Single long-lived consumer session on ``127.0.0.1:8194`` with exponential
backoff reconnection, generation tracking and service reopening. In-flight
requests are never replayed automatically.

Native methods used (blpapi 3.26.7.1): ``SessionOptions.setServerHost``,
``SessionOptions.setServerPort``, ``SessionOptions.setConnectTimeout``,
``Session(options, eventHandler)``, ``Session.start``, ``Session.stop``,
``Session.openService``, ``Session.getService``, ``Session.isValid``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import blpapi

from bloomberg_mcp.config import BloombergConfig
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import SessionState

if TYPE_CHECKING:
    from bloomberg_mcp.blp.subscription_dispatcher import SubscriptionDispatcher

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        config: BloombergConfig,
        dispatcher: SubscriptionDispatcher,
        on_generation_change: Callable[[int], Any] | None = None,
    ) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._on_generation_change = on_generation_change
        self._session: blpapi.Session | None = None
        self._state = SessionState.STOPPED
        self._generation = 0
        self._opened_services: set[str] = set()
        self._configured_services: set[str] = set(config.startup_services)
        self._lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_session_status: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------- properties

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def session(self) -> blpapi.Session | None:
        return self._session

    @property
    def last_session_status(self) -> str:
        return self._last_session_status

    def opened_services(self) -> set[str]:
        return set(self._opened_services)

    def configured_services(self) -> set[str]:
        return set(self._configured_services)

    def record_session_status(self, text: str) -> None:
        self._last_session_status = text

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            self._stopping = False
            self._state = SessionState.STARTING
            await self._transition(services_required=True)

    async def _transition(self, *, services_required: bool) -> None:
        """Single generation-transition coordinator (finding N5/N1-N4).

        Owns the complete transition — old-session teardown, CONNECTING,
        native start, generation bump, service reopening, CONNECTED — and
        emits exactly one generation-change notification per transition.
        Startup and reconnect share this path so the two can never drift.

        ``services_required=True`` (startup): any service failure aborts the
        transition and the session is never published as CONNECTED
        (findings N2/N3). ``services_required=False`` (reconnect): a flaky
        service logs a warning and the transition completes — a single
        optional-service failure must not stall reconnection (finding N4).
        """
        session = self._session
        if session is not None:
            try:
                await asyncio.to_thread(session.stop)
            except Exception:
                logger.debug("old session stop failed", exc_info=True)
            self._session = None
        self._opened_services.clear()
        self._last_session_status = ""
        self._state = SessionState.CONNECTING
        await asyncio.to_thread(self._start_native)
        self._generation += 1
        for service in self._configured_services:
            try:
                await self._open_native_service(service, required=services_required)
            except Exception:
                if services_required:
                    self._state = SessionState.FAILED
                    raise
                logger.warning("service %s did not reopen after reconnect; continuing", service)
        self._state = SessionState.CONNECTED
        if self._on_generation_change:
            self._on_generation_change(self._generation)
        logger.info("bloomberg session connected (generation %d)", self._generation)

    def _start_native(self) -> None:
        options = blpapi.SessionOptions()
        options.setServerHost(self._config.host)
        options.setServerPort(self._config.port)
        options.setConnectTimeout(self._config.connect_timeout_seconds * 1000)
        session = blpapi.Session(options, self._dispatcher.handle_event)
        self._dispatcher.attach_session_manager(self)
        if not session.start():
            detail = self._last_session_status.lower()
            if "not logged on" in detail or "login" in detail or "desktop" in detail:
                raise GatewayError(
                    ErrorCode.BLOOMBERG_TERMINAL_NOT_LOGGED_IN,
                    "Bloomberg Terminal is not logged on.",
                    details={"session_status": self._last_session_status[:500]},
                )
            raise GatewayError(
                ErrorCode.BLOOMBERG_SESSION_FAILED,
                "Failed to start Bloomberg session.",
                details={"session_status": self._last_session_status[:500]},
            )
        self._session = session

    async def stop(self) -> None:
        async with self._lock:
            self._stopping = True
            if self._reconnect_task is not None:
                self._reconnect_task.cancel()
                self._reconnect_task = None
            self._state = SessionState.STOPPING
            session = self._session
            if session is not None:
                try:
                    await asyncio.to_thread(session.stop)
                except Exception:
                    logger.exception("error stopping bloomberg session")
                self._session = None
            self._opened_services.clear()
            self._state = SessionState.STOPPED

    # ---------------------------------------------------------------- services

    async def open_service(self, name: str) -> blpapi.Service:
        async with self._lock:
            return await self._open_native_service(name, required=True)

    async def _open_native_service(self, name: str, *, required: bool) -> blpapi.Service:
        session = self._session
        if session is None:
            raise GatewayError(ErrorCode.BLOOMBERG_NOT_CONNECTED, "Session not started.", retryable=True)
        if name in self._opened_services:
            return await asyncio.to_thread(session.getService, name)
        opened = await asyncio.to_thread(session.openService, name)
        if not opened:
            if required:
                raise GatewayError(ErrorCode.BLOOMBERG_SERVICE_OPEN_FAILED, f"Could not open service {name!r}.")
            raise GatewayError(ErrorCode.BLOOMBERG_SERVICE_NOT_OPEN, f"Service {name!r} is not open.")
        self._opened_services.add(name)
        return await asyncio.to_thread(session.getService, name)

    # -------------------------------------------------------------- reconnect

    def notify_session_down(self) -> None:
        """Called from the dispatcher thread when Bloomberg reports a loss."""
        if self._stopping or not self._config.reconnect.enabled:
            self._state = SessionState.FAILED
            return
        self._state = SessionState.RECONNECTING
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._schedule_reconnect)

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.get_running_loop().create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        cfg = self._config.reconnect
        delay = cfg.initial_delay_seconds
        while not self._stopping:
            jitter = 1.0 + random.uniform(-cfg.jitter, cfg.jitter)
            await asyncio.sleep(delay * jitter)
            try:
                await self._reconnect_once()
                return
            except Exception:
                logger.exception("reconnect attempt failed; backing off")
            delay = min(delay * cfg.multiplier, cfg.maximum_delay_seconds)

    async def _reconnect_once(self) -> None:
        async with self._lock:
            await self._transition(services_required=False)
