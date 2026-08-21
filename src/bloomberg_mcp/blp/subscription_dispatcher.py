"""Asynchronous event dispatcher for subscription/session/admin events (SPEC §2.5).

A single blpapi dispatcher thread receives SESSION_STATUS, SERVICE_STATUS,
SUBSCRIPTION_STATUS, SUBSCRIPTION_DATA, TOPIC_STATUS and ADMIN events. Each
event is decoded completely into canonical models on that thread before any
hand-off; native objects never cross into the async application layer.

Per-request EventQueues are reserved for request/response operations and are
handled by the request executor instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import blpapi

from bloomberg_mcp.blp.backend import SubscriptionEvent, SubscriptionSink
from bloomberg_mcp.blp.event_decoder import canonical_event_kind, decode_sequence_element
from bloomberg_mcp.models import EventKind, utc_now

if TYPE_CHECKING:
    from bloomberg_mcp.blp.session_manager import SessionManager

logger = logging.getLogger(__name__)


class SubscriptionDispatcher:
    def __init__(self) -> None:
        self._session_manager: SessionManager | None = None
        self._sink: SubscriptionSink | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Throttle state for diagnostic logging (key -> last emit timestamp).
        self._warned_at: dict[str, float] = {}
        self._drop_count: int = 0

    def attach_session_manager(self, manager: SessionManager) -> None:
        self._session_manager = manager

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_sink(self, sink: SubscriptionSink | None) -> None:
        self._sink = sink

    # ------------------------------------------------------------ blpapi thread

    def handle_event(self, event: blpapi.Event, session: blpapi.Session) -> None:
        """Native event handler; runs on the blpapi dispatcher thread."""
        event_type = event.eventType()
        try:
            if event_type == blpapi.Event.SESSION_STATUS:
                self._handle_session_status(event)
            elif event_type in (
                blpapi.Event.SUBSCRIPTION_DATA,
                blpapi.Event.SUBSCRIPTION_STATUS,
                blpapi.Event.SERVICE_STATUS,
                blpapi.Event.TOPIC_STATUS,
                blpapi.Event.ADMIN,
                blpapi.Event.RESOLUTION_STATUS,
            ):
                self._handle_canonical_events(event)
        except Exception:
            logger.exception("dispatcher failed to decode a native event")

    def _handle_session_status(self, event: blpapi.Event) -> None:
        manager = self._session_manager
        for message in event:
            text = str(message.messageType())
            try:
                payload = decode_sequence_element(message.asElement(), typed=False)
            except Exception:
                payload = {}
            description = payload.get("description") if isinstance(payload, dict) else None
            if isinstance(description, str) and manager is not None:
                manager.record_session_status(description)
            logger.info("session status: %s", text)
            if text in ("SessionTerminated", "ConnectionDown") and manager is not None:
                manager.notify_session_down()
            self._post_session_status(text, payload)

    def _post_session_status(self, message_type: str, payload: Any) -> None:
        if self._sink is None or self._loop is None:
            return
        event = SubscriptionEvent(
            native_token=0,
            kind=EventKind.SESSION_STATUS,
            message_type=message_type,
            payload=payload if isinstance(payload, dict) else {},
            received_at=utc_now().isoformat(),
            status=message_type,
        )
        self._loop.call_soon_threadsafe(self._enqueue, event)

    def _handle_canonical_events(self, event: blpapi.Event) -> None:
        kind = canonical_event_kind(event.eventType())
        if self._sink is None or self._loop is None:
            return
        for message in event:
            tokens = _extract_tokens(message)
            if not tokens:
                self._drop_count += 1
                now = time.monotonic()
                key = f"drop:{kind.value}:{message.messageType()}"
                last = self._warned_at.get(key, 0.0)
                if now - last >= 10.0:
                    self._warned_at[key] = now
                    raw_cids = [repr(cid.value()) for cid in message.correlationIds()] or ["<none>"]
                    logger.warning(
                        "dropped %d native subscription event(s) (%s, message_type=%s) with unroutable "
                        "correlation id %s; data is arriving but cannot be matched to an item",
                        self._drop_count,
                        kind.value,
                        message.messageType(),
                        ",".join(raw_cids[:8]),
                    )
                continue
            payload = decode_sequence_element(message.asElement(), typed=False)
            status, error_code, error_message = _extract_status(payload)
            subscription_event = SubscriptionEvent(
                native_token=tokens[0] if tokens else 0,
                kind=kind,
                message_type=str(message.messageType()),
                payload=payload,
                received_at=utc_now().isoformat(),
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
            self._loop.call_soon_threadsafe(self._enqueue, subscription_event)

    def _enqueue(self, event: SubscriptionEvent) -> None:
        sink = self._sink
        if sink is None:
            return
        asyncio.ensure_future(sink(event))


def _extract_tokens(message: blpapi.Message) -> list[int]:
    """Correlation-id tokens from a native message, type-agnostic.

    Live observation (2026-08-21): MarketDataEvents carry correlation ids that
    are not INT_TYPE even though the subscription was registered with integer
    tokens; filtering by type silently dropped every subscription event. Accept
    any id whose value coerces to int (INT ids pass through unchanged).
    """
    tokens: list[int] = []
    for cid in message.correlationIds():
        try:
            tokens.append(int(cid.value()))
        except (TypeError, ValueError):
            continue
    return tokens


def _extract_status(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Pull subscription status fields out of a decoded status message."""
    if not isinstance(payload, dict):
        return None, None, None
    reason = payload.get("reason")
    status = None
    code: str | None = None
    message: str | None = None
    if isinstance(reason, dict):
        category = reason.get("category")
        description = reason.get("description")
        code = str(category) if isinstance(category, str) else None
        message = str(description) if isinstance(description, str) else None
    for key in ("isEntitled", "reason"):
        if key in payload and isinstance(payload[key], dict):
            inner = payload[key]
            if isinstance(inner.get("category"), str):
                code = inner["category"]
            if isinstance(inner.get("description"), str):
                message = inner["description"]
    if isinstance(payload.get("status"), str):
        status = payload["status"]
    return status, code, message
