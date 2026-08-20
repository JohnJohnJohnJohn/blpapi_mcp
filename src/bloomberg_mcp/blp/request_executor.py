"""Per-request EventQueue execution worker (SPEC §2.5).

Runs on a bounded worker thread: reads one request's native EventQueue until
final response, cancellation, deadline or session failure. Every event is
decoded into canonical messages *before* leaving the worker; only canonical
models (or a terminal :class:`GatewayError`) are handed to the async layer.

Native methods used (blpapi 3.26.7.1): ``EventQueue.nextEvent``,
``Event.eventType``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import blpapi

from bloomberg_mcp.blp.event_decoder import decode_event
from bloomberg_mcp.errors import ErrorCode, GatewayError
from bloomberg_mcp.models import CanonicalMessage, EventKind, SessionState

logger = logging.getLogger(__name__)

EntitlementCallback = Callable[[], None]


def read_event_queue(
    event_queue: blpapi.EventQueue,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[Any],
    request_id: str,
    service: str,
    generation: int,
    deadline: float,
    session_state: Callable[[], SessionState],
    on_entitlement_failure: EntitlementCallback | None,
) -> None:
    sequence = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _put(loop, queue, GatewayError(ErrorCode.TIMEOUT, "Bloomberg request timed out.", retryable=True))
                return
            timeout_ms = int(min(remaining, 1.0) * 1000)
            event = event_queue.nextEvent(timeout_ms)
            if event.eventType() == blpapi.Event.TIMEOUT:
                continue
            messages = decode_event(
                event,
                request_id=request_id,
                service=service,
                session_generation=generation,
                start_sequence=sequence,
                typed=False,
            )
            sequence += len(messages)
            terminal: GatewayError | None = None
            for message in messages:
                if message.event_type is EventKind.REQUEST_STATUS:
                    terminal = request_status_error(message, on_entitlement_failure)
                _put(loop, queue, message if terminal is None else terminal)
                if message.event_type is EventKind.RESPONSE:
                    return
            if terminal is not None:
                return
            if session_state() not in (SessionState.CONNECTED, SessionState.STARTING):
                # Session lost mid-request: fail, never replay (SPEC §2.6).
                _put(
                    loop,
                    queue,
                    GatewayError(ErrorCode.BLOOMBERG_SESSION_LOST, "Bloomberg session lost.", retryable=True),
                )
                return
    except Exception as exc:
        logger.exception("request reader failed")
        _put(
            loop,
            queue,
            GatewayError(
                ErrorCode.BLOOMBERG_REQUEST_FAILED,
                "Request stream failed.",
                details={"local_detail": str(exc)},
            ),
        )


def _put(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Any], item: Any) -> None:
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(queue.put_nowait, item)


def request_status_error(
    message: CanonicalMessage, on_entitlement_failure: EntitlementCallback | None
) -> GatewayError:
    """Map a REQUEST_STATUS message onto the stable application error set."""
    payload = message.payload
    reason = payload.get("reason") if isinstance(payload, Mapping) else None
    category = ""
    description = str(message.message_type)
    if isinstance(reason, Mapping):
        category = str(reason.get("category") or "")
        description = str(reason.get("description") or description)
    if category == "NO_AUTH":
        if on_entitlement_failure is not None:
            on_entitlement_failure()
        return GatewayError(
            ErrorCode.BLOOMBERG_NOT_ENTITLED,
            "Not entitled to the requested data.",
            details={"category": category},
        )
    if message.message_type == "RequestFailure":
        return GatewayError(
            ErrorCode.BLOOMBERG_REQUEST_FAILED,
            description[:500],
            details={"category": category},
        )
    return GatewayError(ErrorCode.BLOOMBERG_RESPONSE_ERROR, description[:500])
